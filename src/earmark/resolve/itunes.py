"""Parse Apple's iTunes Lookup JSON. Pure: no network."""

import json
import math
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urlsplit

from earmark.errors import TransientError
from earmark.models import EpisodeRef

LOOKUP_URL: Final = "https://itunes.apple.com/lookup"
LOOKUP_LIMIT: Final = 200
MAX_GUID_CHARS: Final = 1000
UNREADABLE: Final = "Apple's lookup returned a response earmark cannot read."


@dataclass(frozen=True, slots=True)
class AppleShow:
    show_id: str
    title: str
    feed_url: str | None  # None when missing, blank, or not an absolute http(s) URL


@dataclass(frozen=True, slots=True)
class AppleEpisode:
    episode_id: str
    guid: str | None
    title: str
    published_at: datetime | None
    date_only: bool  # published_at came from a date without a time
    duration_s: float | None
    audio_url: str | None  # a public http(s) audio URL from Apple, if any
    description: str
    subscriber_only: bool  # Apple lists only a subscriber stream


@dataclass(frozen=True, slots=True)
class LookupResult:
    show: AppleShow | None
    episode: AppleEpisode | None
    episode_count: int  # episode records in the result, for logging


class LookupFormatError(TransientError):
    """Apple's lookup answered with something that is not the expected JSON."""


def lookup_params(ref: EpisodeRef) -> dict[str, str]:
    return {
        "id": ref.show_id,
        "entity": "podcastEpisode",
        "limit": str(LOOKUP_LIMIT),
        "country": ref.country,
    }


def id_equals(value: object, expected: str) -> bool:
    """Apple sends ids as JSON integers in some fields and strings in others."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return str(value) == expected
    return isinstance(value, str) and value.strip() == expected


def http_audio_url(value: object) -> str | None:
    """A public http(s) URL that is not an HLS playlist, else ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return None
    if parts.path.lower().endswith(".m3u8"):
        return None
    return text


def valid_guid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_GUID_CHARS:
        return None
    if any(unicodedata.category(c) == "Cc" for c in text):
        return None
    return text


def parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def positive_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _feed_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return None
    return text


def _show(record: dict[str, object], show_id: str) -> AppleShow:
    title = _string(record.get("collectionName")) or _string(record.get("trackName"))
    return AppleShow(show_id=show_id, title=title, feed_url=_feed_url(record.get("feedUrl")))


def _episode(record: dict[str, object], episode_id: str) -> AppleEpisode:
    millis = positive_seconds(record.get("trackTimeMillis"))
    return AppleEpisode(
        episode_id=episode_id,
        guid=valid_guid(record.get("episodeGuid")),
        title=_string(record.get("trackName")),
        published_at=parse_iso_datetime(record.get("releaseDate")),
        date_only=False,
        duration_s=None if millis is None else millis / 1000,
        audio_url=http_audio_url(record.get("episodeUrl")),
        description=_string(record.get("description")) or _string(record.get("shortDescription")),
        subscriber_only=False,
    )


def parse_lookup(data: bytes, show_id: str, episode_id: str) -> LookupResult:
    try:
        parsed = json.loads(data)
    except ValueError:
        raise LookupFormatError(UNREADABLE) from None
    if not isinstance(parsed, dict):
        raise LookupFormatError(UNREADABLE)
    results = parsed.get("results")
    if not isinstance(results, list):
        raise LookupFormatError(UNREADABLE)
    show: AppleShow | None = None
    episode: AppleEpisode | None = None
    count = 0
    for entry in results:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("wrapperType")
        if kind == "podcastEpisode":
            count += 1
            if episode is None and id_equals(entry.get("trackId"), episode_id):
                episode = _episode(entry, episode_id)
        elif (
            kind == "track"
            and show is None
            and id_equals(entry.get("collectionId"), show_id)
            and entry.get("kind", "podcast") == "podcast"
        ):
            show = _show(entry, show_id)
    return LookupResult(show=show, episode=episode, episode_count=count)
