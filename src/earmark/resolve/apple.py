"""Parse Apple Podcasts episode links into a canonical form. Pure: no network, no DNS."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import parse_qsl, urlsplit

from earmark.errors import PermanentError
from earmark.models import MAX_URL_CHARS, EpisodeRef

APPLE_HOSTS: Final = frozenset({"podcasts.apple.com", "itunes.apple.com"})
DEFAULT_COUNTRY: Final = "us"

UrlProblem = Literal[
    "malformed",
    "unsupported_host",
    "show_link",
    "not_episode",
    "credentials",
    "youtube_not_video",
    "youtube_bad_id",
]

MESSAGES: Final[Mapping[UrlProblem, str]] = MappingProxyType(
    {
        "malformed": "That is not a valid link. Share an episode from Apple Podcasts.",
        "unsupported_host": "Only Apple Podcasts episode links are supported.",
        "show_link": "This links to a whole show. Share a single episode instead.",
        "not_episode": "This Apple link is not a podcast episode.",
        "credentials": "Links with a user name or password are not supported.",
        "youtube_not_video": (
            "This YouTube link is not a single video. "
            "Share a video, not a channel, playlist or search."
        ),
        "youtube_bad_id": "This YouTube link has no valid video id.",
    }
)

_ID_PATH_RE: Final = re.compile(r"id(\d+)", re.IGNORECASE)
_ID_RE: Final = re.compile(r"[0-9]{1,20}")


class UnsupportedUrlError(PermanentError):
    def __init__(self, problem: UrlProblem) -> None:
        super().__init__(MESSAGES[problem])
        self.problem = problem


def canonical_episode_url(country: str, show_id: str, episode_id: str) -> str:
    return f"https://podcasts.apple.com/{country}/podcast/id{show_id}?i={episode_id}"


def _numeric_id(digits: str) -> str:
    """Normalise an id of ASCII digits (no leading zeros, not zero); anything else is malformed."""
    if _ID_RE.fullmatch(digits) is None or int(digits) == 0:
        raise UnsupportedUrlError("malformed")
    return str(int(digits))


def parse_apple_url(url: str, /) -> EpisodeRef:
    text = url.strip()
    if not text or len(text) > MAX_URL_CHARS or any(c <= " " or c == "\x7f" for c in text):
        raise UnsupportedUrlError("malformed")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise UnsupportedUrlError("malformed") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsupportedUrlError("malformed")
    if "@" in parts.netloc or port is not None:
        raise UnsupportedUrlError("malformed")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsupportedUrlError("malformed")
    if host not in APPLE_HOSTS:
        raise UnsupportedUrlError("unsupported_host")

    segments = [s for s in parts.path.split("/") if s]
    country = DEFAULT_COUNTRY
    rest = segments
    if segments and len(segments[0]) == 2 and segments[0].isascii() and segments[0].isalpha():
        country = segments[0].lower()
        rest = segments[1:]
    id_match = _ID_PATH_RE.fullmatch(rest[-1]) if rest else None
    if len(rest) not in (2, 3) or rest[0].lower() != "podcast" or id_match is None:
        raise UnsupportedUrlError("not_episode")
    show_id = _numeric_id(id_match.group(1))

    values = {v for k, v in parse_qsl(parts.query, keep_blank_values=True) if k == "i" and v}
    if not values:
        raise UnsupportedUrlError("show_link")
    if len(values) > 1:
        raise UnsupportedUrlError("malformed")
    episode_id = _numeric_id(next(iter(values)))

    return EpisodeRef(
        source="apple_podcasts",
        show_id=show_id,
        episode_id=episode_id,
        country=country,
        url=canonical_episode_url(country, show_id, episode_id),
    )
