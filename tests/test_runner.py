import hashlib
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from earmark.config import REDACTED, Settings
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fakes import FakeAudioFetcher, FakeTranscriber, FakeYoutubeSource, make_fake_backends
from earmark.models import Episode, Job, JobResult, Segment, Transcript, content_key, youtube_key
from earmark.pipeline.runner import MAX_ERROR_CHARS, describe_error
from earmark.staging import StagedUpload
from earmark.transcribe.base import UnusableAudioError
from earmark.transcribe.faster_whisper import file_key
from earmark.youtube.links import youtube_episode
from tests.factories import (
    T0,
    YOUTUBE_ID,
    make_transcript,
    new_podcast_job,
    new_upload_job,
    new_url_job,
    new_youtube_job,
)
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


def _enqueue_and_run(env: PipelineEnv, prompt: str = "") -> Job:
    env.jobs.enqueue(new_podcast_job(CANON, instructions=prompt))
    return _run(env)


def _stored(env: PipelineEnv, job: Job) -> Job:
    found = env.jobs.get(job.id)
    assert found is not None
    return found


# -- A2: caching ------------------------------------------------------------------------


def test_reshare_reuses_transcript(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = _enqueue_and_run(env)
    assert first.status == "done"
    assert first.result is not None
    second = _enqueue_and_run(env, "again?")
    assert second.status == "done"
    assert second.result == first.result
    f = env.fakes
    assert f.fetcher.call_count == f.fetcher.computed == 1
    assert f.transcriber.call_count == f.transcriber.computed == 1
    assert f.resolver.call_count == 2


def test_cached_transcript_skips_fetch_even_without_audio(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first = _enqueue_and_run(env)
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
    env.fakes.transcriber.fail_next(PermanentError("bad transcript"))
    job = _enqueue_and_run(env)
    assert (job.status, job.error, job.stage, job.attempts) == (
        "failed",
        "bad transcript",
        "transcribe",
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
    env.fakes.transcriber.fail_next(PermanentError("bad"))
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
    env.jobs.enqueue(new_podcast_job(CANON, instructions="why?"))
    job = _run(env)
    assert counting_store(env).stages == ["resolve", "fetch", "transcribe", "deliver"]
    key = job.episode_key
    assert key is not None
    assert env.fakes.transcriber.calls[0].audio == env.fakes.artifacts.find_audio(key)
    assert job.stage == "deliver"


def test_stage_order_cached_transcript(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    _enqueue_and_run(env)
    store = counting_store(env)
    store.stages.clear()
    _enqueue_and_run(env, "again?")
    assert store.stages == ["resolve", "deliver"]


def test_notifier_none(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), notifier=None))
    assert _enqueue_and_run(env).status == "done"
    assert fakes.notifier.call_count == 0


# -- B2: warnings ------------------------------------------------------------------------


def test_stage_warnings_copied(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    fakes.transcriber = FakeTranscriber(fakes.artifacts, warnings=("no segments",))
    env = make_env(tmp_path, fakes=fakes)
    assert _enqueue_and_run(env).warnings == ("no segments",)
    assert _enqueue_and_run(env, "again?").warnings == ("no segments",)


# -- B3: shared_at -----------------------------------------------------------------------


def test_shared_at_is_first_share(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue_and_run(env)
    env.clock.advance(3600)
    _enqueue_and_run(env, "second?")
    key = env.fakes.resolver.resolve(CANON).key
    share = env.fakes.artifacts.read_share(key)
    assert share is not None
    assert share.first_shared_at == T0
    earlier = T0 - timedelta(days=1)
    env.jobs.enqueue(new_podcast_job(CANON, instructions="third?", shared_at=earlier))
    _run(env)
    share = env.fakes.artifacts.read_share(key)
    assert share is not None
    assert share.first_shared_at == earlier


# -- B4: lease lost ----------------------------------------------------------------------


def test_lease_lost_mid_run(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    holder: list[PipelineEnv] = []

    def steal() -> None:
        env = holder[0]
        env.clock.advance(901)
        assert len(env.jobs.requeue_stale()) == 1

    fakes = make_fake_backends(tmp_path / "data")
    backends = replace(fakes.backends(), transcriber=BlockingTranscriber(fakes.transcriber, steal))
    env = make_env(tmp_path, heartbeat_s=100, fakes=fakes, backends=backends)
    holder.append(env)
    enqueued = env.jobs.enqueue(new_podcast_job(CANON))
    claim = env.jobs.claim_next()
    assert claim is not None
    with caplog.at_level(logging.WARNING):
        assert env.pipeline.run(claim) is None
    job = _stored(env, enqueued.job)
    assert job.status == "queued"
    assert job.error is not None
    assert job.error.startswith("worker stopped responding")
    assert job.result is None
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
    def cached(self, episode: Episode, /) -> Transcript | None:
        return None

    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript:
        return make_transcript(guid="urn:example:other")


def test_transcript_for_other_episode_fails(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    backends = replace(fakes.backends(), transcriber=_WrongTranscriber())
    env = make_env(tmp_path, fakes=fakes, backends=backends)
    job = _enqueue_and_run(env)
    assert job.status == "failed"
    assert job.attempts == 1
    assert job.error == "transcriber returned a transcript for another episode"


def test_describe_error_scrubs_surrogates() -> None:
    text = describe_error(PermanentError("bad \ud800 text"))
    assert text == "bad \ufffd text"
    text.encode("utf-8")
    text = describe_error(_raised(ValueError("oops \udfff")))
    assert "\ufffd" in text
    text.encode("utf-8")


def test_describe_error_redacts_bare_ntfy_topic() -> None:
    settings = Settings(_env_file=None, ntfy_url=SecretStr("https://ntfy.test/sentinel-topic-404x"))
    text = describe_error(PermanentError("bad topic sentinel-topic-404x"), settings.secret_values())
    assert "sentinel-topic-404x" not in text
    assert text == f"bad topic {REDACTED}"


def test_describe_error_scrubs_library_urls() -> None:
    exc = _raised(
        ValueError(
            "Client error '404 Not Found' for url 'https://huggingface.co/api/models/x?sid=1'"
        )
    )
    text = describe_error(exc)
    assert "https://huggingface.co/…" in text
    assert "sid=1" not in text
    assert "api/models" not in text


def test_describe_error_keeps_earmark_text() -> None:
    exc = PermanentError("see https://example.org/docs/x")
    assert describe_error(exc) == "see https://example.org/docs/x"


def test_done_result_is_transcript_duration(tmp_path: Path) -> None:
    from earmark.fakes import FakeResolver
    from tests.factories import make_episode

    episode = make_episode(duration_s=None)
    fakes = make_fake_backends(tmp_path / "data")
    fakes.resolver = FakeResolver({CANON: episode})
    fakes.transcriber = FakeTranscriber(
        fakes.artifacts, segments=(Segment(start=0.0, end=42.0, text="x"),)
    )
    env = make_env(tmp_path, fakes=fakes)
    job = _enqueue_and_run(env)
    assert job.status == "done"
    assert job.result == JobResult(duration_s=42.0)


# -- request sources: uploads and direct URLs -------------------------------------------


AUDIO_URL = "https://audio.example.net/shows/clip.mp3"
OTHER_URL = "https://cdn.example.org/other/name.mp3"


def _stage(env: PipelineEnv, payload: bytes = b"ID3" + bytes(64), ext: str = "mp3") -> StagedUpload:
    with env.staging.begin_upload(ext) as writer:
        writer.write(payload)
        return writer.commit()


def _enqueue_upload(
    env: PipelineEnv, payload: bytes = b"ID3" + bytes(64), **kw: Any
) -> tuple[StagedUpload, Job]:
    staged = _stage(env, payload)
    new = new_upload_job(staged.sha256, upload_file=staged.name, **kw)
    return staged, env.jobs.enqueue(new).job


def _uploads(env: PipelineEnv) -> list[str]:
    return sorted(p.name for p in env.staging.uploads_dir.iterdir())


def _downloads(env: PipelineEnv) -> list[str]:
    if not env.staging.downloads_dir.exists():
        return []
    return sorted(p.name for p in env.staging.downloads_dir.iterdir())


def test_reshare_podcast_new_to_no_refetch_no_retranscribe(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.jobs.enqueue(new_podcast_job(CANON, to="a@example.org"))
    first = _run(env)
    env.jobs.enqueue(new_podcast_job(CANON, to="b@example.org"))
    second = _run(env)
    assert (first.status, second.status) == ("done", "done")
    assert first.id != second.id
    assert env.fakes.fetcher.computed == 1
    assert env.fakes.transcriber.computed == 1
    assert env.fakes.resolver.call_count == 2


def test_reshare_url_new_to_redownloads_not_retranscribed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.jobs.enqueue(new_url_job(AUDIO_URL, to="a@example.org"))
    first = _run(env)
    env.jobs.enqueue(new_url_job(AUDIO_URL, to="b@example.org"))
    second = _run(env)
    assert (first.status, second.status) == ("done", "done")
    assert first.episode_key == second.episode_key
    assert env.fakes.fetcher.downloads == 2
    assert env.fakes.transcriber.computed == 1
    assert env.fakes.fetcher.computed == 0


def test_direct_url_stage_order_and_fields(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    env.jobs.enqueue(new_url_job(AUDIO_URL, title="My clip"))
    job = _run(env)
    assert counting_store(env).stages == ["resolve", "fetch", "transcribe", "deliver"]
    assert job.status == "done"
    assert job.episode_key is not None
    episode = env.fakes.artifacts.read_episode(job.episode_key)
    assert episode is not None
    assert (episode.source, episode.title, episode.show_id) == ("url", "My clip", "audio")
    assert episode.key == content_key(hashlib.sha256(b"fake audio\n").hexdigest())
    assert env.fakes.artifacts.read_transcript(job.episode_key) is not None
    assert env.fakes.resolver.call_count == 0


def test_cached_url_transcript_skips_transcribe_stage(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    env.jobs.enqueue(new_url_job(AUDIO_URL, to="a@example.org"))
    _run(env)
    store = counting_store(env)
    store.stages.clear()
    env.jobs.enqueue(new_url_job(AUDIO_URL, to="b@example.org"))
    _run(env)
    assert store.stages == ["resolve", "fetch", "deliver"]


def test_two_urls_same_bytes_share_transcript(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    fakes.fetcher = FakeAudioFetcher(
        fakes.artifacts, payloads={AUDIO_URL: b"same bytes", OTHER_URL: b"same bytes"}
    )
    env = make_env(tmp_path, fakes=fakes)
    env.jobs.enqueue(new_url_job(AUDIO_URL))
    first = _run(env)
    env.jobs.enqueue(new_url_job(OTHER_URL))
    second = _run(env)
    assert first.episode_key == second.episode_key is not None
    assert fakes.transcriber.computed == 1
    assert fakes.fetcher.downloads == 2


def test_two_urls_different_bytes_differ(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    fakes.fetcher = FakeAudioFetcher(
        fakes.artifacts, payloads={AUDIO_URL: b"one", OTHER_URL: b"two"}
    )
    env = make_env(tmp_path, fakes=fakes)
    env.jobs.enqueue(new_url_job(AUDIO_URL))
    first = _run(env)
    env.jobs.enqueue(new_url_job(OTHER_URL))
    second = _run(env)
    assert first.episode_key != second.episode_key
    assert fakes.transcriber.computed == 2


def test_upload_episode_key_is_file_key(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    staged, _ = _enqueue_upload(env)
    expected = file_key(staged.path)
    job = _run(env)
    assert job.status == "done"
    assert job.episode_key == expected == content_key(staged.sha256)
    assert env.fakes.transcriber.calls[0].audio == staged.path


def test_resolver_not_called_for_upload_or_url(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _enqueue_upload(env)
    env.jobs.enqueue(new_url_job(AUDIO_URL))
    assert _run(env).status == "done"
    assert _run(env).status == "done"
    assert env.fakes.resolver.call_count == 0
    assert env.fakes.fetcher.call_count == 1  # the one download; fetch() was never needed
    assert env.fakes.fetcher.downloads == 1


def test_upload_transcribes_without_fetching(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    _enqueue_upload(env, title="Voice memo")
    job = _run(env)
    assert counting_store(env).stages == ["resolve", "transcribe", "deliver"]
    assert job.status == "done"
    assert env.fakes.fetcher.call_count == 0
    assert env.fakes.resolver.call_count == 0


def test_tiny_transcript_not_reused_for_large(tmp_path: Path) -> None:
    tiny = make_fake_backends(tmp_path / "data")
    tiny.transcriber = FakeTranscriber(tiny.artifacts, model="tiny")
    env_tiny = make_env(tmp_path, fakes=tiny)
    env_tiny.jobs.enqueue(new_podcast_job(CANON, to="a@example.org"))
    first = _run(env_tiny)
    assert first.episode_key is not None
    assert env_tiny.fakes.artifacts.delete_audio(first.episode_key)
    large = make_fake_backends(tmp_path / "data")
    large.transcriber = FakeTranscriber(large.artifacts, model="large-v3")
    env_large = make_env(tmp_path, fakes=large)
    env_large.jobs.enqueue(new_podcast_job(CANON, to="b@example.org"))
    second = _run(env_large)
    assert (first.status, second.status) == ("done", "done")
    assert large.transcriber.computed == 1
    assert large.fetcher.computed == 1  # the audio was gone, so it is fetched again
    stored = large.artifacts.read_transcript(first.episode_key or "")
    assert stored is not None
    assert (stored.model, stored.asr_profile) == ("large-v3", large.transcriber.profile)


def test_done_upload_audio_deleted(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _, job = _enqueue_upload(env)
    assert len(_uploads(env)) == 1
    assert _run(env).status == "done"
    assert _uploads(env) == []
    assert _stored(env, job).upload_file is not None  # the row keeps its name; the file is gone


def test_failed_upload_audio_deleted(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(UnusableAudioError("could not decode"))
    _enqueue_upload(env)
    job = _run(env)
    assert (job.status, job.attempts) == ("failed", 1)
    assert _uploads(env) == []
    [call] = env.fakes.notifier.calls
    assert call.job.status == "failed"


def test_transient_exhausted_upload_audio_deleted(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    env.fakes.transcriber.fail_next(TransientError("asr busy"), times=2)
    _enqueue_upload(env)
    assert _run(env).status == "queued"
    env.clock.advance(60)
    assert _run(env).status == "failed"
    assert _uploads(env) == []


def test_transient_requeue_keeps_upload_audio(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(TransientError("asr busy"))
    staged, _ = _enqueue_upload(env)
    assert _run(env).status == "queued"
    assert _uploads(env) == [staged.name]
    env.clock.advance(60)
    assert _run(env).status == "done"
    assert _uploads(env) == []


def test_skipped_upload_audio_deleted(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(SkipEpisode("nothing to do"))
    _enqueue_upload(env)
    assert _run(env).status == "skipped"
    assert _uploads(env) == []


def test_url_download_deleted_on_done_and_failed(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    fakes.fetcher = FakeAudioFetcher(
        fakes.artifacts, payloads={AUDIO_URL: b"one", OTHER_URL: b"two"}
    )
    env = make_env(tmp_path, fakes=fakes)
    env.jobs.enqueue(new_url_job(AUDIO_URL))
    assert _run(env).status == "done"
    assert _downloads(env) == []
    env.fakes.transcriber.fail_next(PermanentError("bad audio"))
    env.jobs.enqueue(new_url_job(OTHER_URL))
    assert _run(env).status == "failed"
    assert _downloads(env) == []


def test_url_download_kept_while_retrying(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(TransientError("asr busy"))
    env.jobs.enqueue(new_url_job(AUDIO_URL))
    job = _run(env)
    assert job.status == "queued"
    assert len(_downloads(env)) == 1
    env.clock.advance(60)
    assert _run(env).status == "done"
    assert _downloads(env) == []


def test_missing_upload_is_permanent(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    staged, _ = _enqueue_upload(env)
    staged.path.unlink()
    job = _run(env)
    assert job.status == "failed"
    assert job.error == "the uploaded audio is missing; upload it again"


def test_changed_upload_is_permanent(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    staged, _ = _enqueue_upload(env)
    staged.path.write_bytes(b"tampered")
    job = _run(env)
    assert job.status == "failed"
    assert job.error == "the uploaded audio changed after it was received"
    assert env.fakes.transcriber.call_count == 0
    assert _uploads(env) == []


def test_direct_url_download_failure_is_reported(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.fetcher.fail_next(PermanentError("audio URL on audio.example.net returned 404"))
    env.jobs.enqueue(new_url_job(AUDIO_URL))
    job = _run(env)
    assert job.status == "failed"
    assert job.stage == "fetch"
    [call] = env.fakes.notifier.calls
    assert call.episode is None


def test_load_episode_per_source(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    staged, upload_job = _enqueue_upload(env, upload_name="memo.m4a")
    episode = env.pipeline.load_episode(upload_job)
    assert episode is not None
    assert (episode.key, episode.title) == (content_key(staged.sha256), "memo")
    env.jobs.enqueue(new_url_job(AUDIO_URL, title="Named"))
    _run(env)  # the upload
    url_job = _run(env)
    assert url_job.source == "url"
    loaded = env.pipeline.load_episode(url_job)
    assert loaded is not None
    assert loaded.title == "Named"
    unresolved = url_job.model_copy(update={"episode_key": None})
    assert env.pipeline.load_episode(unresolved) is None


# -- YouTube ------------------------------------------------------------------------------


def test_youtube_job_runs(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    env.jobs.enqueue(new_youtube_job(instructions="Summarise."))
    job = _run(env)
    assert job.status == "done"
    assert counting_store(env).stages == ["resolve", "fetch", "transcribe", "deliver"]
    assert job.episode_key == youtube_key(YOUTUBE_ID)
    assert env.fakes.youtube.call_count == 2  # resolve, then download
    assert env.fakes.youtube.downloads == 1
    assert env.fakes.resolver.call_count == 0
    assert env.fakes.fetcher.call_count == 0
    episode = env.fakes.artifacts.read_episode(youtube_key(YOUTUBE_ID))
    assert episode is not None
    assert (episode.source, episode.guid) == ("youtube", f"youtube:{YOUTUBE_ID}")
    assert env.fakes.transcriber.computed == 1
    (mail,) = env.fakes.mailer.calls
    assert b'type: "video"' in _decoded_note(mail.data)
    assert b'origin: "youtube"' in _decoded_note(mail.data)
    assert _downloads(env) == []


def _decoded_note(data: bytes) -> bytes:
    import email
    import email.policy

    message = email.message_from_bytes(data, policy=email.policy.default)
    parts = [
        p
        for p in message.iter_attachments()
        if not (p.get_filename() or "").endswith(".instructions.md")
    ]
    payload = parts[0].get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload


def test_youtube_cache_reused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.jobs.enqueue(new_youtube_job(instructions="one"))
    first = _run(env)
    env.jobs.enqueue(new_youtube_job(instructions="two"))
    second = _run(env)
    assert (first.status, second.status) == ("done", "done")
    assert first.episode_key == second.episode_key
    assert env.fakes.youtube.downloads == 1
    assert env.fakes.transcriber.computed == 1
    assert len(env.fakes.mailer.calls) == 2


def test_youtube_cached_skips_download(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    env.jobs.enqueue(new_youtube_job(instructions="one"))
    _run(env)
    counting_store(env).stages.clear()
    env.jobs.enqueue(new_youtube_job(instructions="two"))
    _run(env)
    assert counting_store(env).stages == ["resolve", "deliver"]
    assert env.fakes.youtube.downloads == 1


def test_youtube_episode_mismatch(tmp_path: Path) -> None:
    other = youtube_episode(
        "ex_ample-02",
        title="Wrong",
        channel="",
        published_at=None,
        published_date_only=False,
        duration_s=None,
    )
    fakes = make_fake_backends(tmp_path / "data")
    fakes.youtube = FakeYoutubeSource(episodes={YOUTUBE_ID: other})
    env = make_env(tmp_path, fakes=fakes)
    env.jobs.enqueue(new_youtube_job())
    job = _run(env)
    assert job.status == "failed"
    assert job.error == "the YouTube source returned another video"
    assert fakes.youtube.downloads == 0
    assert not fakes.mailer.calls


def test_youtube_refusal_fails_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.youtube.fail_next(PermanentError("This YouTube video is private."))
    env.jobs.enqueue(new_youtube_job())
    job = _run(env)
    assert (job.status, job.error) == ("failed", "This YouTube video is private.")
    assert len(env.fakes.notifier.calls) == 1
    assert env.fakes.notifier.calls[0].job.status == "failed"
    assert _downloads(env) == []
    assert not env.fakes.mailer.calls


def test_youtube_download_kept_while_retrying(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.fakes.transcriber.fail_next(TransientError("asr busy"))
    env.jobs.enqueue(new_youtube_job())
    assert _run(env).status == "queued"
    assert len(_downloads(env)) == 1
    env.clock.advance(60)
    assert _run(env).status == "done"
    assert _downloads(env) == []
    assert env.fakes.youtube.downloads == 2  # the retry downloads again


def test_load_episode_for_youtube(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.jobs.enqueue(new_youtube_job())
    job = _run(env)
    loaded = env.pipeline.load_episode(job)
    assert loaded is not None
    assert loaded.key == youtube_key(YOUTUBE_ID)
