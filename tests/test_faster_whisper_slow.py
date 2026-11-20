"""Real faster-whisper `tiny` on CPU. Opt-in: `pytest -m slow` with the transcribe extra."""

import hashlib
import shutil
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from earmark.artifacts import ArtifactStore
from earmark.cli import app
from earmark.config import Settings
from earmark.transcribe.base import CachedTranscriber
from earmark.transcribe.faster_whisper import (
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
