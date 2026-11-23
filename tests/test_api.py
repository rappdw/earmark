import hmac
import json
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
from earmark.api.routes import MAX_BODY_BYTES, build_router
from earmark.config import Settings
from earmark.db import LegacyDatabaseError, open_database
from earmark.jobs import JobStore
from earmark.models import MAX_PROMPT_CHARS, Job
from earmark.resolve.apple import MESSAGES, UrlProblem
from tests.factories import T0, MutableClock
from tests.legacy_db import V010_JOB_COUNT, V010_SCHEMA_VERSION, make_v010_database

TOKEN = "test-token-6b1f0e"
SHOW = "1200361736"
EP = "1000802907981"
SHARE = f"https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet/id{SHOW}?i={EP}&uo=4"
CANON = f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")


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
    return c.post("/ingest", json={"url": SHARE, **body})


# -- A1: auth ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/ingest"),
        ("GET", "/jobs"),
        ("GET", f"/jobs/{'a' * 32}"),
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
    r = anon.post("/ingest", json={"url": SHARE}, headers={"Authorization": f"bearer {TOKEN}"})
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
    assert anon.post("/ingest", content=b"{not json", headers=h).status_code == 401
    assert anon.post("/ingest", content=b"x" * 100_000, headers=h).status_code == 401
    assert anon.post("/ingest", content=b'{"promt": 1}', headers=h).status_code == 401
    assert store.list_recent() == []


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


def test_ingest_returns_202_and_queues(client: TestClient, store: JobStore) -> None:
    r = _ingest(client, prompt="why?", timestamp="2026-03-04T05:06:07-08:00")
    assert r.status_code == 202
    body = r.json()
    assert body == {"job_id": body["job_id"], "status": "queued", "created": True}
    assert r.headers["Location"] == f"/jobs/{body['job_id']}"
    job = store.get(body["job_id"])
    assert job is not None
    assert (job.status, job.kind, job.url, job.prompt) == ("queued", "ingest", CANON, "why?")
    assert job.shared_at.isoformat() == "2026-03-04T13:06:07+00:00"


def test_ingest_shared_at_defaults_to_now(
    client: TestClient, store: JobStore, clock: MutableClock
) -> None:
    job_id = _ingest(client).json()["job_id"]
    job = store.get(job_id)
    assert job is not None
    assert job.shared_at == clock.now == T0


def test_get_job_after_ingest(client: TestClient, store: JobStore) -> None:
    job_id = _ingest(client, prompt="p").json()["job_id"]
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert Job.model_validate(r.json()) == store.get(job_id)


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
        ("https://open.spotify.com/episode/abc", "unsupported_host"),
        (f"https://podcasts.apple.com/us/podcast/the-daily/id{SHOW}", "show_link"),
        ("not a url", "malformed"),
        ("https://podcasts.apple.com/us/channel/x/id1?i=2", "not_episode"),
    ],
)
def test_ingest_rejects_url(
    client: TestClient, store: JobStore, url: str, problem: UrlProblem
) -> None:
    r = client.post("/ingest", json={"url": url})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail == MESSAGES[problem]
    assert len(detail) < 100
    assert store.list_recent() == []


def test_ingest_dedupe_hit_returns_existing_job(client: TestClient, store: JobStore) -> None:
    first = _ingest(client).json()
    second = client.post(
        "/ingest",
        json={"url": f"https://itunes.apple.com/US/podcast/x/id{SHOW}?i={EP}&mt=2"},
    )
    assert second.status_code == 202
    assert second.json() == {**first, "created": False}
    assert store.claim_next() is not None
    third = _ingest(client)
    assert third.status_code == 202
    assert third.json() == {"job_id": first["job_id"], "status": "running", "created": False}


# -- A6: prompt and body limits ----------------------------------------------------------


def test_ingest_blank_prompt_stored_as_null(client: TestClient, store: JobStore) -> None:
    first = _ingest(client, prompt="   ").json()
    job = store.get(first["job_id"])
    assert job is not None
    assert job.prompt is None
    second = _ingest(client).json()
    assert second["job_id"] == first["job_id"]
    assert second["created"] is False


def test_ingest_overlong_prompt_422(client: TestClient, store: JobStore) -> None:
    r = _ingest(client, prompt="x" * (MAX_PROMPT_CHARS + 1))
    assert r.status_code == 422
    assert r.json()["detail"] == "prompt is too long (at most 4000 characters)"
    assert store.list_recent() == []


def test_ingest_body_too_large_413(client: TestClient, store: JobStore) -> None:
    big = {"url": SHARE, "prompt": "x" * (MAX_BODY_BYTES + 1)}
    assert client.post("/ingest", json=big).status_code == 413

    def chunks() -> Iterator[bytes]:
        yield b'{"url": "'
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"a" * 1024
        yield b'"}'

    r = client.post("/ingest", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert store.list_recent() == []


def test_ingest_max_valid_body_accepted(client: TestClient, store: JobStore) -> None:
    body = json.dumps({"url": SHARE, "prompt": "\U0001f600" * MAX_PROMPT_CHARS})
    assert len(body) > 40_000
    r = client.post("/ingest", content=body.encode(), headers={"Content-Type": "application/json"})
    assert r.status_code == 202
    assert len(store.list_recent()) == 1


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
        (
            {"content": b"", "headers": JSON_H},
            "request body is missing; send a JSON object with a url",
        ),
        ({"content": b"null", "headers": JSON_H}, CT_MSG),
        ({"content": b"[1]", "headers": JSON_H}, CT_MSG),
        ({"content": b'{"url": "sentinel-4a7d"}'}, CT_MSG),
        ({"json": {}}, "url is required"),
        ({"json": {"url": ["sentinel-4a7d"]}}, None),
        (
            {"json": {"url": SHARE, "promt": "sentinel-4a7d"}},
            "unknown field 'promt' (allowed: url, prompt, timestamp)",
        ),
        ({"json": {"url": SHARE, "timestamp": "2026-01-01T00:00:00"}}, None),
        ({"json": {"url": SHARE, "prompt": 5}}, None),
    ],
)
def test_validation_messages(
    client: TestClient, store: JobStore, kwargs: dict[str, Any], expected: str | None
) -> None:
    r = client.post("/ingest", **kwargs)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert len(detail) <= MAX_DETAIL_CHARS
    assert "sentinel" not in r.text
    if expected is not None:
        assert detail == expected
    assert store.list_recent() == []


def test_validation_message_specifics(client: TestClient) -> None:
    d = client.post("/ingest", json={"url": ["sentinel-4a7d"]}).json()["detail"]
    assert d.startswith("url:")
    d = client.post("/ingest", json={"url": SHARE, "timestamp": "2026-01-01T00:00:00"}).json()[
        "detail"
    ]
    assert "timestamp:" in d
    assert "UTC offset" in d
    d = client.post("/ingest", json={"url": SHARE, "prompt": 5}).json()["detail"]
    assert d.startswith("prompt:")
    d = client.post("/ingest", json={"url": "   "}).json()["detail"]
    assert d == "url must not be empty"


def test_unknown_field_name_truncated(client: TestClient) -> None:
    r = client.post("/ingest", json={"url": SHARE, "k" * 100: 1})
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
        ids.append(_ingest(client, prompt=f"p{i}").json()["job_id"])
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
        r = c.post("/ingest", json={"url": SHARE})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"
    assert r.json() == {"detail": "the job store is unavailable; try again shortly"}


def test_routes_are_exactly_the_kept_ones(store: JobStore) -> None:
    routes = {
        (route.path, frozenset(route.methods or ()))
        for route in build_router(store).routes
        if isinstance(route, APIRoute)
    }
    assert routes == {
        ("/ingest", frozenset({"POST"})),
        ("/jobs/{job_id}", frozenset({"GET"})),
        ("/jobs", frozenset({"GET"})),
        ("/healthz", frozenset({"GET", "HEAD"})),
    }
    assert len(build_router(store).routes) == 4


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
