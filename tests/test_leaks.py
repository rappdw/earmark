"""Instructions, destination addresses and direct URLs stay out of logs and pushes."""

import io
import json
from collections.abc import Callable
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
from earmark.fakes import FakeTranscriber, make_fake_backends
from earmark.fetch import MIB, PermanentFetchError
from earmark.jobs import NewJob
from earmark.log import configure_logging
from earmark.models import Job, Segment
from earmark.notify.ntfy import NtfyClient, NtfyNotifier, NtfyOptions
from tests.factories import new_podcast_job, new_upload_job, new_url_job, new_youtube_job
from tests.http_helpers import mock_fetcher
from tests.ntfy_stub import PUBLISHED, TOPIC_URL, json_reply
from tests.pipeline_env import CANON, PipelineEnv, make_env
from tests.smtp_stub import SmtpStub, stub_factory, trusted_test_context

pytestmark = pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")

TOKEN = "leak-token-77aa"
INSTRUCTIONS = "INSTR-TOKEN-5c1e summarise the budget"
SECRET_TO = "zelda.secret@example.org"
MASKED_TO = "z…@example.org"
OTHER_TO = "you@example.org"
DIRECT_URL = "https://audio.example.net/private/PATH-TOKEN-91d2.mp3?sig=QUERY-TOKEN-a77b"
FILENAME = "FILE-TOKEN-3b8f.m4a"
TITLE = "TITLE-TOKEN-0d4e"
YOUTUBE_ID = "jNQXAC9IVRw"
YOUTUBE_URL = f"https://www.youtube.com/watch?v={YOUTUBE_ID}"
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
    elif source == "youtube":
        new = new_youtube_job(YOUTUBE_URL, to=SECRET_TO, instructions=INSTRUCTIONS, title=TITLE)
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
@pytest.mark.parametrize("source", ["podcast", "upload", "url", "youtube"])
def test_push_has_no_instructions_url_path_or_address(
    tmp_path: Path, source: str, status: str
) -> None:
    text = _push_for(tmp_path, source, status)
    assert text
    for token in ("INSTR-TOKEN", "summarise", SECRET_TO, "zelda", *URL_TOKENS):
        assert token not in text
    assert text.count("@") == text.count("To: z…@example.org")
    assert "To: z…@example.org" in text
    if source == "url":
        assert "audio.example.net" in text


@pytest.mark.parametrize("source", ["podcast", "upload", "url", "youtube"])
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


def test_youtube_job_leaks_nothing(tmp_path: Path, log_stream: io.StringIO) -> None:
    """Logs and the push: no instructions, a masked address, no URL, no guid."""
    notifier, bodies = _ntfy_with_capture()
    fakes = make_fake_backends(tmp_path / "data")
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), notifier=notifier))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(_settings(tmp_path), store=env.jobs), headers=headers) as client:
        posted = client.post(
            "/transcriptions",
            json={
                "url": f"https://youtu.be/{YOUTUBE_ID}?si=SI-TOKEN-4e2a&t=9",
                "to": SECRET_TO,
                "instructions": INSTRUCTIONS,
                "title": TITLE,
            },
        )
        assert posted.status_code == 202
    ran = env.worker.run_once()
    assert ran is not None
    assert ran.status == "done"
    logs = log_stream.getvalue()
    assert f"YouTube video {YOUTUBE_ID}" in logs
    assert MASKED_TO in logs
    [body] = bodies
    push = body.decode()
    for text in (logs, push):
        assert "INSTR-TOKEN" not in text
        assert "summarise the budget" not in text
        assert "zelda.secret" not in text
        assert "SI-TOKEN" not in text
        assert "watch?v=" not in text
        assert "youtu.be" not in text
        assert "youtube:" not in text  # the guid
    assert "https://" not in push


def test_failed_push_scrubs_urls_from_the_reason(tmp_path: Path) -> None:
    text = _push_for(tmp_path, "upload", "failed")
    assert "Reason:" in text
    assert "PATH-TOKEN" not in text
    assert "QUERY-TOKEN" not in text


# -- the SMTP password (step 25) ---------------------------------------------------------

SMTP_PASSWORD = "abcd efgh ijkl mnop"
make_stub = pytest.fixture(stub_factory)


def _smtp_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "mail_backend": "smtp",
        "smtp_host": "smtp.test",
        "smtp_username": "mailer",
        "smtp_password": SecretStr(SMTP_PASSWORD),
        "smtp_from": "earmark@example.org",
        "resolver_backend": "fake",
        "fetch_backend": "fake",
        "youtube_backend": "fake",
        "asr_backend": "fake",
        "notify_backend": "fake",
        "api_token": SecretStr(TOKEN),
        "data_dir": tmp_path / "data",
        "allowed_recipients": (OTHER_TO,),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_smtp_password_never_leaks(
    tmp_path: Path,
    log_stream: io.StringIO,
    make_stub: Callable[..., SmtpStub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from earmark.cli import app
    from earmark.doctor import Probes, run_doctor
    from earmark.mail.smtp import SmtpMailer, SmtpOptions

    stub = make_stub(auth_reply="535 5.7.8 rejected the password {password} for {user}")
    settings = _smtp_settings(tmp_path)
    options = SmtpOptions.from_settings(settings)
    mailer = SmtpMailer(
        options, socket_factory=stub.socket_factory, context_factory=trusted_test_context
    )
    fakes = make_fake_backends(tmp_path / "data")
    env = make_env(tmp_path, fakes=fakes, backends=replace(fakes.backends(), mailer=mailer))
    env.jobs.enqueue(new_podcast_job(CANON, to=OTHER_TO))
    failed = env.worker.run_once()
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error is not None
    assert "EARMARK_SMTP_PASSWORD" in failed.error  # it names the setting, never the value
    assert {auth[1] for auth in stub.auth} == {SMTP_PASSWORD}  # the stub did get it

    # Doctor, offline and online (the stub rejects the login again).
    probes = Probes(
        smtp_socket_factory=stub.socket_factory,
        smtp_context_factory=trusted_test_context,
        timeout_s=5.0,
    )
    doctor = [
        r.detail
        for online in (False, True)
        for r in run_doctor(settings, online=online, probes=probes)
    ]

    # `earmark config`, with the password in the environment.
    monkeypatch.setenv("EARMARK_SMTP_PASSWORD", SMTP_PASSWORD)
    monkeypatch.setenv("EARMARK_DATA_DIR", str(tmp_path / "data"))
    config = CliRunner().invoke(app, ["config"])
    assert config.exit_code == 0
    assert "EARMARK_SMTP_PASSWORD=**********" in config.stdout

    pushes = [repr(call.job) for call in fakes.notifier.calls]
    haystacks = [
        log_stream.getvalue(),
        failed.error,
        repr(failed),
        config.output,
        *doctor,
        *pushes,
    ]
    for text in haystacks:
        assert SMTP_PASSWORD not in text
        assert "efgh" not in text
    assert "smtp session failed" in log_stream.getvalue()  # the DEBUG log captured the run


def _transcript_env(tmp_path: Path) -> PipelineEnv:
    fakes = make_fake_backends(tmp_path / "data")
    fakes.transcriber = FakeTranscriber(
        fakes.artifacts, segments=[Segment(start=0, end=5, text="SPOKEN-TOKEN-6e1f and more words")]
    )
    return make_env(tmp_path, fakes=fakes)


def test_transcript_route_leaks_nothing(tmp_path: Path, log_stream: io.StringIO) -> None:
    env = _transcript_env(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(_settings(tmp_path), store=env.jobs), headers=headers) as client:
        podcast = client.post(
            "/transcriptions", json={"url": CANON, "to": SECRET_TO, "instructions": INSTRUCTIONS}
        )
        upload = client.post(
            "/transcriptions",
            files={"file": (FILENAME, b"ID3" + bytes(500), "audio/mp4")},
            data={"to": SECRET_TO, "instructions": INSTRUCTIONS},
        )
        for _ in range(2):
            done = env.worker.run_once()
            assert done is not None
            assert done.status == "done"
        env.fakes.fetcher.fail_next(PermanentFetchError("audio URL returned 404", host="x"))
        failing = client.post(
            "/transcriptions",
            json={"url": DIRECT_URL, "to": SECRET_TO, "instructions": INSTRUCTIONS},
        )
        failed = env.worker.run_once()
        assert failed is not None
        assert failed.status == "failed"
        ids = [r.json()["job_id"] for r in (podcast, upload, failing)]
        mark = log_stream.tell()
        bodies: list[tuple[int, str]] = []
        for job_id in [*ids, "a" * 32]:
            for fmt in ("note", "text", "json", "xml"):
                got = client.get(f"/jobs/{job_id}/transcript", params={"format": fmt})
                bodies.append((got.status_code, got.text))
        log_stream.seek(mark)
        text = log_stream.read()
    assert "transcript served as note" in text
    for secret in ("SPOKEN-TOKEN", "INSTR-TOKEN", SECRET_TO, MASKED_TO):
        assert secret not in text
    for status, body in bodies:
        assert INSTRUCTIONS not in body
        assert "INSTR-TOKEN" not in body
        assert SECRET_TO not in body
        if status != 200:
            assert "SPOKEN-TOKEN" not in body
    assert {s for s, _ in bodies} == {200, 404, 409, 422}


def test_unreadable_cache_logs_no_content(tmp_path: Path, log_stream: io.StringIO) -> None:
    env = _transcript_env(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(create_app(_settings(tmp_path), store=env.jobs), headers=headers) as client:
        posted = client.post("/transcriptions", json={"url": CANON, "to": SECRET_TO})
        job = env.worker.run_once()
        assert job is not None
        assert job.status == "done"
        path = env.fakes.artifacts.transcript_json_path(job.episode_key or "")
        path.write_text('"SPOKEN-TOKEN-6e1f"')
        mark = log_stream.tell()
        got = client.get(f"/jobs/{posted.json()['job_id']}/transcript")
        log_stream.seek(mark)
        text = log_stream.read()
    assert got.status_code == 500
    assert "unreadable" in text
    for haystack in (text, got.text):
        for banned in ("SPOKEN-TOKEN", "Traceback", "input_value"):
            assert banned not in haystack


def test_api_client_leaks_no_token(tmp_path: Path, log_stream: io.StringIO) -> None:
    from earmark import api_client as ac
    from earmark.models import TranscriptionRequest
    from tests.api_client_helpers import CLIENT_TOKEN, api_settings, in_process_transport

    token = SecretStr(CLIENT_TOKEN)
    url = "https://earmark.example.com"
    messages: list[str] = []

    def attempt(call: Callable[[], object]) -> None:
        try:
            call()
        except ac.ApiError as exc:
            for text in (exc.message, str(exc), repr(exc)):
                assert CLIENT_TOKEN not in text
            messages.append(exc.message)

    app = create_app(api_settings(tmp_path / "data"))
    real = ac.ApiClient(url, token, transport=in_process_transport(app))
    with real:
        accepted = real.submit_url(TranscriptionRequest(url=DIRECT_URL, to=OTHER_TO))
        attempt(lambda: real.submit_url(TranscriptionRequest(url=DIRECT_URL, to="x@example.com")))
        real.list_jobs(limit=5, status=None)
        real.get_job(accepted.job_id)
        attempt(lambda: real.get_job("a" * 32))
        attempt(lambda: real.transcript(accepted.job_id, "note"))
    wrong_app = create_app(api_settings(tmp_path / "other", token="another-token-123"))
    with ac.ApiClient(url, token, transport=in_process_transport(wrong_app)) as wrong:
        attempt(lambda: wrong.list_jobs(limit=1, status=None))

    def echo(request: httpx2.Request) -> httpx2.Response:
        seen = request.headers.get("authorization")
        path = request.url.path
        if path == "/401":
            return httpx2.Response(401, json={"detail": f"seen {seen}"})
        if path == "/422":
            return httpx2.Response(422, json={"detail": f"seen {seen}"})
        if path == "/500":
            return httpx2.Response(500, json={"detail": f"seen {seen}"})
        if path == "/302":
            return httpx2.Response(302, headers={"Location": f"http://x.example.net/{seen}"})
        return httpx2.Response(200, text=f"not json {seen}")

    with ac.ApiClient(url, token, transport=httpx2.MockTransport(echo)) as stub:
        for path in ("/401", "/422", "/500", "/302", "/other"):
            attempt(lambda path=path: stub._call("GET", path, expected=200))  # type: ignore[misc]
        attempt(lambda: stub.list_jobs(limit=1, status=None))
    assert any("seen Bearer **********" in m for m in messages)

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"boom {CLIENT_TOKEN}", request=request)

    with ac.ApiClient(url, token, transport=httpx2.MockTransport(boom)) as broken:
        attempt(lambda: broken.list_jobs(limit=1, status=None))
    assert CLIENT_TOKEN not in log_stream.getvalue()


# -- /v1 ------------------------------------------------------------------------------------

V1_NAME = "MARKNAME-q7.flac"
V1_PROMPT = "MARKPROMPT-4e1a " + "x" * 984
V1_TEXT = "MARKTEXT-b83d the quarterly numbers"


def _v1_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_v1_job_leaks_nothing(tmp_path: Path, log_stream: io.StringIO) -> None:
    from tests.v1_env import v1_env

    segments = (Segment(start=0.0, end=3.0, text=V1_TEXT),)
    with v1_env(tmp_path, segments=segments) as v1:
        job_id = v1.submit(filename=V1_NAME, initial_prompt=V1_PROMPT, language="en")
        done = v1.run()
        assert done.status == "done"
        assert done.upload_name is None
        v1.client.get(f"/v1/jobs/{job_id}")
        as_json = v1.transcript(job_id)
        as_text = v1.transcript(job_id, "text")
        assert V1_TEXT in as_json.text
        assert V1_TEXT in as_text.text
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        v1.client.get(f"/v1/jobs/{job_id}")
        v1.transcript(job_id)
        # a failed job logs nothing of the request either
        v1.fakes.transcriber.fail_next(PermanentError("transcription failed"))
        failed_id = v1.submit(filename=V1_NAME, initial_prompt=V1_PROMPT[:-1] + "y", language="en")
        assert v1.run().id == failed_id
        files = _v1_bytes(tmp_path / "data")
    text = log_stream.getvalue()
    assert "enqueued job" in text  # the requests really were logged
    assert "purged" in text
    for marker in ("MARKNAME", "MARKPROMPT", "MARKTEXT"):
        assert marker not in text
    assert V1_NAME not in text
    # after the purge nothing of the first job is on disk, the database and its log included
    assert "earmark.db" in files
    for name, data in files.items():
        for needle in (b"MARKNAME", b"MARKTEXT"):
            assert needle not in data, (name, needle)
    # the first job's prompt is gone; the failed job's (kept until it is deleted) is the only copy
    assert sum(data.count(b"MARKPROMPT-4e1a") for data in files.values()) <= 1


def test_v1_job_purge_leaves_no_prompt_in_the_database(tmp_path: Path) -> None:
    from tests.v1_env import v1_env

    with v1_env(tmp_path) as v1:
        job_id = v1.submit(filename=V1_NAME, initial_prompt=V1_PROMPT)
        v1.run()
        before = _v1_bytes(tmp_path / "data")
        assert any(b"MARKPROMPT" in data for data in before.values())  # the check can see it
        assert v1.client.delete(f"/v1/jobs/{job_id}").status_code == 200
        after = _v1_bytes(tmp_path / "data")
    assert all(b"MARKPROMPT" not in data for data in after.values())


def test_v1_enqueue_log_line(tmp_path: Path, log_stream: io.StringIO) -> None:
    from tests.v1_env import v1_env

    with v1_env(tmp_path) as v1:
        job_id = v1.submit(initial_prompt="p", language="de", payload=b"ID3" + bytes(61))
    lines = [ln for ln in log_stream.getvalue().splitlines() if "enqueued job" in ln]
    assert len(lines) == 1
    assert lines[0].endswith(
        f"enqueued job {job_id}: upload of 64 bytes, no delivery (created=True)"
    )
