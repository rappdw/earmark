# Step 03: Job store

## 1. Outcome

The package gains a durable job queue in SQLite:

- `earmark/db.py`: one place that opens `$EARMARK_DATA_DIR/earmark.db`. Every connection
  is short-lived, in `autocommit=True` mode, with a busy timeout, and is always closed.
  Write transactions are `BEGIN IMMEDIATE`. The database is switched to WAL once, and
  schema migrations are an ordered tuple applied under `PRAGMA user_version`, atomically
  and safely when several processes open a fresh database at once.
- `earmark/jobs.py`: `JobStore` with `enqueue`, `enqueue_reanalyze`, `claim_next`,
  `heartbeat`, `set_stage`, `set_episode_key`, `complete`, `fail`, `skip`, `release`,
  `add_warning`, `requeue_stale`, `get`, `list_recent`, plus `retry_delay` (the backoff)
  and `open_job_store(settings)`. Claiming is one `UPDATE … RETURNING` statement inside
  a write transaction, so each job is claimed exactly once across threads and processes.
  A claim carries a private lease token, so a worker that lost its lease cannot touch
  the job again. The clock is injected.

Nothing outside tests calls the store yet: step 04 enqueues from the API, step 05 runs
the worker loop. No new dependency, no new setting, no CLI change.

## 2. Findings

All checks run on 2026-11-19 with throwaway scripts in the scratchpad (since deleted),
using the locked dev environment for pytest and mypy.

### 2.1 SQLite versions: here and on CI

| Interpreter | How found | SQLite | Linkage |
|---|---|---|---|
| Sandbox: `/usr/bin/python3.13` (Debian, 3.13.5), which `.venv` uses | `readlink -f .venv/bin/python`; `sqlite3.sqlite_version` | 3.46.1 | dynamic, `/lib/aarch64-linux-gnu/libsqlite3.so.0` |
| CI: uv-managed CPython 3.13.18 (python-build-standalone), the interpreter `setup-uv` + `uv sync` gets on `ubuntu-24.04-arm`, whose system Python is 3.12 | `UV_PYTHON_INSTALL_DIR=<scratch> uv python install 3.13 --managed-python`, then the same probes | 3.53.1 | static |

Both are far above the features this step needs: `RETURNING` (3.35), `STRICT` tables
(3.37), built-in JSON functions with `json_insert(x, '$[#]', v)` (3.38 default build),
and partial indexes. A deployment container on `python:3.13-slim` (Debian) gets 3.46.x,
the same as the sandbox. Plan for "SQLite >= 3.46"; do not assert a version in code.

**Dead end: `UPDATE … ORDER BY … LIMIT`.** It works on Debian's build
(`ENABLE_UPDATE_DELETE_LIMIT` is in `PRAGMA compile_options`) and is a *syntax error*
on the uv-managed build CI uses. Code written against the sandbox would pass locally and
fail in CI. Select the row with a subquery: `UPDATE jobs … WHERE seq = (SELECT seq …
ORDER BY … LIMIT 1) RETURNING …`. Both builds accept that.

### 2.2 Python's sqlite3 transaction control (3.12+ `autocommit`)

Verified on both interpreters:

- `sqlite3.connect(path, autocommit=True, timeout=7.5)`: `PRAGMA busy_timeout` reads
  `7500`, so `timeout=` *is* the busy timeout. With `autocommit=True` the module never
  issues an implicit `BEGIN`; `in_transaction` is `False` after DDL, `True` after an
  explicit `BEGIN IMMEDIATE`, `False` after `COMMIT`.
- The default (`autocommit=LEGACY_TRANSACTION_CONTROL`, `isolation_level=""`) opens a
  **deferred** transaction implicitly before DML. Do not use it.
- **Dead end: deferred transactions that read and then write.** Connection A runs
  `BEGIN`, a `SELECT`, then B commits a write, then A writes: A fails *immediately* with
  `database is locked` (`sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"`), ignoring a 5 s
  busy timeout. Every read-then-write transaction must start with `BEGIN IMMEDIATE`,
  which takes the write lock up front and does honour the busy timeout (a second
  `BEGIN IMMEDIATE` waited 0.26–0.35 s for a 0.3 s holder; with `timeout=0.1` it raised
  `OperationalError("database is locked")`, `sqlite_errorname == "SQLITE_BUSY"`).
- Readers are not blocked by a writer in WAL mode (a `SELECT` during another
  connection's open `BEGIN IMMEDIATE` returned at once).
- **Dead end: `with conn:` for transactions.** With `autocommit=True` the connection
  context manager does nothing: a row inserted inside `with conn:` survived an exception
  raised in the block. And it never closes the connection.
- **Dead end: `with sqlite3.connect(...) as conn:` does not close.** The connection is
  still usable after the block. Python 3.13 emits `ResourceWarning: unclosed database`
  when an unclosed connection is collected, which this suite's
  `filterwarnings = ["error"]` turns into a failure. Close every connection explicitly
  (`try/finally` or `contextlib.closing`).
- `COMMIT` before the `RETURNING` rows are fetched fails with `OperationalError: cannot
  commit transaction - SQL statements in progress`. Call `fetchone()`/`fetchall()`
  first.
- `executescript` inside an explicit `BEGIN` under `autocommit=True` does not commit;
  still, use one `execute` per statement in migrations so errors point at a statement.
- `PRAGMA user_version = N` is transactional: rolled back with the transaction.
- Passing a `datetime` as a parameter emits `DeprecationWarning: The default datetime
  adapter is deprecated as of Python 3.12`, which fails under `filterwarnings=error`.
  Convert datetimes to strings yourself.
- typeshed (mypy 2.4.1): `connect(..., autocommit=True)` type-checks; `fetchone()`
  returns `Any` and `Connection.autocommit` is `int`. Rows must be mapped to typed
  values explicitly, or strict mypy reports `no-any-return`.

### 2.3 WAL across threads and processes

- `PRAGMA journal_mode=WAL` returns `'wal'` on tmpfs and persists in the file.
- **Dead end: switching to WAL while another process opens the same fresh database.**
  Eight threads (or six spawned processes) each running `PRAGMA journal_mode=WAL` on a
  new file at once: some fail immediately with `database is locked`, without waiting
  for the busy timeout. (When a single other connection merely holds a read lock, the
  switch does wait the full busy timeout, 2.07 s for `timeout=2`, and then fails; the
  immediate failures come from SQLite's deadlock avoidance, which skips the busy
  handler when two connections both hold shared locks and want to escalate.) Fix,
  verified over 20 thread trials and 5 process trials on both interpreters: read
  `PRAGMA journal_mode` first and only set it when it is not `wal`, retrying on
  `SQLITE_BUSY` with a short sleep until the busy timeout elapses. After the switch, migrations in `BEGIN IMMEDIATE` serialise
  correctly: across all trials, exactly one opener applied the migrations and the rest
  applied none.
- `sqlite3.OperationalError.sqlite_errorcode` may be an extended code; compare
  `code & 0xFF == sqlite3.SQLITE_BUSY`.
- Opening, writing in `BEGIN IMMEDIATE`, and closing a connection per operation cost
  0.06–0.08 ms here (tmpfs, so no fsync cost). A connection per operation is cheap
  enough, makes the store safe to share across threads (`check_same_thread` never
  trips), and leaves nothing to leak. When the last connection closes, SQLite
  checkpoints and removes `-wal`/`-shm`; that is expected.
- A process SIGKILLed while holding `BEGIN IMMEDIATE` with an uncommitted insert: the
  next `BEGIN IMMEDIATE` from another process got the lock at once and the row was
  absent. Crash safety needs nothing beyond transactions.
- WAL needs shared memory between processes on one host. The data directory must be a
  local filesystem (a Docker named volume or bind mount on the same host is fine; NFS
  and SMB are not). Step 16 documents this.

### 2.4 Testing exactly-once claims without flakiness

Harness (stdlib, both interpreters): enqueue N jobs, then K claimers start together on a
barrier and each loops "claim until none left", recording what it claimed. Assert the
multiset of claims equals the enqueued ids exactly. It is not flaky because the
assertion holds for every interleaving, including none; contention only makes it a
stronger test.

- Correct claim (`BEGIN IMMEDIATE` + `UPDATE … WHERE seq = (SELECT …) RETURNING`):
  400 jobs, 8 threads, 400 claims, 400 unique, ~0.05 s; 4 spawned processes × 4
  threads, 400/400, ~0.1 s. Same with the `UPDATE` as a single autocommit statement.
- Negative control (`SELECT` the oldest queued id, then `UPDATE … WHERE id = ?`
  without a status guard): 5 of 5 trials double-claimed (577–640 claims for 400 jobs).
  The harness has teeth; the plan's test would fail against a racy claim.
- Processes: use `multiprocessing.get_context("spawn")`. `os.fork()` in a process with
  a running thread emits `DeprecationWarning: This process … is multi-threaded, use of
  fork() may lead to deadlocks`, which fails under `filterwarnings=error`, and SQLite
  forbids carrying a connection across `fork`. Spawned children received the parent's
  `sys.path`, so a module-level target in an importable test-helper module worked from
  pytest after `monkeypatch.chdir(tmp_path)` (as this repo's autouse fixture does), and
  ran fine under `--disable-socket --allow-unix-socket` (spawn uses pipes). Synchronise
  with `ctx.Barrier(n)`; return results through files in `tmp_path`; `join(timeout)`,
  check `exitcode == 0`, then `close()`.

### 2.5 Fit with later steps

- **Step 04 (API).** `POST /ingest` calls `enqueue(url, prompt=…, shared_at=…)` and
  returns `job.id` and `job.status`. The dedupe compares the exact `url` string; step 04
  should pass the URL it validated (a double-tapped share sends the same string, so
  exact matching covers the brief). `GET /jobs/{id}` uses `get` (which returns `None`
  for a malformed id without querying), `GET /jobs` uses `list_recent`. FastAPI runs
  sync handlers in a thread pool; the store is thread-safe.
- **Step 05 (worker).** `claim_next` returns a `Claim` (job and lease token).
  `heartbeat`, `set_stage`, `set_episode_key`, `complete`, `fail`, `skip`, `release`
  take the claim and raise `LeaseLostError` if the job was requeued or finished
  meanwhile, so a stale worker cannot overwrite a newer run. A heartbeat thread can
  share the store. On SIGTERM the worker calls `release`, which undoes the attempt.
  `requeue_stale` returns the jobs it changed so the worker can notify those that
  became `failed`. `add_warning` takes a job id and works on terminal jobs, because
  notifier failures are recorded after the job is `done`.
- **Step 17 (reanalyze).** `enqueue_reanalyze(episode_key, prompt=…, shared_at=…)`
  creates a `reanalyze` job with no URL, deduped on `(episode_key, prompt)`. Ingest and
  reanalyze jobs never dedupe against each other.
- **Step 13** adds tables by appending to `db.MIGRATIONS`.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/db.py` | New. `Database`, `Migration`, `MIGRATIONS`, `SCHEMA_VERSION`, `DatabaseSetupError`, `retry_on_busy`, `database_path`, `open_database`. |
| `src/earmark/jobs.py` | New. `JobStore`, `Claim`, `Enqueued`, `LeaseLostError`, `JobNotFoundError`, `retry_delay`, `open_job_store`, backoff constants. |
| `tests/test_db.py` | New. Connections, WAL, migrations, concurrent first open, crash during a transaction. |
| `tests/test_jobs.py` | New. Every `JobStore` behaviour, including the thread and process claim tests. |
| `tests/db_helpers.py` | New. Module-level functions run in spawned child processes (a module, not a test file). |
| `tests/factories.py` | Add `MutableClock`. |
| `docs/ARCHITECTURE.md` | Jobs section rewritten to the contract below; data-layout note on the local filesystem (section 8). |

`src/earmark/__init__.py` is unchanged: importing `earmark` still imports neither module.

## 4. Interfaces

### 4.1 `earmark.db`

Imports only the standard library.

```python
DB_FILENAME: Final = "earmark.db"
DEFAULT_BUSY_TIMEOUT_S: Final = 10.0


class DatabaseSetupError(Exception):
    """The database cannot be used: newer schema than this code, or WAL unavailable."""


@dataclass(frozen=True, slots=True)
class Migration:
    name: str
    statements: tuple[str, ...]


MIGRATIONS: Final[tuple[Migration, ...]] = ...  # one entry in this step: 0001_jobs
SCHEMA_VERSION: Final = len(MIGRATIONS)


class Database:
    def __init__(self, path: Path, *, busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S) -> None: ...

    @property
    def path(self) -> Path: ...

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]: ...

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]: ...

    def ensure_wal(self) -> None: ...

    def schema_version(self) -> int: ...

    def migrate(self, migrations: Sequence[Migration] = MIGRATIONS) -> int: ...


def retry_on_busy[T](fn: Callable[[], T], *, timeout_s: float) -> T: ...


def database_path(data_dir: Path) -> Path: ...


def open_database(
    path: Path,
    *,
    busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> Database: ...
```

- `Database.__init__` does no I/O. `busy_timeout_s` must be finite and `> 0`
  (`ValueError`).
- `connect()`: `sqlite3.connect(self.path, autocommit=True, timeout=busy_timeout_s)`,
  `row_factory = sqlite3.Row`, `PRAGMA foreign_keys = ON`; yields; closes in `finally`,
  even if the body raised. Synchronous mode stays at the default (`FULL` in WAL on both
  builds, per `compile_options`); do not set `NORMAL`.
- `transaction()`: inside `connect()`, `BEGIN IMMEDIATE`, yield, `COMMIT`. If the body
  raises, `ROLLBACK` (only when `conn.in_transaction`) and re-raise the original
  exception. Callers fetch `RETURNING` rows inside the block.
- `retry_on_busy(fn, timeout_s=...)`: call `fn`; on `sqlite3.OperationalError` whose
  `sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY`, sleep 10 ms and call again, until
  `timeout_s` has elapsed since the first call (`time.monotonic()`), then re-raise the
  last error. Any other exception propagates at once. It exists for `ensure_wal` and is
  public so it can be unit-tested with a fake callable.
- `ensure_wal()`: on one connection, `retry_on_busy` around: read `PRAGMA
  journal_mode`; if it is not `wal`, run `PRAGMA journal_mode=WAL` and return the mode
  it reports. Timeout is `busy_timeout_s`. If the final mode is not `wal`, raise
  `DatabaseSetupError` naming the path and the mode (the filesystem probably lacks
  shared-memory support).
- `schema_version()`: `PRAGMA user_version`.
- `migrate(migrations)`: in one `transaction()`: read `user_version` (inside the lock,
  so a concurrent opener that already migrated is seen); if it exceeds
  `len(migrations)`, raise `DatabaseSetupError("database schema version N is newer
  than this earmark supports (M)")`; run the statements of each pending migration in
  order with one `execute` each; set `PRAGMA user_version = len(migrations)` (an int
  from `len`, formatted into the pragma, which takes no parameters); return the number
  applied. Any failure rolls back every pending migration and the version.
- `database_path(data_dir)`: `data_dir / DB_FILENAME`.
- `open_database`: create the parent directory (`parents=True, exist_ok=True`), build a
  `Database`, `ensure_wal()`, `migrate(migrations)`, return it.

Migration rules, stated in a comment above `MIGRATIONS`: append only, never edit or
reorder an entry that has shipped; plain `CREATE TABLE`/`CREATE INDEX` without `IF NOT
EXISTS`, so an accidental rerun fails loudly instead of silently passing.

Migration 1, `Migration("0001_jobs", (...))`, has these three statements:

```sql
CREATE TABLE jobs (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('ingest', 'reanalyze')),
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'skipped')),
    stage TEXT,
    url TEXT,
    prompt TEXT,
    episode_key TEXT,
    dedupe_key TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    run_after TEXT NOT NULL,
    error TEXT,
    warnings TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings)),
    result TEXT,
    shared_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT,
    lease_token TEXT
) STRICT;

CREATE UNIQUE INDEX jobs_active_dedupe ON jobs (dedupe_key)
    WHERE status IN ('queued', 'running');

CREATE INDEX jobs_by_status ON jobs (status, created_at, seq);
```

(Written as three separate strings in the tuple, without the trailing semicolons. A
`jobs_by_created` index for `list_recent` is not needed at this volume.)

`seq` gives insertion order for ties. `stage` is not constrained in SQL so a later stage
name needs no migration; the `Job` model validates it on read.

### 4.2 `earmark.jobs`

Imports the standard library, `earmark.models`, `earmark.db`, and `earmark.config`
(for `open_job_store`'s type only).

```python
RETRY_BASE_S: Final = 60.0
RETRY_FACTOR: Final = 4
RETRY_MAX_S: Final = 6 * 3600.0
LIST_LIMIT_MAX: Final = 500


class LeaseLostError(RuntimeError):
    """The claim no longer holds the job (requeued, released, or finished)."""


class JobNotFoundError(LookupError): ...


@dataclass(frozen=True, slots=True)
class Claim:
    job: Job  # the job as claimed: status "running", attempts already incremented
    token: str  # uuid4().hex, private to this claim; never shown in the API


@dataclass(frozen=True, slots=True)
class Enqueued:
    job: Job
    created: bool  # False when an identical active job was returned instead


def retry_delay(attempts: int, retry_after_s: float | None = None) -> timedelta: ...


class JobStore:
    def __init__(
        self,
        db: Database,
        *,
        clock: Clock = utc_now,
        max_attempts: int = 4,
        lease: timedelta = timedelta(seconds=900),
    ) -> None: ...

    def enqueue(
        self, url: str, *, prompt: str | None = None, shared_at: datetime | None = None
    ) -> Enqueued: ...

    def enqueue_reanalyze(
        self,
        episode_key: str,
        *,
        prompt: str | None = None,
        shared_at: datetime | None = None,
    ) -> Enqueued: ...

    def claim_next(self) -> Claim | None: ...

    def heartbeat(self, claim: Claim) -> None: ...

    def set_stage(self, claim: Claim, stage: Stage) -> None: ...

    def set_episode_key(self, claim: Claim, episode_key: str) -> None: ...

    def complete(self, claim: Claim, result: JobResult | None = None) -> Job: ...

    def fail(
        self,
        claim: Claim,
        error: str,
        *,
        transient: bool,
        retry_after_s: float | None = None,
    ) -> Job: ...

    def skip(self, claim: Claim, reason: str) -> Job: ...

    def release(self, claim: Claim) -> Job: ...

    def add_warning(self, job_id: str, message: str) -> None: ...

    def requeue_stale(self) -> list[Job]: ...

    def get(self, job_id: str) -> Job | None: ...

    def list_recent(self, limit: int = 20, *, status: JobStatus | None = None) -> list[Job]: ...


def open_job_store(settings: Settings, *, clock: Clock = utc_now) -> JobStore: ...
```

`Clock`, `utc_now`, `Job`, `JobResult`, `JobStatus`, `Stage` come from `earmark.models`.
`max_attempts >= 1` and `lease > 0` or `ValueError`.

**Storage of fields.**

- Datetimes: `_to_db(dt) = dt.astimezone(UTC).isoformat(timespec="microseconds")`, for
  example `2026-01-01T12:00:00.000000+00:00`. Fixed width and always UTC, so string
  comparison in SQL is time order. `_from_db(s) = datetime.fromisoformat(s)`. A naive
  datetime (from `shared_at` or from the clock) raises `ValueError` before anything is
  written; check `dt.tzinfo is None or dt.utcoffset() is None` explicitly, because
  `astimezone` on a naive value silently assumes local time.
- `warnings`: JSON array text. `result`: `JobResult.model_dump_json()` or `NULL`.
- `dedupe_key`: `sha256` hex of `json.dumps([kind, url_or_episode_key, prompt],
  ensure_ascii=False, separators=(",", ":"))`. `prompt=None` and `""` give different
  keys (step 04 normalises blank prompts to `None`).
- Rows become models through one private `_row_to_job(row) -> Job`, which builds
  `Job(...)` with every field (never `model_copy`), so a corrupt row fails validation
  loudly. `seq`, `dedupe_key`, and `lease_token` are not part of `Job`.

**`claim_next` SQL**, inside `db.transaction()`, with `:now` from the clock and a fresh
`:token`:

```sql
UPDATE jobs
SET status = 'running', attempts = attempts + 1, lease_token = :token,
    heartbeat_at = :now, updated_at = :now
WHERE seq = (
    SELECT seq FROM jobs
    WHERE status = 'queued' AND run_after <= :now
    ORDER BY created_at, seq
    LIMIT 1
)
RETURNING *
```

`fetchone()` before the transaction commits. `None` means nothing is claimable.

**Fenced updates.** Every method taking a `Claim` updates with `WHERE id = :id AND
status = 'running' AND lease_token = :token` and checks `cursor.rowcount == 1` (or that
`RETURNING` produced a row); otherwise it raises `LeaseLostError(job_id)` and changes
nothing. `fail` needs the row's current `attempts` to choose between retry and final
failure: it reads the fenced row and updates it inside one `transaction()` (never a
deferred transaction, section 2.2), using `max_attempts` from the store, not from the
`Claim`'s possibly stale copy.

**`add_warning`**, one statement:

```sql
UPDATE jobs
SET warnings = json_insert(warnings, '$[#]', :message), updated_at = :now
WHERE id = :id AND NOT EXISTS (SELECT 1 FROM json_each(jobs.warnings) WHERE value = :message)
```

If no row changed, a `SELECT` tells "already present" (return) from "no such job"
(`JobNotFoundError`). Run both in one `transaction()`.

**`open_job_store(settings, clock=...)`**: `open_database(database_path(settings.data_dir))`,
then `JobStore(db, clock=clock, max_attempts=settings.job_max_attempts,
lease=timedelta(seconds=settings.job_lease_seconds))`.

## 5. Behaviour and edge cases

1. **Lifecycle.** `enqueue` → `queued` (attempts 0, `run_after = created_at = now`).
   `claim_next` → `running`, attempts + 1, fresh token, `heartbeat_at = now`. From
   `running`: `complete` → `done`; `skip` → `skipped`; `fail(transient=False)` →
   `failed`; `fail(transient=True)` → `queued` with backoff, or `failed` once
   `attempts >= max_attempts`; `release` → `queued`, attempt undone; `requeue_stale` →
   `queued`, or `failed` at the cap. Terminal rows never change status again. Every
   transition out of `running` sets `lease_token = NULL`. Every write sets
   `updated_at = now` except `heartbeat`, which sets only `heartbeat_at`.
2. **Attempts count claims.** Incremented by `claim_next`, decremented only by
   `release`. `requeue_stale` does not decrement: a job that crashes its worker every
   time (out of memory in transcription, say) must eventually fail instead of looping.
3. **Backoff.** `retry_delay(attempts, retry_after_s)` returns
   `min(RETRY_BASE_S * RETRY_FACTOR ** (attempts - 1), RETRY_MAX_S)` seconds, raised to
   `min(retry_after_s, RETRY_MAX_S)` when that is larger. With the defaults: 60 s,
   240 s, 960 s, then the fourth failure is final. Cap the exponent (for example
   `min(attempts - 1, 16)`) so huge attempt counts do not build huge ints. `attempts <
   1`, or a negative or non-finite `retry_after_s`, raises `ValueError`. No jitter:
   tests need exact values, and there is one worker.
   `fail(transient=True)` sets `run_after = now + retry_delay(job.attempts,
   retry_after_s)`, `status = 'queued'`, `error = error`.
4. **Final failure keeps the last message.** `failed` after exhausting attempts stores
   the given `error` unchanged; the runner owns the wording. `complete` clears `error`;
   a queued retry keeps it, so `GET /jobs/{id}` shows why it is retrying.
5. **Ordering.** Oldest `created_at` first, `seq` breaking ties, among `queued` rows
   with `run_after <= now` (equal is claimable). A retried job keeps its place by age
   once its `run_after` passes. `list_recent` is newest first (`created_at DESC, seq
   DESC`), optionally filtered by status, `1 <= limit <= LIST_LIMIT_MAX` or
   `ValueError`.
6. **Dedupe.** Inside one `transaction()`: look up a row with the same `dedupe_key`
   and `status IN ('queued', 'running')`; if found, return `Enqueued(job, False)`
   without changing it (its `shared_at` stays the first share's). Otherwise build the
   `Job` model (validating url and key), insert it with a plain `INSERT` (not `ON
   CONFLICT DO NOTHING`, which would also swallow an `id` collision), and return
   `Enqueued(job, True)`. The partial unique index enforces "one active job per key"
   in the database itself. A queued retry with a future `run_after` still counts as
   active. After the job is `done`, `failed`, or `skipped`, the same request creates a
   new job.
7. **Leases.** A `running` job is stale when `heartbeat_at < now - lease` (strictly
   older; exactly `lease` old is not stale). `requeue_stale`, in one `transaction()`,
   selects stale rows; each one with `attempts >= max_attempts` becomes `failed` with
   `error = "worker stopped responding (no heartbeat for {lease_s} s)"`, the rest
   become `queued` with `run_after = now` and the same `error` text. Returns the
   updated jobs (both kinds), oldest first. `heartbeat` and `set_stage` refresh
   `heartbeat_at`. Step 05 decides the heartbeat interval.
8. **Release (SIGTERM).** `release` sets `queued`, `attempts - 1`, `run_after = now`,
   clears the token, and leaves `error`, `stage`, and `warnings` alone. Because the
   token is random per claim, a released and re-claimed job is not reachable through
   the old `Claim` even though `attempts` returns to the same number.
9. **Fenced methods raise `LeaseLostError`** after the job was released, requeued,
   reclaimed, or finished. `add_warning`, `get`, and `list_recent` are not fenced.
10. **Validation.** `error`, `reason`, and `message` must be non-empty after `strip()`
    (`ValueError`); they are stored as given, not truncated. `set_episode_key` and
    `enqueue_reanalyze` validate the key with `models.is_episode_key` (`ValueError`).
    `enqueue` with a blank `url` raises `ValueError` (the `Job` model only requires the
    url to be present, so the store checks this itself). The store does not parse or
    normalise URLs; that is step 04's job. `get` returns `None`
    without a query when the id does not match `^[0-9a-f]{32}$`.
11. **Timezones.** Everything is stored in UTC, so a `shared_at` given with `-08:00`
    comes back as the same instant with `tzinfo=UTC`; models compare equal.
12. **Connections.** One short-lived connection per method, always closed. A
    `JobStore` may be shared by threads; separate processes each build their own.
    `sqlite3.OperationalError` (for example "database is locked" after the busy
    timeout) propagates unchanged; the store does not translate it.
13. **No SQL by string formatting** except the integer `user_version` pragma. All
    values are bound parameters.
14. **`stage`** is set only by `set_stage` and left as is by `claim_next`, `release`,
    and retries, so it shows the last stage reached.

## 6. Tests

Shared helpers:

- `tests/factories.py` gains `MutableClock`: `__init__(self, start: datetime = T0)`, a
  public writable attribute `now: datetime`, `__call__(self) -> datetime` returning
  `now`, and `advance(self, seconds: float) -> None`. Plain class, typed. (No method
  named `set`: it would shadow a builtin.)
- `tests/test_jobs.py` builds stores with a local helper `make_store(tmp_path, clock,
  *, max_attempts=4, lease_s=900) -> JobStore` over `open_database(tmp_path /
  "earmark.db")`.
- `tests/db_helpers.py`: module-level, picklable targets for spawned children, taking
  only `str`/`int`/barrier arguments:
  - `claim_until_empty(db_path: str, now_iso: str, threads: int, out_path: str,
    start: Barrier) -> None`: builds a `JobStore(Database(Path(db_path)),
    clock=fixed_clock(datetime.fromisoformat(now_iso)))` (`fixed_clock` from
    `earmark.fakes`; `Barrier` is `multiprocessing.synchronize.Barrier`), waits on
    `start`, runs `threads` threads that loop `claim_next()`
    until `None`, and writes the claimed ids to `out_path` as a JSON list.
  - `open_concurrently(db_path: str, start: Barrier, out_path: str) -> None`: builds a
    `Database`, waits on `start`, calls `ensure_wal()` then `migrate()` (the same steps
    as `open_database`, but keeping `migrate`'s return value), and writes that count to
    `out_path`.
  - `hold_write_lock(db_path: str, ready: Event) -> None`: `BEGIN IMMEDIATE`, inserts a
    job row via raw SQL, sets `ready`, sleeps 60 s (the parent kills it).
- Process tests use `multiprocessing.get_context("spawn")`, `join(timeout=60)`, assert
  `exitcode == 0` (or `-SIGKILL` for the killed child), then `close()`. No fixtures or
  network are needed; everything is generated in `tmp_path`.

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | With several threads and several processes claiming at once, each job is claimed exactly once | `test_jobs.py::test_claim_exactly_once_threads`: 200 jobs, one shared `JobStore`, 8 threads released by a `threading.Barrier`, each claiming until `None`; the concatenated claims equal the enqueued ids as a multiset (`sorted(claimed) == sorted(ids)`), and afterwards every row is `running` with `attempts == 1`. `test_jobs.py::test_claim_exactly_once_processes`: 300 jobs, 4 spawned processes × 4 threads via `db_helpers.claim_until_empty`, same assertions. `test_jobs.py::test_claim_returns_none_when_empty` |
| A2 | A transient failure requeues with increasing `run_after` and becomes `failed` at `JOB_MAX_ATTEMPTS`; a permanent failure is `failed` immediately | `test_jobs.py::test_transient_failure_backs_off_then_fails`: `max_attempts=4`; three claim / `fail(transient=True)` rounds, each advancing the clock to `run_after`; status `queued`, delays exactly 60, 240, 960 s (strictly increasing), job not claimable before `run_after`; the fourth failure gives `failed`, `attempts == 4`, last error kept, not claimable. `test_jobs.py::test_permanent_failure_fails_immediately` (`attempts == 1`, `failed`). `test_jobs.py::test_retry_after_raises_delay` (`retry_after_s=600` on attempt 1 gives 600 s; `retry_after_s=1` keeps 60 s). `test_jobs.py::test_retry_delay_values` (1..5 and a huge count, the 6 h cap, `ValueError` for 0, -1 s, and `inf`). `test_jobs.py::test_max_attempts_one` (first transient failure is final) |
| A3 | A stale `running` job is requeued; one with a fresh heartbeat is not | `test_jobs.py::test_requeue_stale_boundary`: lease 900 s; at exactly +900 s nothing changes, at +900.000001 s the job is `queued`, claimable, `error` set, and returned. `test_jobs.py::test_fresh_heartbeat_not_requeued` (heartbeat at +800, check at +1000: still `running`). `test_jobs.py::test_set_stage_refreshes_heartbeat`. `test_jobs.py::test_requeue_stale_fails_at_max_attempts` (returned job is `failed`). `test_jobs.py::test_old_claim_cannot_act_after_requeue` (each fenced method on the old claim raises `LeaseLostError`; the job is unchanged) |
| A4 | Same `(url, prompt)` while `queued` or `running` returns the same id; after terminal, a new job | `test_jobs.py::test_dedupe_while_queued` (`created` is `True` then `False`, same id, one row). `test_jobs.py::test_dedupe_while_running`. `test_jobs.py::test_dedupe_while_waiting_for_retry`. `test_jobs.py::test_new_job_after_terminal`, parametrized over complete, skip, and permanent fail. `test_jobs.py::test_dedupe_distinguishes_prompt` (`None`, `""`, `"a"`, `"b"` are four jobs; a different url is another). `test_jobs.py::test_dedupe_concurrent_threads` (8 threads on a barrier enqueue the same request: one job, all returned ids equal, exactly one `created`). `test_jobs.py::test_reanalyze_dedupe` (same key and prompt dedupe; an ingest with any url never matches a reanalyze) |
| A5 | Reopening an existing database runs no migration twice and loses no rows | `test_db.py::test_reopen_runs_no_migration_and_keeps_rows` (open, enqueue 3 jobs, open again with a new `Database`: `migrate` returns 0, `user_version == SCHEMA_VERSION`, all 3 jobs equal by `get`). `test_db.py::test_only_pending_migrations_run` (custom tuple `[m1]` then `[m1, m2]`: second open applies 1, `m1`'s plain `CREATE TABLE` would raise if rerun, rows in `m1`'s table survive). `test_db.py::test_failed_migration_rolls_back` (second statement invalid: `user_version` unchanged, first statement's table absent). `test_db.py::test_newer_schema_refused` (`user_version` set past `len(migrations)`: `DatabaseSetupError` naming both numbers) |
| A6 | Jobs come out oldest first; a future `run_after` is not claimed | `test_jobs.py::test_claim_oldest_first` (3 jobs at increasing clock times, claimed in order). `test_jobs.py::test_claim_ties_by_insertion_order` (same instant). `test_jobs.py::test_future_run_after_not_claimed` (a job retrying with a future `run_after` is skipped in favour of a newer one; claimable at exactly `run_after`). `test_jobs.py::test_retried_job_keeps_age_order` |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | The database is in WAL mode and connections are configured and closed | `test_db.py::test_open_sets_wal` (`PRAGMA journal_mode` is `wal` from a fresh connection). `test_db.py::test_connect_settings` (`busy_timeout` equals `busy_timeout_s * 1000`, `foreign_keys == 1`, `autocommit` is true). `test_db.py::test_connect_closes` (after the block, and after an exception in it, `conn.execute` raises `sqlite3.ProgrammingError`). `test_db.py::test_transaction_rolls_back_on_error` and `test_transaction_commits`. `test_db.py::test_open_creates_parent_dir` |
| B2 | Concurrent first opens of a fresh database all succeed and migrate once | `test_db.py::test_concurrent_first_open_threads` (8 threads, barrier, fresh path: no exception, applied counts sum to `SCHEMA_VERSION`). `test_db.py::test_concurrent_first_open_processes` (4 spawned processes via `db_helpers.open_concurrently`). `test_db.py::test_retry_on_busy` (a fake callable that raises `OperationalError` with `sqlite_errorcode = sqlite3.SQLITE_BUSY` twice, then returns: the value comes back after 3 calls; one that always raises busy: re-raised after `timeout_s=0.05`; one raising a non-busy `OperationalError` or `ValueError`: raised after 1 call. Set the attribute on a constructed exception; it is writable). `test_db.py::test_ensure_wal_waits_for_reader` (fresh file in rollback mode, a second connection holds `BEGIN` + `SELECT`, a `threading.Timer` commits it after 0.1 s; `ensure_wal` with `busy_timeout_s=5` returns and the mode is `wal`). The concurrent tests failed without the retry in section 2.3's experiments, but not on every run, so `test_retry_on_busy` is what pins the behaviour |
| B3 | A crash during a write leaves no partial state and no stuck lock | `test_db.py::test_killed_writer_leaves_no_trace`: spawn `db_helpers.hold_write_lock`, wait for `ready`, `kill()`; then `JobStore.claim_next()` on the parent returns `None` promptly (no lock) and the half-inserted row is absent |
| B4 | A claim is fenced by its token | `test_jobs.py::test_release_undoes_attempt` (`queued`, `attempts == 0`, claimable at once, `error`/`stage`/`warnings` kept). `test_jobs.py::test_old_claim_after_release_and_reclaim` (old claim's `complete` raises `LeaseLostError` though `attempts` matches; the new claim completes). `test_jobs.py::test_fenced_after_terminal`, parametrized over every fenced method |
| B5 | Results, warnings, stage, and episode key are stored and read back | `test_jobs.py::test_complete_stores_result` (`JobResult` equal, `error` cleared, `done`). `test_jobs.py::test_add_warning_appends_and_dedupes` (non-ASCII and quote characters; a repeat is ignored; order kept). `test_jobs.py::test_add_warning_on_terminal_job`. `test_jobs.py::test_add_warning_unknown_job` (`JobNotFoundError`). `test_jobs.py::test_set_stage_and_episode_key` (invalid key `ValueError`). `test_jobs.py::test_skip_records_reason` |
| B6 | Timestamps are UTC, offsets preserved as instants, naive input refused | `test_jobs.py::test_shared_at_offset_round_trips` (`-08:00` in, equal instant out, `tzinfo` is UTC). `test_jobs.py::test_shared_at_defaults_to_now`. `test_jobs.py::test_naive_datetimes_refused` (naive `shared_at`, naive clock: `ValueError`, no row written). `test_jobs.py::test_stored_timestamps_sort_as_text` (raw column values are fixed-width UTC strings) |
| B7 | Lookups and listing | `test_jobs.py::test_get_unknown_and_malformed` (`None` for an unknown 32-hex id, for `"../x"`, and for `""`). `test_jobs.py::test_list_recent_order_limit_status`. `test_jobs.py::test_list_recent_limit_bounds` (0 and 501 raise) |
| B8 | Reanalyze jobs | `test_jobs.py::test_enqueue_reanalyze` (`kind == "reanalyze"`, `url is None`, key kept; invalid key `ValueError`; claimable like any job) |
| B9 | `open_job_store` uses settings | `test_jobs.py::test_open_job_store_from_settings` (`Settings(_env_file=None, data_dir=tmp_path / "d", job_max_attempts=2, job_lease_seconds=60)`: database at `tmp_path / "d" / "earmark.db"`, second transient failure is final, stale after 60 s) |
| B10 | Error texts validated | `test_jobs.py::test_blank_messages_refused` (`fail`, `skip`, `add_warning` with `""` and `"  "`) |

Each test in A1 would fail against a racy claim (section 2.4 negative control). Process
tests take about a second in total; they stay in the default suite, unmarked.

## 7. Verification

`SCRATCH` is the verifier's scratchpad directory.

```bash
cd /home/sandy/dev/earmark
uv sync
make check
uv run ruff format --check docs/
uv run pytest -q tests/test_db.py tests/test_jobs.py

# The concurrency tests, five times in a row (they must never flake)
for i in 1 2 3 4 5; do
  uv run pytest -q tests/test_jobs.py tests/test_db.py -k "exactly_once or concurrent" || break
done

# No UPDATE ... LIMIT (not supported on the uv-managed SQLite CI uses)
grep -n "LIMIT" src/earmark/jobs.py   # every hit belongs to a SELECT

# Importing earmark still loads neither module
uv run python -c 'import sys, earmark; print("earmark.db" in sys.modules, "earmark.jobs" in sys.modules)'
# expect: False False
```

Run the claim and migration tests once under the interpreter CI uses (python-build-
standalone, SQLite 3.53.1), in a throwaway environment, then delete it:

```bash
export UV_PYTHON_INSTALL_DIR="$SCRATCH/py" UV_PROJECT_ENVIRONMENT="$SCRATCH/venv" UV_CACHE_DIR="$SCRATCH/uvcache"
uv python install 3.13 --managed-python
uv sync --locked --managed-python
uv run --managed-python python -c 'import sqlite3, sys; print(sys.version.split()[0], sqlite3.sqlite_version)'
# expect 3.13.x and 3.5x.y (3.13.18 / 3.53.1 on 2026-11-19)
uv run --managed-python pytest -q tests/test_db.py tests/test_jobs.py
unset UV_PYTHON_INSTALL_DIR UV_PROJECT_ENVIRONMENT UV_CACHE_DIR
rm -rf "$SCRATCH/py" "$SCRATCH/venv" "$SCRATCH/uvcache"
git status --short   # the repo's .venv and uv.lock are untouched
```

(The planner ran this recipe with an existing test module: about 240 MB in the
scratchpad including the uv cache, which fits the 1 GB tmpfs, and the repo's `.venv` was
not touched. If the download is unavailable, record this as unverified rather than
skipping silently.)

Live check of the store from the command line, in a scratch directory:

```bash
uv run python - <<'EOF'
import tempfile
from pathlib import Path

from earmark.db import open_database
from earmark.jobs import JobStore

d = Path(tempfile.mkdtemp())
store = JobStore(open_database(d / "earmark.db"))
a = store.enqueue("https://podcasts.apple.com/us/podcast/x/id1?i=2", prompt=None)
b = store.enqueue("https://podcasts.apple.com/us/podcast/x/id1?i=2", prompt=None)
print(a.created, b.created, a.job.id == b.job.id)  # True False True
claim = store.claim_next()
assert claim is not None
print(store.fail(claim, "feed host 503", transient=True).status)  # queued
print(store.get(a.job.id))
EOF
```

Also check:

- `git status` shows only the files in section 3 and this plan.
- No `# type: ignore` or `noqa` in the new files; no loosened lint, type, or test settings.
- No `datetime` is bound as a SQL parameter (search `jobs.py` for `execute(` calls and
  check the values are strings or ints).

There is no service-level live smoke for this step; step 05's smoke covers the store
through the API and worker.

## 8. Deviations

1. **The claim is `UPDATE … WHERE seq = (SELECT … LIMIT 1) RETURNING`, inside `BEGIN
   IMMEDIATE`.** The architecture says "a single atomic `UPDATE … RETURNING`", which
   still holds; this pins the shape because `UPDATE … LIMIT` does not exist on the CI
   build (section 2.1). Recorded in the architecture.
2. **`Claim` with a private lease token.** The brief's methods take a job; here the
   worker-side methods take the `Claim` returned by `claim_next`. Without a token, a
   worker whose lease expired could complete or fail a job another worker is running.
   `attempts` alone cannot serve as the fence because `release` decrements it. The
   token is a column, not a `Job` field, so the API never exposes it. Recorded in the
   architecture.
3. **Methods beyond the brief:** `enqueue_reanalyze` (step 17's job kind already exists
   in the model), `release` (step 05's SIGTERM path), and `set_episode_key` (the runner
   learns the key after resolve). `enqueue` returns `Enqueued(job, created)` so step 04
   and `earmark submit` can tell a deduped request from a new one.
4. **Attempt accounting:** `claim_next` increments, `release` undoes, a stale requeue
   counts and fails the job at the cap. The brief did not say; step 02 left it to this
   step.
5. **Backoff values** (60 s × 4ⁿ, capped at 6 h, raised to `retry_after_s`) are
   constants, not settings. Adding settings would mean `.env.example` and README
   changes for knobs nobody has needed yet.
6. **Timestamps are stored in UTC**, so a `shared_at` offset is kept as an instant, not
   as its original offset. Notes render dates in `EARMARK_TZ` anyway (steps 12, 18).
7. **`add_warning` ignores an identical repeat**, so a retried run that re-reports a
   cached artifact's warnings does not duplicate them.
8. **Architecture edits:** the Jobs section is rewritten to this contract (claim shape,
   claim token, attempts, backoff, release, stale requeue, dedupe key and index, UTC
   storage, connection discipline); the data layout gains "the data directory must be
   on a local filesystem (WAL)".

## 9. Out of scope

- The API routes, request validation, and prompt normalisation (step 04).
- The worker loop, heartbeat thread and interval, signal handling, how exceptions map
  to `fail`/`skip`, traceback recording, notifying, `earmark worker`, `earmark submit`,
  `earmark jobs` (step 05).
- Entity tables (step 13, as a new migration).
- Pruning old jobs, a `earmark jobs retry` command, and a status page (backlog).
- Documenting the local-filesystem requirement for deployment (step 16).
