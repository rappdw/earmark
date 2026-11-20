# Step 14: Vector index and search

Plan written 2026-11-20 against the code at `854698e` (steps 01–13 done).

## 1. Outcome

Every processed episode becomes searchable. After the store stage, the `lancedb` indexer
cuts the transcript into chunks along segment boundaries (each with a start and end time),
turns the summary, key claims, research leads, open questions, and prompt answer of every
analysis into rows of their own with a `kind`, embeds all rows locally with fastembed
(`BAAI/bge-small-en-v1.5`, ONNX on CPU), and replaces the episode's rows in one LanceDB
table, `<DATA_DIR>/lancedb/chunks.lance`, in a single atomic commit. Re-indexing unchanged
inputs embeds nothing and writes nothing.

`earmark search "<query>"` and the authenticated `GET /search?q=` return ranked hits:
episode, show, kind, timestamp, snippet, note path, and score. The table records the
embedding model and dimension it was built with. A table built with another model, another
dimension, or another index version is refused, on write and on search, with a message
that says to run `earmark reindex`. `earmark reindex` drops the table and rebuilds it from
`data/episodes/*` and the note paths found by scanning the vault; `--episode KEY`
re-indexes one episode.

lancedb and fastembed live in the `index` extra and are imported only through
`importlib` inside two loader functions, so `import earmark` and the default suite work
without them. The real-LanceDB tests carry a new `index` marker, run with sockets blocked
and a deterministic fake embedder, and run on every push in a new, non-required workflow
(`.github/workflows/index.yml`). One `slow` test downloads the real model in the weekly
live workflow.

## 2. Findings

All checks ran on 2026-11-20 in this sandbox (aarch64, CPython 3.13.5, uv 0.13.9) in
scratch environments under
`/tmp/claude-501/-home-sandy-dev-earmark/88372f9b-11a6-42b3-950d-76488ef69465/scratchpad/s14`,
with `UV_CACHE_DIR` pointed there too, so `/home/sandy/.cache/uv` was not touched. Scripts
ran with `python -I`, `warnings.simplefilter("error")`, and, where noted, a patched
`socket.socket` that refuses `AF_INET`/`AF_INET6` and a `getaddrinfo` that raises.

### 2.1 Wheels and installs (architecture risk 3)

- `uv pip install --only-binary :all: lancedb` resolved **lancedb 0.41.0** (`cp310-abi3`
  manylinux aarch64, 69.8 MiB download) with pyarrow 26.0.1 (49.4 MiB), numpy 2.5.5,
  lance-namespace 0.13.0, deprecation, tqdm, python-dateutil, urllib3. No pylance is
  needed (it is lancedb's optional `pylance` extra).
- `fastembed` resolved **fastembed 0.9.1** (pure Python) with onnxruntime 1.32.0
  (22.1 MiB), tokenizers 0.23.3, huggingface-hub 1.33.0, hf-xet 1.8.0, pillow 13.0.0,
  loguru 0.7.3, mmh3, py-rust-stemmers, requests, httpx 0.28 (the old httpx, a fastembed
  dependency; earmark keeps using httpx2).
- Both import cleanly with warnings as errors. lancedb 0.41.0 ships `py.typed`.
- A trial `uv lock` of `pyproject.toml` with `index = ["lancedb>=0.41.0",
  "fastembed>=0.9.1"]` succeeds with the existing `required-environments` (so every new
  package has a linux/aarch64 wheel). It adds 18 packages and **changes
  `huggingface-hub` from 2.4.0 to 1.33.0**, because fastembed requires
  `huggingface-hub>=0.20,<2.0` and a universal lock picks one version for all extras. The
  `transcribe` extra then gets hub 1.33.0 too. I installed faster-whisper 1.2.1,
  ctranslate2 4.9.0, and av 18.1.0 beside hub 1.33.0: faster-whisper imports with warnings
  as errors, its only hub call is `huggingface_hub.snapshot_download`, and the four error
  classes step 08 matches by name (`RepositoryNotFoundError`, `RevisionNotFoundError`,
  `GatedRepoError`, `DisabledRepoError`) all exist in `huggingface_hub.errors`. I did not
  transcribe with it (no room in `/tmp`); the weekly `slow` job does. Declaring the two
  extras as conflicting would avoid the downgrade but would make it impossible to install
  both, and the worker needs both.
- The trial lock's resolution is deterministic: a second `uv lock` from a cold cache gave a
  byte-identical `uv.lock`.

### 2.2 Disk (the sandbox's tight part)

`/home/sandy` is a 2 GB tmpfs with about 1.2 GB free, of which the uv cache uses 917 MB.
`/tmp` is a separate 1 GB tmpfs.

| What | Size |
|---|---|
| lancedb installed (one 156 MB `_lancedb.abi3.so`) | 160 MB |
| pyarrow | 151 MB |
| numpy + numpy.libs | 54 MB |
| onnxruntime | 59 MB |
| tokenizers, pillow (+libs), hf_xet, huggingface_hub, fastembed, the rest | about 50 MB |
| **The `index` extra on top of the base install** | **about 440 MB** |
| Base + dev + `index` in one venv, uv cache on the same filesystem (hardlinked, so counted once) | **655 MB** |
| `BAAI/bge-small-en-v1.5` (`Qdrant/bge-small-en-v1.5-onnx-Q`, `model_optimized.onnx` plus tokenizer files) | 65 MB |
| `uv lock` with the extra, from a cold cache | 19 MB of cache (metadata only) |

Consequences, which section 7 turns into commands:

- **Never install the `index` extra into the repo's `.venv`.** The repo `.venv` and
  `~/.cache/uv` are on `/home/sandy`; the extra would add about 440 MB to each side of the
  hardlinks (about 500 MB in total) and leave about 700 MB free. `make check` does not need
  it.
- The extra goes into a scratch venv under the session scratchpad in `/tmp`, with
  `UV_CACHE_DIR` and `UV_PROJECT_ENVIRONMENT` both under the scratchpad, so uv hardlinks
  instead of copying and nothing lands in the home cache. That needs about 720 MB of
  `/tmp` with the model. Check `df -h /tmp` first; delete the scratch directory afterwards.
- The model cache for tests and smoke runs goes under the scratchpad too
  (`EARMARK_EMBED_MODEL_DIR`, `SLOW_TEST_CACHE`), never the repo and never `/tmp/fastembed_cache`.
- `uv lock` in the repo with the normal cache adds only a few MB of metadata to
  `~/.cache/uv`. **No `uv cache prune` or `uv cache clean` is needed** after this step if
  the rules above are followed. If someone does install the extra into the repo by
  mistake: `uv sync --locked` (removes it from `.venv`), then `uv cache clean lancedb
  pyarrow onnxruntime fastembed numpy tokenizers pillow hf-xet` to give the space back.
- At the end of my research the scratch directory held about 750 MB. I deleted it (section 8
  records that the scratchpad is empty again).

### 2.3 LanceDB behaviour (0.41.0, checked by running it)

- `lancedb.connect(path)` **creates the directory** (and missing parents) even when only
  `list_tables()` follows. So nothing may call `connect` until the index has been written
  once: the builder and the search path check `db_dir.is_dir()` first, and only the write
  path connects on a missing directory.
- `db.table_names()` emits a `DeprecationWarning` ("use list_tables() instead"), which the
  suite's `filterwarnings = error` turns into a failure. Use `db.list_tables().tables`
  (a `list[str]`; one page of up to 100 names, and there is one table).
- `db.open_table("chunks")` on a missing table raises `ValueError("Table 'chunks' was not
  found")`; `drop_table` on a missing table raises the same. Check `list_tables` instead of
  matching the message. `open_table` costs about 0.25 ms locally.
- `db.rename_table` raises `NotImplementedError` ("not supported in LanceDB OSS"), so a
  rebuild cannot build a side table and swap it in. Reindex drops and recreates.
- `create_table(name, schema=pa.schema(..., metadata={...}))` keeps the schema metadata.
  `table.schema.metadata` comes back as a `dict[bytes, bytes]` and survives `add`,
  `merge_insert`, `delete`, and `optimize`. This is where the embedding model, dimension,
  and index version live.
- **Atomic replace.** `table.merge_insert("id").when_matched_update_all()
  .when_not_matched_insert_all().when_not_matched_by_source_delete("episode_key = '<k>'")
  .execute(pa_table)` updates, inserts, and deletes the episode's rows in one commit
  (`MergeResult(num_updated_rows=3, num_inserted_rows=1, num_deleted_rows=2, ...)`), and
  leaves other episodes alone. With an empty source table (`pa.Table.from_pylist([],
  schema=schema)`) it deletes all of the episode's rows. A list of dicts works as the
  source too, but cannot express an empty one; always pass a `pa.Table` built with the
  table's schema.
- **Search.** `table.search(vector, vector_column_name="vector").distance_type("cosine")
  .where("<filter>", prefilter=True).select([...]).limit(k).to_list()` returns dicts with
  `_distance` (cosine distance, `1 - cosine similarity`) as a Python `float`. Without a
  vector index the search is exhaustive (exact): 30 000 rows of 384 dimensions took 20–38
  ms. No ANN index is needed at homelab scale. `table.search()` with no vector is a plain
  filtered scan (`.where(...).select(["source_digest"]).limit(1)` worked).
- If `select` omits `_distance`, the Rust layer prints `WARN lance::dataset::scanner
  Deprecation warning ... did not include _distance` to stderr (not a Python warning).
  Always select `_distance`.
- A query vector of the wrong length raises `ValueError("Invalid input, query dim(4)
  doesn't match the column vector vector dim(8)")`; adding a row of the wrong length raises
  `ValueError("lance error: ... Cannot cast to FixedSizeList(8) ...")`. The metadata check
  (section 5.6) catches both cases first, with a useful message.
- A table handle opened before another process wrote keeps reading its old version unless
  the connection has `read_consistency_interval=timedelta(0)`; a fresh `open_table` sees
  the latest version. Search opens the table per query.
- **Versions and disk.** Every write is a version, and old versions' files stay on disk:
  50 episodes × 600 rows took 46.7 MB, and 10 more replaces of one episode grew it to
  56 MB. `table.optimize(cleanup_older_than=timedelta(hours=1))` compacts (which
  temporarily adds the compacted copy: 102 MB) and later removes versions older than an
  hour; with every old version removed it was back to 46 MB. `cleanup_older_than=0`
  emits a `UserWarning` (fatal in the suite) because concurrent readers of old versions
  fail. Use one hour.
- **Concurrency.** A reader process opening the table and searching in a loop (1 647
  searches) while a writer process ran 60 `merge_insert`s and 12 `optimize` calls saw no
  error. LanceDB resolves concurrent commits itself, but earmark still serialises its own
  writers with a lock file (section 5.7), so a reindex cannot interleave with the worker.
- **fork.** With lancedb imported, `os.fork` (multiprocessing's `fork` start method) emits
  `RuntimeWarning: lancedb fork support is experimental ... a small chance of deadlock`.
  earmark never forks (uvicorn runs one process, the worker is single-threaded per job).
  Tests must not use `multiprocessing` with `fork` after importing lancedb.
- No telemetry: the only "telemetry" module, `lancedb/otel.py`, bridges metrics into a
  user's OpenTelemetry provider only when `instrument_lancedb_metrics()` is called. With
  sockets blocked, connect, create, merge, search, optimize, and drop all worked.

### 2.4 fastembed behaviour (0.9.1, checked by running it)

- `TextEmbedding.__init__` defaults to `model_name="BAAI/bge-small-en-v1.5"` and
  `cache_dir=None`, which means `$FASTEMBED_CACHE_PATH` or **`<tempdir>/fastembed_cache`**:
  wiped on reboot, so earmark always passes an explicit `cache_dir`.
- `TextEmbedding.list_supported_models()` returns dicts with `model`, `dim`, `size_in_GB`,
  `sources`, `description`, and more, without any I/O. That gives the canonical model name
  (matching is case-insensitive) and the dimension at build time, without loading or
  downloading. `TextEmbedding.get_embedding_size(name)` does the same for the dimension and
  raises `ValueError` for an unknown name. `TextEmbedding("nope/nope")` raises
  `ValueError("Model nope/nope is not supported in TextEmbedding. ...")`.
- **The cache lookup or download happens in `__init__`, even with `lazy_load=True`**
  (`lazy_load` only defers the ONNX session). So earmark must not construct
  `TextEmbedding` at build time; it constructs it on first use.
- The bge-small model resolves to the Hugging Face repo `Qdrant/bge-small-en-v1.5-onnx-Q`
  (a quantised ONNX file, 66 465 124 bytes) and downloads through `huggingface_hub` into
  the HF cache layout under `cache_dir`, in about 6 s here (the sandbox reaches Hugging Face
  through its proxy). It prints "You are sending unauthenticated requests to the HF Hub"
  and tqdm progress bars on stderr.
- A cached model loads with sockets and DNS blocked, both with `local_files_only=True` and
  without it: fastembed first tries the cache with `local_files_only=True`, then downloads.
  `HF_HUB_OFFLINE=1` also forces cache-only.
- A missing model with `local_files_only=True` raises `ValueError("Could not load model
  BAAI/bge-base-en-v1.5 from any source.")` at once, after two **loguru** `ERROR` lines on
  stderr (fastembed logs through loguru, not `logging`, so the redacting formatter never
  sees them). Without `local_files_only` it retries three times with 3 s and 9 s sleeps,
  then raises the same `ValueError`. earmark calls `loguru.logger.disable("fastembed")` in
  its loader and reports errors itself.
- Vectors are `numpy.float32` arrays of length 384, L2-normalised (`sum(v**2) == 1.0000001`).
  `query_embed` and `passage_embed` give identical vectors for bge-small (fastembed adds
  prefixes only for models that need them, such as e5 and nomic); earmark calls the right
  one anyway so other models work.
- **Truncation.** The tokenizer truncates at the model's limit
  (`model.model.tokenizer.truncation == {"max_length": 512, ...}` for bge-small) without
  any warning, and `token_count(text)` reports the truncated count: `"word " * 50` → 52
  (50 + `[CLS]` + `[SEP]`), `"!" * 1000` → 512, `"汉字" * 400` → 512, `"x" * 3000` → 3
  (one over-long word becomes `[UNK]`). So "the text fits" is `token_count(text) <
  max_length`. Limits differ by model: `sentence-transformers/paraphrase-multilingual-
  MiniLM-L12-v2` truncates at 128 tokens, nomic at 8192. A fixed character budget cannot
  be right for all of them (CJK text is about one token per character), so the chunker
  asks the embedder (section 5.1).
- Speed and memory: the first embed after a cached load took 0.07 s; 256 chunks of about
  750 characters took 13.7 s with `threads=2` (batch 64); peak RSS of that process was
  626 MB. A one-hour episode is about 80 chunks. earmark uses onnxruntime's default thread
  count and batch 32.
- `token_count` is on `TextEmbedding` (`token_count(texts, batch_size=1024) -> int`, a sum
  over the texts); call it with one text at a time.

### 2.5 Choice of store and embedder

LanceDB works on this platform, so the architecture's choice stands; nothing from the
original sketch's alternatives was needed. fastembed stays the embedder: it is small (no
torch), has aarch64 wheels for everything it needs, knows each model's dimension and
query/passage prefixes, and exposes the tokenizer for the fit check. The alternatives were
worse here: sentence-transformers needs torch (gigabytes); calling onnxruntime and
tokenizers directly would mean re-implementing model download, pooling, and normalisation.
The default model stays `BAAI/bge-small-en-v1.5` (fastembed's default, 384 dimensions,
67 MB, English). `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384
dimensions, 220 MB, about 50 languages, 128-token limit) is the documented choice for
non-English shows.

### 2.6 What exists that this step builds on

- `Indexer` (step 02) is `index(episode, transcript, analyses, stored, /) -> None`; the
  runner (step 05) calls it after the store with every analysis on disk, sets stage
  `index`, and turns any exception into the job warning `index failed: <describe_error>`.
  No runner change is needed.
- `INDEXERS` in `pipeline/factory.py` has `fake` and `none`; `lancedb` is the default
  setting and is not built, so today the default worker refuses to start.
- `Settings.embed_model` exists (`BAAI/bge-small-en-v1.5`) and is in `.env.example`.
- `ArtifactStore.list_episode_keys()` lists episodes with `episode.json`;
  `read_episode`, `read_transcript`, and `list_analyses` (sorted by `(created_at,
  prompt_hash)`) read them and raise `PermanentError` on corrupt JSON.
- `VaultStore._scan(root)` maps `(kind, episode_key)` identities to note paths without
  writing; `Vault.open_subdir()` **creates** missing subdir parts, so a read-only locator
  needs a non-creating variant (section 4.9).
- `vault.fs.vault_lock(path, timeout_s=)` is a generic `flock` helper whose busy message
  names the vault.
- `analyze.chunking._split_text(text, fits)` splits text at sentence ends, then words,
  then characters, so every piece satisfies `fits`. The index chunker reuses it.
- `tests/test_ci.py` pins the shape of every workflow; `tests/test_tooling.py` pins the
  marker list and the default `-m` expression; `tests/test_factory.py` and
  `tests/test_cli.py` have light-import tests listing `lancedb` and `fastembed`.
- `tests/test_cli.py::test_serve_runs_uvicorn` runs `earmark serve` with default settings
  (so `INDEX_BACKEND=lancedb`) and expects it to start.
- `tests/test_api.py` builds `create_app(settings, store=...)` with default settings.

### 2.7 Dead ends

- Do not call `db.table_names()` (deprecation warning, fatal in tests).
- Do not call `lancedb.connect` on a path that does not exist yet from the builder, the
  API, or `earmark search` (it creates the directory).
- Do not construct `fastembed.TextEmbedding` in a builder, not even with `lazy_load=True`
  (it looks up or downloads the model in `__init__`).
- Do not rely on `rename_table` (unsupported in OSS).
- Do not call `optimize(cleanup_older_than=timedelta(0))` (warning; breaks readers).
- Do not `select` without `_distance` (Rust warning on stderr).
- Do not import `lancedb`, `pyarrow`, `fastembed`, `numpy`, or `loguru` at module top or in
  tests; go through `importlib` inside the loaders (mypy runs without the extra, and the
  repo has no `ignore_missing_imports` overrides, which must stay that way).
- Do not interpolate anything into a LanceDB filter string that is not a validated episode
  key or a member of the `RowKind` literal.
- Do not compare a `token_count` with `<=` the max length; a truncated text reports exactly
  the max length (use `<`).
- Do not use fastembed's default cache dir.

## 3. Files

| File | Change |
|---|---|
| `pyproject.toml` | `index` extra: `lancedb>=0.41.0`, `fastembed>=0.9.1`. New marker `index`. The default `-m` expression gains `and not index`. |
| `uv.lock` | Regenerated with `uv lock` (adds 18 packages; huggingface-hub 2.4.0 → 1.33.0). |
| `.env.example` | Add `# EARMARK_EMBED_MODEL_DIR=` under `--- Index ---` with a one-line comment. |
| `src/earmark/config.py` | New field `embed_model_dir: Path \| None`; `embed_model` joins the strip-and-non-empty validator. |
| `src/earmark/index/__init__.py` | New. Docstring only. |
| `src/earmark/index/search.py` | New. `RowKind`, `SearchQuery`, `parse_query`, `SearchHit`, `SearchResults`, `Searcher`, `UnavailableSearcher`, `SearchUnavailableError`, `IndexMismatchError`, `snippet`, constants. Imports only stdlib and `earmark.models`/`earmark.errors`. |
| `src/earmark/index/chunks.py` | New. Pure: `IndexRow`, `EpisodeDocs`, `NotePaths`, `build_rows`, `rows_digest`, `load_episode_docs`. |
| `src/earmark/index/embed.py` | New. `Embedder` protocol, `FastEmbedEmbedder`, `FastEmbedRuntime`, `load_fastembed_runtime`, `build_embedder`, `IndexExtraMissing`, `EmbedModelMissing`, `default_model_dir`. |
| `src/earmark/index/lancedb_index.py` | New. `LanceRuntime`, `load_lance_runtime`, `LanceIndex` (the `lancedb` `Indexer` and a `Searcher`), `ReplaceResult`, `build_lancedb_index`. |
| `src/earmark/index/rebuild.py` | New. `RebuildTarget`, `NoteLocator`, `ReindexReport`, `reindex`. |
| `src/earmark/fakes.py` | Add `FakeEmbedder` and `FakeSearcher`; `FakeStore.locate_notes`. `FakeIndexer` is unchanged. |
| `src/earmark/pipeline/factory.py` | `INDEXERS["lancedb"]`; new `SEARCHERS` registry and `build_searcher`; `build_note_locator`. |
| `src/earmark/vault/fs.py` | `vault_lock` gains a keyword `busy_message`; `Vault.open_existing_subdir()`. |
| `src/earmark/vault/notes.py` | `VaultStore.locate_notes(keys)`, read-only. |
| `src/earmark/analyze/chunking.py` | Public alias `split_text = _split_text` (no behaviour change). |
| `src/earmark/api/app.py` | `create_app(..., searcher=None)`; handlers for search errors. |
| `src/earmark/api/routes.py` | `GET /search`. |
| `src/earmark/api/schemas.py` | `SearchResponse`. |
| `src/earmark/cli.py` | `earmark search`, `earmark reindex`; `serve` builds the searcher and degrades to 503 when it cannot. |
| `tests/conftest.py` | For tests without `network`/`slow`/`llm`: set `HF_HUB_OFFLINE=1` and point `FASTEMBED_CACHE_PATH` at an empty directory from `tmp_path_factory` (defence in depth; sockets are blocked anyway). |
| `tests/index_helpers.py` | New. Builders for synthetic episodes, transcripts, and analyses; `RecordingTarget` for `reindex`; `stub_fastembed_runtime()`; `stub_lance_runtime_missing()`. |
| `tests/test_index_chunks.py` | New, default suite. |
| `tests/test_index_embed.py` | New, default suite. |
| `tests/test_index_search.py` | New, default suite: query parsing, snippets, `FakeSearcher`, `UnavailableSearcher`. |
| `tests/test_index_rebuild.py` | New, default suite: `reindex` orchestration with `RecordingTarget`; `locate_notes`. |
| `tests/test_api_search.py` | New, default suite: `GET /search` with `FakeSearcher`. |
| `tests/test_cli.py` | `search`, `reindex`, and `serve` cases with fakes and stubs. |
| `tests/test_factory.py` | `lancedb` builder cases; `SEARCHERS` matches `INDEXERS`; `test_default_settings_list_all_unbuilt` now expects only `notify_backend`; heavier light-import lists. |
| `tests/test_lancedb_index.py` | New, `pytestmark = pytest.mark.index`: real LanceDB in `tmp_path`, `FakeEmbedder`. |
| `tests/test_index_slow.py` | New, `pytestmark = pytest.mark.slow`: real fastembed model, real LanceDB. |
| `tests/test_tooling.py` | `index` in the registered-marker list and the inner marker tests. |
| `tests/test_ci.py` | Tests for `index.yml`; the slow job's new commands and `cache-suffix`. |
| `tests/slow_assets.py` | `embed_model_dir()` → `cache_dir() / "fastembed"`. |
| `.github/workflows/index.yml` | New workflow "Index tests". |
| `.github/workflows/network.yml` | The `slow` job installs and runs with both extras; `cache-suffix: slow`. |
| `docs/ARCHITECTURE.md` | Already updated by this plan (section 8). |
| `docs/PLAN.md` | Status row only, by the orchestrator. |

## 4. Interfaces

Snippets are illustrative; names, fields, and signatures are binding.

### 4.1 Settings

```python
embed_model: str = Field("BAAI/bge-small-en-v1.5", description="Embedding model (fastembed).")
embed_model_dir: Path | None = Field(
    None, description="Where embedding models are downloaded (default: DATA_DIR/models/embed)."
)
```

`embed_model` is added to the existing `_check_model_name` validator (stripped, non-empty).
No other new setting. `EMBED_MODEL` must be a name in fastembed's
`TextEmbedding.list_supported_models()` (case-insensitive); that is checked by the builder,
not at settings load, because settings must load without the extra.

`.env.example` under `--- Index ---`:

```
EARMARK_EMBED_MODEL=BAAI/bge-small-en-v1.5
# EARMARK_EMBED_MODEL_DIR=
# Changing EARMARK_EMBED_MODEL needs `earmark reindex`.
```

### 4.2 `earmark.index.search`

```python
RowKind = Literal["transcript", "summary", "claim", "lead", "question", "answer"]
ROW_KINDS: Final[tuple[RowKind, ...]] = typing.get_args(RowKind)
MAX_QUERY_CHARS: Final = 500
MAX_SEARCH_LIMIT: Final = 50
DEFAULT_SEARCH_LIMIT: Final = 10
SNIPPET_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str  # whitespace-collapsed, 1..MAX_QUERY_CHARS characters
    limit: int = DEFAULT_SEARCH_LIMIT  # 1..MAX_SEARCH_LIMIT
    kinds: tuple[RowKind, ...] = ()  # empty: every kind; deduplicated, in ROW_KINDS order
    episode_key: str | None = None  # 20 lower-case hex characters


class InvalidQuery(ValueError):
    """str(exc) is a short message that never contains the query."""


def parse_query(
    text: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    kinds: Sequence[str] = (),
    episode_key: str | None = None,
) -> SearchQuery: ...  # raises InvalidQuery


def snippet(text: str, max_chars: int = SNIPPET_CHARS) -> str: ...


class SearchHit(Model):  # earmark.models.Model: frozen, extra="forbid", surrogates scrubbed
    rank: int  # 1-based
    score: float  # cosine similarity, rounded to 4 places
    episode_key: EpisodeKey
    show_title: str
    episode_title: str
    published_at: AwareDatetime | None
    episode_url: str
    kind: RowKind
    prompt_hash: PromptHash | None
    prompt: str | None  # at most 200 characters
    start_s: Seconds | None
    end_s: Seconds | None
    timestamp: str | None  # format_hms(start_s), "HH:MM:SS"
    snippet: str
    note_path: str | None
    transcript_note_path: str | None


class SearchResults(Model):
    hits: tuple[SearchHit, ...] = ()
    index_exists: bool = True  # False when no index has been written yet


class Searcher(Protocol):
    def search(self, query: SearchQuery, /) -> SearchResults: ...


class SearchUnavailableError(EarmarkError):
    """Search cannot run in this process (extra missing, backend none, bad config)."""


class IndexMismatchError(PermanentError):
    """The table was built with another embedding model, dimension, or index version."""


class UnavailableSearcher:
    def __init__(self, reason: str) -> None: ...

    def search(self, query: SearchQuery, /) -> SearchResults: ...  # raises SearchUnavailableError
```

Messages of `parse_query` (exact, used by the API and the CLI):

| Input | Message |
|---|---|
| blank after collapsing | `q must not be blank` |
| more than 500 characters after collapsing | `q must be at most 500 characters` |
| limit out of range | `limit must be between 1 and 50` |
| unknown kind | `kind must be one of: transcript, summary, claim, lead, question, answer` |
| bad episode key | `episode must be an episode key (20 hexadecimal characters)` |

`SearchHit`, `SearchResults` live here, not in `models.py`, so
`test_models.py::test_round_trip_covers_every_model` is unaffected; give them their own
round-trip test.

### 4.3 `earmark.index.chunks`

```python
MAX_ROW_CHARS: Final = 1000  # a row's text, after whitespace collapsing
MAX_CHUNK_SPAN_S: Final = 120.0  # a transcript chunk covers at most this much time
PROMPT_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class NotePaths:
    note_path: str | None = None
    transcript_note_path: str | None = None

    @classmethod
    def from_store_result(cls, stored: StoreResult) -> Self: ...


@dataclass(frozen=True, slots=True)
class EpisodeDocs:
    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]  # sorted by (created_at, prompt_hash), at least one


@dataclass(frozen=True, slots=True)
class IndexRow:
    id: str
    episode_key: str
    kind: RowKind
    prompt_hash: str | None
    prompt: str | None
    ordinal: int
    text: str
    start_s: float | None
    end_s: float | None
    show_title: str
    episode_title: str
    published_at: str | None  # ISO 8601 in UTC, or None
    episode_url: str
    note_path: str | None
    transcript_note_path: str | None


def build_rows(
    docs: EpisodeDocs, notes: NotePaths, *, fits: Callable[[str], bool]
) -> tuple[IndexRow, ...]: ...


def rows_digest(rows: Sequence[IndexRow], *, model: str, dim: int) -> str: ...  # 32 hex


def load_episode_docs(artifacts: ArtifactStore, key: str) -> EpisodeDocs | None: ...
```

`load_episode_docs` returns `None` when `episode.json` or `transcript.json` is missing or
there is no analysis; it lets `PermanentError` (corrupt JSON) propagate. Pure apart from
those reads; imports only stdlib, `earmark.models`, `earmark.artifacts`,
`earmark.analyze.chunking.split_text`, and `earmark.index.search.RowKind`.

### 4.4 `earmark.index.embed`

```python
EXTRA: Final = "index"
INSTALL_HINT: Final = "install it with `uv sync --extra index`"
EMBED_BATCH: Final = 32
DEFAULT_MAX_TOKENS: Final = 512


class Embedder(Protocol):
    @property
    def model(self) -> str: ...  # canonical id, recorded in the table

    @property
    def dim(self) -> int: ...

    def fits(self, text: str, /) -> bool: ...  # embeddable without truncation

    def embed_documents(self, texts: Sequence[str], /) -> list[list[float]]: ...

    def embed_query(self, text: str, /) -> list[float]: ...


class IndexExtraMissing(ConfigError):
    """lancedb or fastembed (the `index` extra) is not installed or does not import."""


class EmbedModelMissing(TransientError):
    """The model is not in the cache and this process may not download it."""


class TextModelLike(Protocol):  # what FastEmbedEmbedder needs from fastembed.TextEmbedding
    max_tokens: int

    def passage_embed(self, texts: Sequence[str], /) -> list[list[float]]: ...

    def query_embed(self, text: str, /) -> list[float]: ...

    def token_count(self, text: str, /) -> int: ...


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str  # canonical, as fastembed lists it
    dim: int


@dataclass(frozen=True, slots=True)
class FastEmbedRuntime:
    supported: Callable[[], Sequence[ModelInfo]]
    load: Callable[[str, Path, bool], TextModelLike]  # (name, cache_dir, allow_download)


def load_fastembed_runtime() -> FastEmbedRuntime: ...  # raises IndexExtraMissing


def default_model_dir(settings: Settings) -> Path: ...  # embed_model_dir or data_dir/models/embed


class FastEmbedEmbedder:
    def __init__(
        self,
        runtime: FastEmbedRuntime,
        info: ModelInfo,
        cache_dir: Path,
        *,
        allow_download: bool,
    ) -> None: ...


def build_embedder(settings: Settings, *, allow_download: bool) -> Embedder: ...
```

- `load_fastembed_runtime()` imports `fastembed` (and `loguru`, to call
  `logger.disable("fastembed")`) with `importlib.import_module` inside the function, the
  same way as `transcribe.faster_whisper.load_runtime`. `ModuleNotFoundError` whose `name`
  is `fastembed` (or a submodule) → `IndexExtraMissing` with "which is not installed;
  install it with `uv sync --extra index`"; any other `ImportError` → "which failed to
  import (<ExceptionType>); reinstall it with `uv sync --extra index`".
- `runtime.load(name, cache_dir, allow_download)` constructs
  `TextEmbedding(model_name=name, cache_dir=str(cache_dir), local_files_only=not
  allow_download)` and wraps it: `passage_embed` and `query_embed` convert each numpy vector
  with `.tolist()` (batch size `EMBED_BATCH`); `max_tokens` is
  `model.model.tokenizer.truncation["max_length"]` when that is a positive int, else
  `DEFAULT_MAX_TOKENS`; `token_count(text)` is `model.token_count(text)`. All numpy and
  fastembed objects stay inside the closure.
- `build_embedder` loads the runtime, finds `settings.embed_model` among
  `runtime.supported()` case-insensitively, and returns a `FastEmbedEmbedder` with the
  canonical name and dimension. An unknown name → `ConfigError(["EARMARK_EMBED_MODEL:
  '<name>' is not a fastembed text embedding model (see
  https://qdrant.github.io/fastembed/examples/Supported_Models/)"])`, with the name cleaned
  and capped at 100 characters. **It constructs no model and creates no directory.**
- `FastEmbedEmbedder.fits`, `embed_documents`, and `embed_query` load the model on first
  use under a `threading.Lock` (one load per instance), creating `cache_dir` first.
  `fits(text)` is `token_count(text) < max_tokens`. `embed_*` also run under the lock.
- Load errors: a `ValueError` from fastembed with `allow_download=False` →
  `EmbedModelMissing("the embedding model <name> is not downloaded yet (looked in
  EARMARK_EMBED_MODEL_DIR); it is downloaded when the worker indexes an episode or by
  `earmark reindex`")`. With `allow_download=True`, a `ValueError` or `OSError` →
  `TransientError("could not load or download the embedding model <name> (check network
  access to Hugging Face and EARMARK_EMBED_MODEL_DIR)")`. Other exceptions from onnxruntime
  → `TransientError("the embedding model <name> failed to load (<ExceptionType>)")`. No
  library text or path in any message.
- `embed_documents([])` returns `[]` without loading the model.

### 4.5 `earmark.index.lancedb_index`

```python
TABLE: Final = "chunks"
INDEX_VERSION: Final = 1
META_VERSION: Final = "earmark.index_version"
META_MODEL: Final = "earmark.embed_model"
META_DIM: Final = "earmark.embed_dim"
LOCK_TIMEOUT_S: Final = 300.0
OPTIMIZE_EVERY: Final = 20  # optimize after a write whose table version is a multiple of this
CLEANUP_OLDER_THAN: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    rows: int
    skipped: bool  # inputs unchanged; nothing embedded or written


def load_lance_runtime() -> LanceRuntime: ...  # imports lancedb, pyarrow; raises IndexExtraMissing


class LanceIndex:
    """The `lancedb` Indexer and a Searcher over <DATA_DIR>/lancedb."""

    def __init__(
        self,
        db_dir: Path,
        embedder: Embedder,
        *,
        lock_path: Path,
        runtime: LanceRuntime | None = None,  # None: load_lance_runtime()
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        optimize_every: int = OPTIMIZE_EVERY,
    ) -> None: ...

    def index(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        stored: StoreResult,
        /,
    ) -> None: ...

    def replace(
        self, docs: EpisodeDocs, notes: NotePaths, *, force: bool = False
    ) -> ReplaceResult: ...

    def rebuild_episode(
        self, key: str, load: Callable[[], EpisodeDocs | None], notes: NotePaths, /
    ) -> int | None: ...  # rows written, None when load() returned None

    def reset(self) -> None: ...

    def optimize(self) -> None: ...

    def search(self, query: SearchQuery, /) -> SearchResults: ...


def build_lancedb_index(
    settings: Settings, *, allow_download: bool = True, embedder: Embedder | None = None
) -> LanceIndex: ...
```

- `LanceRuntime` is a frozen dataclass of callables (the shape is the implementer's
  choice) built in `load_lance_runtime()` from `importlib.import_module("lancedb")` and
  `("pyarrow")`, typed through small local Protocols, like step 08's `WhisperRuntime`. No
  `Any` escapes it, and no `# type: ignore` is needed. `IndexExtraMissing` messages as in
  4.4 with `lancedb`.
- `build_lancedb_index` loads both runtimes (so a missing extra is a startup
  `ConfigError`), builds the embedder with `embed.build_embedder(settings,
  allow_download=...)` looked up through the module at call time (tests monkeypatch
  `earmark.index.embed.build_embedder`) unless `embedder` is given, and returns
  `LanceIndex(settings.data_dir / "lancedb", embedder, lock_path=settings.data_dir /
  "index.lock")`. It creates nothing on disk and connects to nothing.
- Table schema (exact field names, order, and nullability):

| Field | Arrow type | Null |
|---|---|---|
| `id` | `string` | no |
| `episode_key` | `string` | no |
| `kind` | `string` | no |
| `prompt_hash` | `string` | yes |
| `prompt` | `string` | yes |
| `ordinal` | `int32` | no |
| `text` | `string` | no |
| `start_s` | `float64` | yes |
| `end_s` | `float64` | yes |
| `show_title` | `string` | no |
| `episode_title` | `string` | no |
| `published_at` | `string` | yes |
| `episode_url` | `string` | no |
| `note_path` | `string` | yes |
| `transcript_note_path` | `string` | yes |
| `source_digest` | `string` | no |
| `vector` | `fixed_size_list<float32>[dim]` | no |

  Schema metadata: `earmark.index_version` = `"1"`, `earmark.embed_model` = the embedder's
  canonical name, `earmark.embed_dim` = the dimension as a decimal string.

### 4.6 `earmark.index.rebuild`

```python
type NoteLocator = Callable[[Sequence[str]], Mapping[str, NotePaths]]


class RebuildTarget(Protocol):
    def reset(self) -> None: ...

    def rebuild_episode(
        self, key: str, load: Callable[[], EpisodeDocs | None], notes: NotePaths, /
    ) -> int | None: ...

    def optimize(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReindexReport:
    episodes: int
    rows: int
    skipped: int
    warnings: tuple[str, ...]


def reindex(
    artifacts: ArtifactStore,
    target: RebuildTarget,
    locate: NoteLocator,
    *,
    only: str | None = None,
) -> ReindexReport: ...
```

### 4.7 Fakes (`earmark.fakes`)

```python
class FakeEmbedder:
    def __init__(
        self, *, model: str = "fake-embed-v1", dim: int = 64, max_chars: int = 1000
    ) -> None:
        self.documents: list[str] = []  # every text embed_documents received
        self.queries: list[str] = []
        ...

    def fail_next(self, exc: Exception, times: int = 1) -> None: ...


class FakeSearcher(FakeStage[SearchQuery]):
    def __init__(self, hits: Sequence[SearchHit] = (), *, index_exists: bool = True) -> None: ...

    def search(self, query: SearchQuery) -> SearchResults: ...  # the first query.limit hits
```

`FakeEmbedder` is deterministic and ranks by shared words: tokens are
`re.findall(r"\w+", text.casefold())`; each token adds `±1` at index
`int.from_bytes(sha256(token)[:4]) % dim`, the sign from bit 0 of byte 4; the vector is
L2-normalised, and a text with no tokens maps to the unit vector on index 0. `fits(text)`
is `len(text) <= max_chars`. `fail_next` makes the next `embed_documents` or `embed_query`
raise. It satisfies `Embedder` structurally (checked in a test).

`FakeStore.locate_notes(keys)` returns `{k: NotePaths(f"{subdir}/Podcasts/{k}.md", None)}`
for every key, matching the paths `FakeStore.store` returns.

### 4.8 Factory

```python
INDEXERS: Final[Mapping[str, Builder[Indexer | None]]] = {
    "fake": lambda ctx: FakeIndexer(),
    "lancedb": _lancedb_indexer,  # build_lancedb_index(ctx.settings, allow_download=True)
    "none": lambda ctx: None,
}
SEARCHERS: Final[Mapping[str, Callable[[Settings], Searcher | None]]] = {
    "fake": lambda settings: FakeSearcher(),
    "lancedb": _lancedb_searcher,  # build_lancedb_index(settings, allow_download=False)
    "none": lambda settings: None,
}


def build_searcher(settings: Settings) -> Searcher | None: ...  # ConfigError if unbuilt
def build_note_locator(settings: Settings) -> NoteLocator: ...  # from store_backend
```

`build_note_locator`: `vault` → `build_vault_store(settings).locate_notes` (so a missing
`VAULT_DIR` is a `ConfigError`); `fake` → `FakeStore(settings.vault_dir,
settings.vault_subdir).locate_notes`. Builders import `earmark.index.*` inside the
function; `factory` itself must not import `earmark.index.lancedb_index` or
`earmark.index.embed` at module top.

### 4.9 Vault additions

```python
# vault/fs.py
@contextmanager
def vault_lock(
    path: Path,
    *,
    timeout_s: float,
    busy_message: str = "another earmark process is writing to the vault; try again later",
) -> Iterator[None]: ...


class Vault:
    def open_existing_subdir(self) -> VaultDir | None: ...  # None if VAULT_DIR or a part is missing


# vault/notes.py
class VaultStore:
    def locate_notes(self, keys: Sequence[str], /) -> dict[str, NotePaths]: ...
```

`open_existing_subdir` opens like `open_subdir` but with `create=False`, refusing symlinks
the same way, and returns `None` when `VAULT_DIR` or any subdir part does not exist.
`locate_notes` scans once with `_scan` (no lock, no registry, no write; nothing is
created), and for each requested key returns the vault-relative paths of its `episode`
and `transcript` identity notes, the smallest path when there are duplicates; keys with
neither are omitted.

### 4.10 HTTP

`GET /search`, behind the existing bearer middleware (no change to `auth.py`).

| Parameter | Type | Rule |
|---|---|---|
| `q` | string, required | whitespace-collapsed; 1–500 characters |
| `limit` | int | 1–50, default 10 |
| `kind` | repeatable | each a `RowKind`; omitted means all |
| `episode` | string | an episode key |

```python
class SearchResponse(ApiModel):
    hits: tuple[SearchHit, ...]
```

| Status | When | Body |
|---|---|---|
| 200 | search ran (no index yet: `{"hits": []}`) | `SearchResponse` |
| 401 | missing or wrong token (middleware) | `{"detail": "missing or invalid API token"}` |
| 409 | `IndexMismatchError` | `{"detail": <its message>}` |
| 422 | `InvalidQuery` or a FastAPI query validation error | `{"detail": ...}`, never the input |
| 503 | `SearchUnavailableError` | `{"detail": <reason>}` |
| 503 | any other `EarmarkError` (model not downloaded, LanceDB read failure, lock) | `{"detail": <message>}`, `Retry-After: 5` for a `TransientError` |

`create_app(settings, *, store=None, searcher=None)`: `searcher=None` means
`UnavailableSearcher("search is not configured in this process")`. The route is a plain
`def` (it runs in the thread pool). It reads no body; no `max_body_size` is needed.

### 4.11 CLI

```
earmark search QUERY [--limit N] [--kind KIND ...] [--episode KEY] [--json]
earmark reindex [--episode KEY]
```

`earmark search` builds the searcher with `factory.build_searcher` (never downloads) and
prints, per hit, three lines (all through `_clean`):

```
1. 0.8312  Fake Show — Fake episode 1a2b3c  [00:01:05]  claim
   The fake episode makes a claim.
   Earmark/Podcasts/<key>.md
```

The third line is omitted when the note path is `None`. `--json` prints
`SearchResults.model_dump_json()`. No hits: `no results`; no index yet: `the search index
is empty; it fills as the worker processes episodes, or run \`earmark reindex\``. Exit codes:
0 on success (also for no results), 2 for `InvalidQuery`, `ConfigError` (unknown model,
missing extra) and `INDEX_BACKEND=none` (`search is turned off
(EARMARK_INDEX_BACKEND=none)`), 1 for `IndexMismatchError` and other `EarmarkError`s.

`earmark reindex` requires `INDEX_BACKEND=lancedb` (otherwise exit 2: `earmark reindex
rebuilds the lancedb index; EARMARK_INDEX_BACKEND is '<name>'`), builds the index with
`allow_download=True` and the note locator, and runs `rebuild.reindex`. Output on stdout:
`reindexed N episodes (M rows)` plus `; skipped K incomplete episodes` when K > 0; warnings
on stderr as `earmark reindex: warning: ...`. Exit 2 for `ConfigError` (including a bad
`--episode` key), 1 for `EarmarkError` (with ` (temporary; try again later)` appended for a
`TransientError`, as `entities rebuild` does) and for `sqlite3`/`OSError`.

`earmark serve`: after the token check and the job store (unchanged order and exit codes),
it calls `factory.build_searcher(settings)`. `None` → `UnavailableSearcher("search is
turned off (EARMARK_INDEX_BACKEND=none)")`. A `ConfigError` → a `WARNING` log line
`search is unavailable: <message>` and `UnavailableSearcher(<first error line>)`. serve
still starts.

## 5. Behaviour and edge cases

### 5.1 Transcript chunks

`build_rows` makes transcript rows first, in segment order:

1. Each segment's text is whitespace-collapsed; empty ones are dropped.
2. Segments are packed greedily into a chunk while the joined text (single spaces) stays
   within `MAX_ROW_CHARS`, `fits(text)` holds, and `segment.end - chunk.start <=
   MAX_CHUNK_SPAN_S`. A segment that fails any of these starts the next chunk.
3. A segment whose own text is longer than `MAX_ROW_CHARS` or does not `fit` is split with
   `analyze.chunking.split_text(text, ok)` where `ok(t)` is `len(t) <= MAX_ROW_CHARS and
   fits(t)`. Each piece is its own chunk with the segment's `start` and `end`; the piece
   before it and the chunk after it are closed. (A remote-ASR transcript without segment
   timestamps is one segment, so it becomes many pieces that all carry `[00:00:00]`; that
   is the truth the transcript holds.)
4. A chunk's `start_s` is its first segment's start, `end_s` the largest end among its
   segments (never below `start_s`).
5. Chunks never overlap and never cut a segment except in rule 3. Joining the rows' texts
   with spaces gives exactly the joined collapsed segment texts (no text lost, none
   repeated). A test checks this on a seeded random transcript.

To bound tokenizer calls, the implementation may pack by characters and time first and
call `fits` once on each finished chunk, splitting a chunk that does not fit at the middle
segment boundary until it fits (rule 3 for a single segment). Either way every row
satisfies `len(text) <= MAX_ROW_CHARS` and `fits(text)`.

### 5.2 Analysis rows

For each analysis in `docs.analyses` order, in this order: `summary`, `claim` (one per
`key_claims` item), `lead` (one per `research_leads` item), `question` (one per
`open_questions` item), `answer` (`prompt_response`, when not `None`). Entities are not
indexed (they have entity notes). Each text is whitespace-collapsed; empty ones are skipped.
A lead's text is `"<text> — <rationale>"`, or just the text when the rationale is blank. Any
text longer than `MAX_ROW_CHARS` or not fitting is split with `split_text` into several
rows of the same kind (consecutive ordinals). Timestamps: `claim` and `lead` rows get
`start_s = timestamp_s`, `end_s = None`; the others have none. Every analysis row carries
the analysis's `prompt_hash` and `prompt` (first 200 characters plus `…` when longer;
`None` for the default prompt).

### 5.3 Ids, ordinals, metadata, and the digest

- `ordinal` counts from 0 per `(kind, prompt_hash)` for analysis rows and per episode for
  transcript rows, after splitting.
- `id` is `"{episode_key}:{kind}:{prompt_hash or '-'}:{ordinal}"`; ids are unique within an
  episode and only need to be.
- Every row carries `show_title`, `episode_title`, `episode_url` (the canonical Apple URL),
  `published_at` (UTC ISO 8601, or `None`), and the note paths: analysis rows the episode
  note, transcript rows the same pair (the hit shows the transcript note when there is one).
- `rows_digest` is the first 32 hex characters of sha256 over the UTF-8 canonical JSON
  (sorted keys, no spaces, `ensure_ascii=False`) of `{"index_version": INDEX_VERSION,
  "model": model, "dim": dim, "rows": [<every IndexRow field as a dict>]}`. It is stored in
  every row of the episode as `source_digest`.
- `build_rows` is deterministic: the same inputs give equal tuples.

### 5.4 Writing (`replace`, `index`, `rebuild_episode`)

`index(episode, transcript, analyses, stored)` checks that every input belongs to
`episode.key` (else `PermanentError("indexer received data for another episode")`), sorts
the analyses by `(created_at, prompt_hash)`, and calls `replace(EpisodeDocs(...),
NotePaths.from_store_result(stored))`.

`replace(docs, notes, force=False)`:

1. Validate the key with `is_episode_key` (else `PermanentError`). If the table exists,
   check its metadata (5.6) **before** loading the model, so a mismatch never triggers a
   download.
2. `rows = build_rows(docs, notes, fits=embedder.fits)` (this loads the model), and the
   digest.
3. Take the index lock (5.7).
4. Connect (creating `db_dir` if needed). If the table is missing, create it with the
   current metadata; otherwise check the metadata again (another process may have reset
   it).
5. Unless `force`, read one `source_digest` of the episode (`search().where("episode_key =
   '<key>'").select(["source_digest"]).limit(1)`). Equal → `ReplaceResult(len(rows),
   skipped=True)`; nothing is embedded or written.
6. `vectors = embedder.embed_documents([r.text for r in rows])`; check every vector has
   `dim` finite floats (else `PermanentError("the embedding model returned vectors of the
   wrong size")`).
7. One `merge_insert` as in 2.3 with the episode filter and a `pa.Table` of the rows.
8. If the new table version is a multiple of `optimize_every`, `optimize()`.
9. Release the lock; return `ReplaceResult(len(rows), skipped=False)`.

`rebuild_episode(key, load, notes)` first calls `self._embedder.fits("")`, which loads
(and, the first time, downloads) the model without the lock held. Then it takes the lock,
calls `load()` inside it (so a worker that writes a new analysis and indexes it cannot be
overwritten by a reindex holding older artifacts), returns `None` when `load()` does, and
otherwise builds the rows and runs steps 4–8 with `force=True`.

`reset()` takes the lock, drops the table if it exists, and creates an empty one with the
current metadata (connecting creates `db_dir`). `optimize()` runs
`table.optimize(cleanup_older_than=CLEANUP_OLDER_THAN)` when the table exists.

### 5.5 Searching

`search(query)`:

1. If `db_dir` is not a directory → `SearchResults(hits=(), index_exists=False)`, without
   connecting (no directory is created). Connect once per `LanceIndex` with
   `read_consistency_interval=timedelta(0)`; reuse the connection.
2. No `chunks` table → `index_exists=False`, no hits.
3. Check the metadata (5.6).
4. `vector = embedder.embed_query(query.text)` (an `EmbedModelMissing` propagates).
5. Filter: `kind IN ('claim', 'lead')` from `query.kinds` and/or `episode_key = '<key>'`
   from `query.episode_key`, joined with `AND`, built only from validated values (`kinds`
   are `RowKind` members, the key matched `[0-9a-f]{20}`); no filter when both are empty.
   Use `prefilter=True` so the limit applies after filtering.
6. `limit(query.limit)`, select every scalar column plus `_distance` (not `vector`).
7. Sort by `(_distance, id)`, number from 1, `score = round(1 - _distance, 4)`,
   `timestamp = format_hms(start_s)` when `start_s` is set, `snippet(text)`, `published_at`
   parsed back to an aware datetime. A row that fails `SearchHit` validation (a hand-edited
   table) is skipped with a `WARNING` log line, not an error.
8. A lock around steps 1–7 serialises searches in one process (the API's thread pool).
   Searches take no file lock.

`snippet(text)`: collapse whitespace; if longer than 300 characters, cut at the last space
at or before character 299 (or at 299 when there is none) and append `…`.

### 5.6 Mismatch detection

A table matches when its metadata has `earmark.index_version == "1"`,
`earmark.embed_model == embedder.model`, `earmark.embed_dim == str(embedder.dim)`, and its
`vector` field is a fixed-size list of that size. Anything else (another value, a missing
key, a table created by something else) raises `IndexMismatchError`:

```
the search index was built with embedding model 'BAAI/bge-small-en-v1.5' (384 dimensions,
index version 1), but EARMARK_EMBED_MODEL is 'BAAI/bge-base-en-v1.5' (768 dimensions);
run `earmark reindex` to rebuild it
```

Values read from the table are cleaned (non-printable characters replaced) and capped at
100 characters; a missing value reads `unknown`. On the worker this becomes the job warning
`index failed: the search index was built with ...`; the job stays `done`. `earmark reindex
--episode KEY` against a mismatched table raises it too, with ` (without --episode)` before
`to rebuild it`.

### 5.7 Locking

One writer at a time across processes: `vault_lock(data_dir / "index.lock",
timeout_s=lock_timeout_s, busy_message="another earmark process is writing the search
index; try again later")`. `reset`, `replace`, and `rebuild_episode` hold it; `search`
does not. The lock is held for one episode at a time, so a reindex and the worker
interleave by episode. A lock timeout is a `TransientError` (a warning on the worker; exit
1 for `reindex`).

### 5.8 Errors from LanceDB

Exceptions raised by lancedb or pyarrow calls are mapped where they are called: an
`OSError` with `ENOSPC`/`EDQUOT` → `TransientError("the search index disk is full")`; any
other exception during a write → `TransientError("search index write failed
(<ExceptionType>)")`; during a read → `TransientError("the search index could not be read
(<ExceptionType>)")`. `IndexMismatchError`, `EmbedModelMissing`, and other `EarmarkError`s
pass through unchanged. No message contains a path, a query, or library text.

### 5.9 Reindex

`reindex(artifacts, target, locate, only=None)`:

- Full (`only is None`): `target.reset()`, then `keys = artifacts.list_episode_keys()`
  (after the reset, so an episode the worker finishes meanwhile is either listed or indexed
  by the worker after the reset), `notes = locate(keys)`, then for each key in order
  `target.rebuild_episode(key, lambda: load_episode_docs(artifacts, key), notes.get(key,
  NotePaths()))`. `None` → skipped (incomplete: no transcript or no analysis). A
  `PermanentError` from loading → warning `episode <key>: unreadable files under
  EARMARK_DATA_DIR; skipped` and skipped. A `TransientError` stops the run and propagates.
  An episode with rows but no note found → warning `episode <key>: no note found in the
  vault; its hits have no note path`. Then `target.optimize()`.
- One episode (`only=key`): no reset; the key must be an episode key (else `ConfigError`
  naming `--episode`) with loadable docs (else `PermanentError("episode <key> has no
  transcript or analyses under EARMARK_DATA_DIR")`); one `rebuild_episode`; then
  `optimize()`.
- The report counts indexed episodes, rows, and skipped episodes, and carries the warnings
  in order without duplicates.
- While a full reindex runs, search returns fewer hits (the table was emptied). That is
  accepted and documented; `rename_table` does not exist in LanceDB OSS (2.3).
- Rows written by `reindex` for an episode equal the rows the worker would write with the
  same note paths (same `build_rows`), so their digests match and a later re-share of that
  episode embeds nothing.

### 5.10 Model downloads

- The worker's indexer and `earmark reindex` may download (`allow_download=True`), into
  `default_model_dir(settings)` = `EMBED_MODEL_DIR` or `<DATA_DIR>/models/embed`, created on
  first load. The first job after installing therefore spends a few seconds downloading
  66 MB; a failure is a job warning, and the next job (or `earmark reindex`) tries again.
- The API and `earmark search` never download (`allow_download=False`): until the worker
  or a reindex has fetched the model, a search on an existing table answers 503 /
  exit 1 with the `EmbedModelMissing` message. A missing table is reported first, without
  the model.
- `HF_HUB_OFFLINE`, `HF_ENDPOINT`, and proxy variables are honoured by `huggingface_hub` as
  usual. `FASTEMBED_CACHE_PATH` is ignored because earmark always passes `cache_dir`.

### 5.11 Startup behaviour

- `earmark worker` with `INDEX_BACKEND=lancedb`: the builder runs inside `build_backends`,
  so a missing extra or an unknown `EMBED_MODEL` is a `ConfigError` before the database is
  opened (exit 2), with `EARMARK_INDEX_BACKEND: backend 'lancedb' needs the optional
  'index' extra, which is not installed; install it with \`uv sync --extra index\`, or set
  EARMARK_INDEX_BACKEND=none`.
- `earmark serve` starts anyway and answers `/search` with 503 and that reason
  (section 8, deviation 2).
- Nothing is created under `DATA_DIR` by building, by a search before the first write, or
  by a failed build.

### 5.12 Security notes

- The query goes only to the embedder; it is never logged (log the hit count and time
  only), never part of a filter string, and never echoed in an error.
- Filter strings are built only from `RowKind` members and regex-checked keys.
- Hits carry untrusted text (transcript and LLM output) as JSON strings; the CLI passes
  every printed value through `_clean`.
- The limit caps work per request (50 rows). The query cap (500 characters) bounds the
  embedding cost; uvicorn's own request-line limit bounds the raw URL.
- Model files come from Hugging Face through `huggingface_hub`, chosen by the operator
  (`EMBED_MODEL`), not by a third party: ONNX graphs and tokenizer JSON, nothing unpickled.
  Like faster-whisper's download, this does not go through `fetch.py`.

## 6. Tests

Fixtures: none to record. Synthetic episodes, transcripts, and analyses come from
`tests/index_helpers.py` (built from `earmark.fakes` and `tests/factories.py`); the vault
for `locate_notes` tests is written by `VaultStore.store` into `tmp_path`. The only
download is the real model in the `slow` test, cached under
`$SLOW_TEST_CACHE/fastembed` (default `~/.cache/earmark-tests/fastembed`). The `index`
tests never touch the network: the marker does not re-enable sockets.

### Brief acceptance criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | Transcript chunks follow segment boundaries and carry start and end times; summary, claims, and leads are their own rows with a `kind` | `test_index_chunks.py::test_chunks_are_runs_of_whole_segments` (each transcript row's text is the space-joined collapsed text of consecutive segments; `start_s` = first start, `end_s` = max end); `test_chunks_cover_every_segment_once` (seeded random transcript of 500 segments: joined row texts equal joined segment texts); `test_chunk_limits` (every row `len <= MAX_ROW_CHARS`, `fits`, span ≤ 120 s unless a single segment); `test_long_segment_split_keeps_times` (one 5 000-character segment → several rows, all with the segment's start and end, text preserved); `test_fits_drives_splitting` (a `fits` that allows only 100 characters yields rows that all satisfy it); `test_analysis_rows_kinds` (summary, claim, lead, question, answer rows with their kinds, `prompt_hash`, claim and lead `start_s`, lead text with rationale; entities absent; blank items skipped); `test_no_answer_without_prompt`; `test_rows_deterministic_and_ids_unique`; `test_note_paths_on_rows`; `test_digest_changes_with_inputs` (text, note path, model, dim each change it). `test_lancedb_index.py::test_rows_in_table_have_kinds_and_times` (index marker: the stored rows of a real table) |
| A2 | Reindexing an episode replaces its rows without duplicates | `test_lancedb_index.py::test_index_twice_no_duplicates` (same inputs twice: row count equals `len(build_rows(...))`, ids unique, second call `skipped` and `FakeEmbedder.documents` unchanged); `test_replace_after_change` (a second analysis added, then a transcript shortened: rows match the new `build_rows` exactly, stale ids gone); `test_other_episodes_untouched`; `test_force_replace_reembeds` |
| A3 | Search returns ranked hits with episode, timestamp, snippet, and note path, from both the CLI and the authenticated endpoint | Default suite: `test_api_search.py::test_search_returns_hits` (`FakeSearcher` hits serialised with every field; the searcher got the normalised `SearchQuery`); `test_search_requires_token` (401, searcher not called); `test_cli.py::test_search_prints_hits` and `test_search_json` (monkeypatched `factory.build_searcher` → `FakeSearcher`). Index marker: `test_lancedb_index.py::test_search_ranks_relevant_rows_first` (three episodes, a query sharing words with one claim: that claim is rank 1, scores descending, `timestamp == "00:01:05"`, `snippet`, `note_path` from the `StoreResult`); `test_api_search_real` (`TestClient(create_app(settings, store=..., searcher=index))` with the token returns those hits; without it 401); `test_cli_search_real` (`earmark search` with `earmark.index.embed.build_embedder` monkeypatched to a `FakeEmbedder` and `EARMARK_DATA_DIR` at the indexed directory prints the hit lines) |
| A4 | Tests run a real LanceDB in a temp directory with a deterministic fake embedder | `tests/test_lancedb_index.py` as a whole (`pytestmark = pytest.mark.index`, `LanceIndex(tmp_path / "lancedb", FakeEmbedder(), lock_path=tmp_path / "index.lock")`); `test_fake_embedder_deterministic` (default suite: same text, same vector; normalised; shared words rank higher); the `index.yml` workflow tests in `test_ci.py` (A7 below) |
| A5 | A table built with a different embedding model or dimension is detected, with an error pointing to `earmark reindex` | `test_lancedb_index.py::test_model_mismatch_on_index` (built with `FakeEmbedder(model="fake-a")`, indexed with `"fake-b"`: `IndexMismatchError`, message names both models and contains `earmark reindex`, and `FakeEmbedder.documents` is empty: no embedding happened); `test_dim_mismatch_on_search` (dim 64 vs 32); `test_index_version_mismatch` (a table whose metadata says version `"0"`); `test_foreign_table_mismatch` (a `chunks` table without earmark metadata); `test_mismatch_is_job_warning` (a `LanceIndex` as `indexer` in `tests/pipeline_env.make_env`: job `done`, warning starts with `index failed: the search index was built with`); default suite: `test_api_search.py::test_mismatch_is_409` and `test_cli.py::test_search_mismatch_exits_1` (FakeSearcher failing with `IndexMismatchError`) |
| A6 | `earmark reindex` rebuilds the index from artifacts | Default suite: `test_index_rebuild.py::test_full_reindex_resets_then_rebuilds_each_episode` (`RecordingTarget`: `reset` first, one `rebuild_episode` per complete episode in key order, `optimize` last; the loader reads the artifacts inside the call); `test_incomplete_and_corrupt_episodes_skipped` (no transcript, no analysis, corrupt `transcript.json`: skipped counts and warnings); `test_missing_note_warns`; `test_only_one_episode` (no reset; bad key → `ConfigError`; missing docs → `PermanentError`); `test_transient_error_stops`; `test_locate_notes_reads_vault` (`VaultStore.store` two episodes, rename one note: paths found, transcript paths too, nothing written: vault tree and mtimes unchanged; a missing subdir returns `{}` and creates nothing). Index marker: `test_lancedb_index.py::test_reindex_rebuilds_from_artifacts` (worker-indexed table, then the table directory deleted: `reindex` restores the same rows and digests); `test_reindex_after_model_change` (built with `fake-a`; reindex with `fake-b` succeeds, metadata now `fake-b`, searches work); `test_cli_reindex_real` (`earmark reindex` prints `reindexed 2 episodes (N rows)`); `test_reindex_episode_against_mismatch` (`--episode` on a mismatched table: exit 1, message says `(without --episode)`) |
| A7 | The default suite passes without the extra installed | `make check` in the repo `.venv` (which has no lancedb or fastembed); `test_index_embed.py::test_missing_extra_is_config_error` and `test_factory.py::test_lancedb_indexer_without_extra` (monkeypatched `importlib.import_module` raising `ModuleNotFoundError(name="lancedb")` / `name="fastembed"`: `ConfigError` naming `EARMARK_INDEX_BACKEND` and `uv sync --extra index`; data dir not created); `test_factory.py::test_pipeline_modules_import_light` and `test_cli.py::test_import_earmark_is_light` with `pyarrow`, `onnxruntime`, `numpy`, `loguru` added to the heavy sets; new `test_index_search.py::test_index_modules_import_light` (subprocess imports `earmark.index.search`, `earmark.index.chunks`, `earmark.index.embed`, `earmark.index.lancedb_index`, `earmark.index.rebuild`, `earmark.fakes`, `earmark.api.routes`: none of `lancedb`, `pyarrow`, `fastembed`, `onnxruntime`, `numpy`, `loguru` loaded); `test_tooling.py::test_live_markers_deselected_by_default` (now five markers deselected); `test_ci.py::test_index_workflow_*` |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | The default suite never downloads a model or opens a socket for the index | `test_index_embed.py::test_build_embedder_constructs_nothing` (stub runtime records `load` calls: none after `build_embedder`; no directory created); `test_embedder_loads_once_lazily` (two `fits` calls, one `load`); `test_search_side_never_downloads` (`allow_download=False` passed to `load`); `test_conftest_sets_hf_offline` (`os.environ["HF_HUB_OFFLINE"] == "1"` in a default test); the `index` tests run with sockets blocked (`test_tooling.py::test_index_marker_keeps_sockets_blocked`: an inner `@pytest.mark.index` test opening an `AF_INET` socket fails under `-m index`) |
| B2 | Re-indexing unchanged inputs does no work | `test_lancedb_index.py::test_index_twice_no_duplicates` (A2) and `test_reshare_embeds_nothing` (through `make_env`: a second job for the same episode leaves `FakeEmbedder.documents` unchanged and the table version unchanged) |
| B3 | `GET /search` validates its parameters without echoing them | `test_api_search.py::test_invalid_params` (parametrized: missing `q`, blank `q`, 501-character `q`, `limit=0`, `limit=51`, `kind=entity`, `episode=XYZ`: 422 with the 4.2 messages or a FastAPI message, the raw value absent from the body, searcher not called); `test_query_normalised` (`q="  fake \n claim "` reaches the searcher as `"fake claim"`; repeated `kind` deduplicated in `ROW_KINDS` order) |
| B4 | Search errors map to the documented statuses | `test_api_search.py::test_unconfigured_is_503` (`create_app` without `searcher`), `test_unavailable_reason_is_503`, `test_transient_is_503_with_retry_after`, `test_mismatch_is_409` |
| B5 | `serve` starts without the extra and reports why search is off | `test_cli.py::test_serve_starts_when_search_unavailable` (default settings, extra missing via monkeypatched `factory.build_searcher` raising the `IndexExtraMissing`: exit 0, uvicorn called, a warning logged); `test_serve_with_index_none` (the app's `/search` answers 503 `search is turned off (EARMARK_INDEX_BACKEND=none)`); the existing `test_serve_runs_uvicorn` keeps passing unchanged |
| B6 | No files appear before the first write | `test_lancedb_index.py::test_search_before_any_write_creates_nothing` (`index_exists=False`, `tmp_path` unchanged); `test_build_creates_nothing` (`build_lancedb_index` with a `FakeEmbedder`: no `lancedb/`, no `index.lock`, no `models/`) |
| B7 | Filters cannot inject | `test_index_search.py::test_parse_query_rejects_bad_kind_and_key`; `test_lancedb_index.py::test_filters` (kind and episode filters return only matching rows, limit applies after the filter) |
| B8 | Writers are serialised | `test_lancedb_index.py::test_lock_timeout_is_transient` (hold `index.lock` with `vault_lock` in the test, `LanceIndex(lock_timeout_s=0.2).index(...)` raises `TransientError` naming the search index); `test_vault_fs.py::test_vault_lock_busy_message` (default suite) |
| B9 | Old versions are cleaned up | `test_lancedb_index.py::test_optimize_every` (`optimize_every=2`: after four writes `optimize` ran and searches still return every row; versions of the dropped table are gone after `reset`) |
| B10 | The real model works | `test_index_slow.py::test_real_model_ranks_semantically` (slow: downloads `BAAI/bge-small-en-v1.5` into `slow_assets.embed_model_dir()`, `dim == 384`; three chunks about the telephone, photosynthesis, and the Roman empire; the query "who invented the telephone" ranks the telephone chunk first with no shared words beyond "telephone"); `test_real_model_fits_detects_overflow` (`fits("word " * 600) is False`, `fits("word " * 100) is True`); `test_real_model_loads_offline_from_cache` (a second embedder with `allow_download=False` and the same dir embeds) |
| B11 | Embedder adapter details | `test_index_embed.py::test_unknown_model_is_config_error` (message names `EARMARK_EMBED_MODEL`, value capped), `test_model_name_canonicalised` (`baai/BGE-small-en-v1.5` → `BAAI/bge-small-en-v1.5`), `test_load_errors_mapped` (`ValueError` with and without download; no library text in the message), `test_fits_uses_max_tokens` (stub `token_count` == `max_tokens` → `False`), `test_default_model_dir` |
| B12 | Settings | `test_config.py::test_defaults` gains `embed_model_dir is None`; `test_embed_model_stripped_and_required`; the existing `.env.example` coverage test covers `EARMARK_EMBED_MODEL_DIR` |

### CI tests (`tests/test_ci.py`)

- `INDEX_WORKFLOW = WORKFLOWS_DIR / "index.yml"`, `INDEX_COMMANDS = ["uv sync --locked
  --extra index", "uv run --locked --extra index pytest -m index -v -rs"]`.
- `test_index_workflow_triggers`: exactly `push` (branches `main`, `impl`) and
  `pull_request`, like `ci.yml`.
- `test_index_workflow_job`: one job `index`, `runs-on: ubuntu-24.04-arm`, integer
  `timeout-minutes` in 1..30, steps `[checkout, setup-uv, run, run]`, run commands equal
  `INDEX_COMMANDS`, pins equal `ci.yml`'s, setup-uv `with` equal to ci's plus
  `cache-suffix: index`, checkout `persist-credentials: false`.
- `test_index_workflow_read_only_no_secrets`: top-level `permissions == {"contents":
  "read"}`, no job-level permissions, no `env` anywhere, no `secrets`.
- `test_index_workflow_concurrency`: same group and `cancel-in-progress: true` as ci.
- `SLOW_COMMANDS` becomes `["uv sync --locked --extra transcribe --extra index", "uv run
  --locked --extra transcribe --extra index pytest -m slow -v -rs"]` and
  `test_slow_job_pins` expects `cache-suffix: slow`.
- The parametrized rules (actionlint, pinning, permissions, no `pull_request_target`) pick
  up the new file automatically; `test_only_llm_workflow_references_secrets` covers it.

### `.github/workflows/index.yml`

```yaml
# The real-LanceDB tests (pytest -m index) need the `index` extra, which the required
# `make check` job does not install. Not a required check (yet); see docs/ARCHITECTURE.md.
name: Index tests

on:
  push:
    branches: [main, impl]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  index:
    name: pytest -m index (linux/arm64)
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 15
    steps:
      - name: Check out
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Install uv
        uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
        with:
          # Same minor as the uv_build bound in pyproject.toml; bump both together.
          version: ">=0.13.9,<0.14"
          enable-cache: true
          cache-dependency-glob: uv.lock
          prune-cache: true
          cache-python: true
          cache-suffix: index
      - name: Install dependencies with the index extra
        run: uv sync --locked --extra index
      - name: Index tests
        run: uv run --locked --extra index pytest -m index -v -rs
```

### Markers and conftest

- `pyproject.toml` markers gain `"index: needs the index extra; real LanceDB in a temp
  directory, no network"`; `addopts` `-m` becomes `"not network and not slow and not llm
  and not gpu and not index"` (keep the NOTE comment accurate).
- `tests/conftest.py`: `index` is **not** added to `_SOCKET_MARKERS`. In
  `_isolate_environment`, for tests without `network`, `slow`, or `llm`: `HF_HUB_OFFLINE=1`
  and `FASTEMBED_CACHE_PATH=<tmp_path_factory.mktemp("fastembed-cache")>` (not under
  `tmp_path`, which some tests assert stays empty).
- `tests/test_lancedb_index.py` fails (does not skip) without the extra: its fixtures call
  `load_lance_runtime()`, which raises `IndexExtraMissing`.

## 7. Verification

Run everything from `/home/sandy/dev/earmark`. Use the session scratchpad
(`$SCRATCH` below, a directory under `/tmp/claude-*/.../scratchpad/`) for every install and
cache; never install the extra into the repo `.venv`.

```bash
# 1. The gate, in the repo .venv (no extras). Must pass.
make check
uv run --locked python -c "import importlib.util as u; print(u.find_spec('lancedb'), u.find_spec('fastembed'))"
# expect: None None

# 2. The lock is consistent and the extra is declared.
uv lock --check
grep -n -A3 '^index' pyproject.toml

# 3. The index tests, in a scratch venv under /tmp (needs ~720 MB free there).
df -h /tmp
export SCRATCH=<session scratchpad>/step14
export UV_CACHE_DIR=$SCRATCH/uv-cache UV_PROJECT_ENVIRONMENT=$SCRATCH/venv-index
uv sync --locked --extra index
uv run --locked --extra index pytest -m index -v -rs
uv run --locked --extra index pytest -q          # the default suite also passes WITH the extra

# 4. The real model (downloads 66 MB into the scratchpad).
SLOW_TEST_CACHE=$SCRATCH/slow uv run --locked --extra index pytest -m slow tests/test_index_slow.py -v -rs
```

Live smoke 1 (repo `.venv`, all fakes, no extra):

```bash
export EARMARK_DATA_DIR=$SCRATCH/smoke1 EARMARK_VAULT_DIR=$SCRATCH/vault1 EARMARK_API_TOKEN=smoke-token-14 EARMARK_PORT=8799
mkdir -p $EARMARK_VAULT_DIR
for b in RESOLVER FETCH ASR STORE INDEX NOTIFY; do export EARMARK_${b}_BACKEND=fake; done
export EARMARK_LLM_PROVIDER=fake
uv run --locked earmark serve &   # then:
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8799/search?q=x'                                    # 401
curl -s -H 'Authorization: Bearer smoke-token-14' 'http://127.0.0.1:8799/search?q=fake+claim'                   # 200 {"hits":[]}
curl -s -H 'Authorization: Bearer smoke-token-14' 'http://127.0.0.1:8799/search?q=%20'                          # 422
# restart serve with EARMARK_INDEX_BACKEND=lancedb (extra absent): it starts, logs "search is unavailable",
# and /search answers 503 naming the index extra. With EARMARK_INDEX_BACKEND=lancedb, `earmark worker --once`
# exits 2 with the same message.
```

Live smoke 2 (scratch venv with the extra, real fastembed and LanceDB; downloads the model
once into `$SCRATCH/models`):

```bash
export UV_CACHE_DIR=$SCRATCH/uv-cache UV_PROJECT_ENVIRONMENT=$SCRATCH/venv-index
export EARMARK_DATA_DIR=$SCRATCH/smoke2 EARMARK_VAULT_DIR=$SCRATCH/vault2 EARMARK_API_TOKEN=smoke-token-14 EARMARK_PORT=8799
export EARMARK_EMBED_MODEL_DIR=$SCRATCH/models EARMARK_INDEX_BACKEND=lancedb
mkdir -p $EARMARK_VAULT_DIR    # other backends fake as above
R="uv run --locked --extra index earmark"
$R submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981'
$R worker --once && $R jobs                      # done, no "index failed" warning
$R search "fake claim"                           # rank 1 is the claim row, with [00:..] and the note path
ls $EARMARK_DATA_DIR/lancedb/chunks.lance/_versions | wc -l    # note the count
$R submit '<same URL>' ; $R worker --once                        # same episode and prompt again
ls $EARMARK_DATA_DIR/lancedb/chunks.lance/_versions | wc -l    # unchanged: nothing re-embedded or written
$R serve &  ; curl -s -H 'Authorization: Bearer smoke-token-14' 'http://127.0.0.1:8799/search?q=fake+claim&kind=claim'
EARMARK_EMBED_MODEL=BAAI/bge-base-en-v1.5 $R search "fake claim"   # exit 1, mismatch, points to `earmark reindex`; no download
$R reindex && $R search "fake claim"             # rebuilt, same hits
ls $EARMARK_DATA_DIR                             # earmark.db  episodes  index.lock  lancedb  (models/ only if no EMBED_MODEL_DIR)
```

Afterwards: stop `serve`, `rm -rf $SCRATCH`, and confirm `du -sh ~/.cache/uv` grew by only
a few MB (the `uv lock`). No `uv cache prune` is needed.

Static review: the verifier reads `.github/workflows/index.yml` (it cannot run here) and
checks that `lancedb`, `pyarrow`, `fastembed`, `numpy`, and `loguru` appear only inside the
two loaders.

Not verifiable in the sandbox: the `index.yml` and `slow` CI runs themselves (they run on
GitHub after push), and a full transcription with huggingface-hub 1.33 (covered by the
weekly `slow` job).

## 8. Deviations

1. **Two extra row kinds.** The brief names summary, claims, and leads. Open questions
   (`question`) and the prompt answer (`answer`) are indexed too: the answer is the reply to
   the user's own question and the most likely thing to search for. Entities are not
   indexed (they have notes).
2. **`earmark serve` does not refuse to start without the extra.** PLAN's convention says
   a backend that cannot be built is a startup error. The worker follows it. The API's main
   job is ingest from the Shortcut, so a missing optional search extra must not stop it:
   serve logs a warning and `/search` answers 503 with the reason. The existing
   `test_serve_runs_uvicorn` (default settings) keeps passing unchanged.
3. **New workflow and slow-job change.** The real-LanceDB tests run under a new `index`
   marker in `.github/workflows/index.yml` on every push and pull request, not in the
   required `make check` job (which installs no extras). It is not a required check; making
   it one is a branch-protection decision for the operator. The weekly `slow` job now
   installs both extras (and its cache suffix becomes `slow`) to run the real-model test.
4. **huggingface-hub moves from 2.4.0 to 1.33.0** in the lock, for the `transcribe` extra
   too (fastembed requires `<2.0`). faster-whisper imports and its hub API is present; a
   real transcription with 1.33 is checked by the weekly `slow` job only.
5. **Small changes outside the index package.** `vault_lock` gains `busy_message`;
   `Vault.open_existing_subdir` and `VaultStore.locate_notes` (read-only) let `reindex`
   find note paths from the vault, the system of record; `analyze.chunking` exports
   `split_text`; `FakeStore.locate_notes`.
6. **New setting `EMBED_MODEL_DIR`** (default `<DATA_DIR>/models/embed`), because
   fastembed's default cache is the system temp directory.
7. **`docs/ARCHITECTURE.md` changes** made with this plan: the Decisions row for the vector
   store; the module layout for `index/`; a new "Indexing and search" section under Stage
   contracts; `locate_notes` under Storing; Data layout (`index.lock`, `lancedb/chunks.lance`,
   `models/embed/`); the `GET /search` row; `EMBED_MODEL` and `EMBED_MODEL_DIR`; Security
   (model downloads, search input); Testing (the `index` marker, `index.yml`, the slow job's
   extras); Known risk 3 updated and risk 10 added.
8. My research scratch directory was deleted after the measurements; nothing was written
   outside `docs/` and the session scratchpad.

## 9. Out of scope

- An ANN vector index (IVF-PQ, HNSW): exhaustive search is fast enough at this scale
  (2.3). Revisit past a few hundred thousand rows.
- Hybrid or full-text (BM25/tantivy) search, reranking, and query prefixes beyond what
  fastembed applies.
- GPU embeddings (fastembed's CUDA provider needs onnxruntime-gpu).
- `earmark doctor` checks for the extra and the model (step 15).
- Indexing on reanalyze and `GET /episodes` (step 17 calls the same indexer).
- Search over entities, digests, or user notes.
- Making `index.yml` a required check, and the container image (step 16).
