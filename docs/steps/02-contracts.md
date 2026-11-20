# Step 02: Core contracts and fakes

## 1. Outcome

The package gains the shared vocabulary every later step codes against:

- `earmark/models.py`: frozen pydantic models for requests, episodes, transcripts,
  analyses, store results, and jobs, plus `episode_key`, `prompt_hash`, `format_hms`,
  the `Clock` alias, and the `Literal` types for sources, job kinds, statuses, and stages.
- `earmark/errors.py`: `EarmarkError` and its three retry-intent subclasses
  `TransientError`, `PermanentError`, `SkipEpisode`.
- `earmark/artifacts.py`: atomic file writes and `ArtifactStore`, which owns every path
  under `$EARMARK_DATA_DIR/episodes/<episode_key>/` and reads and writes the artifacts.
- `earmark/pipeline/stages.py`: one `typing.Protocol` per stage.
- `earmark/fakes.py`: a deterministic, call-counting, failure-injectable fake for every
  stage. The cache-aware fakes really write their artifacts through `ArtifactStore`, so
  step 05 can run the pipeline end to end on them.

Nothing calls these yet. No new dependency, no new setting, no CLI change.

## 2. Findings

All checks run on 2026-11-19 in this sandbox with the locked environment (pydantic
2.14.4, pydantic-core 2.50.5, mypy 2.4.1 with the pydantic plugin, ruff 0.17.6, Python
3.13), using throwaway scripts in the scratchpad. Nothing was installed.

### 2.1 pydantic JSON round-trips

- A frozen model with `AwareDatetime`, `tuple[float, ...]`, `Literal`, and constrained
  `str` fields satisfies `type(m).model_validate_json(m.model_dump_json()) == m`, and
  dumping the re-loaded model gives byte-identical JSON. A UTC datetime dumps as
  `2026-01-01T00:00:00Z`; a `-08:00` offset dumps as `...T03:00:00-08:00` and survives.
  After loading, `tzinfo` is `pydantic_core.TzInfo`, not `datetime.UTC`, but aware
  datetimes compare by instant, so model equality still holds.
- `AwareDatetime` rejects a naive datetime (`timezone_aware`). That matches ruff's DTZ
  rules and keeps every stored time unambiguous.
- `Field(allow_inf_nan=False)` rejects `nan` (`finite_number`). JSON has no NaN, so a
  NaN timestamp would otherwise break the round-trip.
- `ConfigDict(extra="forbid")` rejects unknown keys on `model_validate_json`
  (`extra_forbidden`).
- A frozen model whose fields are all hashable (tuples, not lists) is hashable.
- `600.0` dumps as `600.0` and loads back as a float.

### 2.2 mypy and the pydantic plugin

- With `init_typed = true` (set in step 01), passing a `list` to a `tuple[float, ...]`
  field is a mypy error (`arg-type`), although pydantic would accept it at runtime.
  **Callers must pass tuples to tuple fields.** This is deliberate: it is the price of
  real immutability and hashability. Plans for later steps should say `tuple(...)`.
- mypy checks Protocol compatibility of parameter and return types, but it did **not**
  flag an implementation whose parameter *name* differs from the Protocol's
  (`def fetch(self, ep: Episode)` against `def fetch(self, episode: Episode)`). A caller
  passing that argument by keyword would then fail at runtime. The stage Protocols
  therefore make every parameter positional-only (`/`), so names cannot matter and the
  runner must call stages positionally.
- `mypy.api.run([...])` with `--config-file <repo>/pyproject.toml`, an explicit file
  argument (which overrides `files = ["src", "tests"]`), and a temporary `--cache-dir`
  type-checks a snippet that imports `earmark` from any working directory, in under a
  second cold. That makes a negative-control test (a deliberately wrong fake must fail
  mypy) cheap enough for the default suite.

### 2.3 Regex anchors

`re.match(r"^[0-9a-f]{20}$", "0" * 20 + "\n")` is truthy in Python, because `$` matches
before a trailing newline. pydantic's `StringConstraints(pattern=...)` (Rust regex)
rejects the same string. **Python-side key checks must use `re.fullmatch` without
anchors**, never `re.match` with `$`.

### 2.4 pathlib globbing sees dotfiles

`Path.glob("*.json")` on Python 3.13 matches `.hidden.json`. Temporary files from atomic
writes are therefore named `.<name>.<random>.tmp` (ending in `.tmp`, not `.json`), and
listings filter by an exact filename pattern, not by glob alone.

### 2.5 Golden values

Computed with `hashlib.sha256` in the scratchpad, using exactly the encodings pinned in
section 4:

| Input | Value |
|---|---|
| `episode_key("apple_podcasts", "1200361736", "urn:example:guid-1")` | `3848d011be9e28df3ac6` |
| `prompt_hash(None, "claude-opus-5-5", schema_version=1)` | `c416a435201e0e94` |
| `prompt_hash("What papers are cited?", "claude-opus-5-5", schema_version=1)` | `b26ea955c101e2dc` |
| `prompt_hash("", "claude-opus-5-5", schema_version=1)` | `d986cc9161e3bbc9` |
| `prompt_hash("What papers are cited?", "fake-llm", schema_version=1)` | `9fc6ff2750517634` |
| `prompt_hash("What papers are cited?", "claude-opus-5-5", schema_version=2)` | `c30c4e2ff7a8345b` |

If an implementer's function disagrees, the function is wrong, not the table: the
encodings in 4.1 reproduce these values.

### 2.6 Design findings from reading steps 03–18

These shaped the contracts. None of the behaviour is built now.

- **Stage warnings.** Step 09 (text without segments) and step 10 (out-of-range
  timestamps dropped) must add job warnings, but stages do not see the job. Warnings
  are carried as data: `Transcript.warnings`, `Analysis.warnings`, and
  `StoreResult.warnings`. The runner (step 05) copies them into `job.warnings`. They are
  persisted in the artifacts, so a cache hit reports the same warnings again, which is
  correct: the artifact still has the defect.
- **Audio is deleted after transcription** (step 08) but the runner calls every stage
  (architecture rule 1). On a re-share, `fetch` would download the audio again only for
  `transcribe` to return its cache. The contract now says the runner skips `fetch` and
  `transcribe` and reads the transcript from `ArtifactStore` when `transcript.json`
  exists. This is the one artifact check the runner does itself. Step 05 implements it.
- **"Called" versus "did the work".** Step 05 must show that a second job "calls neither
  the transcriber nor the analyzer". Depending on how the runner short-circuits, a fake
  may be invoked and return its cache. Every fake therefore records each invocation in
  `calls`, and the cache-aware fakes also count cache misses in `computed`. Tests can
  assert on either.
- **Note path.** Step 12 records it in the job result, step 14 returns it in search
  hits, step 15 builds an `obsidian://` link from it. `StoreResult.note_path` is a POSIX
  path relative to `VAULT_DIR` (so it includes `VAULT_SUBDIR`), which is exactly the
  `file=` argument of `obsidian://open`. `Indexer.index` receives the `StoreResult`.
- **Share time.** Note frontmatter has `shared_at` (step 12) and the digest selects by it
  (step 18), so `Store.store` takes `shared_at`. `Job.shared_at` comes from the
  request's `timestamp`, or the enqueue time.
- **Re-analysis** (step 17) needs a job without a URL: `Job.url` is optional, and a model
  validator requires `url` for `ingest` and `episode_key` for `reanalyze`. `GET
  /episodes` and the rebuild commands of steps 13 and 14 need to enumerate episodes and
  their analyses: `ArtifactStore.list_episode_keys()` and `list_analyses(key)`.
- **Entities** (step 13) need name, context, and timestamps per mention; claims and
  research leads (steps 14, 18) need a timestamp each. Item models `Entity`, `Claim`,
  and `ResearchLead` fix those shapes.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/models.py` | New. Models, type aliases, `episode_key`, `prompt_hash`, `format_hms`, `is_episode_key`, `is_prompt_hash`, `utc_now`, constants. |
| `src/earmark/errors.py` | New. `EarmarkError`, `TransientError`, `PermanentError`, `SkipEpisode`. |
| `src/earmark/artifacts.py` | New. `atomic_writer`, `atomic_write_bytes`, `atomic_write_text`, `ArtifactStore`. |
| `src/earmark/pipeline/__init__.py` | New. Docstring only. |
| `src/earmark/pipeline/stages.py` | New. The seven stage Protocols. |
| `src/earmark/fakes.py` | New. `FakeStage`, call records, seven fakes, `FakeBackends`, `make_fake_backends`, `FAKE_NOW`, `fixed_clock`. |
| `tests/test_models.py` | New. |
| `tests/test_errors.py` | New. |
| `tests/test_artifacts.py` | New. |
| `tests/test_stages.py` | New. Typed assignments and the mypy negative control. |
| `tests/test_fakes.py` | New. |
| `tests/factories.py` | New. Sample-model builders shared by the test modules (plain functions, not fixtures, so they are typed). |
| `docs/ARCHITECTURE.md` | Domain models, episode key and prompt hash, stage contracts, data layout, and jobs sections updated (section 8). |

No change to `pyproject.toml`, `uv.lock`, `config.py`, `cli.py`, or `.env.example`.

## 4. Interfaces

Snippets show signatures and shapes. Bodies are elided with `...`. Docstrings are the
implementer's to write.

### 4.1 `earmark.models`

Imports only the standard library and pydantic. Every model inherits one base:

```python
class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
```

Rules for every model in this module:

- Sequences are `tuple[X, ...]` with default `()`. No `list` or `dict` fields.
- Datetimes are `AwareDatetime`.
- Every float is finite (`allow_inf_nan=False`); times and durations are also `ge=0`.
- Fields appear in the order listed below; JSON dumps keep that order.

#### Aliases and constants

```python
Source = Literal["apple_podcasts"]
JobKind = Literal["ingest", "reanalyze"]
JobStatus = Literal["queued", "running", "done", "failed", "skipped"]
Stage = Literal["resolve", "fetch", "transcribe", "analyze", "store", "index", "notify"]
Clock = Callable[[], datetime]

TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset({"done", "failed", "skipped"})
ANALYSIS_SCHEMA_VERSION: Final = 1
EPISODE_KEY_LENGTH: Final = 20
PROMPT_HASH_LENGTH: Final = 16

EpisodeKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{20}$")]
PromptHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
JobId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
```

Use plain assignments for the aliases, as `config.py` does, not the `type` statement:
pydantic must see them as ordinary annotations. `Stage` lists the pipeline stage names
for `Job.stage`; it is unrelated to `config.py`'s backend literals.

#### Functions

```python
def utc_now() -> datetime: ...  # datetime.now(UTC); the default Clock


def episode_key(source: str, show_id: str, guid: str) -> str: ...


def prompt_hash(
    prompt: str | None, model: str, schema_version: int = ANALYSIS_SCHEMA_VERSION
) -> str: ...


def is_episode_key(value: str) -> bool: ...  # re.fullmatch("[0-9a-f]{20}", value)


def is_prompt_hash(value: str) -> bool: ...  # re.fullmatch("[0-9a-f]{16}", value)


def format_hms(seconds: float) -> str: ...  # "HH:MM:SS", hours unbounded
```

- `episode_key`: `hashlib.sha256(f"{source}:{show_id}:{guid}".encode("utf-8"))
  .hexdigest()[:20]`. No normalisation of the parts (callers strip whitespace from a
  feed GUID before calling). Raises `ValueError` if any part is empty, or if `source` or
  `show_id` contains `:` (which would make the joined string ambiguous). A GUID may
  contain colons (`urn:...`); it is last, so that is unambiguous.
- `prompt_hash`: sha256 of the UTF-8 bytes of
  `json.dumps({"model": model, "prompt": prompt, "schema_version": schema_version},
  sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, first 16 hex characters.
  `None` (the default prompt) and `""` hash differently. No stripping or Unicode
  normalisation: step 04 normalises blank prompts to `None` before they get here.
  Raises `ValueError` if `model` is empty or `schema_version < 1`.
- `format_hms`: `s = int(seconds)` (floor, since seconds are non-negative), then
  `f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"`. `36000.4` gives `10:00:00`,
  `360000` gives `100:00:00`, `3725.9` gives `01:02:05`. Raises `ValueError` for a
  negative or non-finite value.

#### Models

```python
class IngestRequest(Model):
    url: str
    prompt: str | None = None
    timestamp: AwareDatetime | None = None


class EpisodeRef(Model):
    source: Source
    show_id: str
    episode_id: str
    country: str
    url: str


class Episode(Model):
    key: EpisodeKey
    source: Source
    show_id: str
    episode_id: str
    guid: str
    title: str
    show_title: str
    published_at: AwareDatetime | None
    duration_s: Seconds | None
    audio_url: str
    feed_url: str | None
    url: str
    description: str = ""


class Segment(Model):
    start: Seconds
    end: Seconds
    text: str
    speaker: str | None = None


class Transcript(Model):
    episode_key: EpisodeKey
    language: str | None
    duration_s: Seconds
    model: str
    segments: tuple[Segment, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_text(self) -> str: ...


class Claim(Model):
    text: str
    timestamp_s: Seconds | None = None


class Entity(Model):
    name: str
    context: str = ""
    timestamps_s: tuple[Seconds, ...] = ()


class ResearchLead(Model):
    text: str
    rationale: str = ""
    timestamp_s: Seconds | None = None


class Analysis(Model):
    episode_key: EpisodeKey
    prompt: str | None
    prompt_hash: PromptHash
    model: str
    schema_version: int
    created_at: AwareDatetime
    summary: str
    key_claims: tuple[Claim, ...] = ()
    people: tuple[Entity, ...] = ()
    papers: tuple[Entity, ...] = ()
    companies: tuple[Entity, ...] = ()
    tools: tuple[Entity, ...] = ()
    topics: tuple[Entity, ...] = ()
    open_questions: tuple[str, ...] = ()
    research_leads: tuple[ResearchLead, ...] = ()
    prompt_response: str | None = None
    warnings: tuple[str, ...] = ()


class StoreResult(Model):
    note_path: str
    transcript_note_path: str | None = None
    warnings: tuple[str, ...] = ()


class JobResult(Model):
    note_path: str | None = None
    transcript_note_path: str | None = None
    analysis_hash: PromptHash | None = None


class Job(Model):
    id: JobId
    kind: JobKind
    status: JobStatus
    stage: Stage | None = None
    url: str | None
    prompt: str | None
    episode_key: EpisodeKey | None = None
    attempts: int = Field(0, ge=0)
    run_after: AwareDatetime
    error: str | None = None
    warnings: tuple[str, ...] = ()
    result: JobResult | None = None
    shared_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    heartbeat_at: AwareDatetime | None = None
```

Field meanings and validators:

- `IngestRequest`: the `POST /ingest` body. This step adds no URL or prompt validation;
  step 04 adds validators (Apple host, blank prompt to `None`, length caps) on this model
  or a request model that produces it. `timestamp` is the share time the Shortcut sends.
- `EpisodeRef`: what `parse_apple_url` returns (step 04). `country` is the lower-case
  storefront code from the URL path; step 04 picks the default when the URL has none.
- `Episode`:
  - `show_id` and `episode_id` are strings even though Apple's are numeric.
    `show_id` has `min_length=1` and must not contain `:`; `guid` has `min_length=1`.
  - Model validator (`mode="after"`): `key == episode_key(source, show_id, guid)`,
    otherwise `ValueError("key does not match source, show_id and guid")`. Build an
    episode by computing the key first; reading a tampered `episode.json` fails.
  - `published_at` and `duration_s` are optional because feeds omit them; step 07 fills
    them from the iTunes lookup when it can, and step 12 decides the note-date fallback.
  - `feed_url` is optional so non-RSS sources (backlog) fit; the Apple resolver always
    sets it.
  - `url` is the URL the user shared. `audio_url` is the enclosure (or the lookup's
    `episodeUrl` fallback).
- `Segment`: model validator `end >= start`. `speaker` is for future diarization.
- `Transcript`:
  - `segments` in time order; this step does not enforce monotonic starts (step 08's
    concern, since ASR output can overlap slightly).
  - `to_text()`: one line per segment, `f"[{format_hms(seg.start)}] {text}"` where
    `text = " ".join(seg.text.split())` (all internal whitespace, including newlines,
    collapsed to single spaces, so one segment is always one line). When the collapsed
    text is empty the line is just `[HH:MM:SS]` with no trailing space. Lines are joined
    with `"\n"` and the result ends with `"\n"`. No segments gives `""`. This is the
    exact content of `transcript.txt`.
- `Claim`, `Entity`, `ResearchLead`: the analysis items. `Entity` is used for all five
  entity kinds (people, papers, companies, tools, topics); the list it sits in is its
  kind. `name` has `min_length=1`. `timestamps_s` are the seconds where the entity is
  discussed. `ResearchLead.rationale` says why the lead is worth following.
- `Analysis`:
  - `prompt` is the user's prompt, `None` for the default analysis.
  - `model` is the model id that produced it (`Settings.llm_model`, or the fake's).
  - `schema_version` is the `ANALYSIS_SCHEMA_VERSION` the result was produced under,
    `ge=1`.
  - Model validator: `self.prompt_hash == prompt_hash(self.prompt, self.model,
    self.schema_version)`. Inside the validator method, the bare name `prompt_hash`
    resolves to the module-level function (method bodies do not see the class
    namespace, and an annotation without a value binds nothing there), so no alias is
    needed.
  - `created_at` comes from the analyzer's injected clock and is stored, so re-storing a
    cached analysis is byte-stable.
  - `prompt_response` is the answer to the user's prompt, `None` for the default.
  - `warnings`: problems the analyzer corrected, such as dropped out-of-range
    timestamps (step 10).
- `StoreResult`:
  - `note_path` and `transcript_note_path` are POSIX paths relative to `VAULT_DIR`, for
    example `Earmark/Podcasts/Show/2026-11-19 Title.md`. Validator: not empty, not
    absolute, no backslash, no `..` part (checked with `PurePosixPath`).
  - `warnings`: non-fatal problems while storing.
- `JobResult`: what a finished job reports. `analysis_hash` is the prompt hash of the
  analysis this job produced or reused. Same path validator as `StoreResult` for
  non-`None` paths. Factor the path check into one private function.
- `Job`:
  - `id` is `uuid.uuid4().hex`, generated by `JobStore` (step 03). Unguessable, so
    `GET /jobs/{id}` does not invite enumeration.
  - `stage` is the stage currently or last running, for observability only.
  - `error` holds the failure message for `failed` and the reason for `skipped`.
  - `shared_at` is `IngestRequest.timestamp` if given, else the enqueue time.
  - `attempts` counts claims (step 03 defines exactly when it increments).
  - Model validator: `kind == "ingest"` requires `url`; `kind == "reanalyze"` requires
    `episode_key`.

### 4.2 `earmark.errors`

```python
class EarmarkError(Exception):
    def __init__(self, message: str) -> None: ...

    message: str


class TransientError(EarmarkError):
    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None: ...

    retry_after_s: float | None


class PermanentError(EarmarkError): ...


class SkipEpisode(EarmarkError):
    def __init__(self, reason: str) -> None: ...

    reason: str
```

- `str(exc) == exc.message`. The message is shown to the user (job error, notification)
  and stored in SQLite, so it must be short, human-readable, and free of secrets and of
  untrusted bulk text (no transcript excerpts, no response bodies). Raisers chain the
  cause with `raise ... from exc` so the traceback is still logged.
- `TransientError.retry_after_s`: a server-provided delay (for example HTTP
  `Retry-After` on a 429). `None` means use the job store's backoff. Must be finite and
  `>= 0` if given, otherwise `ValueError`.
- `SkipEpisode.reason` equals `message`.
- `TransientError`, `PermanentError`, and `SkipEpisode` are siblings: none is a subclass
  of another, so `except PermanentError` never catches a skip. Later distinct errors
  subclass one of them (step 10's `NeedsChunking` decides its own parent).
- `ConfigError` stays in `config.py` and is not an `EarmarkError`: it is a startup
  error, not a job outcome.

### 4.3 `earmark.artifacts`

```python
@contextmanager
def atomic_writer(
    path: Path, *, mode: int = 0o644, temp_suffix: str = ".tmp"
) -> Iterator[BinaryIO]: ...


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None: ...


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None: ...


class ArtifactStore:
    def __init__(self, data_dir: Path) -> None: ...

    @property
    def root(self) -> Path: ...  # data_dir / "episodes"

    def episode_dir(self, key: str) -> Path: ...
    def episode_path(self, key: str) -> Path: ...  # episode.json
    def transcript_json_path(self, key: str) -> Path: ...
    def transcript_text_path(self, key: str) -> Path: ...
    def analyses_dir(self, key: str) -> Path: ...
    def analysis_path(self, key: str, prompt_hash: str) -> Path: ...
    def audio_path(self, key: str, ext: str) -> Path: ...
    def find_audio(self, key: str) -> Path | None: ...
    def delete_audio(self, key: str) -> bool: ...

    def write_episode(self, episode: Episode) -> Path: ...
    def read_episode(self, key: str) -> Episode | None: ...
    def write_transcript(self, transcript: Transcript) -> Path: ...
    def read_transcript(self, key: str) -> Transcript | None: ...
    def has_transcript(self, key: str) -> bool: ...
    def write_analysis(self, analysis: Analysis) -> Path: ...
    def read_analysis(self, key: str, prompt_hash: str) -> Analysis | None: ...
    def list_analyses(self, key: str) -> list[Analysis]: ...
    def list_episode_keys(self) -> list[str]: ...
```

`atomic_writer`:

1. `path.parent.mkdir(parents=True, exist_ok=True)`.
2. `tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=temp_suffix)`;
   wrap the descriptor with `os.fdopen(fd, "wb")`.
3. Yield the file. When the body exits normally: `flush()`, `os.fsync()`,
   `os.fchmod(fd, mode)`, close, `os.replace(temp, path)`, then fsync the directory
   (open it `O_RDONLY`, fsync, close; ignore `OSError` from this last step only).
4. If anything raises (the body, flush, fsync, chmod, replace): close the file if still
   open, unlink the temp file (ignore `FileNotFoundError`), and re-raise the original
   exception. The final path is never opened for writing, so it either keeps its old
   content or does not exist.

`mode` defaults to `0o644` because `mkstemp` creates `0o600` files, and step 12 reuses
this writer for vault notes that other tools (Obsidian, sync clients, a host user other
than the container's) must read. `temp_suffix` lets step 06 name its download temp file
`.part` as its brief asks. `atomic_write_bytes` writes `data` inside `atomic_writer`;
`atomic_write_text` encodes `text` as UTF-8 and calls `atomic_write_bytes` (no newline
translation).

`ArtifactStore`:

- The constructor stores `data_dir` and creates nothing. Writes create directories.
- Every method taking `key` first checks `is_episode_key(key)` and raises
  `ValueError(f"not an episode key: {key!r}")` otherwise. `analysis_path` and
  `read_analysis` also check `is_prompt_hash(prompt_hash)`. `audio_path` requires `ext`
  to match `re.fullmatch("[a-z0-9]{1,5}", ext)` (lower-case, no dot).
- Layout, exactly as the architecture's data layout:
  `root/<key>/episode.json`, `transcript.json`, `transcript.txt`, `audio.<ext>`,
  `analyses/<prompt_hash>.json`.
- JSON artifacts are written as `model.model_dump_json(indent=2) + "\n"` through
  `atomic_write_text`. Each `write_*` returns the path it wrote (for a transcript, the
  JSON path).
- `write_transcript` writes `transcript.txt` (`transcript.to_text()`) first and
  `transcript.json` second, so the existence of the JSON (the cache marker) implies the
  text file exists.
- `read_*` return `None` when the file does not exist. A file that exists but does not
  parse or validate raises `PermanentError(f"corrupt artifact: {path}")` chained from the
  pydantic error. The message must not include the file's contents (pydantic's error
  string includes input values). A loaded episode or transcript whose `key` /
  `episode_key` differs from the directory, or an analysis whose `episode_key` or
  `prompt_hash` differs from its path, raises the same `PermanentError`.
- `has_transcript(key)` is `transcript_json_path(key).is_file()`.
- `find_audio(key)` returns the one file in the episode directory whose name fully
  matches `audio\.[a-z0-9]{1,5}`, or `None`. Temp files (`.audio.mp3.xxxx.part`) and
  anything else do not match. If several match, return the first in sorted order.
- `delete_audio(key)` deletes every matching audio file and returns whether it deleted
  any. Step 08 calls it after a successful transcription.
- `list_analyses(key)` loads every file in `analyses/` whose name fully matches
  `[0-9a-f]{16}\.json`, sorted by `(created_at, prompt_hash)` ascending. Missing
  directory gives `[]`. Order is deterministic so step 12 can sort newest first.
- `list_episode_keys()` returns, sorted, the names of directories directly under
  `root` that are valid keys and contain `episode.json`. Missing `root` gives `[]`.

### 4.4 `earmark.pipeline.stages`

```python
class Resolver(Protocol):
    def resolve(self, url: str, /) -> Episode: ...


class AudioFetcher(Protocol):
    def fetch(self, episode: Episode, /) -> Path: ...


class Transcriber(Protocol):
    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript: ...


class Analyzer(Protocol):
    def analyze(
        self, episode: Episode, transcript: Transcript, prompt: str | None, /
    ) -> Analysis: ...


class Store(Protocol):
    def store(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
        /,
    ) -> StoreResult: ...


class Indexer(Protocol):
    def index(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        stored: StoreResult,
        /,
    ) -> None: ...


class Notifier(Protocol):
    def notify(self, job: Job, episode: Episode | None, /) -> None: ...
```

Each Protocol's docstring states its contract, which backends follow:

- `Resolver.resolve`: turns a shared URL into an `Episode`. Not cached (it must ask the
  network what the GUID is). Raises `SkipEpisode` when there is no public feed or the
  episode is subscriber-only.
- `AudioFetcher.fetch`: returns the local audio path under
  `ArtifactStore.audio_path(episode.key, ext)`. If `find_audio` already finds a file,
  returns it without a request.
- `Transcriber.transcribe`: returns the cached transcript if `transcript.json` exists
  (without loading a model); otherwise transcribes `audio` and writes both transcript
  files through `ArtifactStore.write_transcript`.
- `Analyzer.analyze`: returns the cached analysis at
  `analysis_path(key, prompt_hash(prompt, model))` if present, without an LLM call;
  otherwise analyzes and writes it. `prompt` is already normalised (`None` = default).
- `Store.store`: writes the notes for the episode from all its analyses (the runner
  passes `ArtifactStore.list_analyses(key)`), deterministically, and returns where.
  `shared_at` is the episode's share time for the frontmatter.
- `Indexer.index`: replaces the episode's rows in the search index. Failures are
  recorded as job warnings by the runner, never fail the job.
- `Notifier.notify`: called with the job in its terminal state (`done`, `failed`, or
  `skipped`), and the episode if resolution got that far. Must not raise for delivery
  problems; the runner also catches anything it does raise and records a warning.

Parameters are positional-only (section 2.2). The runner calls them positionally.
Implementations may use any parameter names. No `@runtime_checkable`: the factory is
statically typed and the decorator only checks attribute presence.

### 4.5 `earmark.fakes`

Imports only `earmark.models`, `earmark.errors`, `earmark.artifacts`, and the standard
library. It does not need `earmark.pipeline.stages`: conformance is structural and is
proven in `tests/test_stages.py`. Each fake's `__init__` calls `super().__init__()`,
which sets `calls = []` and the empty failure queue.

```python
FAKE_NOW: Final = datetime(2026, 1, 1, tzinfo=UTC)


def fixed_clock(at: datetime = FAKE_NOW) -> Clock: ...


class FakeStage[C]:
    calls: list[C]

    def fail_next(self, exc: Exception, times: int = 1) -> None: ...

    @property
    def call_count(self) -> int: ...


@dataclass(frozen=True)
class ResolveCall:
    url: str


@dataclass(frozen=True)
class FetchCall:
    episode: Episode


@dataclass(frozen=True)
class TranscribeCall:
    episode: Episode
    audio: Path


@dataclass(frozen=True)
class AnalyzeCall:
    episode: Episode
    transcript: Transcript
    prompt: str | None


@dataclass(frozen=True)
class StoreCall:
    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]
    shared_at: datetime


@dataclass(frozen=True)
class IndexCall:
    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]
    stored: StoreResult


@dataclass(frozen=True)
class NotifyCall:
    job: Job
    episode: Episode | None


class FakeResolver(FakeStage[ResolveCall]):
    def __init__(self, episodes: Mapping[str, Episode] | None = None) -> None: ...
    def resolve(self, url: str) -> Episode: ...


class FakeAudioFetcher(FakeStage[FetchCall]):
    def __init__(self, artifacts: ArtifactStore, payload: bytes = b"fake audio\n") -> None: ...
    def fetch(self, episode: Episode) -> Path: ...

    computed: int


class FakeTranscriber(FakeStage[TranscribeCall]):
    def __init__(
        self,
        artifacts: ArtifactStore,
        segments: Sequence[Segment] | None = None,
        *,
        model: str = "fake-asr",
        language: str = "en",
        warnings: Sequence[str] = (),
    ) -> None: ...
    def transcribe(self, episode: Episode, audio: Path) -> Transcript: ...

    computed: int


class FakeAnalyzer(FakeStage[AnalyzeCall]):
    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        model: str = "fake-llm",
        clock: Clock = ...,  # fixed_clock()
        warnings: Sequence[str] = (),
    ) -> None: ...
    def analyze(self, episode: Episode, transcript: Transcript, prompt: str | None) -> Analysis: ...

    computed: int


class FakeStore(FakeStage[StoreCall]):
    def __init__(self, vault_dir: Path | None = None, subdir: str = "Earmark") -> None: ...
    def store(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
    ) -> StoreResult: ...


class FakeIndexer(FakeStage[IndexCall]):
    def index(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        stored: StoreResult,
    ) -> None: ...


class FakeNotifier(FakeStage[NotifyCall]):
    def notify(self, job: Job, episode: Episode | None) -> None: ...


@dataclass
class FakeBackends:
    artifacts: ArtifactStore
    resolver: FakeResolver
    fetcher: FakeAudioFetcher
    transcriber: FakeTranscriber
    analyzer: FakeAnalyzer
    store: FakeStore
    indexer: FakeIndexer
    notifier: FakeNotifier


def make_fake_backends(
    data_dir: Path,
    *,
    vault_dir: Path | None = None,
    subdir: str = "Earmark",
    clock: Clock = ...,  # fixed_clock()
) -> FakeBackends: ...
```

The `...` defaults above stand for `fixed_clock()`; write the real default (a
module-level `_DEFAULT_CLOCK = fixed_clock()` is fine, since the clock is a pure
function). `FakeStage[C]` uses the Python 3.12+ generic class syntax; mypy 2.4 supports
it. Call records are stdlib frozen dataclasses (not pydantic), because they only exist
in memory.

Common behaviour (`FakeStage`):

- Every stage method first appends its call record to `calls`, then raises the next
  queued exception if any, then does its work. A call that raised is still counted.
- `fail_next(exc, times)` queues `exc` `times` times (`times >= 1`, else `ValueError`).
  Queued exceptions are raised in FIFO order, one per call; once the queue is empty the
  fake behaves normally. This lets step 05 test "transient once, then success".
- `computed` (cache-aware fakes only) counts calls that did the work, as opposed to
  returning a cached artifact. Calls that raised an injected error do not count.

Per fake:

- `FakeResolver`: returns `episodes[url]` when the mapping has `url`. Otherwise it
  synthesizes an episode from the URL, deterministically, with
  `digest = sha256(url.encode()).hexdigest()[:12]`:
  `source="apple_podcasts"`, `show_id="1000000001"`, `episode_id=digest`,
  `guid=f"urn:earmark-fake:{digest}"`, `title=f"Fake episode {digest[:6]}"`,
  `show_title="Fake Show"`, `published_at=FAKE_NOW`, `duration_s=900.0`,
  `audio_url=f"https://fake.invalid/audio/{digest}.mp3"`,
  `feed_url="https://fake.invalid/feed.xml"`, `url=url`,
  `description="A synthetic episode for tests."`, and the matching `key`. The `.invalid`
  TLD (RFC 2606) can never resolve. Same URL, same episode, in any process.
- `FakeAudioFetcher`: if `artifacts.find_audio(episode.key)` returns a path, return it.
  Otherwise write `payload` to `artifacts.audio_path(episode.key, "mp3")` with
  `atomic_write_bytes`, increment `computed`, and return the path. No network.
- `FakeTranscriber`: if `artifacts.read_transcript(episode.key)` returns one, return it.
  Otherwise, if `audio` is not an existing file, raise
  `PermanentError(f"audio file missing: {audio.name}")`. Otherwise build the transcript,
  write it with `artifacts.write_transcript`, increment `computed`, and return it.
  Default segments, when none were given (pydantic models take keyword arguments only):
  `Segment(start=0.0, end=5.0, text=f"Welcome to {episode.show_title}.")`,
  `Segment(start=5.0, end=10.0, text=f"Today: {episode.title}.")`,
  `Segment(start=10.0, end=15.0, text="Thanks for listening.")`.
  `duration_s = max(episode.duration_s or 0.0, last segment end, 0.0)`.
  It does not delete the audio (that is step 08's real backend behaviour).
- `FakeAnalyzer`: computes `h = prompt_hash(prompt, self.model)`; if
  `artifacts.read_analysis(episode.key, h)` returns one, return it. Otherwise build the
  analysis below, write it, increment `computed`, return it. With
  `t0 = transcript.segments[0].start if transcript.segments else None` and
  `ts = (t0,) if t0 is not None else ()`:
  - `summary=f"Fake summary of {episode.title}."`
  - `key_claims=(Claim(text="The fake episode makes a claim.", timestamp_s=t0),)`
  - `people=(Entity(name="Ada Lovelace", context="Mentioned as an example.", timestamps_s=ts),)`
  - `papers` one entity `"On Computable Numbers"`, `companies` one `"Acme Corp"`,
    `tools` one `"Fake Tool"`, `topics` one `"Testing"`, each with
    `context="Mentioned as an example."` and `timestamps_s=ts`.
  - `open_questions=("What did the fake episode leave out?",)`
  - `research_leads=(ResearchLead(text="Follow up on the fake claim.", rationale="It is a test.", timestamp_s=t0),)`
  - `prompt_response=f"Fake response to: {prompt}" if prompt is not None else None`
  - `created_at=self.clock()`, `schema_version=ANALYSIS_SCHEMA_VERSION`,
    `model=self.model`, `warnings=tuple(self.warnings)`.
- `FakeStore`: returns
  `StoreResult(note_path=f"{subdir}/Podcasts/{episode.key}.md", transcript_note_path=None)`.
  The filename uses the key, never the title, so the fake needs no sanitizer. If
  `vault_dir` is set it also writes that note under `vault_dir` with
  `atomic_write_text`: `# {title}` line, then for each analysis sorted newest first by
  `(created_at, prompt_hash)` descending, a `## {prompt or "Default analysis"}` heading
  and the summary. Content depends only on the inputs, so storing twice yields the same
  bytes. `subdir` is checked like `StoreResult.note_path` (relative, no `..`).
- `FakeIndexer`: records the call and returns `None`. It keeps
  `rows: dict[str, int]` mapping episode key to `1 + len(analyses)` so tests can check
  that a re-index replaced rather than appended (assignment, not addition).
- `FakeNotifier`: records the call and returns `None`.
- `make_fake_backends(data_dir, ...)` builds one `ArtifactStore(data_dir)` and every
  fake around it, sharing the clock. Step 05's factory and tests use it.

## 5. Behaviour and edge cases

1. **Frozen, forbid-extra models.** Assigning to a field raises `ValidationError`;
   updates use `model_copy(update=...)`, which does not re-validate, so step 03 should
   prefer building a new `Job(...)` from a row. An unknown key in JSON is rejected,
   which protects artifacts from silent typos. Adding a field later needs a default if
   old artifacts must still load. For `IngestRequest` this means an unknown key in the
   `POST /ingest` body is a 422; step 04 owns that choice and may relax it on its
   request model.
2. **Tuples, not lists** (section 2.2). `Transcript(segments=tuple(segs))`.
3. **Aware datetimes everywhere.** A naive datetime fails validation. `utc_now()` is the
   only place `datetime.now` is called in these modules.
4. **`episode_key` inputs.** Not normalised. `episode_key("apple_podcasts", "1", " g ")`
   differs from `"g"`; the resolver strips. Empty parts and `:` in `source` or `show_id`
   raise `ValueError`.
5. **Key checks use `re.fullmatch`** (section 2.3). `"0" * 20 + "\n"` is not a key.
   Upper-case hex is not a key: keys are produced by `hexdigest()`, which is lower-case,
   so accepting upper case would let two spellings name one directory.
6. **`prompt_hash` and `None`.** `None` and `""` differ; the default prompt is `None`,
   never the default prompt's text, so editing the default prompt in step 10 does not
   change the hash. Step 10 must bump `ANALYSIS_SCHEMA_VERSION` when it changes the
   standing system prompt, the default prompt, or the output schema in a way that
   should invalidate cached analyses.
7. **`to_text()`.** Floor, not round (`59.9` is `00:00:59`); hours are not wrapped at
   24; whitespace collapsed; empty text gives a bare marker; trailing newline only when
   there are segments.
8. **Atomicity.** The final path is only ever touched by `os.replace`. A failure leaves
   the previous content (or nothing) and no temp file. Writes across two files
   (`write_transcript`) are not atomic as a pair; the ordering in 4.3 makes the cache
   check safe anyway.
9. **Corrupt artifacts are permanent errors**, not cache misses: silently recomputing
   would hide a bug and, for analyses, spend money. The error message names the path
   only.
10. **ArtifactStore never follows a key or hash it did not validate**, so no caller can
    build a path outside `root` through it. It does not defend against a symlink placed
    inside `data_dir` by someone with write access there; that directory is trusted.
11. **Fakes are deterministic.** No randomness, no wall clock, no environment. Two fresh
    sets of fakes given the same inputs in different directories return equal models
    and write identical bytes.
12. **Fakes record before failing**, so `call_count` counts attempts, including failed
    ones; `computed` counts real work only.
13. **Protocol parameters are positional-only.** A step that calls a stage by keyword
    gets a mypy error, which is the point.
14. **No heavy imports.** `earmark.models`, `errors`, `artifacts`, `pipeline.stages`, and
    `fakes` import nothing outside the standard library and pydantic. `import earmark`
    still imports none of them.

## 6. Tests

Shared builders live in `tests/factories.py` as plain typed functions. Do not use
`**overrides: Any` forwarded into a model constructor; under `init_typed` that defeats
the type check. Give each builder explicit keyword parameters for the fields tests vary,
for example:

```python
def make_episode(
    *,
    guid: str = "urn:example:guid-1",
    title: str = "Épisode 1: «Tests»",
    duration_s: float | None = 3600.0,
) -> Episode: ...
```

Provide `make_episode`, `make_transcript`, `make_analysis`, and `make_job`, each
returning a fully valid instance with every optional field populated (non-ASCII text,
at least one datetime with a `-08:00` offset). Tests needing other variations use
`model_copy(update=...)` only where re-validation does not matter, or construct the
model directly. No fixture files are needed; everything is built in code. Tests that
write files use `tmp_path`. `tests/factories.py` is a module, not a test file (its name
does not start with `test_`), and test modules import it as `from tests.factories
import ...` (`tests/` is a package in step 01).

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | Every model round-trips through JSON unchanged | `test_models.py::test_json_round_trip`, parametrized over a fully populated and a minimal (defaults only) instance of each of `IngestRequest`, `EpisodeRef`, `Episode`, `Segment`, `Transcript`, `Claim`, `Entity`, `ResearchLead`, `Analysis`, `StoreResult`, `JobResult`, `Job`: `type(m).model_validate_json(m.model_dump_json()) == m` and the re-dump is byte-identical. `test_models.py::test_round_trip_covers_every_model`: every `Model` subclass defined in `earmark.models` (found via `vars(earmark.models)`) appears in the parametrization, so a new model cannot skip the test. `test_models.py::test_round_trip_preserves_offset` (a `-08:00` datetime keeps its offset in the JSON) |
| A2 | `episode_key` is deterministic, checked against a golden value | `test_models.py::test_episode_key_golden` (`episode_key("apple_podcasts", "1200361736", "urn:example:guid-1") == "3848d011be9e28df3ac6"`); `test_models.py::test_episode_key_shape` (20 chars, `is_episode_key` true); `test_models.py::test_episode_key_differs_by_part` (changing the show id alone, or the guid alone, changes the key); `test_models.py::test_episode_key_rejects_bad_parts` (parametrized: empty source, empty show id, empty guid, `:` in show id → `ValueError`) |
| A3 | `Transcript.to_text()` renders `[HH:MM:SS] text` lines, including over ten hours and for empty segments | `test_models.py::test_to_text_basic` (two segments → exact string `"[00:00:00] Hello\n[00:01:05] World\n"`); `test_models.py::test_to_text_over_ten_hours` (starts `36000.4` and `360000` → `[10:00:00]` and `[100:00:00]`); `test_models.py::test_to_text_empty_segment_text` (`""` and `"  \n "` → line `[00:00:05]` with no trailing space); `test_models.py::test_to_text_no_segments` (`""`); `test_models.py::test_to_text_collapses_newlines` (`"a\nb  c"` → `[..] a b c`, one line); `test_models.py::test_format_hms` (parametrized `0 → 00:00:00`, `59.9 → 00:00:59`, `3725.9 → 01:02:05`, negative and `inf` → `ValueError`) |
| A4 | `ArtifactStore` writes atomically and refuses keys that are not plain hex | Atomicity: `test_artifacts.py::test_atomic_write_creates_file_and_parents`; `test_artifacts.py::test_atomic_write_failure_leaves_no_file` (monkeypatch `earmark.artifacts.os.fsync` to raise `OSError`; the final path does not exist and the directory holds no temp file); `test_artifacts.py::test_atomic_write_failure_keeps_old_content` (same, with an existing file: content unchanged); `test_artifacts.py::test_atomic_writer_body_exception` (exception raised inside the `with` block propagates, nothing at the final path, no temp file); `test_artifacts.py::test_atomic_write_replace_failure` (monkeypatch `os.replace` to raise: no final file, no temp file); `test_artifacts.py::test_atomic_write_mode` (file mode `0o644`, and `mode=0o600` honoured); `test_artifacts.py::test_atomic_writer_temp_suffix` (inside the block, the only file in the directory ends with `.part` and starts with `.`). Keys: `test_artifacts.py::test_rejects_bad_keys`, parametrized over `""`, 19 and 21 hex chars, upper-case hex, `"g" * 20`, `"../" + "0" * 17`, `"0" * 20 + "\n"`, `"0" * 19 + "/"`, Arabic-Indic digits `"٣" * 20`; each raises `ValueError` from `episode_dir`, `transcript_json_path`, `analysis_path`, `find_audio`, and `read_episode`, and nothing is created under `tmp_path`; `test_artifacts.py::test_rejects_bad_prompt_hash` and `test_rejects_bad_audio_ext` (`"MP3"`, `".mp3"`, `"mp3/x"`, `""`, `"abcdef"`) |
| A5 | Each fake satisfies its Protocol under mypy, by typed assignments in a test module | `test_stages.py::test_fakes_satisfy_protocols`: annotated assignments `resolver: Resolver = FakeResolver()`, and likewise for all seven, inside the test function, followed by one positional call on each so the assignments are used. mypy over `tests/` (the gate) is the proof. `test_stages.py::test_protocol_mismatch_fails_mypy` (negative control): writes a snippet to `tmp_path` that defines a class whose `fetch` returns `str` and assigns it to `AudioFetcher`, plus a second class whose `transcribe` takes `(Episode,)` only, runs `mypy.api.run(["--config-file", str(ROOT / "pyproject.toml"), "--cache-dir", str(tmp_path / ".mypy_cache"), "--no-error-summary", str(snippet)])`, and asserts exit status 1 with an `[assignment]` error on each of the two lines; a third, correct class in the same snippet produces no error on its line. This proves the Protocols are real constraints (not `Any`). |
| A6 | Fakes are deterministic and count their calls | `test_fakes.py::test_fakes_deterministic` (two `make_fake_backends` in two temp dirs; run resolve, fetch, transcribe, analyze, store on the same URL and prompt; returned models are equal and every written file is byte-identical between the two dirs); `test_fakes.py::test_resolver_same_url_same_episode` and `test_resolver_mapping_wins`; `test_fakes.py::test_call_counts` (each fake's `call_count` and `calls` records match the arguments passed); `test_fakes.py::test_transcriber_cache_hit_not_computed` (second call: `call_count == 2`, `computed == 1`, transcript equal); same for `test_analyzer_cache_hit_not_computed` (same prompt) and `test_fetcher_cache_hit_not_computed`; `test_fakes.py::test_analyzer_new_prompt_computes` (a second prompt: `computed == 2`, two files under `analyses/`) |
| A7 | `prompt_hash` changes with prompt, model, or schema version and is stable otherwise | `test_models.py::test_prompt_hash_golden` (the five values in section 2.5); `test_models.py::test_prompt_hash_changes` (each of prompt, model, schema version changed alone gives a different hash; `None` vs `""` differ); `test_models.py::test_prompt_hash_stable` (same inputs twice, and default `schema_version` equals passing `ANALYSIS_SCHEMA_VERSION`); `test_models.py::test_prompt_hash_rejects_bad_args` (empty model, `schema_version=0`) |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | Model invariants hold | `test_models.py::test_episode_key_must_match` (an `Episode` with a wrong `key` fails validation, also when loaded from JSON); `test_models.py::test_analysis_hash_must_match`; `test_models.py::test_segment_end_before_start_rejected`; `test_models.py::test_negative_and_nan_seconds_rejected` (parametrized over `Segment.start`, `Episode.duration_s`, `Claim.timestamp_s`, `Entity.timestamps_s`); `test_models.py::test_naive_datetime_rejected`; `test_models.py::test_extra_field_rejected`; `test_models.py::test_models_frozen`; `test_models.py::test_job_kind_requirements` (ingest without url fails; reanalyze without episode_key fails; reanalyze with key and no url passes); `test_models.py::test_store_result_paths_relative` (parametrized `/abs.md`, `../x.md`, `a/../b.md`, `a\\b.md`, `""` rejected; `Earmark/Podcasts/Show/x.md` accepted); `test_models.py::test_bad_ids_rejected` (job id not 32 hex, episode key in `Transcript` not 20 hex) |
| B2 | Retry-intent errors | `test_errors.py::test_hierarchy` (each is an `EarmarkError`; none is an instance of another's class); `test_errors.py::test_message_and_str`; `test_errors.py::test_retry_after` (default `None`, positive accepted, negative and `nan` → `ValueError`); `test_errors.py::test_skip_reason`; `test_errors.py::test_config_error_is_separate` (`ConfigError` is not an `EarmarkError`) |
| B3 | ArtifactStore layout and reads | `test_artifacts.py::test_layout_paths` (each path equals `tmp_path / "episodes" / key / <name>`); `test_artifacts.py::test_constructor_creates_nothing`; `test_artifacts.py::test_episode_round_trip`, `test_transcript_round_trip` (both files written, `.txt` equals `to_text()`, JSON ends with `"\n"`), `test_analysis_round_trip`; `test_artifacts.py::test_read_missing_returns_none`; `test_artifacts.py::test_corrupt_artifact_is_permanent` (garbage JSON and valid JSON of the wrong shape each raise `PermanentError` whose message contains the path and not the file contents, using a sentinel string in the file); `test_artifacts.py::test_mismatched_key_is_permanent` (a valid episode JSON for key A copied into directory B); `test_artifacts.py::test_find_and_delete_audio` (ignores `.audio.mp3.x.part` and `audio.mp3.part`, finds `audio.m4a`, `delete_audio` returns `True` then `False`); `test_artifacts.py::test_list_analyses_sorted_and_filtered` (three analyses with different `created_at`, plus a stray `notes.json` and a temp file; result ordered by `created_at`); `test_artifacts.py::test_list_episode_keys` (ignores a non-key directory and a key directory without `episode.json`); `test_artifacts.py::test_has_transcript` |
| B4 | Fake failure injection | `test_fakes.py::test_fail_next_once_then_succeeds` (`TransientError` raised on call 1, success on call 2, `call_count == 2`, `computed == 1`); `test_fakes.py::test_fail_next_times_and_order` (two different exceptions queued are raised in order); `test_fakes.py::test_fail_next_rejects_zero`; `test_fakes.py::test_every_fake_supports_fail_next` (parametrized over the seven fakes: a queued `PermanentError` is raised by the stage method) |
| B5 | Fake outputs are usable downstream | `test_fakes.py::test_fake_pipeline_writes_artifacts` (resolve → fetch → transcribe → analyze → store with `make_fake_backends(tmp_path, vault_dir=...)`: `episode.json` is not written by fakes (the runner writes it in step 05) but `audio.mp3`, `transcript.json`, `transcript.txt`, `analyses/<hash>.json`, and the note exist; `StoreResult.note_path` resolves under `vault_dir`); `test_fakes.py::test_store_twice_same_bytes`; `test_fakes.py::test_transcriber_missing_audio_is_permanent`; `test_fakes.py::test_analyzer_prompt_response` (`None` prompt → `None`, text prompt → response containing it); `test_fakes.py::test_fake_warnings_flow` (warnings given to the transcriber and analyzer appear on the returned models and survive the cache); `test_fakes.py::test_indexer_rows_replace` |
| B6 | New modules stay light | `test_models.py::test_contract_modules_import_light` (subprocess `[sys.executable, "-c", ...]` imports `earmark.models`, `earmark.errors`, `earmark.artifacts`, `earmark.pipeline.stages`, `earmark.fakes`, then prints the sorted top-level names in `sys.modules` among `fastapi`, `uvicorn`, `httpx2`, `typer`, `faster_whisper`, `ctranslate2`, `lancedb`, `fastembed`, `anthropic`; expects `[]`). The existing `test_cli.py::test_import_earmark_is_light` keeps proving `import earmark` alone loads nothing. |

The negative-control mypy test needs `from mypy import api`; mypy ships `py.typed`, so
it type-checks under strict mode. It runs in well under a second with a fresh cache
(section 2.2). It must not use the repo's `.mypy_cache`.

## 7. Verification

`SCRATCH` is the verifier's scratchpad directory.

```bash
cd /home/sandy/dev/earmark
uv sync
make check
uv run ruff format --check docs/          # the plan's and architecture's Python blocks
uv run mypy                               # "Success", file count includes tests/test_stages.py
uv run pytest -q tests/test_models.py tests/test_errors.py tests/test_artifacts.py \
  tests/test_stages.py tests/test_fakes.py

# Golden values, independently of the tests
uv run python -c 'from earmark.models import episode_key as k; print(k("apple_podcasts", "1200361736", "urn:example:guid-1"))'
# expect 3848d011be9e28df3ac6
uv run python -c 'from earmark.models import prompt_hash as h; print(h(None, "claude-opus-5-5", 1))'
# expect c416a435201e0e94

# Protocols really constrain: write a wrong fake to the scratchpad and type-check it.
# Expect exit 1 and an [assignment] error on the last line.
printf 'from pathlib import Path\nfrom earmark.models import Episode\nfrom earmark.pipeline.stages import AudioFetcher\n\n\nclass Bad:\n    def fetch(self, episode: Episode) -> str:\n        return ""\n\n\nf: AudioFetcher = Bad()\n' > "$SCRATCH/bad_fake.py"
uv run mypy --config-file pyproject.toml --cache-dir "$SCRATCH/mypycache" "$SCRATCH/bad_fake.py"; echo "exit=$?"
rm -rf "$SCRATCH/mypycache" "$SCRATCH/bad_fake.py"

# Fakes end to end in a scratch dir
uv run python - <<'EOF'
import tempfile
from pathlib import Path
from earmark.fakes import make_fake_backends

d = Path(tempfile.mkdtemp())
fb = make_fake_backends(d / "data", vault_dir=d / "vault")
ep = fb.resolver.resolve("https://podcasts.apple.com/us/podcast/x/id1?i=2")
audio = fb.fetcher.fetch(ep)
tr = fb.transcriber.transcribe(ep, audio)
an = fb.analyzer.analyze(ep, tr, None)
res = fb.store.store(ep, tr, [an], ep.published_at or an.created_at)
print(res.note_path, sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()))
EOF
```

Also check:

- `git status` shows only the files in section 3 and this plan.
- No `# type: ignore` or `noqa` in the new files.
- `src/earmark/__init__.py` is unchanged.

There is no live smoke for this step: nothing is wired into a service yet. Step 05's
smoke exercises these fakes.

## 8. Deviations

1. **Stage signatures changed from the architecture's indicative ones**, and
   `docs/ARCHITECTURE.md` now shows the fixed versions: every parameter is
   positional-only; `Store.store` takes `analyses: Sequence[Analysis]` (not `list`) and
   a `shared_at`; `Indexer.index` takes `analyses: Sequence[Analysis]` and the
   `StoreResult`. Reasons in sections 2.2 and 2.6.
2. **Warnings as data.** `Transcript`, `Analysis`, and `StoreResult` gain a `warnings`
   field, and the architecture's rule 4 now says stages report non-fatal problems
   through it. The alternative, a per-call context object with a `warn()` method, would
   add a parameter to every stage for a need two backends have.
3. **Runner skips fetch and transcribe when `transcript.json` exists.** Added to the
   architecture's rule 1. Without it, deleting audio after transcription (step 08)
   forces a re-download on every re-share. Step 05 implements it.
4. **New models** `Claim`, `Entity`, `ResearchLead`, `StoreResult` (was referenced but
   undefined), and `JobResult`. `Analysis` gains `schema_version` and `warnings`.
   `Episode.published_at`, `duration_s`, and `feed_url` are optional; `Job.url` is
   optional with a kind-dependent validator; `Job.result` is a `JobResult`.
5. **`prompt_hash` is 16 hex characters** of a canonical JSON encoding, now stated in
   the architecture's data layout. The architecture said what it covers but not its
   form.
6. **`Job.id` is a 32-hex-character `uuid4`**, fixed here so step 03 and step 04 agree.
7. **Models are frozen with `extra="forbid"`, and sequences are tuples.** The
   architecture did not say. See section 5.1 and 2.2 for the consequences later steps
   must respect.
8. **`ArtifactStore.write_episode` exists, but no stage writes `episode.json`.** The
   runner (step 05) writes it after `resolve`, since the resolver is not cache-aware and
   step 17 reads it back. Recorded in the architecture's data layout.
9. **Extra helpers beyond the brief**: `format_hms` (steps 10, 12, 14 all render
   `[HH:MM:SS]`), `is_episode_key` (step 17's API validates keys before touching disk),
   `atomic_writer` with `mode` and `temp_suffix` (reused by steps 06 and 12),
   `make_fake_backends` (step 05).

## 9. Out of scope

- The pipeline runner, the factory, the worker, and writing `episode.json` (step 05).
- `JobStore`, its schema, backoff, and how `attempts` and `retry_after_s` are used
  (step 03).
- URL validation, prompt normalisation, and length caps for `IngestRequest`;
  `parse_apple_url` (step 04).
- The LLM-facing analysis schema (`analyze/schema.py`), prompts, and `NeedsChunking`
  (step 10). Step 10 may bump `ANALYSIS_SCHEMA_VERSION` and add fields to `Analysis`
  with defaults.
- Sanitizing titles and names for paths and YAML, and the real note layout (step 12).
- Entity normalisation and registry (step 13).
- Which `shared_at` to pass when an episode is shared more than once (step 05 decides;
  the suggestion is the earliest ingest job's share time for that episode).
