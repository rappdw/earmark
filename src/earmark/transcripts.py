"""A job's cached transcript as the mailed note, the plain text or the JSON view.

Reads the job and the files under episodes/<key>/ only: writes nothing, starts no stage,
makes no request, logs nothing. The API route and `earmark transcript` share it.
"""

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.mail.note import render_note
from earmark.models import (
    AUDIO_SOURCES,
    GUID_SHA256_PREFIX,
    Episode,
    EpisodeKey,
    Job,
    JobId,
    Transcript,
    TranscriptFormat,
    content_key,
)
from earmark.sources import audio_episode

TRANSCRIPT_FORMATS: Final[tuple[TranscriptFormat, ...]] = ("note", "text", "json")
NOTE_MEDIA_TYPE: Final = "text/markdown; charset=utf-8"
TEXT_MEDIA_TYPE: Final = "text/plain; charset=utf-8"
JSON_MEDIA_TYPE: Final = "application/json"
JOB_NOT_FOUND: Final = "job not found"

NOT_YET: Final = "this job has no transcript yet; it is {status}"
NOT_YET_AT_STAGE: Final = "this job has no transcript yet; it is {status} (stage: {stage})"
FAILED_BEFORE: Final = "this job has no transcript; it failed before the transcript was made"
SKIPPED: Final = "this job has no transcript; it was skipped"
GONE: Final = "the cache of this job is gone: episodes/{key}/{file} is missing; share it again"
GONE_NO_KEY: Final = "the cache of this job is gone: it has no episode key; share it again"
UNREADABLE: Final = (
    "the cache of this job is unreadable: episodes/{key}/{file}; "
    "delete episodes/{key}/ and share it again"
)

SUMMARY_ELSEWHERE: Final = "this job is a summary; fetch it from /v1/jobs/{id}/summary"
V1_ELSEWHERE: Final = "this job was sent to /v1; fetch its transcript from /v1/jobs/{id}/transcript"

UnavailableReason = Literal["not_ready", "gone", "unreadable"]


class TranscriptUnavailable(Exception):  # noqa: N818  # an outcome, like SkipEpisode
    """No transcript to serve. ``detail`` is the message for the API and the CLI."""

    def __init__(self, reason: UnavailableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: UnavailableReason = reason
        self.detail: str = detail


@dataclass(frozen=True, slots=True)
class TranscriptFile:
    format: TranscriptFormat
    data: bytes  # UTF-8
    media_type: str
    filename: str | None  # "note": the note attachment's file name; else None


class SegmentView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: float
    end: float
    text: str
    speaker: str | None


class TranscriptView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: JobId
    episode_key: EpisodeKey
    language: str | None
    duration_s: float
    segments: tuple[SegmentView, ...]
    warnings: tuple[str, ...]


def has_transcript(job: Job) -> bool:
    """True when this job produced a transcript in some attempt."""
    return job.status == "done" or job.stage == "deliver"


def episode_for_job(job: Job, artifacts: ArtifactStore) -> Episode | None:
    """The Episode the runner rendered this job's note from, rebuilt from the cache.

    upload: audio_episode(job, job.content_sha256), no file read.
    url: audio_episode(job, <hex of episode.json's sha256: guid>).
    apple_podcasts, youtube: episode.json as stored.
    summary: None, a summary has no episode.
    None when there is nothing to read from. Raises PermanentError when episode.json is
    corrupt, or when a URL job's episode.json has no sha256 guid or a key other than the
    job's.
    """
    if job.kind == "summary":
        return None
    if job.source == "upload":
        if job.content_sha256 is None:
            return None
        return audio_episode(job, job.content_sha256)
    if job.episode_key is None:
        return None
    episode = artifacts.read_episode(job.episode_key)
    if episode is None or job.source != "url":
        return episode
    guid = episode.guid
    digest = guid.removeprefix(GUID_SHA256_PREFIX)
    if (
        episode.source not in AUDIO_SOURCES
        or not guid.startswith(GUID_SHA256_PREFIX)
        or len(digest) != 64
        or content_key(digest) != job.episode_key
    ):
        raise PermanentError("episode.json does not describe this URL job's audio")
    return audio_episode(job, digest)


def _not_ready(job: Job) -> TranscriptUnavailable:
    if job.status == "failed":
        detail = FAILED_BEFORE
    elif job.status == "skipped":
        detail = SKIPPED
    elif job.stage is None:
        detail = NOT_YET.format(status=job.status)
    else:
        detail = NOT_YET_AT_STAGE.format(status=job.status, stage=job.stage)
    return TranscriptUnavailable("not_ready", detail)


def _gone(key: str, file: str) -> TranscriptUnavailable:
    return TranscriptUnavailable("gone", GONE.format(key=key, file=file))


def _unreadable(key: str, file: str) -> TranscriptUnavailable:
    return TranscriptUnavailable("unreadable", UNREADABLE.format(key=key, file=file))


def read_transcript_file(
    job: Job, artifacts: ArtifactStore, fmt: TranscriptFormat
) -> TranscriptFile:
    """The job's transcript in ``fmt``. Raises TranscriptUnavailable."""
    if job.kind == "summary":
        raise TranscriptUnavailable("not_ready", SUMMARY_ELSEWHERE)
    if job.delivery == "none":
        raise TranscriptUnavailable("not_ready", V1_ELSEWHERE)
    if not has_transcript(job):
        raise _not_ready(job)
    key = job.episode_key
    if key is None:
        raise TranscriptUnavailable("gone", GONE_NO_KEY)
    if not artifacts.has_transcript(key):
        raise _gone(key, "transcript.json")
    if fmt == "text":
        try:
            data = artifacts.transcript_text_path(key).read_bytes()
        except FileNotFoundError:
            raise _gone(key, "transcript.txt") from None
        except OSError:
            raise _unreadable(key, "transcript.txt") from None
        return TranscriptFile("text", data, TEXT_MEDIA_TYPE, None)
    try:
        transcript = artifacts.read_transcript(key)
    except (PermanentError, OSError):
        raise _unreadable(key, "transcript.json") from None
    if transcript is None:
        raise _gone(key, "transcript.json")
    if fmt == "json":
        return _json_file(job, transcript)
    return _note_file(job, artifacts, key, transcript)


def _json_file(job: Job, transcript: Transcript) -> TranscriptFile:
    view = TranscriptView(
        job_id=job.id,
        episode_key=transcript.episode_key,
        language=transcript.language,
        duration_s=transcript.duration_s,
        segments=tuple(
            SegmentView(start=s.start, end=s.end, text=s.text, speaker=s.speaker)
            for s in transcript.segments
        ),
        warnings=transcript.warnings,
    )
    data = (view.model_dump_json(indent=2) + "\n").encode("utf-8")
    return TranscriptFile("json", data, JSON_MEDIA_TYPE, None)


def _note_file(
    job: Job, artifacts: ArtifactStore, key: str, transcript: Transcript
) -> TranscriptFile:
    try:
        episode = episode_for_job(job, artifacts)
    except (PermanentError, OSError):
        raise _unreadable(key, "episode.json") from None
    if episode is None:
        raise _gone(key, "episode.json")
    try:
        share = artifacts.read_share(key)
    except (PermanentError, OSError):
        raise _unreadable(key, "shared.json") from None
    if share is None:
        raise _gone(key, "shared.json")
    rendered = render_note(job, episode, transcript, first_shared_at=share.first_shared_at)
    # The instructions attachment is dropped: it is never served.
    return TranscriptFile("note", rendered.note.data, NOTE_MEDIA_TYPE, rendered.note.filename)
