"""Parse podcast RSS feeds (with defusedxml) and match a feed item to an Apple episode.

This is the only module that parses XML. The stdlib parser is not used for parsing: with
expat 2.9 it still expands internal entities, which is a decompression bomb on a hostile feed.
"""

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Final, Literal
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import defusedxml
from defusedxml.ElementTree import ParseError, fromstring

from earmark.errors import PermanentError
from earmark.resolve.itunes import http_audio_url
from earmark.resolve.text import normalize_title

logger = logging.getLogger(__name__)

ITUNES_NS: Final = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS: Final = "http://purl.org/rss/1.0/modules/content/"
DATE_TOLERANCE: Final = timedelta(seconds=60)  # precise Apple timestamps
DATE_WINDOW: Final = timedelta(days=1)  # Apple gives only a calendar date
DURATION_TOLERANCE_S: Final = 30.0
DURATION_TOLERANCE_RATIO: Final = 0.02

type MatchTier = Literal["guid", "enclosure", "title", "date"]

_DURATION_PART = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class FeedItem:
    index: int  # position in the feed, 0-based
    guid: str | None
    title: str
    published_at: datetime | None
    duration_s: float | None
    enclosure_url: str | None  # first usable enclosure
    description: str  # raw: itunes:summary, else description, else content:encoded


@dataclass(frozen=True, slots=True)
class Feed:
    title: str
    items: tuple[FeedItem, ...]


@dataclass(frozen=True, slots=True)
class Match:
    item: FeedItem
    tier: MatchTier


class FeedFormatError(PermanentError):
    def __init__(self, message: str, *, host: str | None) -> None:
        super().__init__(message)
        self.host = host


def _host_text(host: str | None) -> str:
    return "an unknown host" if not host else host[:100]


# -- element helpers -----------------------------------------------------------------------------


def _split(tag: object) -> tuple[str, str]:
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def _local(element: Element) -> str:
    return _split(element.tag)[1]


def _children(parent: Element, local: str, namespace: str | None = None) -> list[Element]:
    """Children by local name; ``namespace`` is compared case-insensitively, ``""`` is none."""
    found: list[Element] = []
    for child in parent:
        ns, name = _split(child.tag)
        if name != local:
            continue
        if namespace is None or ns.lower() == namespace.lower():
            found.append(child)
    return found


def _text(parent: Element, local: str, namespace: str | None = "") -> str:
    for child in _children(parent, local, namespace):
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


# -- parsing -------------------------------------------------------------------------------------


def parse_pub_date(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
    except (ValueError, TypeError, IndexError):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        return None
    for part in parts[:-1]:
        if _DIGITS.fullmatch(part) is None:
            return None
    if _DURATION_PART.fullmatch(parts[-1]) is None:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds if math.isfinite(seconds) else None


_ENCLOSURE_TYPES: Final = ("audio/", "video/")


def _enclosure_url(item: Element) -> str | None:
    for enclosure in _children(item, "enclosure", ""):
        url = http_audio_url(enclosure.get("url"))
        if url is None:
            continue
        kind = (enclosure.get("type") or "").strip().lower()
        if not kind or kind == "application/octet-stream" or kind.startswith(_ENCLOSURE_TYPES):
            return url
    return None


def _item(element: Element, index: int) -> FeedItem:
    guid = _text(element, "guid")
    title = _text(element, "title") or _text(element, "title", ITUNES_NS)
    description = (
        _text(element, "summary", ITUNES_NS)
        or _text(element, "description")
        or _text(element, "encoded", CONTENT_NS)
    )
    return FeedItem(
        index=index,
        guid=guid or None,
        title=title,
        published_at=parse_pub_date(_text(element, "pubDate")),
        duration_s=parse_duration(_text(element, "duration", ITUNES_NS)),
        enclosure_url=_enclosure_url(element),
        description=description,
    )


def parse_feed(data: bytes, *, host: str | None) -> Feed:
    where = _host_text(host)
    try:
        root = fromstring(data)
    except defusedxml.DefusedXmlException as exc:
        raise FeedFormatError(
            f"The show's feed from {where} declares XML entities, which earmark refuses.",
            host=host,
        ) from exc
    except ParseError as exc:
        raise FeedFormatError(f"The show's feed from {where} is not valid XML.", host=host) from exc
    channels = _children(root, "channel") if _local(root) == "rss" else []
    if not channels:
        raise FeedFormatError(f"The show's feed from {where} is not an RSS feed.", host=host)
    channel = channels[0]
    items = tuple(_item(el, i) for i, el in enumerate(_children(channel, "item")))
    return Feed(title=_text(channel, "title"), items=items)


# -- matching ------------------------------------------------------------------------------------


def _enclosure_key(url: str | None) -> tuple[str, str] | None:
    if url is None:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if not host or parts.path in ("", "/"):
        return None
    return host.lower(), parts.path


def _distance(item: FeedItem, when: datetime | None) -> timedelta | None:
    if when is None or item.published_at is None:
        return None
    return abs(item.published_at - when)


def durations_agree(apple_s: float, item_s: float) -> bool:
    return abs(apple_s - item_s) <= max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_RATIO * apple_s)


def _date_candidates(
    items: Sequence[FeedItem],
    published_at: datetime,
    date_only: bool,
    duration_s: float | None,
) -> list[FeedItem]:
    found: list[FeedItem] = []
    for item in items:
        when = item.published_at
        if when is None:
            continue
        known = duration_s is not None and item.duration_s is not None
        agree = (
            duration_s is not None
            and item.duration_s is not None
            and durations_agree(duration_s, item.duration_s)
        )
        if date_only:
            close = abs(when.astimezone(UTC).date() - published_at.astimezone(UTC).date())
            if close <= DATE_WINDOW and agree:
                found.append(item)
        elif abs(when - published_at) <= DATE_TOLERANCE and (agree or not known):
            found.append(item)
    return found


def match_item(
    items: Sequence[FeedItem],
    *,
    guid: str | None,
    audio_url: str | None,
    title: str,
    published_at: datetime | None,
    date_only: bool = False,
    duration_s: float | None = None,
) -> Match | None:
    """Find the feed item for an Apple episode. Tiers, most to least specific; first hit wins."""
    if guid is not None:
        wanted = guid.strip()
        for item in items:
            if item.guid == wanted:
                return Match(item, "guid")

    key = _enclosure_key(audio_url)
    if key is not None:
        same = [i for i in items if _enclosure_key(i.enclosure_url) == key]
        if len(same) == 1:
            return Match(same[0], "enclosure")

    normalized = normalize_title(title)
    if normalized:
        titled = [i for i in items if normalize_title(i.title) == normalized]
        if len(titled) == 1:
            return Match(titled[0], "title")
        if titled:
            if published_at is None:
                return Match(titled[0], "title")

            def rank(item: FeedItem) -> timedelta:
                gap = _distance(item, published_at)
                return timedelta.max if gap is None else gap

            return Match(min(titled, key=rank), "title")

    if published_at is not None:
        candidates = _date_candidates(items, published_at, date_only, duration_s)
        if len(candidates) == 1:
            return Match(candidates[0], "date")
        if candidates:
            logger.info("episode date matches %d feed items; not guessing", len(candidates))
    return None
