"""Test doubles for the ntfy client: a unix-socket ntfy server and the recorded ntfy.sh bodies.

The default suite blocks TCP sockets but allows unix sockets, so ``NtfyStub`` gives the
client a real HTTP/1.1 round trip without a network (like ``llm_stub.LlmStub``). The bodies
are what ntfy.sh answered on 2026-11-20 (docs/steps/15-notify-doctor.md 2.1).
"""

import json
import shutil
import socketserver
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Self

import httpx2

HEALTH_OK = b'{"healthy":true,"role":"active"}'
UNAUTHORIZED = (
    b'{"code":40101,"http":401,"error":"unauthorized",'
    b'"link":"https://ntfy.sh/docs/publish/#authentication"}'
)
PUBLISHED = (
    b'{"id":"cB3aIQWm7ySR","time":1795000000,"expires":1795043200,"event":"message",'
    b'"topic":"earmark-test","title":"Note ready: x","message":"line1\\nline2",'
    b'"priority":3,"tags":["white_check_mark"]}'
)
ACCOUNT_ANON = (
    b'{"username":"*","role":"anonymous","limits":{"messages":250},'
    b'"stats":{"messages":0,"messages_remaining":250}}'
)
ACCOUNT_EXHAUSTED = (
    b'{"username":"bob","role":"user","stats":{"messages":250,"messages_remaining":0}}'
)
TOO_MANY = b'{"code":42901,"http":429,"error":"limit reached: too many requests"}'
JSON_HEADERS = {"Content-Type": "application/json"}

TOPIC = "sentinel-topic-91x"
TOKEN = "tk_sentinel0token0value0abcdef12345"
TOPIC_URL = f"https://ntfy.test/{TOPIC}"
ROOT_URL = "https://ntfy.test/"


def json_reply(status: int, body: bytes, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, content=body, headers={**JSON_HEADERS, **headers})


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b"{}"
    headers: Mapping[str, str] = field(default_factory=lambda: dict(JSON_HEADERS))


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    headers: Mapping[str, str]  # names lower-cased
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    stub: "NtfyStub"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def address_string(self) -> str:
        return "unix"

    def log_message(self, *args: Any) -> None:
        pass

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        received = Received(
            self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
        )
        reply = self.server.stub.next_reply(received)
        try:
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()


class NtfyStub:
    """Threaded HTTP/1.1 server on a unix socket: POST /, GET /v1/health, GET /v1/account."""

    def __init__(self, route: Callable[[Received], Reply] | None = None) -> None:
        self._route = route or self._default
        self._lock = threading.Lock()
        self.requests: list[Received] = []
        self._dir = Path(tempfile.mkdtemp(prefix="ntfy-"))
        self.socket_path = self._dir / "s"
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.stub = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @staticmethod
    def _default(request: Received) -> Reply:
        if request.method == "POST" and request.path == "/":
            return Reply(200, PUBLISHED)
        if request.method == "GET" and request.path == "/v1/health":
            return Reply(200, HEALTH_OK)
        if request.method == "GET" and request.path == "/v1/account":
            return Reply(200, ACCOUNT_ANON)
        return Reply(404, b'{"code":40401,"http":404,"error":"page not found"}')

    def next_reply(self, request: Received) -> Reply:
        with self._lock:
            self.requests.append(request)
        return self._route(request)

    def transport(self) -> httpx2.HTTPTransport:
        return httpx2.HTTPTransport(uds=str(self.socket_path))

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        shutil.rmtree(self._dir, ignore_errors=True)
