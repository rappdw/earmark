"""Test doubles for the local LLM client: a unix-socket OpenAI-style chat server.

The default suite blocks TCP sockets but allows unix sockets, so ``LlmStub`` gives the
client a real HTTP/1.1 round trip without a network (like ``asr_stub.AsrStub``).
"""

import json
import shutil
import socketserver
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Self

import httpx2

JSON_HEADERS = (("Content-Type", "application/json"),)


@dataclass(frozen=True)
class ChatReply:
    status: int = 200
    body: bytes = b"{}"
    headers: tuple[tuple[str, str], ...] = JSON_HEADERS


@dataclass(frozen=True)
class ReceivedChat:
    method: str
    path: str
    headers: Mapping[str, str]  # names lower-cased
    json: dict[str, Any]


def chat_completion(
    content: str | None,
    *,
    finish_reason: str | None = "stop",
    usage: tuple[int, int] = (100, 50),
    **message_extra: Any,
) -> bytes:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    message.update(message_extra)
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
    }
    return json.dumps(body).encode()


def error_body(message: str, *, type_: str = "invalid_request_error", code: int = 400) -> bytes:
    return json.dumps({"error": {"message": message, "type": type_, "code": code}}).encode()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    stub: "LlmStub"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def address_string(self) -> str:
        return "unix"

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        parsed = json.loads(self.rfile.read(length))
        received = ReceivedChat(
            method=self.command,
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            json=parsed,
        )
        reply = self.server.stub.next_reply(received)
        try:
            self.send_response(reply.status)
            for name, value in reply.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class LlmStub:
    """Threaded HTTP/1.1 server on a unix socket; scripted replies or a callable."""

    def __init__(self, replies: Sequence[ChatReply] | Callable[[ReceivedChat], ChatReply]) -> None:
        self._replies = replies
        self._lock = threading.Lock()
        self.requests: list[ReceivedChat] = []
        self._dir = Path(tempfile.mkdtemp(prefix="llm-"))
        self.socket_path = self._dir / "s"
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.stub = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def next_reply(self, request: ReceivedChat) -> ChatReply:
        with self._lock:
            index = len(self.requests)
            self.requests.append(request)
        if callable(self._replies):
            return self._replies(request)
        if index < len(self._replies):
            return self._replies[index]
        return ChatReply(500, b'{"error": "unscripted"}')

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
