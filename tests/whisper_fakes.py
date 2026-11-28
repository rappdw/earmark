"""A fake faster-whisper model and runtime, so tests exercise the engine without the extra."""

import builtins
import sys
import types
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from earmark.transcribe.faster_whisper import (
    DecodedAudio,
    WhisperModelLike,
    WhisperOptions,
    WhisperRuntime,
)


@dataclass(frozen=True)
class FakeSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class FakeInfo:
    language: str
    duration: float


class FakeWhisperModel:
    def __init__(
        self,
        segments: Sequence[FakeSegment],
        info: FakeInfo,
        *,
        on_segment: Callable[[int], None] | None = None,
        transcribe_error: Exception | None = None,
    ) -> None:
        self.segments = list(segments)
        self.info = info
        self.on_segment = on_segment
        self.transcribe_error = transcribe_error
        self.calls: list[dict[str, object]] = []

    def transcribe(
        self,
        audio: object,
        /,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        initial_prompt: str | None = None,
    ) -> tuple[Iterable[FakeSegment], FakeInfo]:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
                "initial_prompt": initial_prompt,
            }
        )
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return self._generate(), self.info

    def _generate(self) -> Iterator[FakeSegment]:
        for index, segment in enumerate(self.segments):
            if self.on_segment is not None:
                self.on_segment(index)
            yield segment


class FakeRuntime:
    """Builds a WhisperRuntime; counts loads and decodes; can fail either.

    ``decode_error = AudioDecodeError("...")`` simulates bad input; any other exception
    simulates a library or environment failure.
    """

    def __init__(
        self,
        model: FakeWhisperModel,
        *,
        duration_s: float = 60.0,
        cuda_devices: int = 0,
        cuda_types: frozenset[str] = frozenset(),
        types_error: BaseException | None = None,
    ) -> None:
        self.model = model
        self.duration_s = duration_s
        self.cuda_devices = cuda_devices
        self.cuda_types = cuda_types
        self.types_error = types_error
        self.types_queries: list[str] = []
        self.loads: list[WhisperOptions] = []
        self.decodes: list[Path] = []
        self.load_errors: list[BaseException] = []
        self.decode_error: BaseException | None = None

    def _load_model(self, options: WhisperOptions) -> WhisperModelLike:
        self.loads.append(options)
        if self.load_errors:
            raise self.load_errors.pop(0)
        return self.model

    def _decode_audio(self, path: Path) -> DecodedAudio:
        self.decodes.append(path)
        if self.decode_error is not None:
            raise self.decode_error
        return DecodedAudio(samples=("fake", path.name), duration_s=self.duration_s)

    def _supported_compute_types(self, device: str) -> frozenset[str]:
        self.types_queries.append(device)
        if self.types_error is not None:
            raise self.types_error
        return self.cuda_types

    def runtime(self) -> WhisperRuntime:
        return WhisperRuntime(
            load_model=self._load_model,
            decode_audio=self._decode_audio,
            cuda_device_count=lambda: self.cuda_devices,
            supported_compute_types=self._supported_compute_types,
        )


class _StubFFmpegError(Exception):
    """Mirrors PyAV's ``FFmpegError``: ``strerror`` plus the path in ``str()``."""

    strerror = "Unknown error"
    code = 1

    def __init__(self, path: object = "") -> None:
        super().__init__(f"[Errno {self.code}] {self.strerror}: '{path}'")


class StubInvalidDataError(_StubFFmpegError, ValueError):
    strerror = "Invalid data found when processing input"


class StubEOFError(_StubFFmpegError, EOFError):
    strerror = "End of file"


class StubFileNotFoundError(_StubFFmpegError, builtins.FileNotFoundError):
    strerror = "No such file or directory"


class StubDecoderNotFoundError(_StubFFmpegError, LookupError):
    strerror = "Decoder not found"


class StubPipeline:
    """Stands in for faster_whisper.BatchedInferencePipeline: records, then delegates."""

    def __init__(self, model: object, delegate: FakeWhisperModel) -> None:
        self.model = model
        self.delegate = delegate
        self.calls: list[dict[str, object]] = []

    def transcribe(
        self,
        audio: object,
        /,
        *,
        language: str | None,
        beam_size: int,
        vad_filter: bool,
        initial_prompt: str | None = None,
        batch_size: int,
    ) -> tuple[Iterable[FakeSegment], FakeInfo]:
        self.calls.append(
            {
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
                "initial_prompt": initial_prompt,
                "batch_size": batch_size,
            }
        )
        return self.delegate.transcribe(
            audio,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            initial_prompt=initial_prompt,
        )


class StubExtra:
    """Stub faster_whisper, ctranslate2, av, and av.error modules for the real load_runtime."""

    def __init__(
        self,
        model: FakeWhisperModel,
        *,
        decode: Callable[[str], Sequence[float]],
        audio_streams: int | BaseException = 1,
        cuda_devices: int = 0,
        cuda_types: Iterable[str] = (),
        types_error: BaseException | None = None,
    ) -> None:
        self.model = model
        self.decode = decode
        self.audio_streams = audio_streams
        self.cuda_devices = cuda_devices
        self.cuda_types = list(cuda_types)
        self.types_error = types_error
        self.model_kwargs: list[dict[str, Any]] = []
        self.pipelines: list[StubPipeline] = []
        self.inner_model = types.SimpleNamespace(device="cuda", compute_type="float16")
        self.decode_calls: list[str] = []
        self.open_calls: list[str] = []
        self.errors = types.ModuleType("av.error")
        self.errors.__dict__.update(
            {
                "FFmpegError": _StubFFmpegError,
                "InvalidDataError": StubInvalidDataError,
                "EOFError": StubEOFError,
                "FileNotFoundError": StubFileNotFoundError,
                "DecoderNotFoundError": StubDecoderNotFoundError,
            }
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fw = types.ModuleType("faster_whisper")
        ct2 = types.ModuleType("ctranslate2")
        av = types.ModuleType("av")

        def decode_audio(path: str, sampling_rate: int) -> Sequence[float]:
            self.decode_calls.append(path)
            return self.decode(path)

        def open_(path: str) -> Any:
            self.open_calls.append(path)
            if isinstance(self.audio_streams, BaseException):
                raise self.audio_streams
            container = types.SimpleNamespace(
                streams=types.SimpleNamespace(audio=[object()] * self.audio_streams)
            )

            class Context:
                def __enter__(self) -> Any:
                    return container

                def __exit__(self, *exc: object) -> None:
                    return None

            return Context()

        def whisper_model(*args: Any, **kwargs: Any) -> FakeWhisperModel:
            self.model_kwargs.append({"args": args, **kwargs})
            self.model.model = self.inner_model  # type: ignore[attr-defined]  # test stub attr
            return self.model

        def pipeline(model: object) -> StubPipeline:
            made = StubPipeline(model, self.model)
            self.pipelines.append(made)
            return made

        def supported_compute_types(device: str) -> list[str]:
            if self.types_error is not None:
                raise self.types_error
            return self.cuda_types

        fw.__dict__.update(
            {
                "WhisperModel": whisper_model,
                "BatchedInferencePipeline": pipeline,
                "decode_audio": decode_audio,
            }
        )
        ct2.__dict__["get_cuda_device_count"] = lambda: self.cuda_devices
        ct2.__dict__["get_supported_compute_types"] = supported_compute_types
        av.__dict__.update({"open": open_, "error": self.errors})
        monkeypatch.setitem(sys.modules, "faster_whisper", fw)
        monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
        monkeypatch.setitem(sys.modules, "av", av)
        monkeypatch.setitem(sys.modules, "av.error", self.errors)
