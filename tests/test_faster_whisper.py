import dataclasses
import importlib
import logging
import math
import random
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings, load_settings
from earmark.errors import PermanentError, TransientError
from earmark.fakes import FakeResolver, make_fake_backends
from earmark.jobs import open_job_store
from earmark.models import Episode, Transcript, is_episode_key
from earmark.pipeline import factory
from earmark.pipeline.stages import Transcriber
from earmark.pipeline.worker import Shutdown
from earmark.transcribe import faster_whisper as fw
from earmark.transcribe.base import (
    CachedTranscriber,
    NoSpeechError,
    TranscriptionEngine,
    UnusableAudioError,
)
from earmark.transcribe.faster_whisper import (
    MAX_DETAIL_CHARS,
    AudioDecodeError,
    DecodedAudio,
    FasterWhisperEngine,
    MissingExtraError,
    WhisperModelLike,
    WhisperOptions,
    build_faster_whisper_transcriber,
    check_device,
    file_key,
    load_runtime,
    segment_from,
)
from tests.factories import TO, make_episode, new_podcast_job
from tests.http_helpers import FakeMonotonic
from tests.pipeline_env import CANON, CountingJobStore, make_env
from tests.whisper_fakes import (
    FakeInfo,
    FakeRuntime,
    FakeSegment,
    FakeWhisperModel,
    StubDecoderNotFoundError,
    StubEOFError,
    StubExtra,
    StubFileNotFoundError,
    StubInvalidDataError,
)

LOGGER = "earmark.transcribe.faster_whisper"

THREE = (
    FakeSegment(0.0, 4.0, " Hello\nthere "),
    FakeSegment(4.0, 9.0, "  second   line"),
    FakeSegment(9.0, 12.0, " third"),
)


def _options(**overrides: Any) -> WhisperOptions:
    values: dict[str, Any] = {
        "model": "tiny",
        "device": "cpu",
        "compute_type": "int8",
        "language": None,
        "model_dir": None,
    }
    values.update(overrides)
    return WhisperOptions(**values)


def _fake(
    segments: tuple[FakeSegment, ...] = THREE,
    *,
    language: str = "en",
    duration: float = 12.0,
    **kwargs: Any,
) -> FakeRuntime:
    model = FakeWhisperModel(segments, FakeInfo(language, duration), **kwargs)
    return FakeRuntime(model, duration_s=duration)


def _setup(tmp_path: Path, guid: str = "urn:example:guid-1") -> tuple[ArtifactStore, Episode, Path]:
    artifacts = ArtifactStore(tmp_path / "data")
    episode = make_episode(guid=guid)
    audio = artifacts.audio_path(episode.key, "mp3")
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    return artifacts, episode, audio


def _transcriber(
    artifacts: ArtifactStore, fake: FakeRuntime, *, keep_audio: bool = False, **options: Any
) -> CachedTranscriber:
    engine = FasterWhisperEngine(_options(**options), fake.runtime())
    return CachedTranscriber(artifacts, engine, keep_audio=keep_audio)


# -- A1: light imports and the missing extra --------------------------------------------


def test_transcribe_modules_import_light() -> None:
    code = (
        "import sys\n"
        "import earmark.transcribe.base, earmark.transcribe.faster_whisper\n"
        "import earmark.pipeline.factory, earmark.cli\n"
        "heavy = {'faster_whisper', 'ctranslate2', 'av', 'huggingface_hub', 'numpy',\n"
        "         'onnxruntime', 'tokenizers'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


@pytest.mark.parametrize("hidden", ["faster_whisper", "ctranslate2"])
def test_load_runtime_missing_extra(monkeypatch: pytest.MonkeyPatch, hidden: str) -> None:
    monkeypatch.setitem(sys.modules, hidden, None)
    if hidden == "ctranslate2":
        monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    with pytest.raises(MissingExtraError) as info:
        load_runtime()
    assert isinstance(info.value, ConfigError)
    [line] = info.value.errors
    assert line.startswith("EARMARK_ASR_BACKEND: ")
    assert "'transcribe' extra" in line
    assert "not installed" in line
    assert "uv sync --extra transcribe" in line


@pytest.mark.parametrize(
    "error",
    [
        ImportError("libfoo.so: cannot open shared object file"),
        ModuleNotFoundError("No module named 'av'", name="av"),
    ],
)
def test_load_runtime_broken_extra(monkeypatch: pytest.MonkeyPatch, error: ImportError) -> None:
    real = importlib.import_module

    def wrapper(name: str, package: str | None = None) -> types.ModuleType:
        if name == "faster_whisper":
            raise error
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", wrapper)
    with pytest.raises(MissingExtraError) as info:
        load_runtime()
    [line] = info.value.errors
    assert "failed to import" in line
    assert type(error).__name__ in line
    assert "uv sync --extra transcribe" in line
    assert "libfoo" not in line
    assert "No module named" not in line
    assert "not installed" not in line


def test_load_runtime_av_unimportable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "ctranslate2", types.ModuleType("ctranslate2"))
    monkeypatch.setitem(sys.modules, "av", None)
    with pytest.raises(MissingExtraError) as info:
        load_runtime()
    [line] = info.value.errors
    assert "failed to import" in line
    assert "not installed" not in line


def test_factory_missing_extra_is_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        resolver_backend="fake",
        fetch_backend="fake",
        youtube_backend="fake",
        asr_backend="faster_whisper",
        notify_backend="fake",
        mail_backend="fake",
    )
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(settings.data_dir))
    [line] = info.value.errors
    assert line.startswith("EARMARK_ASR_BACKEND: ")
    assert "uv sync --extra transcribe" in line
    assert not (tmp_path / "data").exists()


# -- A2: lazy, once per process ----------------------------------------------------------


def test_builder_loads_no_model(tmp_path: Path) -> None:
    fake = _fake()
    t = build_faster_whisper_transcriber(
        Settings(_env_file=None, data_dir=tmp_path), ArtifactStore(tmp_path), runtime=fake.runtime()
    )
    assert fake.loads == []
    assert isinstance(t.engine, FasterWhisperEngine)
    assert t.engine.model_loaded is False


def test_model_loaded_once_for_two_episodes(tmp_path: Path) -> None:
    artifacts, first, first_audio = _setup(tmp_path)
    _, second, second_audio = _setup(tmp_path, guid="urn:example:guid-2")
    fake = _fake()
    t = _transcriber(artifacts, fake)
    t.transcribe(first, first_audio)
    t.transcribe(second, second_audio)
    assert len(fake.loads) == 1
    assert len(fake.model.calls) == 2


def test_failed_load_is_retried(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    fake.load_errors = [OSError("network down")]
    t = _transcriber(artifacts, fake)
    with pytest.raises(TransientError):
        t.transcribe(episode, audio)
    assert isinstance(t.engine, FasterWhisperEngine)
    assert t.engine.model_loaded is False
    t.transcribe(episode, audio)
    assert len(fake.loads) == 2
    assert t.engine.model_loaded is True


def test_worker_loads_model_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake()
    monkeypatch.setattr(fw, "load_runtime", fake.runtime)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        resolver_backend="fake",
        fetch_backend="fake",
        youtube_backend="fake",
        asr_backend="faster_whisper",
        notify_backend="fake",
        mail_backend="fake",
        allowed_recipients=(TO,),
    )
    worker = factory.build_worker(settings)
    store = open_job_store(settings)
    store.enqueue(new_podcast_job(CANON))
    store.enqueue(new_podcast_job(CANON.replace("1000802907981", "1000802907982")))
    for _ in range(2):
        job = worker.run_once()
        assert job is not None
        assert job.status == "done"
    assert len(fake.loads) == 1
    assert len(fake.model.calls) == 2


# -- A3: settings reach the model --------------------------------------------------------


def test_options_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_ASR_MODEL", "small")
    monkeypatch.setenv("EARMARK_ASR_DEVICE", "CPU")
    monkeypatch.setenv("EARMARK_ASR_COMPUTE_TYPE", "INT8")
    monkeypatch.setenv("EARMARK_ASR_LANGUAGE", " EN ")
    monkeypatch.setenv("EARMARK_ASR_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("EARMARK_ASR_BATCH_SIZE", "8")
    settings = load_settings(env_file=None)
    expected = WhisperOptions("small", "cpu", "int8", "en", tmp_path, 8)
    assert WhisperOptions.from_settings(settings) == expected
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    t = build_faster_whisper_transcriber(settings, artifacts, runtime=fake.runtime())
    t.transcribe(episode, audio)
    assert fake.loads == [expected]


def test_language_and_options_reach_model(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake(language="fr")
    result = _transcriber(artifacts, fake, language="en").transcribe(episode, audio)
    [call] = fake.model.calls
    assert call["language"] == "en"
    assert call["beam_size"] == 5
    assert call["vad_filter"] is True
    assert call["audio"] == ("fake", "audio.mp3")
    assert result.language == "en"

    artifacts, episode, audio = _setup(tmp_path / "b")
    fake = _fake(language="fr")
    result = _transcriber(artifacts, fake).transcribe(episode, audio)
    assert fake.model.calls[0]["language"] is None
    assert result.language == "fr"


def test_real_runtime_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    constructed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    decoded: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class RecordingModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append((args, kwargs))

    def fake_decode(*args: Any, **kwargs: Any) -> list[float]:
        decoded.append((args, kwargs))
        return [0.0] * 32_000

    class StubFasterWhisper(types.ModuleType):
        WhisperModel = RecordingModel
        decode_audio = staticmethod(fake_decode)

    class StubCtranslate2(types.ModuleType):
        @staticmethod
        def get_cuda_device_count() -> int:
            return 2

    monkeypatch.setitem(sys.modules, "faster_whisper", StubFasterWhisper("faster_whisper"))
    monkeypatch.setitem(sys.modules, "ctranslate2", StubCtranslate2("ctranslate2"))
    av_stub = types.ModuleType("av")
    av_error = types.ModuleType("av.error")
    av_stub.__dict__["error"] = av_error
    monkeypatch.setitem(sys.modules, "av", av_stub)
    monkeypatch.setitem(sys.modules, "av.error", av_error)

    runtime = load_runtime()
    runtime.load_model(_options(model="small", model_dir=tmp_path))
    runtime.load_model(_options(model="small"))
    assert constructed == [
        (("small",), {"device": "cpu", "compute_type": "int8", "download_root": str(tmp_path)}),
        (("small",), {"device": "cpu", "compute_type": "int8", "download_root": None}),
    ]
    audio = runtime.decode_audio(tmp_path / "a.mp3")
    assert decoded == [((str(tmp_path / "a.mp3"),), {"sampling_rate": 16000})]
    assert audio.duration_s == 2.0
    assert runtime.cuda_device_count() == 2


def _stray_hf_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    logging.getLogger("huggingface_hub").addHandler(handler)
    return handler


@pytest.mark.usefixtures("restore_library_loggers")
def test_load_runtime_adopts_library_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "ctranslate2", types.ModuleType("ctranslate2"))
    av_stub = types.ModuleType("av")
    av_error = types.ModuleType("av.error")
    av_stub.__dict__["error"] = av_error
    monkeypatch.setitem(sys.modules, "av", av_stub)
    monkeypatch.setitem(sys.modules, "av.error", av_error)
    stray = _stray_hf_handler()
    load_runtime()
    assert stray not in logging.getLogger("huggingface_hub").handlers


@pytest.mark.usefixtures("restore_library_loggers")
def test_model_cache_status_adopts_library_loggers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_utils(monkeypatch, "/cache/snapshot")
    stray = _stray_hf_handler()
    fw.model_cache_status(_options(model="small", model_dir=tmp_path))
    assert stray not in logging.getLogger("huggingface_hub").handlers


def test_cuda_without_device_is_config_error(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, asr_device="cuda")
    artifacts = ArtifactStore(tmp_path)
    with pytest.raises(ConfigError) as info:
        build_faster_whisper_transcriber(settings, artifacts, runtime=_fake().runtime())
    text = str(info.value)
    assert "EARMARK_ASR_DEVICE" in text
    assert "no CUDA device" in text
    assert "Dockerfile.gpu" in text
    assert "gpus: all" in text
    assert "EARMARK_ASR_BACKEND=remote" in text
    gpu = _fake()
    gpu.cuda_devices = 1
    build_faster_whisper_transcriber(settings, artifacts, runtime=gpu.runtime())
    check_device(_options(device="auto"), _fake().runtime())


def test_auto_device_without_cuda_logs_cpu(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _transcriber(artifacts, _fake(), device="auto").transcribe(episode, audio)
    messages = [r.getMessage() for r in caplog.records]
    assert "CTranslate2 sees no CUDA device; transcribing on CPU" in messages
    assert any(m.startswith("loading Whisper model 'tiny'") for m in messages)
    assert any(m.startswith("loaded Whisper model 'tiny' in ") for m in messages)


# -- A4: segment mapping -----------------------------------------------------------------


def test_segments_map_to_transcript(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    result = _transcriber(artifacts, _fake()).transcribe(episode, audio)
    assert result.model == "faster-whisper:tiny"
    assert result.language == "en"
    assert result.duration_s == 12.0
    assert result.episode_key == episode.key
    assert [s.text for s in result.segments] == ["Hello there", "second line", "third"]
    assert [(s.start, s.end) for s in result.segments] == [(0.0, 4.0), (4.0, 9.0), (9.0, 12.0)]
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("start", "end", "previous", "duration", "expected"),
    [
        (2.0, 5.0, 3.0, 10.0, (3.0, 5.0)),
        (4.0, 3.0, 0.0, 10.0, (4.0, 4.0)),
        (math.nan, 6.0, 2.0, 10.0, (2.0, 6.0)),
        (3.0, math.nan, 0.0, 10.0, (3.0, 3.0)),
        (-5.0, 1.0, 0.0, 10.0, (0.0, 1.0)),
        (12.0, 15.0, 0.0, 10.0, (10.0, 10.0)),
        (12.0, 15.0, 0.0, 0.0, (12.0, 15.0)),
        (math.inf, math.inf, 1.0, 10.0, (1.0, 1.0)),
    ],
)
def test_segment_from(
    start: float, end: float, previous: float, duration: float, expected: tuple[float, float]
) -> None:
    seg = segment_from(FakeSegment(start, end, "x"), previous_start=previous, duration_s=duration)
    assert seg is not None
    assert (seg.start, seg.end) == expected


def test_segment_from_blank_text_is_none() -> None:
    assert segment_from(FakeSegment(0, 1, " \n\t "), previous_start=0, duration_s=5) is None


def test_timestamps_monotonic_property(tmp_path: Path) -> None:
    rng = random.Random(7)  # noqa: S311  # deterministic test data, not security
    segments = tuple(
        FakeSegment(rng.uniform(-10, 130), rng.uniform(-10, 130), f"s{i}") for i in range(50)
    )
    artifacts, episode, audio = _setup(tmp_path)
    result = _transcriber(artifacts, _fake(segments, duration=100.0)).transcribe(episode, audio)
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts)
    assert all(s.end >= s.start for s in result.segments)
    assert all(s.start >= 0 and s.end <= 100.0 for s in result.segments)


# -- A5: cache ---------------------------------------------------------------------------


def test_cached_transcript_loads_no_model(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    t = _transcriber(artifacts, fake, keep_audio=True)
    first = t.transcribe(episode, audio)
    fake2 = _fake()
    again = _transcriber(artifacts, fake2).transcribe(episode, audio)
    assert again == first
    assert fake2.loads == []
    assert fake2.decodes == []


# -- A6: progress and heartbeat ----------------------------------------------------------


def test_progress_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    clock = FakeMonotonic()
    segments = tuple(FakeSegment(300.0 * i, 300.0 * (i + 1), f"s{i}") for i in range(12))
    fake = _fake(segments, duration=3600.0, on_segment=lambda i: clock.advance(25))
    engine = FasterWhisperEngine(
        _options(), fake.runtime(), monotonic=clock, progress_interval_s=60.0
    )
    t = CachedTranscriber(artifacts, engine, keep_audio=False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        t.transcribe(episode, audio)
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    pattern = re.compile(
        rf"^transcribing {episode.key}: \d{{2}}:\d{{2}}:\d{{2}} of 01:00:00 \((\d+)%\)$"
    )
    progress = [m for m in messages if pattern.match(m)]
    assert 4 <= len(progress) <= 6
    assert len(progress) < 12
    percents = [int(m.rsplit("(", 1)[1].rstrip("%)")) for m in progress]
    assert percents == sorted(set(percents))
    assert any(
        m == f"transcribing {episode.key}: 01:00:00 of audio with faster-whisper:tiny"
        for m in messages
    )
    assert any(m.startswith(f"transcribed {episode.key}: 12 segments") for m in messages)


def test_heartbeat_fresh_during_transcription(tmp_path: Path) -> None:
    stale: list[Any] = []
    holder: list[Any] = []

    def on_segment(index: int) -> None:
        if index != 0:
            return
        env = holder[0]
        env.clock.advance(31)
        assert env.jobs.wait_for_beats(env.jobs.beats + 2)
        stale.append(env.jobs.requeue_stale())

    fake = _fake(on_segment=on_segment)
    fakes = make_fake_backends(tmp_path / "data")
    transcriber = CachedTranscriber(
        fakes.artifacts, FasterWhisperEngine(_options(), fake.runtime()), keep_audio=False
    )
    env = make_env(
        tmp_path,
        store_type=CountingJobStore,
        lease_s=30,
        heartbeat_s=0.01,
        fakes=fakes,
        backends=dataclasses.replace(fakes.backends(), transcriber=transcriber),
    )
    holder.append(env)
    env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert stale == [[]]


# -- A7: audio cleanup through the builder and pipeline ----------------------------------


def test_builder_passes_keep_audio(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, keep_audio=True)
    t = build_faster_whisper_transcriber(
        settings, ArtifactStore(tmp_path), runtime=_fake().runtime()
    )
    assert t.keep_audio is True


def test_pipeline_with_faster_whisper(tmp_path: Path) -> None:
    fake = _fake()
    fakes = make_fake_backends(tmp_path / "data")
    transcriber = CachedTranscriber(
        fakes.artifacts, FasterWhisperEngine(_options(), fake.runtime()), keep_audio=False
    )
    env = make_env(
        tmp_path,
        fakes=fakes,
        backends=dataclasses.replace(fakes.backends(), transcriber=transcriber),
    )
    env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    key = job.episode_key
    assert key is not None
    assert fakes.artifacts.transcript_json_path(key).is_file()
    assert fakes.artifacts.find_audio(key) is None
    loads, decodes = len(fake.loads), len(fake.decodes)
    env.jobs.enqueue(new_podcast_job(CANON, instructions="a new prompt"))
    again = env.worker.run_once()
    assert again is not None
    assert again.status == "done"
    assert (len(fake.loads), len(fake.decodes)) == (loads, decodes)


# -- A8 / B1: protocols and the factory --------------------------------------------------


def test_transcriber_satisfies_protocol(tmp_path: Path) -> None:
    fake = _fake()
    m: WhisperModelLike = fake.model
    e: TranscriptionEngine = FasterWhisperEngine(_options(), fake.runtime())
    t: Transcriber = CachedTranscriber(ArtifactStore(tmp_path), e, keep_audio=False)
    assert (m, e, t) is not None


def test_profile_includes_model_and_options() -> None:
    def profile(**overrides: Any) -> str:
        return FasterWhisperEngine(_options(**overrides), _fake().runtime()).profile

    base = profile()
    assert base.startswith("faster_whisper ")
    assert '"model":"tiny"' in base
    assert profile(model="large-v3") != base
    assert profile(language="fr") != base
    assert profile(compute_type="float16") != base
    assert profile(device="cuda") != base
    assert profile(batch_size=8) != base
    assert profile(batch_size=16) != profile(batch_size=8)
    assert profile(batch_size=0) == base
    assert profile(model_dir=Path("/elsewhere")) == base  # where models live is not an option
    assert profile() == base


def test_factory_builds_faster_whisper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake()
    monkeypatch.setattr(fw, "load_runtime", fake.runtime)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        resolver_backend="fake",
        fetch_backend="fake",
        youtube_backend="fake",
        asr_backend="faster_whisper",
        asr_model="small",
        notify_backend="fake",
        mail_backend="fake",
    )
    b = factory.build_backends(settings, ArtifactStore(settings.data_dir))
    assert isinstance(b.transcriber, CachedTranscriber)
    engine = b.transcriber.engine
    assert isinstance(engine, FasterWhisperEngine)
    assert engine.options == WhisperOptions.from_settings(settings)
    assert engine.model_loaded is False
    assert fake.loads == []


# -- B2: load error mapping --------------------------------------------------------------


class RepositoryNotFoundError(OSError):
    pass


class GatedRepoError(RepositoryNotFoundError):
    pass


class LocalEntryNotFoundError(FileNotFoundError):
    pass


URL = "https://huggingface.co/api/models/Systran/faster-whisper-nope"


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (ValueError("Invalid model size 'x'"), PermanentError, "EARMARK_ASR_COMPUTE_TYPE"),
        (RepositoryNotFoundError(f"404 at {URL}"), PermanentError, "was not found"),
        (GatedRepoError(f"401 at {URL}"), PermanentError, "was not found"),
        (LocalEntryNotFoundError(f"offline: {URL}"), TransientError, "LocalEntryNotFoundError"),
        (ConnectionError(f"reset {URL}"), TransientError, "ConnectionError"),
    ],
)
def test_load_error_mapping(
    tmp_path: Path, error: Exception, expected: type[Exception], message: str
) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    fake.load_errors = [error]
    t = _transcriber(artifacts, fake, model="whisper-x")
    with pytest.raises(expected) as info:
        t.transcribe(episode, audio)
    assert type(info.value) is expected
    assert message in str(info.value)
    assert "huggingface.co" not in str(info.value)
    assert "whisper-x" in str(info.value)
    assert info.value.__cause__ is error
    if isinstance(error, ValueError):
        for name in ("EARMARK_ASR_MODEL", "EARMARK_ASR_DEVICE", "EARMARK_ASR_COMPUTE_TYPE"):
            assert name in str(info.value)


def test_load_error_unknown_propagates(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    fake.load_errors = [RuntimeError("boom")]
    with pytest.raises(RuntimeError, match="boom"):
        _transcriber(artifacts, fake).transcribe(episode, audio)


# -- B3: unusable audio ------------------------------------------------------------------


@pytest.mark.parametrize("keep_audio", [False, True])
def test_decode_error_is_unusable_audio(tmp_path: Path, keep_audio: bool) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    cause = AudioDecodeError("Invalid data found when processing input")
    fake.decode_error = cause
    with pytest.raises(UnusableAudioError) as info:
        _transcriber(artifacts, fake, keep_audio=keep_audio).transcribe(episode, audio)
    assert str(info.value) == "could not decode audio.mp3: Invalid data found when processing input"
    assert info.value.__cause__ is cause
    assert str(tmp_path) not in str(info.value)
    assert audio.exists() is keep_audio


@pytest.mark.parametrize("keep_audio", [False, True])
def test_zero_duration_is_unusable(tmp_path: Path, keep_audio: bool) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    fake.duration_s = 0.0
    with pytest.raises(UnusableAudioError, match=r"audio\.mp3 contains no audio"):
        _transcriber(artifacts, fake, keep_audio=keep_audio).transcribe(episode, audio)
    assert fake.model.calls == []
    assert audio.exists() is keep_audio


@pytest.mark.parametrize("keep_audio", [False, True])
@pytest.mark.parametrize("segments", [(), (FakeSegment(0, 1, "  "), FakeSegment(1, 2, "\n"))])
def test_no_speech_keeps_audio(
    tmp_path: Path, segments: tuple[FakeSegment, ...], keep_audio: bool
) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    with pytest.raises(NoSpeechError, match=r"no speech found in audio.mp3 \(00:00:12 of") as info:
        _transcriber(artifacts, _fake(segments), keep_audio=keep_audio).transcribe(episode, audio)
    assert type(info.value) is NoSpeechError
    assert isinstance(info.value, PermanentError)
    assert not isinstance(info.value, UnusableAudioError)
    assert not artifacts.has_transcript(episode.key)
    assert audio.exists()


def test_no_speech_fails_job_and_keeps_audio(tmp_path: Path) -> None:
    fake = _fake(())
    fakes = make_fake_backends(tmp_path / "data")
    transcriber = CachedTranscriber(
        fakes.artifacts, FasterWhisperEngine(_options(), fake.runtime()), keep_audio=False
    )
    env = make_env(
        tmp_path,
        fakes=fakes,
        backends=dataclasses.replace(fakes.backends(), transcriber=transcriber),
    )
    env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "failed"
    assert job.attempts == 1
    assert job.error is not None
    assert "no speech found in audio.mp3" in job.error
    assert job.episode_key is not None
    assert fakes.artifacts.find_audio(job.episode_key) is not None


_LIBRARY_ERRORS: list[Any] = [
    TypeError("open() got an unexpected keyword argument 'metadata_errors'"),
    RuntimeError("x"),
    IndexError("tuple index out of range"),
    PermissionError(13, "Permission denied"),
    StubDecoderNotFoundError("/x/audio.mp3"),
    MemoryError(),
]


@pytest.mark.parametrize("error", _LIBRARY_ERRORS, ids=lambda e: type(e).__name__)
def test_decoder_library_errors_propagate(tmp_path: Path, error: BaseException) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    fake = _fake()
    fake.decode_error = error
    with pytest.raises(type(error)) as info:
        _transcriber(artifacts, fake, keep_audio=False).transcribe(episode, audio)
    assert info.value is error
    assert not isinstance(info.value, UnusableAudioError)
    assert audio.exists()
    assert not artifacts.has_transcript(episode.key)


def _stub_extra(decode: Any, **kwargs: Any) -> StubExtra:
    model = FakeWhisperModel(THREE, FakeInfo("en", 12.0))
    return StubExtra(model, decode=decode, **kwargs)


def _raiser(error: BaseException) -> Any:
    def decode(path: str) -> list[float]:
        raise error

    return decode


@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        (StubInvalidDataError, "Invalid data found when processing input"),
        (StubEOFError, "End of file"),
    ],
)
def test_real_runtime_classifies_bad_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: Any, detail: str
) -> None:
    path = tmp_path / "audio.mp3"
    cause = error_type(path)
    stub = _stub_extra(_raiser(cause))
    stub.install(monkeypatch)
    with pytest.raises(AudioDecodeError) as info:
        load_runtime().decode_audio(path)
    assert info.value.detail == detail
    assert str(tmp_path) not in info.value.detail
    assert info.value.__cause__ is cause
    assert stub.open_calls == []


def test_real_runtime_no_audio_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "audio.mp4"
    cause = IndexError("tuple index out of range")
    stub = _stub_extra(_raiser(cause), audio_streams=0)
    stub.install(monkeypatch)
    with pytest.raises(AudioDecodeError, match="no audio stream") as info:
        load_runtime().decode_audio(path)
    assert info.value.__cause__ is cause
    assert stub.open_calls == [str(path)]


def test_real_runtime_unusable_through_transcriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    stub = _stub_extra(_raiser(StubInvalidDataError(audio)))
    stub.install(monkeypatch)
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", asr_device="cpu")
    transcriber = build_faster_whisper_transcriber(settings, artifacts)
    with pytest.raises(UnusableAudioError, match=r"could not decode audio\.mp3: Invalid data"):
        transcriber.transcribe(episode, audio)
    assert not audio.exists()


def test_real_runtime_type_error_not_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = TypeError("open() got an unexpected keyword argument 'metadata_errors'")
    stub = _stub_extra(_raiser(error))
    stub.install(monkeypatch)
    with pytest.raises(TypeError) as info:
        load_runtime().decode_audio(tmp_path / "audio.mp3")
    assert info.value is error
    assert stub.open_calls == []


@pytest.mark.parametrize(
    "streams",
    [1, UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")],
    ids=["has-audio-stream", "probe-fails"],
)
def test_real_runtime_index_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streams: int | BaseException
) -> None:
    path = tmp_path / "audio.mp3"
    error = IndexError("tuple index out of range")
    stub = _stub_extra(_raiser(error), audio_streams=streams)
    stub.install(monkeypatch)
    with pytest.raises(IndexError) as info:
        load_runtime().decode_audio(path)
    assert info.value is error
    assert stub.open_calls == [str(path)]


@pytest.mark.parametrize("error_type", [StubFileNotFoundError, StubDecoderNotFoundError])
def test_real_runtime_other_av_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: Any
) -> None:
    path = tmp_path / "audio.mp3"
    error = error_type(path)
    stub = _stub_extra(_raiser(error))
    stub.install(monkeypatch)
    with pytest.raises(type(error)) as info:
        load_runtime().decode_audio(path)
    assert info.value is error


def test_pyav19_type_error_requeues_and_keeps_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    message = "open() got an unexpected keyword argument 'metadata_errors'"
    broken = True

    def decode(path: str) -> list[float]:
        if broken:
            raise TypeError(message)
        return [0.0] * 32_000

    stub = _stub_extra(decode)
    stub.install(monkeypatch)
    fakes = make_fake_backends(tmp_path / "data")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", asr_device="cpu")
    transcriber = build_faster_whisper_transcriber(settings, fakes.artifacts)
    env = make_env(
        tmp_path,
        fakes=fakes,
        backends=dataclasses.replace(fakes.backends(), transcriber=transcriber),
    )
    env.jobs.enqueue(new_podcast_job(CANON))
    with caplog.at_level(logging.ERROR):
        job = env.worker.run_once()
    assert job is not None
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.error is not None
    assert job.error.startswith(f"unexpected error: TypeError: {message}")
    assert "could not decode" not in job.error
    key = job.episode_key
    assert key is not None
    assert fakes.artifacts.find_audio(key) is not None
    assert not fakes.artifacts.has_transcript(key)
    assert stub.open_calls == []
    assert any(
        r.name == "earmark.pipeline.runner"
        and r.levelno == logging.ERROR
        and r.exc_info is not None
        and r.exc_info[0] is TypeError
        for r in caplog.records
    )

    broken = False
    env.clock.advance(61)
    again = env.worker.run_once()
    assert again is not None
    assert again.status == "done"
    assert fakes.artifacts.has_transcript(key)
    assert fakes.artifacts.find_audio(key) is None
    assert len(stub.decode_calls) == 2


def test_pyav19_type_error_exhausted_keeps_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub_extra(_raiser(TypeError("open() got an unexpected keyword argument")))
    stub.install(monkeypatch)
    fakes = make_fake_backends(tmp_path / "data")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", asr_device="cpu")
    transcriber = build_faster_whisper_transcriber(settings, fakes.artifacts)
    env = make_env(
        tmp_path,
        max_attempts=1,
        fakes=fakes,
        backends=dataclasses.replace(fakes.backends(), transcriber=transcriber),
    )
    env.jobs.enqueue(new_podcast_job(CANON))
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "failed"
    assert job.episode_key is not None
    assert fakes.artifacts.find_audio(job.episode_key) is not None


def test_transcribe_value_error_is_permanent(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    detail = "'xx' is not a valid language code " + "x" * 500
    fake = _fake(transcribe_error=ValueError(detail))
    with pytest.raises(PermanentError) as info:
        _transcriber(artifacts, fake, language="xx").transcribe(episode, audio)
    message = str(info.value)
    assert message.endswith("(check EARMARK_ASR_LANGUAGE)")
    assert message.startswith("transcription failed: 'xx' is not a valid language code")
    suffix = " (check EARMARK_ASR_LANGUAGE)"
    assert len(message) <= len("transcription failed: ") + MAX_DETAIL_CHARS + len(suffix)
    assert audio.exists()


def test_transcribe_other_value_error_propagates(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    error = ValueError("stft: expected 0 < n_fft <= 10, but got n_fft=400")
    fake = _fake(transcribe_error=error)
    with pytest.raises(ValueError, match="n_fft") as info:
        _transcriber(artifacts, fake, language="en").transcribe(episode, audio)
    assert info.value is error
    assert not isinstance(info.value, PermanentError)
    assert audio.exists()


def test_value_error_while_iterating_propagates(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)

    def on_segment(index: int) -> None:
        if index == 1:
            raise ValueError("not a valid language code")

    fake = _fake(on_segment=on_segment)
    with pytest.raises(ValueError, match="not a valid language code") as info:
        _transcriber(artifacts, fake, language="en").transcribe(episode, audio)
    assert not isinstance(info.value, PermanentError)
    assert audio.exists()


# -- B4: interruption --------------------------------------------------------------------


def test_shutdown_mid_transcription(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)

    def interrupt(index: int) -> None:
        if index == 1:
            raise Shutdown

    fake = _fake(on_segment=interrupt)
    t = _transcriber(artifacts, fake)
    with pytest.raises(Shutdown):
        t.transcribe(episode, audio)
    directory = artifacts.episode_dir(episode.key)
    assert not list(directory.glob("transcript.*"))
    assert not [p for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert audio.exists()
    fake.model.on_segment = None
    t.transcribe(episode, audio)
    assert len(fake.loads) == 1


# -- B5: file_key ------------------------------------------------------------------------


def test_file_key_deterministic(tmp_path: Path) -> None:
    a, b, c = tmp_path / "a.mp3", tmp_path / "b.mp3", tmp_path / "c.mp3"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    c.write_bytes(b"different")
    assert file_key(a) == file_key(b)
    assert file_key(a) != file_key(c)
    assert is_episode_key(file_key(a))


def test_decoded_audio_is_opaque() -> None:
    assert DecodedAudio(samples=object(), duration_s=1.0).duration_s == 1.0


def test_transcript_validates_back(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    result = _transcriber(artifacts, _fake(), keep_audio=True).transcribe(episode, audio)
    assert (
        Transcript.model_validate_json(artifacts.transcript_json_path(episode.key).read_text())
        == result
    )
    assert FakeResolver().resolve(CANON).key  # fakes are importable alongside


# -- model_cache_status (doctor) ------------------------------------------------------------


def _stub_utils(
    monkeypatch: pytest.MonkeyPatch, behaviour: Any
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    utils = types.ModuleType("faster_whisper.utils")

    def download_model(*args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        if isinstance(behaviour, Exception):
            raise behaviour
        return str(behaviour)

    utils.download_model = download_model  # type: ignore[attr-defined]  # a stub module
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    return calls


def test_model_cache_status_cached_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_utils(monkeypatch, "/cache/snapshot")
    options = _options(model="small", model_dir=tmp_path)
    assert fw.model_cache_status(options) == "cached"
    assert calls == [(("small",), {"local_files_only": True, "cache_dir": str(tmp_path)})]


def test_model_cache_status_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_utils(monkeypatch, FileNotFoundError("not cached"))
    assert fw.model_cache_status(_options(model="small")) == "missing"
    assert calls[0][1] == {"local_files_only": True, "cache_dir": None}


def test_model_cache_status_unknown_size(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_utils(monkeypatch, ValueError("Invalid model size"))
    with pytest.raises(ConfigError) as info:
        fw.model_cache_status(_options(model="nope\x1b"))
    [line] = info.value.errors
    assert line.startswith("EARMARK_ASR_MODEL: 'nope?'")


def test_model_cache_status_local_directory(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    with pytest.raises(ConfigError, match=r"no model\.bin"):
        fw.model_cache_status(_options(model=str(model)))
    (model / "model.bin").write_bytes(b"x")
    assert fw.model_cache_status(_options(model=str(model))) == "local"


# -- step 26: batching, the CUDA report, the refusal --------------------------------------

PRE_STEP_26_PROFILE = (
    'faster_whisper {"beam_size":5,"compute_type":"int8","device":"cpu","language":null,'
    '"model":"tiny","vad_filter":true}'
)


def test_profile_unchanged_when_batching_off() -> None:
    engine = FasterWhisperEngine(_options(), _fake().runtime())
    assert engine.profile == PRE_STEP_26_PROFILE


def _store_pre_step_transcript(artifacts: ArtifactStore, episode: Episode) -> None:
    transcript = Transcript(
        episode_key=episode.key,
        language="en",
        duration_s=12.0,
        model="faster-whisper:tiny",
        segments=(),
        asr_profile=PRE_STEP_26_PROFILE,
    )
    artifacts.write_transcript(transcript)


def test_cached_transcript_kept_when_batching_off(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    _store_pre_step_transcript(artifacts, episode)
    fake = _fake()
    _transcriber(artifacts, fake).transcribe(episode, audio)
    assert fake.loads == []
    assert fake.decodes == []


def test_cached_transcript_miss_when_batch_changes(tmp_path: Path) -> None:
    artifacts, episode, audio = _setup(tmp_path)
    _store_pre_step_transcript(artifacts, episode)
    batched = _fake()
    _transcriber(artifacts, batched, batch_size=8).transcribe(episode, audio)
    assert len(batched.loads) == 1
    assert batched.loads[0].batch_size == 8
    assert len(batched.decodes) == 1


def _stub_model(**extra: Any) -> tuple[FakeWhisperModel, StubExtra]:
    model = FakeWhisperModel(THREE, FakeInfo("en", 12.0))
    return model, StubExtra(model, decode=lambda _p: [0.0] * 32_000, **extra)


def test_sequential_when_batch_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    model, stub = _stub_model()
    stub.install(monkeypatch)
    loaded = load_runtime().load_model(_options())
    assert loaded is model
    assert stub.pipelines == []
    loaded.transcribe("a", language=None, beam_size=5, vad_filter=True, initial_prompt=None)
    assert all("batch_size" not in call for call in model.calls)


def test_batched_pipeline_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    model, stub = _stub_model()
    stub.install(monkeypatch)
    loaded = load_runtime().load_model(_options(batch_size=8, language="en"))
    assert isinstance(loaded, fw.BatchedWhisper)
    [pipeline] = stub.pipelines
    assert pipeline.model is model
    segments, info = loaded.transcribe(
        "a", language="en", beam_size=5, vad_filter=True, initial_prompt=None
    )
    assert [seg.text for seg in segments] == [seg.text for seg in THREE]
    assert info.language == "en"
    assert pipeline.calls == [
        {
            "language": "en",
            "beam_size": 5,
            "vad_filter": True,
            "initial_prompt": None,
            "batch_size": 8,
        }
    ]


def test_batched_load_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, stub = _stub_model()
    stub.install(monkeypatch)
    engine = FasterWhisperEngine(
        _options(device="cuda", compute_type="float16", batch_size=8), load_runtime()
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        engine._load()
    messages = [r.getMessage() for r in caplog.records]
    assert any("batch size 8" in m and m.startswith("loading Whisper model") for m in messages)
    assert any(m.endswith("on cuda (float16)") for m in messages)


def test_load_log_unchanged_without_batching(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        FasterWhisperEngine(_options(), _fake().runtime())._load()
    [loading] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("loading")]
    assert loading == "loading Whisper model 'tiny' (device cpu, compute type int8)"


def test_real_supported_compute_types(monkeypatch: pytest.MonkeyPatch) -> None:
    _, stub = _stub_model(cuda_devices=1, cuda_types=["float32", "float16"])
    stub.install(monkeypatch)
    assert load_runtime().supported_compute_types("cuda") == frozenset({"float32", "float16"})


def test_cuda_report_no_device_skips_query() -> None:
    fake = _fake()
    report = fw.cuda_report(fake.runtime())
    assert report == fw.CudaReport(0, None, None)
    assert fake.types_queries == []


def test_cuda_report_types() -> None:
    fake = _fake()
    fake.cuda_devices = 2
    fake.cuda_types = frozenset({"float16", "int8"})
    assert fw.cuda_report(fake.runtime()) == fw.CudaReport(2, frozenset({"float16", "int8"}), None)
    assert fake.types_queries == ["cuda"]


def test_cuda_report_query_error_is_type_name() -> None:
    fake = _fake()
    fake.cuda_devices = 1
    fake.types_error = ValueError("secret detail not to be shown")
    report = fw.cuda_report(fake.runtime())
    assert report == fw.CudaReport(1, None, "ValueError")


def test_worker_refuses_cuda_without_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fw, "load_runtime", _fake().runtime)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        resolver_backend="fake",
        fetch_backend="fake",
        youtube_backend="fake",
        asr_backend="faster_whisper",
        asr_device="cuda",
    )
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(tmp_path / "data"))
    assert "no CUDA device" in str(info.value)
    assert "Dockerfile.gpu" in str(info.value)


# -- step 36: per-request options --------------------------------------------------------------


def _engine_for(fake: FakeRuntime, **options: Any) -> FasterWhisperEngine:
    return FasterWhisperEngine(_options(**options), fake.runtime())


def test_initial_prompt_passed_sequential(tmp_path: Path) -> None:
    from earmark.models import DecodeOptions

    fake = _fake()
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    _engine_for(fake).transcribe_file(
        audio, "a" * 20, DecodeOptions(initial_prompt="Alice, Kubernetes", language="de")
    )
    [call] = fake.model.calls
    assert call["initial_prompt"] == "Alice, Kubernetes"
    assert call["language"] == "de"


def test_initial_prompt_passed_batched(monkeypatch: pytest.MonkeyPatch) -> None:
    model, stub = _stub_model()
    stub.install(monkeypatch)
    loaded = load_runtime().load_model(_options(batch_size=4))
    loaded.transcribe("a", language="de", beam_size=5, vad_filter=True, initial_prompt="Alice")
    [pipeline] = stub.pipelines
    assert pipeline.calls[0]["initial_prompt"] == "Alice"
    assert pipeline.calls[0]["batch_size"] == 4
    assert model.calls[0]["initial_prompt"] == "Alice"


def test_mail_path_passes_none_and_setting_language(tmp_path: Path) -> None:
    fake = _fake()
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    transcript = _engine_for(fake, language="en").transcribe_file(audio, "a" * 20)
    [call] = fake.model.calls
    assert call["initial_prompt"] is None
    assert call["language"] == "en"
    assert transcript.language == "en"


def test_options_language_overrides_setting(tmp_path: Path) -> None:
    from earmark.models import DecodeOptions

    fake = _fake(language="fr")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    engine = _engine_for(fake, language="en")
    detect = engine.transcribe_file(audio, "a" * 20, DecodeOptions())
    german = engine.transcribe_file(audio, "a" * 20, DecodeOptions(language="de"))
    assert [c["language"] for c in fake.model.calls] == [None, "de"]
    assert (detect.language, german.language) == ("fr", "de")  # None: the detected one


def test_profile_for_options() -> None:
    import hashlib

    from earmark.models import DecodeOptions

    engine = _engine_for(_fake(), language="en")
    assert engine.profile_for(None) == engine.profile
    prompted = engine.profile_for(DecodeOptions(initial_prompt="Alice", language="de"))
    digest = hashlib.sha256(b"Alice").hexdigest()
    assert f'"initial_prompt_sha256":"{digest}"' in prompted
    assert '"language":"de"' in prompted
    assert "Alice" not in prompted
    assert '"language":null' in engine.profile_for(DecodeOptions())
    assert "initial_prompt_sha256" not in engine.profile_for(DecodeOptions(language="de"))
    assert prompted != engine.profile_for(DecodeOptions(initial_prompt="Bob", language="de"))


def test_profile_unchanged_without_options() -> None:
    off = FasterWhisperEngine(_options(language="en"), _fake().runtime())
    assert off.profile == (
        'faster_whisper {"beam_size":5,"compute_type":"int8","device":"cpu","language":"en",'
        '"model":"tiny","vad_filter":true}'
    )
    on = FasterWhisperEngine(_options(batch_size=8), _fake().runtime())
    assert on.profile == (
        'faster_whisper {"batch_size":8,"beam_size":5,"compute_type":"int8","device":"cpu",'
        '"language":null,"model":"tiny","vad_filter":true}'
    )


def test_language_error_hint_names_request(tmp_path: Path) -> None:
    from earmark.models import DecodeOptions

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    detail = "'xx' is not a valid language code (accepted language codes: af, am)"
    engine = _engine_for(_fake(transcribe_error=ValueError(detail)), language="en")
    with pytest.raises(PermanentError) as info:
        engine.transcribe_file(audio, "a" * 20, DecodeOptions(language="xx"))
    assert str(info.value).endswith("(check the request's language)")
    assert "EARMARK_ASR_LANGUAGE" not in str(info.value)
    with pytest.raises(PermanentError) as setting:
        engine.transcribe_file(audio, "a" * 20)
    assert str(setting.value).endswith("(check EARMARK_ASR_LANGUAGE)")
