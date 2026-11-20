"""AnthropicClient against the real SDK, driven through a mock transport."""

import io
import logging
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from earmark.analyze.analyzer import LlmAnalyzer
from earmark.analyze.llm.anthropic import (
    FALLBACK_BETA,
    NO_CREDENTIALS,
    AnthropicClient,
    AnthropicOptions,
    build_anthropic_analyzer,
)
from earmark.analyze.llm.base import LLMClient, LLMRequest
from earmark.analyze.prompts import SYSTEM_PROMPT
from earmark.analyze.schema import ANALYSIS_OUTPUT_SCHEMA, SCHEMA_NAME
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.errors import NeedsChunking, PermanentError, TransientError
from earmark.log import configure_logging
from tests.analysis_samples import sample_episode, sample_transcript, valid_wire_json
from tests.anthropic_stub import (
    KEY,
    AnthropicStub,
    error_reply,
    make_client,
    make_options,
    message_events,
    request_json,
    stream_reply,
)

TRANSCRIPT_SENTINEL = "zebra-sentinel-transcript-text"


def make_request(user: str = "the user message") -> LLMRequest:
    return LLMRequest(
        system=SYSTEM_PROMPT,
        user=user,
        schema=ANALYSIS_OUTPUT_SCHEMA,
        schema_name=SCHEMA_NAME,
        max_tokens=64_000,
    )


@pytest.fixture
def stub() -> AnthropicStub:
    return AnthropicStub()


@pytest.fixture
def client(stub: AnthropicStub) -> Iterator[AnthropicClient]:
    c = make_client(stub)
    yield c
    c.close()


def _complete(stub: AnthropicStub, client: AnthropicClient, text: str = "{}", **kw: Any) -> Any:
    stub.replies.append(stream_reply(message_events(text, **kw)))
    return client.complete(make_request())


def test_satisfies_protocol(client: AnthropicClient) -> None:
    typed: LLMClient = client
    assert typed.model == "claude-opus-5-5"
    assert typed.context_tokens == 1_000_000
    assert typed.output_tokens == 64_000
    assert typed.max_output_tokens == 128_000


def test_request_shape(stub: AnthropicStub, client: AnthropicClient) -> None:
    request = make_request()
    _complete(stub, client)
    (posted,) = stub.message_requests()
    body = request_json(posted)
    assert set(body) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "thinking",
        "output_config",
        "fallbacks",
        "stream",
    }
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 64000
    assert body["stream"] is True
    assert body["system"] == SYSTEM_PROMPT
    assert body["messages"] == [{"role": "user", "content": request.user}]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": ANALYSIS_OUTPUT_SCHEMA},
    }
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in posted.headers["anthropic-beta"]

    client.count_tokens(request)
    (counted,) = stub.count_requests()
    count_body = request_json(counted)
    assert set(count_body) == {"model", "system", "messages", "thinking", "output_config"}
    for name in ("model", "system", "messages", "thinking", "output_config"):
        assert count_body[name] == body[name]


def test_effort_and_model_from_settings(stub: AnthropicStub) -> None:
    settings = Settings(
        _env_file=None, llm_effort="high", llm_model="claude-sonnet-5-5", llm_api_key=SecretStr(KEY)
    )
    options = AnthropicOptions.from_settings(settings)
    assert options.context_tokens == 1_000_000
    c = make_client(stub, options)
    try:
        _complete(stub, c)
    finally:
        c.close()
    body = request_json(stub.message_requests()[0])
    assert body["model"] == "claude-sonnet-5-5"
    assert body["output_config"]["effort"] == "high"


def test_context_tokens_from_settings() -> None:
    settings = Settings(_env_file=None, llm_context_tokens=200_000, llm_api_key=SecretStr(KEY))
    assert AnthropicOptions.from_settings(settings).context_tokens == 200_000


def test_fallbacks_disabled(stub: AnthropicStub) -> None:
    c = make_client(stub, fallbacks=False)
    try:
        _complete(stub, c)
    finally:
        c.close()
    (posted,) = stub.message_requests()
    assert "fallbacks" not in request_json(posted)
    assert "anthropic-beta" not in posted.headers


def test_no_forbidden_parameters(stub: AnthropicStub, client: AnthropicClient) -> None:
    _complete(stub, client)
    body = request_json(stub.message_requests()[0])
    for name in ("temperature", "top_p", "top_k", "tools", "tool_choice", "metadata"):
        assert name not in body
    assert "budget_tokens" not in str(body["thinking"])


def test_key_from_setting_is_sent(stub: AnthropicStub, client: AnthropicClient) -> None:
    _complete(stub, client)
    headers = stub.message_requests()[0].headers
    assert headers.get("authorization") == f"Bearer {KEY}" or headers.get("x-api-key") == KEY


def test_env_key_used_when_setting_unset(
    stub: AnthropicStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    c = make_client(stub, api_key=None)
    try:
        _complete(stub, c)
    finally:
        c.close()
    headers = stub.message_requests()[0].headers
    sent = headers.get("authorization", "") + headers.get("x-api-key", "")
    assert "sk-ant-from-env" in sent


def test_setting_wins_over_env(stub: AnthropicStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    c = make_client(stub)
    try:
        _complete(stub, c)
    finally:
        c.close()
    headers = stub.message_requests()[0].headers
    sent = headers.get("authorization", "") + headers.get("x-api-key", "")
    assert KEY in sent
    assert "sk-ant-from-env" not in sent


def test_no_credential_is_config_error(stub: AnthropicStub) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_client(stub, api_key=None)
    assert excinfo.value.errors == [NO_CREDENTIALS]
    assert "EARMARK_LLM_API_KEY" in NO_CREDENTIALS


def test_text_blocks_concatenated(stub: AnthropicStub, client: AnthropicClient) -> None:
    response = _complete(stub, client, "abcdef", fallback=("a", "b", None), split_at=3)
    assert response.text == "abcdef"
    plain = _complete(stub, client, '{"x": 1}', thinking=True)
    assert plain.text == '{"x": 1}'


def test_fallback_warning_mid_output(stub: AnthropicStub, client: AnthropicClient) -> None:
    response = _complete(
        stub,
        client,
        "partial continued",
        model="claude-opus-4-8",
        fallback=("claude-opus-5-5", "claude-opus-4-8", "cyber"),
        split_at=7,
        iterations=[
            {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1, "output_tokens": 1},
            {
                "type": "fallback_message",
                "model": "claude-opus-4-8",
                "input_tokens": 1,
                "output_tokens": 1,
            },
        ],
    )
    assert response.text == "partial continued"
    assert response.warnings == (
        "claude-opus-5-5 declined (refusal category: cyber); claude-opus-4-8 answered instead",
    )


def test_fallback_warning_before_output(stub: AnthropicStub, client: AnthropicClient) -> None:
    response = _complete(
        stub, client, "whole", fallback=("claude-opus-5-5", "claude-opus-4-8", None)
    )
    assert response.text == "whole"
    assert response.warnings == (
        "claude-opus-5-5 declined (refusal category: unspecified); "
        "claude-opus-4-8 answered instead",
    )


def test_sticky_fallback_warning(stub: AnthropicStub, client: AnthropicClient) -> None:
    response = _complete(
        stub,
        client,
        "text",
        model="claude-opus-4-8",
        iterations=[
            {
                "type": "fallback_message",
                "model": "claude-opus-4-8",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        ],
    )
    assert response.warnings == (
        "claude-opus-4-8 answered instead of claude-opus-5-5 (fallback routing)",
    )


def test_usage_tokens(stub: AnthropicStub, client: AnthropicClient) -> None:
    response = _complete(stub, client, input_tokens=1234, output_tokens=56)
    assert (response.input_tokens, response.output_tokens) == (1234, 56)


@pytest.mark.parametrize(
    ("reason", "details", "stop", "category"),
    [
        ("end_turn", None, "end", None),
        ("max_tokens", None, "max_tokens", None),
        ("refusal", {"type": "refusal", "category": "bio", "explanation": None}, "refusal", "bio"),
        ("refusal", {"type": "refusal", "category": None, "explanation": None}, "refusal", None),
        ("model_context_window_exceeded", None, "context_exceeded", None),
        ("pause_turn", None, "other", None),
    ],
)
def test_stop_reasons_mapped(
    stub: AnthropicStub,
    client: AnthropicClient,
    reason: str,
    details: dict[str, Any] | None,
    stop: str,
    category: str | None,
) -> None:
    response = _complete(stub, client, stop_reason=reason, stop_details=details)
    assert response.stop == stop
    assert response.raw_stop == reason
    assert response.refusal_category == category


def _transport_error(request: httpx2.Request) -> Exception:
    return httpx2.ConnectError("boom", request=request)


@pytest.mark.parametrize(
    ("reply", "transient", "retry_after", "fragments"),
    [
        (
            error_reply(429, "rate_limit_error", "slow down", headers={"retry-after": "17"}),
            True,
            17.0,
            ["rate limiting", "slow down", "req_test123"],
        ),
        (
            error_reply(
                429,
                "rate_limit_error",
                "cap",
                details={"error_code": "enforced_spend_limit_reached"},
            ),
            False,
            None,
            ["spend limit"],
        ),
        (error_reply(500, "api_error", "oops"), True, None, ["HTTP 500"]),
        (error_reply(503, "api_error", "oops"), True, None, ["HTTP 503"]),
        (error_reply(529, "overloaded_error", "busy"), True, None, ["HTTP 529"]),
        (error_reply(408, "timeout_error", "late"), True, None, ["HTTP 408"]),
        (error_reply(400, "invalid_request_error", "bad field"), False, None, ["bad field"]),
        (
            error_reply(401, "authentication_error", "nope"),
            False,
            None,
            ["EARMARK_LLM_API_KEY"],
        ),
        (error_reply(402, "billing_error", "pay"), False, None, ["billing"]),
        (error_reply(403, "permission_error", "no"), False, None, ["HTTP 403"]),
        (error_reply(404, "not_found_error", "model"), False, None, ["EARMARK_LLM_MODEL"]),
        (error_reply(413, "request_too_large", "big"), False, None, ["HTTP 413"]),
    ],
)
def test_api_errors_mapped(
    stub: AnthropicStub,
    client: AnthropicClient,
    reply: httpx2.Response,
    transient: bool,
    retry_after: float | None,
    fragments: list[str],
) -> None:
    stub.replies.append(reply)
    expected = TransientError if transient else PermanentError
    with pytest.raises(expected) as excinfo:
        client.complete(make_request())
    assert not isinstance(excinfo.value, PermanentError if transient else TransientError)
    for fragment in fragments:
        assert fragment in excinfo.value.message
    if isinstance(excinfo.value, TransientError):
        assert excinfo.value.retry_after_s == retry_after


def test_connection_error_is_transient(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.replies.append(httpx2.ConnectError("boom"))
    with pytest.raises(TransientError, match="connection error"):
        client.complete(make_request())


def test_timeout_is_transient(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.replies.append(httpx2.ReadTimeout("slow"))
    with pytest.raises(TransientError, match="timed out"):
        client.complete(make_request())


def _error_event(error_type: str) -> list[dict[str, Any]]:
    events = message_events("x")[:1]
    events.append({"type": "error", "error": {"type": error_type, "message": "mid-stream"}})
    return events


def test_mid_stream_overloaded_is_transient(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.replies.append(stream_reply(_error_event("overloaded_error")))
    with pytest.raises(TransientError, match="overloaded_error"):
        client.complete(make_request())


def test_mid_stream_invalid_request_is_permanent(
    stub: AnthropicStub, client: AnthropicClient
) -> None:
    stub.replies.append(stream_reply(_error_event("invalid_request_error")))
    with pytest.raises(PermanentError, match="invalid_request_error"):
        client.complete(make_request())


def test_count_tokens_errors_mapped(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.count = error_reply(529, "overloaded_error", "busy")
    with pytest.raises(TransientError):
        client.count_tokens(make_request())
    assert stub.message_requests() == []


def test_count_tokens_value(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.count = 4321
    assert client.count_tokens(make_request()) == 4321


def test_needs_chunking_via_settings(stub: AnthropicStub, tmp_path: Any) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        llm_context_tokens=5000,
        llm_api_key=SecretStr(KEY),
    )
    stub.count = 4500
    client = make_client(stub, AnthropicOptions.from_settings(settings))
    analyzer = LlmAnalyzer(ArtifactStore(settings.data_dir), client)
    try:
        with pytest.raises(NeedsChunking) as excinfo:
            analyzer.analyze(sample_episode(), sample_transcript(), None)
    finally:
        analyzer.close()
    assert excinfo.value.input_tokens == 4500
    assert stub.message_requests() == []


def test_end_to_end_through_the_sdk(stub: AnthropicStub, tmp_path: Any) -> None:
    stub.replies.append(stream_reply(message_events(valid_wire_json())))
    artifacts = ArtifactStore(tmp_path / "data")
    analyzer = LlmAnalyzer(artifacts, make_client(stub))
    try:
        analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
        again = analyzer.analyze(sample_episode(), sample_transcript(), None)
    finally:
        analyzer.close()
    assert again == analysis
    assert analysis.summary == "A summary."
    assert len(stub.message_requests()) == 1
    assert len(stub.count_requests()) == 1


def test_build_anthropic_analyzer(tmp_path: Any) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", llm_api_key=SecretStr(KEY))
    analyzer = build_anthropic_analyzer(settings, ArtifactStore(settings.data_dir))
    try:
        assert isinstance(analyzer.client, AnthropicClient)
        assert analyzer.client.options.effort == "medium"
        assert analyzer.client.options.fallbacks is True
    finally:
        analyzer.close()


@pytest.mark.parametrize("unquieted", [False, True])
def test_key_not_logged(
    stub: AnthropicStub, tmp_path: Any, restore_root_logging: None, unquieted: bool
) -> None:
    buf = io.StringIO()
    configure_logging("DEBUG", secrets=(), stream=buf)
    if unquieted:
        # The SDK logs request bodies at DEBUG (hence the quieting), but never the key.
        logging.getLogger("anthropic").setLevel(logging.DEBUG)
    transcript = sample_transcript()
    segments = (transcript.segments[0].model_copy(update={"text": TRANSCRIPT_SENTINEL}),)
    transcript = transcript.model_copy(update={"segments": segments})
    stub.replies.append(stream_reply(message_events(valid_wire_json())))
    stub.replies.append(error_reply(401, "authentication_error", "bad key"))
    analyzer = LlmAnalyzer(ArtifactStore(tmp_path / "data"), make_client(stub))
    try:
        analyzer.analyze(sample_episode(), transcript, None)
        with pytest.raises(PermanentError):
            analyzer.analyze(sample_episode(), transcript, "another prompt")
    finally:
        analyzer.close()
    logged = buf.getvalue()
    assert KEY not in logged
    if not unquieted:
        assert TRANSCRIPT_SENTINEL not in logged


def test_error_detail_redacts_key(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.replies.append(error_reply(400, "invalid_request_error", f"bad key {KEY} here"))
    with pytest.raises(PermanentError) as excinfo:
        client.complete(make_request())
    assert KEY not in excinfo.value.message
    assert "**********" in excinfo.value.message


def test_error_detail_is_capped(stub: AnthropicStub, client: AnthropicClient) -> None:
    stub.replies.append(error_reply(400, "invalid_request_error", "x" * 5000))
    with pytest.raises(PermanentError) as excinfo:
        client.complete(make_request())
    assert len(excinfo.value.message) < 400


def test_repr_hides_key(client: AnthropicClient) -> None:
    assert KEY not in repr(client.options)
    assert KEY not in repr(client)
    assert KEY not in repr(make_options())
