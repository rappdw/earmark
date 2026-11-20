from pathlib import Path

import pytest

from earmark.db import open_database
from earmark.ingest import enqueue_ingest
from earmark.jobs import JobStore
from earmark.models import IngestRequest
from earmark.resolve.apple import UnsupportedUrlError
from tests.factories import T0, MutableClock

SHOW = "1200361736"
EP = "1000802907981"
CANON = f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}"


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(open_database(tmp_path / "earmark.db"), clock=MutableClock())


def test_variants_dedupe(store: JobStore) -> None:
    variants = [
        f"https://podcasts.apple.com/us/podcast/the-slug/id{SHOW}?i={EP}&uo=4",
        f"https://podcasts.apple.com/us/podcast/id{SHOW}?i={EP}",
        f"https://itunes.apple.com/us/podcast/x/id{SHOW}?i={EP}&mt=2",
        f"https://podcasts.apple.com/us/podcast/x/id{SHOW}?i={EP}&r=754",
        f"http://podcasts.apple.com/US/podcast/x/id{SHOW}?i={EP}",
    ]
    results = [enqueue_ingest(store, IngestRequest(url=u)) for u in variants]
    assert [r.created for r in results] == [True, False, False, False, False]
    assert len({r.job.id for r in results}) == 1
    assert results[0].job.url == CANON
    assert len(store.list_recent()) == 1


def test_prompt_and_country_distinguish(store: JobStore) -> None:
    def go(url: str, prompt: str | None) -> str:
        return enqueue_ingest(store, IngestRequest(url=url, prompt=prompt)).job.id

    gb = f"https://podcasts.apple.com/gb/podcast/id{SHOW}?i={EP}"
    ids = [go(CANON, "a"), go(CANON, None), go(CANON, "b"), go(gb, None)]
    assert len(set(ids)) == 4
    assert go(CANON, "  a ") == ids[0]


def test_timestamp_becomes_shared_at(store: JobStore) -> None:
    job = enqueue_ingest(store, IngestRequest(url=CANON, timestamp=T0)).job
    assert job.shared_at == T0


def test_unsupported_url_enqueues_nothing(store: JobStore) -> None:
    with pytest.raises(UnsupportedUrlError) as exc:
        enqueue_ingest(store, IngestRequest(url="https://open.spotify.com/episode/x"))
    assert exc.value.problem == "unsupported_host"
    with pytest.raises(UnsupportedUrlError) as exc:
        enqueue_ingest(store, IngestRequest(url=f"https://podcasts.apple.com/us/podcast/id{SHOW}"))
    assert exc.value.problem == "show_link"
    assert store.list_recent() == []
