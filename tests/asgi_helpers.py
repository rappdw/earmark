"""Drive an ASGI app with scripted ``http.request`` chunks.

``TestClient`` cannot test streaming: it reads the whole body and sends it as one message.
``call_asgi`` delivers each chunk as its own message, can disconnect part-way, and counts
how often the app called ``receive``.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio


@dataclass
class AsgiResult:
    status: int
    headers: dict[str, str]
    body: bytes
    receive_calls: int
    sent: list[dict[str, Any]] = field(default_factory=list)

    def json(self) -> Any:
        import json

        return json.loads(self.body)


def call_asgi(
    app: Any,
    *,
    method: str = "POST",
    path: str = "/transcriptions",
    headers: Mapping[str, str] | None = None,
    chunks: Sequence[bytes] = (),
    disconnect_after: int | None = None,
    on_receive: Callable[[int], None] | None = None,
) -> AsgiResult:
    """Run one request. ``on_receive(n)`` runs before message ``n`` (0-based) is returned."""
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }
    messages = list(chunks) or [b""]
    result = AsgiResult(0, {}, b"", 0)

    async def receive() -> dict[str, Any]:
        index = result.receive_calls
        result.receive_calls += 1
        if on_receive is not None:
            on_receive(index)
        if disconnect_after is not None and index >= disconnect_after:
            return {"type": "http.disconnect"}
        if index < len(messages):
            return {
                "type": "http.request",
                "body": messages[index],
                "more_body": index < len(messages) - 1,
            }
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        result.sent.append(message)
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]
            }
        elif message["type"] == "http.response.body":
            result.body += message.get("body", b"")

    async def run() -> None:
        await app(scope, receive, send)

    anyio.run(run)
    return result
