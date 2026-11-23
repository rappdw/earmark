"""Sample-model builders shared by the test modules."""

from datetime import UTC, datetime, timedelta, timezone

from earmark.models import (
    Episode,
    Job,
    JobResult,
    Segment,
    Transcript,
    episode_key,
)

PST = timezone(timedelta(hours=-8))
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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
) -> Episode:
    return Episode(
        key=episode_key("apple_podcasts", "1200361736", guid),
        source="apple_podcasts",
        show_id="1200361736",
        episode_id="1000700000001",
        guid=guid,
        title=title,
        show_title="Le Podcast ☕",
        published_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=PST),
        duration_s=duration_s,
        audio_url="https://example.com/audio.mp3",
        feed_url="https://example.com/feed.xml",
        url="https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000700000001",
        description="Une description avec des accents: é à ü.",
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


def make_job() -> Job:
    return Job(
        id="a" * 32,
        kind="ingest",
        status="running",
        stage="fetch",
        url="https://podcasts.apple.com/us/podcast/x/id1?i=2",
        prompt="Quoi ?",
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
