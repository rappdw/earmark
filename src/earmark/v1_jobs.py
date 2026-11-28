"""The /v1 API's reads and its purge: a delivery-free job's transcript and its deletion.

Imports the models, the artifact store, the job store and staging only: no FastAPI and no
heavy libraries. Reads write nothing and log nothing. Logs carry ids and counts, never the
prompt, a file name or transcript text.
"""

import logging
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.jobs import JobActiveError, JobNotFoundError, JobStore
from earmark.models import Job, JobId, decode_key
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
