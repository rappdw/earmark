"""Render the transcript note and the instructions file to the mail contract (``contract: 2``).

Standard library, ``earmark.models``, ``earmark.sanitize`` and the light parts of
``earmark.sources`` only. Everything that came from a feed or a caller is cleaned here
before it reaches YAML, a file name or a heading.
"""

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Final

from earmark.models import (
    AUDIO_SOURCES,
    GUID_SHA256_PREFIX,
    GUID_YOUTUBE_PREFIX,
    Episode,
    Job,
    Segment,
    Transcript,
    format_hms,
    scrub_surrogates,
)
from earmark.sanitize import YamlValue, clean_unicode, emit_frontmatter, fit_chars
from earmark.sources import display_url
from earmark.youtube.links import short_youtube_url

CONTRACT: Final = 2
MAX_TEXT_CHARS: Final = 300  # feed- or caller-supplied strings
MAX_TITLE_CHARS: Final = 200
MAX_SLUG_CHARS: Final = 80
PARAGRAPH_TARGET_S: Final = 30.0
PARAGRAPH_MAX_S: Final = 60.0
MAX_GUID_CHARS: Final = 300

_ZWNJ: Final = "‌"
_ZWJ: Final = "‍"
_DROPPED: Final = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp"})
_LANGUAGE: Final = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*")
_NOT_SLUG: Final = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    data: bytes  # UTF-8, no BOM, LF line endings


@dataclass(frozen=True, slots=True)
class RenderedNote:
    note: Attachment
    instructions: Attachment
    title: str  # the note's `title` value, for the subject
    guid: str
    transcript_sha256: str


def requester_tz(job: Job) -> timezone:
    """The requester's fixed offset, or UTC when the request carried no timestamp."""
    if job.utc_offset_min is None:
        return UTC
    return timezone(timedelta(minutes=job.utc_offset_min))


def contract_text(text: str, max_chars: int) -> str:
    """One line of untrusted text for the contract: cleaned, collapsed, and capped."""
    kept: list[str] = []
    for ch in clean_unicode(text):
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) in _DROPPED and ch not in {_ZWNJ, _ZWJ}:
            continue
        else:
            kept.append(ch)
    collapsed = unicodedata.normalize("NFC", " ".join("".join(kept).split()))
    return fit_chars(collapsed, max_chars)


def slugify(text: str) -> str:
    """Lower-case ASCII ``a-z0-9`` words joined by single hyphens."""
    folded = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    ascii_text = stripped.encode("ascii", "ignore").decode("ascii").lower()
    return _NOT_SLUG.sub("-", ascii_text).strip("-")


def note_guid(episode: Episode) -> str:
    """``sha256:<hex>`` for audio, ``youtube:<id>`` for a video; for a podcast
    ``feed:<guid>`` verbatim, else ``apple:<id>``."""
    if episode.source in AUDIO_SOURCES or episode.source == "youtube":
        return episode.guid
    if episode.feed_guid is not None:
        candidate = "feed:" + episode.feed_guid
        if (
            len(candidate) <= MAX_GUID_CHARS
            and contract_text(candidate, MAX_GUID_CHARS) == candidate
        ):
            return candidate
    return "apple:" + episode.episode_id


def _paragraph_text(text: str) -> str:
    kept = [
        " " if ch.isspace() else ch
        for ch in scrub_surrogates(text)
        if ch.isspace() or unicodedata.category(ch) != "Cc"
    ]
    return " ".join("".join(kept).split())


@dataclass
class _Paragraph:
    start: float
    end: float
    speaker: str | None
    text: str


def transcript_paragraphs(segments: Sequence[Segment]) -> list[str]:
    """``[hh:mm:ss] text`` paragraphs of about 30 to 60 s; every segment's text is kept."""
    paragraphs: list[_Paragraph] = []
    current: _Paragraph | None = None
    for seg in segments:
        text = _paragraph_text(seg.text)
        if not text:
            continue
        if (
            current is None
            or seg.speaker != current.speaker
            or current.end - current.start >= PARAGRAPH_TARGET_S
            or seg.end - current.start > PARAGRAPH_MAX_S
        ):
            current = _Paragraph(seg.start, seg.end, seg.speaker, text)
            paragraphs.append(current)
        else:
            current.text += " " + text
            current.end = max(current.end, seg.end)
    return [f"[{format_hms(p.start)}] {p.text}" for p in paragraphs]


def _publish_date(episode: Episode, tz: timezone) -> date | None:
    published = episode.published_at
    if published is None:
        return None
    if episode.published_date_only:
        return published.date()
    return published.astimezone(tz).date()


def _slug(episode: Episode, show: str | None, title: str) -> str:
    """The slug of the file name; ``show`` is the show of a podcast or the channel of a video."""
    if show is not None:
        slug = "-".join(p for p in (slugify(show), slugify(title)) if p)
    else:
        slug = slugify(title)
    slug = slug[:MAX_SLUG_CHARS].rstrip("-")
    if slug:
        return slug
    if episode.source == "youtube":
        suffix = slugify(episode.episode_id)
        fallback = f"youtube-{suffix}" if suffix else "youtube"
    elif show is not None:
        suffix = slugify(episode.episode_id)
        fallback = f"podcast-{suffix}" if suffix else "podcast"
    else:
        digest = episode.guid.removeprefix(GUID_SHA256_PREFIX)
        fallback = f"audio-{digest[:12]}"
    return fallback[:MAX_SLUG_CHARS].rstrip("-")


def _source_url(job: Job, episode: Episode) -> str | None:
    if episode.source == "youtube":
        return short_youtube_url(episode.episode_id)
    if job.source != "url" or job.url is None or "?" in job.url:
        return None
    value = display_url(job.url)
    if len(value) > MAX_TEXT_CHARS or contract_text(value, MAX_TEXT_CHARS) != value:
        return None
    return value


def _language(transcript: Transcript) -> str | None:
    language = transcript.language
    if language is not None and _LANGUAGE.fullmatch(language):
        return language
    return None


def _file(fields: Sequence[tuple[str, YamlValue]], body: str) -> bytes:
    text = "---\n" + "".join(emit_frontmatter(fields)) + "---\n" + body
    return text.encode("utf-8")


def render_note(
    job: Job, episode: Episode, transcript: Transcript, *, first_shared_at: datetime
) -> RenderedNote:
    """The note and the instructions file for one finished transcription."""
    tz = requester_tz(job)
    podcast = episode.source == "apple_podcasts"
    video = episode.source == "youtube"
    title = contract_text(episode.title, MAX_TITLE_CHARS)
    show: str | None = None  # the podcast's show
    channel: str | None = None
    if video:
        title = title or contract_text(
            f"YouTube video {episode.guid.removeprefix(GUID_YOUTUBE_PREFIX)}", MAX_TITLE_CHARS
        )
        channel = contract_text(episode.show_title, MAX_TEXT_CHARS) or None
    elif podcast:
        title = title or contract_text(f"Episode {episode.episode_id}", MAX_TITLE_CHARS)
        show = contract_text(episode.show_title, MAX_TEXT_CHARS) or contract_text(
            f"Show {episode.show_id}", MAX_TEXT_CHARS
        )
    else:
        title = title or "Uploaded audio"

    published = _publish_date(episode, tz)
    file_date = published or first_shared_at.astimezone(tz).date()
    stem = f"{file_date.isoformat()}-{_slug(episode, show or channel, title)}"
    note_name = f"{stem}.md"
    guid = note_guid(episode)

    paragraphs = transcript_paragraphs(transcript.segments)
    lead = show or channel
    heading = title if lead is None else f"{lead} — {title}"
    body = f"# {heading}\n\n## Transcript\n\n" + "\n\n".join(paragraphs)
    if paragraphs:
        body += "\n"
    sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    apple_url: str | None = None
    if podcast and job.url is not None and contract_text(job.url, MAX_TEXT_CHARS) == job.url:
        apple_url = job.url
    kind = "podcast" if podcast else "video" if video else "audio"
    note = _file(
        [
            ("type", kind),
            ("contract", CONTRACT),
            ("source", "earmark"),
            ("origin", job.source),
            ("title", title),
            ("show", show),
            ("channel", channel),
            ("date", file_date),
            ("published", published),
            ("duration", format_hms(transcript.duration_s)),
            ("language", _language(transcript)),
            ("apple_url", apple_url),
            ("source_url", _source_url(job, episode)),
            ("guid", guid),
            ("shared_at", first_shared_at.astimezone(tz)),
            ("transcript_sha256", sha256),
            ("tags", (kind,)),
        ],
        body,
    )
    instructions_body = job.instructions.replace("\r\n", "\n").replace("\r", "\n")
    instructions = _file(
        [
            ("type", "earmark-instructions"),
            ("contract", CONTRACT),
            ("for", note_name),
            ("guid", guid),
            ("job_id", job.id),
            ("shared_at", job.shared_at.astimezone(tz)),
        ],
        instructions_body,
    )
    return RenderedNote(
        note=Attachment(note_name, note),
        instructions=Attachment(f"{stem}.instructions.md", instructions),
        title=title,
        guid=guid,
        transcript_sha256=sha256,
    )
