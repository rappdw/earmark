"""A wired-up clock, job store, fakes, pipeline, and worker for the runner and worker tests."""

import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from earmark.db import open_database
from earmark.fakes import FakeBackends, make_fake_backends
from earmark.jobs import Claim, JobStore
from earmark.models import Episode, Stage, Transcript
from earmark.pipeline.runner import Pipeline
from earmark.pipeline.stages import Backends, Transcriber
from earmark.pipeline.worker import Worker
from tests.factories import MutableClock

SECRET = "sentinel-secret-3f9a"
CANON = "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981"


@dataclass
class PipelineEnv:
    clock: MutableClock
    jobs: JobStore
    fakes: FakeBackends
    pipeline: Pipeline
    worker: Worker


def make_env(
    tmp_path: Path,
    *,
    max_attempts: int = 4,
    lease_s: float = 900.0,
    heartbeat_s: float = 60.0,
    backends: Backends | None = None,
    store_type: type[JobStore] = JobStore,
    fakes: FakeBackends | None = None,
) -> PipelineEnv:
    clock = MutableClock()
    db = open_database(tmp_path / "data" / "earmark.db")
    jobs = store_type(db, clock=clock, max_attempts=max_attempts, lease=timedelta(seconds=lease_s))
    fakes = fakes or make_fake_backends(tmp_path / "data")
    pipeline = Pipeline(backends or fakes.backends(), fakes.artifacts, jobs, secrets=(SECRET,))
    worker = Worker(jobs, pipeline, poll_seconds=0.01, heartbeat_seconds=heartbeat_s)
    return PipelineEnv(clock, jobs, fakes, pipeline, worker)


class BlockingTranscriber:
    """Runs ``callback`` before delegating, so a test can act in the middle of a stage."""

    def __init__(self, inner: Transcriber, callback: Callable[[], None]) -> None:
        self.inner = inner
        self.callback = callback

    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript:
        self.callback()
        return self.inner.transcribe(episode, audio)


def signal_and_sleep(signum: int = signal.SIGTERM) -> Callable[[], None]:
    """A callback that signals this process, then sleeps long enough for it to be noticed."""

    def hook() -> None:
        os.kill(os.getpid(), signum)
        time.sleep(5)

    return hook


class CountingJobStore(JobStore):
    """Counts heartbeat attempts, optionally failing them, and records ``set_stage`` calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.attempts = 0
        self.beats = 0
        self.heartbeat_error: Exception | None = None
        self.stages: list[str] = []
        self._cond = threading.Condition()

    def heartbeat(self, claim: Claim) -> None:
        try:
            if self.heartbeat_error is not None:
                raise self.heartbeat_error
            super().heartbeat(claim)
            with self._cond:
                self.beats += 1
        finally:
            with self._cond:
                self.attempts += 1
                self._cond.notify_all()

    def set_stage(self, claim: Claim, stage: Stage) -> None:
        self.stages.append(stage)
        super().set_stage(claim, stage)

    def wait_for_beats(self, count: int, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self.beats >= count, timeout)

    def wait_for_attempts(self, count: int, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self.attempts >= count, timeout)


def counting_store(env: PipelineEnv) -> CountingJobStore:
    assert isinstance(env.jobs, CountingJobStore)
    return env.jobs


def with_transcriber(env_fakes: FakeBackends, transcriber: Transcriber) -> Backends:
    return replace(env_fakes.backends(), transcriber=transcriber)


def make_blocking_env(
    tmp_path: Path, make_callback: Callable[[PipelineEnv], Callable[[], None]], **kwargs: Any
) -> PipelineEnv:
    """An env whose transcriber runs ``make_callback(env)()`` before transcribing."""
    holder: list[PipelineEnv] = []
    fakes = make_fake_backends(tmp_path / "data")
    transcriber = BlockingTranscriber(fakes.transcriber, lambda: make_callback(holder[0])())
    env = make_env(
        tmp_path, fakes=fakes, backends=replace(fakes.backends(), transcriber=transcriber), **kwargs
    )
    holder.append(env)
    return env
