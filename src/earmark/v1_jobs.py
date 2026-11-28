"""The /v1 API's reads and its purge: a delivery-free job's transcript and its deletion.

Imports the models, the artifact store, the job store and staging only: no FastAPI and no
heavy libraries. Reads write nothing and log nothing. Logs carry ids and counts, never the
prompt, a file name or transcript text.
"""

import logging
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.jobs import JobActiveError, JobNotFoundError, JobStore
from earmark.models import MAX_SUMMARY_INSTRUCTIONS_CHARS, Job, JobId, decode_key
from earmark.staging import Staging
from earmark.transcripts import (
    JSON_MEDIA_TYPE,
    NOT_YET,
    NOT_YET_AT_STAGE,
    SKIPPED,
    TEXT_MEDIA_TYPE,
    TranscriptFile,
    TranscriptUnavailable,
)

logger = logging.getLogger(__name__)

V1TranscriptFormat = Literal["json", "text"]

PURGED: Final = "this job was deleted; its transcript is gone"
V1_FAILED: Final = "this job has no transcript; it failed"
V1_GONE: Final = "the transcript of this job is gone; send the audio again"
V1_UNREADABLE: Final = "the transcript of this job is unreadable; send the audio again"
STILL_ACTIVE: Final = "this job is still {status}; delete it once it has finished"
V1_ONLY_MULTIPART: Final = (
    "send a multipart form (Content-Type: multipart/form-data) with the audio in a field "
    "named 'file'"
)
DELETE_FAILED: Final = "could not delete the transcript files; try again"

SUMMARIES_OFF: Final = "summaries are not configured on this server (EARMARK_LLM_BACKEND)"
SUMMARY_ONLY_JSON: Final = (
    "send JSON (Content-Type: application/json) with the fields transcript and instructions"
)
SUMMARY_BODY_TOO_LARGE: Final = (
    "the request body is larger than {limit}, the most a transcript at the server's cap can "
    "take (EARMARK_LLM_MAX_TRANSCRIPT_CHARS)"
)
SUMMARY_MISSING_BODY: Final = (
    "request body is missing; send a JSON object with transcript and instructions"
)
TRANSCRIPT_TOO_LONG: Final = (
    "transcript is too long (at most {n} characters on this server; "
    "EARMARK_LLM_MAX_TRANSCRIPT_CHARS)"
)
SUMMARY_NOT_YET: Final = "this job has no summary yet; it is {status}"
SUMMARY_NOT_YET_AT_STAGE: Final = "this job has no summary yet; it is {status} (stage: {stage})"
SUMMARY_FAILED: Final = "this job has no summary; it failed"
SUMMARY_SKIPPED: Final = "this job has no summary; it was skipped"
NOT_A_SUMMARY: Final = (
    "this job is a transcription; fetch its transcript from /v1/jobs/{job_id}/transcript"
)
NOT_A_TRANSCRIPTION: Final = "this job is a summary; fetch it from /v1/jobs/{job_id}/summary"
SUMMARY_PURGED: Final = "this job was deleted; its summary is gone"
SUMMARY_GONE: Final = "the summary of this job is gone; send the transcript again"
SUMMARY_UNREADABLE: Final = "the summary of this job is unreadable; send the transcript again"
SUMMARY_DELETE_FAILED: Final = "could not delete the summary files; try again"

# JSON's worst case is six bytes for one escaped character; twice that is room to spare.
_BODY_BYTES_PER_CHAR: Final = 12
_BODY_SLACK_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class SummaryLimits:
    """The caps of POST /v1/summaries; None in place of it means summaries are off."""

    max_transcript_chars: int

    @property
    def max_body_bytes(self) -> int:
        chars = self.max_transcript_chars + MAX_SUMMARY_INSTRUCTIONS_CHARS
        return _BODY_BYTES_PER_CHAR * chars + _BODY_SLACK_BYTES


def delete_message(job: Job | None) -> str:
    """The 500 text for a failed file deletion on this kind of job."""
    return SUMMARY_DELETE_FAILED if job is not None and job.kind == "summary" else DELETE_FAILED


class V1SummaryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: JobId
    model: str
    text: str
    warnings: tuple[str, ...]


class V1SegmentView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: float
    end: float
    text: str


class V1TranscriptView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: JobId
    language: str | None
    duration_s: float
    segments: tuple[V1SegmentView, ...]
    warnings: tuple[str, ...]


def v1_job(store: JobStore, job_id: str) -> Job | None:
    """The job, or None when it is unknown or not a delivery-free job."""
    job = store.get(job_id)
    if job is None or job.delivery != "none":
        return None
    return job


def _dkey(job: Job) -> str:
    options = job.decode_options()
    if job.content_sha256 is None or options is None:  # a delivery-free job always has both
        raise PermanentError("a /v1 job without its audio hash")
    return decode_key(job.content_sha256, options)


def read_v1_transcript(
    job: Job, artifacts: ArtifactStore, fmt: V1TranscriptFormat
) -> TranscriptFile:
    """The transcript of a delivery-free job. Raises TranscriptUnavailable."""
    if job.kind == "summary":
        raise TranscriptUnavailable("not_ready", NOT_A_TRANSCRIPTION)
    if job.purged_at is not None:
        raise TranscriptUnavailable("gone", PURGED)
    if job.status in ("queued", "running"):
        if job.stage is None:
            detail = NOT_YET.format(status=job.status)
        else:
            detail = NOT_YET_AT_STAGE.format(status=job.status, stage=job.stage)
        raise TranscriptUnavailable("not_ready", detail)
    if job.status == "failed":
        raise TranscriptUnavailable("not_ready", V1_FAILED)
    if job.status == "skipped":
        raise TranscriptUnavailable("not_ready", SKIPPED)
    dkey = _dkey(job)
    if not artifacts.has_decoded(dkey):
        raise TranscriptUnavailable("gone", V1_GONE)
    if fmt == "text":
        try:
            data = artifacts.decoded_text_path(dkey).read_bytes()
        except FileNotFoundError:
            raise TranscriptUnavailable("gone", V1_GONE) from None
        except OSError:
            raise TranscriptUnavailable("unreadable", V1_UNREADABLE) from None
        return TranscriptFile("text", data, TEXT_MEDIA_TYPE, None)
    try:
        transcript = artifacts.read_decoded(dkey, job.episode_key or "")
    except (PermanentError, OSError):
        raise TranscriptUnavailable("unreadable", V1_UNREADABLE) from None
    if transcript is None:
        raise TranscriptUnavailable("gone", V1_GONE)
    view = V1TranscriptView(
        job_id=job.id,
        language=transcript.language,
        duration_s=transcript.duration_s,
        segments=tuple(
            V1SegmentView(start=s.start, end=s.end, text=s.text) for s in transcript.segments
        ),
        warnings=transcript.warnings,
    )
    data = (view.model_dump_json(indent=2) + "\n").encode("utf-8")
    return TranscriptFile("json", data, JSON_MEDIA_TYPE, None)


def read_v1_summary(job: Job, artifacts: ArtifactStore) -> bytes:
    """The JSON view of a summary job's output. Raises TranscriptUnavailable.

    Writes nothing and logs nothing. The first matching state decides.
    """
    if job.kind != "summary":
        raise TranscriptUnavailable("not_ready", NOT_A_SUMMARY)
    if job.purged_at is not None:
        raise TranscriptUnavailable("gone", SUMMARY_PURGED)
    if job.status in ("queued", "running"):
        if job.stage is None:
            detail = SUMMARY_NOT_YET.format(status=job.status)
        else:
            detail = SUMMARY_NOT_YET_AT_STAGE.format(status=job.status, stage=job.stage)
        raise TranscriptUnavailable("not_ready", detail)
    if job.status == "failed":
        raise TranscriptUnavailable("not_ready", SUMMARY_FAILED)
    if job.status == "skipped":
        raise TranscriptUnavailable("not_ready", SUMMARY_SKIPPED)
    try:
        record = artifacts.read_summary(job.id)
    except (PermanentError, OSError):
        raise TranscriptUnavailable("unreadable", SUMMARY_UNREADABLE) from None
    if record is None:
        raise TranscriptUnavailable("gone", SUMMARY_GONE)
    view = V1SummaryView(
        job_id=job.id, model=record.model, text=record.text, warnings=record.warnings
    )
    return (view.model_dump_json(indent=2) + "\n").encode("utf-8")


def purge_summary_job(store: JobStore, artifacts: ArtifactStore, staging: Staging, job: Job) -> Job:
    """Delete a finished summary job's output and any input left; the job is purged alone."""
    removed = artifacts.remove_summary(job.id)
    if job.upload_file is not None:
        staging.remove_upload(job.upload_file)
    purged = store.mark_purged(job.id)
    if not store.checkpoint():
        logger.debug("the database checkpoint after a purge was busy")
    logger.info("job %s purged (%d job(s), %d file(s) removed)", job.id, len(purged), removed)
    return purged[0]


def purge_job(store: JobStore, artifacts: ArtifactStore, staging: Staging, job_id: str) -> Job:
    """Delete a finished delivery-free job's transcript, staged file and prompt.

    Files go first, so a failure leaves the job unpurged and a retry works. Raises
    JobNotFoundError, JobActiveError, or OSError from the file deletions.
    """
    job = v1_job(store, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if job.purged_at is not None:
        return job
    if job.status in ("queued", "running"):
        raise JobActiveError(job)
    if job.kind == "summary":
        return purge_summary_job(store, artifacts, staging, job)
    removed = artifacts.remove_decoded(_dkey(job))
    if job.upload_file is not None:
        staging.remove_upload(job.upload_file)
    purged = store.mark_purged(job_id)
    for other in purged[1:]:
        if other.upload_file is None:
            continue
        try:
            staging.remove_upload(other.upload_file)
        except OSError as exc:
            logger.warning("could not delete a staged upload: %s", type(exc).__name__)
    if not store.checkpoint():
        logger.debug("the database checkpoint after a purge was busy")
    logger.info("job %s purged (%d job(s), %d file(s) removed)", job_id, len(purged), removed)
    return purged[0]
