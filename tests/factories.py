"""Sample-model builders shared by the test modules."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone

from earmark.jobs import NewJob
from earmark.models import (
    Episode,
    Job,
    JobResult,
    Segment,
    Source,
    Transcript,
    episode_key,
)
from earmark.sources import audio_episode

CANON = "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981"
TO = "you@example.org"
DIRECT_URL = "https://audio.example.net/private/clip.mp3"
SHA = "ab" * 32
PST = timezone(timedelta(hours=-8))
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=PST)


class MutableClock:
    """A Clock whose time tests move by hand."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_episode(
    *,
    guid: str = "urn:example:guid-1",
    title: str = "Épisode 1: «Tests»",
    duration_s: float | None = 3600.0,
    feed_guid: str | None = None,
    show_title: str = "Le Podcast ☕",
    published_at: datetime | None = PUBLISHED_AT,
    published_date_only: bool = False,
    episode_id: str = "1000700000001",
) -> Episode:
    return Episode(
        key=episode_key("apple_podcasts", "1200361736", guid),
        source="apple_podcasts",
        show_id="1200361736",
        episode_id=episode_id,
        guid=guid,
        title=title,
        show_title=show_title,
        published_at=published_at,
        duration_s=duration_s,
        audio_url="https://example.com/audio.mp3",
        feed_url="https://example.com/feed.xml",
        url="https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000700000001",
        description="Une description avec des accents: é à ü.",
        feed_guid=feed_guid,
        published_date_only=published_date_only,
    )


def make_transcript(*, guid: str = "urn:example:guid-1") -> Transcript:
    return Transcript(
        episode_key=make_episode(guid=guid).key,
        language="fr",
        duration_s=125.5,
        model="fake-asr",
        segments=(
            Segment(start=0.0, end=65.0, text="Bonjour à tous", speaker="A"),
            Segment(start=65.0, end=125.5, text="Merci"),
        ),
        warnings=("a transcript warning",),
    )


def new_podcast_job(
    url: str = CANON,
    to: str = TO,
    *,
    instructions: str = "",
    title: str | None = None,
    shared_at: datetime | None = None,
) -> NewJob:
    return NewJob(
        source="apple_podcasts",
        url=url,
        to=to,
        instructions=instructions,
        title=title,
        shared_at=shared_at,
    )


def new_url_job(
    url: str = DIRECT_URL, to: str = TO, *, instructions: str = "", title: str | None = None
) -> NewJob:
    return NewJob(source="url", url=url, to=to, instructions=instructions, title=title)


def new_upload_job(
    sha256: str = SHA,
    to: str = TO,
    *,
    upload_file: str | None = None,
    upload_name: str | None = "clip.mp3",
    instructions: str = "",
    title: str | None = None,
) -> NewJob:
    return NewJob(
        source="upload",
        to=to,
        instructions=instructions,
        title=title,
        upload_file=upload_file or f"{sha256[:32]}.mp3",
        upload_name=upload_name,
        content_sha256=sha256,
    )


def make_job() -> Job:
    return Job(
        id="a" * 32,
        kind="ingest",
        status="running",
        stage="fetch",
        source="apple_podcasts",
        url="https://podcasts.apple.com/us/podcast/x/id1?i=2",
        to=TO,
        instructions="Quoi ?",
        episode_key=make_episode().key,
        attempts=1,
        run_after=T0,
        error=None,
        warnings=("w",),
        result=JobResult(duration_s=125.5),
        shared_at=datetime(2026, 1, 1, 4, 0, tzinfo=PST),
        created_at=T0,
        updated_at=T0,
        heartbeat_at=T0,
    )


DELIVERY_JOB_ID = "0123456789abcdef" * 2


def make_delivery_job(
    *,
    source: Source = "apple_podcasts",
    url: str | None = CANON,
    to: str | None = TO,
    instructions: str = "",
    title: str | None = None,
    utc_offset_min: int | None = -480,
    shared_at: datetime = T0,
    upload_name: str | None = None,
    sha256: str = SHA,
) -> Job:
    """A running job ready for delivery, with a fixed id."""
    upload = source == "upload"
    return Job(
        id=DELIVERY_JOB_ID,
        kind="ingest",
        status="running",
        stage="deliver",
        source=source,
        url=None if upload else url,
        to=to,
        instructions=instructions,
        title=title,
        upload_file=f"{sha256[:32]}.mp3" if upload else None,
        upload_name=upload_name,
        content_sha256=sha256 if upload else None,
        attempts=1,
        run_after=T0,
        shared_at=shared_at,
        created_at=T0,
        updated_at=T0,
        utc_offset_min=utc_offset_min,
    )


def make_audio_job(
    *,
    source: Source = "upload",
    title: str | None = None,
    upload_name: str | None = "clip.mp3",
    url: str = DIRECT_URL,
    instructions: str = "",
    sha256: str = SHA,
    utc_offset_min: int | None = -480,
) -> tuple[Job, Episode]:
    """An upload or direct-URL job and the episode the runner would build for it."""
    job = make_delivery_job(
        source=source,
        url=url,
        title=title,
        instructions=instructions,
        upload_name=upload_name if source == "upload" else None,
        sha256=sha256,
        utc_offset_min=utc_offset_min,
    )
    return job, audio_episode(job, sha256)


def make_transcript_with(
    segments: Sequence[Segment], *, episode: Episode | None = None, language: str | None = "en"
) -> Transcript:
    """A transcript of ``segments`` whose duration is the end of the last one."""
    return Transcript(
        episode_key=(episode or make_episode()).key,
        language=language,
        duration_s=max((seg.end for seg in segments), default=0.0),
        model="fake-asr",
        segments=tuple(segments),
    )
