import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from earmark.analyze.chunking import estimate_request_tokens
from earmark.analyze.llm.base import LLMRequest
from earmark.analyze.llm.openai_compat import (
    CHAT_PATH,
    OpenAICompatClient,
    OpenAICompatOptions,
    build_openai_compat_analyzer,
    clean_content,
)
from earmark.analyze.prompts import (
    MAP_SYSTEM_PROMPT,
    REDUCE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_message,
    schema_section,
)
from earmark.analyze.schema import ANALYSIS_OUTPUT_SCHEMA, SCHEMA_NAME
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.errors import PermanentError, TransientError
from tests.analysis_samples import sample_episode, sample_transcript, valid_wire_json
from tests.llm_stub import ChatReply, LlmStub, ReceivedChat, chat_completion, error_body
from tests.long_transcripts import MapReduceResponder, long_transcript, map_reduce_responder

KEY = "sk-local-test-sentinel-91ab"
ROOT = Path(__file__).resolve().parent.parent
HOST = "llm.test:8000"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "openai_compat",
        "llm_base_url": f"http://{HOST}/v1",
        "llm_model": "local-model",
        "llm_context_tokens": 32768,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_options(**overrides: Any) -> OpenAICompatOptions:
    return OpenAICompatOptions.from_settings(make_settings(**overrides))


def request(user: str = "hello") -> LLMRequest:
    return LLMRequest(
        system=SYSTEM_PROMPT,
        user=user,
        schema=ANALYSIS_OUTPUT_SCHEMA,
        schema_name=SCHEMA_NAME,
        max_tokens=2048,
    )


class Server:
    """A MockTransport handler that records requests and serves scripted replies."""

    def __init__(self, *replies: httpx2.Response | BaseException) -> None:
        self.replies = list(replies) or [
            httpx2.Response(200, content=chat_completion(valid_wire_json()))
        ]
        self.requests: list[httpx2.Request] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.requests.append(req)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


def respond(status: int = 200, body: bytes | None = None, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, content=body if body is not None else b"{}", headers=headers)


def ok(content: str | None = "VALID", **kwargs: Any) -> httpx2.Response:
    text = valid_wire_json() if content == "VALID" else content
    return respond(200, chat_completion(text, **kwargs))


def client_for(server: Server, **overrides: Any) -> OpenAICompatClient:
    return OpenAICompatClient(make_options(**overrides), transport=server.transport())


# -- B5: options ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("context", "output", "maximum"),
    [(8192, 2048, 4096), (32768, 8192, 16384), (200000, 16384, 32768)],
)
def test_options_from_settings(context: int, output: int, maximum: int) -> None:
    options = make_options(llm_context_tokens=context, llm_api_key=KEY)
    assert (options.output_tokens, options.max_output_tokens) == (output, maximum)
    assert options.endpoint == f"http://{HOST}/v1{CHAT_PATH}"
    assert options.host == HOST
    assert options.response_format == "json_schema"
    assert options.timeout_s == 3600.0
    client = OpenAICompatClient(options)
    assert (client.context_tokens, client.output_tokens, client.max_output_tokens) == (
        context,
        output,
        maximum,
    )
    assert client.model == "local-model"


def test_config_errors() -> None:
    settings = Settings(_env_file=None, llm_provider="openai_compat")
    with pytest.raises(ConfigError) as excinfo:
        OpenAICompatOptions.from_settings(settings)
    errors = excinfo.value.errors
    assert [e.split(":")[0] for e in errors] == [
        "EARMARK_LLM_BASE_URL",
        "EARMARK_LLM_CONTEXT_TOKENS",
        "EARMARK_LLM_MODEL",
    ]
    assert "http://spark:8000/v1" in errors[0]
    assert "docs/llm.md" in errors[1]
    assert "Claude" in errors[2]

    small = make_settings(llm_context_tokens=4096, llm_model="Claude-Sonnet", llm_api_key=KEY)
    with pytest.raises(ConfigError) as excinfo:
        OpenAICompatOptions.from_settings(small)
    text = str(excinfo.value)
    assert "must be at least 8192" in text
    assert "Claude-Sonnet" not in text
    assert "4096" not in text
    assert KEY not in text


def test_builder_enables_repair(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    server = Server()
    caplog.set_level(logging.INFO)
    analyzer = build_openai_compat_analyzer(
        make_settings(), ArtifactStore(tmp_path), transport=server.transport()
    )
    assert analyzer.repairs_invalid_json is True
    assert isinstance(analyzer.client, OpenAICompatClient)
    assert server.requests == []
    assert f"local LLM at {HOST} (model 'local-model', context 32768 tokens" in caplog.text
    analyzer.close()


def test_builder_config_error_before_anything(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        build_openai_compat_analyzer(
            Settings(_env_file=None, llm_provider="openai_compat"), ArtifactStore(tmp_path)
        )


# -- A6: the wire --------------------------------------------------------------------------


def test_json_schema_request_over_http() -> None:
    reply = ChatReply(body=chat_completion(valid_wire_json(), usage=(11, 22)))
    with LlmStub([reply]) as stub:
        client = OpenAICompatClient(make_options(), transport=stub.transport())
        response = client.complete(request("the user message"))
        client.close()
    (got,) = stub.requests
    assert got.method == "POST"
    assert got.path == "/v1/chat/completions"
    assert list(got.json) == ["model", "messages", "max_tokens", "stream", "response_format"]
    assert got.json["model"] == "local-model"
    assert got.json["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "earmark_analysis",
            "strict": True,
            "schema": ANALYSIS_OUTPUT_SCHEMA,
        },
    }
    assert got.json["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert got.json["messages"][1] == {"role": "user", "content": "the user message"}
    assert got.json["stream"] is False
    assert got.json["max_tokens"] == 2048
    assert got.headers["content-type"] == "application/json"
    assert "authorization" not in got.headers
    assert response.stop == "end"
    assert response.text == valid_wire_json()
    assert (response.input_tokens, response.output_tokens) == (11, 22)


def test_json_object_request_over_http() -> None:
    with LlmStub([ChatReply(body=chat_completion(valid_wire_json()))]) as stub:
        client = OpenAICompatClient(
            make_options(llm_response_format="json_object"), transport=stub.transport()
        )
        client.complete(request("the user message"))
        client.close()
    (got,) = stub.requests
    assert got.json["response_format"] == {"type": "json_object"}
    user = got.json["messages"][1]["content"]
    assert user.startswith("the user message\n")
    assert user.endswith(schema_section(ANALYSIS_OUTPUT_SCHEMA))
    assert list(got.json) == ["model", "messages", "max_tokens", "stream", "response_format"]


def test_request_body_does_not_mutate_the_schema() -> None:
    client = OpenAICompatClient(make_options())
    body = client.request_body(request())
    body["response_format"]["json_schema"]["schema"]["properties"].clear()
    assert ANALYSIS_OUTPUT_SCHEMA["properties"]


@pytest.mark.parametrize("response_format", ["json_schema", "json_object"])
def test_analysis_end_to_end_both_formats(tmp_path: Path, response_format: str) -> None:
    settings = make_settings(llm_response_format=response_format)
    artifacts = ArtifactStore(tmp_path / "data")
    with LlmStub([ChatReply(body=chat_completion(valid_wire_json()))]) as stub:
        analyzer = build_openai_compat_analyzer(settings, artifacts, transport=stub.transport())
        analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
        again = analyzer.analyze(sample_episode(), sample_transcript(), None)
        analyzer.close()
    assert again == analysis
    assert len(stub.requests) == 1
    assert analysis.model == settings.llm_model
    assert artifacts.read_analysis(sample_episode().key, analysis.prompt_hash) == analysis
    user = stub.requests[0].json["messages"][1]["content"]
    assert user.startswith(build_user_message(sample_episode(), sample_transcript(), None))


def reply_from(responder: MapReduceResponder) -> Callable[[ReceivedChat], ChatReply]:
    def reply(got: ReceivedChat) -> ChatReply:
        system, user = (m["content"] for m in got.json["messages"])
        answer = responder(LLMRequest(system, user, {}, "x", got.json["max_tokens"]))
        return ChatReply(body=chat_completion(answer.text))

    return reply


def test_map_reduce_over_http(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    artifacts = ArtifactStore(tmp_path / "data")
    transcript = long_transcript(hours=40 / 60)
    with LlmStub(reply_from(responder)) as stub:
        analyzer = build_openai_compat_analyzer(
            make_settings(llm_context_tokens=8192), artifacts, transport=stub.transport()
        )
        analysis = analyzer.analyze(sample_episode(), transcript, None)
        analyzer.close()
    systems = [r.json["messages"][0]["content"] for r in stub.requests]
    assert len(responder.maps) >= 2
    assert systems[:-1] == [MAP_SYSTEM_PROMPT] * len(responder.maps)
    assert systems[-1] == REDUCE_SYSTEM_PROMPT
    assert analysis.warnings[0].startswith("the transcript was analyzed in ")
    for got in stub.requests:
        user = got.json["messages"][1]["content"]
        assert estimate_request_tokens(LLMRequest("", user, ANALYSIS_OUTPUT_SCHEMA, "x", 0)) < 8192
        assert got.json["max_tokens"] == 2048


def test_end_to_end_repair_over_http(tmp_path: Path) -> None:
    replies = [
        ChatReply(body=chat_completion("{definitely not json")),
        ChatReply(body=chat_completion(valid_wire_json())),
    ]
    artifacts = ArtifactStore(tmp_path / "data")
    with LlmStub(replies) as stub:
        analyzer = build_openai_compat_analyzer(
            make_settings(), artifacts, transport=stub.transport()
        )
        analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
        analyzer.close()
    assert len(stub.requests) == 2
    assert "<invalid_answer>" in stub.requests[1].json["messages"][1]["content"]
    assert artifacts.read_analysis(sample_episode().key, analysis.prompt_hash) is not None


def test_count_tokens_is_the_local_estimate_and_makes_no_request() -> None:
    server = Server()
    client = client_for(server)
    assert client.count_tokens(request()) == estimate_request_tokens(request())
    assert server.requests == []


# -- B1: responses ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('<think>hmm {"a": 1}</think>\n{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('<think>x</think>\n```json\n{"a": 1}\n```\n', '{"a": 1}'),
        ('<think>never closed {"a": 1}', '<think>never closed {"a": 1}'),
        ('  {"a": 1}  ', '{"a": 1}'),
        ('Sure! {"a": 1}', 'Sure! {"a": 1}'),
        ("", ""),
    ],
)
def test_clean_content(raw: str, expected: str) -> None:
    assert clean_content(raw) == expected


@pytest.mark.parametrize(
    ("finish", "content", "stop"),
    [
        ("stop", "{}", "end"),
        ("length", "{", "max_tokens"),
        ("content_filter", "", "refusal"),
        (None, "{}", "end"),
        (None, "   ", "other"),
        ("tool_calls", "", "other"),
    ],
)
def test_finish_reasons_mapped(finish: str | None, content: str, stop: str) -> None:
    client = client_for(Server(ok(content, finish_reason=finish)))
    response = client.complete(request())
    assert response.stop == stop
    assert response.raw_stop == finish


def test_refusal_field() -> None:
    client = client_for(Server(ok("", refusal="I can't help with that.")))
    response = client.complete(request())
    assert response.stop == "refusal"
    assert response.refusal_category is None


def test_reasoning_fields_ignored() -> None:
    body = ok(
        valid_wire_json(),
        reasoning="deep thoughts",
        reasoning_content="more thoughts",
        tool_calls=[],
    )
    response = client_for(Server(body)).complete(request())
    assert response.text == valid_wire_json()


def test_usage_mapped() -> None:
    response = client_for(Server(ok(usage=(7, 9)))).complete(request())
    assert (response.input_tokens, response.output_tokens) == (7, 9)
    no_usage = respond(
        200,
        json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}).encode(),
    )
    bare = client_for(Server(no_usage)).complete(request())
    assert (bare.input_tokens, bare.output_tokens) == (None, None)


def test_null_content() -> None:
    response = client_for(Server(ok(None, finish_reason="stop"))).complete(request())
    assert response.text == ""
    assert response.stop == "end"


# -- B2: errors ------------------------------------------------------------------------------


def error_case(status: int, **headers: str) -> httpx2.Response:
    return respond(status, error_body("bad response_format here"), **headers)


@pytest.mark.parametrize(
    ("reply", "kind", "needles"),
    [
        (error_case(429, **{"Retry-After": "120"}), TransientError, ["HTTP 429"]),
        (error_case(500), TransientError, ["HTTP 500"]),
        (error_case(503), TransientError, ["HTTP 503"]),
        (error_case(408), TransientError, ["HTTP 408"]),
        (error_case(401), PermanentError, ["EARMARK_LLM_API_KEY"]),
        (error_case(403), PermanentError, ["EARMARK_LLM_API_KEY"]),
        (error_case(404), PermanentError, ["EARMARK_LLM_BASE_URL", "EARMARK_LLM_MODEL"]),
        (error_case(400), PermanentError, ["json_object", "bad response_format here"]),
        (error_case(422), PermanentError, ["HTTP 422", "json_object"]),
        (error_case(418), PermanentError, ["HTTP 418"]),
        (respond(302, b"", Location="http://elsewhere/"), PermanentError, ["redirect"]),
        (respond(200, b"<html>"), PermanentError, ["not JSON"]),
        (respond(200, b'{"choices": []}'), PermanentError, ["not a chat completion"]),
        (respond(200, b'{"choices": [{"message": 5}]}'), PermanentError, ["not a chat completion"]),
        (respond(200, b"x" * (17 * 1024 * 1024)), PermanentError, ["more than 16 MiB"]),
        (respond(100 + 99, b""), PermanentError, ["unexpected HTTP"]),
    ],
)
def test_http_errors_mapped(
    reply: httpx2.Response, kind: type[Exception], needles: list[str]
) -> None:
    server = Server(reply)
    client = client_for(server)
    with pytest.raises(kind) as excinfo:
        client.complete(request())
    assert type(excinfo.value) is kind
    message = excinfo.value.message  # type: ignore[attr-defined]
    assert HOST in message
    assert "/chat/completions" not in message
    for needle in needles:
        assert needle in message
    if isinstance(excinfo.value, TransientError) and reply.status_code == 429:
        assert excinfo.value.retry_after_s == 120.0
    assert len(server.requests) == 1  # a redirect is not followed


def test_json_object_mode_has_no_response_format_hint() -> None:
    client = client_for(Server(error_case(400)), llm_response_format="json_object")
    with pytest.raises(PermanentError) as excinfo:
        client.complete(request())
    assert "json_object" not in excinfo.value.message
    assert "HTTP 400" in excinfo.value.message


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (httpx2.ReadTimeout("boom"), "EARMARK_LLM_TIMEOUT_SECONDS"),
        (httpx2.ConnectError("boom"), "ConnectError"),
        (httpx2.ConnectTimeout("boom"), "timed out"),
    ],
)
def test_transport_errors_are_transient(error: Exception, needle: str) -> None:
    client = client_for(Server(error))
    with pytest.raises(TransientError) as excinfo:
        client.complete(request())
    assert needle in excinfo.value.message
    assert HOST in excinfo.value.message
    assert "boom" not in excinfo.value.message
    assert excinfo.value.__cause__ is error


# -- B3: context overflow ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (
            400,
            error_body(
                "request (9000 tokens) exceeds the available context size (8192 tokens), "
                "try increasing it",
                type_="exceed_context_size_error",
            ),
        ),
        (
            400,
            error_body(
                "This model's maximum context length is 8192 tokens. However, your prompt "
                "contains 9000 input tokens.",
                type_="BadRequestError",
            ),
        ),
        (413, b"Prompt is too long"),
    ],
)
def test_context_overflow_bodies(status: int, body: bytes) -> None:
    server = Server(respond(status, body))
    response = client_for(server).complete(request())
    assert response.stop == "context_exceeded"
    assert response.text == ""
    assert response.raw_stop == f"HTTP {status}"


def test_other_400_is_permanent() -> None:
    with pytest.raises(PermanentError):
        client_for(Server(respond(400, error_body("something else")))).complete(request())


# -- B4: key, redirects, proxies, cookies --------------------------------------------------------


def test_api_key_sent_only_when_set() -> None:
    server = Server()
    client_for(server, llm_api_key=KEY).complete(request())
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"
    bare = Server()
    client_for(bare).complete(request())
    assert "authorization" not in bare.requests[0].headers


def test_key_never_logged_or_in_errors(
    caplog: pytest.LogCaptureFixture, restore_root_logging: None
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = respond(401, error_body(f"bad key {KEY}"))
    mock = Server(ok(), bad)
    with LlmStub(
        [
            ChatReply(body=chat_completion(valid_wire_json())),
            ChatReply(401, error_body(f"bad key {KEY}")),
        ]
    ) as stub:
        for transport in (mock.transport(), stub.transport()):
            client = OpenAICompatClient(make_options(llm_api_key=KEY), transport=transport)
            client.complete(request())
            with pytest.raises(PermanentError) as excinfo:
                client.complete(request())
            assert KEY not in excinfo.value.message
            assert "**********" in excinfo.value.message
            client.close()
    assert KEY not in caplog.text


def test_repr_hides_key() -> None:
    options = make_options(llm_api_key=KEY)
    assert KEY not in repr(options)
    assert "**********" in repr(options)
    assert KEY not in repr(OpenAICompatClient(options))
    assert isinstance(options.api_key, SecretStr)


def test_no_redirect_followed() -> None:
    server = Server(respond(307, b"", Location="http://other.test/v1/chat/completions"))
    with pytest.raises(PermanentError, match="redirect"):
        client_for(server).complete(request())
    assert len(server.requests) == 1


def test_env_proxy_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:1")
    with LlmStub([ChatReply(body=chat_completion(valid_wire_json()))]) as stub:
        uds = stub.transport()
        monkeypatch.setattr(httpx2, "HTTPTransport", lambda *a, **k: uds)
        client = OpenAICompatClient(make_options())
        client.complete(request())
        client.close()
    assert len(stub.requests) == 1


def test_cookies_not_sent_back() -> None:
    first = httpx2.Response(
        200, content=chat_completion(valid_wire_json()), headers={"Set-Cookie": "sid=abc; Path=/"}
    )
    server = Server(first, ok())
    client = client_for(server)
    client.complete(request())
    client.complete(request())
    assert "cookie" not in server.requests[1].headers


def test_client_timeout() -> None:
    server = Server()
    client_for(server).complete(request())
    assert server.requests[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 3600.0,
        "write": 3600.0,
        "pool": 10.0,
    }
    short = Server()
    client_for(short, llm_timeout_seconds=60).complete(request())
    assert short.requests[0].extensions["timeout"]["read"] == 60.0


def test_close_then_reuse() -> None:
    server = Server()
    client = client_for(server)
    client.complete(request())
    client.close()
    client.close()
    assert client.complete(request()).stop == "end"


# -- B10: the operator guide ------------------------------------------------------------------


def test_llm_doc_covers_settings() -> None:
    text = (ROOT / "docs" / "llm.md").read_text()
    for needle in (
        "EARMARK_LLM_BASE_URL",
        "EARMARK_LLM_MODEL",
        "EARMARK_LLM_CONTEXT_TOKENS",
        "EARMARK_LLM_API_KEY",
        "EARMARK_LLM_RESPONSE_FORMAT",
        "EARMARK_LLM_TIMEOUT_SECONDS",
        "OLLAMA_CONTEXT_LENGTH",
        "max_model_len",
        "n_ctx",
    ):
        assert needle in text
