import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import earmark
from earmark.cli import app
from earmark.config import Settings, env_var

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
