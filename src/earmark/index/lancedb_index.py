"""The `lancedb` Indexer and Searcher: one LanceDB table of transcript and analysis rows.

lancedb and pyarrow ship few type hints and are an optional extra, so they are imported
through ``importlib`` inside ``load_lance_runtime`` only. Everything else here is typed
against ``LanceRuntime`` and the small Protocols below; the untyped library handles live
inside the adapter classes and never leave them.
"""

import contextlib
import errno
import importlib
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from earmark.config import Settings
from earmark.errors import EarmarkError, PermanentError, TransientError
from earmark.index import embed as embed_module
from earmark.index.chunks import (
    INDEX_VERSION as INDEX_VERSION,
)
from earmark.index.chunks import (
    EpisodeDocs,
    IndexRow,
    NotePaths,
    build_rows,
    rows_digest,
)
from earmark.index.embed import Embedder, clean_text, missing_extra
from earmark.index.search import (
    ROW_KINDS,
    IndexMismatchError,
    InvalidQuery,
    SearchHit,
    SearchQuery,
    SearchResults,
    snippet,
)
from earmark.models import Analysis, Episode, StoreResult, Transcript, format_hms, is_episode_key
from earmark.vault.fs import vault_lock

logger = logging.getLogger(__name__)

TABLE: Final = "chunks"
META_VERSION: Final = "earmark.index_version"
META_MODEL: Final = "earmark.embed_model"
META_DIM: Final = "earmark.embed_dim"
LOCK_TIMEOUT_S: Final = 300.0
OPTIMIZE_EVERY: Final = 20  # optimize after a write whose table version is a multiple of this
CLEANUP_OLDER_THAN: Final = timedelta(hours=1)
BUSY_MESSAGE: Final = "another earmark process is writing the search index; try again later"
SCALAR_COLUMNS: Final = (
    "id",
    "episode_key",
    "kind",
    "prompt_hash",
    "prompt",
    "ordinal",
    "text",
    "start_s",
    "end_s",
    "show_title",
    "episode_title",
    "published_at",
    "episode_url",
    "note_path",
    "transcript_note_path",
)
_FULL_ERRNOS: Final = frozenset({errno.ENOSPC, errno.EDQUOT})

type Row = Mapping[str, object]


class LanceTable(Protocol):
    def metadata(self) -> Mapping[str, str]: ...

    def vector_dim(self) -> int | None: ...

    def version(self) -> int: ...

    def episode_digest(self, key: str, /) -> str | None: ...

    def replace_episode(self, key: str, rows: Sequence[Row], /) -> None:
        """Replace every row of the episode with ``rows`` in one commit."""
        ...

    def search(self, vector: Sequence[float], where: str | None, limit: int, /) -> list[Row]: ...

    def optimize(self, older_than: timedelta, /) -> None: ...


class LanceDb(Protocol):
    def has_table(self) -> bool: ...

    def open_table(self) -> LanceTable: ...

    def create_table(self, dim: int, metadata: Mapping[str, str], /) -> LanceTable: ...

    def drop_table(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LanceRuntime:
    connect: Callable[[Path], LanceDb]  # creates the directory if it is missing


class _Table:
    def __init__(self, pa: Any, table: Any) -> None:
        self._pa = pa
        self._t = table

    def metadata(self) -> Mapping[str, str]:
        raw = self._t.schema.metadata or {}
        return {
            bytes(k).decode("utf-8", "replace"): bytes(v).decode("utf-8", "replace")
            for k, v in raw.items()
        }

    def vector_dim(self) -> int | None:
        schema = self._t.schema
        if "vector" not in schema.names:
            return None
        kind = schema.field("vector").type
        if not self._pa.types.is_fixed_size_list(kind):
            return None
        return int(kind.list_size)

    def version(self) -> int:
        return int(self._t.version)

    def episode_digest(self, key: str, /) -> str | None:
        found: list[Row] = list(
            self._t.search()
            .where(f"episode_key = '{key}'")
            .select(["source_digest"])
            .limit(1)
            .to_list()
        )
        if not found:
            return None
        digest = found[0].get("source_digest")
        return digest if isinstance(digest, str) else None

    def replace_episode(self, key: str, rows: Sequence[Row], /) -> None:
        source = self._pa.Table.from_pylist(list(rows), schema=self._t.schema)
        (
            self._t.merge_insert("id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .when_not_matched_by_source_delete(f"episode_key = '{key}'")
            .execute(source)
        )

    def search(self, vector: Sequence[float], where: str | None, limit: int, /) -> list[Row]:
        query = self._t.search(list(vector), vector_column_name="vector").distance_type("cosine")
        if where is not None:
            query = query.where(where, prefilter=True)
        rows: list[Row] = list(query.select([*SCALAR_COLUMNS, "_distance"]).limit(limit).to_list())
        return rows

    def optimize(self, older_than: timedelta, /) -> None:
        self._t.optimize(cleanup_older_than=older_than)


class _Db:
    def __init__(self, pa: Any, db: Any) -> None:
        self._pa = pa
        self._db = db

    def has_table(self) -> bool:
        return TABLE in list(self._db.list_tables().tables)

    def open_table(self) -> LanceTable:
        return _Table(self._pa, self._db.open_table(TABLE))

    def create_table(self, dim: int, metadata: Mapping[str, str], /) -> LanceTable:
        pa = self._pa

        def field(name: str, kind: Any, *, nullable: bool) -> Any:
            return pa.field(name, kind, nullable=nullable)

        schema = pa.schema(
            [
                field("id", pa.string(), nullable=False),
                field("episode_key", pa.string(), nullable=False),
                field("kind", pa.string(), nullable=False),
                field("prompt_hash", pa.string(), nullable=True),
                field("prompt", pa.string(), nullable=True),
                field("ordinal", pa.int32(), nullable=False),
                field("text", pa.string(), nullable=False),
                field("start_s", pa.float64(), nullable=True),
                field("end_s", pa.float64(), nullable=True),
                field("show_title", pa.string(), nullable=False),
                field("episode_title", pa.string(), nullable=False),
                field("published_at", pa.string(), nullable=True),
                field("episode_url", pa.string(), nullable=False),
                field("note_path", pa.string(), nullable=True),
                field("transcript_note_path", pa.string(), nullable=True),
                field("source_digest", pa.string(), nullable=False),
                field("vector", pa.list_(pa.float32(), dim), nullable=False),
            ],
            metadata=dict(metadata),
        )
        return _Table(pa, self._db.create_table(TABLE, schema=schema))

    def drop_table(self) -> None:
        self._db.drop_table(TABLE)


def load_lance_runtime() -> LanceRuntime:
    """Import lancedb and pyarrow and wrap them. Raises ``IndexExtraMissing``."""
    try:
        lancedb = importlib.import_module("lancedb")
        pa = importlib.import_module("pyarrow")
    except ImportError as exc:
        raise missing_extra(exc, "lancedb", "pyarrow") from None

    def connect(path: Path) -> LanceDb:
        # timedelta(0): a handle sees other processes' commits at once.
        return _Db(pa, lancedb.connect(str(path), read_consistency_interval=timedelta(0)))

    return LanceRuntime(connect)


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    rows: int
    skipped: bool  # inputs unchanged; nothing embedded or written


@contextlib.contextmanager
def _lance(what: str) -> Iterator[None]:
    """Map exceptions from lancedb and pyarrow calls to earmark errors."""
    try:
        yield
    except EarmarkError:
        raise
    except OSError as exc:
        if exc.errno in _FULL_ERRNOS:
            raise TransientError("the search index disk is full") from exc
        raise TransientError(_failure(what, exc)) from exc
    except Exception as exc:
        raise TransientError(_failure(what, exc)) from exc


def _failure(what: str, exc: Exception) -> str:
    if what == "write":
        return f"search index write failed ({type(exc).__name__})"
    return f"the search index could not be read ({type(exc).__name__})"


def _metadata(model: str, dim: int) -> dict[str, str]:
    return {META_VERSION: str(INDEX_VERSION), META_MODEL: model, META_DIM: str(dim)}


class LanceIndex:
    """The `lancedb` Indexer and a Searcher over <DATA_DIR>/lancedb."""

    def __init__(
        self,
        db_dir: Path,
        embedder: Embedder,
        *,
        lock_path: Path,
        runtime: LanceRuntime | None = None,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        optimize_every: int = OPTIMIZE_EVERY,
    ) -> None:
        self._db_dir = db_dir
        self._embedder = embedder
        self._lock_path = lock_path
        self._runtime = runtime if runtime is not None else load_lance_runtime()
        self._lock_timeout_s = lock_timeout_s
        self._optimize_every = optimize_every
        self._db: LanceDb | None = None
        self._search_lock = threading.Lock()

    # -- connection and metadata ----------------------------------------------------------

    def _connection(self, *, create: bool) -> LanceDb | None:
        """The connection; ``None`` when the directory is missing and ``create`` is false.

        ``connect`` creates the directory, so nothing connects before the first write.
        """
        exists = self._db_dir.is_dir()
        if not exists and not create:
            self._db = None
            return None
        if self._db is None or not exists:
            with _lance("write" if create else "read"):
                self._db = self._runtime.connect(self._db_dir)
        return self._db

    def _check(self, table: LanceTable) -> None:
        """Raise ``IndexMismatchError`` unless the table was built like this embedder."""
        meta = table.metadata()
        embedder = self._embedder
        if (
            meta.get(META_VERSION) == str(INDEX_VERSION)
            and meta.get(META_MODEL) == embedder.model
            and meta.get(META_DIM) == str(embedder.dim)
            and table.vector_dim() == embedder.dim
        ):
            return

        def shown(key: str) -> str:
            value = meta.get(key)
            return clean_text(value) if value else "unknown"

        raise IndexMismatchError(
            f"the search index was built with embedding model '{shown(META_MODEL)}' "
            f"({shown(META_DIM)} dimensions, index version {shown(META_VERSION)}), but "
            f"EARMARK_EMBED_MODEL is '{clean_text(embedder.model)}' ({embedder.dim} "
            "dimensions); run `earmark reindex` to rebuild it"
        )

    def _existing_table(self, *, what: str) -> LanceTable | None:
        db = self._connection(create=False)
        if db is None:
            return None
        with _lance(what):
            return db.open_table() if db.has_table() else None

    # -- writing --------------------------------------------------------------------------

    def index(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        stored: StoreResult,
        /,
    ) -> None:
        if transcript.episode_key != episode.key or any(
            a.episode_key != episode.key for a in analyses
        ):
            raise PermanentError("indexer received data for another episode")
        ordered = tuple(sorted(analyses, key=lambda a: (a.created_at, a.prompt_hash)))
        self.replace(EpisodeDocs(episode, transcript, ordered), NotePaths.from_store_result(stored))

    def _writer_lock(self) -> contextlib.AbstractContextManager[None]:
        return vault_lock(
            self._lock_path, timeout_s=self._lock_timeout_s, busy_message=BUSY_MESSAGE
        )

    def replace(self, docs: EpisodeDocs, notes: NotePaths, *, force: bool = False) -> ReplaceResult:
        key = docs.episode.key
        if not is_episode_key(key):
            raise PermanentError("indexer received an invalid episode key")
        # A mismatch must not trigger a model download, so check before loading the model.
        existing = self._existing_table(what="write")
        if existing is not None:
            self._check(existing)
        rows = build_rows(docs, notes, fits=self._embedder.fits)
        digest = rows_digest(rows, model=self._embedder.model, dim=self._embedder.dim)
        with self._writer_lock():
            return self._write(key, rows, digest, force=force)

    def rebuild_episode(
        self, key: str, load: Callable[[], EpisodeDocs | None], notes: NotePaths, /
    ) -> int | None:
        if not is_episode_key(key):
            raise PermanentError("indexer received an invalid episode key")
        # Loads (and the first time downloads) the model without the lock held.
        self._embedder.fits("")
        with self._writer_lock():
            docs = load()
            if docs is None:
                return None
            rows = build_rows(docs, notes, fits=self._embedder.fits)
            digest = rows_digest(rows, model=self._embedder.model, dim=self._embedder.dim)
            self._write(key, rows, digest, force=True)
            return len(rows)

    def _write(
        self, key: str, rows: Sequence[IndexRow], digest: str, *, force: bool
    ) -> ReplaceResult:
        """Steps 4-8 of a replace; the writer lock is held."""
        embedder = self._embedder
        db = self._connection(create=True)
        if db is None:  # pragma: no cover  # create=True always connects
            raise TransientError("the search index could not be opened")
        with _lance("write"):
            if db.has_table():
                table = db.open_table()
                self._check(table)
            else:
                table = db.create_table(embedder.dim, _metadata(embedder.model, embedder.dim))
            if not force and table.episode_digest(key) == digest:
                return ReplaceResult(len(rows), skipped=True)
        vectors = embedder.embed_documents([r.text for r in rows])
        if len(vectors) != len(rows) or any(
            len(v) != embedder.dim or not all(math.isfinite(x) for x in v) for v in vectors
        ):
            raise PermanentError("the embedding model returned vectors of the wrong size")
        records = [
            {**asdict(row), "source_digest": digest, "vector": vector}
            for row, vector in zip(rows, vectors, strict=True)
        ]
        with _lance("write"):
            table.replace_episode(key, records)
            if self._optimize_every > 0 and table.version() % self._optimize_every == 0:
                table.optimize(CLEANUP_OLDER_THAN)
        return ReplaceResult(len(rows), skipped=False)

    def reset(self) -> None:
        embedder = self._embedder
        with self._writer_lock():
            db = self._connection(create=True)
            if db is None:  # pragma: no cover
                raise TransientError("the search index could not be opened")
            with _lance("write"):
                if db.has_table():
                    db.drop_table()
                db.create_table(embedder.dim, _metadata(embedder.model, embedder.dim))

    def optimize(self) -> None:
        with self._writer_lock():
            table = self._existing_table(what="write")
            if table is not None:
                with _lance("write"):
                    table.optimize(CLEANUP_OLDER_THAN)

    # -- searching ------------------------------------------------------------------------

    @staticmethod
    def _filter(query: SearchQuery) -> str | None:
        clauses: list[str] = []
        if query.kinds:
            if any(k not in ROW_KINDS for k in query.kinds):
                raise InvalidQuery("kind must be one of: " + ", ".join(ROW_KINDS))
            clauses.append("kind IN (" + ", ".join(f"'{k}'" for k in query.kinds) + ")")
        if query.episode_key is not None:
            if not is_episode_key(query.episode_key):
                raise InvalidQuery("episode must be an episode key (20 hexadecimal characters)")
            clauses.append(f"episode_key = '{query.episode_key}'")
        return " AND ".join(clauses) or None

    def search(self, query: SearchQuery, /) -> SearchResults:
        started = time.monotonic()
        with self._search_lock:
            table = self._existing_table(what="read")
            if table is None:
                return SearchResults(hits=(), index_exists=False)
            with _lance("read"):
                self._check(table)
            vector = self._embedder.embed_query(query.text)
            where = self._filter(query)
            with _lance("read"):
                raw = table.search(vector, where, query.limit)
        ordered = sorted(raw, key=lambda r: (_number(r.get("_distance")), str(r.get("id"))))
        hits: list[SearchHit] = []
        for row in ordered:
            hit = _hit(row, len(hits) + 1)
            if hit is not None:
                hits.append(hit)
        logger.info("search: %d hits in %.0f ms", len(hits), (time.monotonic() - started) * 1000)
        return SearchResults(hits=tuple(hits), index_exists=True)


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else math.inf


def _hit(row: Row, rank: int) -> SearchHit | None:
    start = row.get("start_s")
    text = row.get("text")
    try:
        return SearchHit.model_validate(
            {
                "rank": rank,
                "score": round(1.0 - _number(row.get("_distance")), 4),
                "episode_key": row.get("episode_key"),
                "show_title": row.get("show_title"),
                "episode_title": row.get("episode_title"),
                "published_at": row.get("published_at"),
                "episode_url": row.get("episode_url"),
                "kind": row.get("kind"),
                "prompt_hash": row.get("prompt_hash"),
                "prompt": row.get("prompt"),
                "start_s": start,
                "end_s": row.get("end_s"),
                "timestamp": format_hms(start) if isinstance(start, int | float) else None,
                "snippet": snippet(text) if isinstance(text, str) else "",
                "note_path": row.get("note_path"),
                "transcript_note_path": row.get("transcript_note_path"),
            }
        )
    except (ValidationError, ValueError):
        logger.warning(
            "skipped a malformed row in the search index (id %s)", clean_text(str(row.get("id")))
        )
        return None


def build_lancedb_index(
    settings: Settings, *, allow_download: bool = True, embedder: Embedder | None = None
) -> LanceIndex:
    """Build the index. Creates nothing on disk and connects to nothing.

    Raises ``ConfigError`` when the `index` extra is missing or ``EMBED_MODEL`` is unknown.
    """
    runtime = load_lance_runtime()
    if embedder is None:
        embedder = embed_module.build_embedder(settings, allow_download=allow_download)
    return LanceIndex(
        settings.data_dir / "lancedb",
        embedder,
        lock_path=settings.data_dir / "index.lock",
        runtime=runtime,
    )
