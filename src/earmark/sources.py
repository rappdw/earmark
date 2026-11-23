"""What a request points at: classifying URLs, upload checks, and audio-episode identity.

Light imports only: ``earmark.fetch`` (httpx) is imported inside ``upload_extension``.
Everything that takes untrusted text returns a cleaned value or raises without echoing
the input.
"""

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal
from urllib.parse import unquote, urlsplit

from earmark.endpoint import host_label
from earmark.models import (
    AUDIO_SHOW_ID,
    GUID_SHA256_PREFIX,
    MAX_URL_CHARS,
    Episode,
    Job,
    content_key,
)
from earmark.resolve.apple import APPLE_HOSTS, UnsupportedUrlError, parse_apple_url
from earmark.sanitize import clean_unicode

HASH_CHUNK: Final = 1024 * 1024
MAX_UPLOAD_NAME_CHARS: Final = 255
MAX_AUDIO_TITLE_CHARS: Final = 200
UPLOAD_FALLBACK_TITLE: Final = "Uploaded audio"
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp"})
_JOINERS: Final = frozenset({"‌", "‍"})


@dataclass(frozen=True, slots=True)
class SourceUrl:
    source: Literal["apple_podcasts", "url"]
    url: str  # apple: canonical episode URL; url: the URL without its fragment


def parse_source_url(text: str, /) -> SourceUrl:
    """Classify a request URL. Raises ``UnsupportedUrlError``."""
    text = text.strip()
    if not text or len(text) > MAX_URL_CHARS or any(c <= " " or c == "\x7f" for c in text):
        raise UnsupportedUrlError("malformed")
    if "://" not in text:
        # Only an Apple link may leave out the scheme.
        try:
            host = (urlsplit("https://" + text).hostname or "").rstrip(".")
        except ValueError:
            raise UnsupportedUrlError("malformed") from None
        if host in APPLE_HOSTS:
            return SourceUrl("apple_podcasts", parse_apple_url(text).url)
        raise UnsupportedUrlError("malformed")
    try:
        parts = urlsplit(text)
        _ = parts.port
    except ValueError:
        raise UnsupportedUrlError("malformed") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsupportedUrlError("malformed")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsupportedUrlError("malformed")
    if host in APPLE_HOSTS:
        return SourceUrl("apple_podcasts", parse_apple_url(text).url)
    if host == "apple.com" or host.endswith(".apple.com"):
        raise UnsupportedUrlError("not_episode")
    if "@" in parts.netloc:
        raise UnsupportedUrlError("credentials")
    return SourceUrl("url", parts._replace(fragment="").geturl())


def display_url(url: str, /) -> str:
    """``scheme://host[:port]/path``: no userinfo, query or fragment."""
    parts = urlsplit(url)
    label = host_label(url)
    return f"{parts.scheme.lower()}://{label}{parts.path}"


def url_host(url: str, /) -> str:
    return host_label(url)


def upload_extension(filename: str | None, content_type: str | None) -> str | None:
    """The extension to stage an upload under, or None when it does not look like audio."""
    # earmark.fetch loads httpx, which the worker's import path must not (tests/test_factory.py).
    from earmark.fetch import AUDIO_EXTENSIONS, CONTENT_TYPE_EXTENSIONS, media_type

    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in AUDIO_EXTENSIONS:
            return ext
    kind = media_type(content_type) or ""
    mapped = CONTENT_TYPE_EXTENSIONS.get(kind)
    if mapped is not None:
        return mapped
    if kind.startswith("audio/"):
        return "bin"
    return None


def _clean_text(text: str) -> str:
    """Untrusted text as one line: whitespace becomes a space, invisible characters go."""
    kept = [
        " " if ch.isspace() else ch
        for ch in clean_unicode(text)
        if ch.isspace() or ch in _JOINERS or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    ]
    return " ".join("".join(kept).split())


def clean_upload_name(raw: str | None) -> str | None:
    """The client's file name, without directories or control characters; None if empty."""
    if raw is None:
        return None
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _clean_text(name)[:MAX_UPLOAD_NAME_CHARS].strip()
    return cleaned or None


def audio_title(job: Job) -> str:
    """The title of an upload or URL episode."""
    candidates: list[str] = [job.title or ""]
    if job.source == "upload":
        candidates.append(PurePosixPath(job.upload_name).stem if job.upload_name else "")
    elif job.url:
        segments = [s for s in urlsplit(job.url).path.split("/") if s]
        candidates.append(unquote(segments[-1]) if segments else "")
    for candidate in candidates:
        cleaned = _clean_text(candidate)[:MAX_AUDIO_TITLE_CHARS].strip()
        if cleaned:
            return cleaned
    if job.source == "upload":
        return UPLOAD_FALLBACK_TITLE
    return _clean_text(url_host(job.url or ""))[:MAX_AUDIO_TITLE_CHARS] or UPLOAD_FALLBACK_TITLE


def sha256_file(path: Path, /) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def audio_episode(job: Job, sha256_hex: str) -> Episode:
    """The episode of an upload or direct-URL job, identified by its audio bytes."""
    return Episode(
        key=content_key(sha256_hex),
        source=job.source,
        show_id=AUDIO_SHOW_ID,
        episode_id="",
        guid=GUID_SHA256_PREFIX + sha256_hex,
        title=audio_title(job),
        show_title="",
        published_at=None,
        duration_s=None,
        audio_url="",
        feed_url=None,
        url="",
        description="",
    )
