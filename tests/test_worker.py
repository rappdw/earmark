import logging
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from earmark.fakes import FakeNotifier, make_fake_backends
from earmark.models import Episode, Job
from earmark.pipeline.runner import Pipeline
from earmark.pipeline.worker import Heartbeat, Worker, heartbeat_interval
from tests.factories import make_episode, new_podcast_job, new_upload_job, new_url_job
from tests.pipeline_env import (
    CANON,
    CountingJobStore,
    PipelineEnv,
    counting_store,
    make_blocking_env,
    make_env,
    signal_and_sleep,
)


def _stored(env: PipelineEnv, job: Job) -> Job:
    found = env.jobs.get(job.id)
    assert found is not None
    return found


def test_heartbeat_interval() -> None:
    assert heartbeat_interval(900) == 60.0
    assert heartbeat_interval(30) == 7.5


# -- A8: stale jobs ----------------------------------------------------------------------


def test_stale_failed_job_is_notified(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=1, lease_s=10)
    enqueued = env.jobs.enqueue(new_podcast_job(CANON))
    claim = env.jobs.claim_next()  # a worker that then crashed
    assert claim is not None
    episode = make_episode()
    env.fakes.artifacts.write_episode(episode)
    env.jobs.set_episode_key(claim, episode.key)
    env.clock.advance(11)
    assert env.worker.run_once() is None
    job = _stored(env, enqueued.job)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith("worker stopped responding")
    [call] = env.fakes.notifier.calls
    assert call.job == job
    assert call.episode == episode


def test_stale_failed_upload_audio_deleted(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=1, lease_s=10)
    with env.staging.begin_upload("mp3") as writer:
        writer.write(b"ID3" + bytes(32))
        staged = writer.commit()
    enqueued = env.jobs.enqueue(new_upload_job(staged.sha256, upload_file=staged.name))
    assert env.jobs.claim_next() is not None  # a worker that then crashed
    assert staged.path.exists()
    env.clock.advance(11)
    assert env.worker.run_once() is None
    job = _stored(env, enqueued.job)
    assert job.status == "failed"
    assert not staged.path.exists()
    [call] = env.fakes.notifier.calls
    assert call.job == job
    assert call.episode is not None
    assert call.episode.source == "upload"


def test_stale_failed_url_job_clears_partial_download(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=1, lease_s=10)
    enqueued = env.jobs.enqueue(new_url_job("https://audio.example.net/a.mp3"))
    claim = env.jobs.claim_next()
    assert claim is not None
    partial = env.staging.download_destination(claim.job.id)("mp3")
    partial.write_bytes(b"half")
    env.clock.advance(11)
    assert env.worker.run_once() is None
    assert _stored(env, enqueued.job).status == "failed"
    assert not partial.exists()


def test_stale_requeued_job_is_run_not_notified(tmp_path: Path) -> None:
    env = make_env(tmp_path, lease_s=10)
    env.jobs.enqueue(new_podcast_job(CANON))
    assert env.jobs.claim_next() is not None
    env.clock.advance(11)
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert env.fakes.notifier.call_count == 1  # for the successful run only


# -- A9: heartbeat -----------------------------------------------------------------------


def test_heartbeat_keeps_long_stage_alive(tmp_path: Path) -> None:
    stale: list[list[Job]] = []

    def make(env: PipelineEnv) -> Callable[[], None]:
        def callback() -> None:
            store = counting_store(env)
            env.clock.advance(31)
            start = store.beats
            assert store.wait_for_beats(start + 2)
            stale.append(env.jobs.requeue_stale())

        return callback

    env = make_blocking_env(
        tmp_path, make, store_type=CountingJobStore, lease_s=30, heartbeat_s=0.01
    )
    env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert stale == [[]]
    assert counting_store(env).beats >= 2


def test_without_heartbeat_long_stage_is_requeued(tmp_path: Path) -> None:
    stale: list[list[Job]] = []

    def make(env: PipelineEnv) -> Callable[[], None]:
        def callback() -> None:
            env.clock.advance(31)
            stale.append(env.jobs.requeue_stale())

        return callback

    env = make_blocking_env(tmp_path, make, lease_s=30, heartbeat_s=100)
    enqueued = env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    assert [j.id for j in stale[0]] == [enqueued.job.id]
    assert job.status == "queued"
    assert job.error is not None
    assert job.error.startswith("worker stopped responding")
    assert job.result is None
    assert env.fakes.notifier.call_count == 0


def test_heartbeat_thread_survives_store_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def make(env: PipelineEnv) -> Callable[[], None]:
        def callback() -> None:
            assert counting_store(env).wait_for_attempts(2)

        return callback

    env = make_blocking_env(tmp_path, make, store_type=CountingJobStore, heartbeat_s=0.01)
    counting_store(env).heartbeat_error = sqlite3.OperationalError("disk I/O error")
    env.jobs.enqueue(new_podcast_job(CANON))
    with caplog.at_level(logging.WARNING):
        job = env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert any(
        r.levelno == logging.WARNING and "heartbeat failed" in r.getMessage()
        for r in caplog.records
    )


def _heartbeat_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("heartbeat-")]


def test_heartbeat_unit(tmp_path: Path) -> None:
    env = make_env(tmp_path, store_type=CountingJobStore)
    store = counting_store(env)
    store.enqueue(new_podcast_job(CANON))
    claim = store.claim_next()
    assert claim is not None
    with pytest.raises(ValueError, match="interval_s"):
        Heartbeat(store, claim, 0)
    with Heartbeat(store, claim, 0.01) as hb:
        assert store.wait_for_beats(3, timeout=1.0)
        assert hb.beats >= 3
    after_exit = store.attempts
    time.sleep(0.05)
    assert store.attempts == after_exit
    assert _heartbeat_threads() == []

    with Heartbeat(store, claim, 0.01):
        store.release(claim)
        deadline = time.monotonic() + 1.0
        while _heartbeat_threads() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _heartbeat_threads() == []  # ended by itself, quietly


# -- A10: signals ------------------------------------------------------------------------


def _fresh_worker(env: PipelineEnv) -> tuple[Worker, Pipeline]:
    pipeline = Pipeline(env.fakes.backends(), env.fakes.artifacts, env.jobs, staging=env.staging)
    return Worker(env.jobs, pipeline, poll_seconds=0.01, heartbeat_seconds=60), pipeline


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_during_stage_releases(tmp_path: Path, signum: signal.Signals) -> None:
    env = make_blocking_env(tmp_path, lambda _env: signal_and_sleep(signum))
    enqueued = env.jobs.enqueue(new_podcast_job(CANON))
    before = signal.getsignal(signum)
    started = time.monotonic()
    with env.worker.handle_signals():
        job = env.worker.run_once()
    assert time.monotonic() - started < 2
    assert signal.getsignal(signum) is before
    assert job is not None
    assert (job.status, job.attempts, job.stage) == ("queued", 0, "transcribe")
    assert job.run_after == env.clock.now
    assert job.id == enqueued.job.id
    assert job.episode_key is not None
    assert not env.fakes.artifacts.has_transcript(job.episode_key)
    assert env.fakes.notifier.call_count == 0
    assert env.worker.stopping

    worker, _ = _fresh_worker(env)
    resumed = worker.run_once()
    assert resumed is not None
    assert resumed.status == "done"
    assert env.fakes.fetcher.computed == 1  # the audio cache was reused


class _SignalingNotifier:
    def __init__(self, inner: FakeNotifier) -> None:
        self.inner = inner
        self.completed: list[Job] = []

    def notify(self, job: Job, episode: Episode | None, /) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        self.inner.notify(job, episode)
        self.completed.append(job)


def test_sigterm_during_notify_finishes(tmp_path: Path) -> None:
    fakes = make_fake_backends(tmp_path / "data")
    notifier = _SignalingNotifier(fakes.notifier)
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), notifier=notifier))
    first = env.jobs.enqueue(new_podcast_job(CANON, instructions="one")).job
    second = env.jobs.enqueue(new_podcast_job(CANON, instructions="two")).job
    before = signal.getsignal(signal.SIGTERM)
    env.worker.run_forever()
    assert signal.getsignal(signal.SIGTERM) is before
    done = _stored(env, first)
    assert done.status == "done"
    assert [j.id for j in notifier.completed] == [first.id]
    assert fakes.notifier.call_count == 1
    left = _stored(env, second)
    assert (left.status, left.attempts) == ("queued", 0)


def test_sigterm_while_idle_exits(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    worker = Worker(env.jobs, env.pipeline, poll_seconds=10, heartbeat_seconds=60)
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))
    started = time.monotonic()
    try:
        timer.start()
        worker.run_forever()
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 2
    assert signal.getsignal(signal.SIGTERM) is before
    assert worker.stopping


def test_stop_requested_before_claim(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    enqueued = env.jobs.enqueue(new_podcast_job(CANON))
    env.worker.request_stop()
    assert env.worker.run_once() is None
    job = _stored(env, enqueued.job)
    assert (job.status, job.attempts) == ("queued", 0)


def test_handle_signals_off_main_thread(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    errors: list[Exception] = []

    def target() -> None:
        try:
            with env.worker.handle_signals():
                inside = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
                assert inside == before
        except (AssertionError, OSError, ValueError, RuntimeError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    assert errors == []
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def test_run_forever_processes_then_stops(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    enqueued = env.jobs.enqueue(new_podcast_job(CANON))
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        env.worker.run_forever()
    finally:
        timer.cancel()
        timer.join()
    assert _stored(env, enqueued.job).status == "done"
