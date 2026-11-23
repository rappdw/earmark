"""A hand-written SMTP server for the tests, run in a thread over ``socket.socketpair()``.

No port is ever opened: ``SmtpStub.socket_factory`` hands the client one end of a socketpair
and serves the other. TLS is real (STARTTLS or implicit), with the test-only certificate in
``tests/fixtures/tls`` for the name ``smtp.test``. Commands are matched without regard to case
(``smtplib`` sends ``ehlo`` and ``mail from:`` in lower case).

The default replies carry ``REPLY_SENTINEL`` and a recipient address in their text, as real
servers echo addresses, so the leak tests can check that neither reaches a job error.
"""

import base64
import contextlib
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

TLS_DIR: Final = Path(__file__).parent / "fixtures" / "tls"
REPLY_SENTINEL: Final = "REPLY-SENTINEL-7c1d"
ECHOED_ADDRESS: Final = "pka@example.net"
_ECHO: Final = f"<{REPLY_SENTINEL}> for {ECHOED_ADDRESS}"

DisconnectAt = Literal["greeting", "mail", "data_command", "mid_payload", "after_payload"]


def trusted_test_context() -> ssl.SSLContext:
    """A client context that trusts only the test CA. Verification stays on."""
    return ssl.create_default_context(cadata=(TLS_DIR / "ca.pem").read_text())


class RecordingContextFactory:
    """``trusted_test_context`` that keeps every context it hands out."""

    def __init__(self) -> None:
        self.contexts: list[ssl.SSLContext] = []

    def __call__(self) -> ssl.SSLContext:
        context = trusted_test_context()
        self.contexts.append(context)
        return context


def _server_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS_DIR / "server.pem", TLS_DIR / "server.key")
    return context


class _Lines:
    """A line reader over a socket (efficient for megabytes of data lines)."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._pos = 0

    def readline(self) -> bytes | None:
        while True:
            end = self._buf.find(b"\n", self._pos)
            if end >= 0:
                line = bytes(self._buf[self._pos : end]).rstrip(b"\r")
                self._pos = end + 1
                if self._pos > 1 << 20:
                    del self._buf[: self._pos]
                    self._pos = 0
                return line
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                return None
            self._buf += chunk


@dataclass
class SmtpStub:
    greeting: str = "220 smtp.test ESMTP ready"
    offer_starttls: bool = True
    implicit_tls: bool = False
    auth_mechanisms: str | None = "PLAIN LOGIN"
    ehlo_reply_code: int = 250
    auth_reply: str = f"235 2.7.0 Authentication successful {_ECHO}"
    mail_reply: str = f"250 2.1.0 sender ok {_ECHO}"
    rcpt_reply: str = f"250 2.1.5 recipient ok {_ECHO}"
    data_reply: str = f"354 go ahead {_ECHO}"
    final_reply: str = f"250 2.0.0 queued {_ECHO}"
    disconnect_at: DisconnectAt | None = None
    stall_after_payload: bool = False
    drip_s: float | None = None
    # recorders
    connections: int = 0
    ehlo_names: list[str] = field(default_factory=list)
    commands: list[tuple[bool, str]] = field(default_factory=list)  # (over TLS, VERB)
    auth: list[tuple[str, str, bool]] = field(default_factory=list)  # (user, password, over TLS)
    mail_from: list[str] = field(default_factory=list)
    rcpt_to: list[str] = field(default_factory=list)
    messages: list[bytes] = field(default_factory=list)  # un-dot-stuffed
    _threads: list[threading.Thread] = field(default_factory=list, repr=False)
    _release: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- client side ------------------------------------------------------------------

    def socket_factory(self, host: str, port: int, timeout: float) -> socket.socket:
        """A ``SocketFactory``: a connected socketpair end, served by a daemon thread."""
        client, server = socket.socketpair()
        client.settimeout(timeout)
        self.connections += 1
        thread = threading.Thread(target=self._serve, args=(server,), daemon=True)
        self._threads.append(thread)
        thread.start()
        return client

    @property
    def verbs(self) -> list[str]:
        return [verb for _, verb in self.commands]

    def join(self, timeout: float = 5.0) -> None:
        """Release a stalled server and wait for every connection to finish."""
        self._release.set()
        for thread in self._threads:
            thread.join(timeout)

    def close(self) -> None:
        self.join()

    # -- server side ------------------------------------------------------------------

    def _serve(self, conn: socket.socket) -> None:
        sock = conn
        try:
            tls = self.implicit_tls
            if tls:
                sock = _server_context().wrap_socket(conn, server_side=True)
            if self.disconnect_at == "greeting":
                return
            self._write(sock, self.greeting)
            lines = _Lines(sock)
            while (line := lines.readline()) is not None:
                text = line.decode("ascii", "replace")
                verb, _, rest = text.partition(" ")
                verb = verb.upper()
                self.commands.append((tls, verb))
                if verb in {"EHLO", "HELO"}:
                    self._ehlo(sock, rest, tls)
                elif verb == "STARTTLS":
                    self._write(sock, "220 2.0.0 Ready to start TLS")
                    sock = _server_context().wrap_socket(sock, server_side=True)
                    lines = _Lines(sock)
                    tls = True
                elif verb == "AUTH":
                    self._auth(sock, lines, rest, tls)
                elif verb == "MAIL":
                    if self.disconnect_at == "mail":
                        return
                    self.mail_from.append(_between_angles(rest))
                    self._write(sock, self.mail_reply)
                elif verb == "RCPT":
                    self.rcpt_to.append(_between_angles(rest))
                    self._write(sock, self.rcpt_reply)
                elif verb == "DATA":
                    if self.disconnect_at == "data_command":
                        return
                    self._write(sock, self.data_reply)
                    if not self.data_reply.startswith("354"):
                        continue
                    if not self._data(sock, lines):
                        return
                elif verb == "QUIT":
                    self._write(sock, "221 2.0.0 Bye")
                    return
                else:
                    self._write(sock, "500 5.5.2 command not recognised")
        except OSError:
            pass  # the client hung up, or the stub closed on purpose
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    def _ehlo(self, sock: socket.socket, name: str, tls: bool) -> None:
        self.ehlo_names.append(name.strip())
        if self.ehlo_reply_code != 250:
            self._write(sock, f"{self.ehlo_reply_code} 5.5.1 EHLO refused {_ECHO}")
            return
        extensions = ["smtp.test greets you"]
        if self.offer_starttls and not tls:
            extensions.append("STARTTLS")
        if self.auth_mechanisms is not None and tls:
            extensions.append(f"AUTH {self.auth_mechanisms}")
        extensions.append("8BITMIME")
        for ext in extensions[:-1]:
            self._write(sock, f"250-{ext}")
        self._write(sock, f"250 {extensions[-1]}")

    def _auth(self, sock: socket.socket, lines: _Lines, rest: str, tls: bool) -> None:
        mechanism, _, initial = rest.partition(" ")
        if mechanism.upper() == "PLAIN":
            if not initial:
                self._write(sock, "334 ")
                initial = (lines.readline() or b"").decode("ascii", "replace")
            _, user, password = base64.b64decode(initial).decode().split("\0")
        else:  # LOGIN; smtplib sends the user name as the initial response
            if initial:
                user = base64.b64decode(initial).decode()
            else:
                self._write(sock, "334 VXNlcm5hbWU6")
                user = base64.b64decode(lines.readline() or b"").decode()
            self._write(sock, "334 UGFzc3dvcmQ6")
            password = base64.b64decode(lines.readline() or b"").decode()
        self.auth.append((user, password, tls))
        self._write(sock, self.auth_reply.replace("{password}", password).replace("{user}", user))

    def _data(self, sock: socket.socket, lines: _Lines) -> bool:
        """Read the message. False when the stub should hang up."""
        out: list[bytes] = []
        while (line := lines.readline()) is not None:
            if line == b".":
                break
            out.append(line[1:] if line.startswith(b"..") else line)
            if self.disconnect_at == "mid_payload":
                return False
        else:
            return False
        self.messages.append(b"".join(piece + b"\r\n" for piece in out))
        if self.disconnect_at == "after_payload":
            return False
        if self.stall_after_payload:
            self._release.wait(30)
            return False
        if self.drip_s is not None:
            for byte in (self.final_reply + "\r\n").encode():
                sock.sendall(bytes([byte]))
                time.sleep(self.drip_s)
            return True
        self._write(sock, self.final_reply)
        return True

    @staticmethod
    def _write(sock: socket.socket, line: str) -> None:
        sock.sendall(line.encode("ascii", "replace") + b"\r\n")


def _between_angles(text: str) -> str:
    _, _, tail = text.partition("<")
    return tail.partition(">")[0]


def stub_factory() -> Iterator[Callable[..., SmtpStub]]:
    """The body of the ``make_stub`` fixture: ``make_stub = pytest.fixture(stub_factory)``.

    ``make_stub(**knobs)`` builds an in-process SMTP server; every one is released at teardown.
    (It is not decorated here, because ``tests/conftest.py`` is copied into pytester runs.)
    """
    stubs: list[SmtpStub] = []

    def make(**knobs: object) -> SmtpStub:
        stub = SmtpStub(**knobs)  # type: ignore[arg-type]  # keyword passthrough
        stubs.append(stub)
        return stub

    yield make
    for stub in stubs:
        stub.close()
