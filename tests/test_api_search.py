from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from earmark.api.app import create_app
from earmark.config import Settings
from earmark.db import open_database
from earmark.errors import EarmarkError, TransientError
from earmark.fakes import FakeSearcher
from earmark.index.search import IndexMismatchError, SearchQuery, SearchUnavailableError
from earmark.jobs import JobStore
from tests.factories import MutableClock
from tests.test_index_search import KEY, make_hit

TOKEN = "test-token-6b1f0e"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(open_database(tmp_path / "earmark.db"), clock=MutableClock())


def client_for(
    settings: Settings, store: JobStore, searcher: FakeSearcher | None
) -> Iterator[TestClient]:
    with TestClient(create_app(settings, store=store, searcher=searcher), headers=AUTH) as c:
        yield c


@pytest.fixture
def searcher() -> FakeSearcher:
    return FakeSearcher([make_hit(1), make_hit(2, kind="summary", start_s=None, timestamp=None)])


@pytest.fixture
def client(settings: Settings, store: JobStore, searcher: FakeSearcher) -> Iterator[TestClient]:
    yield from client_for(settings, store, searcher)


def test_search_returns_hits(client: TestClient, searcher: FakeSearcher) -> None:
    response = client.get("/search", params={"q": "fake claim"})
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert [h["rank"] for h in hits] == [1, 2]
    assert hits[0] == {
        "rank": 1,
        "score": 0.8312,
        "episode_key": KEY,
        "show_title": "Fake Show",
        "episode_title": "Fake episode",
        "published_at": "2026-01-01T00:00:00Z",
        "episode_url": "https://podcasts.apple.com/us/podcast/id1?i=2",
        "kind": "claim",
        "prompt_hash": "b" * 16,
        "prompt": "why?",
        "start_s": 65.0,
        "end_s": None,
        "timestamp": "00:01:05",
        "snippet": "The fake episode makes a claim.",
        "note_path": "Earmark/Podcasts/x.md",
        "transcript_note_path": None,
    }
    assert searcher.calls == [SearchQuery("fake claim", 10, (), None)]


def test_search_no_index_is_empty_200(settings: Settings, store: JobStore) -> None:
    for c in client_for(settings, store, FakeSearcher(index_exists=False)):
        response = c.get("/search", params={"q": "x"})
        assert (response.status_code, response.json()) == (200, {"hits": []})


def test_search_requires_token(settings: Settings, store: JobStore, searcher: FakeSearcher) -> None:
    with TestClient(create_app(settings, store=store, searcher=searcher)) as c:
        assert c.get("/search", params={"q": "x"}).status_code == 401
        wrong = c.get("/search", params={"q": "x"}, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
    assert searcher.calls == []


def test_query_normalised(client: TestClient, searcher: FakeSearcher) -> None:
    response = client.get(
        "/search",
        params=[
            ("q", "  fake \n claim "),
            ("kind", "claim"),
            ("kind", "summary"),
            ("kind", "claim"),
            ("limit", "3"),
            ("episode", KEY),
        ],
    )
    assert response.status_code == 200
    assert searcher.calls == [SearchQuery("fake claim", 3, ("summary", "claim"), KEY)]


@pytest.mark.parametrize(
    ("params", "needle", "message"),
    [
        ({}, None, "q is required"),
        ({"q": "  "}, None, "q must not be blank"),
        ({"q": "z" * 501}, "zzzzz", "q must be at most 500 characters"),
        ({"q": "x", "limit": "0"}, None, "limit must be between 1 and 50"),
        ({"q": "x", "limit": "51"}, None, "limit must be between 1 and 50"),
        ({"q": "x", "limit": "sentinel-abc"}, "sentinel-abc", "limit"),
        ({"q": "x", "kind": "sentinel-entity"}, "sentinel-entity", "kind must be one of"),
        ({"q": "x", "episode": "sentinel-XYZ"}, "sentinel-XYZ", "episode must be an episode key"),
    ],
)
def test_invalid_params(
    client: TestClient,
    searcher: FakeSearcher,
    params: dict[str, str],
    needle: str | None,
    message: str,
) -> None:
    response = client.get("/search", params=params)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert message in detail
    if needle is not None:
        assert needle not in response.text
    assert searcher.calls == []


def test_unconfigured_is_503(settings: Settings, store: JobStore) -> None:
    for c in client_for(settings, store, None):
        response = c.get("/search", params={"q": "x"})
        assert response.status_code == 503
        assert response.json() == {"detail": "search is not configured in this process"}
        assert "retry-after" not in response.headers


def test_unavailable_reason_is_503(client: TestClient, searcher: FakeSearcher) -> None:
    searcher.fail_next(SearchUnavailableError("the index extra is missing"))
    response = client.get("/search", params={"q": "x"})
    assert (response.status_code, response.json()) == (
        503,
        {"detail": "the index extra is missing"},
    )
    assert "retry-after" not in response.headers


def test_transient_is_503_with_retry_after(client: TestClient, searcher: FakeSearcher) -> None:
    searcher.fail_next(TransientError("the embedding model is not downloaded yet"))
    response = client.get("/search", params={"q": "x"})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": "the embedding model is not downloaded yet"}


def test_other_earmark_error_is_503(client: TestClient, searcher: FakeSearcher) -> None:
    searcher.fail_next(EarmarkError("odd"))
    assert client.get("/search", params={"q": "x"}).status_code == 503


def test_mismatch_is_409(client: TestClient, searcher: FakeSearcher) -> None:
    searcher.fail_next(IndexMismatchError("built with another model; run `earmark reindex`"))
    response = client.get("/search", params={"q": "x"})
    assert response.status_code == 409
    assert "earmark reindex" in response.json()["detail"]
