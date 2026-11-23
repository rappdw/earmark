import json
import multiprocessing
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
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
    NewJob,
    open_job_store,
    retry_delay,
)
from earmark.models import JobResult, episode_key
from tests import db_helpers
from tests.factories import (
    PST,
    T0,
    TO,
    MutableClock,
    new_podcast_job,
    new_upload_job,
    new_url_job,
)

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
    ids = [store.enqueue(new_podcast_job(f"{URL}{i}")).job.id for i in range(200)]
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
    ids = [store.enqueue(new_podcast_job(f"{URL}{i}")).job.id for i in range(300)]
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
    job_id = store.enqueue(new_podcast_job(URL)).job.id
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
    store.enqueue(new_podcast_job(URL))
    job = store.fail(claim_one(store), "bad url", transient=False)
    assert (job.status, job.attempts) == ("failed", 1)


def test_retry_after_raises_delay(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(new_podcast_job(URL))
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
    store.enqueue(new_podcast_job(URL))
    assert store.fail(claim_one(store), "x", transient=True).status == "failed"


def test_invalid_store_arguments(tmp_path: Path, clock: MutableClock) -> None:
    db = open_database(tmp_path / "earmark.db")
    with pytest.raises(ValueError, match="max_attempts"):
        JobStore(db, max_attempts=0)
    with pytest.raises(ValueError, match="lease"):
        JobStore(db, lease=timedelta(0))


# --- A3: leases ----------------------------------------------------------------------


def test_requeue_stale_boundary(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
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
    job_id = store.enqueue(new_podcast_job(URL)).job.id
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
    job_id = store.enqueue(new_podcast_job(URL)).job.id
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
    store.enqueue(new_podcast_job(URL))
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
    ("complete", lambda s, c: s.complete(c, JobResult(duration_s=12.5))),
    ("fail", lambda s, c: s.fail(c, "e", transient=True)),
    ("skip", lambda s, c: s.skip(c, "r")),
    ("release", lambda s, c: s.release(c)),
]
FENCED_IDS = [name for name, _ in FENCED]


@pytest.mark.parametrize("call", [c for _, c in FENCED], ids=FENCED_IDS)
def test_old_claim_cannot_act_after_requeue(
    store: JobStore, clock: MutableClock, call: FencedCall
) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    old = claim_one(store)
    clock.advance(1000)
    store.requeue_stale()
    before = store.get(job_id)
    with pytest.raises(LeaseLostError):
        call(store, old)
    assert store.get(job_id) == before


@pytest.mark.parametrize("call", [c for _, c in FENCED], ids=FENCED_IDS)
def test_fenced_after_terminal(store: JobStore, call: FencedCall) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    claim = claim_one(store)
    done = store.complete(claim)
    with pytest.raises(LeaseLostError):
        call(store, claim)
    assert store.get(job_id) == done


# --- A4: dedupe ----------------------------------------------------------------------


def test_dedupe_while_queued(store: JobStore) -> None:
    a = store.enqueue(new_podcast_job(URL, instructions="p"))
    b = store.enqueue(new_podcast_job(URL, instructions="p"))
    assert (a.created, b.created) == (True, False)
    assert a.job.id == b.job.id
    assert len(store.list_recent()) == 1


def test_dedupe_while_running(store: JobStore) -> None:
    a = store.enqueue(new_podcast_job(URL))
    claim_one(store)
    b = store.enqueue(new_podcast_job(URL))
    assert not b.created
    assert b.job.id == a.job.id
    assert b.job.status == "running"


def test_dedupe_while_waiting_for_retry(store: JobStore) -> None:
    a = store.enqueue(new_podcast_job(URL))
    store.fail(claim_one(store), "x", transient=True)
    b = store.enqueue(new_podcast_job(URL))
    assert not b.created
    assert b.job.id == a.job.id


def test_dedupe_keeps_first_shared_at(store: JobStore, clock: MutableClock) -> None:
    a = store.enqueue(new_podcast_job(URL))
    clock.advance(5)
    b = store.enqueue(new_podcast_job(URL, shared_at=clock()))
    assert b.job.shared_at == a.job.shared_at == T0


@pytest.mark.parametrize("how", ["skip", "fail"])
def test_new_job_after_failed_or_skipped(store: JobStore, how: str) -> None:
    a = store.enqueue(new_podcast_job(URL))
    claim = claim_one(store)
    if how == "skip":
        store.skip(claim, "nothing to do")
    else:
        store.fail(claim, "bad", transient=False)
    b = store.enqueue(new_podcast_job(URL))
    assert b.created
    assert b.job.id != a.job.id


def finish_done(store: JobStore) -> None:
    store.complete(claim_one(store))


def test_dedupe_active_returns_same(store: JobStore) -> None:
    a = store.enqueue(new_podcast_job(URL, instructions="x"))
    b = store.enqueue(new_podcast_job(URL, instructions="x"))
    assert (a.created, b.created) == (True, False)
    assert a.job.id == b.job.id


def test_dedupe_done_within_window_returns_same(store: JobStore, clock: MutableClock) -> None:
    a = store.enqueue(new_podcast_job(URL))
    finish_done(store)
    clock.advance(599)
    b = store.enqueue(new_podcast_job(URL))
    assert not b.created
    assert b.job.id == a.job.id
    assert b.job.status == "done"


def test_dedupe_done_after_window_creates_new(store: JobStore, clock: MutableClock) -> None:
    a = store.enqueue(new_podcast_job(URL))
    finish_done(store)
    clock.advance(601)
    b = store.enqueue(new_podcast_job(URL))
    assert b.created
    assert b.job.id != a.job.id


def test_dedupe_window_zero_active_only(tmp_path: Path, clock: MutableClock) -> None:
    db = open_database(tmp_path / "earmark.db")
    store = JobStore(db, clock=clock, dedupe_window=timedelta(0))
    a = store.enqueue(new_podcast_job(URL))
    assert not store.enqueue(new_podcast_job(URL)).created
    finish_done(store)
    b = store.enqueue(new_podcast_job(URL))
    assert b.created
    assert b.job.id != a.job.id


def test_dedupe_prefers_active_over_done(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(new_podcast_job(URL))
    finish_done(store)
    clock.advance(700)
    active = store.enqueue(new_podcast_job(URL))
    assert active.created
    again = store.enqueue(new_podcast_job(URL))
    assert (again.created, again.job.id) == (False, active.job.id)


def test_negative_dedupe_window_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dedupe_window"):
        JobStore(open_database(tmp_path / "e.db"), dedupe_window=timedelta(seconds=-1))


def test_new_to_new_job(store: JobStore) -> None:
    a = store.enqueue(new_podcast_job(URL, to="a@example.org"))
    b = store.enqueue(new_podcast_job(URL, to="b@example.org"))
    assert b.created
    assert a.job.id != b.job.id
    assert (a.job.to, b.job.to) == ("a@example.org", "b@example.org")


def test_new_instructions_new_job(store: JobStore) -> None:
    ids = {store.enqueue(new_podcast_job(URL, instructions=p)).job.id for p in ("", "a", "b")}
    ids.add(store.enqueue(new_podcast_job(URL + "3")).job.id)
    assert len(ids) == 4


def test_title_change_same_job(store: JobStore) -> None:
    a = store.enqueue(new_podcast_job(URL, title="one"))
    b = store.enqueue(new_podcast_job(URL, title="two"))
    assert not b.created
    assert b.job.id == a.job.id
    assert b.job.title == "one"


def test_dedupe_distinguishes_source_and_subject(store: JobStore) -> None:
    ids = {
        store.enqueue(new_podcast_job(URL)).job.id,
        store.enqueue(new_url_job(URL)).job.id,
        store.enqueue(new_upload_job("a" * 64)).job.id,
        store.enqueue(new_upload_job("b" * 64)).job.id,
    }
    assert len(ids) == 4
    again = store.enqueue(new_upload_job("a" * 64, upload_file="f" * 32 + ".mp3"))
    assert not again.created


def test_enqueue_rejects_inconsistent_jobs(store: JobStore) -> None:
    bad = [
        NewJob(source="upload", to=TO, url=URL),
        NewJob(source="upload", to=TO, upload_file="a" * 32 + ".mp3"),
        NewJob(source="url", to=TO),
        NewJob(source="url", to=TO, url=URL, upload_file="a" * 32 + ".mp3"),
        NewJob(source="apple_podcasts", to=" ", url=URL),
    ]
    for new in bad:
        with pytest.raises(ValueError, match=r"upload|url|to must"):
            store.enqueue(new)
    assert store.list_recent() == []


def test_request_fields_round_trip(store: JobStore) -> None:
    new = new_upload_job("c" * 64, instructions="Why?\nHow?", title="T", upload_name="x y.mp3")
    job = store.enqueue(new).job
    assert store.get(job.id) == job
    assert (job.source, job.url, job.to, job.instructions) == ("upload", None, TO, "Why?\nHow?")
    assert (job.upload_name, job.content_sha256) == ("x y.mp3", "c" * 64)


def test_active_upload_files_and_running_ids(store: JobStore) -> None:
    up = store.enqueue(new_upload_job("a" * 64)).job
    store.enqueue(new_url_job(URL))
    assert store.active_upload_files() == {up.upload_file}
    assert store.running_job_ids() == set()
    claim = claim_one(store)
    assert store.running_job_ids() == {claim.job.id}
    store.complete(claim)
    assert store.active_upload_files() == set()


def test_dedupe_concurrent_threads(store: JobStore) -> None:
    n = 8
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def work() -> None:
        barrier.wait(timeout=30)
        r = store.enqueue(new_podcast_job(URL, instructions="p"))
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


# --- A6: ordering --------------------------------------------------------------------


def test_claim_oldest_first(store: JobStore, clock: MutableClock) -> None:
    ids = []
    for i in range(3):
        ids.append(store.enqueue(new_podcast_job(f"{URL}{i}")).job.id)
        clock.advance(1)
    assert [claim_one(store).job.id for _ in range(3)] == ids


def test_claim_ties_by_insertion_order(store: JobStore) -> None:
    ids = [store.enqueue(new_podcast_job(f"{URL}{i}")).job.id for i in range(5)]
    assert [claim_one(store).job.id for _ in range(5)] == ids


def test_future_run_after_not_claimed(store: JobStore, clock: MutableClock) -> None:
    first = store.enqueue(new_podcast_job(URL))
    retry = store.fail(claim_one(store), "x", transient=True)
    clock.advance(1)
    newer = store.enqueue(new_podcast_job(URL + "2"))
    assert claim_one(store).job.id == newer.job.id
    assert store.claim_next() is None
    clock.now = retry.run_after - timedelta(microseconds=1)
    assert store.claim_next() is None
    clock.now = retry.run_after
    assert claim_one(store).job.id == first.job.id


def test_retried_job_keeps_age_order(store: JobStore, clock: MutableClock) -> None:
    first = store.enqueue(new_podcast_job(URL))
    clock.advance(1)
    second = store.enqueue(new_podcast_job(URL + "2"))
    store.fail(claim_one(store), "x", transient=True)  # first goes back with a delay
    assert claim_one(store).job.id == second.job.id
    clock.advance(3600)
    assert claim_one(store).job.id == first.job.id


# --- B4: fencing ---------------------------------------------------------------------


def test_release_undoes_attempt(store: JobStore) -> None:
    store.enqueue(new_podcast_job(URL))
    claim = claim_one(store)
    store.set_stage(claim, "fetch")
    store.add_warning(claim.job.id, "w")
    job = store.release(claim)
    assert (job.status, job.attempts, job.stage, job.warnings) == ("queued", 0, "fetch", ("w",))
    again = claim_one(store)
    assert again.job.attempts == 1


def test_release_keeps_error(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(new_podcast_job(URL))
    retry = store.fail(claim_one(store), "why", transient=True)
    clock.now = retry.run_after
    assert store.release(claim_one(store)).error == "why"


def test_old_claim_after_release_and_reclaim(store: JobStore) -> None:
    store.enqueue(new_podcast_job(URL))
    old = claim_one(store)
    store.release(old)
    new = claim_one(store)
    assert new.job.attempts == old.job.attempts
    with pytest.raises(LeaseLostError):
        store.complete(old)
    assert store.complete(new).status == "done"


# --- B5: stored fields ---------------------------------------------------------------


def test_complete_stores_result(store: JobStore, clock: MutableClock) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    claim = claim_one(store)
    retry = store.fail(claim, "transient", transient=True)
    assert retry.error == "transient"
    clock.now = retry.run_after
    result = JobResult(duration_s=12.5)
    job = store.complete(claim_one(store), result)
    assert job.status == "done"
    assert job.error is None
    assert job.result == result
    assert store.get(job_id) == job


def test_add_warning_appends_and_dedupes(store: JobStore) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    for msg in ["é ☕", 'say "hi"', "é ☕", "it's"]:
        store.add_warning(job_id, msg)
    job = store.get(job_id)
    assert job is not None
    assert job.warnings == ("é ☕", 'say "hi"', "it's")


def test_add_warning_on_terminal_job(store: JobStore) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    store.complete(claim_one(store))
    store.add_warning(job_id, "notifier failed")
    job = store.get(job_id)
    assert job is not None
    assert (job.status, job.warnings) == ("done", ("notifier failed",))


def test_add_warning_unknown_job(store: JobStore) -> None:
    with pytest.raises(JobNotFoundError):
        store.add_warning("0" * 32, "w")


def test_set_stage_and_episode_key(store: JobStore) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    claim = claim_one(store)
    store.set_stage(claim, "transcribe")
    store.set_episode_key(claim, KEY)
    with pytest.raises(ValueError, match="episode_key"):
        store.set_episode_key(claim, "nope")
    job = store.get(job_id)
    assert job is not None
    assert (job.stage, job.episode_key) == ("transcribe", KEY)


def test_skip_records_reason(store: JobStore) -> None:
    store.enqueue(new_podcast_job(URL))
    job = store.skip(claim_one(store), "not a podcast")
    assert (job.status, job.error) == ("skipped", "not a podcast")


# --- B6: timestamps ------------------------------------------------------------------


def test_shared_at_offset_round_trips(store: JobStore) -> None:
    shared = datetime(2026, 1, 1, 4, 0, tzinfo=PST)
    job = store.enqueue(new_podcast_job(URL, shared_at=shared)).job
    got = store.get(job.id)
    assert got is not None
    assert got.shared_at == shared
    assert got.shared_at.tzinfo is UTC
    assert got == job


def test_shared_at_defaults_to_now(store: JobStore) -> None:
    assert store.enqueue(new_podcast_job(URL)).job.shared_at == T0


def test_naive_datetimes_refused(store: JobStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        store.enqueue(new_podcast_job(URL, shared_at=NAIVE))
    naive = JobStore(Database(tmp_path / "earmark.db"), clock=lambda: NAIVE)
    with pytest.raises(ValueError, match="timezone"):
        naive.enqueue(new_podcast_job(URL))
    assert raw(tmp_path, "SELECT count(*) FROM jobs")[0][0] == 0


def test_stored_timestamps_sort_as_text(store: JobStore, tmp_path: Path) -> None:
    store.enqueue(new_podcast_job(URL, shared_at=datetime(2026, 1, 1, 4, 0, tzinfo=PST)))
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
        ids.append(store.enqueue(new_podcast_job(f"{URL}{i}")).job.id)
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


def test_enqueue_blank_url_refused(store: JobStore) -> None:
    with pytest.raises(ValueError, match="url"):
        store.enqueue(new_podcast_job("  "))


# --- B9: settings --------------------------------------------------------------------


def test_open_job_store_from_settings(tmp_path: Path, clock: MutableClock) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "d", job_max_attempts=2, job_lease_seconds=60
    )
    store = open_job_store(settings, clock=clock)
    assert (tmp_path / "d" / "earmark.db").exists()
    store.enqueue(new_podcast_job(URL))
    retry = store.fail(claim_one(store), "x", transient=True)
    clock.now = retry.run_after
    assert store.fail(claim_one(store), "y", transient=True).status == "failed"
    store.enqueue(new_podcast_job(URL + "2"))
    claim_one(store)
    clock.advance(60)
    assert store.requeue_stale() == []
    clock.advance(1)
    assert len(store.requeue_stale()) == 1


# --- B10: validation -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "  "])
def test_blank_messages_refused(store: JobStore, text: str) -> None:
    job_id = store.enqueue(new_podcast_job(URL)).job.id
    claim = claim_one(store)
    with pytest.raises(ValueError, match="error"):
        store.fail(claim, text, transient=False)
    with pytest.raises(ValueError, match="reason"):
        store.skip(claim, text)
    with pytest.raises(ValueError, match="message"):
        store.add_warning(job_id, text)
    assert store.get(job_id) == claim.job.model_copy(update={"updated_at": T0})


# -- delivery state and the requester's offset (step 25) ---------------------------------------


@pytest.mark.parametrize(
    ("shared_at", "minutes"),
    [
        (datetime.fromisoformat("2026-01-01T00:00:00-08:00"), -480),
        (datetime.fromisoformat("2026-01-01T00:00:00+05:30"), 330),
        (datetime.fromisoformat("2026-01-01T00:00:00+00:00"), 0),
        (datetime(2026, 1, 1, tzinfo=UTC), 0),
        (datetime.fromisoformat("2026-01-01T00:00:00+23:59"), 1439),
        (None, None),
    ],
)
def test_enqueue_stores_request_offset(
    store: JobStore, shared_at: datetime | None, minutes: int | None
) -> None:
    enqueued = store.enqueue(new_podcast_job(shared_at=shared_at))
    assert enqueued.job.utc_offset_min == minutes
    again = store.get(enqueued.job.id)
    assert again is not None
    assert again.utc_offset_min == minutes
    if shared_at is not None:
        assert again.shared_at == shared_at  # the instant is unchanged; only the offset is kept


def test_enqueue_refuses_an_offset_with_seconds(store: JobStore) -> None:
    odd = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1, seconds=15)))
    with pytest.raises(ValueError, match="whole number of minutes"):
        store.enqueue(new_podcast_job(shared_at=odd))
    too_far = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=24) - timedelta(seconds=1)))
    with pytest.raises(ValueError, match="whole number of minutes"):
        store.enqueue(new_podcast_job(shared_at=too_far))


def test_mark_sending_then_complete_sent(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(new_podcast_job())
    claim = claim_one(store)
    assert (claim.job.delivery_state, claim.job.resent) == (None, False)
    clock.advance(5)
    sending = store.mark_sending(claim, "<m@example.org>")
    assert (sending.delivery_state, sending.message_id, sending.resent) == (
        "sending",
        "<m@example.org>",
        False,
    )
    assert sending.heartbeat_at == clock.now
    assert sending.sent_at is None
    clock.advance(5)
    done = store.complete_sent(claim, JobResult(duration_s=3.0))
    assert (done.status, done.delivery_state, done.resent, done.error) == (
        "done",
        "sent",
        False,
        None,
    )
    assert done.sent_at == clock.now
    assert done.result == JobResult(duration_s=3.0)
    with pytest.raises(LeaseLostError):  # a done job holds no lease
        store.complete_sent(claim, JobResult(duration_s=3.0))
    with pytest.raises(LeaseLostError):
        store.mark_sending(claim, "<m@example.org>")


def test_second_mark_sending_sets_resent(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(new_podcast_job())
    first = claim_one(store)
    store.mark_sending(first, "<m@example.org>")
    clock.advance(1000)
    [stale] = store.requeue_stale()
    assert (stale.status, stale.delivery_state, stale.resent) == ("queued", "sending", False)
    second = claim_one(store)
    assert second.job.delivery_state == "sending"  # the claim shows an interrupted delivery
    resent = store.mark_sending(second, "<m@example.org>")
    assert (resent.delivery_state, resent.resent) == ("sending", True)
    with pytest.raises(LeaseLostError):  # the old claim was fenced out
        store.complete_sent(first, JobResult(duration_s=1.0))
    assert store.complete_sent(second, JobResult(duration_s=1.0)).resent is True


def test_clear_sending_only_clears_sending(store: JobStore) -> None:
    store.enqueue(new_podcast_job())
    claim = claim_one(store)
    assert store.clear_sending(claim).delivery_state is None  # nothing to clear: no change
    store.mark_sending(claim, "<m@example.org>")
    cleared = store.clear_sending(claim)
    assert (cleared.delivery_state, cleared.resent) == (None, False)
    assert cleared.message_id == "<m@example.org>"  # kept for the record
    again = store.mark_sending(claim, "<m@example.org>")
    assert (again.delivery_state, again.resent) == ("sending", False)  # not a resend


def test_delivery_state_survives_fail_and_release(store: JobStore, clock: MutableClock) -> None:
    store.enqueue(new_podcast_job())
    claim = claim_one(store)
    store.mark_sending(claim, "<m@example.org>")
    queued = store.fail(claim, "disconnected", transient=True)
    assert (queued.status, queued.delivery_state) == ("queued", "sending")
    clock.advance(3600)
    again = claim_one(store)
    released = store.release(again)
    assert (released.status, released.delivery_state) == ("queued", "sending")
    last = claim_one(store)
    failed = store.fail(last, "gave up", transient=False)
    assert (failed.status, failed.delivery_state) == ("failed", "sending")


def test_delivery_writes_are_fenced(store: JobStore) -> None:
    store.enqueue(new_podcast_job())
    claim = claim_one(store)
    store.release(claim)
    with pytest.raises(LeaseLostError):
        store.mark_sending(claim, "<m@example.org>")
    with pytest.raises(LeaseLostError):
        store.clear_sending(claim)
    with pytest.raises(LeaseLostError):
        store.complete_sent(claim, JobResult(duration_s=1.0))


def test_mark_sending_refuses_a_sent_job(tmp_path: Path, clock: MutableClock) -> None:
    store = make_store(tmp_path, clock)
    store.enqueue(new_podcast_job())
    claim = claim_one(store)
    with store._db.connect() as conn:  # a hand-edited row
        conn.execute(
            "UPDATE jobs SET delivery_state = 'sent', message_id = 'm', sent_at = run_after"
        )
    with pytest.raises(LeaseLostError):
        store.mark_sending(claim, "<m@example.org>")
