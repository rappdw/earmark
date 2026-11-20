"""A fake Anthropic API behind httpx2.MockTransport, for driving the real SDK offline."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
from pydantic import SecretStr

from earmark.analyze.llm.anthropic import AnthropicClient, AnthropicOptions

KEY = "sk-ant-test-sentinel-7c2e"


def sse_body(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def message_events(
    text: str,
    *,
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    model: str = "claude-opus-5-5",
    thinking: bool = True,
    fallback: tuple[str, str, str | None] | None = None,
    split_at: int | None = None,
    iterations: list[dict[str, Any]] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> list[dict[str, Any]]:
    usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": 1}
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": usage,
            },
        }
    ]
    index = 0

    def text_block(chunk: str) -> None:
        nonlocal index
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            }
        )
        if chunk:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": chunk},
                }
            )
        events.append({"type": "content_block_stop", "index": index})
        index += 1

    if thinking:
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            }
        )
        events.append(
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "signature_delta", "signature": "sig"},
            }
        )
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    if fallback is not None:
        src, dst, category = fallback
        if split_at is not None:
            text_block(text[:split_at])
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "fallback",
                    "from": {"model": src},
                    "to": {"model": dst},
                    "trigger": {"type": "refusal", "category": category},
                },
            }
        )
        events.append({"type": "content_block_stop", "index": index})
        index += 1
        text_block(text[split_at:] if split_at is not None else text)
    else:
        text_block(text)
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    delta_usage: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if iterations is not None:
        delta_usage["iterations"] = iterations
    events.append({"type": "message_delta", "delta": delta, "usage": delta_usage})
    events.append({"type": "message_stop"})
    return events


def stream_reply(events: list[dict[str, Any]]) -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse_body(events)
    )


def error_reply(
    status: int,
    error_type: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> httpx2.Response:
    error: dict[str, Any] = {"type": error_type, "message": message}
    if details is not None:
        error["details"] = details
    return httpx2.Response(
        status,
        headers={"request-id": "req_test123", **(headers or {})},
        json={"type": "error", "error": error},
    )


class AnthropicStub:
    """A callable for ``httpx2.MockTransport`` that records requests and plays scripted replies."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.replies: list[httpx2.Response | Exception] = []
        self.count: int | httpx2.Response | Callable[[dict[str, Any]], int] = 1000

    def _matching(self, path: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path == path]

    def message_requests(self) -> list[httpx2.Request]:
        return self._matching("/v1/messages")

    def count_requests(self) -> list[httpx2.Request]:
        return self._matching("/v1/messages/count_tokens")

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/messages/count_tokens":
            if isinstance(self.count, httpx2.Response):
                return self.count
            if callable(self.count):
                return httpx2.Response(
                    200, json={"input_tokens": self.count(json.loads(request.content))}
                )
            return httpx2.Response(200, json={"input_tokens": self.count})
        if request.method == "POST" and path == "/v1/messages":
            if not self.replies:
                raise AssertionError("no scripted reply left for /v1/messages")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        raise AssertionError(f"unexpected request: {request.method} {path}")


def request_json(request: httpx2.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    assert isinstance(body, dict)
    return body


def make_options(**overrides: Any) -> AnthropicOptions:
    values: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "api_key": SecretStr(KEY),
        "effort": "medium",
        "fallbacks": True,
        "context_tokens": 1_000_000,
    }
    values.update(overrides)
    return AnthropicOptions(**values)


def make_client(
    stub: Callable[[httpx2.Request], httpx2.Response],
    options: AnthropicOptions | None = None,
    **option_overrides: Any,
) -> AnthropicClient:
    return AnthropicClient(
        options or make_options(**option_overrides),
        http_client=httpx2.Client(transport=httpx2.MockTransport(stub)),
        max_retries=0,
    )
