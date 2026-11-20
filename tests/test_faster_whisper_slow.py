"""Real faster-whisper `tiny` on CPU. Opt-in: `pytest -m slow` with the transcribe extra."""

import hashlib
import importlib
import shutil
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from earmark.artifacts import ArtifactStore
from earmark.cli import app
from earmark.config import Settings
from earmark.transcribe.base import CachedTranscriber, UnusableAudioError
from earmark.transcribe.faster_whisper import (
    AudioDecodeError,
    FasterWhisperEngine,
    MissingExtraError,
    WhisperOptions,
    WhisperRuntime,
    build_faster_whisper_transcriber,
    load_runtime,
)
from tests import slow_assets
from tests.factories import make_episode
from tests.whisper_fakes import FakeInfo, FakeRuntime, FakeWhisperModel

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def whisper_runtime() -> WhisperRuntime:
    try:
        return load_runtime()
    except MissingExtraError as exc:
        pytest.fail(f"{exc} (the slow tests need the transcribe extra)")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        asr_model=slow_assets.SLOW_MODEL,
        asr_device="cpu",
        asr_compute_type="int8",
        asr_model_dir=slow_assets.model_dir(),
    )


class _TickWatcher:
    """Records the largest gap between 50 ms ticks of a daemon thread."""

    def __init__(self) -> None:
        self.largest_gap = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(0.05):
            now = time.monotonic()
            self.largest_gap = max(self.largest_gap, now - last)
            last = now

    def __enter__(self) -> "_TickWatcher":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


@pytest.mark.usefixtures("whisper_runtime")
def test_tiny_model_transcribes_clip(tmp_path: Path) -> None:
    clip = slow_assets.fetch_clip()
    settings = _settings(tmp_path)
    artifacts = ArtifactStore(settings.data_dir)
    episode = make_episode()
    audio = artifacts.audio_path(episode.key, "flac")
    audio.parent.mkdir(parents=True)
    shutil.copyfile(clip, audio)

    transcriber = build_faster_whisper_transcriber(settings, artifacts)
    with _TickWatcher() as ticks:
        transcript = transcriber.transcribe(episode, audio)

    assert isinstance(transcriber.engine, FasterWhisperEngine)
    assert transcriber.engine.model_loaded
    text = " ".join(s.text for s in transcript.segments)
    assert text.strip()
    assert "country" in text.lower()
    assert transcript.language == "en"
    assert 10.5 <= transcript.duration_s <= 11.5
    starts = [s.start for s in transcript.segments]
    assert starts == sorted(starts)
    assert all(s.end >= s.start for s in transcript.segments)
    assert artifacts.transcript_json_path(episode.key).is_file()
    assert artifacts.transcript_text_path(episode.key).is_file()
    assert artifacts.find_audio(episode.key) is None
    # CTranslate2 releases the GIL, so the worker's heartbeat thread keeps running.
    assert ticks.largest_gap < 2.0

    broken = FakeRuntime(FakeWhisperModel([], FakeInfo("en", 1.0)))
    broken.load_errors = [RuntimeError("must not load")]
    options = WhisperOptions.from_settings(settings)
    cached = CachedTranscriber(
        artifacts, FasterWhisperEngine(options, broken.runtime()), keep_audio=False
    )
    assert cached.transcribe(episode, audio) == transcript
    assert broken.loads == []


@pytest.mark.usefixtures("whisper_runtime")
def test_cli_transcribe_tiny(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "jfk.flac"
    shutil.copyfile(slow_assets.fetch_clip(), clip)
    before = hashlib.sha256(clip.read_bytes()).hexdigest()
    monkeypatch.setenv("EARMARK_ASR_MODEL", slow_assets.SLOW_MODEL)
    monkeypatch.setenv("EARMARK_ASR_DEVICE", "cpu")
    monkeypatch.setenv("EARMARK_ASR_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("EARMARK_ASR_MODEL_DIR", str(slow_assets.model_dir()))
    result = CliRunner().invoke(app, ["transcribe", str(clip)])
    assert result.exit_code == 0, result.stderr
    first = result.stdout.splitlines()[0]
    assert first.startswith("[00:00:00]")
    assert "country" in first.lower()
    assert hashlib.sha256(clip.read_bytes()).hexdigest() == before


def _write_flac(path: Path) -> None:
    av = importlib.import_module("av")
    np = importlib.import_module("numpy")

    with av.open(str(path), "w") as container:
        stream = container.add_stream("flac", rate=16_000)
        stream.layout = "mono"
        t = np.arange(32_000) / 16_000
        samples = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 16_000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _write_video_only_mp4(path: Path) -> None:
    av = importlib.import_module("av")
    np = importlib.import_module("numpy")

    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = 64
        stream.height = 64
        stream.pix_fmt = "yuv420p"
        for _ in range(5):
            frame = av.VideoFrame.from_ndarray(
                np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24"
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


@pytest.mark.usefixtures("whisper_runtime")
def test_real_decode_classifies_inputs(tmp_path: Path) -> None:
    flac = tmp_path / "ok.flac"
    _write_flac(flac)
    mp4 = tmp_path / "video.mp4"
    _write_video_only_mp4(mp4)
    text = tmp_path / "text.mp3"
    text.write_text("not audio")
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    short = tmp_path / "short.flac"
    short.write_bytes(flac.read_bytes()[:100])

    runtime = load_runtime()
    assert runtime.decode_audio(flac).duration_s > 1.0
    for path, detail in [
        (text, None),
        (empty, "Invalid data found when processing input"),
        (short, "End of file"),
        (mp4, "no audio stream"),
    ]:
        with pytest.raises(AudioDecodeError) as info:
            runtime.decode_audio(path)
        if detail is not None:
            assert info.value.detail == detail
    for bad in (tmp_path / "missing.mp3", tmp_path):
        try:
            runtime.decode_audio(bad)
        except AudioDecodeError:
            pytest.fail(f"{bad.name} must not be classified as bad audio")
        except OSError:
            pass
        else:
            pytest.fail(f"{bad.name} should not decode")

    artifacts = ArtifactStore(tmp_path / "data")
    episode = make_episode()
    audio = artifacts.audio_path(episode.key, "mp3")
    audio.parent.mkdir(parents=True)
    shutil.copyfile(text, audio)
    model = FakeWhisperModel([], FakeInfo("en", 1.0))
    fake_runtime = WhisperRuntime(
        load_model=lambda _o: model,
        decode_audio=runtime.decode_audio,
        cuda_device_count=runtime.cuda_device_count,
    )
    options = WhisperOptions("tiny", "cpu", "int8", None, None)
    cached = CachedTranscriber(
        artifacts, FasterWhisperEngine(options, fake_runtime), keep_audio=False
    )
    with pytest.raises(UnusableAudioError):
        cached.transcribe(episode, audio)
    assert not audio.exists()
