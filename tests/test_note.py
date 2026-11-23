"""The transcript note and the instructions file, to the mail contract (``contract: 1``)."""

import hashlib
import re
from datetime import UTC, date, datetime, timedelta

import pytest
import yaml

from earmark.mail.note import (
    MAX_SLUG_CHARS,
    contract_text,
    note_guid,
    render_note,
    requester_tz,
    slugify,
    transcript_paragraphs,
)
from earmark.models import Segment
from tests.factories import (
    CANON,
    DELIVERY_JOB_ID,
    DIRECT_URL,
    SHA,
    T0,
    make_audio_job,
    make_delivery_job,
    make_episode,
    make_transcript_with,
)
from tests.test_sanitize import YAML_CASES

SEGMENTS = (
    Segment(start=0.0, end=5.0, text="Welcome to Le Podcast ☕."),
    Segment(start=5.0, end=10.0, text="Today: Épisode 1: «Tests»."),
    Segment(start=10.0, end=15.0, text="Thanks for listening."),
)
GOLDEN_BODY = (
    "# Le Podcast ☕ — Épisode 1: «Tests»\n\n"
    "## Transcript\n\n"
    "[00:00:00] Welcome to Le Podcast ☕. Today: Épisode 1: «Tests». Thanks for listening.\n"
)


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _front(raw: bytes) -> dict[str, object]:
    text = raw.decode()
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n") + 1]
    for line in block.splitlines():
        assert re.match(r"^([a-z][a-z0-9_]*:( |$)|  - )", line), line
    front = yaml.safe_load(block)
    assert isinstance(front, dict)
    return front


def _podcast(**episode_kw: object) -> tuple[bytes, str]:
    """(the note bytes, the file name) for a podcast job at -08:00."""
    job = make_delivery_job()
    episode = make_episode(**episode_kw)  # type: ignore[arg-type]  # keyword passthrough
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    )
    return rendered.note.data, rendered.note.filename


# -- exact output ------------------------------------------------------------------------


def test_podcast_note_exact() -> None:
    job = make_delivery_job(instructions="Which papers are mentioned?")
    episode = make_episode(feed_guid="urn:example:guid-1")
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    )
    assert rendered.note.filename == "2026-01-02-le-podcast-episode-1-tests.md"
    expected = (
        "---\n"
        'type: "podcast"\n'
        "contract: 1\n"
        'source: "earmark"\n'
        'origin: "apple_podcasts"\n'
        'title: "Épisode 1: «Tests»"\n'
        'show: "Le Podcast ☕"\n'
        "date: 2026-01-02\n"
        "published: 2026-01-02\n"
        'duration: "00:00:15"\n'
        'language: "en"\n'
        f'apple_url: "{CANON}"\n'
        'guid: "feed:urn:example:guid-1"\n'
        "shared_at: 2026-01-01T04:00:00-08:00\n"
        f'transcript_sha256: "{_sha(GOLDEN_BODY)}"\n'
        "tags:\n"
        '  - "podcast"\n'
        "---\n" + GOLDEN_BODY
    )
    assert rendered.note.data == expected.encode("utf-8")
    assert rendered.title == "Épisode 1: «Tests»"
    assert rendered.guid == "feed:urn:example:guid-1"
    assert rendered.transcript_sha256 == _sha(GOLDEN_BODY)


def test_audio_url_note_exact() -> None:
    job, episode = make_audio_job(source="url", title="Board meeting")
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS[:2], episode=episode), first_shared_at=T0
    )
    body = (
        "# Board meeting\n\n## Transcript\n\n"
        "[00:00:00] Welcome to Le Podcast ☕. Today: Épisode 1: «Tests».\n"
    )
    assert rendered.note.filename == "2026-01-01-board-meeting.md"
    assert rendered.note.data.decode() == (
        "---\n"
        'type: "audio"\n'
        "contract: 1\n"
        'source: "earmark"\n'
        'origin: "url"\n'
        'title: "Board meeting"\n'
        "date: 2026-01-01\n"
        'duration: "00:00:10"\n'
        'language: "en"\n'
        f'source_url: "{DIRECT_URL}"\n'
        f'guid: "sha256:{SHA}"\n'
        "shared_at: 2026-01-01T04:00:00-08:00\n"
        f'transcript_sha256: "{_sha(body)}"\n'
        "tags:\n"
        '  - "audio"\n'
        "---\n" + body
    )


def test_upload_note_exact() -> None:
    job, episode = make_audio_job(source="upload")  # no title: the file name stem
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS[:1], episode=episode), first_shared_at=T0
    )
    body = "# clip\n\n## Transcript\n\n[00:00:00] Welcome to Le Podcast ☕.\n"
    assert rendered.note.filename == "2026-01-01-clip.md"
    assert rendered.note.data.decode() == (
        "---\n"
        'type: "audio"\n'
        "contract: 1\n"
        'source: "earmark"\n'
        'origin: "upload"\n'
        'title: "clip"\n'
        "date: 2026-01-01\n"
        'duration: "00:00:05"\n'
        'language: "en"\n'
        f'guid: "sha256:{SHA}"\n'
        "shared_at: 2026-01-01T04:00:00-08:00\n"
        f'transcript_sha256: "{_sha(body)}"\n'
        "tags:\n"
        '  - "audio"\n'
        "---\n" + body
    )


def test_instructions_file_exact() -> None:
    job = make_delivery_job(instructions="Which papers?")
    episode = make_episode(feed_guid="urn:example:guid-1")
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    )
    assert rendered.instructions.filename == "2026-01-02-le-podcast-episode-1-tests.instructions.md"
    assert rendered.instructions.data.decode() == (
        "---\n"
        'type: "earmark-instructions"\n'
        "contract: 1\n"
        'for: "2026-01-02-le-podcast-episode-1-tests.md"\n'
        'guid: "feed:urn:example:guid-1"\n'
        f'job_id: "{DELIVERY_JOB_ID}"\n'
        "shared_at: 2026-01-01T04:00:00-08:00\n"
        "---\n"
        "Which papers?"
    )


def test_instructions_body_verbatim_lf() -> None:
    episode = make_episode()
    transcript = make_transcript_with(SEGMENTS, episode=episode)
    text = "line one\r\nline two\rline three\n\n  indented, with: yaml-ish --- text  "
    job = make_delivery_job(instructions=text)
    data = render_note(job, episode, transcript, first_shared_at=T0).instructions.data
    assert b"\r" not in data
    assert data.endswith(
        b"---\nline one\nline two\nline three\n\n  indented, with: yaml-ish --- text  "
    )
    empty = render_note(make_delivery_job(), episode, transcript, first_shared_at=T0)
    assert empty.instructions.data.endswith(b"shared_at: 2026-01-01T04:00:00-08:00\n---\n")
    assert empty.instructions.data.count(b"---\n") == 2


# -- frontmatter -------------------------------------------------------------------------

HOSTILE = list(
    dict.fromkeys(
        [
            "line\r\nBcc: evil@example.org",
            "line\nsecond: line",
            "---",
            'say "hi"',
            "back\\slash",
            "a: b",
            "# not a comment",
            "yes",
            "null",
            "2026-01-01",
            "=?utf-8?q?x?=",
            "evil \u202e txt.exe",
            "bom\ufeffhere",
            "lone \ud800 surrogate",
            "nul\x00byte",
            "x" * 500,
            *[c for c in YAML_CASES if c],
        ]
    )
)


@pytest.mark.parametrize("hostile", HOSTILE, ids=[f"case{i}" for i in range(len(HOSTILE))])
def test_frontmatter_parses_and_round_trips_hostile_titles(hostile: str) -> None:
    episode = make_episode(title=hostile, show_title=hostile)
    note = render_note(
        make_delivery_job(),
        episode,
        make_transcript_with(SEGMENTS, episode=episode),
        first_shared_at=T0,
    ).note.data
    front = _front(note)
    assert front["title"] == (contract_text(hostile, 200) or "Episode 1000700000001")
    assert front["show"] == (contract_text(hostile, 300) or "Show 1200361736")
    assert note.decode().count("\n---\n") == 1  # only the closing line of the frontmatter
    assert "\r" not in note.decode()
    # An audio note's title goes through the same cleaning.
    job, audio = make_audio_job(source="url", title=hostile)
    audio_note = render_note(
        job, audio, make_transcript_with(SEGMENTS, episode=audio), first_shared_at=T0
    ).note.data
    assert _front(audio_note)["title"] == (contract_text(audio.title, 200) or "Uploaded audio")


def test_unknown_values_are_omitted() -> None:
    upload_job, upload = make_audio_job(source="upload")
    for language in (None, "english"):
        note = render_note(
            upload_job,
            upload,
            make_transcript_with(SEGMENTS, episode=upload, language=language),
            first_shared_at=T0,
        ).note.data
        front = _front(note)
        for absent in ("show", "published", "apple_url", "source_url", "language"):
            assert absent not in front
    episode = make_episode(published_at=None)
    note = render_note(
        make_delivery_job(),
        episode,
        make_transcript_with(SEGMENTS, episode=episode, language=None),
        first_shared_at=T0,
    ).note.data
    front = _front(note)
    assert "published" not in front
    assert "language" not in front
    assert front["date"] == date(2026, 1, 1)
    for raw in (note.decode(),):
        assert "null" not in raw.split("\n---\n")[0]
        assert '""' not in raw.split("\n---\n")[0]
        assert "[]" not in raw.split("\n---\n")[0]
    assert all(v not in (None, "", []) for v in front.values())


def test_key_order_and_scalar_types() -> None:
    note, _ = _podcast(feed_guid="g1")
    front = _front(note)
    assert list(front) == [
        "type", "contract", "source", "origin", "title", "show", "date", "published",
        "duration", "language", "apple_url", "guid", "shared_at", "transcript_sha256", "tags",
    ]  # fmt: skip
    assert front["contract"] == 1
    assert type(front["contract"]) is int
    assert type(front["date"]) is date
    assert type(front["published"]) is date
    shared = front["shared_at"]
    assert isinstance(shared, datetime)
    assert shared.utcoffset() == timedelta(hours=-8)
    assert isinstance(front["duration"], str)
    assert front["tags"] == ["podcast"]


def test_strings_capped() -> None:
    episode = make_episode(title="t" * 500, show_title="s" * 500)
    front = _front(
        render_note(
            make_delivery_job(),
            episode,
            make_transcript_with(SEGMENTS, episode=episode),
            first_shared_at=T0,
        ).note.data
    )
    assert front["title"] == "t" * 199 + "…"
    assert front["show"] == "s" * 299 + "…"


@pytest.mark.parametrize(
    ("end", "expected"),
    [(0.0, "00:00:00"), (59.9, "00:00:59"), (3725.9, "01:02:05"), (36000.0, "10:00:00")],
)
def test_duration_hms(end: float, expected: str) -> None:
    episode = make_episode()
    transcript = make_transcript_with([Segment(start=0.0, end=end, text="x")], episode=episode)
    front = _front(
        render_note(make_delivery_job(), episode, transcript, first_shared_at=T0).note.data
    )
    assert front["duration"] == expected


def test_tags() -> None:
    assert _front(_podcast()[0])["tags"] == ["podcast"]
    job, episode = make_audio_job(source="url")
    note = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    ).note.data
    assert _front(note)["tags"] == ["audio"]


@pytest.mark.parametrize(
    ("language", "kept"),
    [
        ("en", True),
        ("haw", True),
        ("zh-Hans", True),
        ("pt-BR", True),
        ("zh-Hant-TW", True),
        ("english", False),
        ("EN", False),
        ("e", False),
        ("en_US", False),
        ("en-", False),
        ("en-a", False),
        ("en\nBcc: x", False),
    ],
)
def test_language_bcp47_or_omitted(language: str, kept: bool) -> None:
    episode = make_episode()
    transcript = make_transcript_with(SEGMENTS, episode=episode, language=language)
    front = _front(
        render_note(make_delivery_job(), episode, transcript, first_shared_at=T0).note.data
    )
    assert (front.get("language") == language) is kept
    assert ("language" in front) is kept


def test_shared_at_is_rfc3339_with_requester_offset() -> None:
    episode = make_episode()
    transcript = make_transcript_with(SEGMENTS, episode=episode)
    first = datetime(2026, 1, 1, 12, 0, 7, 999999, tzinfo=UTC)
    for minutes, text in ((-480, "2026-01-01T04:00:07-08:00"), (330, "2026-01-01T17:30:07+05:30")):
        job = make_delivery_job(utc_offset_min=minutes)
        note = render_note(job, episode, transcript, first_shared_at=first).note.data
        assert f"\nshared_at: {text}\n" in note.decode()
        instr = render_note(job, episode, transcript, first_shared_at=first).instructions.data
        assert f"\nshared_at: {job.shared_at.astimezone(requester_tz(job)).isoformat()}\n" in (
            instr.decode()
        )


def test_shared_at_utc_without_offset() -> None:
    episode = make_episode()
    job = make_delivery_job(utc_offset_min=None)
    assert requester_tz(job) is UTC
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    )
    assert "\nshared_at: 2026-01-01T12:00:00+00:00\n" in rendered.note.data.decode()
    assert "\nshared_at: 2026-01-01T12:00:00+00:00\n" in rendered.instructions.data.decode()


def test_note_shared_at_is_first_share_and_instructions_is_this_request() -> None:
    episode = make_episode()
    first = datetime(2025, 12, 30, 20, 0, tzinfo=UTC)
    rendered = render_note(
        make_delivery_job(),
        episode,
        make_transcript_with(SEGMENTS, episode=episode),
        first_shared_at=first,
    )
    assert "\nshared_at: 2025-12-30T12:00:00-08:00\n" in rendered.note.data.decode()
    assert "\nshared_at: 2026-01-01T04:00:00-08:00\n" in rendered.instructions.data.decode()


# -- file name date and slug -------------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "date_only", "offset", "first_shared", "expected"),
    [
        # 01:30Z is still the 1st at -08:00 (across midnight).
        (datetime(2026, 1, 2, 1, 30, tzinfo=UTC), False, -480, T0, "2026-01-01"),
        (datetime(2026, 1, 2, 1, 30, tzinfo=UTC), False, 0, T0, "2026-01-02"),
        (datetime(2026, 1, 2, 23, 30, tzinfo=UTC), False, 330, T0, "2026-01-03"),
        # A date-only publish time keeps its calendar date in any offset.
        (datetime(2026, 1, 2, 0, 0, tzinfo=UTC), True, -480, T0, "2026-01-02"),
        (datetime(2026, 1, 2, 0, 0, tzinfo=UTC), True, 600, T0, "2026-01-02"),
        # Unknown: the first-share date in the offset.
        (None, False, -480, datetime(2026, 1, 2, 3, 0, tzinfo=UTC), "2026-01-01"),
        (None, False, 330, datetime(2026, 1, 2, 3, 0, tzinfo=UTC), "2026-01-02"),
        (None, False, None, datetime(2026, 1, 2, 3, 0, tzinfo=UTC), "2026-01-02"),
    ],
)
def test_filename_date_rules(
    published: datetime | None,
    date_only: bool,
    offset: int | None,
    first_shared: datetime,
    expected: str,
) -> None:
    episode = make_episode(published_at=published, published_date_only=date_only)
    rendered = render_note(
        make_delivery_job(utc_offset_min=offset),
        episode,
        make_transcript_with(SEGMENTS, episode=episode),
        first_shared_at=first_shared,
    )
    assert rendered.note.filename.startswith(expected + "-")
    front = _front(rendered.note.data)
    assert front["date"] == date.fromisoformat(expected)
    assert ("published" in front) is (published is not None)
    if published is not None:
        assert front["published"] == date.fromisoformat(expected)


def test_slugify() -> None:
    assert slugify("Le Podcast ☕") == "le-podcast"
    assert slugify("Épisode 1: «Tests»") == "episode-1-tests"
    assert slugify("  --Hello,   World!!  ") == "hello-world"
    assert slugify("\uff26\uff35\uff2c\uff2c width") == "full-width"
    assert slugify("日本語") == ""
    assert slugify("") == ""


def _slug_of(note_name: str) -> str:
    match = re.fullmatch(r"\d{4}-\d\d-\d\d-(.+)\.md", note_name)
    assert match
    return match[1]


def test_slug_rules() -> None:
    def name(show: str, title: str, **kw: object) -> str:
        episode = make_episode(show_title=show, title=title, **kw)  # type: ignore[arg-type]
        return render_note(
            make_delivery_job(),
            episode,
            make_transcript_with(SEGMENTS, episode=episode),
            first_shared_at=T0,
        ).note.filename

    assert _slug_of(name("Le Podcast ☕", "Épisode 1: «Tests»")) == "le-podcast-episode-1-tests"
    # Cut as a whole to 80 characters.
    cut = _slug_of(name("a" * 50, "b" * 50))
    assert cut == "a" * 50 + "-" + "b" * 29
    assert len(cut) == MAX_SLUG_CHARS
    # A trailing hyphen after the cut is stripped.
    assert _slug_of(name("a" * 79, "bbb")) == "a" * 79
    # An empty slug falls back.
    assert _slug_of(name("☕", "☕")) == "podcast-1000700000001"
    assert _slug_of(name("☕", "☕", episode_id="")) == "podcast"
    job, episode = make_audio_job(source="upload", title="日本語のタイトル")
    rendered = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    )
    assert _slug_of(rendered.note.filename) == "audio-abababababab"
    assert rendered.instructions.filename == rendered.note.filename.removesuffix(".md") + (
        ".instructions.md"
    )
    for text in (rendered.note.filename, rendered.instructions.filename):
        assert re.fullmatch(r"[0-9a-z.-]+", text)


def test_missing_title_and_show_get_documented_fallbacks() -> None:
    episode = make_episode(title="\u200b", show_title="  ")
    rendered = render_note(
        make_delivery_job(),
        episode,
        make_transcript_with(SEGMENTS, episode=episode),
        first_shared_at=T0,
    )
    front = _front(rendered.note.data)
    assert front["title"] == "Episode 1000700000001"
    assert front["show"] == "Show 1200361736"
    assert rendered.note.filename == "2026-01-02-show-1200361736-episode-1000700000001.md"
    job, audio = make_audio_job(source="url", title=None, url="https://audio.example.net/")
    bare = render_note(
        job,
        audio.model_copy(update={"title": ""}),
        make_transcript_with(SEGMENTS),
        first_shared_at=T0,
    )
    assert _front(bare.note.data)["title"] == "Uploaded audio"


# -- guid --------------------------------------------------------------------------------


def test_guid_feed_verbatim() -> None:
    assert note_guid(make_episode(feed_guid="urn:uuid:1234")) == "feed:urn:uuid:1234"
    assert note_guid(make_episode(feed_guid="a:b c/é")) == "feed:a:b c/é"


def test_guid_apple_when_no_feed_item() -> None:
    assert note_guid(make_episode(feed_guid=None)) == "apple:1000700000001"


@pytest.mark.parametrize(
    "feed_guid",
    ["with\ttab", "ctl\x01char", "zero\u200bwidth", "e\u0301 decomposed", "x" * 296, "a\u2028b"],
)
def test_guid_apple_when_feed_guid_not_verbatim(feed_guid: str) -> None:
    assert note_guid(make_episode(feed_guid=feed_guid)) == "apple:1000700000001"


def test_guid_feed_at_exactly_300_characters() -> None:
    assert note_guid(make_episode(feed_guid="x" * 295)) == "feed:" + "x" * 295
    assert note_guid(make_episode(feed_guid="x" * 296)).startswith("apple:")


def test_guid_sha256_for_audio() -> None:
    for source in ("upload", "url"):
        _, episode = make_audio_job(source=source)
        assert note_guid(episode) == f"sha256:{SHA}"


# -- source_url --------------------------------------------------------------------------


def _url_note(url: str, *, source: str = "url") -> dict[str, object]:
    job, episode = make_audio_job(source=source, url=url)  # type: ignore[arg-type]
    return _front(
        render_note(
            job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
        ).note.data
    )


def test_source_url_without_query() -> None:
    front = _url_note("https://audio.example.net:8443/a/b%20c.mp3")
    assert front["source_url"] == "https://audio.example.net:8443/a/b%20c.mp3"


@pytest.mark.parametrize("url", ["https://audio.example.net/x.mp3?a=1", "https://a.example/x?"])
def test_source_url_omitted_with_query(url: str) -> None:
    assert "source_url" not in _url_note(url)


def test_source_url_omitted_when_too_long() -> None:
    assert "source_url" in _url_note("https://a.example/" + "p" * 270)
    assert "source_url" not in _url_note("https://a.example/" + "p" * 290)


def test_source_url_drops_userinfo() -> None:
    front = _url_note("https://user:secret@audio.example.net/x.mp3")
    assert front["source_url"] == "https://audio.example.net/x.mp3"


def test_source_url_only_for_url_origin() -> None:
    assert "source_url" not in _front(_podcast()[0])
    assert "source_url" not in _url_note("https://audio.example.net/x.mp3", source="upload")
    assert _front(_podcast()[0])["apple_url"] == CANON


def test_apple_url_omitted_when_cleaning_would_change_it() -> None:
    job = make_delivery_job(url=CANON + "#a\tb")
    episode = make_episode()
    note = render_note(
        job, episode, make_transcript_with(SEGMENTS, episode=episode), first_shared_at=T0
    ).note.data
    assert "apple_url" not in _front(note)


# -- paragraphs --------------------------------------------------------------------------


def _segs(*spans: tuple[float, float], speakers: str = "") -> list[Segment]:
    return [
        Segment(
            start=a,
            end=b,
            text=f"s{i}",
            speaker=speakers[i] if i < len(speakers) and speakers[i] != "-" else None,
        )
        for i, (a, b) in enumerate(spans)
    ]


def _starts(paragraphs: list[str]) -> list[str]:
    return [p[1:9] for p in paragraphs]


@pytest.mark.parametrize(
    ("spans", "starts"),
    [
        # 10-second segments make 30-second paragraphs.
        ([(i * 10.0, i * 10.0 + 10) for i in range(7)], ["00:00:00", "00:00:30", "00:01:00"]),
        # 25-second segments: the second joins (50 s), the third starts a new one.
        ([(0, 25), (25, 50), (50, 75)], ["00:00:00", "00:00:50"]),
        # A segment that would take it past 60 s starts a new paragraph.
        ([(0, 29), (29, 70)], ["00:00:00", "00:00:29"]),
        # Exactly 60 s is still allowed.
        ([(0, 29), (29, 60)], ["00:00:00"]),
        # Gaps count from the paragraph's first start.
        ([(0, 5), (58, 65)], ["00:00:00", "00:00:58"]),
        ([(0, 5), (20, 25)], ["00:00:00"]),
        ([], []),
    ],
)
def test_paragraph_merge_30_to_60(spans: list[tuple[float, float]], starts: list[str]) -> None:
    assert _starts(transcript_paragraphs(_segs(*spans))) == starts


def test_long_segment_is_its_own_paragraph() -> None:
    segments = [
        Segment(start=0, end=5, text="before"),
        Segment(start=5, end=125, text="a very long monologue " * 3),
        Segment(start=125, end=130, text="after"),
    ]
    paragraphs = transcript_paragraphs(segments)
    assert len(paragraphs) == 3
    assert paragraphs[1] == "[00:00:05] " + " ".join(("a very long monologue " * 3).split())


def test_speaker_change_starts_paragraph() -> None:
    paragraphs = transcript_paragraphs(_segs((0, 5), (5, 10), (10, 15), (15, 20), speakers="AABB"))
    assert paragraphs == ["[00:00:00] s0 s1", "[00:00:10] s2 s3"]
    assert len(transcript_paragraphs(_segs((0, 5), (5, 10), speakers="A-"))) == 2


def test_paragraphs_cover_every_segment() -> None:
    texts = [
        "Hello,\tworld",
        "  spaced \n out ",
        "",
        "ctl\x01removed",
        "nbsp\u00a0here",
        "é",
        "x\ud800y",
    ]
    segments = [Segment(start=i * 7.0, end=i * 7.0 + 7, text=t) for i, t in enumerate(texts)]
    paragraphs = transcript_paragraphs(segments)
    joined = " ".join(p.split("] ", 1)[1] for p in paragraphs)
    assert joined == "Hello, world spaced out ctlremoved nbsp here é x\ufffdy"
    markers = [p[:10] for p in paragraphs]
    assert markers[0] == "[00:00:00]"
    for marker in markers:
        assert re.fullmatch(r"\[\d\d:\d\d:\d\d\]", marker)
    # Every non-empty segment's text is present; nothing is cut.
    big = [Segment(start=float(i), end=i + 1.0, text=f"word{i}") for i in range(5000)]
    out = " ".join(p.split("] ", 1)[1] for p in transcript_paragraphs(big))
    assert out == " ".join(f"word{i}" for i in range(5000))


def test_ten_hour_transcript_not_truncated() -> None:
    segments = [
        Segment(start=0.0, end=5.0, text="start"),
        Segment(start=36000.0, end=36005.0, text="ten hours in"),
    ]
    paragraphs = transcript_paragraphs(segments)
    assert paragraphs == ["[00:00:00] start", "[10:00:00] ten hours in"]
    episode = make_episode()
    note = render_note(
        make_delivery_job(),
        episode,
        make_transcript_with(segments, episode=episode),
        first_shared_at=T0,
    ).note.data.decode()
    assert "[10:00:00] ten hours in\n" in note
    assert 'duration: "10:00:05"' in note


def test_empty_transcript_has_a_valid_note() -> None:
    episode = make_episode()
    note = render_note(
        make_delivery_job(),
        episode,
        make_transcript_with([], episode=episode),
        first_shared_at=T0,
    ).note.data.decode()
    assert note.endswith("## Transcript\n\n")


def test_transcript_sha256_is_over_the_body_bytes() -> None:
    note, _ = _podcast()
    head, _, body = note.partition(b"\n---\n")
    assert _front(note)["transcript_sha256"] == hashlib.sha256(body).hexdigest()
    assert head.startswith(b"---\n")
