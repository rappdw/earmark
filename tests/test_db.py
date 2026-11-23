import json
import multiprocessing
import signal
import sqlite3
import threading
from pathlib import Path

import pytest

from earmark.db import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    Database,
    DatabaseSetupError,
    ForeignDatabaseError,
    LegacyDatabaseError,
    Migration,
    identity_error,
    open_database,
    retry_on_busy,
)
from earmark.jobs import JobStore
from tests import db_helpers
from tests.factories import MutableClock
from tests.legacy_db import V010_JOB_COUNT, V010_SCHEMA_VERSION, make_v010_database

URL = "https://podcasts.apple.com/us/podcast/x/id1?i=2"


def journal_mode(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


def test_open_sets_wal(tmp_path: Path) -> None:
    db = open_database(tmp_path / "earmark.db")
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert journal_mode(db.path) == "wal"


def test_open_creates_parent_dir(tmp_path: Path) -> None:
    db = open_database(tmp_path / "a" / "b" / "earmark.db")
    assert db.path.exists()
    assert db.schema_version() == SCHEMA_VERSION


@pytest.mark.parametrize("bad", [0, -1.0, float("inf"), float("nan")])
def test_busy_timeout_validated(tmp_path: Path, bad: float) -> None:
    with pytest.raises(ValueError, match="busy_timeout_s"):
        Database(tmp_path / "x.db", busy_timeout_s=bad)


def test_connect_settings(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db", busy_timeout_s=2.5)
    with db.connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2500
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.autocommit
        assert conn.row_factory is sqlite3.Row


def test_connect_closes(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    with db.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="boom"), db.connect() as conn2:
        raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn2.execute("SELECT 1")


def test_transaction_commits(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    with db.transaction() as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    with db.connect() as conn:
        assert conn.execute("SELECT a FROM t").fetchall()[0][0] == 1


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    with db.transaction() as conn:
        conn.execute("CREATE TABLE t (a INTEGER NOT NULL)")

    def insert_then_violate() -> None:
        with db.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("INSERT INTO t VALUES (NULL)")

    with pytest.raises(sqlite3.IntegrityError):
        insert_then_violate()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_reopen_runs_no_migration_and_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "earmark.db"
    store = JobStore(open_database(path))
    jobs = [store.enqueue(f"{URL}{i}").job for i in range(3)]
    db = Database(path)
    assert db.migrate() == 0
    assert db.schema_version() == SCHEMA_VERSION
    store2 = JobStore(open_database(path))
    assert [store2.get(j.id) for j in jobs] == jobs


def test_only_pending_migrations_run(tmp_path: Path) -> None:
    m1 = Migration("0001", ("CREATE TABLE a (x INTEGER)",))
    m2 = Migration("0002", ("CREATE TABLE b (y INTEGER)",))
    path = tmp_path / "x.db"
    db = open_database(path, migrations=[m1])
    with db.transaction() as conn:
        conn.execute("INSERT INTO a VALUES (7)")
    db2 = Database(path)
    assert db2.migrate([m1, m2]) == 1
    assert db2.schema_version() == 2
    with db2.connect() as conn:
        assert conn.execute("SELECT x FROM a").fetchone()[0] == 7
        conn.execute("SELECT * FROM b")


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    db = open_database(path, migrations=[])
    bad = Migration("0001", ("CREATE TABLE a (x INTEGER)", "CREATE TABLE ("))
    with pytest.raises(sqlite3.OperationalError):
        db.migrate([bad])
    assert db.schema_version() == 0
    with db.connect() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE name = 'a'").fetchall()
    assert tables == []


def test_newer_schema_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    db = open_database(path)
    with db.transaction() as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(DatabaseSetupError, match=rf"99.*\({SCHEMA_VERSION}\)"):
        db.migrate()


def test_concurrent_first_open_threads(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    n = 8
    barrier = threading.Barrier(n)
    applied: list[int] = []
    errors: list[sqlite3.Error] = []

    def work() -> None:
        try:
            db = Database(path)
            barrier.wait(timeout=30)
            db.ensure_wal()
            applied.append(db.migrate())
        except sqlite3.Error as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert sum(applied) == SCHEMA_VERSION


def test_concurrent_first_open_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "fresh.db"
    n = 4
    barrier = ctx.Barrier(n)
    outs = [tmp_path / f"out{i}.json" for i in range(n)]
    procs = [
        ctx.Process(target=db_helpers.open_concurrently, args=(str(path), barrier, str(o)))
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
    assert sum(json.loads(o.read_text()) for o in outs) == SCHEMA_VERSION


def _busy_error() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


def test_retry_on_busy_recovers() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _busy_error()
        return "ok"

    assert retry_on_busy(fn, timeout_s=5) == "ok"
    assert calls == 3


def test_retry_on_busy_gives_up() -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise _busy_error()

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        retry_on_busy(fn, timeout_s=0.05)
    assert calls > 1


@pytest.mark.parametrize("exc", [sqlite3.OperationalError("other"), ValueError("v")])
def test_retry_on_busy_other_errors_propagate(exc: Exception) -> None:
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(type(exc)):
        retry_on_busy(fn, timeout_s=5)
    assert calls == 1


def test_ensure_wal_waits_for_reader(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE t (a)")
    seed.commit()
    seed.close()
    assert journal_mode(path) == "delete"
    reader = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()
    timer = threading.Timer(0.1, lambda: reader.execute("COMMIT"))
    timer.start()
    try:
        Database(path, busy_timeout_s=5).ensure_wal()
    finally:
        timer.join()
        reader.close()
    assert journal_mode(path) == "wal"


def test_killed_writer_leaves_no_trace(tmp_path: Path) -> None:
    path = tmp_path / "earmark.db"
    db = open_database(path, busy_timeout_s=5)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=db_helpers.hold_write_lock, args=(str(path), ready))
    proc.start()
    assert ready.wait(timeout=60)
    proc.kill()
    proc.join(timeout=60)
    assert proc.exitcode == -signal.SIGKILL
    proc.close()
    store = JobStore(db, clock=MutableClock())
    assert store.claim_next() is None
    assert store.list_recent() == []


def pragmas(path: Path) -> dict[str, int | str]:
    conn = sqlite3.connect(path)
    try:
        return {
            "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        }
    finally:
        conn.close()


def job_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0])
    finally:
        conn.close()


def test_v010_database_refused(tmp_path: Path) -> None:
    path = make_v010_database(tmp_path / "earmark.db")
    with pytest.raises(LegacyDatabaseError) as info:
        open_database(path)
    assert str(path) in str(info.value)
    assert "Move it aside" in str(info.value)
    assert pragmas(path) == {
        "application_id": 0,
        "user_version": V010_SCHEMA_VERSION,
        "journal_mode": "wal",
    }
    assert job_count(path) == V010_JOB_COUNT


def test_v010_rollback_journal_not_switched_to_wal(tmp_path: Path) -> None:
    path = make_v010_database(tmp_path / "earmark.db", wal=False)
    assert journal_mode(path) == "delete"
    with pytest.raises(LegacyDatabaseError):
        open_database(path)
    assert journal_mode(path) == "delete"
    assert job_count(path) == V010_JOB_COUNT


def test_pre_rescope_dev_database_refused(tmp_path: Path) -> None:
    path = tmp_path / "earmark.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(LegacyDatabaseError):
        open_database(path)
    assert pragmas(path)["application_id"] == 0
    assert pragmas(path)["user_version"] == 1


def test_foreign_application_id_refused(tmp_path: Path) -> None:
    path = tmp_path / "earmark.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA application_id = 0x12345678")
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(ForeignDatabaseError) as info:
        open_database(path)
    assert "0x12345678" in str(info.value)
    assert str(path) in str(info.value)


def test_fresh_database_identity(tmp_path: Path) -> None:
    path = tmp_path / "earmark.db"
    db = open_database(path)
    assert db.identity() == (APPLICATION_ID, SCHEMA_VERSION)
    assert SCHEMA_VERSION == 1
    with db.connect() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == {"jobs"}
    assert Database(path).migrate() == 0
    assert open_database(path).identity() == (APPLICATION_ID, SCHEMA_VERSION)


def test_empty_migrations_still_stamp(tmp_path: Path) -> None:
    path = tmp_path / "earmark.db"
    db = open_database(path, migrations=())
    assert db.identity() == (APPLICATION_ID, 0)
    assert open_database(path).identity() == (APPLICATION_ID, SCHEMA_VERSION)


@pytest.mark.parametrize(
    ("application_id", "user_version", "expected"),
    [
        (0, 0, None),
        (0, 2, LegacyDatabaseError),
        (APPLICATION_ID, 0, None),
        (APPLICATION_ID, 7, None),
        (0x12345678, 0, ForeignDatabaseError),
        (0x12345678, 3, ForeignDatabaseError),
    ],
)
def test_identity_error_table(
    tmp_path: Path, application_id: int, user_version: int, expected: type[Exception] | None
) -> None:
    error = identity_error(tmp_path / "x.db", application_id, user_version)
    if expected is None:
        assert error is None
    else:
        assert type(error) is expected
        assert isinstance(error, DatabaseSetupError)
