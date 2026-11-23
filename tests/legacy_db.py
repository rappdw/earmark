"""A database the way earmark v0.1.0 left it. A helper module, not collected.

It is the only kept file that may spell the removed job kind and the old table names.
"""

import sqlite3
from pathlib import Path
from typing import Final

V010_SCHEMA_VERSION: Final = 2
V010_JOB_COUNT: Final = 3

# The eight statements of v0.1.0's MIGRATIONS ("0001_jobs", then "0002_entities"), in order.
V010_STATEMENTS: Final[tuple[str, ...]] = (
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
)

_KEY = "0123456789abcdef0123"
_NOW = "2026-01-01T00:00:00.000000Z"
_INSERT = """
INSERT INTO jobs (id, kind, status, stage, url, prompt, episode_key, dedupe_key, attempts,
                  run_after, warnings, result, shared_at, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)
"""
_ROWS: Final = (
    (
        "a" * 32,
        "ingest",
        "done",
        "notify",
        "https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981",
        None,
        _KEY,
        "dedupe-1",
        1,
        _NOW,
        '{"note_path":"Earmark/Podcasts/x.md","transcript_note_path":null,'
        '"analysis_hash":"bbbbbbbbbbbbbbbb"}',
        _NOW,
        _NOW,
        _NOW,
    ),
    (
        "b" * 32,
        "reanalyze",
        "done",
        "notify",
        None,
        "a question",
        _KEY,
        "dedupe-2",
        1,
        _NOW,
        '{"note_path":"Earmark/Podcasts/x.md","transcript_note_path":null,'
        '"analysis_hash":"cccccccccccccccc"}',
        _NOW,
        _NOW,
        _NOW,
    ),
    (
        "c" * 32,
        "reanalyze",
        "queued",
        None,
        None,
        "another question",
        _KEY,
        "dedupe-3",
        0,
        _NOW,
        None,
        _NOW,
        _NOW,
        _NOW,
    ),
)


def make_v010_database(path: Path, *, wal: bool = True) -> Path:
    """Write ``path`` the way v0.1.0 left it: schema version 2, no application id."""
    conn = sqlite3.connect(path, autocommit=True)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        for statement in V010_STATEMENTS:
            conn.execute(statement)
        for row in _ROWS:
            conn.execute(_INSERT, row)
        conn.execute(f"PRAGMA user_version = {V010_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return path
