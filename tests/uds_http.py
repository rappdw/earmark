"""Unix-socket HTTP helpers: real HTTP/1.1 round trips with TCP sockets blocked.

``UdsHttpServer`` is a scripted server for client libraries (httpx, httpx2, urllib3);
``asgi_on_uds`` runs a real uvicorn on a unix socket for the app under test.
"""

import shutil
import socket
import socketserver
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Self

import uvicorn


@dataclass(frozen=True)
class UdsReply:
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    owner: "UdsHttpServer"

    def get_request(self) -> tuple[Any, Any]:
        request, _ = super().get_request()
        return request, ("unix", 0)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *args: Any) -> None:
        pass

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        owner = self.server.owner
        owner.requests.append((self.command, self.path))
        reply = owner.reply(self.command, self.path)
        try:
            self.send_response(reply.status)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()


class UdsHttpServer:
    """Threaded HTTP/1.1 server on a unix socket in a short temp directory."""

    def __init__(self, reply: Callable[[str, str], UdsReply]) -> None:
        self.reply = reply
        self.requests: list[tuple[str, str]] = []
        self._dir = Path(tempfile.mkdtemp(prefix="em", dir="/tmp"))
        self.socket_path = self._dir / "s"
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        shutil.rmtree(self._dir, ignore_errors=True)


@contextmanager
def asgi_on_uds(app: object, **config: Any) -> Iterator[Path]:
    """Run ``app`` under a real uvicorn on a unix socket; yield the socket path."""
    directory = Path(tempfile.mkdtemp(prefix="em", dir="/tmp"))
    path = directory / "s"
    server = uvicorn.Server(uvicorn.Config(app, uds=str(path), **config))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "uvicorn did not start"
        yield path
    finally:
        server.should_exit = True
        thread.join(5)
        shutil.rmtree(directory, ignore_errors=True)


def uds_connection_class(base: type[Any], socket_path: Path) -> type[Any]:
    """A subclass of ``base`` (an http.client-style connection) that dials a unix socket."""

    def connect(self: Any) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(socket_path))
        self.sock = sock

    conn_cls: type[Any] = type("UdsHTTPConnection", (base,), {"connect": connect})
    return conn_cls
