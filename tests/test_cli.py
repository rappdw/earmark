import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import earmark
from earmark.artifacts import ArtifactStore
from earmark.cli import app
from earmark.config import ConfigError, Settings, env_var
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fakes import FakeResolver, FakeSearcher
from earmark.index.embed import IndexExtraMissing
from earmark.index.search import IndexMismatchError, SearchQuery
from earmark.jobs import open_job_store
from earmark.models import MAX_PROMPT_CHARS, Episode, Transcript
from earmark.pipeline.worker import Worker
from earmark.resolve.apple import MESSAGES, UrlProblem
from earmark.transcribe import faster_whisper as fw
from tests.factories import make_analysis, make_episode, make_transcript
from tests.index_helpers import RecordingTarget, make_docs, stub_extra_missing
from tests.test_index_search import make_hit
from tests.whisper_fakes import FakeInfo, FakeRuntime, FakeSegment, FakeWhisperModel

runner = CliRunner()
VERSION = importlib.metadata.version("earmark")


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"earmark {VERSION}\n"


def test_console_script_prints_version() -> None:
    exe = Path(sys.executable).parent / "earmark"
    proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "earmark 0.1.0"


def test_version_works_with_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_PORT", "nope")
    assert runner.invoke(app, ["--version"]).exit_code == 0


def test_module_entry_point_version() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "earmark", "--version"], capture_output=True, text=True, check=True
    )
    assert proc.stdout == f"earmark {VERSION}\n"


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "config" in result.output


@pytest.mark.usefixtures("restore_root_logging")
def test_config_never_prints_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", "sentinel-api-7f3a9c")
    monkeypatch.setenv("EARMARK_ASR_API_KEY", "sentinel-asr-1b2c3d")
    monkeypatch.setenv("EARMARK_LLM_API_KEY", "sentinel-llm-4e5f6a")
    (tmp_path / ".env").write_text("EARMARK_NTFY_TOKEN=sentinel-ntfy-8d9e0f\n")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    for s in ("api-7f3a9c", "asr-1b2c3d", "llm-4e5f6a", "ntfy-8d9e0f"):
        assert f"sentinel-{s}" not in result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert "EARMARK_API_TOKEN=**********" in lines
    assert "EARMARK_NTFY_TOKEN=**********" in lines


@pytest.mark.usefixtures("restore_root_logging")
def test_config_redacts_ntfy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_NTFY_URL", "https://ntfy.example/sentinel-topic-77")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "EARMARK_NTFY_URL=**********" in result.output.splitlines()
    assert "sentinel-topic-77" not in result.output


def test_config_refuses_ntfy_url_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_NTFY_URL", "https://bob:sentinel-pw-1@ntfy.example/t")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 2
    assert "EARMARK_NTFY_URL" in result.stderr
    assert "sentinel-pw-1" not in result.stdout + result.stderr


def test_config_error_does_not_echo_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_PORT", "sentinel-port-x")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 2
    assert "EARMARK_PORT" in result.stderr
    assert result.stdout == ""
    assert "sentinel-port-x" not in result.stdout + result.stderr


@pytest.mark.usefixtures("restore_root_logging")
def test_config_lists_every_field() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    for field in Settings.model_fields:
        prefix = f"{env_var(field)}="
        assert sum(line.startswith(prefix) for line in lines) == 1, field
    assert "EARMARK_PORT=8787" in lines
    assert "EARMARK_KEEP_AUDIO=false" in lines


def test_import_earmark_is_light() -> None:
    code = (
        "import sys, earmark\n"
        "heavy = {'pydantic', 'pydantic_settings', 'typer', 'fastapi', 'uvicorn', 'httpx2',\n"
        "         'faster_whisper', 'ctranslate2', 'lancedb', 'fastembed', 'anthropic',\n"
        "         'pyarrow', 'onnxruntime', 'numpy', 'loguru'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"
    assert earmark.__version__ == VERSION


# -- serve -------------------------------------------------------------------------------

SERVE_TOKEN = "sentinel-serve-5c1d9e"


class _UvicornCalls:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> _UvicornCalls:
    calls = _UvicornCalls()
    monkeypatch.setattr(uvicorn, "run", calls)
    return calls


@pytest.mark.usefixtures("restore_root_logging")
@pytest.mark.parametrize("token", [None, "   "])
def test_serve_refuses_without_token(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls, tmp_path: Path, token: str | None
) -> None:
    if token is not None:
        monkeypatch.setenv("EARMARK_API_TOKEN", token)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 2
    assert "EARMARK_API_TOKEN is not set" in result.stderr
    assert fake_uvicorn.calls == []
    assert not (tmp_path / "data").exists()


@pytest.mark.usefixtures("restore_root_logging")
@pytest.mark.parametrize("token", ["tok en", "tök"])
def test_serve_rejects_unsafe_token(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls, token: str
) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", token)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 2
    assert "printable ASCII" in result.stderr
    assert token not in result.stdout + result.stderr
    assert fake_uvicorn.calls == []


@pytest.mark.usefixtures("restore_root_logging")
def test_serve_runs_uvicorn(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls, tmp_path: Path
) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    monkeypatch.setenv("EARMARK_PORT", "8799")
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert len(fake_uvicorn.calls) == 1
    args, kwargs = fake_uvicorn.calls[0]
    assert isinstance(args[0], FastAPI)
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 8799,
        "log_config": None,
        "server_header": False,
    }
    assert (tmp_path / "data" / "earmark.db").exists()
    assert SERVE_TOKEN not in result.stdout + result.stderr


@pytest.mark.usefixtures("restore_root_logging")
def test_serve_store_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    monkeypatch.setenv("EARMARK_DATA_DIR", str(blocker))
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert result.stderr.startswith("earmark serve: cannot open the job store")
    assert SERVE_TOKEN not in result.stdout + result.stderr
    assert fake_uvicorn.calls == []


def test_cli_import_is_light() -> None:
    code = (
        "import sys, earmark.cli\n"
        "heavy = {'fastapi', 'starlette', 'uvicorn'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


# -- worker, submit, jobs ----------------------------------------------------------------

SHARE = "https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4"
SHOW_LINK = "https://podcasts.apple.com/us/podcast/x/id1200361736"
BACKEND_VARS = (
    "RESOLVER_BACKEND",
    "FETCH_BACKEND",
    "ASR_BACKEND",
    "LLM_PROVIDER",
    "STORE_BACKEND",
    "INDEX_BACKEND",
    "NOTIFY_BACKEND",
)


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_root_logging: None) -> Path:
    for name in BACKEND_VARS:
        monkeypatch.setenv(f"EARMARK_{name}", "fake")
    monkeypatch.setenv("EARMARK_VAULT_DIR", str(tmp_path / "vault"))
    return tmp_path


def _submit(*args: str) -> str:
    result = runner.invoke(app, ["submit", *args])
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_submit_then_worker_once(fake_env: Path) -> None:
    out = _submit(SHARE, "--prompt", "q")
    match = re.fullmatch(r"([0-9a-f]{32}) queued\n", out)
    assert match
    job_id = match.group(1)
    worked = runner.invoke(app, ["worker", "--once"])
    assert worked.exit_code == 0, worked.stderr
    assert worked.stdout == f"job {job_id} done\n"
    listed = runner.invoke(app, ["jobs", "--json"])
    [job] = json.loads(listed.stdout)["jobs"]
    assert (job["id"], job["status"]) == (job_id, "done")
    assert list((fake_env / "vault" / "Earmark" / "Podcasts").glob("*.md"))


def test_worker_once_empty(fake_env: Path) -> None:
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 0
    assert result.stdout == "no job ready\n"


def test_worker_runs_forever_without_once(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Worker] = []
    monkeypatch.setattr(Worker, "run_forever", lambda self: calls.append(self))
    result = runner.invoke(app, ["worker"])
    assert result.exit_code == 0
    assert len(calls) == 1


def test_worker_store_error_exits_1(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = fake_env / "file"
    blocker.write_text("x")
    monkeypatch.setenv("EARMARK_DATA_DIR", str(blocker))
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 1
    assert result.stderr.startswith("earmark worker: cannot open the job store")


@pytest.mark.usefixtures("restore_root_logging")
def test_worker_unbuilt_backend_exits_2(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.pipeline import factory

    monkeypatch.setattr(
        factory, "NOTIFIERS", {k: v for k, v in factory.NOTIFIERS.items() if k != "ntfy"}
    )
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "ntfy")
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "EARMARK_NOTIFY_BACKEND" in result.stderr
    assert not (fake_env / "data").exists()


def test_worker_invalid_backend_name_exits_2(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "whisperx-sentinel")
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "EARMARK_ASR_BACKEND" in result.stderr
    assert "whisperx-sentinel" not in result.stdout + result.stderr


def test_submit_dedupe(fake_env: Path) -> None:
    first = _submit(SHARE).split()[0]
    second = _submit(SHARE)
    assert second == f"{first} queued (existing job)\n"


@pytest.mark.parametrize(("url", "problem"), [(SHOW_LINK, "show_link"), ("not a url", "malformed")])
def test_submit_rejects_url(fake_env: Path, url: str, problem: UrlProblem) -> None:
    result = runner.invoke(app, ["submit", url])
    assert result.exit_code == 2
    assert MESSAGES[problem] in result.stderr
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_submit_rejects_long_prompt(fake_env: Path) -> None:
    result = runner.invoke(app, ["submit", SHARE, "--prompt", "x" * (MAX_PROMPT_CHARS + 1)])
    assert result.exit_code == 2
    assert f"prompt is too long (at most {MAX_PROMPT_CHARS} characters)" in result.stderr
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_jobs_text_output(fake_env: Path) -> None:
    first = _submit(SHARE, "--prompt", "one").split()[0]
    second = _submit(SHARE, "--prompt", "two").split()[0]
    runner.invoke(app, ["worker", "--once"])  # runs the first job
    lines = runner.invoke(app, ["jobs"]).stdout.splitlines()
    assert [ln.split("  ")[0] for ln in lines] == [second, first]
    assert lines[0].split("  ")[1] == "queued"
    assert lines[1].split("  ")[1:4] == ["done", "index", "1"]
    assert lines[1].endswith("  https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981")
    done = runner.invoke(app, ["jobs", "--status", "done"]).stdout.splitlines()
    assert [ln.split("  ")[0] for ln in done] == [first]
    assert len(runner.invoke(app, ["jobs", "--limit", "1"]).stdout.splitlines()) == 1
    assert runner.invoke(app, ["jobs", "--limit", "0"]).exit_code == 2
    assert runner.invoke(app, ["jobs", "--status", "bogus"]).exit_code == 2


def test_jobs_sanitizes_control_chars(fake_env: Path) -> None:
    store = open_job_store(Settings(_env_file=None))
    store.enqueue("https://podcasts.apple.com/us/podcast/id1?i=2")
    claim = store.claim_next()
    assert claim is not None
    store.fail(claim, "bad \x1b[31mred\x1b[0m\nsecond line", transient=False)
    out = runner.invoke(app, ["jobs"]).stdout
    assert "\x1b" not in out
    assert "  error: bad ?[31mred?[0m?second line" in out


def test_jobs_empty(fake_env: Path) -> None:
    result = runner.invoke(app, ["jobs"])
    assert result.exit_code == 0
    assert result.stdout == "no jobs\n"


# -- resolve ------------------------------------------------------------------------------

CANONICAL_SHARE = "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981"


def _use_resolver(monkeypatch: pytest.MonkeyPatch, resolver: object) -> None:
    from earmark.pipeline import factory

    monkeypatch.setattr(factory, "build_resolver", lambda settings: resolver)


def test_resolve_prints_episode_json(fake_env: Path) -> None:
    result = runner.invoke(app, ["resolve", SHARE])
    assert result.exit_code == 0, result.stderr
    episode = Episode.model_validate_json(result.stdout)
    assert episode.url == CANONICAL_SHARE
    assert result.stdout.endswith("}\n")
    assert result.stderr == ""


def test_resolve_apple_details_on_stderr(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.apple_fixtures import DAILY_GUID, default_server, resolver_for

    _use_resolver(monkeypatch, resolver_for(default_server()))
    result = runner.invoke(app, ["resolve", SHARE])
    assert result.exit_code == 0, result.stderr
    assert "resolved from the lookup; feed match: guid" in result.stderr
    assert json.loads(result.stdout)["guid"] == DAILY_GUID


def test_resolve_apple_unlisted_episode_note(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.apple_fixtures import (
        default_server,
        drop_item,
        feed_reply,
        fixture_bytes,
        resolver_for,
    )

    server = default_server()
    server.feeds["feeds.npr.org/510289/podcast.xml"] = feed_reply(
        drop_item(fixture_bytes("feed_planet_money.xml"), "e3153a19-8617-46a1-a9ef-989fe009911c")
    )
    _use_resolver(monkeypatch, resolver_for(server))
    old = "https://podcasts.apple.com/us/podcast/x/id290783428?i=1000655720058"
    result = runner.invoke(app, ["resolve", old])
    assert result.exit_code == 0, result.stderr
    assert (
        "resolved from the page; the feed does not list it, audio URL from Apple" in result.stderr
    )


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (SkipEpisode("no feed"), 3, "earmark resolve: skipped: no feed\n"),
        (PermanentError("gone"), 1, "earmark resolve: gone\n"),
        (TransientError("busy"), 1, "earmark resolve: busy (temporary; try again later)\n"),
    ],
    ids=["skip", "permanent", "transient"],
)
def test_resolve_failures_exit_codes(
    fake_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: int,
    message: str,
) -> None:
    resolver = FakeResolver()
    resolver.fail_next(error)
    _use_resolver(monkeypatch, resolver)
    result = runner.invoke(app, ["resolve", SHARE])
    assert result.exit_code == code
    assert result.stderr == message
    assert result.stdout == ""


def test_resolve_unsupported_url_exits_2(fake_env: Path) -> None:
    result = runner.invoke(app, ["resolve", SHOW_LINK])
    assert result.exit_code == 2
    assert result.stderr == f"earmark resolve: {MESSAGES['show_link']}\n"
    assert result.stdout == ""


def test_resolve_unbuilt_backend_exits_2(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.pipeline import factory

    monkeypatch.delenv("EARMARK_RESOLVER_BACKEND")
    monkeypatch.setattr(factory, "RESOLVERS", {"fake": factory.RESOLVERS["fake"]})
    result = runner.invoke(app, ["resolve", SHARE])
    assert result.exit_code == 2
    assert "EARMARK_RESOLVER_BACKEND" in result.stderr


def test_resolve_sanitizes_output(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    episode = make_episode(title="\x1b[31mx")
    resolver = FakeResolver({CANONICAL_SHARE: episode})
    _use_resolver(monkeypatch, resolver)
    result = runner.invoke(app, ["resolve", SHARE])
    assert result.exit_code == 0
    assert "\x1b" not in result.stdout
    assert json.loads(result.stdout)["title"] == "\x1b[31mx"


def test_resolve_error_messages_are_cleaned(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = FakeResolver()
    resolver.fail_next(PermanentError("bad \x1b[31m"))
    _use_resolver(monkeypatch, resolver)
    result = runner.invoke(app, ["resolve", SHARE])
    assert "\x1b" not in result.stderr
    assert "bad ?[31m" in result.stderr


# -- worker and transcribe without the extra (step 08) -----------------------------------


def _hide_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)


def test_worker_missing_extra_exits_2(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "faster_whisper")
    _hide_extra(monkeypatch)
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "EARMARK_ASR_BACKEND" in result.stderr
    assert "transcribe" in result.stderr
    assert "uv sync --extra transcribe" in result.stderr
    assert not (fake_env / "data").exists()


def test_transcribe_missing_extra_exits_2(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_extra(monkeypatch)
    clip = fake_env / "clip.mp3"
    clip.write_bytes(b"x")
    result = runner.invoke(app, ["transcribe", str(clip)])
    assert result.exit_code == 2
    assert "uv sync --extra transcribe" in result.stderr
    assert not (fake_env / "data").exists()


# -- transcribe ----------------------------------------------------------------------------


@pytest.fixture
def whisper(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    segments = (FakeSegment(0.0, 4.0, " Hello there"), FakeSegment(65.0, 70.0, "second"))
    fake = FakeRuntime(FakeWhisperModel(segments, FakeInfo("en", 70.0)), duration_s=70.0)
    monkeypatch.setattr(fw, "load_runtime", fake.runtime)
    return fake


def _clip(directory: Path, data: bytes = b"audio bytes") -> Path:
    clip = directory / "clip.mp3"
    clip.write_bytes(data)
    return clip


def test_transcribe_prints_lines(fake_env: Path, whisper: FakeRuntime) -> None:
    clip = _clip(fake_env)
    result = runner.invoke(app, ["transcribe", str(clip)])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "[00:00:00] Hello there\n[00:01:05] second\n"
    assert clip.read_bytes() == b"audio bytes"
    assert not (fake_env / "data").exists()
    assert whisper.decodes == [clip]


def test_transcribe_json(fake_env: Path, whisper: FakeRuntime) -> None:
    clip = _clip(fake_env)
    result = runner.invoke(app, ["transcribe", "--json", str(clip)])
    assert result.exit_code == 0, result.stderr
    transcript = Transcript.model_validate_json(result.stdout)
    assert transcript.episode_key == fw.file_key(clip)
    assert transcript.model == "faster-whisper:large-v3"
    assert [s.text for s in transcript.segments] == ["Hello there", "second"]


def test_transcribe_missing_file_exits_2(fake_env: Path, whisper: FakeRuntime) -> None:
    result = runner.invoke(app, ["transcribe", str(fake_env / "nope.mp3")])
    assert result.exit_code == 2
    assert whisper.loads == []


def test_transcribe_unusable_audio_exits_1(fake_env: Path, whisper: FakeRuntime) -> None:
    whisper.decode_error = ValueError("bad")
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith(
        "earmark transcribe: could not decode clip.mp3"
    )
    assert result.stdout == ""


def test_transcribe_transient_exits_1(fake_env: Path, whisper: FakeRuntime) -> None:
    whisper.load_errors = [OSError("offline")]
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith("earmark transcribe: ")
    assert result.stderr.rstrip().endswith("(temporary; try again later)")


def test_transcribe_cleans_control_chars(fake_env: Path, whisper: FakeRuntime) -> None:
    whisper.model.segments = [FakeSegment(0.0, 1.0, "\x1b[31mred")]
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "red" in result.stdout


def test_transcribe_ignores_asr_backend(
    fake_env: Path, whisper: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "fake")
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 0, result.stderr
    assert len(whisper.loads) == 1


def test_transcribe_cuda_without_device_exits_2(
    fake_env: Path, whisper: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_ASR_DEVICE", "cuda")
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 2
    assert "EARMARK_ASR_DEVICE" in result.stderr


# -- transcribe with the remote engine (step 09) -----------------------------------------


class RemoteServer:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.reply = httpx2.Response(
            200,
            json={
                "text": "Hello there second",
                "duration": 70.0,
                "segments": [
                    {"start": 0.0, "end": 4.0, "text": " Hello there"},
                    {"start": 65.0, "end": 70.0, "text": "second"},
                ],
            },
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply


@pytest.fixture
def remote_server(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> RemoteServer:
    from earmark.transcribe import remote

    server = RemoteServer()

    class MockedEngine(remote.RemoteAsrEngine):
        def __init__(self, options: remote.RemoteOptions) -> None:
            super().__init__(options, transport=httpx2.MockTransport(server))

    monkeypatch.setattr(remote, "RemoteAsrEngine", MockedEngine)
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "remote")
    monkeypatch.setenv("EARMARK_ASR_BASE_URL", "http://asr.test:8000/v1")
    monkeypatch.setenv("EARMARK_ASR_MODEL", "whisper-x")
    return server


def test_cli_transcribe_remote_prints_lines(fake_env: Path, remote_server: RemoteServer) -> None:
    clip = _clip(fake_env)
    result = runner.invoke(app, ["transcribe", str(clip)])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "[00:00:00] Hello there\n[00:01:05] second\n"
    assert clip.read_bytes() == b"audio bytes"
    assert not (fake_env / "data").exists()
    assert len(remote_server.requests) == 1
    assert str(remote_server.requests[0].url) == "http://asr.test:8000/v1/audio/transcriptions"


def test_cli_transcribe_remote_warning_on_stderr(
    fake_env: Path, remote_server: RemoteServer
) -> None:
    remote_server.reply = httpx2.Response(200, json={"text": "just text"})
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 0, result.stderr
    assert "warning: the ASR server returned no segment timestamps" in result.stderr
    assert result.stdout == "[00:00:00] just text\n"


def test_cli_transcribe_remote_errors(fake_env: Path, remote_server: RemoteServer) -> None:
    remote_server.reply = httpx2.Response(503)
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 1
    assert "(temporary; try again later)" in result.stderr
    remote_server.reply = httpx2.Response(404)
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 1
    assert "HTTP 404" in result.stderr
    assert "temporary" not in result.stderr


def test_cli_transcribe_remote_without_url_exits_2(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "remote")
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 2
    assert "EARMARK_ASR_BASE_URL" in result.stderr
    assert not (fake_env / "data").exists()


@pytest.mark.usefixtures("restore_root_logging")
def test_worker_remote_without_url_exits_2(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "remote")
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "EARMARK_ASR_BASE_URL" in result.stderr
    assert not (fake_env / "data").exists()


# -- analyze ------------------------------------------------------------------------------

ANALYZE_SENTINEL = "sk-ant-cli-sentinel-91ab"


def _stored_episode(root: Path) -> Episode:
    from earmark.artifacts import ArtifactStore

    store = ArtifactStore(root / "data")
    episode = make_episode()
    store.write_episode(episode)
    store.write_transcript(make_transcript())
    return episode


def _analyze_env(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch, provider: str = "fake"
) -> Episode:
    monkeypatch.setenv("EARMARK_DATA_DIR", str(fake_env / "data"))
    monkeypatch.setenv("EARMARK_LLM_PROVIDER", provider)
    return _stored_episode(fake_env)


def test_analyze_prints_rendered_analysis(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.analyze.render import render_analysis
    from earmark.artifacts import ArtifactStore
    from earmark.models import prompt_hash

    episode = _analyze_env(fake_env, monkeypatch)
    result = runner.invoke(app, ["analyze", episode.key])
    assert result.exit_code == 0, result.stderr
    store = ArtifactStore(fake_env / "data")
    analysis = store.read_analysis(episode.key, prompt_hash(None, "fake-llm"))
    assert analysis is not None
    assert result.stdout.splitlines() == render_analysis(analysis).splitlines()
    assert result.stdout.startswith("## Default analysis")


def test_analyze_json(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    episode = _analyze_env(fake_env, monkeypatch)
    result = runner.invoke(app, ["analyze", episode.key, "--json"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["episode_key"] == episode.key


def test_analyze_reuses_cache(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.artifacts import ArtifactStore
    from earmark.models import prompt_hash

    episode = _analyze_env(fake_env, monkeypatch)
    assert runner.invoke(app, ["analyze", episode.key]).exit_code == 0
    path = ArtifactStore(fake_env / "data").analysis_path(
        episode.key, prompt_hash(None, "fake-llm")
    )
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert runner.invoke(app, ["analyze", episode.key]).exit_code == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_analyze_prompt(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.artifacts import ArtifactStore
    from earmark.models import prompt_hash

    episode = _analyze_env(fake_env, monkeypatch)
    result = runner.invoke(app, ["analyze", episode.key, "--prompt", "  Why?  "])
    assert result.exit_code == 0, result.stderr
    store = ArtifactStore(fake_env / "data")
    analysis = store.read_analysis(episode.key, prompt_hash("Why?", "fake-llm"))
    assert analysis is not None
    assert analysis.prompt == "Why?"


def test_analyze_openai_compat_missing_settings(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode = _analyze_env(fake_env, monkeypatch, provider="openai_compat")
    result = runner.invoke(app, ["analyze", episode.key])
    assert result.exit_code == 2
    assert "EARMARK_LLM_BASE_URL" in result.stderr
    assert "EARMARK_LLM_CONTEXT_TOKENS" in result.stderr


def test_analyze_bad_key(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _analyze_env(fake_env, monkeypatch)
    result = runner.invoke(app, ["analyze", "nothex"])
    assert result.exit_code == 2
    assert "earmark analyze: not an episode key: nothex" in result.stderr


def test_analyze_prompt_too_long(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    episode = _analyze_env(fake_env, monkeypatch)
    result = runner.invoke(app, ["analyze", episode.key, "--prompt", "x" * (MAX_PROMPT_CHARS + 1)])
    assert result.exit_code == 2
    assert "prompt is too long" in result.stderr


def test_analyze_missing_transcript(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_DATA_DIR", str(fake_env / "data"))
    monkeypatch.setenv("EARMARK_LLM_PROVIDER", "fake")
    key = make_episode().key
    result = runner.invoke(app, ["analyze", key])
    assert result.exit_code == 1
    assert key in result.stderr
    assert "share the episode first" in result.stderr


def test_analyze_anthropic_without_credentials(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from earmark.analyze.llm.anthropic import NO_CREDENTIALS

    episode = _analyze_env(fake_env, monkeypatch, provider="anthropic")
    result = runner.invoke(app, ["analyze", episode.key])
    assert result.exit_code == 2
    assert NO_CREDENTIALS in result.stderr


@pytest.mark.parametrize(
    ("error", "exit_code", "temporary"),
    [(TransientError("api is down"), 1, True), (PermanentError("api said no"), 1, False)],
)
def test_analyze_transient_and_permanent(
    fake_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    exit_code: int,
    temporary: bool,
) -> None:
    from earmark.analyze.llm.anthropic import AnthropicClient
    from earmark.analyze.llm.base import LLMRequest

    def boom(self: AnthropicClient, request: LLMRequest, /) -> int:
        raise error

    monkeypatch.setattr(AnthropicClient, "count_tokens", boom)
    monkeypatch.setenv("EARMARK_LLM_API_KEY", ANALYZE_SENTINEL)
    episode = _analyze_env(fake_env, monkeypatch, provider="anthropic")
    result = runner.invoke(app, ["analyze", episode.key])
    assert result.exit_code == exit_code
    assert f"earmark analyze: {error}" in result.stderr
    assert ("(temporary; try again later)" in result.stderr) is temporary
    assert ANALYZE_SENTINEL not in result.stdout + result.stderr


def test_analyze_never_prints_key(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_LLM_API_KEY", ANALYZE_SENTINEL)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    episode = _analyze_env(fake_env, monkeypatch, provider="anthropic")
    monkeypatch.setenv("EARMARK_LOG_LEVEL", "DEBUG")
    result = runner.invoke(app, ["analyze", episode.key])
    assert result.exit_code == 1
    assert "could not reach the Anthropic API" in result.stderr
    assert ANALYZE_SENTINEL not in result.stdout + result.stderr


def test_analyze_warnings_on_stderr(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from earmark.artifacts import ArtifactStore
    from earmark.models import prompt_hash

    episode = _analyze_env(fake_env, monkeypatch)
    store = ArtifactStore(fake_env / "data")
    warned = make_analysis(prompt=None, model="fake-llm")
    warned = warned.model_copy(
        update={"episode_key": episode.key, "warnings": ("something\x07odd happened",)}
    )
    assert warned.prompt_hash == prompt_hash(None, "fake-llm")
    store.write_analysis(warned)
    result = runner.invoke(app, ["analyze", episode.key])
    assert result.exit_code == 0, result.stderr
    assert "earmark analyze: warning: something?odd happened" in result.stderr
    assert "warning" not in result.stdout


def _vault_share(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = fake_env / "vault"
    vault.mkdir(exist_ok=True)
    monkeypatch.setenv("EARMARK_STORE_BACKEND", "vault")
    monkeypatch.setenv("EARMARK_DATA_DIR", str(fake_env / "data"))
    _submit(SHARE)
    assert runner.invoke(app, ["worker", "--once"]).exit_code == 0
    return vault


def test_entities_rebuild(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _vault_share(fake_env, monkeypatch)
    (vault / "Earmark" / "aliases.yaml").write_text("foods: {}\n")
    result = runner.invoke(app, ["entities", "rebuild"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "rebuilt the entity registry: 1 episodes, 5 entities; 0 notes written\n"
    assert result.stderr.startswith(
        "earmark entities rebuild: warning: aliases.yaml line 1: unknown kind"
    )


def test_entities_rebuild_needs_vault_dir(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EARMARK_VAULT_DIR")
    result = runner.invoke(app, ["entities", "rebuild"])
    assert result.exit_code == 2
    assert "EARMARK_VAULT_DIR" in result.stderr


def test_entities_rebuild_bad_aliases(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _vault_share(fake_env, monkeypatch)
    (vault / "Earmark" / "aliases.yaml").write_text("people: [oops\n")
    result = runner.invoke(app, ["entities", "rebuild"])
    assert result.exit_code == 1
    assert "earmark entities rebuild: aliases.yaml is not valid YAML at line 2" in result.stderr


def test_entities_rebuild_refused(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    _vault_share(fake_env, monkeypatch)
    shutil.rmtree(fake_env / "data" / "episodes")
    result = runner.invoke(app, ["entities", "rebuild"])
    assert result.exit_code == 1
    assert "--prune" in result.stderr
    assert runner.invoke(app, ["entities", "rebuild", "--prune"]).exit_code == 0


# -- search, reindex, and serve's searcher ------------------------------------------------

KEY = "a" * 20


def _patch_searcher(monkeypatch: pytest.MonkeyPatch, searcher: FakeSearcher | None) -> None:
    monkeypatch.setattr("earmark.pipeline.factory.build_searcher", lambda settings: searcher)


@pytest.mark.usefixtures("restore_root_logging")
def test_search_prints_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = FakeSearcher(
        [
            make_hit(1),
            make_hit(2, kind="summary", start_s=None, timestamp=None, note_path=None),
        ]
    )
    _patch_searcher(monkeypatch, searcher)
    result = runner.invoke(app, ["search", "fake claim", "--limit", "5", "--kind", "claim"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "1. 0.8312  Fake Show \u2014 Fake episode  [00:01:05]  claim",
        "   The fake episode makes a claim.",
        "   Earmark/Podcasts/x.md",
        "2. 0.8312  Fake Show \u2014 Fake episode  summary",
        "   The fake episode makes a claim.",
    ]
    assert searcher.calls == [SearchQuery("fake claim", 5, ("claim",), None)]


@pytest.mark.usefixtures("restore_root_logging")
def test_search_json(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = FakeSearcher([make_hit(1)])
    _patch_searcher(monkeypatch, searcher)
    result = runner.invoke(app, ["search", "x", "--json", "--episode", KEY])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["index_exists"] is True
    assert data["hits"][0]["timestamp"] == "00:01:05"
    assert searcher.calls[0].episode_key == KEY


@pytest.mark.usefixtures("restore_root_logging")
def test_search_no_results_and_empty_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_searcher(monkeypatch, FakeSearcher())
    assert runner.invoke(app, ["search", "x"]).stdout == "no results\n"
    _patch_searcher(monkeypatch, FakeSearcher(index_exists=False))
    out = runner.invoke(app, ["search", "x"]).stdout
    assert out.startswith("the search index is empty")
    assert "`earmark reindex`" in out


@pytest.mark.usefixtures("restore_root_logging")
def test_search_cleans_terminal_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_searcher(
        monkeypatch, FakeSearcher([make_hit(1, snippet="evil \x1b[31mred", show_title="a\x07b")])
    )
    result = runner.invoke(app, ["search", "x"])
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout


@pytest.mark.usefixtures("restore_root_logging")
def test_search_mismatch_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = FakeSearcher()
    searcher.fail_next(IndexMismatchError("built with another model; run `earmark reindex`"))
    _patch_searcher(monkeypatch, searcher)
    result = runner.invoke(app, ["search", "x"])
    assert result.exit_code == 1
    assert "earmark reindex" in result.stderr
    assert result.stdout == ""


@pytest.mark.usefixtures("restore_root_logging")
def test_search_transient_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = FakeSearcher()
    searcher.fail_next(TransientError("the embedding model is not downloaded yet"))
    _patch_searcher(monkeypatch, searcher)
    result = runner.invoke(app, ["search", "x"])
    assert result.exit_code == 1
    assert "temporary; try again later" in result.stderr


@pytest.mark.usefixtures("restore_root_logging")
@pytest.mark.parametrize(
    "args", [["  "], ["x", "--limit", "0"], ["x", "--kind", "entity"], ["x", "--episode", "ZZ"]]
)
def test_search_invalid_input_exits_2(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    searcher = FakeSearcher()
    _patch_searcher(monkeypatch, searcher)
    result = runner.invoke(app, ["search", *args])
    assert result.exit_code == 2
    assert searcher.calls == []


@pytest.mark.usefixtures("restore_root_logging")
def test_search_off_and_config_errors_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_searcher(monkeypatch, None)
    off = runner.invoke(app, ["search", "x"])
    assert (off.exit_code, "EARMARK_INDEX_BACKEND=none" in off.stderr) == (2, True)

    def boom(settings: Settings) -> None:
        raise ConfigError(["EARMARK_EMBED_MODEL: 'x' is not a fastembed text embedding model"])

    monkeypatch.setattr("earmark.pipeline.factory.build_searcher", boom)
    bad = runner.invoke(app, ["search", "x"])
    assert bad.exit_code == 2
    assert "EARMARK_EMBED_MODEL" in bad.stderr


@pytest.mark.usefixtures("restore_root_logging")
def test_search_without_extra_exits_2_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_extra_missing(monkeypatch, "lancedb")
    result = runner.invoke(app, ["search", "x"])
    assert result.exit_code == 2
    assert "uv sync --extra index" in result.stderr
    assert not (tmp_path / "data").exists()


@pytest.fixture
def reindex_env(monkeypatch: pytest.MonkeyPatch, fake_env: Path) -> RecordingTarget:
    monkeypatch.setenv("EARMARK_INDEX_BACKEND", "lancedb")
    target = RecordingTarget()
    monkeypatch.setattr(
        "earmark.index.lancedb_index.build_lancedb_index",
        lambda settings, *, allow_download=True, embedder=None: target,
    )
    return target


def test_reindex_requires_lancedb(fake_env: Path) -> None:
    result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 2
    assert "EARMARK_INDEX_BACKEND is 'fake'" in result.stderr


def test_reindex_reports(
    reindex_env: RecordingTarget, fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_STORE_BACKEND", "vault")
    (fake_env / "vault").mkdir()  # empty: no note is found for either episode
    store = ArtifactStore(fake_env / "data")
    for guid in ("urn:c:1", "urn:c:2"):
        docs = make_docs(guid=guid)
        store.write_episode(docs.episode)
        store.write_transcript(docs.transcript)
        store.write_analysis(docs.analyses[0])
    incomplete = make_docs(guid="urn:c:3")
    store.write_episode(incomplete.episode)
    result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "reindexed 2 episodes (6 rows); skipped 1 incomplete episodes\n"
    assert result.stderr.count("earmark reindex: warning: ") == 2
    assert reindex_env.calls[0] == "reset"
    assert reindex_env.calls[-1] == "optimize"


def test_reindex_bad_episode_exits_2(reindex_env: RecordingTarget) -> None:
    result = runner.invoke(app, ["reindex", "--episode", "nope"])
    assert result.exit_code == 2
    assert "--episode" in result.stderr


def test_reindex_missing_episode_exits_1(reindex_env: RecordingTarget) -> None:
    result = runner.invoke(app, ["reindex", "--episode", KEY])
    assert result.exit_code == 1
    assert "has no transcript or analyses" in result.stderr


def test_reindex_errors(
    reindex_env: RecordingTarget, fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(fake_env / "data")
    docs = make_docs()
    store.write_episode(docs.episode)
    store.write_transcript(docs.transcript)
    store.write_analysis(docs.analyses[0])
    key = docs.episode.key
    reindex_env.fail_on[key] = TransientError("the search index disk is full")
    transient = runner.invoke(app, ["reindex", "--episode", key])
    assert transient.exit_code == 1
    assert "(temporary; try again later)" in transient.stderr
    reindex_env.fail_on[key] = IndexMismatchError(
        "the search index was built with x; run `earmark reindex` to rebuild it"
    )
    mismatch = runner.invoke(app, ["reindex", "--episode", key])
    assert mismatch.exit_code == 1
    assert "`earmark reindex` (without --episode) to rebuild it" in mismatch.stderr
    reindex_env.fail_on[key] = OSError("disk exploded")
    assert runner.invoke(app, ["reindex", "--episode", key]).exit_code == 1


def test_reindex_without_vault_exits_2(
    reindex_env: RecordingTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_STORE_BACKEND", "vault")
    monkeypatch.delenv("EARMARK_VAULT_DIR")
    result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 2
    assert "EARMARK_VAULT_DIR" in result.stderr


@pytest.mark.usefixtures("restore_root_logging")
def test_serve_starts_when_search_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    fake_uvicorn: _UvicornCalls,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def missing(settings: Settings) -> None:
        raise IndexExtraMissing(["EARMARK_INDEX_BACKEND: needs the 'index' extra"])

    monkeypatch.setattr("earmark.pipeline.factory.build_searcher", missing)
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.stderr
    assert len(fake_uvicorn.calls) == 1
    assert any("search is unavailable" in r.getMessage() for r in caplog.records)
    api = fake_uvicorn.calls[0][0][0]
    with TestClient(api, headers={"Authorization": f"Bearer {SERVE_TOKEN}"}) as client:
        response = client.get("/search", params={"q": "x"})
    assert response.status_code == 503
    assert "needs the 'index' extra" in response.json()["detail"]


@pytest.mark.usefixtures("restore_root_logging")
def test_serve_with_index_none(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls
) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    monkeypatch.setenv("EARMARK_INDEX_BACKEND", "none")
    assert runner.invoke(app, ["serve"]).exit_code == 0
    with TestClient(
        fake_uvicorn.calls[0][0][0], headers={"Authorization": f"Bearer {SERVE_TOKEN}"}
    ) as client:
        response = client.get("/search", params={"q": "x"})
    assert response.status_code == 503
    assert response.json() == {"detail": "search is turned off (EARMARK_INDEX_BACKEND=none)"}


@pytest.mark.usefixtures("restore_root_logging")
def test_serve_wires_the_searcher(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls
) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    monkeypatch.setenv("EARMARK_INDEX_BACKEND", "fake")
    assert runner.invoke(app, ["serve"]).exit_code == 0
    with TestClient(
        fake_uvicorn.calls[0][0][0], headers={"Authorization": f"Bearer {SERVE_TOKEN}"}
    ) as client:
        assert client.get("/search", params={"q": "x"}).json() == {"hits": []}


# -- doctor ------------------------------------------------------------------------------------


@pytest.fixture
def doctor_env(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (fake_env / "vault").mkdir()
    monkeypatch.setenv("EARMARK_API_TOKEN", "d" * 40)
    monkeypatch.setenv("EARMARK_INDEX_BACKEND", "none")
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "none")
    return fake_env


def test_doctor_all_fake_exit_0(doctor_env: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[-1].startswith("doctor: all checks passed")
    assert any(line.startswith("warn  resolver") for line in lines)
    assert not (doctor_env / "data").exists()
    assert list((doctor_env / "vault").iterdir()) == []


def test_doctor_failure_exit_1(doctor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EARMARK_API_TOKEN")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL  api token" in result.stdout
    assert result.stdout.splitlines()[-1] == "doctor: 1 check failed, 5 warnings"


def test_doctor_bad_settings_exit_2_no_echo(
    doctor_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_PORT", "sentinel-port-x")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2
    assert result.stdout.startswith("FAIL  config         EARMARK_PORT: ")
    assert "sentinel-port-x" not in result.stdout + result.stderr


def test_doctor_never_prints_secrets(doctor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "EARMARK_API_TOKEN": "sentinel-api-token-" + "a" * 30,
        "EARMARK_ASR_API_KEY": "sentinel-asr-key-77",
        "EARMARK_LLM_API_KEY": "sentinel-llm-key-88",
        "EARMARK_NTFY_TOKEN": "sentinel-ntfy-token-99",
        "EARMARK_NTFY_URL": "https://ntfy.test/sentinel-topic-91x",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "remote")
    monkeypatch.setenv("EARMARK_ASR_BASE_URL", "http://asr.test:8000/v1")
    monkeypatch.setenv("EARMARK_LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("EARMARK_LLM_BASE_URL", "http://llm.test:8000/v1")
    monkeypatch.setenv("EARMARK_LLM_MODEL", "qwen")
    monkeypatch.setenv("EARMARK_LLM_CONTEXT_TOKENS", "32768")
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "ntfy")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ntfy at ntfy.test, token set" in result.stdout
    output = result.stdout + result.stderr
    for value in secrets.values():
        assert value not in output
    assert "sentinel-topic-91x" not in output

    # Online, through run_doctor and render, with servers that echo secrets in their errors.
    from earmark.config import load_settings
    from earmark.doctor import Probes, run_doctor
    from earmark.doctor.report import doctor_secrets, render

    def echo(request: httpx2.Request) -> httpx2.Response:
        body = {"error": f"denied {request.headers.get('authorization')} for sentinel-topic-91x"}
        return httpx2.Response(500, json=body)

    settings = load_settings(env_file=None)
    probes = Probes(http_transport=httpx2.MockTransport(echo))
    results = run_doctor(settings, online=True, probes=probes)
    text = "\n".join(render(results, doctor_secrets(settings)))
    assert "FAIL  asr server" in text
    for value in secrets.values():
        assert value not in text
    assert "sentinel-topic-91x" not in text
    assert "denied Bearer **********" in text  # the echo reached a detail, redacted


def test_doctor_online_cli_unreachable(doctor_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "ntfy")
    monkeypatch.setenv("EARMARK_NTFY_URL", "https://ntfy.test/sentinel-topic-12")
    result = runner.invoke(app, ["doctor", "--online"])
    assert result.exit_code == 1
    assert "FAIL  ntfy server" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert "sentinel-topic-12" not in result.stdout + result.stderr


# -- reanalyze and episodes --------------------------------------------------------------


def _ingest_one(prompt: str | None = None) -> str:
    args = [SHARE] if prompt is None else [SHARE, "--prompt", prompt]
    _submit(*args)
    assert runner.invoke(app, ["worker", "--once"]).exit_code == 0
    keys = ArtifactStore(Path("data")).list_episode_keys()
    assert len(keys) == 1
    return keys[0]


def test_reanalyze_then_worker_once(fake_env: Path) -> None:
    key = _ingest_one()
    result = runner.invoke(app, ["reanalyze", key, "--prompt", "q2"])
    assert result.exit_code == 0, result.stderr
    match = re.fullmatch(r"([0-9a-f]{32}) queued\n", result.stdout)
    assert match
    worked = runner.invoke(app, ["worker", "--once"])
    assert worked.stdout == f"job {match.group(1)} done\n"
    assert len(ArtifactStore(Path("data")).list_analyses(key)) == 2
    [note] = (fake_env / "vault" / "Earmark" / "Podcasts").glob("*.md")
    assert note.read_text().count("\n## ") == 2


def test_reanalyze_existing_job(fake_env: Path) -> None:
    key = _ingest_one()
    first = runner.invoke(app, ["reanalyze", key]).stdout.split()[0]
    assert runner.invoke(app, ["reanalyze", key]).stdout == f"{first} queued (existing job)\n"


def test_reanalyze_bad_key_exits_2(fake_env: Path) -> None:
    result = runner.invoke(app, ["reanalyze", "nothex\x1b"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "earmark reanalyze: not an episode key: nothex?\n"
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_reanalyze_prompt_too_long_exits_2(fake_env: Path) -> None:
    key = _ingest_one()
    result = runner.invoke(app, ["reanalyze", key, "--prompt", "x" * (MAX_PROMPT_CHARS + 1)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert f"prompt is too long (at most {MAX_PROMPT_CHARS} characters)" in result.stderr
    assert "queued" not in runner.invoke(app, ["jobs"]).stdout


def test_reanalyze_unknown_episode_exits_1(fake_env: Path) -> None:
    result = runner.invoke(app, ["reanalyze", "0" * 20])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"earmark reanalyze: no episode {'0' * 20} under EARMARK_DATA_DIR; see `earmark episodes`\n"
    )
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_reanalyze_no_transcript_exits_1(fake_env: Path) -> None:
    ArtifactStore(Path("data")).write_episode(make_episode())
    key = make_episode().key
    result = runner.invoke(app, ["reanalyze", key])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"earmark reanalyze: episode {key}: this episode has no transcript yet; "
        "share it again, or wait for its job to finish\n"
    )
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_reanalyze_store_error_exits_1(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = _ingest_one()
    blocker = fake_env / "file"
    blocker.write_text("x")
    monkeypatch.setenv("EARMARK_DATA_DIR", str(blocker))
    result = runner.invoke(app, ["reanalyze", key])
    assert result.exit_code == 1
    assert result.stdout == ""


def test_episodes_text(fake_env: Path) -> None:
    key = _ingest_one()
    runner.invoke(app, ["reanalyze", key, "--prompt", "  who\n is   here? "])
    runner.invoke(app, ["worker", "--once"])
    other = ArtifactStore(Path("data"))
    bare = make_episode(guid="urn:bare", title="Bare")
    other.write_episode(bare)
    other.record_share(bare.key, make_analysis().created_at.replace(year=2020))
    out = runner.invoke(app, ["episodes"])
    assert out.exit_code == 0
    lines = out.stdout.splitlines()
    assert re.fullmatch(rf"{key}  \d{{4}}-\d\d-\d\d  .+ — .+", lines[0])
    assert sum(ln.endswith("  fake-llm  (default analysis)") for ln in lines[1:3]) == 1
    assert any(ln.endswith("  fake-llm  who is here?") for ln in lines)
    assert lines[-2].startswith(bare.key)
    assert lines[-1] == "  no transcript yet"
    paged = runner.invoke(app, ["episodes", "--limit", "1"]).stdout.splitlines()
    assert paged[-1] == "showing 1-1 of 2; next: --offset 1"
    last = runner.invoke(app, ["episodes", "--limit", "1", "--offset", "1"]).stdout.splitlines()
    assert last[-1] == "showing 2-2 of 2"
    assert (
        runner.invoke(app, ["episodes", "--offset", "5"]).stdout
        == "no episodes at offset 5 (total 2)\n"
    )


def test_episodes_json(fake_env: Path) -> None:
    from earmark.episodes import list_episodes

    _ingest_one("p")
    result = runner.invoke(app, ["episodes", "--json"])
    expected = list_episodes(ArtifactStore(Path("data"))).model_dump_json()
    assert result.stdout == expected + "\n"


def test_episodes_empty(fake_env: Path) -> None:
    result = runner.invoke(app, ["episodes"])
    assert result.stdout == "no episodes\n"
    assert not Path("data/episodes").exists()


def test_episodes_cleans_control_chars(fake_env: Path) -> None:
    ArtifactStore(Path("data")).write_episode(make_episode(title="red \x1b[31mtext"))
    out = runner.invoke(app, ["episodes"]).stdout
    assert "\x1b" not in out
    assert "red ?[31mtext" in out


@pytest.mark.parametrize("limit", ["0", "201"])
def test_episodes_bad_limit_exits_2(fake_env: Path, limit: str) -> None:
    assert runner.invoke(app, ["episodes", "--limit", limit]).exit_code == 2


def test_episodes_unreadable_root_exits_1(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: ArtifactStore) -> list[str]:
        raise PermissionError("no")

    monkeypatch.setattr(ArtifactStore, "list_episode_keys", boom)
    result = runner.invoke(app, ["episodes"])
    assert result.exit_code == 1
    assert result.stderr.startswith("earmark episodes: the episode files")
    assert result.stderr.endswith("(temporary; try again later)\n")


def test_episodes_module_is_light() -> None:
    code = (
        "import sys, earmark.episodes\n"
        "heavy = {'fastapi', 'starlette', 'uvicorn', 'httpx2', 'anthropic'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"
