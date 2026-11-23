"""The ``smtp`` mail backend, built on the standard library's ``smtplib``.

Import this module only from the factory's builder and from doctor.

earmark drives the SMTP transaction itself (``mail``, ``rcpt``, ``DATA``, the payload, the
final reply) instead of calling ``sendmail``, because ``sendmail`` cannot tell a loss before
the end of the data from a loss while waiting for the final reply, and the two need
different outcomes (docs/steps/25-delivery.md 2.1).

Every client is constructed with its host and port, so ``server_hostname`` is set for TLS,
and with an explicit ``local_hostname``, so no host-name lookup happens. TLS always
verifies the certificate and the host name. Nothing logs or raises the server's reply text,
which echoes addresses: errors made from ``smtplib`` exceptions are raised ``from None``.
"""

import contextlib
import logging
import re
import smtplib
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Self

from pydantic import SecretStr

from earmark.config import ConfigError, Settings, SmtpSecurity, env_var
from earmark.errors import EarmarkError, MaybeDelivered, PermanentError, TransientError
from earmark.pipeline.stages import OutgoingMail

logger = logging.getLogger(__name__)

type SocketFactory = Callable[[str, int, float], socket.socket]  # (host, port, timeout)
type ContextFactory = Callable[[], ssl.SSLContext]

DEFAULT_CONTEXT_FACTORY: Final[ContextFactory] = ssl.create_default_context
_ENHANCED: Final = re.compile(rb" *([245])\.(\d{1,3})\.(\d{1,3})(?![0-9])")
_DOT_LINE: Final = re.compile(rb"(?m)^\.")

_BEFORE_SEND: Final = "before the message was sent"
_DURING_CHECK: Final = "during the login check"


def enhanced_status(reply: bytes) -> str | None:
    """The enhanced status code (``5.1.1``) a reply starts with, or None."""
    found = _ENHANCED.match(reply)
    if found is None:
        return None
    return f"{found.group(1).decode()}.{found.group(2).decode()}.{found.group(3).decode()}"


@dataclass(frozen=True, slots=True)
class SmtpOptions:
    host: str
    port: int
    security: SmtpSecurity
    username: str
    password: SecretStr
    sender: str
    timeout_s: float

    @property
    def local_hostname(self) -> str:
        return self.sender.rpartition("@")[2]

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """``ConfigError`` naming every missing one of the four required variables."""
        s = settings
        missing = [
            env_var(name)
            for name, value in (
                ("smtp_host", s.smtp_host),
                ("smtp_from", s.smtp_from),
                ("smtp_username", s.smtp_username),
                ("smtp_password", s.smtp_password),
            )
            if value is None
        ]
        if missing:
            raise ConfigError(
                [f"{name}: required when EARMARK_MAIL_BACKEND=smtp" for name in missing]
            )
        if (
            s.smtp_host is None
            or s.smtp_from is None
            or s.smtp_username is None
            or s.smtp_password is None
        ):  # unreachable: `missing` was empty
            raise ConfigError(["EARMARK_SMTP_HOST: required when EARMARK_MAIL_BACKEND=smtp"])
        return cls(
            host=s.smtp_host,
            port=s.smtp_port_effective,
            security=s.smtp_security,
            username=s.smtp_username,
            password=s.smtp_password,
            sender=s.smtp_from,
            timeout_s=s.smtp_timeout_s,
        )


@dataclass(frozen=True, slots=True)
class LoginReport:
    tls_version: str  # for example "TLSv1.3"


class _Deadline:
    """A whole-session deadline, enforced by shutting the socket down from a timer thread.

    Socket timeouts bound each blocking call, not the session: a server that drips one byte
    per interval never times out. The timer shuts down a duplicate of the socket's file
    descriptor (``socket.fromfd``), which ends a blocked read at once. It never touches the
    ``SSLSocket`` object itself, which is not safe to use from two threads.
    """

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self._end = time.monotonic() + timeout_s
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._fd = -1
        self._family = socket.AF_INET
        self._armed = False
        self.expired = False

    def arm(self, sock: socket.socket) -> None:
        with self._lock:
            self._fd = sock.fileno()
            self._family = sock.family
            self._armed = True
            timer = threading.Timer(max(0.0, self._end - time.monotonic()), self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            if not self._armed:
                return
            self.expired = True
            with contextlib.suppress(OSError):
                duplicate = socket.fromfd(self._fd, self._family, socket.SOCK_STREAM)
                try:
                    duplicate.shutdown(socket.SHUT_RDWR)
                finally:
                    duplicate.close()

    def disarm(self) -> None:
        """Call before closing the client, so the descriptor cannot be reused under the timer."""
        with self._lock:
            self._armed = False
            timer = self._timer
        if timer is not None:
            timer.cancel()


def _raw_socket(
    factory: SocketFactory | None, deadline: _Deadline, host: str, port: int, timeout: float
) -> socket.socket:
    if factory is None:
        sock = socket.create_connection((host, port), timeout)
    else:
        sock = factory(host, port, timeout)
    deadline.arm(sock)
    return sock


class _Client(smtplib.SMTP):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        local_hostname: str,
        timeout: float,
        factory: SocketFactory | None,
        deadline: _Deadline,
    ) -> None:
        self._factory = factory
        self._deadline = deadline
        super().__init__(host, port, local_hostname=local_hostname, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return _raw_socket(self._factory, self._deadline, host, port, timeout)


class _TlsClient(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        local_hostname: str,
        timeout: float,
        context: ssl.SSLContext,
        factory: SocketFactory | None,
        deadline: _Deadline,
    ) -> None:
        self._factory = factory
        self._deadline = deadline
        self._server_name = host
        super().__init__(
            host, port, local_hostname=local_hostname, timeout=timeout, context=context
        )

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw = _raw_socket(self._factory, self._deadline, host, port, timeout)
        try:
            return self.context.wrap_socket(raw, server_hostname=self._server_name)
        except BaseException:
            raw.close()
            raise


@dataclass
class _Progress:
    phase: str = "the connection"
    payload_sent: bool = False


class SmtpMailer:
    """The ``smtp`` Mailer: one connection per message, closed afterwards."""

    def __init__(
        self,
        options: SmtpOptions,
        *,
        socket_factory: SocketFactory | None = None,
        context_factory: ContextFactory = DEFAULT_CONTEXT_FACTORY,
    ) -> None:
        self._options = options
        self._socket_factory = socket_factory
        self._context_factory = context_factory

    @property
    def sender(self) -> str:
        return self._options.sender

    @property
    def options(self) -> SmtpOptions:
        return self._options

    def close(self) -> None:
        """No-op: there is no pooled connection."""

    # -- public -----------------------------------------------------------------------

    def send(self, mail: OutgoingMail, /) -> None:
        opts = self._options
        deadline = _Deadline(opts.timeout_s)
        progress = _Progress()
        client: smtplib.SMTP | None = None
        try:
            context = self._context_factory()
            client = self._open(deadline, context)
            self._handshake(client, progress, context)
            self._transaction(client, mail, progress, deadline)
        except EarmarkError:
            raise
        except (smtplib.SMTPException, OSError) as exc:
            raise self._classify(exc, progress, deadline, _BEFORE_SEND) from None
        finally:
            deadline.disarm()
            _close(client)
        logger.info("smtp: message for job %s accepted by %s", mail.job_id, opts.host)

    def check_login(self, *, timeout_s: float | None = None) -> LoginReport:
        """Connect, TLS, EHLO, AUTH, then QUIT. Sends no MAIL command."""
        deadline = _Deadline(self._options.timeout_s if timeout_s is None else timeout_s)
        progress = _Progress()
        client: smtplib.SMTP | None = None
        try:
            context = self._context_factory()
            client = self._open(deadline, context)
            self._handshake(client, progress, context)
            sock = client.sock
            version = sock.version() if isinstance(sock, ssl.SSLSocket) else None
            self._quit(client)
        except EarmarkError:
            raise
        except (smtplib.SMTPException, OSError) as exc:
            raise self._classify(exc, progress, deadline, _DURING_CHECK) from None
        finally:
            deadline.disarm()
            _close(client)
        return LoginReport(tls_version=version or "unknown")

    # -- the session ------------------------------------------------------------------

    def _open(self, deadline: _Deadline, context: ssl.SSLContext) -> smtplib.SMTP:
        """Connect and read the greeting. For ``tls`` the TLS handshake happens here."""
        opts = self._options
        if opts.security == "tls":
            return _TlsClient(
                opts.host,
                opts.port,
                local_hostname=opts.local_hostname,
                timeout=deadline.timeout_s,
                context=context,
                factory=self._socket_factory,
                deadline=deadline,
            )
        return _Client(
            opts.host,
            opts.port,
            local_hostname=opts.local_hostname,
            timeout=deadline.timeout_s,
            factory=self._socket_factory,
            deadline=deadline,
        )

    def _handshake(
        self, client: smtplib.SMTP, progress: _Progress, context: ssl.SSLContext
    ) -> None:
        """EHLO, STARTTLS when asked, EHLO over TLS, then AUTH (only ever after TLS)."""
        opts = self._options
        if opts.security == "starttls":
            progress.phase = "EHLO"
            code, reply = client.ehlo()
            self._expect(code, reply, (250,), "EHLO")
            if not client.has_extn("starttls"):
                raise PermanentError(
                    f"the SMTP server at {opts.host} does not offer STARTTLS; earmark never "
                    "sends in plaintext (check EARMARK_SMTP_SECURITY and EARMARK_SMTP_PORT)"
                )
            progress.phase = "STARTTLS"
            client.starttls(context=context)
        progress.phase = "EHLO"
        code, reply = client.ehlo()
        self._expect(code, reply, (250,), "EHLO")
        progress.phase = "the login"
        client.login(opts.username, opts.password.get_secret_value())

    def _transaction(
        self, client: smtplib.SMTP, mail: OutgoingMail, progress: _Progress, deadline: _Deadline
    ) -> None:
        progress.phase = "the sender"
        code, reply = client.mail(mail.sender)
        self._expect(code, reply, (250,), "the sender")
        progress.phase = "the recipient"
        code, reply = client.rcpt(mail.recipient)
        self._expect(code, reply, (250, 251), "the recipient")
        progress.phase = "DATA"
        code, reply = client.docmd("DATA")
        self._expect(code, reply, (354,), "DATA")
        progress.phase = "the message"
        payload = _DOT_LINE.sub(b"..", mail.data)
        if not payload.endswith(b"\r\n"):
            payload += b"\r\n"
        client.send(payload + b".\r\n")
        progress.payload_sent = True
        code, reply = client.getreply()
        if 200 <= code < 300:
            self._quit(client)
            return
        if 400 <= code < 600:
            raise self._reply_error("the message", code, reply)
        raise self._maybe_delivered(deadline)

    @staticmethod
    def _quit(client: smtplib.SMTP) -> None:
        with contextlib.suppress(smtplib.SMTPException, OSError):
            client.quit()

    # -- errors -----------------------------------------------------------------------

    def _expect(self, code: int, reply: bytes | str, ok: tuple[int, ...], phase: str) -> None:
        if code not in ok:
            raise self._reply_error(phase, code, reply)

    def _reply_error(self, phase: str, code: int, reply: bytes | str) -> EarmarkError:
        host = self._options.host
        enhanced = enhanced_status(reply if isinstance(reply, bytes) else reply.encode())
        detail = f"reply {code}" + (f", {enhanced}" if enhanced else "")
        message = f"the SMTP server at {host} refused {phase} ({detail})"
        if 500 <= code < 600:
            return PermanentError(message)
        return TransientError(message)

    def _maybe_delivered(self, deadline: _Deadline) -> MaybeDelivered:
        host = self._options.host
        if deadline.expired:
            what = (
                f"did not finish within EARMARK_SMTP_TIMEOUT_S ({deadline.timeout_s:g} s) "
                "and did not confirm the message"
            )
        else:
            what = "did not confirm the message"
        return MaybeDelivered(
            f"the SMTP server at {host} {what}; it will be sent again, "
            "so the recipient may get it twice"
        )

    def _classify(
        self,
        exc: smtplib.SMTPException | OSError,
        progress: _Progress,
        deadline: _Deadline,
        when: str,
    ) -> EarmarkError:
        host = self._options.host
        logger.debug("smtp session failed in %s: %s", progress.phase, type(exc).__name__)
        if isinstance(exc, ssl.SSLCertVerificationError):
            return PermanentError(
                f"the TLS certificate of {host} did not verify: {exc.verify_message}; "
                "check EARMARK_SMTP_HOST"
            )
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return self._login_error(exc.smtp_code, exc.smtp_error)
        if isinstance(exc, smtplib.SMTPNotSupportedError):
            return PermanentError(
                f"the SMTP server at {host} does not offer AUTH over TLS; "
                "check EARMARK_SMTP_SECURITY and EARMARK_SMTP_PORT"
            )
        if (
            progress.phase == "the login"
            and type(exc) is smtplib.SMTPException
            and "authentication method" in str(exc)
        ):
            return PermanentError(
                f"the SMTP server at {host} offers no supported AUTH mechanism "
                "(PLAIN, LOGIN or CRAM-MD5)"
            )
        if isinstance(exc, smtplib.SMTPResponseException):
            return self._reply_error(progress.phase, exc.smtp_code, exc.smtp_error)
        if progress.payload_sent:
            return self._maybe_delivered(deadline)
        if deadline.expired:
            return TransientError(
                f"the SMTP session with {host} did not finish within "
                f"EARMARK_SMTP_TIMEOUT_S ({deadline.timeout_s:g} s)"
            )
        return TransientError(
            f"lost the connection to the SMTP server at {host} {when} ({type(exc).__name__})"
        )

    def _login_error(self, code: int, reply: bytes | str) -> EarmarkError:
        host = self._options.host
        enhanced = enhanced_status(reply if isinstance(reply, bytes) else reply.encode())
        detail = f"reply {code}" + (f", {enhanced}" if enhanced else "")
        if 500 <= code < 600:
            return PermanentError(
                f"the SMTP server at {host} rejected the login ({detail}); "
                "check EARMARK_SMTP_USERNAME and EARMARK_SMTP_PASSWORD"
            )
        return TransientError(f"the SMTP server at {host} refused the login ({detail})")


def _close(client: smtplib.SMTP | None) -> None:
    if client is not None:
        with contextlib.suppress(smtplib.SMTPException, OSError):
            client.close()


def build_smtp_mailer(settings: Settings) -> SmtpMailer:
    """Validate the settings and build the mailer. Makes no connection."""
    options = SmtpOptions.from_settings(settings)
    logger.info(
        "mail via smtp to %s:%d (%s), from domain %s",
        options.host,
        options.port,
        options.security,
        options.local_hostname,
    )
    return SmtpMailer(options)
