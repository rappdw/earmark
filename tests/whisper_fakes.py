"""A fake faster-whisper model and runtime, so tests exercise the engine without the extra."""

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

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
        self, audio: object, /, *, language: str | None, beam_size: int, vad_filter: bool
    ) -> tuple[Iterable[FakeSegment], FakeInfo]:
        self.calls.append(
            {
                "audio": audio,
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
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
    """Builds a WhisperRuntime; counts loads and decodes; can fail either."""

    def __init__(
        self,
        model: FakeWhisperModel,
        *,
        duration_s: float = 60.0,
        cuda_devices: int = 0,
    ) -> None:
        self.model = model
        self.duration_s = duration_s
        self.cuda_devices = cuda_devices
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

    def runtime(self) -> WhisperRuntime:
        return WhisperRuntime(
            load_model=self._load_model,
            decode_audio=self._decode_audio,
            cuda_device_count=lambda: self.cuda_devices,
        )
