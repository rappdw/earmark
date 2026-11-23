"""Run one claimed job through the stages and map the outcome onto the job store."""

import logging
import re
import traceback
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from earmark.artifacts import ArtifactStore
from earmark.errors import EarmarkError, PermanentError, SkipEpisode, TransientError
from earmark.jobs import Claim, JobStore, LeaseLostError
from earmark.log import redact, scrub_urls
from earmark.models import (
    Episode,
    Job,
    JobResult,
    Transcript,
    scrub_surrogates,
)
from earmark.pipeline.stages import Backends

logger = logging.getLogger(__name__)


@dataclass
class _RunState:
    episode: Episode | None = None


MAX_ERROR_CHARS: Final = 500
_WHITESPACE: Final = re.compile(r"\s+")


def describe_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """One short line for ``job.error`` and warnings: collapsed, redacted, capped."""
    if isinstance(exc, EarmarkError):
        text = exc.message
    else:
        text = f"unexpected error: {type(exc).__name__}"
        message = scrub_urls(str(exc))
        if message:
            text += f": {message}"
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            last = frames[-1]
            text += f" (at {Path(last.filename).name}:{last.lineno} in {last.name})"
    text = _WHITESPACE.sub(" ", scrub_surrogates(text)).strip()
    text = redact(text, secrets)
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 1] + "…"
    return text or type(exc).__name__


class Pipeline:
    def __init__(
        self,
        backends: Backends,
        artifacts: ArtifactStore,
        jobs: JobStore,
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        self._b = backends
        self._artifacts = artifacts
        self._jobs = jobs
        self._secrets = tuple(secrets)

    # -- public -----------------------------------------------------------------------

    def run(
        self,
        claim: Claim,
        *,
        interruptible: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> Job | None:
        """Run ``claim`` to an outcome and return the job as stored; None if the lease was lost."""
        job = claim.job
        state = _RunState()
        try:
            with interruptible():
                transcript = self._stage_work(claim, state)
            done = self._jobs.complete(claim, JobResult(duration_s=transcript.duration_s))
        except LeaseLostError:
            logger.warning("lost the lease on job %s; abandoning it", job.id)
            return None
        except SkipEpisode as exc:
            logger.info("job %s skipped: %s", job.id, exc.message)
            return self._finish(claim, state.episode, exc, skip=True)
        except PermanentError as exc:
            logger.warning("job %s failed permanently: %s", job.id, exc.message)
            return self._finish(claim, state.episode, exc, transient=False)
        except TransientError as exc:
            logger.warning("job %s failed transiently: %s", job.id, exc.message)
            return self._finish(
                claim, state.episode, exc, transient=True, retry_after_s=exc.retry_after_s
            )
        except Exception as exc:
            logger.error("job %s raised an unexpected error", job.id, exc_info=exc)
            return self._finish(claim, state.episode, exc, transient=True)
        self.notify(done, state.episode)
        return done

    def notify(self, job: Job, episode: Episode | None) -> None:
        notifier = self._b.notifier
        if notifier is None:
            return
        try:
            notifier.notify(job, episode)
        except Exception as exc:
            logger.warning("notifier failed for job %s", job.id, exc_info=exc)
            try:
                self._jobs.add_warning(
                    job.id, f"notify failed: {describe_error(exc, self._secrets)}"
                )
            except Exception:
                logger.warning(
                    "could not record the notify failure on job %s", job.id, exc_info=True
                )

    def load_episode(self, job: Job) -> Episode | None:
        if job.episode_key is None:
            return None
        try:
            return self._artifacts.read_episode(job.episode_key)
        except (PermanentError, OSError):
            return None

    # -- outcomes ---------------------------------------------------------------------

    def _finish(
        self,
        claim: Claim,
        episode: Episode | None,
        exc: Exception,
        *,
        skip: bool = False,
        transient: bool = False,
        retry_after_s: float | None = None,
    ) -> Job | None:
        """Write a failure outcome, then notify if the job is terminal."""
        text = describe_error(exc, self._secrets)
        try:
            if skip:
                job = self._jobs.skip(claim, text)
            else:
                job = self._jobs.fail(claim, text, transient=transient, retry_after_s=retry_after_s)
        except LeaseLostError:
            logger.warning("lost the lease on job %s; abandoning it", claim.job.id)
            return None
        if job.status != "queued":
            self.notify(job, episode)
        return job

    # -- stages -----------------------------------------------------------------------

    def _warn(self, job_id: str, warnings: Iterable[str]) -> None:
        for warning in warnings:
            self._jobs.add_warning(job_id, warning)

    def _stage_work(self, claim: Claim, state: _RunState) -> Transcript:
        job = claim.job
        jobs = self._jobs
        b = self._b
        if job.url is None:
            raise PermanentError("ingest job without a url")

        jobs.set_stage(claim, "resolve")
        episode = b.resolver.resolve(job.url)
        state.episode = episode
        self._artifacts.write_episode(episode)
        jobs.set_episode_key(claim, episode.key)
        self._artifacts.record_share(episode.key, job.shared_at)

        transcript = self._artifacts.read_transcript(episode.key)
        if transcript is None:
            jobs.set_stage(claim, "fetch")
            audio = b.fetcher.fetch(episode)
            jobs.set_stage(claim, "transcribe")
            transcript = b.transcriber.transcribe(episode, audio)
        else:
            logger.info("job %s: transcript for %s is cached", job.id, episode.key)
        if transcript.episode_key != episode.key:
            raise PermanentError("transcriber returned a transcript for another episode")
        self._warn(job.id, transcript.warnings)
        return transcript
