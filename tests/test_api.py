import hmac
import json
import logging
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pytest_socket import SocketBlockedError
from starlette.websockets import WebSocketDisconnect

from earmark.api.app import MAX_DETAIL_CHARS, create_app, describe_validation_errors
from earmark.api.auth import (
    UNAUTHORIZED_DETAIL,
    WWW_AUTHENTICATE,
    ApiTokenError,
    bearer_credentials,
    token_digest,
    token_matches,
)
from earmark.api.routes import MAX_JSON_BODY_BYTES, build_router
from earmark.api.schemas import VIEW_INSTRUCTIONS_CHARS, JobView, job_view
from earmark.artifacts import ArtifactStore
from earmark.config import Settings
from earmark.db import LegacyDatabaseError, open_database
from earmark.jobs import JobStore
from earmark.models import MAX_INSTRUCTIONS_CHARS, Job
from earmark.recipients import RecipientPolicy
from earmark.resolve.apple import MESSAGES, UrlProblem
from earmark.staging import Staging
from earmark.submission import Intake
from tests.asgi_helpers import call_asgi
from tests.factories import T0, TO, MutableClock
from tests.legacy_db import V010_JOB_COUNT, V010_SCHEMA_VERSION, make_v010_database

TOKEN = "test-token-6b1f0e"
SHOW = "1200361736"
EP = "1000802907981"
SHARE = f"https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet/id{SHOW}?i={EP}&uo=4"
CANON = f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET_TO = "zelda.secret@example.org"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        allowed_recipients=(TO, SECRET_TO),
    )


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> JobStore:
    return JobStore(open_database(tmp_path / "earmark.db"), clock=clock)


@pytest.fixture
def client(settings: Settings, store: JobStore) -> Iterator[TestClient]:
    with TestClient(create_app(settings, store=store), headers=AUTH) as c:
        yield c


@pytest.fixture
def anon(settings: Settings, store: JobStore) -> Iterator[TestClient]:
    with TestClient(create_app(settings, store=store)) as c:
        yield c


def _ingest(c: TestClient, **body: Any) -> Any:
    return c.post("/transcriptions", json={"url": SHARE, "to": TO, **body})


# -- A1: auth ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/transcriptions"),
        ("GET", "/jobs"),
        ("GET", f"/jobs/{'a' * 32}"),
        ("GET", f"/jobs/{'a' * 32}/transcript"),
        ("GET", "/nope"),
        ("GET", "/docs"),
    ],
)
def test_missing_token_401(anon: TestClient, method: str, path: str) -> None:
    r = anon.request(method, path)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == WWW_AUTHENTICATE == 'Bearer realm="earmark"'
    assert r.json() == {"detail": UNAUTHORIZED_DETAIL}


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        f"Basic {TOKEN}",
        "Bearer",
        "Bearer ",
        "Bearer   ",
        TOKEN,
    ],
)
def test_wrong_token_401(anon: TestClient, header: str) -> None:
    r = anon.get("/jobs", headers={"Authorization": header})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == WWW_AUTHENTICATE
    assert r.json() == {"detail": UNAUTHORIZED_DETAIL}


def test_bearer_scheme_case_insensitive(anon: TestClient, store: JobStore) -> None:
    r = anon.post(
        "/transcriptions",
        json={"url": SHARE, "to": TO},
        headers={"Authorization": f"bearer {TOKEN}"},
    )
    assert r.status_code == 202


def test_non_ascii_authorization_header_401(anon: TestClient) -> None:
    r = anon.get("/jobs", headers=[(b"authorization", "Bearer t\xe9".encode("latin-1"))])
    assert r.status_code == 401


def test_token_compared_with_compare_digest(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, anon: TestClient
) -> None:
    real = hmac.compare_digest
    calls: list[tuple[Any, Any]] = []

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert client.get("/jobs").status_code == 200
    assert len(calls) == 1
    assert all(isinstance(x, bytes) and len(x) == 32 for x in calls[0])
    assert anon.get("/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert len(calls) == 2


def test_auth_runs_before_body_parsing(anon: TestClient, store: JobStore) -> None:
    h = {"Content-Type": "application/json"}
    assert anon.post("/transcriptions", content=b"{not json", headers=h).status_code == 401
    assert anon.post("/transcriptions", content=b"x" * 100_000, headers=h).status_code == 401
    assert anon.post("/transcriptions", content=b'{"promt": 1}', headers=h).status_code == 401
    assert store.list_recent() == []


def test_auth_before_body_json(settings: Settings, store: JobStore) -> None:
    app = create_app(settings, store=store)
    result = call_asgi(
        app,
        headers={"content-type": "application/json", "content-length": "20"},
        chunks=[b'{"url": "x", "to": ', b'"y"}'],
    )
    assert result.status == 401
    assert result.receive_calls == 0
    assert not (settings.data_dir / "uploads").exists()


def test_websocket_without_token_refused(anon: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc, anon.websocket_connect("/x"):
        pass
    assert exc.value.code == 1008


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER  abc ", "abc"),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic abc", None),
        ("abc", None),
    ],
)
def test_bearer_credentials(header: str | None, expected: str | None) -> None:
    assert bearer_credentials(header) == expected


def test_token_matches() -> None:
    digest = token_digest(TOKEN)
    assert token_matches(TOKEN, digest)
    for bad in (TOKEN[:-1], TOKEN + "x", "", "tökén", "\ud800"):
        assert not token_matches(bad, digest)


# -- A2: ingest --------------------------------------------------------------------------


def test_transcription_returns_202_and_queues(client: TestClient, store: JobStore) -> None:
    r = _ingest(client, instructions="why?", timestamp="2026-03-04T05:06:07-08:00")
    assert r.status_code == 202
    body = r.json()
    assert body == {"job_id": body["job_id"], "status": "queued", "created": True}
    assert r.headers["Location"] == f"/jobs/{body['job_id']}"
    job = store.get(body["job_id"])
    assert job is not None
    assert (job.status, job.kind, job.url, job.instructions) == ("queued", "ingest", CANON, "why?")
    assert (job.source, job.to) == ("apple_podcasts", TO)
    assert job.shared_at.isoformat() == "2026-03-04T13:06:07+00:00"


def test_ingest_shared_at_defaults_to_now(
    client: TestClient, store: JobStore, clock: MutableClock
) -> None:
    job_id = _ingest(client).json()["job_id"]
    job = store.get(job_id)
    assert job is not None
    assert job.shared_at == clock.now == T0


def test_get_job_after_ingest(client: TestClient, store: JobStore) -> None:
    job_id = _ingest(client, instructions="p").json()["job_id"]
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    job = store.get(job_id)
    assert job is not None
    assert JobView.model_validate(r.json()) == job_view(job)


# -- A3: no outbound I/O -----------------------------------------------------------------


def test_ingest_makes_no_outbound_io(client: TestClient) -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(SocketBlockedError):
        socket.getaddrinfo("podcasts.apple.com", 443)
    assert _ingest(client).status_code == 202


# -- A4 / B1: url rejection and dedupe ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        (f"https://podcasts.apple.com/us/podcast/the-daily/id{SHOW}", "show_link"),
        ("not a url", "malformed"),
        ("ftp://audio.example.net/a.mp3", "malformed"),
        ("open.spotify.com/episode/abc", "malformed"),
        ("https://podcasts.apple.com/us/channel/x/id1?i=2", "not_episode"),
        ("https://music.apple.com/us/album/x/1", "not_episode"),
        ("https://user:pw@cdn.example/a.mp3", "credentials"),
    ],
)
def test_bad_urls_422(client: TestClient, store: JobStore, url: str, problem: UrlProblem) -> None:
    r = client.post("/transcriptions", json={"url": url, "to": TO})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail == MESSAGES[problem]
    assert len(detail) < 100
    assert store.list_recent() == []


YT = "jNQXAC9IVRw"
YT_CANON = f"https://www.youtube.com/watch?v={YT}"


def test_youtube_link_enqueues_canonical(client: TestClient, store: JobStore) -> None:
    r = client.post("/transcriptions", json={"url": f"https://youtu.be/{YT}?si=tracking", "to": TO})
    assert r.status_code == 202
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert (job.source, job.url, job.status) == ("youtube", YT_CANON, "queued")
    view = client.get(f"/jobs/{job.id}").json()
    assert (view["source"], view["url"]) == ("youtube", YT_CANON)


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("https://www.youtube.com/@example", "youtube_not_video"),
        ("https://www.youtube.com/playlist?list=PLx", "youtube_not_video"),
        ("https://www.youtube.com/results?search_query=x", "youtube_not_video"),
        ("https://youtu.be/short", "youtube_bad_id"),
        ("https://www.youtube.com/watch?v=", "youtube_bad_id"),
    ],
)
def test_youtube_refusals_are_422(
    client: TestClient, store: JobStore, url: str, problem: UrlProblem
) -> None:
    r = client.post("/transcriptions", json={"url": url, "to": TO})
    assert r.status_code == 422
    assert r.json()["detail"] == MESSAGES[problem]
    assert MESSAGES["youtube_not_video"] == (
        "This YouTube link is not a single video. Share a video, not a channel, playlist or search."
    )
    assert MESSAGES["youtube_bad_id"] == "This YouTube link has no valid video id."
    assert store.list_recent() == []


def test_youtube_forms_dedupe(client: TestClient, store: JobStore) -> None:
    first = client.post(
        "/transcriptions", json={"url": f"https://youtu.be/{YT}?si=x", "to": TO}
    ).json()
    second = client.post(
        "/transcriptions", json={"url": f"https://www.youtube.com/shorts/{YT}", "to": TO}
    )
    assert second.status_code == 202
    assert second.json() == {**first, "created": False}
    assert len(store.list_recent()) == 1


def test_ingest_dedupe_hit_returns_existing_job(client: TestClient, store: JobStore) -> None:
    first = _ingest(client).json()
    second = client.post(
        "/transcriptions",
        json={"url": f"https://itunes.apple.com/US/podcast/x/id{SHOW}?i={EP}&mt=2", "to": TO},
    )
    assert second.status_code == 202
    assert second.json() == {**first, "created": False}
    assert store.claim_next() is not None
    third = _ingest(client)
    assert third.status_code == 202
    assert third.json() == {"job_id": first["job_id"], "status": "running", "created": False}


# -- A6: instructions and body limits ---------------------------------------------------


def test_blank_instructions_stored_as_empty(client: TestClient, store: JobStore) -> None:
    first = _ingest(client, instructions="   ").json()
    job = store.get(first["job_id"])
    assert job is not None
    assert job.instructions == ""
    second = _ingest(client).json()
    assert second["job_id"] == first["job_id"]
    assert second["created"] is False


def test_null_instructions_accepted(client: TestClient, store: JobStore) -> None:
    assert _ingest(client, instructions=None).status_code == 202


def test_overlong_instructions_422(client: TestClient, store: JobStore) -> None:
    r = _ingest(client, instructions="x" * (MAX_INSTRUCTIONS_CHARS + 1))
    assert r.status_code == 422
    assert r.json()["detail"] == "instructions is too long (at most 4000 characters)"
    assert store.list_recent() == []


def test_json_over_64k_413_declared_and_streamed(
    client: TestClient, store: JobStore, settings: Settings
) -> None:
    big = {"url": SHARE, "to": TO, "instructions": "x" * (MAX_JSON_BODY_BYTES + 1)}
    r = client.post("/transcriptions", json=big)
    assert r.status_code == 413
    assert r.json() == {"detail": "request body is larger than 64 KiB"}

    chunks = [b'{"url": "', *[b"a" * 1024] * (MAX_JSON_BODY_BYTES // 1024 + 2), b'"}']
    streamed = call_asgi(
        create_app(settings, store=store),
        headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        chunks=chunks,
    )
    assert streamed.status == 413
    assert streamed.json() == {"detail": "request body is larger than 64 KiB"}
    declared = call_asgi(
        create_app(settings, store=store),
        headers={
            "authorization": f"Bearer {TOKEN}",
            "content-type": "application/json",
            "content-length": str(MAX_JSON_BODY_BYTES + 1),
        },
        chunks=[b"{}"],
    )
    assert declared.status == 413
    assert declared.receive_calls == 0
    assert store.list_recent() == []


def test_json_max_valid_body_accepted(client: TestClient, store: JobStore) -> None:
    body = json.dumps(
        {
            "url": SHARE,
            "to": TO,
            "instructions": "\U0001f600" * MAX_INSTRUCTIONS_CHARS,
            "title": "\U0001f600" * 200,
        }
    )
    assert len(body) > 40_000
    r = client.post(
        "/transcriptions", content=body.encode(), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 202
    assert len(store.list_recent()) == 1


def test_unsupported_content_type_415(client: TestClient, store: JobStore) -> None:
    for headers in (
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {},
    ):
        r = client.post("/transcriptions", content=b"url=x", headers=headers)
        assert r.status_code == 415
        assert "application/json" in r.json()["detail"]
        assert "multipart/form-data" in r.json()["detail"]
    assert store.list_recent() == []


def test_empty_json_body_422(client: TestClient) -> None:
    r = client.post("/transcriptions", content=b"", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"] == "request body is missing; send a JSON object with url and to"


def test_ingest_route_gone_404(client: TestClient) -> None:
    r = client.post("/ingest", json={"url": SHARE, "to": TO})
    assert r.status_code == 404


# -- recipients --------------------------------------------------------------------------


def test_to_missing_422(client: TestClient, store: JobStore) -> None:
    r = client.post("/transcriptions", json={"url": SHARE})
    assert r.status_code == 422
    assert r.json()["detail"] == "to is required"
    assert store.list_recent() == []


@pytest.mark.parametrize(
    "bad", ["not-an-address", "a@b", "a@b.org, c@d.org", "a b@c.org", "a@b.org\r\nBcc: x@y.org"]
)
def test_to_invalid_422_does_not_echo(client: TestClient, store: JobStore, bad: str) -> None:
    r = client.post("/transcriptions", json={"url": SHARE, "to": bad})
    assert r.status_code == 422
    assert r.json()["detail"] == "to must be one email address"
    assert bad not in r.text
    assert store.list_recent() == []


def test_to_not_allowlisted_422(client: TestClient, store: JobStore) -> None:
    r = client.post("/transcriptions", json={"url": SHARE, "to": "stranger@example.com"})
    assert r.status_code == 422
    assert r.json()["detail"] == "to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)"
    assert "stranger" not in r.text
    assert store.list_recent() == []


def test_to_is_matched_after_normalisation(client: TestClient, store: JobStore) -> None:
    r = _ingest(client, to="you@EXAMPLE.org")
    assert r.status_code == 202
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert job.to == TO
    assert _ingest(client, to="You@example.org").status_code == 422  # the local part is exact


def test_allowlist_unset_refuses_everything(
    tmp_path: Path, store: JobStore, settings: Settings
) -> None:
    unset = Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=settings.data_dir)
    with TestClient(create_app(unset, store=store), headers=AUTH) as c:
        r = _ingest(c)
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server"
    )
    assert store.list_recent() == []


def test_allow_all_accepts_any_address(settings: Settings, store: JobStore) -> None:
    everyone = settings.model_copy(update={"allowed_recipients": ("all",)})
    with TestClient(create_app(everyone, store=store), headers=AUTH) as c:
        assert _ingest(c, to="anyone@example.net").status_code == 202
        assert _ingest(c, to="not an address").status_code == 422


def test_allow_all_logs_warning_at_startup(
    settings: Settings, store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    everyone = settings.model_copy(update={"allowed_recipients": ("all",)})
    with caplog.at_level(logging.WARNING, logger="earmark.api.app"):
        create_app(everyone, store=store)
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "EARMARK_ALLOWED_RECIPIENTS=all" in record.getMessage()
    assert "\n" not in record.getMessage()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="earmark.api.app"):
        create_app(settings, store=store)
    assert caplog.records == []


def test_direct_url_request(client: TestClient, store: JobStore) -> None:
    r = client.post(
        "/transcriptions",
        json={"url": "https://audio.example.net/p/clip.mp3?sig=Q#frag", "to": TO, "title": "Clip"},
    )
    assert r.status_code == 202
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert (job.source, job.url, job.title) == (
        "url",
        "https://audio.example.net/p/clip.mp3?sig=Q",
        "Clip",
    )


def test_double_tap_returns_same_job(client: TestClient, store: JobStore) -> None:
    first = _ingest(client, instructions="x").json()
    second = _ingest(client, instructions="x").json()
    assert second == {**first, "created": False}
    claim = store.claim_next()
    assert claim is not None
    store.complete(claim)
    third = _ingest(client, instructions="x").json()
    assert third == {"job_id": first["job_id"], "status": "done", "created": False}
    assert _ingest(client, instructions="y").json()["created"] is True
    assert _ingest(client, instructions="x", to=SECRET_TO).json()["created"] is True


# -- A7: healthz and docs ----------------------------------------------------------------


def test_healthz_public(settings: Settings, tmp_path: Path) -> None:
    class ExplodingStore(JobStore):
        def get(self, job_id: str) -> Job | None:
            raise AssertionError("store touched")

        def list_recent(self, *args: Any, **kwargs: Any) -> list[Job]:
            raise AssertionError("store touched")

        def enqueue(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("store touched")

    boom = ExplodingStore(open_database(tmp_path / "x.db"))
    with TestClient(create_app(settings, store=boom)) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert "server" not in r.headers or "uvicorn" not in r.headers["server"]
        assert c.head("/healthz").status_code == 200
        assert c.get("/jobs").status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_unknown_route_and_method(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404
    assert client.delete("/jobs").status_code == 405


# -- A8: token requirement ---------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "   ", "tok en", "tök", "tok\n"])
def test_create_app_requires_token(tmp_path: Path, token: str | None) -> None:
    s = Settings(
        _env_file=None,
        api_token=None if token is None else SecretStr(token),
        data_dir=tmp_path / "data",
    )
    with pytest.raises(ApiTokenError) as exc:
        create_app(s)
    assert "EARMARK_API_TOKEN" in str(exc.value)
    if token:
        assert token not in str(exc.value)
    assert not (tmp_path / "data").exists()


def test_create_app_opens_store_from_settings(settings: Settings, tmp_path: Path) -> None:
    with TestClient(create_app(settings), headers=AUTH) as c:
        assert c.get("/jobs").json() == {"jobs": []}
    assert (tmp_path / "data" / "earmark.db").exists()


# -- B3: validation messages -------------------------------------------------------------

JSON_H = {"Content-Type": "application/json"}
CT_MSG = "request body must be a JSON object sent with Content-Type: application/json"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"content": b"{not json", "headers": JSON_H}, "request body is not valid JSON"),
        ({"content": b"null", "headers": JSON_H}, CT_MSG),
        ({"content": b"[1]", "headers": JSON_H}, CT_MSG),
        ({"json": {}}, "to is required; url is required"),
        ({"json": {"url": SHARE}}, "to is required"),
        ({"json": {"url": ["sentinel-4a7d"], "to": TO}}, None),
        (
            {"json": {"url": SHARE, "to": TO, "promt": "sentinel-4a7d"}},
            "unknown field 'promt' (allowed: url, to, instructions, title, timestamp)",
        ),
        ({"json": {"url": SHARE, "to": TO, "prompt": "sentinel-4a7d"}}, None),
        ({"json": {"url": SHARE, "to": TO, "timestamp": "2026-01-01T00:00:00"}}, None),
        ({"json": {"url": SHARE, "to": TO, "instructions": 5}}, None),
        (
            {"json": {"url": SHARE, "to": TO, "title": "t" * 201}},
            "title is too long (at most 200 characters)",
        ),
    ],
)
def test_validation_messages(
    client: TestClient, store: JobStore, kwargs: dict[str, Any], expected: str | None
) -> None:
    r = client.post("/transcriptions", **kwargs)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert len(detail) <= MAX_DETAIL_CHARS
    assert "sentinel" not in r.text
    if expected is not None:
        assert detail == expected
    assert store.list_recent() == []


def test_validation_message_specifics(client: TestClient) -> None:
    def post(**body: Any) -> str:
        r = client.post("/transcriptions", json={"url": SHARE, "to": TO, **body})
        return str(r.json()["detail"])

    assert (
        client.post("/transcriptions", json={"url": ["s"], "to": TO})
        .json()["detail"]
        .startswith("url:")
    )
    d = post(timestamp="2026-01-01T00:00:00")
    assert "timestamp:" in d
    assert "UTC offset" in d
    assert post(instructions=5).startswith("instructions:")
    assert post(url="   ") == "url must not be empty"


def test_unknown_field_name_truncated(client: TestClient) -> None:
    r = client.post("/transcriptions", json={"url": SHARE, "to": TO, "k" * 100: 1})
    assert f"unknown field '{'k' * 40}' (" in r.json()["detail"]


def test_describe_validation_errors_dedupes_and_caps() -> None:
    err = {"type": "missing", "loc": ("body", "url"), "msg": "Field required", "input": "s"}
    assert describe_validation_errors([err, err]) == "url is required"
    many = [
        {"type": "x", "loc": ("body", f"f{i}"), "msg": "m" * 30, "input": "sentinel"}
        for i in range(30)
    ]
    out = describe_validation_errors(many)
    assert len(out) == MAX_DETAIL_CHARS + 1
    assert out.endswith("…")
    assert "sentinel" not in out
    assert describe_validation_errors([]) == ""


# -- B5: jobs ----------------------------------------------------------------------------


def test_get_job_unknown_404(client: TestClient) -> None:
    for job_id in ("0123456789abcdef" * 2, "nothex"):
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 404
        assert r.json() == {"detail": "job not found"}


def test_list_jobs(client: TestClient, store: JobStore, clock: MutableClock) -> None:
    ids = []
    for i in range(3):
        clock.advance(10)
        ids.append(_ingest(client, instructions=f"p{i}").json()["job_id"])
    r = client.get("/jobs")
    assert [j["id"] for j in r.json()["jobs"]] == ids[::-1]
    assert [j["id"] for j in client.get("/jobs?limit=2").json()["jobs"]] == ids[:0:-1]
    assert len(client.get("/jobs?status=queued").json()["jobs"]) == 3
    assert client.get("/jobs?status=running").json() == {"jobs": []}
    claim = store.claim_next()
    assert claim is not None
    running = client.get("/jobs?status=running").json()["jobs"]
    assert [j["id"] for j in running] == [claim.job.id]
    assert len(client.get("/jobs?status=queued").json()["jobs"]) == 2


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "status=bogus", "limit=x"])
def test_list_jobs_bad_params_422(client: TestClient, query: str) -> None:
    r = client.get(f"/jobs?{query}")
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


# -- B6: store errors --------------------------------------------------------------------


def test_store_error_503(settings: Settings, tmp_path: Path) -> None:
    class LockedStore(JobStore):
        def enqueue(self, *args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("database is locked")

    locked = LockedStore(open_database(tmp_path / "x.db"))
    with TestClient(create_app(settings, store=locked), headers=AUTH) as c:
        r = c.post("/transcriptions", json={"url": SHARE, "to": TO})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"
    assert r.json() == {"detail": "the job store is unavailable; try again shortly"}


def test_routes_are_exactly_the_kept_ones(store: JobStore, tmp_path: Path) -> None:
    intake = Intake(store, RecipientPolicy.from_setting((TO,)), Staging(tmp_path))
    router = build_router(intake, artifacts=ArtifactStore(tmp_path), max_upload_bytes=1024)
    routes = {
        (route.path, frozenset(route.methods or ()))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    assert routes == {
        ("/transcriptions", frozenset({"POST"})),
        ("/jobs/{job_id}", frozenset({"GET"})),
        ("/jobs/{job_id}/transcript", frozenset({"GET"})),
        ("/jobs", frozenset({"GET"})),
        ("/healthz", frozenset({"GET", "HEAD"})),
    }
    assert len(router.routes) == 5


def test_create_app_refuses_v010_database(settings: Settings, tmp_path: Path) -> None:
    settings.data_dir.mkdir()
    path = make_v010_database(settings.data_dir / "earmark.db")
    with pytest.raises(LegacyDatabaseError, match="Move it aside") as info:
        create_app(settings)
    assert str(path) in str(info.value)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V010_SCHEMA_VERSION
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == V010_JOB_COUNT
    finally:
        conn.close()


# -- job views ---------------------------------------------------------------------------


def _view(client: TestClient, job_id: str) -> dict[str, Any]:
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


def test_job_view_fields(client: TestClient) -> None:
    view = _view(client, _ingest(client).json()["job_id"])
    assert set(view) == set(JobView.model_fields)
    for hidden in ("heartbeat_at", "upload_file", "upload_name", "content_sha256", "lease_token"):
        assert hidden not in view
    assert set(view) == {
        "id", "status", "stage", "source", "url", "to", "instructions", "title", "episode_key",
        "attempts", "run_after", "error", "warnings", "result", "delivery_state", "sent_at",
        "shared_at", "created_at", "updated_at",
    }  # fmt: skip
    assert view["to"] == TO


def test_view_instructions_truncated_to_200(client: TestClient) -> None:
    exact = "é" * VIEW_INSTRUCTIONS_CHARS
    assert (
        _view(client, _ingest(client, instructions=exact).json()["job_id"])["instructions"] == exact
    )
    long = "é" * (VIEW_INSTRUCTIONS_CHARS + 50)
    shown = _view(client, _ingest(client, instructions=long).json()["job_id"])["instructions"]
    assert shown == "é" * 199 + "…"
    assert len(shown) == VIEW_INSTRUCTIONS_CHARS


def test_view_direct_url_scheme_host_path_only(client: TestClient) -> None:
    url = "https://user@Audio.Example.NET:8443/private/PATH-TOKEN.mp3?sig=QUERY-TOKEN#frag"
    r = client.post("/transcriptions", json={"url": url, "to": TO})
    assert r.status_code == 422  # credentials are refused before anything is stored
    url = "https://Audio.Example.NET:8443/private/PATH-TOKEN.mp3?sig=QUERY-TOKEN#frag"
    job_id = client.post("/transcriptions", json={"url": url, "to": TO}).json()["job_id"]
    view = _view(client, job_id)
    assert view["url"] == "https://audio.example.net:8443/private/PATH-TOKEN.mp3"
    assert view["source"] == "url"
    assert "QUERY-TOKEN" not in json.dumps(view)


def test_view_podcast_url_whole(client: TestClient) -> None:
    view = _view(client, _ingest(client).json()["job_id"])
    assert view["url"] == CANON


def test_view_errors_scrubbed(client: TestClient, store: JobStore) -> None:
    job_id = _ingest(client).json()["job_id"]
    claim = store.claim_next()
    assert claim is not None
    store.add_warning(job_id, "slow server https://cdn.example/a/b.mp3?token=WARN-TOKEN")
    store.fail(claim, "got 404 from https://cdn.example/a/b.mp3?token=ERR-TOKEN", transient=False)
    view = _view(client, job_id)
    assert view["error"] == "got 404 from https://cdn.example/…"
    assert view["warnings"] == ["slow server https://cdn.example/…"]
    assert "TOKEN" not in json.dumps(view)


def test_list_jobs_returns_views(client: TestClient) -> None:
    _ingest(client)
    [view] = client.get("/jobs").json()["jobs"]
    assert set(view) == set(JobView.model_fields)


def test_upload_job_view_has_no_url_or_file_name(client: TestClient, store: JobStore) -> None:
    from tests.factories import new_upload_job

    job = store.enqueue(new_upload_job("e" * 64, upload_name="PRIVATE-NAME.mp3")).job
    view = _view(client, job.id)
    assert (view["source"], view["url"]) == ("upload", None)
    assert "PRIVATE-NAME" not in json.dumps(view)


# -- startup sweep -----------------------------------------------------------------------


def test_create_app_sweeps_stale_uploads(settings: Settings, store: JobStore) -> None:
    from tests.factories import new_upload_job

    uploads = settings.data_dir / "uploads"
    uploads.mkdir(parents=True)
    kept = f"{'a' * 32}.mp3"
    (uploads / kept).write_bytes(b"queued job's audio")
    (uploads / f".{'b' * 32}.mp3.part").write_bytes(b"crashed mid-upload")
    (uploads / f"{'c' * 32}.mp3").write_bytes(b"orphan")
    store.enqueue(new_upload_job("d" * 64, upload_file=kept))
    create_app(settings, store=store)
    assert [p.name for p in uploads.iterdir()] == [kept]


# -- the requester's offset and delivery (step 25) ---------------------------------------


@pytest.mark.parametrize(
    ("timestamp", "minutes"),
    [
        ("2026-03-04T05:06:07-08:00", -480),
        ("2026-03-04T05:06:07Z", 0),
        ("2026-03-04T05:06:07+0530", 330),
    ],
)
def test_timestamp_offset_reaches_job(
    client: TestClient, store: JobStore, timestamp: str, minutes: int
) -> None:
    job_id = _ingest(client, timestamp=timestamp).json()["job_id"]
    job = store.get(job_id)
    assert job is not None
    assert job.utc_offset_min == minutes
    upload = client.post(
        "/transcriptions",
        files={"file": ("a.mp3", b"ID3" + bytes(100), "audio/mpeg")},
        data={"to": TO, "timestamp": timestamp},
    )
    stored = store.get(upload.json()["job_id"])
    assert stored is not None
    assert stored.utc_offset_min == minutes


def test_no_timestamp_means_no_offset(client: TestClient, store: JobStore) -> None:
    job = store.get(_ingest(client).json()["job_id"])
    assert job is not None
    assert job.utc_offset_min is None


def test_job_view_shows_delivery_state_and_sent_at(
    client: TestClient, store: JobStore, clock: MutableClock
) -> None:
    from earmark.models import JobResult

    job_id = _ingest(client).json()["job_id"]
    queued = _view(client, job_id)
    assert (queued["delivery_state"], queued["sent_at"]) == (None, None)
    claim = store.claim_next()
    assert claim is not None
    store.mark_sending(claim, "<earmark.secret-message-id@example.org>")
    sending = _view(client, job_id)
    assert (sending["delivery_state"], sending["sent_at"]) == ("sending", None)
    clock.advance(30)
    store.complete_sent(claim, JobResult(duration_s=1.0))
    sent = _view(client, job_id)
    assert sent["delivery_state"] == "sent"
    assert sent["sent_at"] == "2026-01-01T12:00:30Z"
    assert sent["status"] == "done"
    assert "message_id" not in sent
    assert "resent" not in sent
    assert "secret-message-id" not in json.dumps(sent)
