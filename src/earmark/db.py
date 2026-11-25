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
APPLICATION_ID: Final = 0x45524D4B  # b"ERMK": marks the post-step-22 lineage
DEFAULT_BUSY_TIMEOUT_S: Final = 10.0
_BUSY_SLEEP_S: Final = 0.01


class DatabaseSetupError(Exception):
    """The database cannot be used: newer schema than this code, another lineage, or no WAL."""


class LegacyDatabaseError(DatabaseSetupError):
    """The file was made by earmark v0.1.0 (or a pre-release build of it)."""


class ForeignDatabaseError(DatabaseSetupError):
    """The file belongs to another application."""


def identity_error(path: Path, application_id: int, user_version: int) -> DatabaseSetupError | None:
    """Why this file cannot be an earmark database of the current lineage, or None."""
    if application_id == APPLICATION_ID or (application_id == 0 and user_version == 0):
        return None
    if application_id == 0:
        return LegacyDatabaseError(
            f"{path} was created by earmark 0.1.0 (schema version {user_version}), which this "
            "version cannot use and does not migrate. Move it aside, with its -wal and -shm "
            "files if present, and start again."
        )
    return ForeignDatabaseError(
        f"{path} is not an earmark database (application id {application_id:#x}). Move it "
        "aside or set EARMARK_DATA_DIR to another directory."
    )


@dataclass(frozen=True, slots=True)
class Migration:
    name: str
    statements: tuple[str, ...]


# Migration rules: append only; never edit or reorder an entry that has shipped. Use plain
# CREATE TABLE / CREATE INDEX without IF NOT EXISTS, so an accidental rerun fails loudly
# instead of silently passing. The list restarted in step 22, with APPLICATION_ID marking the
# new lineage; databases from before are refused (identity_error), never migrated.
MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        "0001_jobs",
        (
            """
            CREATE TABLE jobs (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('ingest')),
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
        "0002_requests",
        (
            """
            ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'apple_podcasts'
                CHECK (source IN ('apple_podcasts', 'upload', 'url'))
            """,
            "ALTER TABLE jobs ADD COLUMN recipient TEXT",
            "ALTER TABLE jobs ADD COLUMN instructions TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN title TEXT",
            "ALTER TABLE jobs ADD COLUMN upload_file TEXT",
            "ALTER TABLE jobs ADD COLUMN upload_name TEXT",
            "ALTER TABLE jobs ADD COLUMN content_sha256 TEXT",
            "UPDATE jobs SET instructions = COALESCE(prompt, '')",
            "ALTER TABLE jobs DROP COLUMN prompt",
            "CREATE INDEX jobs_by_dedupe ON jobs (dedupe_key, status, updated_at)",
        ),
    ),
    Migration(
        "0003_delivery",
        (
            """
            ALTER TABLE jobs ADD COLUMN utc_offset_min INTEGER
                CHECK (utc_offset_min BETWEEN -1439 AND 1439)
            """,
            """
            ALTER TABLE jobs ADD COLUMN delivery_state TEXT
                CHECK (delivery_state IN ('sending', 'sent'))
            """,
            "ALTER TABLE jobs ADD COLUMN message_id TEXT",
            "ALTER TABLE jobs ADD COLUMN sent_at TEXT",
            """
            ALTER TABLE jobs ADD COLUMN resent INTEGER NOT NULL DEFAULT 0
                CHECK (resent IN (0, 1))
            """,
        ),
    ),
    Migration(
        "0004_youtube",
        (
            # SQLite cannot alter a CHECK, so the table is rebuilt with the new source.
            """
            CREATE TABLE jobs_0004 (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('ingest')),
                status TEXT NOT NULL
                    CHECK (status IN ('queued', 'running', 'done', 'failed', 'skipped')),
                stage TEXT,
                url TEXT,
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
                lease_token TEXT,
                source TEXT NOT NULL DEFAULT 'apple_podcasts'
                    CHECK (source IN ('apple_podcasts', 'upload', 'url', 'youtube')),
                recipient TEXT,
                instructions TEXT NOT NULL DEFAULT '',
                title TEXT,
                upload_file TEXT,
                upload_name TEXT,
                content_sha256 TEXT,
                utc_offset_min INTEGER CHECK (utc_offset_min BETWEEN -1439 AND 1439),
                delivery_state TEXT CHECK (delivery_state IN ('sending', 'sent')),
                message_id TEXT,
                sent_at TEXT,
                resent INTEGER NOT NULL DEFAULT 0 CHECK (resent IN (0, 1))
            ) STRICT
            """,
            """
            INSERT INTO jobs_0004 (
                seq, id, kind, status, stage, url, episode_key, dedupe_key, attempts,
                run_after, error, warnings, result, shared_at, created_at, updated_at,
                heartbeat_at, lease_token, source, recipient, instructions, title, upload_file,
                upload_name, content_sha256, utc_offset_min, delivery_state, message_id,
                sent_at, resent
            )
            SELECT
                seq, id, kind, status, stage, url, episode_key, dedupe_key, attempts,
                run_after, error, warnings, result, shared_at, created_at, updated_at,
                heartbeat_at, lease_token, source, recipient, instructions, title, upload_file,
                upload_name, content_sha256, utc_offset_min, delivery_state, message_id,
                sent_at, resent
            FROM jobs
            """,
            "DROP TABLE jobs",
            "ALTER TABLE jobs_0004 RENAME TO jobs",
            """
            CREATE UNIQUE INDEX jobs_active_dedupe ON jobs (dedupe_key)
                WHERE status IN ('queued', 'running')
            """,
            "CREATE INDEX jobs_by_status ON jobs (status, created_at, seq)",
            "CREATE INDEX jobs_by_dedupe ON jobs (dedupe_key, status, updated_at)",
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

    def identity(self) -> tuple[int, int]:
        """``(application_id, user_version)``; read-only."""
        with self.connect() as conn:
            app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return app_id, version

    def check_identity(self) -> None:
        error = identity_error(self._path, *self.identity())
        if error is not None:
            raise error

    def migrate(self, migrations: Sequence[Migration] = MIGRATIONS) -> int:
        target = len(migrations)
        with self.transaction() as conn:
            app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            error = identity_error(self._path, app_id, current)
            if error is not None:
                raise error
            if current > target:
                raise DatabaseSetupError(
                    f"database schema version {current} is newer than this earmark "
                    f"supports ({target})"
                )
            for migration in migrations[current:]:
                for statement in migration.statements:
                    conn.execute(statement)
            if app_id == 0:
                conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
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
    db.check_identity()
    db.ensure_wal()
    db.migrate(migrations)
    return db
