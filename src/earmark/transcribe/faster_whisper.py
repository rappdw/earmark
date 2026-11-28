"""Local transcription with faster-whisper (CTranslate2).

faster-whisper ships no type information and is an optional extra, so it is imported
through ``importlib`` inside ``load_runtime`` only, and everything else here is typed
against the small Protocols below. Nothing heavy is imported at module top.
"""

import importlib
import logging
import math
import threading
import time
import types
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, Self, cast

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.errors import PermanentError, TransientError
from earmark.log import adopt_library_loggers
from earmark.models import DecodeOptions, Segment, Transcript, content_key, format_hms
from earmark.sources import sha256_file
from earmark.transcribe.base import (
    CachedTranscriber,
    NoSpeechError,
    UnusableAudioError,
    asr_profile,
    with_decode_options,
)
from earmark.transcribe.base import (
    SegmentLike as SegmentLike,
)
from earmark.transcribe.base import (
    segment_from as segment_from,
)

logger = logging.getLogger(__name__)

EXTRA: Final = "transcribe"
INSTALL_HINT: Final = "install it with `uv sync --extra transcribe`"
SAMPLE_RATE: Final = 16_000
BEAM_SIZE: Final = 5
VAD_FILTER: Final = True
PROGRESS_INTERVAL_S: Final = 60.0
MAX_DETAIL_CHARS: Final = 200
MAX_MODEL_CHARS: Final = 100
NOT_FOUND_ERRORS: Final = frozenset(
    {"RepositoryNotFoundError", "RevisionNotFoundError", "GatedRepoError", "DisabledRepoError"}
)

DECODE_ERROR_NAMES: Final = ("InvalidDataError", "EOFError")  # attributes of av.error
LANGUAGE_ERROR_MARK: Final = "not a valid language code"
NO_AUDIO_STREAM: Final = "no audio stream"
FLOAT16: Final = "float16"

type Monotonic = Callable[[], float]


class MissingExtraError(ConfigError):
    """faster-whisper (the `transcribe` extra) is not installed or does not import."""


class AudioDecodeError(Exception):
    """Raised by ``WhisperRuntime.decode_audio`` when the input itself is not decodable audio."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InfoLike(Protocol):
    @property
    def language(self) -> str: ...

    @property
    def duration(self) -> float: ...


class WhisperModelLike(Protocol):
    def transcribe(
        self,
        audio: object,
        /,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        initial_prompt: str | None,
    ) -> tuple[Iterable[SegmentLike], InfoLike]: ...


class BatchedPipelineLike(Protocol):
    def transcribe(
        self,
        audio: object,
        /,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        initial_prompt: str | None,
        batch_size: int,
    ) -> tuple[Iterable[SegmentLike], InfoLike]: ...


class BatchedWhisper:
    """A WhisperModelLike over faster-whisper's BatchedInferencePipeline, fixed batch size."""

    def __init__(self, pipeline: object, whisper: object, batch_size: int) -> None:
        self._pipeline = cast(BatchedPipelineLike, pipeline)
        self._whisper = whisper
        self._batch_size = batch_size

    @property
    def model(self) -> object:
        """The CTranslate2 model, read by the load log."""
        return getattr(self._whisper, "model", None)

    def transcribe(
        self,
        audio: object,
        /,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        initial_prompt: str | None,
    ) -> tuple[Iterable[SegmentLike], InfoLike]:
        return self._pipeline.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            initial_prompt=initial_prompt,
            batch_size=self._batch_size,
        )


@dataclass(frozen=True, slots=True)
class WhisperOptions:
    model: str
    device: str
    compute_type: str
    language: str | None
    model_dir: Path | None
    batch_size: int = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            model=settings.asr_model,
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
            language=settings.asr_language,
            model_dir=settings.asr_model_dir,
            batch_size=settings.asr_batch_size,
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
    supported_compute_types: Callable[[str], frozenset[str]]


def _missing(detail: str) -> MissingExtraError:
    prefix = "EARMARK_ASR_BACKEND: backend 'faster_whisper' needs the optional"
    return MissingExtraError([f"{prefix} '{EXTRA}' extra, {detail}"])


def load_runtime() -> WhisperRuntime:
    """Import faster-whisper and CTranslate2 and wrap them. Raises ``MissingExtraError``."""
    try:
        fw = importlib.import_module("faster_whisper")
        ct2 = importlib.import_module("ctranslate2")
        av = importlib.import_module("av")
        av_error = importlib.import_module("av.error")
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
    adopt_library_loggers()

    candidates = (getattr(av_error, name, None) for name in DECODE_ERROR_NAMES)
    bad_input: tuple[type[Exception], ...] = tuple(
        c for c in candidates if isinstance(c, type) and issubclass(c, Exception)
    )

    def load_model(options: WhisperOptions) -> WhisperModelLike:
        whisper = fw.WhisperModel(
            options.model,
            device=options.device,
            compute_type=options.compute_type,
            download_root=None if options.model_dir is None else str(options.model_dir),
        )
        if options.batch_size > 0:
            pipeline = fw.BatchedInferencePipeline(model=whisper)
            return BatchedWhisper(pipeline, whisper, options.batch_size)
        return cast(WhisperModelLike, whisper)

    def decode_audio(path: Path) -> DecodedAudio:
        try:
            samples = fw.decode_audio(str(path), sampling_rate=SAMPLE_RATE)
        except bad_input as exc:
            raise AudioDecodeError(_decode_detail(exc)) from exc
        except IndexError as exc:
            if _audio_streams(av, path) == 0:
                raise AudioDecodeError(NO_AUDIO_STREAM) from exc
            raise
        return DecodedAudio(samples, duration_s=len(samples) / SAMPLE_RATE)

    def cuda_device_count() -> int:
        return int(ct2.get_cuda_device_count())

    def supported_compute_types(device: str) -> frozenset[str]:
        return frozenset(str(t) for t in ct2.get_supported_compute_types(device))

    return WhisperRuntime(load_model, decode_audio, cuda_device_count, supported_compute_types)


def _audio_streams(av: types.ModuleType, path: Path) -> int | None:
    """Count the audio streams with a plain ``av.open``; None when the probe itself fails."""
    try:
        with av.open(str(path)) as container:
            return len(container.streams.audio)
    except Exception as exc:
        logger.debug("could not count the audio streams of %s", path.name, exc_info=exc)
        return None


def check_device(options: WhisperOptions, runtime: WhisperRuntime) -> None:
    if options.device == "cuda" and runtime.cuda_device_count() == 0:
        raise ConfigError(
            [
                "EARMARK_ASR_DEVICE: 'cuda' was requested but CTranslate2 sees no CUDA device. "
                "The PyPI wheels for linux/aarch64 are CPU-only: run the GPU image "
                "(Dockerfile.gpu) with GPU access (compose `gpus: all`, or "
                "`docker run --gpus all`), or use 'auto' or 'cpu', or "
                "EARMARK_ASR_BACKEND=remote"
            ]
        )


@dataclass(frozen=True, slots=True)
class CudaReport:
    device_count: int
    compute_types: frozenset[str] | None  # None: no device, or the query raised
    error: str | None  # the exception's type name when the query raised


def cuda_report(runtime: WhisperRuntime) -> CudaReport:
    """The CUDA device count and its compute types. Loads no model, makes no network call."""
    count = runtime.cuda_device_count()
    if count <= 0:
        return CudaReport(count, None, None)
    try:
        types_ = runtime.supported_compute_types("cuda")
    except Exception as exc:  # noqa: BLE001 - any CTranslate2 failure is reported by type name
        return CudaReport(count, None, type(exc).__name__)
    return CudaReport(count, types_, None)


type WhisperCacheStatus = Literal["local", "cached", "missing"]


def model_cache_status(options: WhisperOptions) -> WhisperCacheStatus:
    """Offline: a directory with model.bin, a cached snapshot, or missing.

    ConfigError naming EARMARK_ASR_MODEL for an unknown size or a directory without
    model.bin. Loads no model and makes no network call.
    """
    shown = "".join(c if c.isprintable() else "?" for c in options.model)[:MAX_MODEL_CHARS]
    if Path(options.model).is_dir():
        if (Path(options.model) / "model.bin").is_file():
            return "local"
        raise ConfigError(
            [
                f"EARMARK_ASR_MODEL: the directory '{shown}' has no model.bin "
                "(not a CTranslate2 model)"
            ]
        )
    utils = importlib.import_module("faster_whisper.utils")
    adopt_library_loggers()
    try:
        utils.download_model(
            options.model,
            local_files_only=True,
            cache_dir=None if options.model_dir is None else str(options.model_dir),
        )
    except FileNotFoundError:
        return "missing"
    except ValueError:
        raise ConfigError(
            [
                f"EARMARK_ASR_MODEL: '{shown}' is not a faster-whisper model size "
                "(tiny, base, small, medium, large-v3, …), a Hugging Face repo id, or a directory"
            ]
        ) from None
    return "cached"


def _collapse(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    return " ".join(text.split())[:limit]


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
    def profile(self) -> str:
        return self.profile_for(None)

    def profile_for(self, options: DecodeOptions | None, /) -> str:
        o = self._options
        fields: dict[str, str | int | bool | None] = {
            "model": o.model,
            "device": o.device,
            "compute_type": o.compute_type,
            "language": o.language,
            "beam_size": BEAM_SIZE,
            "vad_filter": VAD_FILTER,
        }
        if o.batch_size > 0:
            fields["batch_size"] = o.batch_size
        return asr_profile("faster_whisper", with_decode_options(fields, options))

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
            if o.batch_size > 0:
                logger.info(
                    "loading Whisper model %r (device %s, compute type %s, batch size %d)",
                    o.model,
                    o.device,
                    o.compute_type,
                    o.batch_size,
                )
            else:
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

    def _value_error(self, exc: ValueError, options: DecodeOptions | None) -> PermanentError:
        detail = _collapse(str(exc))
        if options is not None:
            detail += " (check the request's language)"
        elif self._options.language is not None:
            detail += " (check EARMARK_ASR_LANGUAGE)"
        return PermanentError(f"transcription failed: {detail}")

    def transcribe_file(
        self, audio: Path, episode_key: str, options: DecodeOptions | None = None, /
    ) -> Transcript:
        model = self._load()
        requested = self._options.language if options is None else options.language
        prompt = None if options is None else options.initial_prompt
        try:
            decoded = self._runtime.decode_audio(audio)
        except AudioDecodeError as exc:
            raise UnusableAudioError(
                f"could not decode {audio.name}: {_collapse(exc.detail)}"
            ) from exc
        if decoded.duration_s <= 0:
            raise UnusableAudioError(f"{audio.name} contains no audio")

        started = self._monotonic()
        segments: list[Segment] = []
        try:
            raw_segments, info = model.transcribe(
                decoded.samples,
                language=requested,
                beam_size=BEAM_SIZE,
                vad_filter=VAD_FILTER,
                initial_prompt=prompt,
            )
        except ValueError as exc:
            if LANGUAGE_ERROR_MARK in str(exc):
                raise self._value_error(exc, options) from exc
            raise
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
        if not segments:
            raise NoSpeechError(
                f"no speech found in {audio.name} ({format_hms(duration)} of audio)"
            )
        elapsed = max(self._monotonic() - started, 1e-9)
        language = requested or info.language or None
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
    return content_key(sha256_file(path))
