import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from earmark.config import REDACTED
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fakes import FakeAnalyzer, FakeTranscriber, make_fake_backends
from earmark.models import Analysis, Episode, Job, Transcript, prompt_hash
from earmark.pipeline.runner import MAX_ERROR_CHARS, describe_error
from tests.factories import T0, make_analysis, make_episode, make_transcript
from tests.pipeline_env import (
    CANON,
    SECRET,
    BlockingTranscriber,
    CountingJobStore,
    PipelineEnv,
    counting_store,
    make_env,
)


def _run(env: PipelineEnv) -> Job:
    job = env.worker.run_once()
    assert job is not None
    return job


def _enqueue_and_run(env: PipelineEnv, prompt: str | None = None) -> Job:
    env.jobs.enqueue(CANON, prompt=prompt)
    return _run(env)


def _stored(env: PipelineEnv, job: Job) -> Job:
    found = env.jobs.get(job.id)
    assert found is not None
    return found


# -- A2: caching ------------------------------------------------------------------------


def test_same_prompt_reuses_everything(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = _enqueue_and_run(env, "q?")
    assert first.status == "done"
    assert first.result is not None
    assert first.result.note_path is not None
    note = env.vault / first.result.note_path
    first_bytes = note.read_bytes()
    second = _enqueue_and_run(env, "q?")
    assert second.status == "done"
    f = env.fakes
    assert f.fetcher.call_count == 1
    assert f.transcriber.call_count == 1
    assert f.analyzer.computed == 1
    assert f.resolver.call_count == 2
    assert f.store.call_count == 2
    assert note.read_bytes() == first_bytes


def test_new_prompt_runs_only_analyzer(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue_and_run(env, None)
    _enqueue_and_run(env, "What papers are cited?")
    f = env.fakes
    assert f.fetcher.call_count == 1
    assert f.transcriber.call_count == 1
    assert f.analyzer.computed == 2
    analyses = f.store.calls[1].analyses
    assert len(analyses) == 2
    assert list(analyses) == sorted(analyses, key=lambda a: (a.created_at, a.prompt_hash))
    assert {a.prompt for a in analyses} == {None, "What papers are cited?"}


def test_cached_transcript_skips_fetch_even_without_audio(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = _enqueue_and_run(env, None)
    assert first.episode_key is not None
    assert env.fakes.artifacts.delete_audio(first.episode_key)
    second = _enqueue_and_run(env, "new?")
    assert second.status == "done"
    assert env.fakes.fetcher.call_count == 1
    assert env.fakes.transcriber.call_count == 1


# -- A3 to A6: outcomes -----------------------------------------------------------------


def test_skip_episode(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.resolver.fail_next(SkipEpisode("subscriber-only episode"))
    job = _enqueue_and_run(env)
    assert job.status == "skipped"
    assert job.error == "subscriber-only episode"
    assert job.attempts == 1
    assert job.stage == "resolve"
    assert [(c.job, c.episode) for c in env.fakes.notifier.calls] == [(job, None)]
    env.clock.advance(7 * 3600)
    assert env.worker.run_once() is None


def test_transient_requeues(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(TransientError("asr busy"))
    job = _enqueue_and_run(env)
    assert (job.status, job.attempts, job.error) == ("queued", 1, "asr busy")
    assert job.run_after == env.clock.now + timedelta(seconds=60)
    assert env.fakes.notifier.call_count == 0
    assert env.worker.run_once() is None
    env.clock.advance(60)
    assert _run(env).status == "done"


def test_transient_retry_after(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(TransientError("x", retry_after_s=600))
    job = _enqueue_and_run(env)
    assert job.run_after == env.clock.now + timedelta(seconds=600)


def test_transient_exhausted_fails_and_notifies(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    env.fakes.transcriber.fail_next(TransientError("asr busy"), times=2)
    assert _enqueue_and_run(env).status == "queued"
    assert env.fakes.notifier.call_count == 0
    env.clock.advance(60)
    last = _run(env)
    assert last.status == "failed"
    assert [c.job for c in env.fakes.notifier.calls] == [last]


def test_permanent_fails(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.analyzer.fail_next(PermanentError("bad transcript"))
    job = _enqueue_and_run(env)
    assert (job.status, job.error, job.stage, job.attempts) == (
        "failed",
        "bad transcript",
        "analyze",
        1,
    )
    [call] = env.fakes.notifier.calls
    assert call.job == job
    assert call.episode is not None
    env.clock.advance(7 * 3600)
    assert env.worker.run_once() is None


def test_unexpected_exception_is_transient(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = make_env(tmp_path)
    env.fakes.fetcher.fail_next(ValueError("boom-7c1e"))
    with caplog.at_level(logging.WARNING):
        job = _enqueue_and_run(env)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.run_after == env.clock.now + timedelta(seconds=60)
    assert job.error is not None
    assert job.error.startswith("unexpected error: ValueError: boom-7c1e (at ")
    assert "fakes.py:" in job.error
    assert job.error.endswith(" in _enter)")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "earmark.pipeline.runner"
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is ValueError
    assert "Traceback" in logging.Formatter().format(errors[0])
    assert env.fakes.notifier.call_count == 0
    env.clock.advance(60)
    assert _run(env).status == "done"


# -- A7: indexer and notifier failures ---------------------------------------------------


def test_index_failure_is_warning(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.indexer.fail_next(RuntimeError("index down"))
    job = _enqueue_and_run(env)
    assert job.status == "done"
    assert job.result is not None
    [warning] = job.warnings
    assert warning.startswith("index failed: unexpected error: RuntimeError: index down")
    [call] = env.fakes.notifier.calls
    assert warning in call.job.warnings


def test_index_transient_is_warning(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.indexer.fail_next(TransientError("index busy"))
    job = _enqueue_and_run(env)
    assert job.status == "done"
    assert job.warnings == ("index failed: index busy",)


def test_notify_failure_is_warning(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.notifier.fail_next(RuntimeError("ntfy down"))
    job = _enqueue_and_run(env)
    stored = _stored(env, job)
    assert stored.status == "done"
    [warning] = stored.warnings
    assert warning.startswith("notify failed: ")


def test_notify_failure_on_failed_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.analyzer.fail_next(PermanentError("bad"))
    env.fakes.notifier.fail_next(RuntimeError("ntfy down"))
    job = _enqueue_and_run(env)
    stored = _stored(env, job)
    assert stored.status == "failed"
    assert [w.startswith("notify failed: ") for w in stored.warnings] == [True]


# -- A8: notification --------------------------------------------------------------------


def _clean(env: PipelineEnv) -> None:
    """No failure to inject."""


def _permanent(env: PipelineEnv) -> None:
    env.fakes.resolver.fail_next(PermanentError("nope"))


def _skip(env: PipelineEnv) -> None:
    env.fakes.resolver.fail_next(SkipEpisode("no feed"))


@pytest.mark.parametrize(
    ("setup", "status"), [(_clean, "done"), (_permanent, "failed"), (_skip, "skipped")]
)
def test_notifier_runs_for_each_terminal_status(
    tmp_path: Path, setup: Callable[[PipelineEnv], None], status: str
) -> None:
    env = make_env(tmp_path)
    setup(env)
    job = _enqueue_and_run(env)
    assert job.status == status
    [call] = env.fakes.notifier.calls
    assert call.job.status == status
    assert call.job == _stored(env, job)


# -- B1: order ---------------------------------------------------------------------------


def test_stage_order_and_inputs(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    env.jobs.enqueue(CANON, prompt="why?")
    job = _run(env)
    assert counting_store(env).stages == [
        "resolve",
        "fetch",
        "transcribe",
        "analyze",
        "store",
        "index",
    ]
    key = job.episode_key
    assert key is not None
    assert env.fakes.transcriber.calls[0].audio == env.fakes.artifacts.find_audio(key)
    assert env.fakes.analyzer.calls[0].prompt == "why?"
    assert env.fakes.analyzer.calls[0].prompt == job.prompt
    assert job.stage == "index"


def test_stage_order_cached_transcript(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    _enqueue_and_run(env)
    store = counting_store(env)
    store.stages.clear()
    _enqueue_and_run(env, "again?")
    assert store.stages == ["resolve", "analyze", "store", "index"]


def test_index_none_skips_index(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), indexer=None))
    job = _enqueue_and_run(env)
    assert (job.status, job.stage) == ("done", "store")
    assert fakes.indexer.call_count == 0


def test_notifier_none(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), notifier=None))
    assert _enqueue_and_run(env).status == "done"
    assert fakes.notifier.call_count == 0


# -- B2: warnings ------------------------------------------------------------------------


def test_stage_warnings_copied(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    fakes.transcriber = FakeTranscriber(fakes.artifacts, warnings=("no segments",))
    fakes.analyzer = FakeAnalyzer(fakes.artifacts, warnings=("dropped 2 timestamps",))
    env = make_env(tmp_path, fakes=fakes)
    expected = ("no segments", "dropped 2 timestamps")
    assert _enqueue_and_run(env).warnings == expected
    assert _enqueue_and_run(env).warnings == expected


# -- B3: shared_at -----------------------------------------------------------------------


def test_shared_at_is_first_share(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue_and_run(env, None)
    env.clock.advance(3600)
    _enqueue_and_run(env, "second?")
    assert [c.shared_at for c in env.fakes.store.calls] == [T0, T0]
    earlier = T0 - timedelta(days=1)
    env.jobs.enqueue(CANON, prompt="third?", shared_at=earlier)
    _run(env)
    assert env.fakes.store.calls[2].shared_at == earlier


# -- B4: lease lost ----------------------------------------------------------------------


def test_lease_lost_mid_run(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    holder: list[PipelineEnv] = []

    def steal() -> None:
        env = holder[0]
        env.clock.advance(901)
        assert len(env.jobs.requeue_stale()) == 1

    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    backends = replace(fakes.backends(), transcriber=BlockingTranscriber(fakes.transcriber, steal))
    env = make_env(tmp_path, heartbeat_s=100, fakes=fakes, backends=backends)
    holder.append(env)
    enqueued = env.jobs.enqueue(CANON)
    claim = env.jobs.claim_next()
    assert claim is not None
    with caplog.at_level(logging.WARNING):
        assert env.pipeline.run(claim) is None
    job = _stored(env, enqueued.job)
    assert job.status == "queued"
    assert job.error is not None
    assert job.error.startswith("worker stopped responding")
    assert job.result is None
    assert fakes.analyzer.call_count == 0
    assert fakes.store.call_count == 0
    assert fakes.notifier.call_count == 0
    assert any(job.id in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


# -- B5: error text ----------------------------------------------------------------------


def _raised(exc: Exception) -> Exception:
    try:
        raise exc
    except (ValueError, KeyError) as caught:
        return caught


def test_describe_error() -> None:
    assert describe_error(PermanentError("plain message")) == "plain message"
    multi = describe_error(_raised(ValueError("line one\n  line two\r\n\tline three")))
    assert "\n" not in multi
    assert multi.startswith("unexpected error: ValueError: line one line two line three (at ")
    empty = describe_error(_raised(KeyError()))
    assert empty.startswith("unexpected error: KeyError (at test_runner.py:")
    assert "KeyError:" not in empty
    long = describe_error(_raised(ValueError("x" * 2000)))
    assert len(long) == MAX_ERROR_CHARS
    assert long.endswith("…")
    never_raised = describe_error(ValueError("fresh"))
    assert never_raised == "unexpected error: ValueError: fresh"
    assert describe_error(PermanentError("   \n ")) == "PermanentError"


def test_describe_error_redacts() -> None:
    assert describe_error(PermanentError("a secret-xyz b"), ["secret-xyz"]) == f"a {REDACTED} b"


def test_unexpected_error_redacts_secrets(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.fetcher.fail_next(ValueError(f"token {SECRET} leaked"))
    job = _enqueue_and_run(env)
    assert job.error is not None
    assert REDACTED in job.error
    assert SECRET not in job.error


# -- B6: defensive checks ----------------------------------------------------------------


class _WrongTranscriber:
    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript:
        return make_transcript(guid="urn:example:other")


class _ForgetfulAnalyzer:
    def analyze(self, episode: Episode, transcript: Transcript, prompt: str | None, /) -> Analysis:
        return make_analysis(prompt=prompt, model="fake-llm").model_copy(
            update={"episode_key": episode.key}
        )


def test_transcript_for_other_episode_fails(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    backends = replace(fakes.backends(), transcriber=_WrongTranscriber())
    env = make_env(tmp_path, fakes=fakes, backends=backends)
    job = _enqueue_and_run(env)
    assert job.status == "failed"
    assert job.attempts == 1
    assert job.error == "transcriber returned a transcript for another episode"


def test_analysis_not_written_still_stored(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    env = make_env(
        tmp_path, fakes=fakes, backends=replace(fakes.backends(), analyzer=_ForgetfulAnalyzer())
    )
    job = _enqueue_and_run(env, "q?")
    assert job.status == "done"
    [call] = fakes.store.calls
    assert len(call.analyses) == 1
    assert call.analyses[0].prompt_hash == prompt_hash("q?", "fake-llm")
    assert fakes.artifacts.list_analyses(call.episode.key) == []


def test_reanalyze_job_fails_permanently(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.jobs.enqueue_reanalyze(make_episode().key)
    job = _run(env)
    assert job.status == "failed"
    assert job.error == "reanalyze jobs are not supported yet"
    assert job.stage is None
    assert env.fakes.resolver.call_count == 0


def test_describe_error_scrubs_surrogates() -> None:
    text = describe_error(PermanentError("bad \ud800 text"))
    assert text == "bad \ufffd text"
    text.encode("utf-8")
    text = describe_error(_raised(ValueError("oops \udfff")))
    assert "\ufffd" in text
    text.encode("utf-8")
