import importlib.metadata
import io
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import earmark
from earmark.api.schemas import JobView
from earmark.cli import app
from earmark.config import Settings, env_var
from earmark.db import APPLICATION_ID
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fakes import FakeResolver
from earmark.jobs import open_job_store
from earmark.log import configure_logging
from earmark.models import MAX_INSTRUCTIONS_CHARS, Episode, Transcript
from earmark.pipeline import factory
from earmark.pipeline.worker import Worker
from earmark.resolve.apple import MESSAGES, UrlProblem
from earmark.transcribe import faster_whisper as fw
from earmark.transcribe.faster_whisper import AudioDecodeError
from tests.factories import TO, make_episode, new_podcast_job
from tests.legacy_db import V010_SCHEMA_VERSION, make_v010_database
from tests.uds_http import asgi_on_uds
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
    (tmp_path / ".env").write_text("EARMARK_NTFY_TOKEN=sentinel-ntfy-8d9e0f\n")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    for s in ("api-7f3a9c", "asr-1b2c3d", "ntfy-8d9e0f"):
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
        "         'faster_whisper', 'ctranslate2', 'onnxruntime', 'numpy'}\n"
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


@pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")
def test_serve_access_log_drops_query(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: _UvicornCalls
) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    assert runner.invoke(app, ["serve"]).exit_code == 0
    args, kwargs = fake_uvicorn.calls[0]
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    config = {k: v for k, v in kwargs.items() if k not in ("host", "port")}
    with (
        asgi_on_uds(args[0], **config) as path,
        httpx2.Client(
            transport=httpx2.HTTPTransport(uds=str(path)), base_url="http://earmark.test"
        ) as client,
    ):
        url = "/jobs?limit=3&private-sentinel=1"
        auth = {"Authorization": f"Bearer {SERVE_TOKEN}"}
        assert client.get(url, headers=auth).status_code == 200
        assert client.get(url).status_code == 401
    out = stream.getvalue()
    assert '"GET /jobs HTTP/1.1" 200' in out
    assert '"GET /jobs HTTP/1.1" 401' in out
    assert "private-sentinel" not in out
    assert "limit=" not in out


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
    "YOUTUBE_BACKEND",
    "ASR_BACKEND",
    "MAIL_BACKEND",
    "NOTIFY_BACKEND",
)


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore_root_logging: None) -> Path:
    for name in BACKEND_VARS:
        monkeypatch.setenv(f"EARMARK_{name}", "fake")
    monkeypatch.setenv("EARMARK_ALLOWED_RECIPIENTS", TO)
    return tmp_path


def _submit(*args: str, to: str = TO) -> str:
    result = runner.invoke(app, ["submit", *args, "--to", to])
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_submit_then_worker_once(fake_env: Path) -> None:
    out = _submit(SHARE, "--instructions", "q")
    match = re.fullmatch(r"([0-9a-f]{32}) queued\n", out)
    assert match
    job_id = match.group(1)
    worked = runner.invoke(app, ["worker", "--once"])
    assert worked.exit_code == 0, worked.stderr
    assert worked.stdout == f"job {job_id} done\n"
    listed = runner.invoke(app, ["jobs", "--json"])
    [job] = json.loads(listed.stdout)["jobs"]
    assert (job["id"], job["status"]) == (job_id, "done")
    assert len(list((fake_env / "data").glob("episodes/*/transcript.json"))) == 1


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
    result = runner.invoke(app, ["submit", url, "--to", TO])
    assert result.exit_code == 2
    assert MESSAGES[problem] in result.stderr
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_submit_rejects_long_instructions(fake_env: Path) -> None:
    args = ["submit", SHARE, "--to", TO, "--instructions", "x" * (MAX_INSTRUCTIONS_CHARS + 1)]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert (
        f"instructions is too long (at most {MAX_INSTRUCTIONS_CHARS} characters)" in result.stderr
    )
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_submit_prompt_option_is_gone(fake_env: Path) -> None:
    result = runner.invoke(app, ["submit", SHARE, "--to", TO, "--prompt", "q"])
    assert result.exit_code == 2
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_submit_requires_to(fake_env: Path) -> None:
    result = runner.invoke(app, ["submit", SHARE])
    assert result.exit_code == 2
    assert "--to" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)  # CI forces colour
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


@pytest.mark.parametrize("bad", ["not-an-address", "a@b.org, c@d.org"])
def test_submit_invalid_to_exit_2(fake_env: Path, bad: str) -> None:
    result = runner.invoke(app, ["submit", SHARE, "--to", bad])
    assert result.exit_code == 2
    assert "to must be one email address" in result.stderr
    assert bad not in result.stdout + result.stderr


def test_submit_recipient_refused_exit_2(fake_env: Path) -> None:
    result = runner.invoke(app, ["submit", SHARE, "--to", "stranger@example.com"])
    assert result.exit_code == 2
    assert "to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)" in result.stderr
    assert "stranger" not in result.stdout + result.stderr
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_submit_with_no_allowlist_is_refused(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EARMARK_ALLOWED_RECIPIENTS")
    result = runner.invoke(app, ["submit", SHARE, "--to", TO])
    assert result.exit_code == 2
    assert "no recipients are allowed yet" in result.stderr


def test_submit_url(fake_env: Path) -> None:
    out = _submit("https://audio.example.net/p/clip.mp3?sig=Q#f", "--title", "Clip")
    assert re.fullmatch(r"[0-9a-f]{32} queued\n", out)
    [job] = json.loads(runner.invoke(app, ["jobs", "--json"]).stdout)["jobs"]
    assert (job["source"], job["to"], job["title"]) == ("url", TO, "Clip")
    assert job["url"] == "https://audio.example.net/p/clip.mp3"
    assert _submit("https://audio.example.net/p/clip.mp3?sig=Q").endswith("(existing job)\n")


def test_submit_file_copies_to_staging(fake_env: Path) -> None:
    clip = fake_env / "My Clip.m4a"
    clip.write_bytes(b"ID3" + bytes(5000))
    out = _submit(str(clip), "--instructions", "Summarise")
    job_id = out.split()[0]
    [staged] = list((fake_env / "data" / "uploads").iterdir())
    assert staged.read_bytes() == clip.read_bytes()
    assert staged.suffix == ".m4a"
    assert clip.exists()  # the original is left alone
    [job] = json.loads(runner.invoke(app, ["jobs", "--json"]).stdout)["jobs"]
    assert (job["id"], job["source"], job["url"], job["instructions"]) == (
        job_id,
        "upload",
        None,
        "Summarise",
    )
    assert "upload_file" not in job
    worked = runner.invoke(app, ["worker", "--once"])
    assert worked.stdout == f"job {job_id} done\n"
    assert list((fake_env / "data" / "uploads").iterdir()) == []
    again = fake_env / "again.m4a"
    again.write_bytes(clip.read_bytes())
    second = _submit(str(again), "--instructions", "Summarise")
    assert second.endswith("done (existing job)\n")
    assert list((fake_env / "data" / "uploads").iterdir()) == []


def test_submit_file_not_audio_exit_2(fake_env: Path) -> None:
    notes = fake_env / "notes.txt"
    notes.write_text("hello")
    result = runner.invoke(app, ["submit", str(notes), "--to", TO])
    assert result.exit_code == 2
    assert result.stderr.startswith("earmark submit: notes.txt does not look like audio")
    assert not (fake_env / "data" / "uploads").exists()
    assert runner.invoke(app, ["jobs"]).stdout == "no jobs\n"


def test_submit_file_over_cap_exit_2_nothing_staged(
    fake_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARMARK_MAX_UPLOAD_MB", "1")
    big = fake_env / "big.mp3"
    big.write_bytes(bytes(1024 * 1024 + 1))
    result = runner.invoke(app, ["submit", str(big), "--to", TO])
    assert result.exit_code == 2
    assert result.stderr == (
        "earmark submit: the file is larger than the 1 MiB limit (EARMARK_MAX_UPLOAD_MB)\n"
    )
    assert list((fake_env / "data" / "uploads").iterdir()) == []
    exact = fake_env / "exact.mp3"
    exact.write_bytes(bytes(1024 * 1024))
    assert runner.invoke(app, ["submit", str(exact), "--to", TO]).exit_code == 0


def test_submit_empty_file_exit_2(fake_env: Path) -> None:
    empty = fake_env / "empty.mp3"
    empty.write_bytes(b"")
    result = runner.invoke(app, ["submit", str(empty), "--to", TO])
    assert result.exit_code == 2
    assert "is empty" in result.stderr
    assert list((fake_env / "data" / "uploads").iterdir()) == []


def test_submit_file_recipient_refused_stages_nothing(fake_env: Path) -> None:
    clip = fake_env / "clip.mp3"
    clip.write_bytes(b"ID3")
    result = runner.invoke(app, ["submit", str(clip), "--to", "stranger@example.com"])
    assert result.exit_code == 2
    assert not (fake_env / "data" / "uploads").exists()


def test_jobs_json_prints_views(fake_env: Path) -> None:
    _submit(
        "https://audio.example.net/private/PATH-TOKEN.mp3?sig=QUERY-TOKEN",
        "--instructions",
        "i" * 300,
    )
    payload = json.loads(runner.invoke(app, ["jobs", "--json"]).stdout)
    [job] = payload["jobs"]
    assert set(job) == set(JobView.model_fields)
    assert job["url"] == "https://audio.example.net/private/PATH-TOKEN.mp3"
    assert job["instructions"] == "i" * 199 + "…"
    assert job["to"] == TO
    assert "QUERY-TOKEN" not in json.dumps(payload)


def test_jobs_text_shows_upload_subject(fake_env: Path) -> None:
    clip = fake_env / "clip.mp3"
    clip.write_bytes(b"ID3")
    job_id = _submit(str(clip)).split()[0]
    [line] = runner.invoke(app, ["jobs"]).stdout.splitlines()
    assert line.split("  ")[0] == job_id
    assert line.endswith("  upload")


def test_jobs_text_output(fake_env: Path) -> None:
    first = _submit(SHARE, "--instructions", "one").split()[0]
    second = _submit(SHARE, "--instructions", "two").split()[0]
    runner.invoke(app, ["worker", "--once"])  # runs the first job
    lines = runner.invoke(app, ["jobs"]).stdout.splitlines()
    assert [ln.split("  ")[0] for ln in lines] == [second, first]
    assert lines[0].split("  ")[1] == "queued"
    assert lines[1].split("  ")[1:4] == ["done", "deliver", "1"]
    assert lines[1].endswith("  https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981")
    done = runner.invoke(app, ["jobs", "--status", "done"]).stdout.splitlines()
    assert [ln.split("  ")[0] for ln in done] == [first]
    assert len(runner.invoke(app, ["jobs", "--limit", "1"]).stdout.splitlines()) == 1
    assert runner.invoke(app, ["jobs", "--limit", "0"]).exit_code == 2
    assert runner.invoke(app, ["jobs", "--status", "bogus"]).exit_code == 2


def test_jobs_sanitizes_control_chars(fake_env: Path) -> None:
    store = open_job_store(Settings(_env_file=None))
    store.enqueue(new_podcast_job("https://podcasts.apple.com/us/podcast/id1?i=2"))
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
    whisper.decode_error = AudioDecodeError("bad")
    result = runner.invoke(app, ["transcribe", str(_clip(fake_env))])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith(
        "earmark transcribe: could not decode clip.mp3"
    )
    assert result.stdout == ""


def test_transcribe_unexpected_error_exits_1(fake_env: Path, whisper: FakeRuntime) -> None:
    message = "open() got an unexpected keyword argument 'metadata_errors'"
    whisper.decode_error = TypeError(message)
    clip = _clip(fake_env)
    before = clip.read_bytes()
    result = runner.invoke(app, ["transcribe", str(clip)])
    assert result.exit_code == 1
    last = result.stderr.splitlines()[-1]
    assert last.startswith(f"earmark transcribe: unexpected error: TypeError: {message}")
    assert "earmark doctor" in last
    assert "could not decode" not in result.stderr
    assert result.stdout == ""
    assert clip.read_bytes() == before


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


# -- doctor ------------------------------------------------------------------------------------


@pytest.fixture
def doctor_env(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EARMARK_API_TOKEN", "d" * 40)
    monkeypatch.setenv("EARMARK_NOTIFY_BACKEND", "none")
    return fake_env


def test_doctor_all_fake_exit_0(doctor_env: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[-1].startswith("doctor: all checks passed")
    assert any(line.startswith("warn  resolver") for line in lines)
    assert any(line.startswith("warn  mail") for line in lines)
    assert not (doctor_env / "data").exists()


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
        "EARMARK_NTFY_TOKEN": "sentinel-ntfy-token-99",
        "EARMARK_NTFY_URL": "https://ntfy.test/sentinel-topic-91x",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EARMARK_ASR_BACKEND", "remote")
    monkeypatch.setenv("EARMARK_ASR_BASE_URL", "http://asr.test:8000/v1")
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


# -- the command set and the old database (step 22) --------------------------------------


def test_cli_commands_are_exactly() -> None:
    names = {c.name or (c.callback.__name__ if c.callback else "") for c in app.registered_commands}
    assert names == {
        "config",
        "doctor",
        "serve",
        "worker",
        "submit",
        "jobs",
        "transcript",
        "resolve",
        "transcribe",
    }
    assert app.registered_groups == []


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.usefixtures("restore_root_logging")
@pytest.mark.parametrize(
    "args",
    [["serve"], ["worker", "--once"], ["jobs"], ["submit", SHARE, "--to", TO]],
    ids=lambda a: a[0],
)
def test_v010_database_refused_by_commands(
    fake_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_uvicorn: _UvicornCalls,
    args: list[str],
) -> None:
    monkeypatch.setenv("EARMARK_API_TOKEN", SERVE_TOKEN)
    monkeypatch.setenv("EARMARK_DATA_DIR", str(fake_env / "data"))  # absolute, so the path shows
    (fake_env / "data").mkdir()
    path = make_v010_database(fake_env / "data" / "earmark.db")
    before = _row_count(path)
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert str(path) in result.stderr
    assert "Move it aside" in result.stderr
    assert "cannot open the job store" in result.stderr
    assert fake_uvicorn.calls == []
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V010_SCHEMA_VERSION
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0
    finally:
        conn.close()
    assert _row_count(path) == before


@pytest.mark.usefixtures("restore_root_logging")
def test_fresh_database_after_moving_v010_aside(fake_env: Path) -> None:
    (fake_env / "data").mkdir()
    path = make_v010_database(fake_env / "data" / "earmark.db")
    assert runner.invoke(app, ["jobs"]).exit_code == 1
    for suffix in ("", "-wal", "-shm"):
        old = Path(f"{path}{suffix}")
        if old.exists():
            old.rename(Path(f"{path}.v010{suffix}"))
    result = runner.invoke(app, ["jobs"])
    assert result.exit_code == 0
    assert result.stdout == "no jobs\n"
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    finally:
        conn.close()


# -- transcript -----------------------------------------------------------------------------


def _shared_and_run(*extra: str) -> str:
    job_id = _submit(SHARE, "--instructions", "INSTR-TOKEN", *extra).split()[0]
    worked = runner.invoke(app, ["worker", "--once"])
    assert worked.exit_code == 0, worked.stderr
    return job_id


def _episode_dir(root: Path) -> Path:
    [transcript] = (root / "data").glob("episodes/*/transcript.json")
    return transcript.parent


def test_transcript_command_note_equals_mail(fake_env: Path) -> None:
    from tests.mail_helpers import parse_mail

    job_id = _shared_and_run()
    result = runner.invoke(app, ["transcript", job_id])
    assert result.exit_code == 0, result.stderr
    mailed = parse_mail((fake_env / "data" / "outbox" / f"{job_id}.eml").read_bytes())
    assert result.stdout_bytes == mailed.note
    assert b"INSTR-TOKEN" not in result.stdout_bytes


def test_transcript_command_formats(fake_env: Path) -> None:
    job_id = _shared_and_run()
    text = runner.invoke(app, ["transcript", job_id, "--format", "text"])
    assert text.exit_code == 0, text.stderr
    assert text.stdout_bytes == (_episode_dir(fake_env) / "transcript.txt").read_bytes()
    as_json = runner.invoke(app, ["transcript", job_id, "--format", "json"])
    assert as_json.exit_code == 0, as_json.stderr
    assert json.loads(as_json.stdout_bytes)["job_id"] == job_id


@pytest.mark.parametrize("fmt", ["note", "text", "json"])
def test_transcript_command_matches_api(fake_env: Path, fmt: str) -> None:
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from earmark.api.app import create_app

    job_id = _shared_and_run()
    cli = runner.invoke(app, ["transcript", job_id, "--format", fmt])
    settings = Settings(
        _env_file=None,
        api_token=SecretStr("t"),
        data_dir=fake_env / "data",
        allowed_recipients=(TO,),
    )
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t"}) as client:
        served = client.get(f"/jobs/{job_id}/transcript", params={"format": fmt})
    assert (cli.exit_code, served.status_code) == (0, 200)
    assert cli.stdout_bytes == served.content


def test_transcript_command_unknown_job(fake_env: Path) -> None:
    result = runner.invoke(app, ["transcript", "a" * 32])
    assert result.exit_code == 1
    assert result.stderr == "earmark transcript: job not found\n"
    assert result.stdout == ""


def test_transcript_command_not_ready(fake_env: Path) -> None:
    from earmark.transcripts import NOT_YET

    job_id = _submit(SHARE).split()[0]
    result = runner.invoke(app, ["transcript", job_id])
    assert result.exit_code == 1
    assert result.stderr == f"earmark transcript: {NOT_YET.format(status='queued')}\n"
    assert result.stdout == ""


def test_transcript_command_gone(fake_env: Path) -> None:
    job_id = _shared_and_run()
    (_episode_dir(fake_env) / "transcript.json").unlink()
    result = runner.invoke(app, ["transcript", job_id])
    assert result.exit_code == 1
    assert "transcript.json is missing" in result.stderr
    assert result.stdout == ""


def test_transcript_command_bad_format(fake_env: Path) -> None:
    result = runner.invoke(app, ["transcript", "a" * 32, "--format", "pdf"])
    assert result.exit_code == 2


def test_terminal_text() -> None:
    from earmark.cli import terminal_text

    assert terminal_text(b"a\x1b[31mb\r\n\tc\xc3\xa9\xff") == "a?[31mb?\n\tcé�"


def test_transcript_command_at_a_terminal(fake_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = _shared_and_run()
    (_episode_dir(fake_env) / "transcript.txt").write_bytes(b"[00:00:00] hi \x1b[2J there\n")
    raw = runner.invoke(app, ["transcript", job_id, "--format", "text"])
    assert raw.stdout_bytes == b"[00:00:00] hi \x1b[2J there\n"
    monkeypatch.setattr("earmark.cli._stdout_is_terminal", lambda: True)
    shown = runner.invoke(app, ["transcript", job_id, "--format", "text"])
    assert shown.stdout == "[00:00:00] hi ?[2J there\n"
