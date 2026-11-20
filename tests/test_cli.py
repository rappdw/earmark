import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import earmark
from earmark.cli import app
from earmark.config import Settings, env_var
from earmark.jobs import open_job_store
from earmark.models import MAX_PROMPT_CHARS
from earmark.pipeline.worker import Worker
from earmark.resolve.apple import MESSAGES, UrlProblem

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
def test_config_redacts_url_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARMARK_NTFY_URL", "https://bob:sentinel-pw-1@ntfy.example/t")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "https://bob:****@ntfy.example/t" in result.output
    assert "sentinel-pw-1" not in result.output


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
        "         'faster_whisper', 'ctranslate2', 'lancedb', 'fastembed', 'anthropic'}\n"
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
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "faster_whisper")
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 2
    assert "EARMARK_ASR_BACKEND" in result.stderr
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
