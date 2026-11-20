import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from earmark.analyze.analyzer import LlmAnalyzer, effective_duration
from earmark.analyze.llm.anthropic import AnthropicClient
from earmark.analyze.llm.base import LLMClient, LLMResponse
from earmark.analyze.prompts import SYSTEM_PROMPT
from earmark.artifacts import ArtifactStore
from earmark.errors import NeedsChunking, PermanentError, TransientError
from earmark.fakes import make_fake_backends
from earmark.models import Segment, prompt_hash
from earmark.pipeline.stages import Analyzer
from tests.analysis_samples import sample_episode, sample_transcript, valid_wire_json, wire_payload
from tests.anthropic_stub import (
    AnthropicStub,
    error_reply,
    make_client,
    message_events,
    stream_reply,
)
from tests.llm_fakes import ScriptedLLMClient
from tests.pipeline_env import CANON, PipelineEnv, make_env

NOW = datetime(2026, 5, 1, tzinfo=UTC)


def make_analyzer(
    tmp_path: Path, client: ScriptedLLMClient | None = None
) -> tuple[LlmAnalyzer, ScriptedLLMClient, ArtifactStore]:
    client = client or ScriptedLLMClient()
    artifacts = ArtifactStore(tmp_path / "data")
    return LlmAnalyzer(artifacts, client, clock=lambda: NOW), client, artifacts


def end(text: str | None = None) -> LLMResponse:
    return LLMResponse(text=text if text is not None else valid_wire_json(), stop="end")


def test_fake_satisfies_protocol(tmp_path: Path) -> None:
    scripted: LLMClient = ScriptedLLMClient()
    analyzer: Analyzer = LlmAnalyzer(ArtifactStore(tmp_path), scripted)
    stub = AnthropicStub()
    real: LLMClient = make_client(stub)
    try:
        assert analyzer is not None
        assert isinstance(real, AnthropicClient)
    finally:
        real.close()


def test_effective_duration() -> None:
    t = sample_transcript()
    assert effective_duration(t) == 600.4
    longer = t.model_copy(update={"segments": (Segment(start=0, end=700.0, text="x"),)})
    assert effective_duration(longer) == 700.0


def test_writes_analysis_file(tmp_path: Path) -> None:
    analyzer, client, artifacts = make_analyzer(tmp_path)
    episode = sample_episode()
    analysis = analyzer.analyze(episode, sample_transcript(), "Q?")
    digest = prompt_hash("Q?", client.model)
    path = artifacts.analysis_path(episode.key, digest)
    assert path.is_file()
    assert artifacts.read_analysis(episode.key, digest) == analysis
    assert analysis.created_at == NOW
    assert analysis.model == "claude-test"


def test_cache_hit_makes_no_call(tmp_path: Path) -> None:
    analyzer, client, _ = make_analyzer(tmp_path)
    first = analyzer.analyze(sample_episode(), sample_transcript(), None)
    second = analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert second == first
    assert (client.count_calls, client.complete_calls) == (1, 1)


def test_prewritten_file_makes_zero_calls(tmp_path: Path) -> None:
    first, _, artifacts = make_analyzer(tmp_path)
    analysis = first.analyze(sample_episode(), sample_transcript(), None)
    fresh_client = ScriptedLLMClient()
    fresh = LlmAnalyzer(artifacts, fresh_client)
    assert fresh.analyze(sample_episode(), sample_transcript(), None) == analysis
    assert (fresh_client.count_calls, fresh_client.complete_calls) == (0, 0)


def test_new_prompt_misses_cache(tmp_path: Path) -> None:
    analyzer, client, _ = make_analyzer(tmp_path)
    analyzer.analyze(sample_episode(), sample_transcript(), None)
    analyzer.analyze(sample_episode(), sample_transcript(), "another")
    assert client.complete_calls == 2


def test_other_model_misses_cache(tmp_path: Path) -> None:
    _, _, artifacts = make_analyzer(tmp_path)
    for model in ("claude-a", "claude-b"):
        client = ScriptedLLMClient(model=model)
        LlmAnalyzer(artifacts, client).analyze(sample_episode(), sample_transcript(), None)
        assert client.complete_calls == 1


def test_mismatched_transcript_is_permanent(tmp_path: Path) -> None:
    analyzer, client, _ = make_analyzer(tmp_path)
    with pytest.raises(PermanentError, match="another episode"):
        analyzer.analyze(sample_episode(), sample_transcript(guid="other"), None)
    assert client.count_calls == 0


def test_empty_transcript_is_permanent(tmp_path: Path) -> None:
    analyzer, client, _ = make_analyzer(tmp_path)
    blank = sample_transcript(segments=(Segment(start=0, end=5, text="  "),))
    with pytest.raises(PermanentError, match="no text to analyze"):
        analyzer.analyze(sample_episode(), blank, None)
    assert (client.count_calls, client.complete_calls) == (0, 0)


def test_whole_transcript_sent(tmp_path: Path) -> None:
    segments = tuple(
        Segment(start=i * 5.0, end=i * 5.0 + 5.0, text=f"unique line number {i}")
        for i in range(2160)
    )
    transcript = sample_transcript(segments=segments).model_copy(update={"duration_s": 10800.0})
    analyzer, client, _ = make_analyzer(tmp_path)
    analyzer.analyze(sample_episode(), transcript, None)
    (request,) = client.complete_requests
    assert client.count_requests == [request]
    lines = transcript.to_text().splitlines()
    assert len(lines) == 2160
    assert all(line in request.user for line in lines)
    assert len(request.user) > len(transcript.to_text())


def test_system_prompt_sent(tmp_path: Path) -> None:
    analyzer, client, _ = make_analyzer(tmp_path)
    analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_requests[0].system is SYSTEM_PROMPT
    assert client.complete_requests[0].max_tokens == 64_000


# -- validation ---------------------------------------------------------------------------


def test_schema_mismatch_is_permanent(tmp_path: Path) -> None:
    secret_text = "model-said-this-secret"
    bad = json.dumps(wire_payload(summary=5, sneaky=secret_text))
    analyzer, _, artifacts = make_analyzer(tmp_path, ScriptedLLMClient([end(bad)]))
    with pytest.raises(PermanentError) as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    message = excinfo.value.message
    assert "2 errors" in message
    assert "first at" in message
    assert secret_text not in message
    assert not artifacts.analyses_dir(sample_episode().key).exists()


@pytest.mark.parametrize("text", ["not json {secret-model-text", ""])
def test_invalid_json_is_permanent(tmp_path: Path, text: str) -> None:
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([end(text)]))
    with pytest.raises(PermanentError) as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert "1 error, first at (root)" in excinfo.value.message
    assert "secret-model-text" not in excinfo.value.message


def test_dropped_timestamps_become_analysis_warnings(tmp_path: Path) -> None:
    late = valid_wire_json(key_claims=[{"text": "c", "timestamp": "09:00:00"}])
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([end(late)]))
    analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert "dropped 1 timestamp beyond the episode's duration (00:10:00)" in analysis.warnings


def test_client_warnings_recorded(tmp_path: Path) -> None:
    response = LLMResponse(text=valid_wire_json(), stop="end", warnings=("B answered instead",))
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([response]))
    analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert analysis.warnings == ("B answered instead",)


# -- stop reasons -------------------------------------------------------------------------


def test_refusal_permanent_with_category(tmp_path: Path) -> None:
    refusal = LLMResponse(text="", stop="refusal", raw_stop="refusal", refusal_category="bio")
    analyzer, _, artifacts = make_analyzer(tmp_path, ScriptedLLMClient([refusal]))
    with pytest.raises(PermanentError, match="declined") as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert "bio" in excinfo.value.message
    assert not artifacts.analyses_dir(sample_episode().key).exists()


def test_refusal_without_category(tmp_path: Path) -> None:
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([LLMResponse("", "refusal")]))
    with pytest.raises(PermanentError, match="unspecified"):
        analyzer.analyze(sample_episode(), sample_transcript(), None)


def test_max_tokens_retries_once_with_larger_cap(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    script = [LLMResponse("{", "max_tokens"), end()]
    analyzer, client, artifacts = make_analyzer(tmp_path, ScriptedLLMClient(script))
    with caplog.at_level(logging.WARNING, logger="earmark.analyze.analyzer"):
        analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert [r.max_tokens for r in client.complete_requests] == [64_000, 128_000]
    assert client.count_calls == 1
    assert artifacts.read_analysis(analysis.episode_key, analysis.prompt_hash) == analysis
    assert "hit the 64000-token output cap" in caplog.text


def test_max_tokens_twice_is_permanent(tmp_path: Path) -> None:
    script = [LLMResponse("{", "max_tokens"), LLMResponse("{", "max_tokens")]
    analyzer, client, _ = make_analyzer(tmp_path, ScriptedLLMClient(script))
    with pytest.raises(PermanentError, match="128000"):
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_calls == 2


def test_no_retry_when_already_at_max(tmp_path: Path) -> None:
    client = ScriptedLLMClient([LLMResponse("{", "max_tokens")], output_tokens=128_000)
    analyzer, _, _ = make_analyzer(tmp_path, client)
    with pytest.raises(PermanentError, match="128000"):
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_calls == 1


def test_retry_cap_limited_by_context(tmp_path: Path) -> None:
    client = ScriptedLLMClient(
        [LLMResponse("{", "max_tokens"), end()],
        context_tokens=100_000,
        output_tokens=10_000,
        token_count=20_000,
    )
    analyzer, _, _ = make_analyzer(tmp_path, client)
    analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert [r.max_tokens for r in client.complete_requests] == [10_000, 80_000]


def test_unexpected_stop_is_permanent(tmp_path: Path) -> None:
    response = LLMResponse("", "other", raw_stop="pause_turn")
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([response]))
    with pytest.raises(PermanentError, match="pause_turn"):
        analyzer.analyze(sample_episode(), sample_transcript(), None)


def test_client_errors_propagate(tmp_path: Path) -> None:
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([TransientError("later")]))
    with pytest.raises(TransientError, match="later"):
        analyzer.analyze(sample_episode(), sample_transcript(), None)


# -- chunking -----------------------------------------------------------------------------


def test_needs_chunking_before_completion(tmp_path: Path) -> None:
    client = ScriptedLLMClient(context_tokens=10_000, token_count=9_000, output_tokens=2_000)
    analyzer, _, artifacts = make_analyzer(tmp_path, client)
    with pytest.raises(NeedsChunking) as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    exc = excinfo.value
    assert isinstance(exc, PermanentError)
    assert (exc.input_tokens, exc.limit_tokens) == (9000, 8000)
    assert "EARMARK_LLM_CONTEXT_TOKENS" in exc.message
    assert "not truncated" in exc.message
    assert client.complete_calls == 0
    assert not artifacts.analyses_dir(sample_episode().key).exists()
    (counted,) = client.count_requests
    assert all(line in counted.user for line in sample_transcript().to_text().splitlines())


def test_exact_fit_is_allowed(tmp_path: Path) -> None:
    client = ScriptedLLMClient(context_tokens=10_000, token_count=9_000, output_tokens=1_000)
    analyzer, _, _ = make_analyzer(tmp_path, client)
    analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_calls == 1


def test_context_exceeded_stop_raises_needs_chunking(tmp_path: Path) -> None:
    response = LLMResponse("", "context_exceeded", raw_stop="model_context_window_exceeded")
    analyzer, _, _ = make_analyzer(tmp_path, ScriptedLLMClient([response]))
    with pytest.raises(NeedsChunking):
        analyzer.analyze(sample_episode(), sample_transcript(), None)


def test_close_closes_client(tmp_path: Path) -> None:
    analyzer, client, _ = make_analyzer(tmp_path)
    analyzer.close()
    assert client.closed
    assert analyzer.client is client


# -- pipeline integration -----------------------------------------------------------------


def _pipeline(tmp_path: Path, stub: AnthropicStub) -> tuple[PipelineEnv, AnthropicClient]:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    client = make_client(stub)
    analyzer = LlmAnalyzer(fakes.artifacts, client)
    backends = dataclasses.replace(fakes.backends(), analyzer=analyzer)
    env = make_env(tmp_path, fakes=fakes, backends=backends)
    return env, client


def _reply_ok(stub: AnthropicStub, n: int = 1) -> None:
    for _ in range(n):
        stub.replies.append(stream_reply(message_events(valid_wire_json())))


def test_pipeline_job_done_with_llm_analyzer(tmp_path: Path) -> None:
    stub = AnthropicStub()
    _reply_ok(stub)
    env, client = _pipeline(tmp_path, stub)
    try:
        env.jobs.enqueue(CANON, prompt=None)
        job = env.worker.run_once()
    finally:
        client.close()
    assert job is not None
    assert job.status == "done"
    assert job.result is not None
    assert job.result.analysis_hash == prompt_hash(None, "claude-opus-5-5")
    assert env.fakes.store.call_count == 1
    assert env.fakes.store.calls[0].analyses[0].model == "claude-opus-5-5"


def test_pipeline_repeat_share_uses_cache(tmp_path: Path) -> None:
    stub = AnthropicStub()
    _reply_ok(stub, 2)
    env, client = _pipeline(tmp_path, stub)
    try:
        for prompt, expected in ((None, 1), (None, 1), ("a new prompt", 2)):
            env.jobs.enqueue(CANON, prompt=prompt)
            job = env.worker.run_once()
            assert job is not None
            assert job.status == "done"
            assert len(stub.count_requests()) == expected
            assert len(stub.message_requests()) == expected
    finally:
        client.close()


def test_pipeline_refusal_fails_job(tmp_path: Path) -> None:
    stub = AnthropicStub()
    details = {"type": "refusal", "category": "cyber", "explanation": None}
    stub.replies.append(
        stream_reply(message_events("", stop_reason="refusal", stop_details=details))
    )
    env, client = _pipeline(tmp_path, stub)
    try:
        env.jobs.enqueue(CANON, prompt=None)
        job = env.worker.run_once()
    finally:
        client.close()
    assert job is not None
    assert job.status == "failed"
    assert job.attempts == 1
    assert job.error is not None
    assert "cyber" in job.error


def test_pipeline_rate_limit_requeues(tmp_path: Path) -> None:
    stub = AnthropicStub()
    stub.replies.append(error_reply(429, "rate_limit_error", "slow", headers={"retry-after": "17"}))
    env, client = _pipeline(tmp_path, stub)
    try:
        env.jobs.enqueue(CANON, prompt=None)
        job = env.worker.run_once()
    finally:
        client.close()
    assert job is not None
    assert job.status == "queued"
    assert job.run_after >= env.clock.now + timedelta(seconds=17)


def test_pipeline_connection_error_requeues(tmp_path: Path) -> None:
    stub = AnthropicStub()
    stub.replies.append(httpx2.ConnectError("down"))
    env, client = _pipeline(tmp_path, stub)
    try:
        env.jobs.enqueue(CANON, prompt=None)
        job = env.worker.run_once()
    finally:
        client.close()
    assert job is not None
    assert job.status == "queued"


def test_warnings_reach_the_job(tmp_path: Path) -> None:
    stub = AnthropicStub()
    late = valid_wire_json(key_claims=[{"text": "c", "timestamp": "99:00:00"}])
    stub.replies.append(stream_reply(message_events(late)))
    env, client = _pipeline(tmp_path, stub)
    try:
        env.jobs.enqueue(CANON, prompt=None)
        job = env.worker.run_once()
    finally:
        client.close()
    assert job is not None
    assert job.status == "done"
    assert any("beyond the episode's duration" in w for w in job.warnings)
