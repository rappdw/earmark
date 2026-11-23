"""Instructions, destination addresses and direct URLs stay out of logs and pushes."""

import io
import json
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.audio import HttpAudioFetcher
from earmark.config import Settings
from earmark.errors import PermanentError
from earmark.fakes import make_fake_backends
from earmark.fetch import MIB, PermanentFetchError
from earmark.jobs import NewJob
from earmark.log import configure_logging
from earmark.models import Job
from earmark.notify.ntfy import NtfyClient, NtfyNotifier, NtfyOptions
from tests.factories import new_podcast_job, new_upload_job, new_url_job
from tests.http_helpers import mock_fetcher
from tests.ntfy_stub import PUBLISHED, TOPIC_URL, json_reply
from tests.pipeline_env import CANON, PipelineEnv, make_env

pytestmark = pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")

TOKEN = "leak-token-77aa"
INSTRUCTIONS = "INSTR-TOKEN-5c1e summarise the budget"
SECRET_TO = "zelda.secret@example.org"
MASKED_TO = "z…@example.org"
OTHER_TO = "you@example.org"
DIRECT_URL = "https://audio.example.net/private/PATH-TOKEN-91d2.mp3?sig=QUERY-TOKEN-a77b"
FILENAME = "FILE-TOKEN-3b8f.m4a"
TITLE = "TITLE-TOKEN-0d4e"
URL_TOKENS = ("PATH-TOKEN", "QUERY-TOKEN", "/private/", "?sig=")


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(
        "DEBUG", (), stream=stream
    )  # no secrets to redact: our own code must be clean
    return stream


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        allowed_recipients=(OTHER_TO, SECRET_TO),
    )


def _drive_requests(env: PipelineEnv, tmp_path: Path) -> None:
    """A JSON podcast request, a JSON URL request and an upload, run to done; then a failure."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(_settings(tmp_path), store=env.jobs), headers=headers) as client:
        podcast = client.post(
            "/transcriptions",
            json={"url": CANON, "to": SECRET_TO, "instructions": INSTRUCTIONS, "title": TITLE},
        )
        direct = client.post(
            "/transcriptions",
            json={"url": DIRECT_URL, "to": SECRET_TO, "instructions": INSTRUCTIONS},
        )
        upload = client.post(
            "/transcriptions",
            files={"file": (FILENAME, b"ID3" + bytes(500), "audio/mp4")},
            data={"to": SECRET_TO, "instructions": INSTRUCTIONS, "title": TITLE},
        )
        assert [r.status_code for r in (podcast, direct, upload)] == [202, 202, 202]
        client.get("/jobs")
        for _ in range(3):
            done = env.worker.run_once()
            assert done is not None
            assert done.status == "done"
        env.fakes.fetcher.fail_next(
            PermanentFetchError("audio URL on audio.example.net returned 404", host="x")
        )
        failing = client.post(
            "/transcriptions",
            json={"url": DIRECT_URL + "2", "to": SECRET_TO, "instructions": INSTRUCTIONS},
        )
        assert failing.status_code == 202
        failed = env.worker.run_once()
        assert failed is not None
        assert failed.status == "failed"
        client.get(f"/jobs/{failed.id}")


def test_instructions_never_logged(tmp_path: Path, log_stream: io.StringIO) -> None:
    _drive_requests(make_env(tmp_path), tmp_path)
    text = log_stream.getvalue()
    assert "enqueued job" in text  # the requests really were logged
    assert "INSTR-TOKEN" not in text
    assert "summarise the budget" not in text
    assert TITLE not in text
    assert FILENAME not in text


def test_recipient_masked_in_logs(tmp_path: Path, log_stream: io.StringIO) -> None:
    _drive_requests(make_env(tmp_path), tmp_path)
    text = log_stream.getvalue()
    assert MASKED_TO in text
    assert "zelda.secret" not in text
    assert SECRET_TO not in text


def test_direct_url_host_only_in_logs(tmp_path: Path, log_stream: io.StringIO) -> None:
    _drive_requests(make_env(tmp_path), tmp_path)
    text = log_stream.getvalue()
    assert "audio.example.net" in text
    for token in URL_TOKENS:
        assert token not in text


def test_direct_url_host_only_in_logs_real_fetcher(tmp_path: Path, log_stream: io.StringIO) -> None:
    served: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        served.append(str(request.url))
        if "gone" not in request.url.params and request.url.params.get("sig") == "QUERY-TOKEN-a77b":
            return httpx2.Response(200, headers={"Content-Type": "audio/mpeg"}, content=b"ID3abc")
        return httpx2.Response(404)

    fakes = make_fake_backends(tmp_path / "data")
    http = HttpAudioFetcher(fakes.artifacts, mock_fetcher(handler), max_bytes=MIB)
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), fetcher=http))
    env.jobs.enqueue(new_url_job(DIRECT_URL, to=SECRET_TO, instructions=INSTRUCTIONS))
    env.jobs.enqueue(new_url_job(DIRECT_URL + "&gone=1", to=SECRET_TO))
    ok = env.worker.run_once()
    gone = env.worker.run_once()
    assert ok is not None
    assert gone is not None
    assert (ok.status, gone.status) == ("done", "failed")
    assert len(served) == 2  # the real URL, with its query, was requested...
    text = log_stream.getvalue()
    assert "audio.example.net" in text
    for token in URL_TOKENS:  # ...and appears nowhere in the logs
        assert token not in text
    assert gone.error is not None
    assert "audio.example.net" in gone.error
    for token in URL_TOKENS:
        assert token not in gone.error
    assert INSTRUCTIONS not in text


# -- pushes -------------------------------------------------------------------------------


def _ntfy_with_capture() -> tuple[NtfyNotifier, list[bytes]]:
    bodies: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.read())
        return json_reply(200, PUBLISHED)

    options = NtfyOptions.from_settings(Settings(_env_file=None, ntfy_url=SecretStr(TOPIC_URL)))
    assert options is not None
    client = NtfyClient(options, transport=httpx2.MockTransport(handler))
    return NtfyNotifier(client), bodies


def _enqueue_source(env: PipelineEnv, source: str) -> Job:
    new: NewJob
    if source == "podcast":
        new = new_podcast_job(CANON, to=SECRET_TO, instructions=INSTRUCTIONS, title=TITLE)
    elif source == "url":
        new = new_url_job(DIRECT_URL, to=SECRET_TO, instructions=INSTRUCTIONS, title=TITLE)
    else:
        with env.staging.begin_upload("m4a") as writer:
            writer.write(b"ID3" + bytes(200))
            staged = writer.commit()
        new = new_upload_job(
            staged.sha256,
            to=SECRET_TO,
            upload_file=staged.name,
            upload_name=FILENAME,
            instructions=INSTRUCTIONS,
            title=TITLE,
        )
    return env.jobs.enqueue(new).job


def _push_for(tmp_path: Path, source: str, status: str) -> str:
    notifier, bodies = _ntfy_with_capture()
    fakes = make_fake_backends(tmp_path / "data")
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), notifier=notifier))
    job = _enqueue_source(env, source)
    if status == "failed":
        fakes.transcriber.fail_next(PermanentError(f"cannot decode {DIRECT_URL} for {FILENAME}"))
    ran = env.worker.run_once()
    assert ran is not None
    assert ran.id == job.id
    assert ran.status == status
    [body] = bodies
    payload = json.loads(body)
    return str(payload["title"]) + "\n" + str(payload["message"]) + "\n" + body.decode()


@pytest.mark.parametrize("status", ["done", "failed"])
@pytest.mark.parametrize("source", ["podcast", "upload", "url"])
def test_push_has_no_instructions_url_path_or_address(
    tmp_path: Path, source: str, status: str
) -> None:
    text = _push_for(tmp_path, source, status)
    assert text
    for token in ("INSTR-TOKEN", "summarise", SECRET_TO, "zelda", "@", *URL_TOKENS):
        assert token not in text
    if source == "url":
        assert "audio.example.net" in text


@pytest.mark.parametrize("source", ["podcast", "upload", "url"])
def test_push_carries_no_instructions(tmp_path: Path, source: str) -> None:
    assert "INSTR-TOKEN" not in _push_for(tmp_path, source, "done")


@pytest.mark.parametrize("status", ["done", "failed"])
def test_direct_url_host_only_in_push(tmp_path: Path, status: str) -> None:
    text = _push_for(tmp_path, "url", status)
    assert "audio.example.net" in text
    for token in URL_TOKENS:
        assert token not in text
    if status == "done":
        assert "https://" not in text


def test_failed_push_scrubs_urls_from_the_reason(tmp_path: Path) -> None:
    text = _push_for(tmp_path, "upload", "failed")
    assert "Reason:" in text
    assert "PATH-TOKEN" not in text
    assert "QUERY-TOKEN" not in text
