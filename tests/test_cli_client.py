"""The CLI in client mode (EARMARK_API_URL): jobs, submit and transcript over the real API app."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from typer.testing import CliRunner, Result

from earmark.api.app import create_app
from earmark.api.schemas import JobView
from earmark.api_client import NO_TOKEN
from earmark.cli import app
from earmark.config import Settings
from earmark.jobs import open_job_store
from earmark.resolve.apple import MESSAGES
from earmark.transcripts import NOT_YET
from tests.api_client_helpers import (
    API_URL,
    CLIENT_TOKEN,
    RecordingTransport,
    api_settings,
    in_process_transport,
    use_transport,
)
from tests.factories import TO, new_podcast_job
from tests.mail_helpers import parse_mail
from tests.uds_http import asgi_on_uds

runner = CliRunner()

SHARE = "https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4"
SHOW_LINK = "https://podcasts.apple.com/us/podcast/x/id1200361736"
DIRECT = "https://audio.example.net/p/clip.mp3?sig=Q"
UNKNOWN = "a" * 32
LOOPBACK_URL = "http://127.0.0.1:8787"
BACKEND_VARS = (
    "RESOLVER_BACKEND",
    "FETCH_BACKEND",
    "YOUTUBE_BACKEND",
    "ASR_BACKEND",
    "MAIL_BACKEND",
    "NOTIFY_BACKEND",
)

pytestmark = pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    for name in BACKEND_VARS:
        monkeypatch.setenv(f"EARMARK_{name}", "fake")
    monkeypatch.setenv("EARMARK_ALLOWED_RECIPIENTS", TO)
    monkeypatch.setenv("EARMARK_API_TOKEN", CLIENT_TOKEN)
    monkeypatch.setenv("EARMARK_LOG_LEVEL", "ERROR")
    yield tmp_path
    assert not (tmp_path / "client-side").exists(), "client mode must not touch EARMARK_DATA_DIR"


def local(*args: str, env: dict[str, str | None] | None = None) -> Result:
    """Run a command in local mode: the server's data directory is ./data."""
    return runner.invoke(app, list(args), env={"EARMARK_API_URL": None, **(env or {})})


def remote(*args: str, url: str = API_URL, env: dict[str, str | None] | None = None) -> Result:
    """Run a command in client mode, with a data directory that must never appear."""
    client_env: dict[str, str | None] = {
        "EARMARK_API_URL": url,
        "EARMARK_DATA_DIR": str(Path.cwd() / "client-side"),
        **(env or {}),
    }
    return runner.invoke(app, list(args), env=client_env)


@dataclass
class Served:
    transport: RecordingTransport
    calls: list[int]


@pytest.fixture
def served(root: Path, monkeypatch: pytest.MonkeyPatch) -> Served:
    transport = RecordingTransport(in_process_transport(create_app(api_settings(root / "data"))))
    return Served(transport, use_transport(monkeypatch, lambda: transport))


def submit_local(*extra: str, source: str = SHARE) -> str:
    result = local("submit", source, "--to", TO, *extra)
    assert result.exit_code == 0, result.stderr
    return result.stdout.split()[0]


def run_worker() -> str:
    result = local("worker", "--once")
    assert result.exit_code == 0, result.stderr
    return result.stdout


def episode_dir(root: Path) -> Path:
    [transcript] = (root / "data").glob("episodes/*/transcript.json")
    return transcript.parent


# -- success ------


def test_client_jobs_lists(root: Path, served: Served) -> None:
    first = submit_local()
    second = submit_local(source=DIRECT)
    run_worker()
    text = remote("jobs")
    assert text.exit_code == 0, text.stderr
    assert len(text.stdout.splitlines()) == 2
    as_json = remote("jobs", "--json")
    views = [JobView.model_validate(j) for j in json.loads(as_json.stdout)["jobs"]]
    assert {v.id for v in views} == {first, second}
    assert [r[1].split("?")[0] for r in served.transport.requests] == [f"{API_URL}/jobs"] * 2


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_client_job_by_id(root: Path, served: Served, as_json: bool) -> None:
    job_id = submit_local("--instructions", "q")
    submit_local(source=DIRECT)
    result = remote("jobs", job_id, *(["--json"] if as_json else []))
    assert result.exit_code == 0, result.stderr
    if as_json:
        view = JobView.model_validate_json(result.stdout)
        assert (view.id, view.instructions) == (job_id, "q")
    else:
        [line] = result.stdout.splitlines()
        assert line.startswith(f"{job_id}  queued")


def test_client_submit_url(root: Path, served: Served) -> None:
    args = ("submit", DIRECT, "--to", TO, "--instructions", "Be brief", "--title", "A clip")
    first = remote(*args)
    assert first.exit_code == 0, first.stderr
    assert re.fullmatch(r"[0-9a-f]{32} queued\n", first.stdout)
    again = remote(*args)
    assert again.stdout == first.stdout.replace("\n", " (existing job)\n")
    [job] = json.loads(local("jobs", "--json").stdout)["jobs"]
    assert (job["to"], job["instructions"], job["title"]) == (TO, "Be brief", "A clip")
    assert job["id"] == first.stdout.split()[0]


def test_client_submit_file(root: Path, served: Served) -> None:
    clip = root / "My Clip.m4a"
    clip.write_bytes(b"ID3" + bytes(5000))
    result = remote("submit", str(clip), "--to", TO, "--instructions", "Summarise")
    assert result.exit_code == 0, result.stderr
    job_id = result.stdout.split()[0]
    [staged] = list((root / "data" / "uploads").iterdir())
    assert staged.read_bytes() == clip.read_bytes()
    [job] = json.loads(local("jobs", "--json").stdout)["jobs"]
    assert (job["id"], job["source"], job["instructions"]) == (job_id, "upload", "Summarise")
    assert run_worker() == f"job {job_id} done\n"


def test_client_submit_ignores_local_allowlist(root: Path, served: Served) -> None:
    result = remote("submit", SHARE, "--to", TO, env={"EARMARK_ALLOWED_RECIPIENTS": None})
    assert result.exit_code == 0, result.stderr


@pytest.mark.parametrize("fmt", ["note", "text", "json"])
def test_client_transcript(root: Path, served: Served, fmt: str) -> None:
    job_id = submit_local("--instructions", "INSTR-TOKEN")
    run_worker()
    result = remote("transcript", job_id, "--format", fmt)
    assert result.exit_code == 0, result.stderr
    if fmt == "note":
        mailed = parse_mail((root / "data" / "outbox" / f"{job_id}.eml").read_bytes())
        assert result.stdout_bytes == mailed.note
    elif fmt == "text":
        assert result.stdout_bytes == (episode_dir(root) / "transcript.txt").read_bytes()
    else:
        assert json.loads(result.stdout_bytes)["job_id"] == job_id
    assert result.stdout_bytes == local("transcript", job_id, "--format", fmt).stdout_bytes


def test_client_mode_over_real_http(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = root / "memo.m4a"
    clip.write_bytes(b"ID3" + bytes(300_000))
    with asgi_on_uds(create_app(api_settings(root / "data")), log_config=None) as path:
        use_transport(monkeypatch, lambda: httpx2.HTTPTransport(uds=str(path)))
        link = remote("submit", SHARE, "--to", TO, url=LOOPBACK_URL)
        upload = remote("submit", str(clip), "--to", TO, url=LOOPBACK_URL)
        assert (link.exit_code, upload.exit_code) == (0, 0), (link.stderr, upload.stderr)
        assert [staged.stat().st_size for staged in (root / "data" / "uploads").iterdir()] == [
            clip.stat().st_size
        ]
        run_worker()
        run_worker()
        done = link.stdout.split()[0]
        listed = remote("jobs", url=LOOPBACK_URL)
        assert listed.exit_code == 0
        assert listed.stdout.count("  done  ") == 2
        one = remote("jobs", done, "--json", url=LOOPBACK_URL)
        assert JobView.model_validate_json(one.stdout).status == "done"
        note = remote("transcript", done, url=LOOPBACK_URL)
        assert note.exit_code == 0
        assert note.stdout_bytes == local("transcript", done).stdout_bytes


def test_client_upload_over_cap_413(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = root / "big.m4a"
    clip.write_bytes(b"ID3" + bytes(5000))
    settings = api_settings(root / "data")
    with asgi_on_uds(create_app(settings, max_upload_bytes=1000), log_config=None) as path:
        use_transport(monkeypatch, lambda: httpx2.HTTPTransport(uds=str(path)))
        result = remote("submit", str(clip), "--to", TO, url=LOOPBACK_URL)
    assert result.exit_code == 2
    assert result.stderr == (
        "earmark submit: the upload is larger than the 1000 bytes limit (EARMARK_MAX_UPLOAD_MB)\n"
    )


# -- the API's refusals ------

COMMANDS = {
    "jobs": ["jobs"],
    "jobs <id>": ["jobs", UNKNOWN],
    "submit": ["submit", SHARE, "--to", TO],
    "transcript": ["transcript", UNKNOWN],
}


@pytest.mark.parametrize("name", list(COMMANDS))
def test_client_wrong_token_401(root: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    wrong = RecordingTransport(
        in_process_transport(create_app(api_settings(root / "data", token="the-server-token")))
    )
    use_transport(monkeypatch, lambda: wrong)
    result = remote(*COMMANDS[name])
    assert result.exit_code == 1
    command = COMMANDS[name][0]
    assert result.stderr.splitlines()[-1] == (
        f"earmark {command}: missing or invalid API token (check EARMARK_API_TOKEN)"
    )
    assert result.stdout == ""


@pytest.mark.parametrize("command", ["jobs", "transcript"])
def test_client_unknown_job_404(root: Path, served: Served, command: str) -> None:
    result = remote(command, UNKNOWN)
    assert (result.exit_code, result.stdout) == (1, "")
    assert result.stderr == f"earmark {command}: job not found\n"


@pytest.mark.parametrize("bad", ["../healthz", "A" * 32, "a" * 31])
@pytest.mark.parametrize("command", ["jobs", "transcript"])
def test_client_malformed_id_makes_no_request(
    root: Path, served: Served, command: str, bad: str
) -> None:
    result = remote(command, bad)
    assert (result.exit_code, result.stderr) == (1, f"earmark {command}: job not found\n")
    assert served.transport.requests == []


def test_client_transcript_not_ready_409(root: Path, served: Served) -> None:
    job_id = submit_local()
    result = remote("transcript", job_id)
    assert result.exit_code == 1
    assert result.stderr == f"earmark transcript: {NOT_YET.format(status='queued')}\n"


def test_client_transcript_gone_410(root: Path, served: Served) -> None:
    job_id = submit_local()
    run_worker()
    (episode_dir(root) / "transcript.json").unlink()
    result = remote("transcript", job_id)
    assert result.exit_code == 1
    assert result.stderr.startswith("earmark transcript: ")
    assert "transcript.json is missing" in result.stderr
    assert result.stderr == local("transcript", job_id).stderr


def test_client_submit_refused_recipient_422(root: Path, served: Served) -> None:
    result = remote("submit", SHARE, "--to", "stranger@example.com")
    assert result.exit_code == 2
    assert result.stderr == (
        "earmark submit: to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)\n"
    )
    assert "stranger" not in result.stdout + result.stderr


def test_client_submit_show_link_422(root: Path, served: Served) -> None:
    result = remote("submit", SHOW_LINK, "--to", TO)
    assert result.exit_code == 2
    assert MESSAGES["show_link"] in result.stderr


# -- the transport rule ------


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:8787",
        "http://earmark.example.com",
        "http://localhost:8787",
        "http://0.0.0.0:8787",
    ],
)
@pytest.mark.parametrize("command", ["jobs", "submit", "transcript"])
def test_client_refuses_plain_http(
    root: Path, monkeypatch: pytest.MonkeyPatch, url: str, command: str
) -> None:
    calls = use_transport(monkeypatch, lambda: pytest.fail("no transport expected"))
    args = {
        "jobs": ["jobs"],
        "submit": ["submit", SHARE, "--to", TO],
        "transcript": ["transcript", UNKNOWN],
    }[command]
    result = remote(*args, url=url)
    assert result.exit_code == 2
    assert "EARMARK_API_URL" in result.stderr
    assert "loopback" in result.stderr
    host = url.split("//")[1].split(":")[0]
    assert host not in result.stderr.replace("http://127.0.0.1:8787", "")
    assert calls == []
    assert not (root / "client-side").exists()


@pytest.mark.parametrize(
    "url",
    [LOOPBACK_URL, "http://[::1]:8787", "http://127.1.2.3:8787"],
    ids=["127.0.0.1", "::1", "127.1.2.3"],
)
def test_client_allows_loopback_http(root: Path, served: Served, url: str) -> None:
    result = remote("jobs", url=url)
    assert (result.exit_code, result.stdout) == (0, "no jobs\n")
    assert served.calls == [1]


def test_client_without_token_exits_2(root: Path, served: Served) -> None:
    result = remote("jobs", env={"EARMARK_API_TOKEN": None})
    assert result.exit_code == 2
    assert result.stderr == f"earmark jobs: {NO_TOKEN}\n"
    assert served.calls == []


def test_client_unreachable_exits_1(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    use_transport(monkeypatch, lambda: httpx2.MockTransport(refuse))
    result = remote("jobs", url=LOOPBACK_URL)
    assert result.exit_code == 1
    assert result.stderr.startswith(
        "earmark jobs: could not reach the API at 127.0.0.1:8787 (ConnectError)"
    )


# -- local checks before sending ------


def test_client_submit_checks_the_file_first(root: Path, served: Served) -> None:
    (root / "notes.txt").write_text("hello")
    (root / "empty.mp3").write_bytes(b"")
    text = remote("submit", str(root / "notes.txt"), "--to", TO)
    assert text.exit_code == 2
    assert text.stderr == (
        "earmark submit: notes.txt does not look like audio "
        "(use an audio file extension such as .mp3 or .m4a)\n"
    )
    empty = remote("submit", str(root / "empty.mp3"), "--to", TO)
    assert (empty.exit_code, empty.stderr) == (2, "earmark submit: empty.mp3 is empty\n")
    assert served.transport.requests == []


# -- same output as the local mode ------


@pytest.fixture
def mixed_jobs(root: Path, served: Served) -> dict[str, str]:
    store = open_job_store(Settings(_env_file=None))
    store.enqueue(new_podcast_job("https://podcasts.apple.com/us/podcast/id1?i=2"))
    claim = store.claim_next()
    assert claim is not None
    store.fail(claim, "bad \x1b[31mred\x1b[0m\nsecond line", transient=False)
    clip = root / "clip.m4a"
    clip.write_bytes(b"ID3" + bytes(2000))
    ids = {
        "failed": claim.job.id,
        "done": submit_local("--instructions", "Summarise"),
        "direct": submit_local("--title", "T", source=DIRECT),
        "file": submit_local(source=str(clip)),
    }
    run_worker()
    run_worker()
    ids["queued"] = submit_local(source=SHARE + "&extra=1")
    return ids


def both(*args: str) -> tuple[Result, Result]:
    env: dict[str, str | None] = {"EARMARK_LOG_LEVEL": "WARNING"}
    return local(*args, env=env), remote(*args, env=env)


def same(first: Result, second: Result) -> None:
    assert (first.exit_code, first.stdout_bytes, first.stderr) == (
        second.exit_code,
        second.stdout_bytes,
        second.stderr,
    )


def _output_cases(ids: dict[str, str]) -> list[list[str]]:
    return [
        ["jobs"],
        ["jobs", "--json"],
        ["jobs", "--status", "done"],
        ["jobs", "--limit", "1"],
        ["jobs", ids["done"]],
        ["jobs", ids["done"], "--json"],
        ["jobs", ids["failed"]],
        ["jobs", ids["queued"]],
        ["jobs", UNKNOWN],
        ["transcript", ids["done"]],
        ["transcript", ids["done"], "--format", "text"],
        ["transcript", ids["done"], "--format", "json"],
        ["transcript", ids["queued"]],
        ["transcript", UNKNOWN],
    ]


def test_client_output_matches_local(mixed_jobs: dict[str, str]) -> None:
    for args in _output_cases(mixed_jobs):
        first, second = both(*args)
        same(first, second)
        assert first.exit_code in (0, 1), args
    failed = both("jobs", mixed_jobs["failed"])[1]
    assert "  error: bad ?[31mred?[0m?second line" in failed.stdout
    assert both("jobs", UNKNOWN)[1].exit_code == 1
    assert both("transcript", mixed_jobs["queued"])[1].exit_code == 1


def test_client_submit_output_matches_local(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    served_dir = tmp_path_factory.mktemp("served")
    transport = in_process_transport(create_app(api_settings(served_dir / "data")))
    use_transport(monkeypatch, lambda: transport)
    clip = root / "clip.m4a"
    clip.write_bytes(b"ID3" + bytes(2000))
    (root / "notes.txt").write_text("hi")
    (root / "empty.m4a").write_bytes(b"")
    cases = [
        ["submit", SHARE, "--to", "stranger@example.com"],
        ["submit", SHARE, "--to", TO, "--instructions", "x" * 5000],
        ["submit", str(root / "notes.txt"), "--to", TO],
        ["submit", str(root / "empty.m4a"), "--to", TO],
    ]
    for args in cases:
        local_result = local(*args)
        client_result = remote(*args, env={"EARMARK_ALLOWED_RECIPIENTS": None})
        assert (client_result.exit_code, client_result.stderr) == (
            local_result.exit_code,
            local_result.stderr,
        ), args
        assert client_result.exit_code == 2
    # a link: the same shape in both modes (the served data directory is separate)
    first = remote("submit", SHARE, "--to", TO)
    again = remote("submit", SHARE, "--to", TO)
    assert re.fullmatch(r"[0-9a-f]{32} queued\n", first.stdout)
    assert re.fullmatch(r"[0-9a-f]{32} queued \(existing job\)\n", again.stdout)
    local_first = local("submit", SHARE, "--to", TO)
    local_again = local("submit", SHARE, "--to", TO)
    assert re.fullmatch(r"[0-9a-f]{32} queued\n", local_first.stdout)
    assert re.fullmatch(r"[0-9a-f]{32} queued \(existing job\)\n", local_again.stdout)


# -- the token is never printed ------


def test_cli_client_mode_never_prints_the_token(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def echo(request: httpx2.Request) -> httpx2.Response:
        seen = request.headers.get("authorization")
        if request.url.path == "/jobs":
            return httpx2.Response(401, json={"detail": f"bad {seen}"})
        if request.method == "POST":
            return httpx2.Response(422, json={"detail": f"refused {seen}"})
        if request.url.path.endswith("/transcript"):
            return httpx2.Response(500, json={"detail": f"broken {seen}"})
        return httpx2.Response(302, headers={"Location": f"http://x.example.net/{seen}"})

    use_transport(monkeypatch, lambda: httpx2.MockTransport(echo))
    job = "b" * 32
    runs = [
        remote("jobs", env={"EARMARK_LOG_LEVEL": "DEBUG"}),
        remote("jobs", job, env={"EARMARK_LOG_LEVEL": "DEBUG"}),
        remote("submit", SHARE, "--to", TO, env={"EARMARK_LOG_LEVEL": "DEBUG"}),
        remote("transcript", job, env={"EARMARK_LOG_LEVEL": "DEBUG"}),
    ]
    assert [r.exit_code for r in runs] == [1, 1, 2, 1]
    assert "bad Bearer **********" in runs[0].stderr
    for result in runs:
        assert CLIENT_TOKEN.encode() not in result.stdout_bytes
        assert CLIENT_TOKEN not in result.stderr
