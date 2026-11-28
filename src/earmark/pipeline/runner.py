"""Run one claimed job through the stages and map the outcome onto the job store."""

import logging
import re
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from earmark.artifacts import ArtifactStore
from earmark.errors import (
    EarmarkError,
    MaybeDelivered,
    PermanentError,
    SkipEpisode,
    TransientError,
)
from earmark.jobs import Claim, JobStore, LeaseLostError
from earmark.log import redact, scrub_urls
from earmark.mail.message import DEFAULT_MAX_ATTACHMENT_BYTES, compose_mail
from earmark.models import (
    Clock,
    Episode,
    Job,
    JobResult,
    SummaryInput,
    SummaryRecord,
    Transcript,
    scrub_surrogates,
    utc_now,
    youtube_key,
)
from earmark.pipeline.stages import Backends, OutgoingMail
from earmark.recipients import RecipientPolicy, mask_address
from earmark.sources import audio_episode, sha256_file
from earmark.staging import Staging
from earmark.summarize.base import (
    INPUT_CHANGED,
    INPUT_MISSING,
    INPUT_UNREADABLE,
    NOT_CONFIGURED,
    check_result,
)
from earmark.transcripts import episode_for_job
from earmark.youtube.links import video_id_from_canonical

logger = logging.getLogger(__name__)


@dataclass
class _RunState:
    episode: Episode | None = None
    first_shared_at: datetime | None = None


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
        staging: Staging,
        secrets: Sequence[str] = (),
        clock: Clock = utc_now,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        recipient_policy: RecipientPolicy | None = None,
    ) -> None:
        self._clock = clock
        self._max_attachment_bytes = max_attachment_bytes
        self._recipient_policy = recipient_policy
        self._b = backends
        self._artifacts = artifacts
        self._jobs = jobs
        self._staging = staging
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
            result: JobResult | None = None  # None for a summary job
            outgoing: tuple[OutgoingMail, JobResult] | None = None
            with interruptible():
                if job.kind == "summary":
                    self._summarize(claim)
                else:
                    transcript = self._stage_work(claim, state)
                    result = JobResult(
                        duration_s=transcript.duration_s, language=transcript.language
                    )
                    if job.delivery != "none":
                        self._jobs.set_stage(claim, "deliver")
                        outgoing = (self._compose(job, state, transcript), result)
            # Delivery runs outside the interruptible window: a SIGTERM waits for it.
            if outgoing is None:
                done = self._jobs.complete(claim, result)
            else:
                done = self._deliver(claim, *outgoing)
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
        self.finish(done, state.episode)
        return done

    def finish(self, job: Job, episode: Episode | None) -> None:
        """A job reached a terminal state: delete its staged audio, then notify."""
        self._staging.release(job)
        self.notify(job, episode)

    def notify(self, job: Job, episode: Episode | None) -> None:
        notifier = self._b.notifier
        if notifier is None or job.delivery == "none":
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
        try:
            return episode_for_job(job, self._artifacts)
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
            self.finish(job, episode)
        return job

    # -- stages -----------------------------------------------------------------------

    def _warn(self, job_id: str, warnings: Iterable[str]) -> None:
        for warning in warnings:
            self._jobs.add_warning(job_id, warning)

    def _stage_work(self, claim: Claim, state: _RunState) -> Transcript:
        job = claim.job
        jobs = self._jobs
        b = self._b
        staging = self._staging

        jobs.set_stage(claim, "resolve")
        audio: Path | None = None
        if job.source == "apple_podcasts":
            if job.url is None:
                raise PermanentError("ingest job without a url")
            episode = b.resolver.resolve(job.url)
        elif job.source == "upload":
            if job.upload_file is None or job.content_sha256 is None:
                raise PermanentError("upload job without its file")
            audio = staging.upload_path(job.upload_file)
            if not audio.is_file():
                raise PermanentError("the uploaded audio is missing; upload it again")
            if sha256_file(audio) != job.content_sha256:
                raise PermanentError("the uploaded audio changed after it was received")
            episode = audio_episode(job, job.content_sha256)
        elif job.source == "youtube":
            if job.url is None:
                raise PermanentError("ingest job without a url")
            video_id = video_id_from_canonical(job.url)
            episode = b.youtube.resolve(video_id)
            if episode.source != "youtube" or episode.key != youtube_key(video_id):
                raise PermanentError("the YouTube source returned another video")
        elif job.source == "url":
            if job.url is None:
                raise PermanentError("ingest job without a url")
            jobs.set_stage(claim, "fetch")
            staging.clear_download(job.id)
            audio = b.fetcher.download(job.url, staging.download_destination(job.id))
            episode = audio_episode(job, sha256_file(audio))
        else:
            raise PermanentError("unknown source")
        state.episode = episode
        options = job.decode_options()
        if options is None:
            self._artifacts.write_episode(episode)
            jobs.set_episode_key(claim, episode.key)
            state.first_shared_at = self._artifacts.record_share(episode.key, job.shared_at)
            transcript = b.transcriber.cached(episode)
        else:
            jobs.set_episode_key(claim, episode.key)
            transcript = b.transcriber.cached(episode, options)
        if transcript is None:
            if audio is None:
                jobs.set_stage(claim, "fetch")
                if job.source == "youtube":
                    staging.clear_download(job.id)
                    audio = b.youtube.download(
                        video_id_from_canonical(job.url or ""),
                        staging.download_destination(job.id),
                    )
                else:
                    audio = b.fetcher.fetch(episode)
            jobs.set_stage(claim, "transcribe")
            if options is None:
                transcript = b.transcriber.transcribe(episode, audio)
            else:
                transcript = b.transcriber.transcribe(episode, audio, options)
        else:
            logger.info("job %s: transcript for %s is cached", job.id, episode.key)
        if transcript.episode_key != episode.key:
            raise PermanentError("transcriber returned a transcript for another episode")
        self._warn(job.id, transcript.warnings)
        return transcript

    def _summarize(self, claim: Claim) -> None:
        """One model call on the staged input; the summary is written only if it is whole."""
        job = claim.job
        self._jobs.set_stage(claim, "summarize")
        summarizer = self._b.summarizer
        if summarizer is None:
            raise PermanentError(NOT_CONFIGURED)
        if job.upload_file is None or job.content_sha256 is None:
            raise PermanentError(INPUT_MISSING)
        path = self._staging.upload_path(job.upload_file)
        if not path.is_file():
            raise PermanentError(INPUT_MISSING)
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != job.content_sha256:
            raise PermanentError(INPUT_CHANGED)
        try:
            request = SummaryInput.model_validate_json(raw)
        except (ValidationError, UnicodeDecodeError):
            raise PermanentError(INPUT_UNREADABLE) from None
        started = time.monotonic()
        result = summarizer.summarize(request)
        elapsed = time.monotonic() - started
        check_result(result, max_output_tokens=summarizer.max_output_tokens)
        self._artifacts.write_summary(
            SummaryRecord(
                job_id=job.id,
                backend=summarizer.backend,
                model=result.model,
                text=result.text,
                warnings=result.warnings,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                context_tokens=result.context_tokens,
                created_at=self._clock(),
            )
        )
        self._warn(job.id, result.warnings)
        prompt = "?" if result.prompt_tokens is None else result.prompt_tokens
        completion = "?" if result.completion_tokens is None else result.completion_tokens
        logger.info(
            "job %s: summary of %d transcript and %d instructions characters by %s model %s: "
            "%s prompt tokens, %s output tokens, %.1f s",
            job.id,
            len(request.transcript),
            len(request.instructions),
            summarizer.backend,
            result.model,
            prompt,
            completion,
            elapsed,
        )

    # -- delivery ---------------------------------------------------------------------

    def _compose(self, job: Job, state: _RunState, transcript: Transcript) -> OutgoingMail:
        episode = state.episode
        first_shared_at = state.first_shared_at
        if episode is None or first_shared_at is None:
            raise PermanentError("no episode to deliver")  # unreachable after _stage_work
        if job.to is None:
            raise PermanentError("this request has no destination address; share it again")
        policy = self._recipient_policy
        if policy is not None and not policy.allows(job.to):
            raise PermanentError(
                "the destination is no longer allowed by EARMARK_ALLOWED_RECIPIENTS; "
                "nothing was sent"
            )
        return compose_mail(
            job,
            episode,
            transcript,
            first_shared_at=first_shared_at,
            sender=self._b.mailer.sender,
            date=self._clock(),
            max_attachment_bytes=self._max_attachment_bytes,
        )

    def _deliver(self, claim: Claim, mail: OutgoingMail, result: JobResult) -> Job:
        """Send ``mail`` with the delivery state written around it; return the finished job."""
        job = claim.job
        if job.delivery_state == "sent":
            logger.warning("job %s is already marked sent; completing it without sending", job.id)
            return self._jobs.complete(claim, result)
        self._jobs.mark_sending(claim, mail.message_id)
        try:
            self._b.mailer.send(mail)
        except MaybeDelivered:
            raise  # the state stays `sending`: the next attempt resends and says so
        except Exception:
            self._jobs.clear_sending(claim)
            raise
        done = self._jobs.complete_sent(claim, result)
        logger.info("job %s: sent to %s", job.id, mask_address(mail.recipient))
        return done
