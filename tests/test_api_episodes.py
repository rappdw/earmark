import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.api.auth import UNAUTHORIZED_DETAIL, WWW_AUTHENTICATE
from earmark.api.routes import MAX_BODY_BYTES
from earmark.artifacts import ArtifactStore
from earmark.config import Settings
from earmark.db import open_database
from earmark.episodes import NO_TRANSCRIPT, EpisodePage
from earmark.jobs import JobStore
from earmark.models import MAX_PROMPT_CHARS, JobResult
from tests.episodes_helpers import seed_episode
from tests.factories import T0, MutableClock, make_episode

TOKEN = "test-token-6b1f0e"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")


@pytest.fixture
def artifacts(settings: Settings) -> ArtifactStore:
    return ArtifactStore(settings.data_dir)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(open_database(tmp_path / "earmark.db"), clock=MutableClock())


@pytest.fixture
def client(settings: Settings, store: JobStore) -> Iterator[TestClient]:
    with TestClient(create_app(settings, store=store), headers=AUTH) as c:
        yield c


def _post(c: TestClient, key: str, body: Any = None, **kw: Any) -> Any:
    return c.post(f"/episodes/{key}/analyze", json={} if body is None else body, **kw)


# -- GET /episodes ----------------------------------------------------------------------


def test_list_episodes_fields(client: TestClient, artifacts: ArtifactStore) -> None:
    a = seed_episode(artifacts, 1, prompts=(None, "q"))
    b = seed_episode(artifacts, 2, shared_at=T0 + timedelta(hours=1), transcript=False)
    r = client.get("/episodes")
    assert r.status_code == 200
    page = EpisodePage.model_validate(r.json())
    assert (page.total, page.offset, page.limit, page.next_offset) == (2, 0, 50, None)
    first, second = r.json()["episodes"]
    assert first["key"] == b.key
    assert first["has_transcript"] is False
    assert first["analyses"] == []
    assert set(second) == {
        "key",
        "show",
        "title",
        "published_at",
        "shared_at",
        "url",
        "has_transcript",
        "analyses",
    }
    assert (second["key"], second["show"], second["title"]) == (a.key, a.show_title, a.title)
    assert second["url"] == a.url
    assert [x["prompt"] for x in second["analyses"]] == ["q", None]
    assert set(second["analyses"][0]) == {"prompt", "prompt_hash", "model", "created_at"}
    assert second["analyses"][0]["model"] == "fake-llm"


def test_list_episodes_pagination(client: TestClient, artifacts: ArtifactStore) -> None:
    keys = [seed_episode(artifacts, n, shared_at=T0 + timedelta(hours=n)).key for n in range(1, 6)]
    last = seed_episode(artifacts, 6, shared_at=None).key
    expected = [*reversed(keys), last]
    seen: list[str] = []
    for offset, nxt in ((0, 2), (2, 4), (4, None)):
        body = client.get(f"/episodes?limit=2&offset={offset}").json()
        seen.extend(e["key"] for e in body["episodes"])
        assert (body["total"], body["next_offset"]) == (6, nxt)
    assert seen == expected
    past = client.get("/episodes?limit=2&offset=50").json()
    assert (past["episodes"], past["total"], past["next_offset"]) == ([], 6, None)


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=201", "offset=-1", "offset=1000001", "limit=abc"]
)
def test_list_episodes_bad_params_422(client: TestClient, query: str) -> None:
    r = client.get(f"/episodes?{query}")
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert query.split("=")[0] in r.json()["detail"]
    if query.split("=")[1] in {"abc", "201", "1000001"}:
        assert query.split("=")[1] not in r.json()["detail"]


def test_list_episodes_empty_creates_nothing(client: TestClient, settings: Settings) -> None:
    body = client.get("/episodes").json()
    assert body == {"episodes": [], "total": 0, "offset": 0, "limit": 50, "next_offset": None}
    assert not (settings.data_dir / "episodes").exists()


def test_list_episodes_ignores_job_store(settings: Settings, artifacts: ArtifactStore) -> None:
    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"job store touched: {name}")

    seed_episode(artifacts, 1)
    app = create_app(settings, store=Boom())  # type: ignore[arg-type]  # a deliberate stub
    with TestClient(app, headers=AUTH) as c:
        assert c.get("/episodes").status_code == 200


def test_list_unreadable_root_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: ArtifactStore) -> list[str]:
        raise PermissionError("no")

    monkeypatch.setattr(ArtifactStore, "list_episode_keys", boom)
    r = client.get("/episodes")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"
    assert r.json() == {"detail": "the episode files under EARMARK_DATA_DIR cannot be read"}


# -- POST /episodes/{key}/analyze -------------------------------------------------------


def test_reanalyze_202(client: TestClient, artifacts: ArtifactStore, store: JobStore) -> None:
    ep = seed_episode(artifacts, 1)
    r = _post(client, ep.key, {"prompt": "  why?  "})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"job_id", "status", "created"}
    assert (body["status"], body["created"]) == ("queued", True)
    assert r.headers["Location"] == f"/jobs/{body['job_id']}"
    job = store.get(body["job_id"])
    assert job is not None
    assert (job.kind, job.url, job.episode_key, job.prompt) == ("reanalyze", None, ep.key, "why?")


def test_reanalyze_dedupe(client: TestClient, artifacts: ArtifactStore, store: JobStore) -> None:
    ep = seed_episode(artifacts, 1)
    first = _post(client, ep.key, {"prompt": "why?"}).json()
    second = _post(client, ep.key, {"prompt": " why? "}).json()
    assert second == {**first, "created": False}
    ids = set()
    for body in ({}, {"prompt": None}, {"prompt": "  "}):
        ids.add(_post(client, ep.key, body).json()["job_id"])
    assert len(ids) == 1
    default = store.get(ids.pop())
    assert default is not None
    assert default.prompt is None
    assert _post(client, ep.key, {"prompt": "other"}).json()["job_id"] != first["job_id"]
    claim = store.claim_next()
    assert claim is not None
    store.complete(claim, JobResult())
    again = _post(client, ep.key, {"prompt": "why?"}).json()
    assert again["created"] is True
    assert again["job_id"] != first["job_id"]


def test_reanalyze_unknown_key_404(client: TestClient, settings: Settings, store: JobStore) -> None:
    good = make_episode().key
    for key in ("0123456789abcdef0123", "nothex", good.upper(), good + "0"):
        r = _post(client, key)
        assert r.status_code == 404
        assert r.json() == {"detail": "episode not found"}
    assert store.list_recent() == []
    assert not (settings.data_dir / "episodes").exists()


def test_reanalyze_without_transcript_409(
    client: TestClient, artifacts: ArtifactStore, store: JobStore
) -> None:
    ep = seed_episode(artifacts, 1, transcript=False)
    r = _post(client, ep.key)
    assert r.status_code == 409
    assert r.json() == {"detail": NO_TRANSCRIPT}
    assert store.list_recent() == []


def test_reanalyze_transcript_without_analysis_accepted(
    client: TestClient, artifacts: ArtifactStore
) -> None:
    ep = seed_episode(artifacts, 1, prompts=())
    assert _post(client, ep.key).status_code == 202


@pytest.mark.parametrize(
    ("kwargs", "detail"),
    [
        ({"json": {"promt": "x"}}, "unknown field 'promt' (allowed: prompt)"),
        ({"content": b""}, 'request body is missing; send a JSON object such as {"prompt": '),
        ({"content": b"", "headers": JSON}, "request body is missing; send a JSON object such as"),
        ({"json": None, "headers": JSON}, "request body is missing"),
        ({"content": b"null", "headers": JSON}, "request body must be a JSON object"),
        ({"content": b"[]", "headers": JSON}, "request body must be a JSON object"),
        ({"content": b"{nope", "headers": JSON}, "request body is not valid JSON"),
        (
            {"json": {"prompt": "x" * (MAX_PROMPT_CHARS + 1)}},
            "prompt is too long (at most 4000 characters)",
        ),
    ],
)
def test_reanalyze_body_errors_422(
    client: TestClient,
    artifacts: ArtifactStore,
    store: JobStore,
    kwargs: dict[str, Any],
    detail: str,
) -> None:
    ep = seed_episode(artifacts, 1)
    r = client.post(f"/episodes/{ep.key}/analyze", **kwargs)
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(detail)
    assert store.list_recent() == []


def test_reanalyze_body_too_large_413(
    client: TestClient, artifacts: ArtifactStore, store: JobStore
) -> None:
    ep = seed_episode(artifacts, 1)
    big = {"prompt": "x" * (MAX_BODY_BYTES + 1)}
    assert _post(client, ep.key, big).status_code == 413

    def chunks() -> Iterator[bytes]:
        yield b'{"prompt": "'
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"a" * 1024
        yield b'"}'

    r = client.post(f"/episodes/{ep.key}/analyze", content=chunks(), headers=JSON)
    assert r.status_code == 413
    assert store.list_recent() == []


def test_reanalyze_max_valid_body_accepted(client: TestClient, artifacts: ArtifactStore) -> None:
    ep = seed_episode(artifacts, 1)
    body = json.dumps({"prompt": "\U0001f600" * MAX_PROMPT_CHARS})
    r = client.post(f"/episodes/{ep.key}/analyze", content=body.encode(), headers=JSON)
    assert r.status_code == 202


def test_episodes_routes_require_auth(settings: Settings, store: JobStore) -> None:
    with TestClient(create_app(settings, store=store)) as anon:
        for r in (
            anon.get("/episodes"),
            anon.post("/episodes/0123456789abcdef0123/analyze", content=b"{nope", headers=JSON),
        ):
            assert r.status_code == 401
            assert r.headers["WWW-Authenticate"] == WWW_AUTHENTICATE
            assert r.json() == {"detail": UNAUTHORIZED_DETAIL}


def test_reanalyze_store_error_503(
    settings: Settings, artifacts: ArtifactStore, tmp_path: Path
) -> None:
    class Locked(JobStore):
        def enqueue_reanalyze(self, *args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("database is locked")

    ep = seed_episode(artifacts, 1)
    locked = Locked(open_database(tmp_path / "x.db"))
    with TestClient(create_app(settings, store=locked), headers=AUTH) as c:
        r = _post(c, ep.key)
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"
