"""YouTube link parsing and episode identity. Pure: no network, no yt-dlp.

Standard library, ``earmark.models`` and the URL errors of ``earmark.resolve.apple`` only.
A video is identified by its 11-character id alone; every link form maps to one canonical
URL, one episode key and one guid.
"""

import urllib.parse
from datetime import datetime
from typing import Final

from earmark.errors import PermanentError
from earmark.models import (
    GUID_YOUTUBE_PREFIX,
    YOUTUBE_ID,
    YOUTUBE_SHOW_ID,
    Episode,
    youtube_key,
)
from earmark.resolve.apple import UnsupportedUrlError

SHORT_HOSTS: Final = frozenset({"youtu.be"})
WATCH_HOSTS: Final = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
EMBED_HOSTS: Final = frozenset({"youtube-nocookie.com", "www.youtube-nocookie.com"})
VIDEO_PATHS: Final = frozenset({"shorts", "live", "embed", "v"})  # /<word>/<id> on WATCH_HOSTS
NOT_IDS: Final = frozenset({"videoseries", "live_stream"})
CANONICAL_PREFIX: Final = "https://www.youtube.com/watch?v="
SHORT_PREFIX: Final = "https://youtu.be/"
MAX_TITLE_CHARS: Final = 500


def is_youtube_host(host: str) -> bool:
    """youtu.be, youtube.com and any ``*.youtube.com``, youtube-nocookie.com and subdomains."""
    host = host.lower().rstrip(".")
    return (
        host in SHORT_HOSTS
        or host in ("youtube.com", "youtube-nocookie.com")
        or host.endswith((".youtube.com", ".youtube-nocookie.com"))
    )


def is_video_id(text: str) -> bool:
    return YOUTUBE_ID.fullmatch(text) is not None


def canonical_youtube_url(video_id: str) -> str:
    return CANONICAL_PREFIX + video_id


def short_youtube_url(video_id: str) -> str:
    return SHORT_PREFIX + video_id


def video_id_from_canonical(url: str) -> str:
    """The id of a URL in exactly the canonical form; ``PermanentError`` otherwise."""
    if url.startswith(CANONICAL_PREFIX):
        candidate = url[len(CANONICAL_PREFIX) :]
        if is_video_id(candidate):
            return candidate
    raise PermanentError("the job's url is not a canonical YouTube link")


def _candidate(host: str, parts: urllib.parse.SplitResult) -> str:
    segments = [s for s in parts.path.split("/") if s]
    first = segments[0].lower() if segments else ""
    if host in SHORT_HOSTS:
        if len(segments) != 1:
            raise UnsupportedUrlError("youtube_bad_id")
        return segments[0]
    if host in WATCH_HOSTS and len(segments) == 1 and first == "watch":
        values = {
            v for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k == "v"
        }
        if len(values) != 1 or "" in values:
            raise UnsupportedUrlError("youtube_bad_id")
        return next(iter(values))
    if (host in WATCH_HOSTS and first in VIDEO_PATHS) or (host in EMBED_HOSTS and first == "embed"):
        if len(segments) == 1:
            raise UnsupportedUrlError("youtube_bad_id")
        if len(segments) > 2:
            raise UnsupportedUrlError("youtube_not_video")
        return segments[1]
    raise UnsupportedUrlError("youtube_not_video")


def video_id_of(parts: urllib.parse.SplitResult) -> str:
    """The video id of a YouTube link. Raises ``UnsupportedUrlError``.

    Query parameters other than ``v`` of a watch link, and the fragment, are dropped.
    """
    host = (parts.hostname or "").rstrip(".")
    candidate = _candidate(host, parts)
    if candidate in NOT_IDS:
        raise UnsupportedUrlError("youtube_not_video")
    if not is_video_id(candidate):
        raise UnsupportedUrlError("youtube_bad_id")
    return candidate


def youtube_episode(
    video_id: str,
    *,
    title: str,
    channel: str,
    published_at: datetime | None,
    published_date_only: bool,
    duration_s: float | None,
) -> Episode:
    """The Episode of a YouTube video. ``ValueError`` when ``video_id`` is not an id."""
    return Episode(
        key=youtube_key(video_id),
        source="youtube",
        show_id=YOUTUBE_SHOW_ID,
        episode_id=video_id,
        guid=GUID_YOUTUBE_PREFIX + video_id,
        title=title,
        show_title=channel,
        published_at=published_at,
        duration_s=duration_s,
        audio_url="",
        feed_url=None,
        url=canonical_youtube_url(video_id),
        description="",
        feed_guid=None,
        published_date_only=published_date_only,
    )
