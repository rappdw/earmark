"""Transcription through an operator-run OpenAI-compatible ASR server.

POSTs the episode audio as streamed ``multipart/form-data`` to
``{EARMARK_ASR_BASE_URL}/audio/transcriptions`` with ``response_format=verbose_json`` and
maps the answer to a ``Transcript``. The server URL comes only from operator
configuration, so this client has its own small httpx2 client rather than
``fetch.GuardedFetcher`` and no private-address guard (docs/steps/09-remote-asr.md 2.4).
It never follows redirects, ignores proxy environment variables, keeps no cookies, caps
the response, and sends the API key only as a per-request bearer header.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.endpoint import MAX_DETAIL_CHARS as MAX_DETAIL_CHARS  # re-export
from earmark.endpoint import error_detail as error_detail  # re-export
from earmark.endpoint import host_label
from earmark.errors import PermanentError, TransientError
from earmark.fetch import AUDIO_EXTENSIONS, MIB, USER_AGENT, parse_retry_after
from earmark.models import Segment, Transcript, format_hms, utc_now
from earmark.transcribe.base import (
    CachedTranscriber,
    UnusableAudioError,
    segment_from,
)

logger = logging.getLogger(__name__)

TRANSCRIPTIONS_PATH: Final = "/audio/transcriptions"
RESPONSE_FORMAT: Final = "verbose_json"
CONNECT_TIMEOUT_S: Final = 10.0
MAX_RESPONSE_BYTES: Final = 64 * MIB
MAX_ERROR_BYTES: Final = 64 * 1024
MAX_LANGUAGE_CHARS: Final = 32
NO_TIMESTAMPS_WARNING: Final = (
    "the ASR server returned no segment timestamps; the transcript is one segment"
)
UPLOAD_CONTENT_TYPES: Final[Mapping[str, str]] = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "m4b": "audio/mp4",
    "mp4": "audio/mp4",
    "m4v": "video/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "webm": "audio/webm",
    "mka": "audio/x-matroska",
    "mov": "video/quicktime",
    "aiff": "audio/aiff",
}

type Monotonic = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RemoteOptions:
    base_url: str  # validated, no trailing slash
    model: str
    language: str | None
    api_key: SecretStr | None
    max_upload_bytes: int | None
    timeout_s: float

    @property
    def endpoint(self) -> str:
        return self.base_url + TRANSCRIPTIONS_PATH

    @property
    def host(self) -> str:
        return host_label(self.base_url)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        if settings.asr_base_url is None:
            raise ConfigError(
                [
                    "EARMARK_ASR_BASE_URL: required when EARMARK_ASR_BACKEND=remote (the "
                    "server's OpenAI-compatible base URL, such as http://gpu-box:8000/v1)"
                ]
            )
        cap = settings.asr_max_upload_mb
        return cls(
            base_url=settings.asr_base_url,
            model=settings.asr_model,
            language=settings.asr_language,
            api_key=settings.asr_api_key,
            max_upload_bytes=None if cap is None else cap * MIB,
            timeout_s=settings.asr_timeout_seconds,
        )


# -- response mapping ---------------------------------------------------------------------


class _WireSegment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: float | None = None
    end: float | None = None
    text: str = ""


class _WireTranscription(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    language: str | None = None
    duration: float | None = None
    segments: list[_WireSegment] | None = None


@dataclass(frozen=True, slots=True)
class _Raw:
    start: float
    end: float
    text: str


def _finite_positive(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _normalise_language(setting: str | None, reported: str | None) -> str | None:
    if setting is not None:
        return setting
    if reported is None:
        return None
    value = _collapse(reported).lower()
    if not value or len(value) > MAX_LANGUAGE_CHARS:
        return None
    if not all(c.isalpha() or c in " -" for c in value):
        return None
    return value


def transcript_from_response(
    payload: object,
    *,
    episode_key: str,
    model_name: str,
    language: str | None,
    audio_name: str,
) -> Transcript:
    """Map a ``verbose_json`` body to a ``Transcript``; text without timestamps is one segment."""
    try:
        wire = _WireTranscription.model_validate(payload)
    except ValidationError:
        raise PermanentError(
            f"remote ASR returned a response that is not a transcription ({audio_name})"
        ) from None
    wire_segments = wire.segments or []
    ends = [e for s in wire_segments if (e := _finite_positive(s.end)) is not None]
    duration = _finite_positive(wire.duration) or (max(ends) if ends else 0.0)

    segments: list[Segment] = []
    if ends:  # timestamps are present
        previous = 0.0
        for item in wire_segments:
            raw = _Raw(
                math.nan if item.start is None else item.start,
                math.nan if item.end is None else item.end,
                item.text,
            )
            seg = segment_from(raw, previous_start=previous, duration_s=duration)
            if seg is not None:
                segments.append(seg)
                previous = seg.start
    warnings: tuple[str, ...] = ()
    if not segments:
        text = _collapse(wire.text) or _collapse(" ".join(s.text for s in wire_segments))
        if not text:
            raise UnusableAudioError(f"no speech found in {audio_name} (remote ASR)")
        segments = [Segment(start=0.0, end=duration, text=text)]
        warnings = (NO_TIMESTAMPS_WARNING,)
    return Transcript(
        episode_key=episode_key,
        language=_normalise_language(language, wire.language),
        duration_s=duration,
        model=model_name,
        segments=tuple(segments),
        warnings=warnings,
    )


# -- engine -------------------------------------------------------------------------------


class RemoteAsrEngine:
    """Uploads one file at a time to the remote server; writes and deletes nothing."""

    def __init__(
        self,
        options: RemoteOptions,
        *,
        transport: httpx2.BaseTransport | None = None,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._options = options
        self._transport = transport
        self._monotonic = monotonic
        self._client: httpx2.Client | None = None

    @property
    def name(self) -> str:
        return f"remote:{self._options.model}"

    @property
    def options(self) -> RemoteOptions:
        return self._options

    @property
    def timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(
            self._options.timeout_s, connect=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S
        )

    def _get_client(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(
                transport=self._transport or httpx2.HTTPTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _secrets(self) -> list[str]:
        key = self._options.api_key
        return [key.get_secret_value()] if key is not None else []

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript:
        o = self._options
        size = audio.stat().st_size
        if size == 0:
            raise UnusableAudioError(f"{audio.name} is empty")
        if o.max_upload_bytes is not None and size > o.max_upload_bytes:
            raise PermanentError(
                f"{audio.name} is {size / MIB:.1f} MiB, larger than the "
                f"{o.max_upload_bytes // MIB} MiB remote ASR upload limit "
                "(EARMARK_ASR_MAX_UPLOAD_MB)"
            )
        ext = audio.suffix[1:].lower()
        if ext not in AUDIO_EXTENSIONS:
            ext = "bin"
        filename = f"audio.{ext}"
        content_type = UPLOAD_CONTENT_TYPES.get(ext, "application/octet-stream")
        fields = {
            "model": o.model,
            "response_format": RESPONSE_FORMAT,
            "timestamp_granularities[]": "segment",
        }
        if o.language is not None:
            fields["language"] = o.language
        headers: dict[str, str] = {}
        if o.api_key is not None:
            headers["Authorization"] = f"Bearer {o.api_key.get_secret_value()}"

        logger.info(
            "uploading %s (%.1f MiB) to remote ASR at %s for %s",
            audio.name,
            size / MIB,
            o.host,
            self.name,
        )
        client = self._get_client()
        client.cookies.clear()
        started = self._monotonic()
        try:
            with (
                audio.open("rb") as fh,
                client.stream(
                    "POST",
                    o.endpoint,
                    data=fields,
                    files={"file": (filename, fh, content_type)},
                    headers=headers,
                ) as response,
            ):
                body = self._read_response(response)
        except httpx2.ReadTimeout as exc:
            raise TransientError(
                f"remote ASR at {o.host} did not answer within {o.timeout_s:g} s "
                "(EARMARK_ASR_TIMEOUT_SECONDS)"
            ) from exc
        except httpx2.TimeoutException as exc:
            raise TransientError(f"remote ASR request to {o.host} timed out") from exc
        except (httpx2.TransportError, httpx2.DecodingError) as exc:
            raise TransientError(
                f"remote ASR request to {o.host} failed: {type(exc).__name__}"
            ) from exc
        except httpx2.InvalidURL as exc:
            raise PermanentError("invalid EARMARK_ASR_BASE_URL") from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise PermanentError(
                f"remote ASR at {o.host} returned a response that is not JSON"
            ) from exc
        transcript = transcript_from_response(
            payload,
            episode_key=episode_key,
            model_name=self.name,
            language=o.language,
            audio_name=audio.name,
        )
        elapsed = self._monotonic() - started
        logger.info(
            "remote ASR transcribed %s: %d segments, language %s, %s of audio in %.1f s",
            episode_key,
            len(transcript.segments),
            transcript.language,
            format_hms(transcript.duration_s),
            elapsed,
        )
        for warning in transcript.warnings:
            logger.warning("remote ASR transcript for %s: %s", episode_key, warning)
        return transcript

    def _read_response(self, response: httpx2.Response) -> bytes:
        status = response.status_code
        if status != 200:
            raise self._status_error(response)
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise PermanentError(
                    f"remote ASR at {self._options.host} returned more than "
                    f"{MAX_RESPONSE_BYTES // MIB} MiB"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _detail(self, response: httpx2.Response) -> str:
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) >= MAX_ERROR_BYTES:
                break
        detail = error_detail(
            bytes(body[:MAX_ERROR_BYTES]), response.headers.get("Content-Type"), self._secrets()
        )
        return "" if detail is None else f": {detail}"

    def _status_error(self, response: httpx2.Response) -> PermanentError | TransientError:
        status = response.status_code
        host = self._options.host
        if 300 <= status < 400:
            return PermanentError(
                f"remote ASR at {host} answered with a redirect (HTTP {status}); "
                "redirects are not followed, check EARMARK_ASR_BASE_URL"
            )
        if not 400 <= status < 600:
            return PermanentError(f"remote ASR at {host} returned unexpected HTTP {status}")
        detail = self._detail(response)
        if status in (408, 429) or status >= 500:
            return TransientError(
                f"remote ASR at {host} failed: HTTP {status}{detail}",
                retry_after_s=parse_retry_after(response.headers.get("Retry-After"), utc_now()),
            )
        if status in (401, 403):
            return PermanentError(
                f"remote ASR at {host} refused the request: HTTP {status} "
                f"(check EARMARK_ASR_API_KEY){detail}"
            )
        if status == 404:
            return PermanentError(
                f"remote ASR at {host}: HTTP 404 (check EARMARK_ASR_BASE_URL, which should end "
                f"in /v1, and EARMARK_ASR_MODEL){detail}"
            )
        if status == 413:
            return PermanentError(
                f"remote ASR at {host}: HTTP 413, the upload is too large for the server "
                "(raise its limit, or set EARMARK_ASR_MAX_UPLOAD_MB to fail early)"
                f"{detail}"
            )
        return PermanentError(f"remote ASR at {host} rejected the request: HTTP {status}{detail}")


def build_remote_transcriber(
    settings: Settings,
    artifacts: ArtifactStore,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> CachedTranscriber:
    """Build the transcriber without making a request. Raises ``ConfigError`` without a URL."""
    options = RemoteOptions.from_settings(settings)
    cap = options.max_upload_bytes
    logger.info(
        "remote ASR at %s (model %r, upload limit %s, timeout %g s)",
        options.host,
        options.model,
        "none" if cap is None else f"{cap // MIB} MiB",
        options.timeout_s,
    )
    return CachedTranscriber(
        artifacts, RemoteAsrEngine(options, transport=transport), keep_audio=settings.keep_audio
    )
