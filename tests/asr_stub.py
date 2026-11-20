"""Test doubles for the remote ASR client: a unix-socket HTTP server and a recording transport.

The default suite blocks TCP sockets but allows unix sockets, so ``AsrStub`` gives the
client a real HTTP/1.1 round trip (h11 and httpcore2 included) without a network.
"""

import email.parser
import email.policy
import json
import shutil
import socketserver
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Self

import httpx2


@dataclass(frozen=True)
class StubReply:
    status: int = 200
    body: bytes = b'{"text": ""}'
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "application/json"),)
    hold: threading.Event | None = None  # wait for it (with a 10 s cap) before replying


@dataclass(frozen=True)
class ReceivedRequest:
    method: str
    path: str
    headers: Mapping[str, str]  # names lower-cased
    fields: Mapping[str, str]  # non-file parts
    file_name: str | None
    file_type: str | None
    file_bytes: bytes


def parse_multipart(
    content_type: str, body: bytes
) -> tuple[dict[str, str], bytes, str | None, str | None]:
    """Return (non-file fields, file bytes, file name, file content type)."""
    head = b"Content-Type: " + content_type.encode() + b"\r\n\r\n"
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    fields: dict[str, str] = {}
    file_bytes = b""
    file_name: str | None = None
    file_type: str | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        filename = part.get_filename()
        if filename is not None:
            file_bytes, file_name, file_type = payload, filename, part.get_content_type()
        else:
            fields[str(name)] = payload.decode()
    return fields, file_bytes, file_name, file_type


def verbose_json(segments: Sequence[tuple[float, float, str]], **extra: object) -> bytes:
    body: dict[str, object] = {
        "text": " ".join(text.strip() for _, _, text in segments),
        "language": "en",
        "duration": segments[-1][1] if segments else 0.0,
        "segments": [
            {"id": i, "start": start, "end": end, "text": text}
            for i, (start, end, text) in enumerate(segments)
        ],
    }
    body.update(extra)
    return json.dumps(body).encode()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    stub: "AsrStub"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def address_string(self) -> str:
        return "unix"

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        stub = self.server.stub
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        fields, file_bytes, file_name, file_type = parse_multipart(
            self.headers.get("Content-Type", ""), body
        )
        received = ReceivedRequest(
            method=self.command,
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            fields=fields,
            file_name=file_name,
            file_type=file_type,
            file_bytes=file_bytes,
        )
        reply = stub.next_reply(received)
        if reply.hold is not None:
            reply.hold.wait(10)
        try:
            self.send_response(reply.status)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class AsrStub:
    """Threaded HTTP/1.1 server on a unix socket in a short temp dir; scripted replies."""

    def __init__(self, replies: Sequence[StubReply]) -> None:
        self._replies = list(replies)
        self._lock = threading.Lock()
        self.requests: list[ReceivedRequest] = []
        self.errors: list[str] = []
        self._dir = Path(tempfile.mkdtemp(prefix="asr-"))
        self.socket_path = self._dir / "s"
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.stub = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def next_reply(self, request: ReceivedRequest) -> StubReply:
        with self._lock:
            index = len(self.requests)
            self.requests.append(request)
            if index < len(self._replies):
                return self._replies[index]
        return StubReply(500, b'{"error": "unscripted"}')

    def transport(self) -> httpx2.HTTPTransport:
        return httpx2.HTTPTransport(uds=str(self.socket_path))

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        for reply in self._replies:
            if reply.hold is not None:
                reply.hold.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        shutil.rmtree(self._dir, ignore_errors=True)


class RecordingTransport(httpx2.BaseTransport):
    """Iterates the request body without buffering it; records chunk sizes and headers."""

    def __init__(self, reply: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self._reply = reply
        self.chunk_sizes: list[int] = []
        self.headers: Mapping[str, str] | None = None
        self.body = b""

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.headers = {k.lower(): v for k, v in request.headers.items()}
        stream = request.stream
        assert isinstance(stream, Iterable)
        chunks: list[bytes] = []
        for chunk in stream:
            self.chunk_sizes.append(len(chunk))
            chunks.append(chunk)
        self.body = b"".join(chunks)
        return self._reply(request)
