"""Module-level targets run in spawned child processes (a module, not a test file)."""

import json
import threading
import time
from datetime import datetime
from multiprocessing.synchronize import Barrier, Event
from pathlib import Path

from earmark.db import Database
from earmark.fakes import fixed_clock
from earmark.jobs import JobStore


def claim_until_empty(
    db_path: str, now_iso: str, threads: int, out_path: str, start: Barrier
) -> None:
    store = JobStore(Database(Path(db_path)), clock=fixed_clock(datetime.fromisoformat(now_iso)))
    claimed: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        while (claim := store.claim_next()) is not None:
            with lock:
                claimed.append(claim.job.id)

    start.wait(timeout=60)
    workers = [threading.Thread(target=work) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    Path(out_path).write_text(json.dumps(claimed))


def open_concurrently(db_path: str, start: Barrier, out_path: str) -> None:
    db = Database(Path(db_path))
    start.wait(timeout=60)
    db.ensure_wal()
    applied = db.migrate()
    Path(out_path).write_text(json.dumps(applied))


def hold_write_lock(db_path: str, ready: Event) -> None:
    with Database(Path(db_path)).connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO jobs (id, kind, status, url, dedupe_key, run_after, shared_at, "
            "created_at, updated_at) VALUES ('f' || hex(randomblob(15)), 'ingest', 'queued', "
            "'u', 'k', 'x', 'x', 'x', 'x')"
        )
        ready.set()
        time.sleep(60)
