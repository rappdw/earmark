"""The claim/run loop: heartbeat while a job runs, release it on SIGTERM or SIGINT."""

import logging
import signal
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from types import FrameType
from typing import Final, Self

from earmark.jobs import Claim, JobStore, LeaseLostError
from earmark.models import Job
from earmark.pipeline.runner import Pipeline

logger = logging.getLogger(__name__)

HEARTBEAT_MAX_S: Final = 60.0
_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)


class Shutdown(BaseException):
    """Raised in the main thread by the signal handler while a stage runs."""


def heartbeat_interval(lease_seconds: float) -> float:
    return min(HEARTBEAT_MAX_S, lease_seconds / 4)


class Heartbeat:
    """Refreshes the job's heartbeat from a daemon thread while the block runs."""

    def __init__(self, jobs: JobStore, claim: Claim, interval_s: float) -> None:
        if not interval_s > 0:
            raise ValueError("interval_s must be > 0")
        self._jobs = jobs
        self._claim = claim
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._beats = 0
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{claim.job.id[:8]}", daemon=True
        )

    @property
    def beats(self) -> int:
        return self._beats

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._jobs.heartbeat(self._claim)
            except LeaseLostError:
                logger.debug("heartbeat: job %s is no longer ours", self._claim.job.id)
                return
            except Exception:
                logger.warning("heartbeat failed for job %s", self._claim.job.id, exc_info=True)
            else:
                self._beats += 1


class Worker:
    def __init__(
        self,
        jobs: JobStore,
        pipeline: Pipeline,
        *,
        poll_seconds: float,
        heartbeat_seconds: float,
    ) -> None:
        self._jobs = jobs
        self._pipeline = pipeline
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._interruptible = False

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    # -- signals ----------------------------------------------------------------------

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        self._stop.set()
        if self._interruptible:
            self._interruptible = False
            raise Shutdown

    @contextmanager
    def _handling_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            logger.debug("not the main thread; signal handlers left alone")
            yield
            return
        previous = {sig: signal.getsignal(sig) for sig in _SIGNALS}
        for sig in _SIGNALS:
            signal.signal(sig, self._on_signal)
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)

    def handle_signals(self) -> AbstractContextManager[None]:
        return self._handling_signals()

    @contextmanager
    def _window(self) -> Iterator[None]:
        if self._stop.is_set():
            raise Shutdown
        self._interruptible = True
        try:
            yield
        finally:
            self._interruptible = False

    # -- loop -------------------------------------------------------------------------

    def run_once(self) -> Job | None:
        if self.stopping:
            return None
        for stale in self._jobs.requeue_stale():
            logger.warning("job %s: %s", stale.id, stale.error)
            if stale.status == "failed":
                self._pipeline.notify(stale, self._pipeline.load_episode(stale))
        claim = self._jobs.claim_next()
        if claim is None:
            return None
        job_id = claim.job.id
        logger.info("job %s claimed (attempt %d)", job_id, claim.job.attempts)
        started = time.monotonic()
        try:
            with Heartbeat(self._jobs, claim, self._heartbeat_seconds):
                ran = self._pipeline.run(claim, interruptible=self._window)
        except Shutdown:
            return self._release(claim)
        final = ran if ran is not None else self._jobs.get(job_id)
        if final is not None:
            logger.info("job %s %s in %.1f s", job_id, final.status, time.monotonic() - started)
        return final

    def _release(self, claim: Claim) -> Job | None:
        released: Job | None
        try:
            released = self._jobs.release(claim)
        except LeaseLostError:
            released = self._jobs.get(claim.job.id)
        logger.info("job %s released back to the queue on shutdown", claim.job.id)
        return released

    def run_forever(self) -> None:
        with self.handle_signals():
            logger.info("worker started")
            while not self.stopping:
                job: Job | None = None
                try:
                    job = self.run_once()
                except sqlite3.Error:
                    logger.exception("job store error; will retry")
                if job is None:
                    self._stop.wait(self._poll_seconds)
            logger.info("worker stopped")
