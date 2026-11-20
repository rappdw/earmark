import logging
from pathlib import Path

import pytest

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError, TransientError
from earmark.models import Episode, Segment, Transcript
from earmark.pipeline.stages import Transcriber
from earmark.pipeline.worker import Shutdown
from earmark.transcribe.base import CachedTranscriber, TranscriptionEngine, UnusableAudioError
from tests.factories import make_episode


class RecordingEngine:
    name = "recording-engine"

    def __init__(self, *, error: BaseException | None = None, key: str | None = None) -> None:
        self.error = error
        self.key = key
        self.calls: list[tuple[Path, str]] = []

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript:
        self.calls.append((audio, episode_key))
        if self.error is not None:
            raise self.error
        return Transcript(
            episode_key=self.key or episode_key,
            language="en",
            duration_s=10.0,
            model=self.name,
            segments=(Segment(start=0.0, end=5.0, text="hello"),),
        )


def _setup(tmp_path: Path) -> tuple[ArtifactStore, Episode, Path]:
    artifacts = ArtifactStore(tmp_path / "data")
    episode = make_episode()
    audio = artifacts.audio_path(episode.key, "mp3")
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    return artifacts, episode, audio


def _make(
    artifacts: ArtifactStore, engine: RecordingEngine, *, keep_audio: bool = False
) -> CachedTranscriber:
    return CachedTranscriber(artifacts, engine, keep_audio=keep_audio)


def test_protocols_satisfied(tmp_path: Path) -> None:
    engine: TranscriptionEngine = RecordingEngine()
    t: Transcriber = CachedTranscriber(ArtifactStore(tmp_path), engine, keep_audio=False)
    assert t is not None


def test_cached_transcript_returned_without_engine(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    stored = Transcript(
        episode_key=episode.key,
        language="fr",
        duration_s=3.0,
        model="old",
        segments=(Segment(start=0.0, end=1.0, text="stored"),),
    )
    artifacts.write_transcript(stored)
    engine = RecordingEngine()
    result = _make(artifacts, engine).transcribe(episode, audio)
    assert result == stored
    assert engine.calls == []


def test_transcript_written_and_audio_deleted(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    engine = RecordingEngine()
    result = _make(artifacts, engine).transcribe(episode, audio)
    assert engine.calls == [(audio, episode.key)]
    assert artifacts.read_transcript(episode.key) == result
    assert artifacts.transcript_text_path(episode.key).read_text() == result.to_text()
    assert artifacts.find_audio(episode.key) is None


def test_audio_deleted_after_success(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    _make(artifacts, RecordingEngine()).transcribe(episode, audio)
    assert not audio.exists()


def test_keep_audio_keeps_file(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    t = _make(artifacts, RecordingEngine(), keep_audio=True)
    assert t.keep_audio is True
    t.transcribe(episode, audio)
    assert audio.exists()


@pytest.mark.parametrize("keep", [False, True])
def test_cached_hit_cleans_up_audio(tmp_path: Path, keep: bool) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    artifacts.write_transcript(RecordingEngine().transcribe_file(audio, episode.key))
    _make(artifacts, RecordingEngine(), keep_audio=keep).transcribe(episode, audio)
    assert audio.exists() is keep


@pytest.mark.parametrize(
    "error", [TransientError("t"), PermanentError("p"), Shutdown()], ids=["transient", "perm", "bx"]
)
def test_audio_kept_on_failure(tmp_path: Path, error: BaseException) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    with pytest.raises(type(error)):
        _make(artifacts, RecordingEngine(error=error)).transcribe(episode, audio)
    assert audio.exists()
    assert not artifacts.has_transcript(episode.key)


@pytest.mark.parametrize("keep", [False, True])
def test_unusable_audio_deleted(tmp_path: Path, keep: bool) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    t = _make(artifacts, RecordingEngine(error=UnusableAudioError("bad")), keep_audio=keep)
    with pytest.raises(UnusableAudioError):
        t.transcribe(episode, audio)
    assert audio.exists() is keep
    assert not artifacts.has_transcript(episode.key)


def test_delete_failure_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts, episode, audio = _setup(tmp_path)

    def boom(self: ArtifactStore, key: str) -> bool:
        raise OSError("read-only")

    monkeypatch.setattr(ArtifactStore, "delete_audio", boom)
    with caplog.at_level(logging.INFO, logger="earmark.transcribe.base"):
        result = _make(artifacts, RecordingEngine()).transcribe(episode, audio)
    assert result.episode_key == episode.key
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert episode.key in warnings[0].getMessage()


def test_audio_outside_episode_dir_untouched(tmp_path: Path) -> None:
    artifacts, episode, _ = _setup(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "audio.mp3"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"x")
    _make(artifacts, RecordingEngine()).transcribe(episode, elsewhere)
    assert elsewhere.exists()


def test_missing_audio_is_permanent(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    audio.unlink()
    with pytest.raises(PermanentError, match=r"audio file missing: audio\.mp3"):
        _make(artifacts, RecordingEngine()).transcribe(episode, audio)


def test_engine_returning_other_episode_fails(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    other = make_episode(guid="urn:example:other")
    engine = RecordingEngine(key=other.key)
    with pytest.raises(PermanentError, match="recording-engine returned a transcript"):
        _make(artifacts, engine).transcribe(episode, audio)
    assert not artifacts.has_transcript(episode.key)
    assert not artifacts.has_transcript(other.key)
    assert audio.exists()


def test_write_failure_leaves_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, episode, audio = _setup(tmp_path)

    def boom(path: Path, model: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(ArtifactStore, "_write_json", staticmethod(boom))
    with pytest.raises(OSError, match="disk full"):
        _make(artifacts, RecordingEngine()).transcribe(episode, audio)
    directory = artifacts.episode_dir(episode.key)
    assert not artifacts.transcript_json_path(episode.key).exists()
    assert not [p for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert audio.exists()


def test_transcript_files_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    calls: list[str] = []
    original = ArtifactStore.write_transcript

    def spy(self: ArtifactStore, transcript: Transcript) -> Path:
        calls.append(transcript.episode_key)
        return original(self, transcript)

    monkeypatch.setattr(ArtifactStore, "write_transcript", spy)
    result = _make(artifacts, RecordingEngine(), keep_audio=True).transcribe(episode, audio)
    assert calls == [episode.key]
    text_path = artifacts.transcript_text_path(episode.key)
    json_path = artifacts.transcript_json_path(episode.key)
    assert text_path.read_text() == result.to_text()
    assert Transcript.model_validate_json(json_path.read_text()) == result
    assert text_path.stat().st_mode & 0o777 == 0o644
    assert json_path.stat().st_mode & 0o777 == 0o644
    assert not [p for p in text_path.parent.iterdir() if p.name.endswith(".tmp")]


def test_segment_from_lives_in_base() -> None:
    from earmark.transcribe import base, faster_whisper

    assert faster_whisper.segment_from is base.segment_from
