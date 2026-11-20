import json
import multiprocessing
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from earmark.config import Settings
from earmark.db import Database, open_database
from earmark.jobs import (
    LIST_LIMIT_MAX,
    RETRY_MAX_S,
    Claim,
    JobNotFoundError,
    JobStore,
    LeaseLostError,
    open_job_store,
    retry_delay,
)
from earmark.models import JobResult, episode_key
from tests import db_helpers
from tests.factories import PST, T0, MutableClock

URL = "https://podcasts.apple.com/us/podcast/x/id1?i=2"
KEY = episode_key("apple_podcasts", "1", "g")
NAIVE = datetime.fromisoformat("2026-01-01T00:00:00")


def make_store(
    tmp_path: Path, clock: MutableClock, *, max_attempts: int = 4, lease_s: int = 900
) -> JobStore:
    db = open_database(tmp_path / "earmark.db")
    return JobStore(db, clock=clock, max_attempts=max_attempts, lease=timedelta(seconds=lease_s))


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> JobStore:
    return make_store(tmp_path, clock)


def claim_one(store: JobStore) -> Claim:
    claim = store.claim_next()
    assert claim is not None
    return claim


def raw(tmp_path: Path, sql: str) -> list[sqlite3.Row]:
    with Database(tmp_path / "earmark.db").connect() as conn:
        return conn.execute(sql).fetchall()


# --- A1: exactly-once claims ---------------------------------------------------------


def test_claim_returns_none_when_empty(store: JobStore) -> None:
    assert store.claim_next() is None


def test_claim_exactly_once_threads(store: JobStore) -> None:
    ids = [store.enqueue(f"{URL}{i}").job.id for i in range(200)]
    n = 8
    barrier = threading.Barrier(n)
    claimed: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        barrier.wait(timeout=30)
        while (claim := store.claim_next()) is not None:
            with lock:
                claimed.append(claim.job.id)

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(claimed) == sorted(ids)
    jobs = store.list_recent(LIST_LIMIT_MAX)
    assert {j.status for j in jobs} == {"running"}
    assert {j.attempts for j in jobs} == {1}


def test_claim_exactly_once_processes(store: JobStore, tmp_path: Path, clock: MutableClock) -> None:
    ids = [store.enqueue(f"{URL}{i}").job.id for i in range(300)]
    ctx = multiprocessing.get_context("spawn")
    n = 4
    barrier = ctx.Barrier(n)
    outs = [tmp_path / f"claims{i}.json" for i in range(n)]
    procs = [
        ctx.Process(
            target=db_helpers.claim_until_empty,
            args=(str(tmp_path / "earmark.db"), clock().isoformat(), 4, str(o), barrier),
        )
        for o in outs
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    codes = [p.exitcode for p in procs]
    for p in procs:
        p.close()
    assert codes == [0] * n
    claimed = [i for o in outs for i in json.loads(o.read_text())]
    assert sorted(claimed) == sorted(ids)
    jobs = store.list_recent(LIST_LIMIT_MAX)
    assert {j.status for j in jobs} == {"running"}
    assert {j.attempts for j in jobs} == {1}


# --- A2: failure and backoff -----------------------------------------------------------


def test_transient_failure_backs_off_then_fails(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(URL).job.id
    delays = []
    for _ in range(3):
        claim = claim_one(store)
        before = clock()
        job = store.fail(claim, "feed 503", transient=True)
        assert job.status == "queued"
        assert job.error == "feed 503"
        delays.append((job.run_after - before).total_seconds())
        assert store.claim_next() is None
        clock.now = job.run_after
    assert delays == [60, 240, 960]
    claim = claim_one(store)
    job = store.fail(claim, "last", transient=True)
    assert (job.status, job.attempts, job.error) == ("failed", 4, "last")
    clock.advance(10**6)
    assert store.claim_next() is None
    assert store.get(job_id) == job


def test_permanent_failure_fails_immediately(store: JobStore) -> None:
    store.enqueue(URL)
    job = store.fail(claim_one(store), "bad url", transient=False)
    assert (job.status, job.attempts) == ("failed", 1)


def test_retry_after_raises_delay(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(URL)
    job = store.fail(claim_one(store), "x", transient=True, retry_after_s=600)
    assert job.run_after - clock() == timedelta(seconds=600)
    clock.now = job.run_after
    job = store.fail(claim_one(store), "x", transient=True, retry_after_s=1)
    assert job.run_after - clock() == timedelta(seconds=240)


def test_retry_delay_values() -> None:
    assert [retry_delay(n).total_seconds() for n in range(1, 6)] == [60, 240, 960, 3840, 15360]
    assert retry_delay(6).total_seconds() == RETRY_MAX_S
    assert retry_delay(10**9).total_seconds() == RETRY_MAX_S
    assert retry_delay(1, 10**9).total_seconds() == RETRY_MAX_S
    with pytest.raises(ValueError, match="attempts"):
        retry_delay(0)
    with pytest.raises(ValueError, match="retry_after_s"):
        retry_delay(1, -1)
    with pytest.raises(ValueError, match="retry_after_s"):
        retry_delay(1, float("inf"))


def test_max_attempts_one(tmp_path: Path, clock: MutableClock) -> None:
    store = make_store(tmp_path, clock, max_attempts=1)
    store.enqueue(URL)
    assert store.fail(claim_one(store), "x", transient=True).status == "failed"


def test_invalid_store_arguments(tmp_path: Path, clock: MutableClock) -> None:
    db = open_database(tmp_path / "earmark.db")
    with pytest.raises(ValueError, match="max_attempts"):
        JobStore(db, max_attempts=0)
    with pytest.raises(ValueError, match="lease"):
        JobStore(db, lease=timedelta(0))


# --- A3: leases ----------------------------------------------------------------------


def test_requeue_stale_boundary(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(URL).job.id
    claim_one(store)
    clock.advance(900)
    assert store.requeue_stale() == []
    running = store.get(job_id)
    assert running is not None
    assert running.status == "running"
    clock.advance(0.000001)
    stale = store.requeue_stale()
    assert [j.id for j in stale] == [job_id]
    job = store.get(job_id)
    assert job is not None
    assert job.status == "queued"
    assert job.error == "worker stopped responding (no heartbeat for 900 s)"
    assert stale == [job]
    assert claim_one(store).job.attempts == 2


def test_fresh_heartbeat_not_requeued(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(URL).job.id
    claim = claim_one(store)
    clock.advance(800)
    store.heartbeat(claim)
    clock.advance(200)
    assert store.requeue_stale() == []
    job = store.get(job_id)
    assert job is not None
    assert job.status == "running"
    assert job.heartbeat_at == clock() - timedelta(seconds=200)
    assert job.updated_at == T0


def test_set_stage_refreshes_heartbeat(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(URL).job.id
    claim = claim_one(store)
    clock.advance(800)
    store.set_stage(claim, "fetch")
    clock.advance(200)
    assert store.requeue_stale() == []
    job = store.get(job_id)
    assert job is not None
    assert (job.stage, job.heartbeat_at) == ("fetch", T0 + timedelta(seconds=800))


def test_requeue_stale_fails_at_max_attempts(tmp_path: Path, clock: MutableClock) -> None:
    store = make_store(tmp_path, clock, max_attempts=1)
    store.enqueue(URL)
    claim_one(store)
    clock.advance(1000)
    (job,) = store.requeue_stale()
    assert job.status == "failed"
    assert job.error is not None
    assert store.claim_next() is None


FencedCall = Callable[[JobStore, Claim], object]

FENCED: list[tuple[str, FencedCall]] = [
    ("heartbeat", lambda s, c: s.heartbeat(c)),
    ("set_stage", lambda s, c: s.set_stage(c, "fetch")),
    ("set_episode_key", lambda s, c: s.set_episode_key(c, KEY)),
    ("complete", lambda s, c: s.complete(c, JobResult(note_path="a.md"))),
    ("fail", lambda s, c: s.fail(c, "e", transient=True)),
    ("skip", lambda s, c: s.skip(c, "r")),
    ("release", lambda s, c: s.release(c)),
]
FENCED_IDS = [name for name, _ in FENCED]


@pytest.mark.parametrize("call", [c for _, c in FENCED], ids=FENCED_IDS)
def test_old_claim_cannot_act_after_requeue(
    store: JobStore, clock: MutableClock, call: FencedCall
) -> None:
    job_id = store.enqueue(URL).job.id
    old = claim_one(store)
    clock.advance(1000)
    store.requeue_stale()
    before = store.get(job_id)
    with pytest.raises(LeaseLostError):
        call(store, old)
    assert store.get(job_id) == before


@pytest.mark.parametrize("call", [c for _, c in FENCED], ids=FENCED_IDS)
def test_fenced_after_terminal(store: JobStore, call: FencedCall) -> None:
    job_id = store.enqueue(URL).job.id
    claim = claim_one(store)
    done = store.complete(claim)
    with pytest.raises(LeaseLostError):
        call(store, claim)
    assert store.get(job_id) == done


# --- A4: dedupe ----------------------------------------------------------------------


def test_dedupe_while_queued(store: JobStore) -> None:
    a = store.enqueue(URL, prompt="p")
    b = store.enqueue(URL, prompt="p")
    assert (a.created, b.created) == (True, False)
    assert a.job.id == b.job.id
    assert len(store.list_recent()) == 1


def test_dedupe_while_running(store: JobStore) -> None:
    a = store.enqueue(URL)
    claim_one(store)
    b = store.enqueue(URL)
    assert not b.created
    assert b.job.id == a.job.id
    assert b.job.status == "running"


def test_dedupe_while_waiting_for_retry(store: JobStore) -> None:
    a = store.enqueue(URL)
    store.fail(claim_one(store), "x", transient=True)
    b = store.enqueue(URL)
    assert not b.created
    assert b.job.id == a.job.id


def test_dedupe_keeps_first_shared_at(store: JobStore, clock: MutableClock) -> None:
    a = store.enqueue(URL)
    clock.advance(5)
    b = store.enqueue(URL, shared_at=clock())
    assert b.job.shared_at == a.job.shared_at == T0


@pytest.mark.parametrize("how", ["complete", "skip", "fail"])
def test_new_job_after_terminal(store: JobStore, how: str) -> None:
    a = store.enqueue(URL)
    claim = claim_one(store)
    if how == "complete":
        store.complete(claim)
    elif how == "skip":
        store.skip(claim, "nothing to do")
    else:
        store.fail(claim, "bad", transient=False)
    b = store.enqueue(URL)
    assert b.created
    assert b.job.id != a.job.id


def test_dedupe_distinguishes_prompt(store: JobStore) -> None:
    ids = {store.enqueue(URL, prompt=p).job.id for p in (None, "", "a", "b")}
    ids.add(store.enqueue(URL + "3").job.id)
    assert len(ids) == 5


def test_dedupe_concurrent_threads(store: JobStore) -> None:
    n = 8
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def work() -> None:
        barrier.wait(timeout=30)
        r = store.enqueue(URL, prompt="p")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == n
    assert len({r.job.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert len(store.list_recent()) == 1


def test_reanalyze_dedupe(store: JobStore) -> None:
    a = store.enqueue_reanalyze(KEY, prompt="p")
    assert store.enqueue_reanalyze(KEY, prompt="p").job.id == a.job.id
    assert store.enqueue_reanalyze(KEY, prompt="q").created
    assert store.enqueue(KEY, prompt="p").created
    assert store.enqueue(URL, prompt="p").job.id != a.job.id


# --- A6: ordering --------------------------------------------------------------------


def test_claim_oldest_first(store: JobStore, clock: MutableClock) -> None:
    ids = []
    for i in range(3):
        ids.append(store.enqueue(f"{URL}{i}").job.id)
        clock.advance(1)
    assert [claim_one(store).job.id for _ in range(3)] == ids


def test_claim_ties_by_insertion_order(store: JobStore) -> None:
    ids = [store.enqueue(f"{URL}{i}").job.id for i in range(5)]
    assert [claim_one(store).job.id for _ in range(5)] == ids


def test_future_run_after_not_claimed(store: JobStore, clock: MutableClock) -> None:
    first = store.enqueue(URL)
    retry = store.fail(claim_one(store), "x", transient=True)
    clock.advance(1)
    newer = store.enqueue(URL + "2")
    assert claim_one(store).job.id == newer.job.id
    assert store.claim_next() is None
    clock.now = retry.run_after - timedelta(microseconds=1)
    assert store.claim_next() is None
    clock.now = retry.run_after
    assert claim_one(store).job.id == first.job.id


def test_retried_job_keeps_age_order(store: JobStore, clock: MutableClock) -> None:
    first = store.enqueue(URL)
    clock.advance(1)
    second = store.enqueue(URL + "2")
    store.fail(claim_one(store), "x", transient=True)  # first goes back with a delay
    assert claim_one(store).job.id == second.job.id
    clock.advance(3600)
    assert claim_one(store).job.id == first.job.id


# --- B4: fencing ---------------------------------------------------------------------


def test_release_undoes_attempt(store: JobStore) -> None:
    store.enqueue(URL)
    claim = claim_one(store)
    store.set_stage(claim, "fetch")
    store.add_warning(claim.job.id, "w")
    job = store.release(claim)
    assert (job.status, job.attempts, job.stage, job.warnings) == ("queued", 0, "fetch", ("w",))
    again = claim_one(store)
    assert again.job.attempts == 1


def test_release_keeps_error(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(URL)
    retry = store.fail(claim_one(store), "why", transient=True)
    clock.now = retry.run_after
    assert store.release(claim_one(store)).error == "why"


def test_old_claim_after_release_and_reclaim(store: JobStore) -> None:
    store.enqueue(URL)
    old = claim_one(store)
    store.release(old)
    new = claim_one(store)
    assert new.job.attempts == old.job.attempts
    with pytest.raises(LeaseLostError):
        store.complete(old)
    assert store.complete(new).status == "done"


# --- B5: stored fields ---------------------------------------------------------------


def test_complete_stores_result(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(URL).job.id
    claim = claim_one(store)
    retry = store.fail(claim, "transient", transient=True)
    assert retry.error == "transient"
    clock.now = retry.run_after
    result = JobResult(note_path="Earmark/x.md", transcript_note_path="Earmark/y.md")
    job = store.complete(claim_one(store), result)
    assert job.status == "done"
    assert job.error is None
    assert job.result == result
    assert store.get(job_id) == job


def test_add_warning_appends_and_dedupes(store: JobStore) -> None:
    job_id = store.enqueue(URL).job.id
    for msg in ["é ☕", 'say "hi"', "é ☕", "it's"]:
        store.add_warning(job_id, msg)
    job = store.get(job_id)
    assert job is not None
    assert job.warnings == ("é ☕", 'say "hi"', "it's")


def test_add_warning_on_terminal_job(store: JobStore) -> None:
    job_id = store.enqueue(URL).job.id
    store.complete(claim_one(store))
    store.add_warning(job_id, "notifier failed")
    job = store.get(job_id)
    assert job is not None
    assert (job.status, job.warnings) == ("done", ("notifier failed",))


def test_add_warning_unknown_job(store: JobStore) -> None:
    with pytest.raises(JobNotFoundError):
        store.add_warning("0" * 32, "w")


def test_set_stage_and_episode_key(store: JobStore) -> None:
    job_id = store.enqueue(URL).job.id
    claim = claim_one(store)
    store.set_stage(claim, "transcribe")
    store.set_episode_key(claim, KEY)
    with pytest.raises(ValueError, match="episode_key"):
        store.set_episode_key(claim, "nope")
    job = store.get(job_id)
    assert job is not None
    assert (job.stage, job.episode_key) == ("transcribe", KEY)


def test_skip_records_reason(store: JobStore) -> None:
    store.enqueue(URL)
    job = store.skip(claim_one(store), "not a podcast")
    assert (job.status, job.error) == ("skipped", "not a podcast")


# --- B6: timestamps ------------------------------------------------------------------


def test_shared_at_offset_round_trips(store: JobStore) -> None:
    shared = datetime(2026, 1, 1, 4, 0, tzinfo=PST)
    job = store.enqueue(URL, shared_at=shared).job
    got = store.get(job.id)
    assert got is not None
    assert got.shared_at == shared
    assert got.shared_at.tzinfo is UTC
    assert got == job


def test_shared_at_defaults_to_now(store: JobStore) -> None:
    assert store.enqueue(URL).job.shared_at == T0


def test_naive_datetimes_refused(store: JobStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        store.enqueue(URL, shared_at=NAIVE)
    naive = JobStore(Database(tmp_path / "earmark.db"), clock=lambda: NAIVE)
    with pytest.raises(ValueError, match="timezone"):
        naive.enqueue(URL)
    assert raw(tmp_path, "SELECT count(*) FROM jobs")[0][0] == 0


def test_stored_timestamps_sort_as_text(store: JobStore, tmp_path: Path) -> None:
    store.enqueue(URL, shared_at=datetime(2026, 1, 1, 4, 0, tzinfo=PST))
    (row,) = raw(tmp_path, "SELECT run_after, shared_at, created_at, updated_at FROM jobs")
    assert row["shared_at"] == "2026-01-01T12:00:00.000000+00:00"
    assert tuple(row) == ("2026-01-01T12:00:00.000000+00:00",) * 4


# --- B7: lookups ---------------------------------------------------------------------


def test_get_unknown_and_malformed(store: JobStore) -> None:
    assert store.get("0" * 32) is None
    assert store.get("../x") is None
    assert store.get("") is None


def test_list_recent_order_limit_status(store: JobStore, clock: MutableClock) -> None:
    ids = []
    for i in range(4):
        ids.append(store.enqueue(f"{URL}{i}").job.id)
        clock.advance(1)
    store.complete(claim_one(store))  # the oldest
    assert [j.id for j in store.list_recent()] == ids[::-1]
    assert [j.id for j in store.list_recent(2)] == ids[:1:-1]
    assert [j.id for j in store.list_recent(status="done")] == [ids[0]]
    assert len(store.list_recent(status="queued")) == 3
    assert store.list_recent(status="failed") == []


def test_list_recent_limit_bounds(store: JobStore) -> None:
    for bad in (0, LIST_LIMIT_MAX + 1):
        with pytest.raises(ValueError, match="limit"):
            store.list_recent(bad)
    store.list_recent(LIST_LIMIT_MAX)


# --- B8: reanalyze -------------------------------------------------------------------


def test_enqueue_reanalyze(store: JobStore) -> None:
    job = store.enqueue_reanalyze(KEY, prompt="again").job
    assert (job.kind, job.url, job.episode_key, job.prompt) == ("reanalyze", None, KEY, "again")
    with pytest.raises(ValueError, match="episode_key"):
        store.enqueue_reanalyze("bad")
    assert claim_one(store).job.id == job.id


def test_enqueue_blank_url_refused(store: JobStore) -> None:
    with pytest.raises(ValueError, match="url"):
        store.enqueue("  ")


# --- B9: settings --------------------------------------------------------------------


def test_open_job_store_from_settings(tmp_path: Path, clock: MutableClock) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "d", job_max_attempts=2, job_lease_seconds=60
    )
    store = open_job_store(settings, clock=clock)
    assert (tmp_path / "d" / "earmark.db").exists()
    store.enqueue(URL)
    retry = store.fail(claim_one(store), "x", transient=True)
    clock.now = retry.run_after
    assert store.fail(claim_one(store), "y", transient=True).status == "failed"
    store.enqueue(URL + "2")
    claim_one(store)
    clock.advance(60)
    assert store.requeue_stale() == []
    clock.advance(1)
    assert len(store.requeue_stale()) == 1


# --- B10: validation -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "  "])
def test_blank_messages_refused(store: JobStore, text: str) -> None:
    job_id = store.enqueue(URL).job.id
    claim = claim_one(store)
    with pytest.raises(ValueError, match="error"):
        store.fail(claim, text, transient=False)
    with pytest.raises(ValueError, match="reason"):
        store.skip(claim, text)
    with pytest.raises(ValueError, match="message"):
        store.add_warning(job_id, text)
    assert store.get(job_id) == claim.job.model_copy(update={"updated_at": T0})
