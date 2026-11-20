"""Run one claimed job through the stages and map the outcome onto the job store."""

import logging
import re
import traceback
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from earmark.artifacts import ArtifactStore
from earmark.errors import EarmarkError, PermanentError, SkipEpisode, TransientError
from earmark.jobs import Claim, JobStore, LeaseLostError
from earmark.log import redact
from earmark.models import (
    Analysis,
    Episode,
    Job,
    JobResult,
    StoreResult,
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
        message = str(exc)
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
                analysis, stored = self._stage_work(claim, state)
            done = self._jobs.complete(
                claim,
                JobResult(
                    note_path=stored.note_path,
                    transcript_note_path=stored.transcript_note_path,
                    analysis_hash=analysis.prompt_hash,
                ),
            )
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

    def _stage_work(self, claim: Claim, state: _RunState) -> tuple[Analysis, StoreResult]:
        if claim.job.kind == "reanalyze":
            episode, transcript, shared_at = self._stored_inputs(claim, state)
        else:
            episode, transcript, shared_at = self._ingest_inputs(claim, state)
        return self._analyze_store_index(claim, episode, transcript, shared_at)

    def _ingest_inputs(
        self, claim: Claim, state: _RunState
    ) -> tuple[Episode, Transcript, datetime]:
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
        shared_at = self._artifacts.record_share(episode.key, job.shared_at)

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
        jobs.set_stage(claim, "analyze")
        return episode, transcript, shared_at

    def _stored_inputs(
        self, claim: Claim, state: _RunState
    ) -> tuple[Episode, Transcript, datetime]:
        job = claim.job
        key = job.episode_key
        if key is None:
            raise PermanentError("reanalyze job without an episode key")
        self._jobs.set_stage(claim, "analyze")
        episode = self._artifacts.read_episode(key)
        if episode is None:
            raise PermanentError(f"episode {key} is not stored; share it again")
        state.episode = episode
        transcript = self._artifacts.read_transcript(key)
        if transcript is None:
            raise PermanentError(
                f"episode {key} has no transcript; share it again to transcribe it"
            )
        shared_at = self._artifacts.record_share(key, job.shared_at)
        return episode, transcript, shared_at

    def _analyze_store_index(
        self, claim: Claim, episode: Episode, transcript: Transcript, shared_at: datetime
    ) -> tuple[Analysis, StoreResult]:
        job = claim.job
        jobs = self._jobs
        b = self._b
        self._warn(job.id, transcript.warnings)

        analysis = b.analyzer.analyze(episode, transcript, job.prompt)
        if analysis.episode_key != episode.key:
            raise PermanentError("analyzer returned an analysis for another episode")
        self._warn(job.id, analysis.warnings)

        analyses = self._artifacts.list_analyses(episode.key)
        if not any(a.prompt_hash == analysis.prompt_hash for a in analyses):
            analyses.append(analysis)
            analyses.sort(key=lambda a: (a.created_at, a.prompt_hash))

        jobs.set_stage(claim, "store")
        stored = b.store.store(episode, transcript, tuple(analyses), shared_at)
        self._warn(job.id, stored.warnings)

        if b.indexer is not None:
            jobs.set_stage(claim, "index")
            try:
                b.indexer.index(episode, transcript, tuple(analyses), stored)
            except Exception as exc:
                logger.warning("indexing failed for job %s", job.id, exc_info=exc)
                jobs.add_warning(job.id, f"index failed: {describe_error(exc, self._secrets)}")
        return analysis, stored
