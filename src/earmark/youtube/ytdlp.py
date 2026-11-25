"""The ``ytdlp`` YouTube backend: yt-dlp reads a video's metadata and downloads its audio.

yt-dlp is imported only in ``load_runtime``, with ``importlib.import_module``: it is an
optional extra (``youtube``), untyped, and absent from the default environment. The rest of
this module is light, so ``earmark.pipeline.factory`` can import it without the extra.

earmark gives yt-dlp one URL, ``https://www.youtube.com/watch?v=<id>``, built from an id it
validated. Only the ``youtube`` extractor is loaded; there are no cookies, config files,
plugins, playlists, post-processors, proxies or remote components. See ``docs/ARCHITECTURE.md``
"YouTube" for the design and the exception to "all fetches go through fetch.py".
"""

import importlib
import importlib.metadata
import logging
import math
import os
import re
import sysconfig
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeGuard
from urllib.parse import urlsplit

from earmark.config import ConfigError, Settings
from earmark.errors import EarmarkError, PermanentError, TransientError
from earmark.models import Episode, youtube_key
from earmark.resolve.text import clean_text
from earmark.youtube.links import canonical_youtube_url, is_video_id, youtube_episode

logger = logging.getLogger(__name__)

EXTRA: Final = "youtube"
INSTALL_HINT: Final = "install it with `uv sync --extra youtube`"
AUDIO_FORMAT: Final = (
    "bestaudio[protocol=https][vcodec=none][audio_channels<=2][abr<=192]"
    "/bestaudio[protocol=https][vcodec=none]"
)
MEDIA_HOST_SUFFIX: Final = ".googlevideo.com"
SOCKET_TIMEOUT_S: Final = 30
RETRIES: Final = 3
DOWNLOAD_DEADLINE_S: Final = 3600.0
PENDING_MAX_AGE_S: Final = 1800.0
MAX_WARNINGS: Final = 20  # kept per call, each cut to 500 characters
MAX_WARNING_CHARS: Final = 500
YTDLP_LOGGER: Final = "yt_dlp"
MAX_TEXT_CHARS: Final = 500
RATE_LIMIT_RETRY_S: Final = 3600.0
MIB: Final = 1024 * 1024
MAX_TIMESTAMP: Final = 4102444800  # 2100-01-01

# Fixed ASCII sentences: never yt-dlp or YouTube text, never a URL or an id.
REFUSALS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "private": "This YouTube video is private.",
        "members": "This YouTube video is for channel members only.",
        "premium": "This YouTube video is for YouTube Premium members only.",
        "age": "This YouTube video is age-restricted, and earmark does not sign in to YouTube.",
        "sign_in": (
            "This YouTube video needs a signed-in account, and earmark does not sign in to YouTube."
        ),
        "removed": "This YouTube video is unavailable: it was removed, or never existed.",
        "geo": "This YouTube video is not available in this server's country.",
        "live": (
            "This is a live stream. Share it again once it has ended and YouTube has "
            "processed the recording."
        ),
        "upcoming": (
            "This premiere or live stream has not started yet. Share it again once it has aired."
        ),
        "post_live": (
            "This live stream has just ended and YouTube is still processing it. "
            "Share it again later."
        ),
        "bot": (
            "YouTube asked this server to sign in to prove it is not a bot, and earmark "
            "does not sign in. Share it again later."
        ),
        "rate_limited": (
            "YouTube is limiting requests from this server; earmark tries again in an hour."
        ),
        "not_video": "This YouTube link did not lead to a single video.",
        "refused": "YouTube would not play this video.",
        "no_audio": (
            "YouTube offered no audio-only stream for this video. yt-dlp may be out of "
            "date, or its JavaScript runtime missing: run earmark doctor "
            "(docs/deploy.md section 18)."
        ),
        "out_of_date": (
            "yt-dlp could not read this video, which usually means it is out of date: "
            "upgrade it and rebuild the images (docs/deploy.md section 18)."
        ),
        "http_403": (
            "YouTube refused the download (HTTP 403), which usually means yt-dlp is out of "
            "date: upgrade it and rebuild the images (docs/deploy.md section 18)."
        ),
        "off_host": "yt-dlp chose a stream outside YouTube's media hosts; nothing was downloaded.",
        "nothing": (
            "yt-dlp finished without writing the audio file; it may be out of date "
            "(docs/deploy.md section 18)."
        ),
    }
)

# (needles, key), first match wins; the text is lower-cased with curly apostrophes straightened.
_REASONS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("private",), "private"),
    (("members-only", "members only", "join this channel"), "members"),
    (("premium",), "premium"),
    (("confirm your age", "age-restricted", "inappropriate for some users"), "age"),
    (("not a bot", "captcha"), "bot"),
    (("try again later", "rate-limited", "rate limited"), "rate_limited"),
    (("premieres in", "will begin in", "scheduled"), "upcoming"),
    (
        ("not available in your country", "not made this video available in your country"),
        "geo",
    ),
    (
        (
            "recording is not available",
            "removed",
            "unavailable",
            "no longer available",
            "terminated",
            "deleted",
            "does not exist",
        ),
        "removed",
    ),
    (("no suitable extractor",), "not_video"),
    (("sign in",), "sign_in"),
)
_EXT: Final = re.compile(r"[a-z0-9]{1,5}")


class MissingYoutubeExtraError(ConfigError):
    """The ``youtube`` extra, or its Deno runtime, is not installed."""


def _missing(detail: str) -> MissingYoutubeExtraError:
    prefix = "EARMARK_YOUTUBE_BACKEND=ytdlp needs"
    return MissingYoutubeExtraError([f"{prefix} {detail}"])


@dataclass(frozen=True, slots=True)
class YtDlpErrors:
    """The yt-dlp exception classes ``classify_error`` needs."""

    download_error: type[Exception]  # yt_dlp.utils.DownloadError
    extractor_error: type[Exception]  # yt_dlp.utils.ExtractorError
    geo_restricted: type[Exception]  # yt_dlp.utils.GeoRestrictedError
    download_cancelled: type[Exception]  # yt_dlp.utils.DownloadCancelled
    content_too_short: type[Exception]  # yt_dlp.utils.ContentTooShortError
    http_error: type[Exception]  # yt_dlp.networking.exceptions.HTTPError
    transport_error: type[Exception]  # yt_dlp.networking.exceptions.TransportError


class YtDlpSession(Protocol):
    def extract_info(self, url: str, *, download: bool, process: bool, ie_key: str) -> Any: ...

    def process_ie_result(self, info: Any, *, download: bool) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class YtDlpRuntime:
    version: str
    ejs_version: str | None
    deno_version: str | None
    deno_path: Path
    errors: YtDlpErrors
    open_session: Callable[[dict[str, Any]], YtDlpSession]


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def load_runtime() -> YtDlpRuntime:
    """Import yt-dlp and wrap it. Raises ``MissingYoutubeExtraError``."""
    try:
        yt_dlp = importlib.import_module("yt_dlp")
        utils = importlib.import_module("yt_dlp.utils")
        net_errors = importlib.import_module("yt_dlp.networking.exceptions")
        yt_globals = importlib.import_module("yt_dlp.globals")
        yt_version = importlib.import_module("yt_dlp.version")
        importlib.import_module("yt_dlp_ejs")
    except ModuleNotFoundError as exc:
        if exc.name is not None and exc.name.split(".")[0] in ("yt_dlp", "yt_dlp_ejs"):
            raise _missing(f"the '{EXTRA}' extra, which is not installed; {INSTALL_HINT}") from None
        raise _missing(
            f"the '{EXTRA}' extra, which failed to import ({type(exc).__name__}); "
            f"reinstall it with `uv sync --extra {EXTRA}`"
        ) from None
    except ImportError as exc:
        raise _missing(
            f"the '{EXTRA}' extra, which failed to import ({type(exc).__name__}); "
            f"reinstall it with `uv sync --extra {EXTRA}`"
        ) from None

    yt_globals.plugin_dirs.value = []  # no plugins: nothing from disk runs inside yt-dlp

    exe = ".exe" if os.name == "nt" else ""
    deno_path = Path(sysconfig.get_path("scripts")) / ("deno" + exe)
    if not (deno_path.is_file() and os.access(deno_path, os.X_OK)):
        raise _missing(
            f"the Deno runtime that the '{EXTRA}' extra installs into the environment's bin "
            f"directory; reinstall it with `uv sync --extra {EXTRA}`"
        )

    version = getattr(yt_version, "__version__", None)
    errors = YtDlpErrors(
        download_error=utils.DownloadError,
        extractor_error=utils.ExtractorError,
        geo_restricted=utils.GeoRestrictedError,
        download_cancelled=utils.DownloadCancelled,
        content_too_short=utils.ContentTooShortError,
        http_error=net_errors.HTTPError,
        transport_error=net_errors.TransportError,
    )

    def open_session(params: dict[str, Any]) -> YtDlpSession:
        session: YtDlpSession = yt_dlp.YoutubeDL(params)
        return session

    return YtDlpRuntime(
        version=version if isinstance(version, str) else "unknown",
        ejs_version=_package_version("yt-dlp-ejs"),
        deno_version=_package_version("deno"),
        deno_path=deno_path,
        errors=errors,
        open_session=open_session,
    )


class YtDlpLog:
    """yt-dlp's ``logger``: forwards to the ``yt_dlp`` logger and keeps the warning texts."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self._logger = logging.getLogger(YTDLP_LOGGER)

    def debug(self, msg: object) -> None:
        self._logger.debug("%s", msg)

    def info(self, msg: object) -> None:
        self._logger.debug("%s", msg)

    def warning(self, msg: object) -> None:
        text = str(msg)
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(text[:MAX_WARNING_CHARS])
        self._logger.warning("%s", text)

    def error(self, msg: object) -> None:
        self._logger.warning("%s", msg)


def ytdlp_params(
    *,
    deno_path: Path,
    logger: YtDlpLog,
    progress_hook: Callable[[dict[str, Any]], None] | None,
    outtmpl: str | None,
) -> dict[str, Any]:
    """The options for one yt-dlp session: YouTube only, nothing from disk or the environment."""
    params: dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "color": "no_color",
        "logger": logger,
        "allowed_extractors": ["youtube"],
        "noplaylist": True,
        "ignore_no_formats_error": True,
        "format": AUDIO_FORMAT,
        "postprocessors": [],
        "fixup": "never",
        "cachedir": False,
        "proxy": "",
        "socket_timeout": SOCKET_TIMEOUT_S,
        "retries": RETRIES,
        "js_runtimes": {"deno": {"path": str(deno_path)}},
        "remote_components": [],
        "overwrites": True,
        "continuedl": False,
        "updatetime": False,
        "progress_hooks": [progress_hook] if progress_hook is not None else [],
    }
    if outtmpl is not None:
        params["outtmpl"] = {"default": outtmpl}
    return params


@contextmanager
def deno_environment(deno_dir: Path) -> Iterator[None]:
    """Scope ``DENO_DIR`` (unless the operator set it) and ``DENO_NO_UPDATE_CHECK``."""
    names = ("DENO_DIR", "DENO_NO_UPDATE_CHECK")
    saved = {name: os.environ.get(name) for name in names}
    if saved["DENO_DIR"] is None:
        os.environ["DENO_DIR"] = str(deno_dir)
    os.environ["DENO_NO_UPDATE_CHECK"] = "1"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _published(info: Mapping[str, Any]) -> tuple[datetime | None, bool]:
    stamp = info.get("timestamp")
    if _is_number(stamp) and 0 <= stamp <= MAX_TIMESTAMP:
        try:
            return datetime.fromtimestamp(stamp, UTC), False
        except (OverflowError, OSError, ValueError):
            pass
    day = info.get("upload_date")
    if isinstance(day, str) and len(day) == 8 and day.isascii() and day.isdigit():
        try:
            parsed = date(int(day[:4]), int(day[4:6]), int(day[6:]))
        except ValueError:
            return None, False
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC), True
    return None, False


def episode_from_info(video_id: str, info: Mapping[str, Any]) -> Episode:
    """The Episode of a video from yt-dlp's info. Every field is untrusted and cleaned."""
    title_raw = info.get("title")
    title = clean_text(title_raw, limit=MAX_TEXT_CHARS) if isinstance(title_raw, str) else ""
    channel = ""
    for name in ("channel", "uploader"):
        value = info.get(name)
        if isinstance(value, str):
            channel = clean_text(value, limit=MAX_TEXT_CHARS)
            break
    published_at, date_only = _published(info)
    duration = info.get("duration")
    duration_s = float(duration) if _is_number(duration) and duration >= 0 else None
    return youtube_episode(
        video_id,
        title=title,
        channel=channel,
        published_at=published_at,
        published_date_only=date_only,
        duration_s=duration_s,
    )


def _refusal(key: str) -> EarmarkError:
    if key == "rate_limited":
        return TransientError(REFUSALS[key], retry_after_s=RATE_LIMIT_RETRY_S)
    return PermanentError(REFUSALS[key])


def reason_from_text(text: str) -> str:
    """The key of ``REFUSALS`` that yt-dlp's reason text means."""
    lowered = text.lower().replace("\u2019", "'")
    for needles, key in _REASONS:
        if any(needle in lowered for needle in needles):
            return key
    return "refused"


def check_playable(video_id: str, info: Mapping[str, Any], warnings: Sequence[str]) -> None:
    """Raise the refusal for a video earmark will not take; first match wins."""
    if info.get("_type") not in (None, "video") or info.get("id") != video_id:
        raise _refusal("not_video")
    status = info.get("live_status")
    if status == "is_live":
        raise _refusal("live")
    if status == "is_upcoming":
        raise _refusal("upcoming")
    if status == "post_live":
        raise _refusal("post_live")
    availability = info.get("availability")
    if availability == "private":
        raise _refusal("private")
    if availability == "subscriber_only":
        raise _refusal("members")
    if availability == "premium_only":
        raise _refusal("premium")
    if availability == "needs_auth":
        age = info.get("age_limit")
        adult = isinstance(age, int) and not isinstance(age, bool) and age >= 18
        raise _refusal("age" if adult else "sign_in")
    formats = info.get("formats")
    if not isinstance(formats, list) or not formats:
        raise _refusal(reason_from_text(" ".join(warnings)))


def _too_large(max_bytes: int) -> PermanentError:
    return PermanentError(
        f"the YouTube audio is larger than the EARMARK_MAX_AUDIO_MB limit ({max_bytes // MIB} MiB)"
    )


def _http_status_error(status: object) -> EarmarkError:
    if not isinstance(status, int) or isinstance(status, bool):
        return _refusal("out_of_date")
    if status == 403:
        return _refusal("http_403")
    if status in (408, 429) or 500 <= status <= 599:
        return TransientError(f"YouTube answered HTTP {status}; earmark tries again later")
    return PermanentError(f"YouTube answered HTTP {status}")


def _related(inner: BaseException) -> list[BaseException]:
    """``inner`` and what it chains to (cause, __cause__, __context__): breadth first, at most 8."""
    seen: list[BaseException] = []
    queue: deque[BaseException] = deque([inner])
    while queue and len(seen) < 8:
        item = queue.popleft()
        if any(item is other for other in seen):
            continue
        seen.append(item)
        for name in ("cause", "__cause__", "__context__"):
            nxt = getattr(item, name, None)
            if isinstance(nxt, BaseException):
                queue.append(nxt)
    return seen


def classify_error(
    exc: Exception, errors: YtDlpErrors, warnings: Sequence[str]
) -> EarmarkError | None:
    """Map an exception from a yt-dlp call to earmark's error; None to re-raise it unchanged."""
    extra = " ".join(warnings)
    inner: BaseException = exc
    if isinstance(exc, errors.download_error):
        info = getattr(exc, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1 and isinstance(info[1], BaseException):
            inner = info[1]
    chain = _related(inner)
    for item in chain:
        if isinstance(item, errors.http_error):
            return _http_status_error(getattr(item, "status", None))
    for item in chain:
        if isinstance(item, errors.transport_error):
            return TransientError(f"could not reach YouTube ({type(item).__name__})")
    for item in chain:
        if isinstance(item, errors.content_too_short):
            return TransientError("the YouTube download was cut short")
    if isinstance(inner, errors.geo_restricted):
        return _refusal("geo")
    if isinstance(inner, errors.extractor_error):
        if getattr(inner, "expected", False):
            return _refusal(reason_from_text(f"{getattr(inner, 'orig_msg', '')} {extra}"))
        return _refusal("out_of_date")
    if inner is exc and isinstance(exc, errors.download_error):
        return _refusal(reason_from_text(f"{exc} {extra}"))
    if isinstance(inner, OSError):
        return None
    logger.warning("yt-dlp raised %s", type(inner).__name__, exc_info=inner)
    return _refusal("out_of_date")


class _Progress:
    def __init__(self, clock: Callable[[], float], max_bytes: int) -> None:
        self.started = clock()
        self.reason: str | None = None
        self._clock = clock
        self._max_bytes = max_bytes

    def hook_for(self, errors: YtDlpErrors) -> Callable[[dict[str, Any]], None]:
        def hook(d: dict[str, Any]) -> None:
            raw = d.get("downloaded_bytes")
            done = int(raw) if _is_number(raw) else 0
            if done > self._max_bytes:
                self.reason = "too_large"
            elif self._clock() - self.started > DOWNLOAD_DEADLINE_S:
                self.reason = "deadline"
            else:
                return
            raise errors.download_cancelled("earmark stopped the download")

        return hook


def _template(path: Path) -> str:
    return str(path).replace("%", "%%")


class YtDlpYoutubeSource:
    """``YoutubeSource`` on yt-dlp. Not thread-safe: each worker builds its own."""

    def __init__(
        self,
        runtime: YtDlpRuntime,
        *,
        max_bytes: int,
        deno_dir: Path,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self._max_bytes = max_bytes
        self._deno_dir = deno_dir
        self._clock = clock
        self._pending: tuple[str, Mapping[str, Any], float] | None = None

    def _run[T](self, params: dict[str, Any], call: Callable[[YtDlpSession], T]) -> T:
        session = self.runtime.open_session(params)
        try:
            with deno_environment(self._deno_dir):
                return call(session)
        finally:
            session.close()

    def resolve(self, video_id: str, /) -> Episode:
        self._pending = None
        episode, info = self._extract(video_id)
        self._pending = (video_id, info, self._clock())
        return episode

    def _extract(self, video_id: str) -> tuple[Episode, Mapping[str, Any]]:
        if not is_video_id(video_id):
            raise PermanentError("not a YouTube video id")
        log = YtDlpLog()
        params = ytdlp_params(
            deno_path=self.runtime.deno_path, logger=log, progress_hook=None, outtmpl=None
        )
        url = canonical_youtube_url(video_id)
        try:
            info = self._run(
                params,
                lambda s: s.extract_info(url, download=False, process=True, ie_key="Youtube"),
            )
        except Exception as exc:
            mapped = classify_error(exc, self.runtime.errors, log.warnings)
            if mapped is not None:
                raise mapped from exc
            raise
        if not isinstance(info, Mapping):
            raise _refusal("not_video")
        check_playable(video_id, info, log.warnings)
        return episode_from_info(video_id, info), info

    def _take_pending(self, video_id: str) -> Mapping[str, Any]:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending_id, info, at = pending
            if pending_id == video_id and self._clock() - at < PENDING_MAX_AGE_S:
                return info
        return self._extract(video_id)[1]

    def download(self, video_id: str, destination: Callable[[str], Path], /) -> Path:
        info = self._take_pending(video_id)
        format_id = info.get("format_id")
        url = info.get("url")
        ext = info.get("ext")
        if (
            not isinstance(format_id, str)
            or not format_id
            or not isinstance(url, str)
            or not url
            or info.get("vcodec") != "none"
            or info.get("protocol") != "https"
            or not isinstance(ext, str)
            or _EXT.fullmatch(ext) is None
        ):
            raise _refusal("no_audio")
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            raise _refusal("off_host") from None
        if parts.scheme != "https" or not host.endswith(MEDIA_HOST_SUFFIX):
            raise _refusal("off_host")
        for name in ("filesize", "filesize_approx"):
            size = info.get(name)
            if _is_number(size):
                if size > self._max_bytes:
                    raise _too_large(self._max_bytes)
                break

        path = destination(ext)
        progress = _Progress(self._clock, self._max_bytes)
        log = YtDlpLog()
        params = ytdlp_params(
            deno_path=self.runtime.deno_path,
            logger=log,
            progress_hook=progress.hook_for(self.runtime.errors),
            outtmpl=_template(path),
        )
        try:
            self._run(params, lambda s: s.process_ie_result(info, download=True))
        except Exception as exc:
            if progress.reason == "too_large":
                raise _too_large(self._max_bytes) from exc
            if progress.reason == "deadline":
                raise TransientError("the YouTube download took longer than 1 hour") from exc
            mapped = classify_error(exc, self.runtime.errors, log.warnings)
            if mapped is not None:
                raise mapped from exc
            raise
        if not path.is_file():
            raise _refusal("nothing")
        size_on_disk = path.stat().st_size
        if size_on_disk == 0:
            raise TransientError("the YouTube download was empty")
        if size_on_disk > self._max_bytes:
            path.unlink(missing_ok=True)
            raise _too_large(self._max_bytes)
        logger.info(
            "downloaded YouTube audio for %s: %d bytes, format %s",
            youtube_key(video_id),
            size_on_disk,
            format_id,
        )
        return path

    def close(self) -> None:
        self._pending = None


def build_ytdlp_source(settings: Settings) -> YtDlpYoutubeSource:
    """Build the backend. Makes no network request and starts no process."""
    runtime = load_runtime()
    return YtDlpYoutubeSource(
        runtime,
        max_bytes=settings.max_audio_mb * MIB,
        deno_dir=settings.data_dir.resolve() / "cache" / "deno",
    )
