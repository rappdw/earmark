"""Local transcription with faster-whisper (CTranslate2).

faster-whisper ships no type information and is an optional extra, so it is imported
through ``importlib`` inside ``load_runtime`` only, and everything else here is typed
against the small Protocols below. Nothing heavy is imported at module top.
"""

import hashlib
import importlib
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, Self

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.errors import PermanentError, TransientError
from earmark.models import Segment, Transcript, episode_key, format_hms
from earmark.transcribe.base import CachedTranscriber, UnusableAudioError

logger = logging.getLogger(__name__)

EXTRA: Final = "transcribe"
INSTALL_HINT: Final = "install it with `uv sync --extra transcribe`"
SAMPLE_RATE: Final = 16_000
BEAM_SIZE: Final = 5
VAD_FILTER: Final = True
PROGRESS_INTERVAL_S: Final = 60.0
MAX_DETAIL_CHARS: Final = 200
MAX_MODEL_CHARS: Final = 100
HASH_CHUNK: Final = 1024 * 1024
NOT_FOUND_ERRORS: Final = frozenset(
    {"RepositoryNotFoundError", "RevisionNotFoundError", "GatedRepoError", "DisabledRepoError"}
)

type Monotonic = Callable[[], float]


class MissingExtraError(ConfigError):
    """faster-whisper (the `transcribe` extra) is not installed or does not import."""


class SegmentLike(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def text(self) -> str: ...


class InfoLike(Protocol):
    @property
    def language(self) -> str: ...

    @property
    def duration(self) -> float: ...


class WhisperModelLike(Protocol):
    def transcribe(
        self, audio: object, /, *, language: str | None, beam_size: int, vad_filter: bool
    ) -> tuple[Iterable[SegmentLike], InfoLike]: ...


@dataclass(frozen=True, slots=True)
class WhisperOptions:
    model: str
    device: str
    compute_type: str
    language: str | None
    model_dir: Path | None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            model=settings.asr_model,
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
            language=settings.asr_language,
            model_dir=settings.asr_model_dir,
        )


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    samples: object  # a float32 numpy array at SAMPLE_RATE, opaque to earmark
    duration_s: float


@dataclass(frozen=True, slots=True)
class WhisperRuntime:
    load_model: Callable[[WhisperOptions], WhisperModelLike]
    decode_audio: Callable[[Path], DecodedAudio]
    cuda_device_count: Callable[[], int]


def _missing(detail: str) -> MissingExtraError:
    prefix = "EARMARK_ASR_BACKEND: backend 'faster_whisper' needs the optional"
    return MissingExtraError([f"{prefix} '{EXTRA}' extra, {detail}"])


def load_runtime() -> WhisperRuntime:
    """Import faster-whisper and CTranslate2 and wrap them. Raises ``MissingExtraError``."""
    try:
        fw = importlib.import_module("faster_whisper")
        ct2 = importlib.import_module("ctranslate2")
    except ModuleNotFoundError as exc:
        if exc.name in ("faster_whisper", "ctranslate2"):
            raise _missing(f"which is not installed; {INSTALL_HINT}") from None
        raise _missing(
            f"which failed to import ({type(exc).__name__}); reinstall it with "
            f"`uv sync --extra {EXTRA}`"
        ) from None
    except ImportError as exc:
        raise _missing(
            f"which failed to import ({type(exc).__name__}); reinstall it with "
            f"`uv sync --extra {EXTRA}`"
        ) from None

    def load_model(options: WhisperOptions) -> WhisperModelLike:
        model: WhisperModelLike = fw.WhisperModel(
            options.model,
            device=options.device,
            compute_type=options.compute_type,
            download_root=None if options.model_dir is None else str(options.model_dir),
        )
        return model

    def decode_audio(path: Path) -> DecodedAudio:
        samples = fw.decode_audio(str(path), sampling_rate=SAMPLE_RATE)
        return DecodedAudio(samples, duration_s=len(samples) / SAMPLE_RATE)

    def cuda_device_count() -> int:
        return int(ct2.get_cuda_device_count())

    return WhisperRuntime(load_model, decode_audio, cuda_device_count)


def check_device(options: WhisperOptions, runtime: WhisperRuntime) -> None:
    if options.device == "cuda" and runtime.cuda_device_count() == 0:
        raise ConfigError(
            [
                "EARMARK_ASR_DEVICE: 'cuda' was requested but CTranslate2 sees no CUDA device "
                "(the PyPI wheels for linux/aarch64 are CPU-only); use 'auto' or 'cpu', "
                "or EARMARK_ASR_BACKEND=remote"
            ]
        )


def _collapse(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    return " ".join(text.split())[:limit]


def segment_from(raw: SegmentLike, *, previous_start: float, duration_s: float) -> Segment | None:
    """Map a raw segment to a ``Segment`` with start >= previous start, end >= start, in range."""
    text = " ".join(raw.text.split())
    if not text:
        return None
    limit = duration_s if duration_s > 0 else math.inf
    start = raw.start if math.isfinite(raw.start) else previous_start
    start = min(max(start, previous_start, 0.0), limit)
    end = raw.end if math.isfinite(raw.end) else start
    end = min(max(end, start), limit)
    return Segment(start=start, end=end, text=text)


def _decode_detail(exc: Exception) -> str:
    strerror = getattr(exc, "strerror", None)
    if isinstance(strerror, str) and strerror.strip():
        return _collapse(strerror)
    return _collapse(type(exc).__name__)


class FasterWhisperEngine:
    """Loads the model on first use (once per process) and transcribes one file at a time."""

    def __init__(
        self,
        options: WhisperOptions,
        runtime: WhisperRuntime,
        *,
        monotonic: Monotonic = time.monotonic,
        progress_interval_s: float = PROGRESS_INTERVAL_S,
    ) -> None:
        self._options = options
        self._runtime = runtime
        self._monotonic = monotonic
        self._progress_interval_s = progress_interval_s
        self._model: WhisperModelLike | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return f"faster-whisper:{self._options.model}"

    @property
    def options(self) -> WhisperOptions:
        return self._options

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> WhisperModelLike:
        with self._lock:
            if self._model is not None:
                return self._model
            o = self._options
            logger.info(
                "loading Whisper model %r (device %s, compute type %s)",
                o.model,
                o.device,
                o.compute_type,
            )
            started = self._monotonic()
            try:
                model = self._runtime.load_model(o)
            except Exception as exc:
                mapped = _map_load_error(exc, o.model)
                if mapped is None:
                    raise
                raise mapped from exc
            inner = getattr(model, "model", None)
            logger.info(
                "loaded Whisper model %r in %.1f s on %s (%s)",
                o.model,
                self._monotonic() - started,
                getattr(inner, "device", "?"),
                getattr(inner, "compute_type", "?"),
            )
            if o.device == "auto" and self._runtime.cuda_device_count() == 0:
                logger.info("CTranslate2 sees no CUDA device; transcribing on CPU")
            self._model = model
            return model

    def _value_error(self, exc: ValueError) -> PermanentError:
        detail = _collapse(str(exc))
        if self._options.language is not None:
            detail += " (check EARMARK_ASR_LANGUAGE)"
        return PermanentError(f"transcription failed: {detail}")

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript:
        model = self._load()
        try:
            decoded = self._runtime.decode_audio(audio)
        except MemoryError:
            raise
        except Exception as exc:
            raise UnusableAudioError(
                f"could not decode {audio.name}: {_decode_detail(exc)}"
            ) from exc
        if decoded.duration_s <= 0:
            raise UnusableAudioError(f"{audio.name} contains no audio")

        started = self._monotonic()
        segments: list[Segment] = []
        try:
            raw_segments, info = model.transcribe(
                decoded.samples,
                language=self._options.language,
                beam_size=BEAM_SIZE,
                vad_filter=VAD_FILTER,
            )
            info_duration = float(info.duration)
            duration = (
                info_duration
                if math.isfinite(info_duration) and info_duration > 0
                else decoded.duration_s
            )
            logger.info(
                "transcribing %s: %s of audio with %s",
                episode_key,
                format_hms(duration),
                self.name,
            )
            previous = 0.0
            last_log = self._monotonic()
            for raw in raw_segments:
                seg = segment_from(raw, previous_start=previous, duration_s=duration)
                if seg is not None:
                    segments.append(seg)
                    previous = seg.start
                if self._monotonic() - last_log >= self._progress_interval_s:
                    seg_end = raw.end if math.isfinite(raw.end) else previous
                    seg_end = min(max(seg_end, 0.0), duration)
                    logger.info(
                        "transcribing %s: %s of %s (%d%%)",
                        episode_key,
                        format_hms(seg_end),
                        format_hms(duration),
                        min(100, int(100 * seg_end / duration)),
                    )
                    last_log = self._monotonic()
        except ValueError as exc:
            raise self._value_error(exc) from exc
        if not segments:
            raise UnusableAudioError(
                f"no speech found in {audio.name} ({format_hms(duration)} of audio)"
            )
        elapsed = max(self._monotonic() - started, 1e-9)
        language = self._options.language or info.language or None
        logger.info(
            "transcribed %s: %d segments, language %s, %.1f s (%.2fx real time)",
            episode_key,
            len(segments),
            language,
            elapsed,
            duration / elapsed,
        )
        return Transcript(
            episode_key=episode_key,
            language=language,
            duration_s=duration,
            model=self.name,
            segments=tuple(segments),
        )


def _map_load_error(exc: Exception, model: str) -> PermanentError | TransientError | None:
    shown = model[:MAX_MODEL_CHARS]
    if any(cls.__name__ in NOT_FOUND_ERRORS for cls in type(exc).__mro__):
        return PermanentError(
            f"Whisper model {shown!r} was not found on Hugging Face (EARMARK_ASR_MODEL)"
        )
    if isinstance(exc, ValueError):
        first = (str(exc).splitlines() or [""])[0]
        return PermanentError(
            f"cannot load Whisper model {shown!r}: {_collapse(first)} (check EARMARK_ASR_MODEL, "
            "EARMARK_ASR_DEVICE, EARMARK_ASR_COMPUTE_TYPE)"
        )
    if isinstance(exc, OSError):
        return TransientError(
            f"could not download or open Whisper model {shown!r}: {type(exc).__name__}"
        )
    return None


def build_faster_whisper_transcriber(
    settings: Settings, artifacts: ArtifactStore, *, runtime: WhisperRuntime | None = None
) -> CachedTranscriber:
    """Build the transcriber without loading a model. Raises ``ConfigError`` on a bad setup."""
    options = WhisperOptions.from_settings(settings)
    runtime = runtime or load_runtime()
    check_device(options, runtime)
    return CachedTranscriber(
        artifacts, FasterWhisperEngine(options, runtime), keep_audio=settings.keep_audio
    )


def file_key(path: Path) -> str:
    """Episode key for a local file: derived from its content, for ``earmark transcribe``."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(HASH_CHUNK):
            digest.update(chunk)
    return episode_key("local", "file", digest.hexdigest())
