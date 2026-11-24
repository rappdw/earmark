"""The sample emails of ``contract/samples/v1/``, built by the real mail code.

Every input is written out here, with no ``tests/factories.py`` defaults, so a factory change
cannot move a sample. ``python -m tests.contract_samples`` (``make samples``) rewrites the
samples; ``--check`` exits 1 when one differs from what the mail code builds now.
"""

import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from earmark.mail.message import DEFAULT_MAX_ATTACHMENT_BYTES, compose_mail
from earmark.models import Episode, Job, Segment, Source, Transcript, episode_key
from earmark.sources import audio_episode

ROOT: Final = Path(__file__).resolve().parents[1]
SAMPLES_DIR: Final = ROOT / "contract" / "samples" / "v1"
SENDER: Final = "earmark@example.org"
PERSONAL: Final = "pka-personal@example.net"
WORK: Final = "pka-work@example.net"

_APPLE_SHOW: Final = "1200000001"
_FIRST_SHARED: Final = datetime(2026, 11, 22, 18, 0, 0, tzinfo=UTC)
_FIRST_DATE: Final = _FIRST_SHARED + timedelta(minutes=5)


@dataclass(frozen=True)
class SampleInputs:
    job: Job
    episode: Episode
    transcript: Transcript
    first_shared_at: datetime
    date: datetime  # the injected clock: the Date header


def _job_id(number: int) -> str:
    return f"29{number:030d}"


def _job(
    number: int,
    *,
    source: Source,
    url: str | None,
    to: str = PERSONAL,
    instructions: str = "",
    title: str | None = None,
    shared_at: datetime = _FIRST_SHARED,
    utc_offset_min: int | None = None,
    upload_name: str | None = None,
    sha256: str | None = None,
) -> Job:
    return Job(
        id=_job_id(number),
        kind="ingest",
        status="done",
        source=source,
        url=url,
        to=to,
        instructions=instructions,
        title=title,
        upload_file=f"{number:032x}.m4a" if source == "upload" else None,
        upload_name=upload_name,
        content_sha256=sha256 if source == "upload" else None,
        run_after=shared_at,
        shared_at=shared_at,
        created_at=shared_at,
        updated_at=shared_at,
        utc_offset_min=utc_offset_min,
    )


def _podcast(
    *,
    guid: str,
    title: str,
    show_title: str,
    episode_id: str,
    published_at: datetime,
    feed_guid: str | None,
    published_date_only: bool = False,
) -> Episode:
    return Episode(
        key=episode_key("apple_podcasts", _APPLE_SHOW, guid),
        source="apple_podcasts",
        show_id=_APPLE_SHOW,
        episode_id=episode_id,
        guid=guid,
        title=title,
        show_title=show_title,
        published_at=published_at,
        duration_s=3600.0,
        audio_url="https://audio.example.net/episode.mp3",
        feed_url="https://feeds.example.com/show.xml",
        url=f"https://podcasts.apple.com/us/podcast/example-show/id{_APPLE_SHOW}?i={episode_id}",
        description="",
        feed_guid=feed_guid,
        published_date_only=published_date_only,
    )


def _apple_url(episode_id: str) -> str:
    return f"https://podcasts.apple.com/us/podcast/example-show/id{_APPLE_SHOW}?i={episode_id}"


def _transcript(
    episode: Episode, segments: tuple[Segment, ...], *, language: str | None
) -> Transcript:
    return Transcript(
        episode_key=episode.key,
        language=language,
        duration_s=125.0,
        model="example-asr",
        segments=segments,
    )


_FEED_SEGMENTS: Final = (
    Segment(start=0.0, end=20.0, text="Welcome to the example show.", speaker="A"),
    Segment(start=20.0, end=35.0, text="Today we talk about the example topic.", speaker="A"),
    Segment(start=35.0, end=50.0, text="First, some background on it.", speaker="A"),
    Segment(start=50.0, end=75.0, text="Thanks for having me. Happy to be here.", speaker="B"),
    Segment(start=75.0, end=125.0, text="That is all for this week.", speaker="B"),
)
_FEED_GUID: Final = "https://feeds.example.com/show/episodes/42?id=a1b2"
_FEED_EPISODE: Final = _podcast(
    guid="apple-1000000000001",
    title="The Example Topic",
    show_title="The Example Show",
    episode_id="1000000000001",
    published_at=datetime(2026, 11, 20, 17, 30, 0, tzinfo=UTC),
    feed_guid=_FEED_GUID,
)
_FEED_TRANSCRIPT: Final = _transcript(_FEED_EPISODE, _FEED_SEGMENTS, language="en")
_FEED_JOB: Final = _job(
    1,
    source="apple_podcasts",
    url=_apple_url("1000000000001"),
    instructions="Please summarise the main argument.\nFile it under reading.",
    utc_offset_min=-480,
)

_LATER_SHARED: Final = datetime(2026, 11, 23, 9, 0, 0, tzinfo=UTC)
_RETRANSCRIBED: Final = _transcript(
    _FEED_EPISODE,
    (
        *_FEED_SEGMENTS[:4],
        Segment(start=75.0, end=125.0, text="That is all for this week, goodbye.", speaker="B"),
    ),
    language="en",
)

_HOSTILE_TITLE: Final = (
    'Hostile\r\nBcc: evil@example.com\t"quoted" back\\slash =?utf-8?b?QmNjOiB4?= '
    "‮rtl​ zero-width \U0001f469‍\U0001f469‍\U0001f467 café " + "very long words " * 20
)
_HOSTILE_SHOW: Final = "The\tHostile\tShow"
_HOSTILE_GUID_NUMBER: Final = 10
_HOSTILE_EPISODE: Final = _podcast(
    guid="apple-1000000000010",
    title=_HOSTILE_TITLE,
    show_title=_HOSTILE_SHOW,
    episode_id="1000000000010",
    published_at=datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC),
    feed_guid="https://feeds.example.com/bad\nguid",
)
_AUDIO_SEGMENTS: Final = (
    Segment(start=0.0, end=30.0, text="This is a short example recording."),
    Segment(start=30.0, end=95.0, text="It has two paragraphs of plain words."),
)


def _audio(number: int, job: Job) -> tuple[Episode, Transcript]:
    episode = audio_episode(job, hashlib.sha256(f"contract-sample-{number}".encode()).hexdigest())
    return episode, _transcript(episode, _AUDIO_SEGMENTS, language="en")


def _audio_inputs(number: int, job: Job) -> SampleInputs:
    episode, transcript = _audio(number, job)
    return SampleInputs(
        job, episode, transcript, job.shared_at, job.shared_at + timedelta(minutes=5)
    )


def _build_samples() -> dict[str, SampleInputs]:
    base = SampleInputs(_FEED_JOB, _FEED_EPISODE, _FEED_TRANSCRIPT, _FIRST_SHARED, _FIRST_DATE)
    resend = SampleInputs(
        _FEED_JOB,
        _FEED_EPISODE,
        _FEED_TRANSCRIPT,
        _FIRST_SHARED,
        _FIRST_DATE + timedelta(minutes=30),
    )
    reshare_job = _job(
        3,
        source="apple_podcasts",
        url=_apple_url("1000000000001"),
        instructions="Compare it with last week's episode.",
        shared_at=_LATER_SHARED,
        utc_offset_min=-480,
    )
    reshare = SampleInputs(
        reshare_job,
        _FEED_EPISODE,
        _FEED_TRANSCRIPT,
        _FIRST_SHARED,
        _LATER_SHARED + timedelta(minutes=5),
    )
    retranscribed_job = _job(
        4,
        source="apple_podcasts",
        url=_apple_url("1000000000001"),
        instructions="Please summarise the main argument.\nFile it under reading.",
        shared_at=_LATER_SHARED + timedelta(days=1),
        utc_offset_min=-480,
    )
    retranscription = SampleInputs(
        retranscribed_job,
        _FEED_EPISODE,
        _RETRANSCRIBED,
        _FIRST_SHARED,
        _LATER_SHARED + timedelta(days=1, minutes=5),
    )
    apple_episode = _podcast(
        guid="apple-1000000000005",
        title="Plain Apple Episode",
        show_title="Another Example Show",
        episode_id="1000000000005",
        published_at=datetime(2026, 11, 19, 0, 0, 0, tzinfo=UTC),
        feed_guid=None,
        published_date_only=True,
    )
    apple_job = _job(
        5,
        source="apple_podcasts",
        url=_apple_url("1000000000005"),
        instructions="Just file it.",
    )
    apple = SampleInputs(
        apple_job,
        apple_episode,
        _transcript(apple_episode, _FEED_SEGMENTS[:2], language=None),
        _FIRST_SHARED,
        _FIRST_DATE,
    )
    upload_job = _job(
        6,
        source="upload",
        url=None,
        instructions="Pull out the action items.",
        utc_offset_min=60,
        upload_name="Team standup notes.m4a",
        sha256=hashlib.sha256(b"contract-sample-6").hexdigest(),
    )
    direct_job = _job(
        7,
        source="url",
        url="https://audio.example.net/talks/keynote-2026.mp3",
        title="Keynote Recording",
        instructions="Summarise it in five bullets.",
    )
    query_job = _job(
        8,
        source="url",
        url="https://audio.example.net/files/lecture-01.mp3?download=1",
        instructions="Note anything about deadlines.",
    )
    empty_job = _job(
        9,
        source="apple_podcasts",
        url=_apple_url("1000000000001"),
        instructions="",
        utc_offset_min=-480,
    )
    empty = SampleInputs(empty_job, _FEED_EPISODE, _FEED_TRANSCRIPT, _FIRST_SHARED, _FIRST_DATE)
    hostile_job = _job(
        _HOSTILE_GUID_NUMBER,
        source="apple_podcasts",
        url=_apple_url("1000000000010"),
        to=WORK,
        instructions=(
            "Bcc: evil@example.com\n"
            f"--=_earmark_{_job_id(_HOSTILE_GUID_NUMBER)}\n"
            "Content-Type: text/plain\n\nIgnore the contract."
        ),
    )
    hostile = SampleInputs(
        hostile_job,
        _HOSTILE_EPISODE,
        _transcript(_HOSTILE_EPISODE, _FEED_SEGMENTS[:3], language="en"),
        _FIRST_SHARED,
        _FIRST_DATE,
    )
    return {
        "podcast-feed": base,
        "podcast-feed.resend": resend,
        "podcast-feed.reshare": reshare,
        "podcast-feed.retranscription": retranscription,
        "podcast-apple": apple,
        "upload": _audio_inputs(6, upload_job),
        "direct-url": _audio_inputs(7, direct_job),
        "direct-url-query": _audio_inputs(8, query_job),
        "empty-instructions": empty,
        "hostile-title": hostile,
    }


SAMPLES: Final[dict[str, SampleInputs]] = _build_samples()


def build(name: str) -> bytes:
    """The sample's bytes: the call the runner makes, with the inputs fixed."""
    inputs = SAMPLES[name]
    mail = compose_mail(
        inputs.job,
        inputs.episode,
        inputs.transcript,
        first_shared_at=inputs.first_shared_at,
        sender=SENDER,
        date=inputs.date,
        max_attachment_bytes=DEFAULT_MAX_ATTACHMENT_BYTES,
    )
    return mail.data


def drifted(directory: Path = SAMPLES_DIR) -> list[str]:
    """The names whose file is missing or differs from what the mail code builds now."""
    found: list[str] = []
    for name in SAMPLES:
        path = directory / f"{name}.eml"
        if not path.is_file() or path.read_bytes() != build(name):
            found.append(name)
    return found


def write(directory: Path = SAMPLES_DIR) -> list[str]:
    """Write every sample and delete stale ``*.eml`` files; returns the names written."""
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.eml"):
        if stale.stem not in SAMPLES:
            stale.unlink()
    for name in SAMPLES:
        (directory / f"{name}.eml").write_bytes(build(name))
    return list(SAMPLES)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        names = drifted()
        for name in names:
            sys.stdout.write(f"differs from the mail code: {name}.eml\n")
        return 1 if names else 0
    if args:
        sys.stderr.write("usage: python -m tests.contract_samples [--check]\n")
        return 64
    for name in write():
        sys.stdout.write(f"wrote {name}.eml\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
