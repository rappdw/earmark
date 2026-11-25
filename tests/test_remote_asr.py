import dataclasses
import gc
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from earmark import fetch
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings, load_settings
from earmark.errors import PermanentError, TransientError
from earmark.fakes import FakeResolver, make_fake_backends
from earmark.models import Transcript
from earmark.pipeline import factory
from earmark.pipeline.stages import Transcriber
from earmark.pipeline.worker import Shutdown
from earmark.transcribe.base import (
    CachedTranscriber,
    NoSpeechError,
    TranscriptionEngine,
    UnusableAudioError,
)
from earmark.transcribe.remote import (
    MAX_DETAIL_CHARS,
    NO_TIMESTAMPS_WARNING,
    UPLOAD_CONTENT_TYPES,
    RemoteAsrEngine,
    RemoteOptions,
    build_remote_transcriber,
    error_detail,
    transcript_from_response,
)
from tests.asr_stub import (
    AsrStub,
    RecordingTransport,
    StubReply,
    parse_multipart,
)
from tests.factories import new_podcast_job
from tests.pipeline_env import CANON, CountingJobStore, make_env

MIB = 1024 * 1024
EKEY = "0123456789abcdef0123"
BASE = "http://asr.test:8000/v1"
KEY = "sentinel-key-9f3"
CANON_EPISODE = FakeResolver().resolve(CANON)

THREE = json.dumps(
    {
        "task": "transcribe",
        "language": "en",
        "duration": 30.0,
        "text": "Hello there second line third",
        "segments": [
            {
                "id": i,
                "start": start,
                "end": end,
                "text": text,
                "tokens": [1, 2],
                "avg_logprob": -0.2,
                "words": [{"word": "x", "start": start, "end": end}],
            }
            for i, (start, end, text) in enumerate(
                [(0, 10, " Hello\nthere "), (10, 20, "  second   line"), (20, 30, " third")]
            )
        ],
    }
).encode()


def _options(**overrides: Any) -> RemoteOptions:
    values: dict[str, Any] = {
        "base_url": BASE,
        "model": "whisper-x",
        "language": None,
        "api_key": None,
        "max_upload_bytes": None,
        "timeout_s": 10800.0,
    }
    values.update(overrides)
    if isinstance(values["api_key"], str):
        from pydantic import SecretStr

        values["api_key"] = SecretStr(values["api_key"])
    return RemoteOptions(**values)


def _settings(tmp_path: Path | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "asr_backend": "remote",
        "asr_base_url": BASE,
        "asr_model": "whisper-x",
    }
    if tmp_path is not None:
        values["data_dir"] = tmp_path / "data"
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _audio(directory: Path, size: int = 4096, name: str = "in.mp3") -> Path:
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


class Server:
    """A MockTransport handler that counts calls and serves scripted replies."""

    def __init__(self, *replies: httpx2.Response | BaseException) -> None:
        self.replies = list(replies) or [httpx2.Response(200, content=THREE)]
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


def _engine(server: Server | None = None, **overrides: Any) -> RemoteAsrEngine:
    server = server or Server()
    return RemoteAsrEngine(_options(**overrides), transport=server.transport())


def _json(status: int, body: object, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=headers)


@contextmanager
def _cached(
    tmp_path: Path, server: Server, *, keep_audio: bool = False, **overrides: Any
) -> Iterator[tuple[CachedTranscriber, ArtifactStore, Path]]:
    artifacts = ArtifactStore(tmp_path / "data")
    engine = RemoteAsrEngine(_options(**overrides), transport=server.transport())
    transcriber = CachedTranscriber(artifacts, engine, keep_audio=keep_audio)
    key = CANON_EPISODE.key
    path = artifacts.audio_path(key, "mp3")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(3000))
    try:
        yield transcriber, artifacts, path
    finally:
        engine.close()


# -- A1: streamed upload and segment mapping -------------------------------------------------


def test_upload_is_streamed_multipart(tmp_path: Path) -> None:
    audio = _audio(tmp_path, size=MIB)
    recorder = RecordingTransport(lambda r: httpx2.Response(200, content=THREE))
    RemoteAsrEngine(_options(), transport=recorder).transcribe_file(audio, EKEY)
    headers = recorder.headers
    assert headers is not None
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert int(headers["content-length"]) == sum(recorder.chunk_sizes)
    assert "transfer-encoding" not in headers
    assert max(recorder.chunk_sizes) <= 65536
    assert len(recorder.chunk_sizes) > 16
    fields, data, name, ctype = parse_multipart(headers["content-type"], recorder.body)
    assert fields == {
        "model": "whisper-x",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    assert (name, ctype) == ("audio.mp3", "audio/mpeg")
    assert data == audio.read_bytes()


def test_upload_over_unix_socket_server(tmp_path: Path) -> None:
    audio = _audio(tmp_path, size=200_000)
    with AsrStub([StubReply(body=THREE)]) as stub:
        engine = RemoteAsrEngine(_options(), transport=stub.transport())
        transcript = engine.transcribe_file(audio, EKEY)
        engine.close()
    [received] = stub.requests
    assert received.method == "POST"
    assert received.path == "/v1/audio/transcriptions"
    assert received.file_bytes == audio.read_bytes()
    assert received.headers["user-agent"] == fetch.USER_AGENT
    assert received.headers["host"] == "asr.test:8000"
    assert len(transcript.segments) == 3


def test_segments_map_to_transcript(tmp_path: Path) -> None:
    transcript = _engine().transcribe_file(_audio(tmp_path), EKEY)
    assert [s.text for s in transcript.segments] == ["Hello there", "second line", "third"]
    assert [(s.start, s.end) for s in transcript.segments] == [(0, 10), (10, 20), (20, 30)]
    assert transcript.duration_s == 30.0
    assert transcript.model == "remote:whisper-x"
    assert transcript.language == "en"
    assert transcript.warnings == ()
    assert transcript.episode_key == EKEY


def _map(segments: list[dict[str, Any]], **extra: Any) -> Transcript:
    payload = {"text": "t", "segments": segments, **extra}
    return transcript_from_response(
        payload, episode_key=EKEY, model_name="remote:m", language=None, audio_name="a.mp3"
    )


@pytest.mark.parametrize(
    ("segments", "extra", "expected"),
    [
        pytest.param(
            [{"start": 5, "end": 8, "text": "a"}, {"start": 3, "end": 9, "text": "b"}],
            {"duration": 10},
            [(5, 8), (5, 9)],
            id="regressing-start",
        ),
        pytest.param(
            [{"start": 4, "end": 2, "text": "a"}], {"duration": 10}, [(4, 4)], id="end-before-start"
        ),
        pytest.param(
            [{"start": 2, "end": 3, "text": "a"}, {"end": 6, "text": "b"}],
            {"duration": 10},
            [(2, 3), (2, 6)],
            id="start-missing",
        ),
        pytest.param(
            [{"start": 2, "end": 3, "text": "a"}, {"start": 4, "text": "b"}],
            {"duration": 10},
            [(2, 3), (4, 4)],
            id="end-missing",
        ),
        pytest.param(
            [{"start": 8, "end": 12, "text": "a"}, {"start": 11, "end": 13, "text": "b"}],
            {"duration": 10},
            [(8, 10), (10, 10)],
            id="beyond-duration",
        ),
        pytest.param(
            [{"start": 0, "end": 1, "text": "a"}, {"start": 1, "end": 2, "text": "  "}],
            {"duration": 10},
            [(0, 1)],
            id="blank-dropped",
        ),
        pytest.param(
            [{"start": 0, "end": 30.5, "text": "a"}, {"start": 29.5, "end": 60, "text": "b"}],
            {"duration": 60},
            [(0, 30.5), (29.5, 60)],
            id="overlapping-chunks",
        ),
    ],
)
def test_monotonic_mapping(
    segments: list[dict[str, Any]], extra: dict[str, Any], expected: list[tuple[float, float]]
) -> None:
    transcript = _map(segments, **extra)
    assert [(s.start, s.end) for s in transcript.segments] == expected
    assert transcript.warnings == ()


@pytest.mark.parametrize("duration", [None, 0, -5])
def test_duration_fallbacks(duration: float | None) -> None:
    segments = [{"start": 0, "end": 4, "text": "a"}, {"start": 4, "end": 7.5, "text": "b"}]
    extra = {} if duration is None else {"duration": duration}
    assert _map(segments, **extra).duration_s == 7.5


def test_language_from_settings_wins(tmp_path: Path) -> None:
    server = Server()
    transcript = _engine(server, language="de").transcribe_file(_audio(tmp_path), EKEY)
    assert transcript.language == "de"
    request = server.requests[0]
    fields, *_ = parse_multipart(request.headers["content-type"], request.read())
    assert fields["language"] == "de"


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("English", "english"), ("EN", "en"), ("??", None), ("x" * 40, None), ("", None)],
)
def test_language_normalised(reported: str, expected: str | None) -> None:
    assert _map([], text="hi", language=reported).language == expected


def test_no_language_field_by_default(tmp_path: Path) -> None:
    recorder = RecordingTransport(lambda r: httpx2.Response(200, content=THREE))
    RemoteAsrEngine(_options(), transport=recorder).transcribe_file(_audio(tmp_path), EKEY)
    assert recorder.headers is not None
    fields, *_ = parse_multipart(recorder.headers["content-type"], recorder.body)
    assert "language" not in fields


# -- A2: text without segments ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "text", "end"),
    [
        ({"text": " hello  world "}, "hello world", 0.0),
        ({"text": "hello", "segments": []}, "hello", 0.0),
        ({"text": "hello", "segments": None, "duration": 12.5}, "hello", 12.5),
        (
            {"text": "hello", "segments": [{"id": 0, "text": "hello", "start": 0, "end": 0}]},
            "hello",
            0.0,
        ),
        ({"text": "hello", "segments": [{"id": 0, "text": "hello"}]}, "hello", 0.0),
        ({"text": "", "segments": [{"text": "from"}, {"text": "segments"}]}, "from segments", 0.0),
    ],
)
def test_text_without_segments_is_one_segment_with_warning(
    body: dict[str, Any], text: str, end: float
) -> None:
    transcript = transcript_from_response(
        body, episode_key=EKEY, model_name="m", language=None, audio_name="a.mp3"
    )
    [segment] = transcript.segments
    assert (segment.start, segment.end, segment.text) == (0.0, end, text)
    assert transcript.duration_s == end
    assert transcript.warnings == (NO_TIMESTAMPS_WARNING,)


def test_long_text_not_truncated() -> None:
    text = " ".join(["word"] * 40_001)
    assert len(text) >= 200_000
    transcript = transcript_from_response(
        {"text": text}, episode_key=EKEY, model_name="m", language=None, audio_name="a.mp3"
    )
    assert transcript.segments[0].text == text


def test_text_only_warning_reaches_job(tmp_path: Path) -> None:
    server = Server(_json(200, {"text": "hello there"}))
    fakes = make_fake_backends(tmp_path / "data")
    transcriber = CachedTranscriber(
        fakes.artifacts, RemoteAsrEngine(_options(), transport=server.transport()), keep_audio=False
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
    assert NO_TIMESTAMPS_WARNING in job.warnings
    key = job.episode_key
    assert key is not None
    assert NO_TIMESTAMPS_WARNING in fakes.artifacts.transcript_json_path(key).read_text()
    env.jobs.enqueue(new_podcast_job(CANON, instructions="a new prompt"))
    again = env.worker.run_once()
    assert again is not None
    assert again.status == "done"
    assert NO_TIMESTAMPS_WARNING in again.warnings
    assert server.calls == 1


@pytest.mark.parametrize(
    "body",
    [{"text": ""}, {"text": "  ", "segments": [{"start": 0, "end": 1, "text": " "}]}],
)
@pytest.mark.parametrize("keep_audio", [False, True])
def test_no_speech_keeps_audio(tmp_path: Path, body: dict[str, Any], keep_audio: bool) -> None:
    server = Server(_json(200, body))
    with _cached(tmp_path, server, keep_audio=keep_audio) as (transcriber, artifacts, path):
        with pytest.raises(NoSpeechError, match="no speech found"):
            transcriber.transcribe(CANON_EPISODE, path)
        assert path.exists()
        assert not artifacts.has_transcript(CANON_EPISODE.key)


# -- A3: upload cap --------------------------------------------------------------------------


def test_upload_cap_refuses_before_request(tmp_path: Path) -> None:
    server = Server()
    with _cached(tmp_path, server, max_upload_bytes=MIB) as (transcriber, _, path):
        path.write_bytes(b"x" * (MIB + 1))
        with pytest.raises(PermanentError) as info:
            transcriber.transcribe(CANON_EPISODE, path)
        assert type(info.value) is PermanentError
        assert "EARMARK_ASR_MAX_UPLOAD_MB" in info.value.message
        assert "1 MiB" in info.value.message
        assert server.calls == 0
        assert path.exists()


def test_upload_cap_allows_equal_size(tmp_path: Path) -> None:
    server = Server()
    audio = tmp_path / "in.mp3"
    audio.write_bytes(b"x" * MIB)
    _engine(server, max_upload_bytes=MIB).transcribe_file(audio, EKEY)
    assert server.calls == 1


def test_no_cap_by_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    options = RemoteOptions.from_settings(settings)
    assert options.max_upload_bytes is None
    server = Server()
    audio = tmp_path / "in.mp3"
    audio.write_bytes(b"x" * (2 * MIB))
    RemoteAsrEngine(options, transport=server.transport()).transcribe_file(audio, EKEY)
    assert server.calls == 1


def test_options_from_settings_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_ASR_BASE_URL", BASE)
    monkeypatch.setenv("EARMARK_ASR_MAX_UPLOAD_MB", "100")
    options = RemoteOptions.from_settings(load_settings(env_file=None))
    assert options.max_upload_bytes == 100 * 1024 * 1024


def test_empty_file_is_unusable(tmp_path: Path) -> None:
    server = Server()
    audio = tmp_path / "in.mp3"
    audio.write_bytes(b"")
    with pytest.raises(UnusableAudioError, match=r"in\.mp3 is empty"):
        _engine(server).transcribe_file(audio, EKEY)
    assert server.calls == 0


# -- A4: API key and timeout -----------------------------------------------------------------


def test_no_auth_header_without_key(tmp_path: Path) -> None:
    server = Server()
    _engine(server).transcribe_file(_audio(tmp_path), EKEY)
    assert "authorization" not in server.requests[0].headers
    with AsrStub([StubReply(body=THREE)]) as stub:
        engine = RemoteAsrEngine(_options(), transport=stub.transport())
        engine.transcribe_file(_audio(tmp_path), EKEY)
        engine.close()
    assert "authorization" not in stub.requests[0].headers


def test_bearer_header_with_key(tmp_path: Path) -> None:
    server = Server()
    _engine(server, api_key="sk-test-77").transcribe_file(_audio(tmp_path), EKEY)
    assert server.requests[0].headers["authorization"] == "Bearer sk-test-77"
    with AsrStub([StubReply(body=THREE)]) as stub:
        engine = RemoteAsrEngine(_options(api_key="sk-test-77"), transport=stub.transport())
        engine.transcribe_file(_audio(tmp_path), EKEY)
        engine.close()
    assert stub.requests[0].headers["authorization"] == "Bearer sk-test-77"


def test_key_from_env_is_stripped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EARMARK_ASR_BASE_URL", BASE)
    monkeypatch.setenv("EARMARK_ASR_API_KEY", " sk-x\n")
    server = Server()
    options = RemoteOptions.from_settings(load_settings(env_file=None))
    RemoteAsrEngine(options, transport=server.transport()).transcribe_file(_audio(tmp_path), EKEY)
    assert server.requests[0].headers["authorization"] == "Bearer sk-x"


def test_key_never_logged_or_in_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bad_body = json.dumps({"error": {"message": f"bad key {KEY}"}}).encode()
    audio = _audio(tmp_path)
    options = _options(api_key=KEY)
    mock = Server(_json(200, json.loads(THREE)), _json(401, json.loads(bad_body)))
    with AsrStub([StubReply(body=THREE), StubReply(401, bad_body)]) as stub:
        for transport in (mock.transport(), stub.transport()):
            engine = RemoteAsrEngine(options, transport=transport)
            engine.transcribe_file(audio, EKEY)
            with pytest.raises(PermanentError) as info:
                engine.transcribe_file(audio, EKEY)
            assert KEY not in info.value.message
            assert "**********" in info.value.message
            assert KEY not in repr(engine.options)
            engine.close()
    assert KEY not in repr(options)
    assert KEY not in caplog.text


def test_timeout_from_settings(tmp_path: Path) -> None:
    default = RemoteAsrEngine(RemoteOptions.from_settings(_settings()))
    assert (default.timeout.read, default.timeout.write) == (10800.0, 10800.0)
    assert (default.timeout.connect, default.timeout.pool) == (10.0, 10.0)
    short = RemoteAsrEngine(RemoteOptions.from_settings(_settings(asr_timeout_seconds=60)))
    assert short.timeout.read == 60.0
    server = Server()
    _engine(server).transcribe_file(_audio(tmp_path), EKEY)
    assert server.requests[0].extensions["timeout"] == {
        "connect": 10.0,
        "read": 10800.0,
        "write": 10800.0,
        "pool": 10.0,
    }


def test_default_timeout_covers_an_hour() -> None:
    assert Settings(_env_file=None).asr_timeout_seconds >= 3600


def test_read_timeout_is_transient_naming_setting(tmp_path: Path) -> None:
    server = Server(httpx2.ReadTimeout("slow"))
    with pytest.raises(TransientError, match="EARMARK_ASR_TIMEOUT_SECONDS"):
        _engine(server).transcribe_file(_audio(tmp_path), EKEY)


# -- A5: error classes -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_status_transient(tmp_path: Path, status: int) -> None:
    with pytest.raises(TransientError) as info:
        _engine(Server(_json(status, {"error": "busy"}))).transcribe_file(_audio(tmp_path), EKEY)
    assert f"HTTP {status}: busy" in info.value.message
    assert "asr.test:8000" in info.value.message


@pytest.mark.parametrize(
    ("status", "names"),
    [
        (400, []),
        (401, ["EARMARK_ASR_API_KEY"]),
        (403, ["EARMARK_ASR_API_KEY"]),
        (404, ["EARMARK_ASR_BASE_URL", "EARMARK_ASR_MODEL"]),
        (413, ["EARMARK_ASR_MAX_UPLOAD_MB"]),
        (415, []),
        (422, []),
    ],
)
def test_status_permanent(tmp_path: Path, status: int, names: list[str]) -> None:
    with pytest.raises(PermanentError) as info:
        _engine(Server(_json(status, {"detail": "nope"}))).transcribe_file(_audio(tmp_path), EKEY)
    assert not isinstance(info.value, TransientError)
    assert f"HTTP {status}" in info.value.message
    assert info.value.message.endswith(": nope")
    for name in names:
        assert name in info.value.message


@pytest.mark.parametrize("status", [301, 307])
def test_redirect_not_followed(tmp_path: Path, status: int) -> None:
    server = Server(httpx2.Response(status, headers={"Location": "http://elsewhere.test/x"}))
    with pytest.raises(PermanentError, match="redirects are not followed"):
        _engine(server).transcribe_file(_audio(tmp_path), EKEY)
    assert server.calls == 1


@pytest.mark.parametrize("status", [204, 206])
def test_unexpected_success_status(tmp_path: Path, status: int) -> None:
    with pytest.raises(PermanentError, match="unexpected"):
        _engine(Server(httpx2.Response(status))).transcribe_file(_audio(tmp_path), EKEY)


def test_retry_after_honoured(tmp_path: Path) -> None:
    server = Server(httpx2.Response(503, headers={"Retry-After": "120"}))
    with pytest.raises(TransientError) as info:
        _engine(server).transcribe_file(_audio(tmp_path), EKEY)
    assert info.value.retry_after_s == 120


@pytest.mark.parametrize(
    "exc_type",
    [
        httpx2.ConnectError,
        httpx2.ConnectTimeout,
        httpx2.WriteTimeout,
        httpx2.PoolTimeout,
        httpx2.ReadError,
        httpx2.RemoteProtocolError,
        httpx2.WriteError,
        httpx2.DecodingError,
    ],
)
def test_transport_errors_transient(tmp_path: Path, exc_type: type[Exception]) -> None:
    server = Server(exc_type("failed for http://asr.test/v1/secret-path"))
    with pytest.raises(TransientError) as info:
        _engine(server).transcribe_file(_audio(tmp_path), EKEY)
    assert "secret-path" not in info.value.message
    assert "asr.test:8000" in info.value.message
    assert isinstance(info.value.__cause__, exc_type)


# httpcore2's unix-socket connect leaves its socket unclosed when connect() fails (upstream);
# collect it inside this test so the warning cannot surface in an unrelated one.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_connection_refused_over_unix_socket(tmp_path: Path) -> None:
    transport = httpx2.HTTPTransport(uds=str(tmp_path / "missing.sock"))
    engine = RemoteAsrEngine(_options(), transport=transport)
    with pytest.raises(TransientError):
        engine.transcribe_file(_audio(tmp_path), EKEY)
    engine.close()
    gc.collect()


@pytest.mark.parametrize(
    ("body", "ctype", "expected"),
    [
        (b'{"error": {"message": "openai style"}}', "application/json", "openai style"),
        (b'{"error": "whisper cpp style"}', "application/json", "whisper cpp style"),
        (b'{"detail": "fastapi style"}', "application/json", "fastapi style"),
        (b'{"detail": [{"msg": "list style"}]}', "application/json", "list style"),
        (b'{"message": "plain message"}', "application/json", "plain message"),
        (b'{"message": "no content type"}', None, "no content type"),
        (b"just text", "text/plain; charset=utf-8", "just text"),
        (b"<html>oops</html>", "text/html", None),
        (b"{not json", "application/json", None),
        (b'{"error": 5}', "application/json", None),
        (b"", "text/plain", None),
        (b"x" * 300, "text/plain", "x" * MAX_DETAIL_CHARS),
        (b"a\x1b[31m\x00b", "text/plain", "a [31m b"),
        (b"bad key sentinel-key", "text/plain", "bad key **********"),
        (b"\xff\xfe broken", "text/plain", "�� broken"),
    ],
)
def test_error_detail_extraction(body: bytes, ctype: str | None, expected: str | None) -> None:
    assert error_detail(body, ctype, ["sentinel-key"]) == expected


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, content=b"<html>hi</html>", headers={"Content-Type": "text/html"}),
        httpx2.Response(200, json=["a"]),
        httpx2.Response(200, json={"text": 5}),
        httpx2.Response(200, content=b"\xff\xfe"),
        httpx2.Response(200, json={}),
        httpx2.Response(200, json={"error": {"message": "model not loaded"}}),
        httpx2.Response(200, json={"detail": "Not Found"}),
        httpx2.Response(200, json={"segments": []}),
    ],
    ids=["html", "list", "wrong-type", "binary", "empty", "error", "detail", "segments-only"],
)
def test_unreadable_200_is_permanent(tmp_path: Path, response: httpx2.Response) -> None:
    with pytest.raises(PermanentError, match=r"not JSON|not a transcription") as info:
        _engine(Server(response)).transcribe_file(_audio(tmp_path), EKEY)
    assert not isinstance(info.value, NoSpeechError)


@pytest.mark.parametrize(
    "body",
    [{}, {"error": {"message": "model not loaded"}}, {"detail": "Not Found"}, {"segments": []}],
)
def test_body_without_text_is_not_a_transcription(tmp_path: Path, body: dict[str, Any]) -> None:
    with pytest.raises(PermanentError, match="not a transcription") as info:
        _engine(Server(httpx2.Response(200, json=body))).transcribe_file(_audio(tmp_path), EKEY)
    assert not isinstance(info.value, NoSpeechError)


def test_error_shaped_200_keeps_audio(tmp_path: Path) -> None:
    server = Server(_json(200, {"error": "busy"}))
    with _cached(tmp_path, server, keep_audio=False) as (transcriber, artifacts, path):
        with pytest.raises(PermanentError, match="not a transcription"):
            transcriber.transcribe(CANON_EPISODE, path)
        assert path.exists()
        assert not artifacts.has_transcript(CANON_EPISODE.key)


def test_oversized_response_is_permanent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("earmark.transcribe.remote.MAX_RESPONSE_BYTES", 1024)
    server = Server(httpx2.Response(200, content=b" " * 2048))
    with pytest.raises(PermanentError, match="returned more than"):
        _engine(server).transcribe_file(_audio(tmp_path), EKEY)


# -- A6: caching and file outputs ------------------------------------------------------------


@pytest.mark.parametrize("keep_audio", [False, True])
def test_cached_transcript_makes_no_request(tmp_path: Path, keep_audio: bool) -> None:
    server = Server()
    with _cached(tmp_path, server, keep_audio=keep_audio) as (transcriber, artifacts, path):
        stored = transcript_from_response(
            json.loads(THREE),
            episode_key=CANON_EPISODE.key,
            model_name="remote:old",
            language=None,
            audio_name="a",
        ).model_copy(update={"asr_profile": transcriber.profile})
        artifacts.write_transcript(stored)
        assert transcriber.transcribe(CANON_EPISODE, path) == stored
        assert server.calls == 0
        assert path.exists() is keep_audio


@pytest.mark.parametrize("keep_audio", [False, True])
def test_transcript_files_written(tmp_path: Path, keep_audio: bool) -> None:
    server = Server()
    with _cached(tmp_path, server, keep_audio=keep_audio) as (transcriber, artifacts, path):
        transcript = transcriber.transcribe(CANON_EPISODE, path)
        key = CANON_EPISODE.key
        text_path = artifacts.transcript_text_path(key)
        json_path = artifacts.transcript_json_path(key)
        assert text_path.read_text() == transcript.to_text()
        assert Transcript.model_validate_json(json_path.read_text()) == transcript
        for file in (text_path, json_path):
            assert file.stat().st_mode & 0o777 == 0o644
        assert not list(json_path.parent.glob("*.tmp"))
        assert path.exists() is keep_audio


def test_audio_kept_on_failure(tmp_path: Path) -> None:
    server = Server(
        httpx2.Response(503), _json(400, {"error": "bad"}), RuntimeError("never mapped")
    )
    with _cached(tmp_path, server) as (transcriber, artifacts, path):
        for expected in (TransientError, PermanentError):
            with pytest.raises(expected):
                transcriber.transcribe(CANON_EPISODE, path)
            assert path.exists()
        server.replies[2] = Shutdown()
        with pytest.raises(Shutdown):
            transcriber.transcribe(CANON_EPISODE, path)
        assert path.exists()
        assert not artifacts.has_transcript(CANON_EPISODE.key)
        assert not artifacts.transcript_text_path(CANON_EPISODE.key).exists()


def test_pipeline_with_remote(tmp_path: Path) -> None:
    server = Server()
    fakes = make_fake_backends(tmp_path / "data")
    settings = _settings()
    transcriber = build_remote_transcriber(settings, fakes.artifacts, transport=server.transport())
    env = make_env(
        tmp_path,
        fakes=fakes,
        backends=dataclasses.replace(fakes.backends(), transcriber=transcriber),
    )
    env.jobs.enqueue(new_podcast_job(CANON))
    first = env.worker.run_once()
    env.jobs.enqueue(new_podcast_job(CANON, instructions="another prompt"))
    second = env.worker.run_once()
    assert first is not None
    assert second is not None
    assert (first.status, second.status) == ("done", "done")
    assert server.calls == 1


def test_heartbeat_fresh_during_remote_wait(tmp_path: Path) -> None:
    stale: list[Any] = []
    holder: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        env = holder[0]
        env.clock.advance(31)
        assert env.jobs.wait_for_beats(env.jobs.beats + 2)
        stale.append(env.jobs.requeue_stale())
        return httpx2.Response(200, content=THREE)

    fakes = make_fake_backends(tmp_path / "data")
    transcriber = CachedTranscriber(
        fakes.artifacts,
        RemoteAsrEngine(_options(), transport=httpx2.MockTransport(handler)),
        keep_audio=False,
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


def test_shutdown_during_remote_wait(tmp_path: Path) -> None:
    def on_alarm(signum: int, frame: types.FrameType | None) -> None:
        raise Shutdown

    hold = threading.Event()
    previous = signal.signal(signal.SIGALRM, on_alarm)
    try:
        with AsrStub([StubReply(body=THREE, hold=hold)]) as stub:
            artifacts = ArtifactStore(tmp_path / "data")
            engine = RemoteAsrEngine(_options(), transport=stub.transport())
            transcriber = CachedTranscriber(artifacts, engine, keep_audio=False)
            key = CANON_EPISODE.key
            path = artifacts.audio_path(key, "mp3")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 3000)
            started = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, 0.2)
            with pytest.raises(Shutdown):
                transcriber.transcribe(CANON_EPISODE, path)
            assert time.monotonic() - started < 5
            signal.setitimer(signal.ITIMER_REAL, 0)
            assert path.exists()
            assert not artifacts.has_transcript(key)
            assert not artifacts.transcript_text_path(key).exists()
            engine.close()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# -- B1: registration and light imports ------------------------------------------------------


def test_factory_builds_remote(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        resolver_backend="fake",
        fetch_backend="fake",
        youtube_backend="fake",
        notify_backend="fake",
        mail_backend="fake",
        asr_api_key="k-1",
    )
    b = factory.build_backends(settings, ArtifactStore(settings.data_dir))
    assert isinstance(b.transcriber, CachedTranscriber)
    engine = b.transcriber.engine
    assert isinstance(engine, RemoteAsrEngine)
    assert engine.options == RemoteOptions.from_settings(settings)
    assert not (tmp_path / "data").exists()


def test_types_satisfy_protocols(tmp_path: Path) -> None:
    e: TranscriptionEngine = RemoteAsrEngine(_options())
    t: Transcriber = CachedTranscriber(ArtifactStore(tmp_path), e, keep_audio=False)
    assert (e, t) is not None


def test_remote_module_import_light() -> None:
    code = (
        "import sys\n"
        "import earmark.transcribe.remote\n"
        "heavy = {'faster_whisper', 'ctranslate2', 'av', 'numpy', 'huggingface_hub'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


# -- B2: missing base URL --------------------------------------------------------------------


def test_missing_base_url_is_config_error(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        asr_base_url=None,
        resolver_backend="fake",
        fetch_backend="fake",
        youtube_backend="fake",
        notify_backend="fake",
        mail_backend="fake",
    )
    with pytest.raises(ConfigError) as info:
        factory.build_backends(settings, ArtifactStore(settings.data_dir))
    [line] = info.value.errors
    assert line.startswith("EARMARK_ASR_BASE_URL: required when EARMARK_ASR_BACKEND=remote")


# -- B3: client trust boundary ---------------------------------------------------------------


@pytest.mark.parametrize("base", ["http://127.0.0.1:8000/v1", "http://100.100.1.1/v1"])
def test_private_address_allowed(tmp_path: Path, base: str) -> None:
    settings = _settings(tmp_path, asr_base_url=base)
    assert settings.allow_private_fetch is False
    with AsrStub([StubReply(body=THREE)]) as stub:
        engine = RemoteAsrEngine(RemoteOptions.from_settings(settings), transport=stub.transport())
        transcript = engine.transcribe_file(_audio(tmp_path), EKEY)
        engine.close()
    assert len(transcript.segments) == 3
    assert stub.requests[0].path == "/v1/audio/transcriptions"


def test_env_proxy_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:1")
    with AsrStub([StubReply(body=THREE)]) as stub:
        uds_transport = stub.transport()
        monkeypatch.setattr(httpx2, "HTTPTransport", lambda *a, **k: uds_transport)
        engine = RemoteAsrEngine(_options())
        engine.transcribe_file(_audio(tmp_path), EKEY)
        engine.close()
    assert len(stub.requests) == 1


def test_cookies_not_sent(tmp_path: Path) -> None:
    first = httpx2.Response(200, content=THREE, headers={"Set-Cookie": "sid=abc; Path=/"})
    server = Server(first, httpx2.Response(200, content=THREE))
    engine = _engine(server)
    audio = _audio(tmp_path)
    engine.transcribe_file(audio, EKEY)
    engine.transcribe_file(audio, EKEY)
    assert "cookie" not in server.requests[1].headers


def test_close_then_reuse(tmp_path: Path) -> None:
    engine = _engine()
    audio = _audio(tmp_path)
    engine.transcribe_file(audio, EKEY)
    engine.close()
    engine.close()
    assert len(engine.transcribe_file(audio, EKEY).segments) == 3


# -- B5: settings ----------------------------------------------------------------------------


def test_asr_base_url_normalised() -> None:
    assert _settings(asr_base_url=" http://h:8000/v1/ ").asr_base_url == "http://h:8000/v1"
    assert _settings(asr_base_url="HTTPS://H/v1").asr_base_url == "HTTPS://H/v1"


def test_asr_api_key_blank_is_none() -> None:
    settings = _settings(asr_api_key="   ")
    assert settings.asr_api_key is None
    assert settings.secret_values() == []


# -- B8: upload metadata ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "filename", "ctype"),
    [
        (".mp3", "audio.mp3", "audio/mpeg"),
        (".M4A", "audio.m4a", "audio/mp4"),
        (".opus", "audio.opus", "audio/ogg"),
        (".flac", "audio.flac", "audio/flac"),
        (".xyz", "audio.bin", "application/octet-stream"),
        ("", "audio.bin", "application/octet-stream"),
    ],
)
def test_upload_filename_and_type(tmp_path: Path, suffix: str, filename: str, ctype: str) -> None:
    audio = _audio(tmp_path, name=f"secret-local-name{suffix}")
    recorder = RecordingTransport(lambda r: httpx2.Response(200, content=THREE))
    RemoteAsrEngine(_options(), transport=recorder).transcribe_file(audio, EKEY)
    assert recorder.headers is not None
    _, data, name, kind = parse_multipart(recorder.headers["content-type"], recorder.body)
    assert (name, kind) == (filename, ctype)
    assert data == audio.read_bytes()
    assert b"secret-local-name" not in recorder.body


def test_content_types_cover_audio_extensions() -> None:
    assert set(UPLOAD_CONTENT_TYPES) == fetch.AUDIO_EXTENSIONS


def test_asr_doc_names_settings() -> None:
    doc = Path(__file__).resolve().parent.parent / "docs" / "asr.md"
    text = doc.read_text()
    for needle in (
        "EARMARK_ASR_BACKEND=remote",
        "EARMARK_ASR_BASE_URL",
        "EARMARK_ASR_API_KEY",
        "EARMARK_ASR_MAX_UPLOAD_MB",
        "EARMARK_ASR_TIMEOUT_SECONDS",
        "EARMARK_ASR_MODEL",
        "verbose_json",
        "/audio/transcriptions",
    ):
        assert needle in text


def test_no_private_address_guard_in_transcribe() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "earmark" / "transcribe"
    for path in root.glob("*.py"):
        assert "allow_private" not in path.read_text()


def test_lone_surrogate_in_segment_text(tmp_path: Path) -> None:
    body = THREE.replace(b" third", b" a\\ud800")  # the JSON escape of a lone surrogate
    assert b"\\ud800" in body
    server = Server(httpx2.Response(200, content=body))
    with _cached(tmp_path, server) as (transcriber, artifacts, _):
        transcript = transcriber.transcribe(
            CANON_EPISODE, artifacts.audio_path(CANON_EPISODE.key, "mp3")
        )
    assert transcript.segments[-1].text == "a\ufffd"
    stored = artifacts.read_transcript(CANON_EPISODE.key)
    assert stored == transcript
    artifacts.transcript_text_path(CANON_EPISODE.key).read_bytes().decode("utf-8")


def test_profile_includes_model_and_language() -> None:
    base = _engine().profile
    assert base.startswith("remote ")
    assert _engine(model="whisper-y").profile != base
    assert _engine(language="fr").profile != base
    assert _engine(timeout_s=5.0).profile == base
    assert _engine().profile == base
