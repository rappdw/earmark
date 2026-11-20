"""Read an episode from Apple's public episode web page (the out-of-window fallback).

The page embeds the episode as JSON. That structure is undocumented, so this module is
defensive: unreadable or unexpected data means "not identified", never a crash.
Stdlib only (``html.parser``, ``json``); nothing on the page is executed.
"""

import json
import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Final
from urllib.parse import parse_qs, urlsplit

from earmark.errors import PermanentError
from earmark.resolve.itunes import (
    AppleEpisode,
    http_audio_url,
    id_equals,
    parse_iso_datetime,
    positive_seconds,
    valid_guid,
)

logger = logging.getLogger(__name__)

SERVER_DATA_ID: Final = "serialized-server-data"
JSON_LD_ID: Final = "schema:episode"
MAX_NODES: Final = 500_000

_SHOW_ID_IN_PATH = re.compile(r"(?:^|/)id(\d+)(?:/|$)")
_ASCII_DIGITS = re.compile(r"[0-9]{1,20}")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DURATION = re.compile(
    r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?"
)


class WrongShowError(PermanentError):
    def __init__(self, episode_id: str, show_id: str, page_show_id: str) -> None:
        super().__init__(
            f"Apple lists episode {episode_id} under show {page_show_id}, not {show_id}. "
            "Share the episode again from Apple Podcasts."
        )


def _page_show_label(value: str) -> str:
    return value if _ASCII_DIGITS.fullmatch(value) else "another show"


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: dict[str, str] = {}
        self._current: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        script_id = dict(attrs).get("id")
        if script_id in (SERVER_DATA_ID, JSON_LD_ID) and script_id not in self.blocks:
            self._current = script_id
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._current is not None:
            self.blocks[self._current] = "".join(self._parts)
            self._current = None


def extract_page_json(html: str) -> dict[str, object]:
    """Parsed JSON of the ``serialized-server-data`` and ``schema:episode`` script blocks."""
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    found: dict[str, object] = {}
    for script_id, body in collector.blocks.items():
        try:
            found[script_id] = json.loads(body)
        except ValueError:
            logger.debug("episode page block %s is not valid JSON", script_id)
    return found


def _find_offer(data: object, episode_id: str) -> dict[str, object] | None:
    stack: list[object] = [data]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > MAX_NODES:
            logger.debug("episode page data exceeded %d nodes", MAX_NODES)
            return None
        if isinstance(node, dict):
            guid = node.get("guid")
            if id_equals(node.get("contentId"), episode_id) and isinstance(guid, str) and guid:
                return node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return None


def _show_id_of(offer: dict[str, object]) -> str | None:
    show_offer = offer.get("showOffer")
    if isinstance(show_offer, dict):
        adam = show_offer.get("adamId")
        if isinstance(adam, bool):
            adam = None
        if isinstance(adam, int | str) and str(adam).strip():
            return str(adam).strip()
    store_url = offer.get("storeUrl")
    if isinstance(store_url, str):
        try:
            path = urlsplit(store_url).path
        except ValueError:
            return None
        match = _SHOW_ID_IN_PATH.search(path)
        if match:
            return match.group(1)
    return None


def _audio(offer: dict[str, object]) -> tuple[str | None, bool]:
    """The public audio URL and whether Apple lists only a subscriber stream."""
    enclosures = offer.get("mediaEnclosures")
    if isinstance(enclosures, list) and enclosures:
        entries = [e for e in enclosures if isinstance(e, dict)]
        for entry in entries:
            if entry.get("priceType") == "STDQ":
                url = http_audio_url(entry.get("streamUrl"))
                if url is not None:
                    return url, False
        return None, any(e.get("priceType") == "PSUB" for e in entries)
    price = offer.get("priceType")
    if price is None or price == "STDQ":
        return http_audio_url(offer.get("streamUrl")), False
    return None, price == "PSUB"


def episode_from_server_data(data: object, show_id: str, episode_id: str) -> AppleEpisode | None:
    offer = _find_offer(data, episode_id)
    if offer is None:
        return None
    page_show = _show_id_of(offer)
    if page_show is not None and page_show != show_id:
        raise WrongShowError(episode_id, show_id, _page_show_label(page_show))
    audio_url, subscriber_only = _audio(offer)
    title = offer.get("title")
    return AppleEpisode(
        episode_id=episode_id,
        guid=valid_guid(offer.get("guid")),
        title=title.strip() if isinstance(title, str) else "",
        published_at=parse_iso_datetime(offer.get("releaseDate")),
        date_only=False,
        duration_s=positive_seconds(offer.get("duration")),
        audio_url=audio_url,
        description="",
        subscriber_only=subscriber_only,
    )


def parse_iso_duration(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "P" or value.endswith("T"):
        return None
    match = _ISO_DURATION.fullmatch(value)
    if match is None:
        return None
    days, hours, minutes, seconds = (match.group(k) for k in "dhms")
    return (
        int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(minutes or 0) * 60
        + float(seconds or 0)
    )


def episode_from_json_ld(data: object, show_id: str, episode_id: str) -> AppleEpisode | None:
    if not isinstance(data, dict) or data.get("@type") != "PodcastEpisode":
        return None
    url = data.get("url")
    if not isinstance(url, str):
        return None
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    if episode_id not in query.get("i", []):
        return None
    series = data.get("partOfSeries")
    series_url = series.get("url") if isinstance(series, dict) else None
    if isinstance(series_url, str):
        try:
            path = urlsplit(series_url).path
        except ValueError:
            path = ""
        match = _SHOW_ID_IN_PATH.search(path)
        if match and match.group(1) != show_id:
            raise WrongShowError(episode_id, show_id, _page_show_label(match.group(1)))
    name = data.get("name")
    title = name.strip() if isinstance(name, str) else ""
    if not title:
        return None
    published = data.get("datePublished")
    date_only = False
    published_at: datetime | None
    if isinstance(published, str) and _DATE_ONLY.fullmatch(published.strip()):
        date_only = True
        try:
            published_at = datetime.fromisoformat(published.strip()).replace(tzinfo=UTC)
        except ValueError:
            published_at, date_only = None, False
    else:
        published_at = parse_iso_datetime(published)
    description = data.get("description")
    return AppleEpisode(
        episode_id=episode_id,
        guid=None,
        title=title,
        published_at=published_at,
        date_only=date_only,
        duration_s=parse_iso_duration(data.get("duration")),
        audio_url=None,
        description=description.strip() if isinstance(description, str) else "",
        subscriber_only=False,
    )


def parse_episode_page(content: bytes, show_id: str, episode_id: str) -> AppleEpisode | None:
    blocks = extract_page_json(content.decode("utf-8", errors="replace"))
    if SERVER_DATA_ID in blocks:
        episode = episode_from_server_data(blocks[SERVER_DATA_ID], show_id, episode_id)
        if episode is not None:
            return episode
    if JSON_LD_ID in blocks:
        return episode_from_json_ld(blocks[JSON_LD_ID], show_id, episode_id)
    return None
