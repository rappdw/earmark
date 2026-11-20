"""SQLite access: short-lived connections, WAL, and ordered schema migrations.

Imports only the standard library. Every connection is opened per operation in
``autocommit=True`` mode (the module never starts a transaction implicitly), carries a
busy timeout, and is always closed. Write transactions are ``BEGIN IMMEDIATE`` so a
read-then-write never fails with ``SQLITE_BUSY_SNAPSHOT``.
"""

import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DB_FILENAME: Final = "earmark.db"
DEFAULT_BUSY_TIMEOUT_S: Final = 10.0
_BUSY_SLEEP_S: Final = 0.01


class DatabaseSetupError(Exception):
    """The database cannot be used: newer schema than this code, or WAL unavailable."""


@dataclass(frozen=True, slots=True)
class Migration:
    name: str
    statements: tuple[str, ...]


# Migration rules: append only; never edit or reorder an entry that has shipped. Use plain
# CREATE TABLE / CREATE INDEX without IF NOT EXISTS, so an accidental rerun fails loudly
# instead of silently passing.
MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        "0001_jobs",
        (
            """
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
            ) STRICT
            """,
            """
            CREATE UNIQUE INDEX jobs_active_dedupe ON jobs (dedupe_key)
                WHERE status IN ('queued', 'running')
            """,
            "CREATE INDEX jobs_by_status ON jobs (status, created_at, seq)",
        ),
    ),
    Migration(
        "0002_entities",
        (
            """
            CREATE TABLE episodes (
                episode_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                show_title TEXT NOT NULL,
                published_at TEXT,
                first_shared_at TEXT NOT NULL,
                analyses TEXT NOT NULL CHECK (json_valid(analyses)),
                note_path TEXT,
                transcript_note_path TEXT,
                links_digest TEXT
            ) STRICT
            """,
            """
            CREATE TABLE mentions (
                episode_key TEXT NOT NULL REFERENCES episodes (episode_key) ON DELETE CASCADE,
                prompt_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('person', 'paper', 'company', 'tool', 'topic')),
                name_key TEXT NOT NULL CHECK (name_key <> ''),
                position INTEGER NOT NULL CHECK (position >= 0),
                name TEXT NOT NULL,
                context TEXT NOT NULL,
                timestamps TEXT NOT NULL CHECK (json_valid(timestamps)),
                PRIMARY KEY (episode_key, prompt_hash, kind, name_key)
            ) STRICT
            """,
            "CREATE INDEX mentions_by_name ON mentions (kind, name_key)",
            """
            CREATE TABLE aliases (
                kind TEXT NOT NULL CHECK (kind IN ('person', 'paper', 'company', 'tool', 'topic')),
                alias_key TEXT NOT NULL CHECK (alias_key <> ''),
                canonical_key TEXT NOT NULL CHECK (canonical_key <> ''),
                canonical_name TEXT NOT NULL,
                PRIMARY KEY (kind, alias_key)
            ) STRICT
            """,
            """
            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY,
                kind TEXT CHECK (
                    kind IS NULL OR kind IN ('person', 'paper', 'company', 'tool', 'topic')
                ),
                entity_key TEXT,
                name TEXT,
                note_path TEXT NOT NULL,
                content_digest TEXT NOT NULL
            ) STRICT
            """,
        ),
    ),
)
SCHEMA_VERSION: Final = len(MIGRATIONS)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code: int | None = getattr(exc, "sqlite_errorcode", None)
    return code is not None and code & 0xFF == sqlite3.SQLITE_BUSY


def retry_on_busy[T](fn: Callable[[], T], *, timeout_s: float) -> T:
    """Call ``fn``, retrying every 10 ms on SQLITE_BUSY until ``timeout_s`` has elapsed."""
    start = time.monotonic()
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() - start >= timeout_s:
                raise
        time.sleep(_BUSY_SLEEP_S)


class Database:
    def __init__(self, path: Path, *, busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S) -> None:
        if not math.isfinite(busy_timeout_s) or busy_timeout_s <= 0:
            raise ValueError("busy_timeout_s must be finite and > 0")
        self._path = path
        self._busy_timeout_s = busy_timeout_s

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, autocommit=True, timeout=self._busy_timeout_s)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def ensure_wal(self) -> None:
        with self.connect() as conn:

            def switch() -> str:
                row = conn.execute("PRAGMA journal_mode").fetchone()
                mode = str(row[0]).lower()
                if mode != "wal":
                    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                    mode = str(row[0]).lower()
                return mode

            mode = retry_on_busy(switch, timeout_s=self._busy_timeout_s)
        if mode != "wal":
            raise DatabaseSetupError(
                f"cannot enable WAL on {self._path} (journal mode is {mode!r}); "
                "the filesystem probably lacks shared-memory support"
            )

    def schema_version(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self, migrations: Sequence[Migration] = MIGRATIONS) -> int:
        target = len(migrations)
        with self.transaction() as conn:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current > target:
                raise DatabaseSetupError(
                    f"database schema version {current} is newer than this earmark "
                    f"supports ({target})"
                )
            for migration in migrations[current:]:
                for statement in migration.statements:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(target)}")
            return target - current


def database_path(data_dir: Path) -> Path:
    return data_dir / DB_FILENAME


def open_database(
    path: Path,
    *,
    busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> Database:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(path, busy_timeout_s=busy_timeout_s)
    db.ensure_wal()
    db.migrate(migrations)
    return db
