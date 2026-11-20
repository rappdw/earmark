# Step 17: Extension: re-run a prompt

## 1. Outcome

A `reanalyze` job runs a prompt on an episode that is already stored. It reads the
episode and transcript from `data/episodes/<key>/` and runs analyze, store, index, and
notify. It never calls the resolver, the fetcher, or the transcriber. The note gains a
section for the new prompt, and the entity registry, entity notes, and search index are
updated by the same store and index calls an ingest makes.

New surface:

- `POST /episodes/{key}/analyze` with body `{"prompt"?: str | null}` enqueues a reanalyze
  job: 202 `{job_id, status, created}`, the same shape as `POST /ingest`. An unknown or
  malformed key gets 404, and an episode with no transcript gets 409.
- `GET /episodes?limit=&offset=` lists stored episodes newest share first. Each entry has
  the key, show, title, publish date, first share time, URL, whether a transcript exists,
  and the analyses already run (prompt, hash, model, time). The list is read from the
  artifacts on disk, never from SQLite.
- `earmark reanalyze <key> [--prompt]` enqueues through the same function as the route
  and prints one line, like `earmark submit`.
- `earmark episodes [--limit] [--offset] [--json]` prints the same list as `GET /episodes`
  (an addition, see section 8).
- Done and failed notifications for reanalyze jobs get their own titles
  (`Analysis ready: …`, `Analysis failed: …`) and name the prompt. Ingest notifications do
  not change.
- The README, `docs/shortcut.md`, and `docs/ARCHITECTURE.md` describe all of this. Two
  new doc-driven tests make the README's command table and HTTP route list match the
  code.

## 2. Findings

What I checked, against the code as it is on `impl` after step 16 (commit `f50ee1a`).

### 2.1 What already exists

- **Job kind and dedupe** (`src/earmark/jobs.py`). `JobStore.enqueue_reanalyze(key, *,
  prompt=None, shared_at=None)` exists, checks the key with `is_episode_key`
  (`ValueError`), and dedupes on `sha256(json([kind, key, prompt]))` among `queued` and
  `running` jobs. It returns `Enqueued(job, created=False)` for an active duplicate. The
  `jobs_active_dedupe` partial unique index enforces it. Ingest and reanalyze never
  dedupe against each other because `kind` is part of the key. `test_jobs.py::
  test_reanalyze_dedupe` and `test_enqueue_reanalyze` already cover the store. **No
  JobStore change is needed.**
- **Job model** (`models.py`). `Job.kind` is `ingest | reanalyze`, and a validator
  requires `episode_key` for `reanalyze`. `url` is `None` for reanalyze jobs.
  `JobResult` (`note_path`, `transcript_note_path`, `analysis_hash`) fits as is.
- **Runner** (`pipeline/runner.py`). `_stage_work` raises
  `PermanentError("reanalyze jobs are not supported yet")` for any non-ingest job.
  `tests/test_runner.py::test_reanalyze_job_fails_permanently` pins that, and this step
  replaces it. The rest of `_stage_work` (analyze, `list_analyses`, append if missing,
  store, guarded index) is the tail a reanalyze needs, unchanged.
- **Artifacts** (`artifacts.py`). `read_episode`, `read_transcript`, `read_share`,
  `record_share` (keeps the earliest time and writes only when the file is missing or
  later), `has_transcript`, `list_analyses` (strict: raises `PermanentError` on the
  first corrupt file), and `list_episode_keys` (directories with an `episode.json`). No
  method returns analysis hashes without parsing the files. `has_episode` does not
  exist.
- **Store** (`vault/notes.py`). `VaultStore.store` regenerates the whole note from every
  analysis it is given, records the episode and all its mentions in the registry
  (`record_episode` replaces the episode's mentions), writes changed entity notes, and
  relinks other notes. A second analysis therefore adds a section and new entities with
  no new code. Store output depends only on its inputs and the paths on disk, so a store
  with unchanged inputs writes nothing.
- **Index** (`index/lancedb_index.py`). `LanceIndex.index` rebuilds the episode's rows
  from all analyses and replaces them in one `merge_insert`. It skips everything when the
  digest is unchanged. A new analysis adds `summary`, `claim`, `lead`, `question`, and
  `answer` rows under its prompt hash. No new code here either.
- **Notifier** (`notify/ntfy.py::compose`). The title prefix is keyed by status only
  (`TITLE_PREFIX`), the done message prints `Prompt: …` only when `job.prompt` is set,
  and the failed message falls back to `job.url`, which is `None` for reanalyze, so it
  would print `Apple Podcasts episode`. `Worker` notifies jobs failed by `requeue_stale`
  with `pipeline.load_episode(job)`, which works for reanalyze because `episode_key` is
  set.
- **Step 13 `episodes` table** (`db.py` migration `0002_entities`). It has `title`,
  `show_title`, `published_at`, `first_shared_at`, and `analyses` (prompt hashes only, no
  prompt text). It is written only by the `vault` store. `entities rebuild` deletes it
  and refills it, and skips episodes without a transcript, share record, or analysis. So
  it is empty with the fake store (the README quickstart), and it never lists an episode
  whose transcription failed. That is why `GET /episodes` reads the artifacts instead
  (section 5.3).
- **API error text** (`api/app.py::_describe`). The `extra_forbidden` message lists
  `IngestRequest.model_fields`, and a missing body says "send a JSON object with a url".
  Both would be wrong for the new route. `INGEST_ERROR_TYPES` already contains
  `prompt_too_long`, so the same custom error type passes its message through.
- **The EarmarkError handler** (`_on_search_error`) maps any `EarmarkError` raised by a
  route to 503, with `Retry-After` for transient errors. The listing reuses it for an
  unreadable episodes directory.
- **Docs and doc tests** (`tests/test_docs.py`). They check the README settings table,
  that the quickstart uses fake backends and makes no network calls, the Shortcut example
  bodies against `POST /ingest`, the Shortcut action order, and relative links. **No
  test checks the README command table or route list.** The README says "Planned:
  re-running a prompt on a stored episode, and a weekly digest", and its HTTP API
  sentence lists five routes. Step 16's plan told it not to mention `reanalyze` or
  `/episodes` until this step.
- **Claude API.** This step changes no LLM call, prompt, schema, or model id, so the
  `claude-api` skill and `ANALYSIS_SCHEMA_VERSION` are not involved. A cached prompt is
  served by the analyzer's existing cache (`analyses/<prompt_hash>.json`, keyed on
  prompt, `LLM_MODEL`, and schema version).

### 2.2 FastAPI behaviour, probed (FastAPI 0.165.0, Starlette 1.10.0, pydantic 2.14.4)

I ran a throwaway app with `@router.post("/episodes/{key}/analyze", status_code=202,
max_body_size=100)` and a body model with `extra="forbid"` and one optional `prompt`:

- In a `RequestValidationError` handler, `request.scope["route"].path` is the route
  template, `"/episodes/{key}/analyze"`. FastAPI sets `child_scope["route"] = self` in
  `APIRoute.matches`. The handler can pick the right body model from it.
- `{"promt": "x"}` gives `extra_forbidden` at `("body", "promt")`. No body, an empty
  body with `Content-Type: application/json`, and `json=None` all give `missing` at
  `("body",)`. A body of `null` gives `model_attributes_type`, and `{}` is valid.
- A body over the limit gets 413, as on `/ingest`.
- `/episodes/a%2Fb/analyze` and `/episodes/..%2F..%2Fetc/analyze` get the router's 404
  (`{"detail":"Not Found"}`). The `{key}` segment never contains `/`.
- FastAPI 0.165 includes routers as an `_IncludedRouter`, so `app.routes` does not list
  the `APIRoute`s. `build_router(...).routes` does, with `methods` `{"POST"}`,
  `{"GET"}`, and `{"GET", "HEAD"}` for `/healthz`. No `HEAD` is added to plain GET
  routes. The new route-list doc test enumerates `build_router(...)`, not the app.
- `typer`: `earmark.cli.app.registered_commands` gives each command's explicit name, and
  `registered_groups` gives `entities` with its `rebuild` command.

### 2.3 Dead ends

- **Listing from the step 13 `episodes` table.** It is derived, holds no prompt text, is
  empty under the fake store, and omits episodes that have no transcript. Do not use it.
- **Parsing `transcript.json` in the API** to decide 409. It can be many megabytes. A
  stat (`has_transcript`) is enough. The worker parses it and fails permanently if it is
  corrupt.
- **Reading every analysis file for every episode on each `GET /episodes`.** Read
  `episode.json` and `shared.json` for all episodes to sort (small files), then read
  analyses only for the page.
- **`app.routes` to enumerate routes** (2.2). Use `build_router`.
- **`ArtifactStore.list_analyses` in the listing.** One corrupt analysis would fail the
  whole response. Read each file separately and skip the bad ones.
- **Predicting a cache hit in the API** with `prompt_hash(prompt, settings.llm_model)`.
  The fake analyzer's model is `fake-llm`, not `LLM_MODEL`, and the API may run with
  other settings than the worker. The job always runs, and `GET /episodes` shows what
  has been run.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/models.py` | Add `ReanalyzeRequest`. |
| `src/earmark/artifacts.py` | Add `ArtifactStore.has_episode(key)` and `ArtifactStore.list_analysis_hashes(key)`. `list_analyses` may use the latter (no behaviour change). |
| `src/earmark/episodes.py` | New. Stored-episode listing (`AnalysisInfo`, `EpisodeInfo`, `EpisodePage`, `list_episodes`), the reanalyze precondition and enqueue (`require_transcript`, `enqueue_reanalyze`), their errors, and constants. Imports only the standard library, pydantic, `models`, `artifacts`, `errors`, `jobs`. |
| `src/earmark/pipeline/runner.py` | Split `_stage_work` into `_ingest_inputs`, `_stored_inputs`, and the shared `_analyze_store_index`. Reanalyze jobs take the stored path. |
| `src/earmark/api/routes.py` | `build_router(store, searcher, artifacts)`. Add `GET /episodes` and `POST /episodes/{key}/analyze`. |
| `src/earmark/api/app.py` | `create_app(..., artifacts=None)`. The validation handler picks the body model by route, and `describe_validation_errors` gains keyword parameters with the current defaults. |
| `src/earmark/api/schemas.py` | No new model; the route returns `IngestResponse`. `EpisodePage` comes from `earmark.episodes`. |
| `src/earmark/notify/ntfy.py` | Reanalyze titles and message lines in `compose`. |
| `src/earmark/cli.py` | `reanalyze` and `episodes` commands. |
| `tests/test_runner.py` | Replace `test_reanalyze_job_fails_permanently` with the reanalyze runner tests (6). |
| `tests/test_reanalyze.py` | New. End-to-end tests with the real `VaultStore`: second section, entity notes, a restored note, and the store's inputs. |
| `tests/test_api_episodes.py` | New. Both routes. |
| `tests/test_episodes.py` | New. `list_episodes`, `require_transcript`, `enqueue_reanalyze` without HTTP. |
| `tests/test_artifacts.py` | Tests for the two new `ArtifactStore` methods. |
| `tests/test_models.py` | `ReanalyzeRequest` tests. |
| `tests/test_ntfy.py` | Reanalyze `compose` tests. |
| `tests/test_cli.py` | `reanalyze` and `episodes` command tests. |
| `tests/test_docs.py` | Two new doc tests (6, D1 and D2). |
| `tests/test_lancedb_index.py` | One `index`-marked test: reanalyze through the pipeline adds rows under the new prompt hash. |
| `README.md` | Re-running a prompt, quickstart additions, the command table, the HTTP API sentence, and the "Planned" line. |
| `docs/shortcut.md` | Section 5: asking another question. |
| `docs/ARCHITECTURE.md` | Already edited with this plan (section 8). |
| `docs/PLAN.md` | Status row to `done`, by the orchestrator after verification only. |

Nothing else changes. In particular, `jobs.py`, `db.py` (no migration), the analyzer,
the vault store, the indexer, the factory, and `docs/deploy.md` stay as they are.

## 4. Interfaces

### 4.1 `models.ReanalyzeRequest`

```python
class ReanalyzeRequest(Model):
    prompt: str | None = None

    @field_validator("prompt", mode="after")
    @classmethod
    def _normalise_prompt(cls, v: str | None) -> str | None:
        try:
            return normalize_prompt(v)
        except ValueError as exc:
            raise PydanticCustomError("prompt_too_long", str(exc)) from None
```

It is frozen with `extra="forbid"` (from `Model`). Its prompt rules are exactly those of
`IngestRequest`: stripped, blank becomes `None`, at most `MAX_PROMPT_CHARS` after
stripping, error type `prompt_too_long`. `INGEST_ERROR_TYPES` is unchanged. The
implementer may share the validator body through a small helper, but must not change
`IngestRequest`'s behaviour or error types.

### 4.2 `artifacts.ArtifactStore` additions

```python
def has_episode(self, key: str) -> bool:
    """True when ``episodes/<key>/episode.json`` is a file. Raises ValueError on a bad key."""


def list_analysis_hashes(self, key: str) -> list[str]:
    """Sorted prompt hashes of the ``analyses/<hash>.json`` files; [] without the directory.

    Matches only ``[0-9a-f]{16}.json`` regular files; parses nothing.
    """
```

### 4.3 `earmark/episodes.py`

```python
EPISODES_LIMIT_DEFAULT: Final = 50
EPISODES_LIMIT_MAX: Final = 200
EPISODES_OFFSET_MAX: Final = 1_000_000
EPISODE_NOT_FOUND: Final = "episode not found"
NO_TRANSCRIPT: Final = (
    "this episode has no transcript yet; share it again, or wait for its job to finish"
)


class AnalysisInfo(Model):
    prompt: str | None  # None = the default analysis
    prompt_hash: PromptHash
    model: str
    created_at: AwareDatetime


class EpisodeInfo(Model):
    key: EpisodeKey
    show: str  # Episode.show_title
    title: str  # Episode.title
    published_at: AwareDatetime | None
    shared_at: AwareDatetime | None  # shared.json first_shared_at; None when missing
    url: str  # the canonical Apple Podcasts URL
    has_transcript: bool
    analyses: tuple[AnalysisInfo, ...] = ()  # newest first


class EpisodePage(Model):
    episodes: tuple[EpisodeInfo, ...]
    total: int  # every listable episode, not just this page
    offset: int
    limit: int
    next_offset: int | None  # offset + limit while that is < total, else None


class EpisodeLookupError(Exception):
    """Base of the two reanalyze preconditions; ``message`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EpisodeNotFoundError(EpisodeLookupError): ...


class TranscriptMissingError(EpisodeLookupError): ...


def list_episodes(
    artifacts: ArtifactStore,
    *,
    limit: int = EPISODES_LIMIT_DEFAULT,
    offset: int = 0,
) -> EpisodePage: ...


def require_transcript(artifacts: ArtifactStore, key: str) -> None: ...


def enqueue_reanalyze(
    jobs: JobStore, artifacts: ArtifactStore, key: str, request: ReanalyzeRequest
) -> Enqueued: ...
```

- `list_episodes` raises `ValueError` for `limit` outside `1..EPISODES_LIMIT_MAX` or
  `offset` outside `0..EPISODES_OFFSET_MAX`. It raises
  `TransientError("the episode files under EARMARK_DATA_DIR cannot be read")` when listing
  the episodes directory itself fails with an `OSError`. Rules in 5.3.
- `require_transcript` raises `EpisodeNotFoundError(EPISODE_NOT_FOUND)` when `key` fails
  `is_episode_key` or `has_episode(key)` is false. It raises
  `TranscriptMissingError(NO_TRANSCRIPT)` when `has_transcript(key)` is false. It checks
  the key before building any path, and parses nothing.
- `enqueue_reanalyze` calls `require_transcript`, then
  `jobs.enqueue_reanalyze(key, prompt=request.prompt)`. It logs at INFO
  `"enqueued reanalyze job %s for %s (created=%s, prompt=%s)"` with the job id, the key,
  `created`, and `"yes"`/`"no"`. It never logs the prompt text.

### 4.4 HTTP

| Route | Request | Responses |
|---|---|---|
| `GET /episodes` | auth; `limit` int 1–200 (default 50), `offset` int 0–1 000 000 (default 0) | 200 `EpisodePage` JSON. 401. 422 for invalid parameters (`limit: …` / `offset: …`, never echoing input). 503 `{"detail": "the episode files under EARMARK_DATA_DIR cannot be read"}` with `Retry-After: 5`. |
| `POST /episodes/{key}/analyze` | auth; `Content-Type: application/json`; body `ReanalyzeRequest`, at most 64 KiB (`MAX_BODY_BYTES`, shared with `/ingest`) | 202 `IngestResponse` `{job_id, status, created}` with `Location: /jobs/{id}`; `created` is false for an active duplicate. 401. 413. 422 for an invalid body. 404 `{"detail": "episode not found"}`. 409 `{"detail": NO_TRANSCRIPT}`. 503 job store unavailable (the existing `sqlite3.Error` handler). |

Route signatures, in `build_router(store: JobStore, searcher: Searcher, artifacts:
ArtifactStore) -> APIRouter`:

```python
@router.get("/episodes")
def list_stored_episodes(
    limit: Annotated[int, Query(ge=1, le=EPISODES_LIMIT_MAX)] = EPISODES_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0, le=EPISODES_OFFSET_MAX)] = 0,
) -> EpisodePage:
    return list_episodes(artifacts, limit=limit, offset=offset)


@router.post("/episodes/{key}/analyze", status_code=202, max_body_size=MAX_BODY_BYTES)
def reanalyze(key: str, body: ReanalyzeRequest, response: Response) -> IngestResponse:
    try:
        enqueued = enqueue_reanalyze(store, artifacts, key, body)
    except EpisodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from None
    except TranscriptMissingError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from None
    job = enqueued.job
    response.headers["Location"] = f"/jobs/{job.id}"
    return IngestResponse(job_id=job.id, status=job.status, created=enqueued.created)
```

`create_app(settings, *, store=None, searcher=None, artifacts=None)`. With `artifacts`
`None` it uses `ArtifactStore(settings.data_dir)`, which creates nothing.

Validation messages (`api/app.py`):

```python
def describe_validation_errors(
    errors: Sequence[Mapping[str, Any]],
    *,
    body_fields: Sequence[str] = tuple(IngestRequest.model_fields),
    missing_body: str = "request body is missing; send a JSON object with a url",
) -> str: ...
```

`_on_validation_error` reads `getattr(request.scope.get("route"), "path", None)`. For
`"/episodes/{key}/analyze"` it passes `body_fields=tuple(ReanalyzeRequest.model_fields)`
and `missing_body="request body is missing; send a JSON object such as {\"prompt\":
\"...\"}, or {} for the default analysis"`. Every other route keeps the defaults, so
`/ingest` messages stay byte for byte the same (`test_api.py` already pins them). The
`extra_forbidden` text becomes `unknown field 'promt' (allowed: prompt)` on the new route.

### 4.5 Runner

```python
def _stage_work(self, claim: Claim, state: _RunState) -> tuple[Analysis, StoreResult]:
    job = claim.job
    if job.kind == "reanalyze":
        episode, transcript, shared_at = self._stored_inputs(claim, state)
    else:
        episode, transcript, shared_at = self._ingest_inputs(claim, state)
    return self._analyze_store_index(claim, episode, transcript, shared_at)
```

- `_ingest_inputs` is today's resolve → `write_episode` → `set_episode_key` →
  `record_share` → cached transcript, or fetch and transcribe → the transcript key check,
  then `set_stage("analyze")`. Raise `PermanentError("ingest job without a url")` when
  `job.url is None`. The `Job` validator makes that unreachable, but mypy needs the
  narrowing.
- `_stored_inputs` follows 5.1.
- `_analyze_store_index(claim, episode, transcript, shared_at)` copies
  `transcript.warnings`, calls the analyzer, checks the analysis key, copies its
  warnings, then does `list_analyses` (appending if missing), store, store warnings, and
  the guarded index, exactly as today. It does not call `set_stage("analyze")`; each
  input path has already done so.

### 4.6 CLI

```text
earmark reanalyze EPISODE_KEY [--prompt TEXT]
earmark episodes [--limit 1..200 (default 50)] [--offset N (default 0)] [--json]
```

`earmark reanalyze`:

- On success, exactly one stdout line: `<job_id> <status>`, plus ` (existing job)` for
  an active duplicate. It is the same format as `earmark submit`, for example
  `3f2a…c1 queued`.
- Exit 2: `earmark reanalyze: not an episode key: <cleaned input>` (key fails
  `is_episode_key`), or the `ReanalyzeRequest` validation messages joined by `; `
  (prompt too long).
- Exit 1: `earmark reanalyze: no episode <key> under EARMARK_DATA_DIR; see `earmark
  episodes``, or `earmark reanalyze: episode <key>: this episode has no transcript yet;
  share it again, or wait for its job to finish`, or `earmark reanalyze: cannot open the
  job store: <exc>` (`DatabaseSetupError`, `sqlite3.Error`, `OSError`).
- Errors go to stderr, and nothing is printed to stdout on failure. It builds no
  backend, so it works with any backend configuration and loads nothing heavy. Like the
  other commands, both new commands import `earmark.episodes`, `earmark.jobs`, and
  `earmark.artifacts` inside the function, so `test_cli_import_is_light` keeps passing.

`earmark episodes` text output, every line passed through `cli._clean`:

```text
<key>  <YYYY-MM-DD | ->  <show> — <title>
  <prompt_hash>  <model>  <prompt | (default analysis)>
  no transcript yet
showing 1-50 of 120; next: --offset 50
```

- One header line per episode. The date is `published_at` in `EARMARK_TZ`
  (`zoneinfo.ZoneInfo(settings.tz)`), else the system zone (`astimezone()`), the same rule
  the note's date uses. `-` when unknown. The separator is U+2014 between spaces.
- Then one line per analysis, newest first. The prompt is whitespace-collapsed and cut to
  200 characters with `…`.
- `  no transcript yet` when `has_transcript` is false.
- The footer is printed only when `offset > 0` or `next_offset is not None`:
  `showing {offset+1}-{offset+len(page)} of {total}`, plus `; next: --offset {next}` when
  there is a next page.
- `no episodes` when `total == 0`. `no episodes at offset {offset} (total {total})` when
  the page is empty but `total > 0`.
- `--json` prints `EpisodePage.model_dump_json()`, the same document as the API.
- Exit 1 with `earmark episodes: <message> (temporary; try again later)` on the
  `TransientError` from 4.3.

### 4.7 Notifications (`notify/ntfy.py`)

New constant beside `TITLE_PREFIX`, which is unchanged:

```python
REANALYZE_TITLE_PREFIX: Final[Mapping[JobStatus, str]] = {
    "done": "Analysis ready",
    "failed": "Analysis failed",
    "skipped": "Analysis skipped",
}
DEFAULT_ANALYSIS_LINE: Final = "Default analysis"
```

For `job.kind == "reanalyze"` only:

- Title: `REANALYZE_TITLE_PREFIX[status]: <episode title or "podcast episode">`.
- Done message: the show line (as now), then `Prompt: <prompt>` or `Default analysis`
  when `job.prompt` is `None`, then the warning line and the `Note:` line exactly as now.
  The click link is unchanged.
- Failed and skipped message: the show line, falling back to `Episode <job.episode_key>`
  instead of the URL; then `Prompt: <prompt>` or `Default analysis`; then `Stage:`,
  `Reason:`, and the attempts line exactly as now.
- Tags, priority, cleaning, byte caps, redaction: unchanged.

Ingest notifications stay byte for byte the same.

## 5. Behaviour and edge cases

### 5.1 A reanalyze job in the worker

`_stored_inputs(claim, state)`:

1. `key = job.episode_key`. If it is `None`, raise
   `PermanentError("reanalyze job without an episode key")`. That is unreachable through
   `Job`'s validator.
2. `jobs.set_stage(claim, "analyze")`. The first and only input stage, so a failure while
   reading the artifacts reports `Stage: analyze`.
3. `episode = artifacts.read_episode(key)`. If it is `None`, raise
   `PermanentError(f"episode {key} is not stored; share it again")`. Otherwise set
   `state.episode = episode` straight away, so failure notifications carry the episode.
4. `transcript = artifacts.read_transcript(key)`. If it is `None`, raise
   `PermanentError(f"episode {key} has no transcript; share it again to transcribe it")`.
   A corrupt file raises the existing `PermanentError("corrupt artifact: …")`.
5. `shared_at = artifacts.record_share(key, job.shared_at)`. With `shared.json` present,
   which every episode stored since step 05 has, this returns the stored first share and
   writes nothing, because a reanalyze job's `shared_at` (its enqueue time) is later.
   Only when `shared.json` is missing does it write the job's time, so the episode stays
   rebuildable by `entities rebuild`. A reanalyze never moves the first share time
   earlier in practice, so step 18's digest window is unaffected.
6. Return `(episode, transcript, shared_at)`.

What the job does not do: call `resolver`, `fetcher`, or `transcriber`; call
`set_episode_key` (the key is already on the job); or write `episode.json`. Stages
entered are `analyze`, `store`, then `index` (when an indexer is configured). `job.stage`
on a done reanalyze is `index`, or `store` with `INDEX_BACKEND=none`.

Exceptions map to outcomes exactly as for ingest (`SkipEpisode`, `PermanentError`,
`TransientError`, other). An `OSError` reading artifacts (permissions) is "any other
exception": transient, with the frame summary.

`JobResult` is as for ingest: the note paths and `analysis_hash` of this job's
analysis.

### 5.2 A prompt that was already run

The job always runs. There is no shortcut in the API or the CLI.

- The analyzer finds `analyses/<prompt_hash(prompt, LLM_MODEL)>.json` and returns it
  with no LLM call (`FakeAnalyzer.computed` does not change).
- The store still runs with every analysis on disk. If the note is intact, nothing is
  written. If the note was deleted, it is written again, which "restores the note". If
  its managed region was edited by hand, the usual rule applies: the old file is backed
  up to `Backups/` and the region replaced, with a warning. That is the same as for any
  store, and the README says so in one sentence.
- The index call runs. Its digest is unchanged, so it embeds nothing.
- The done notification is sent (`Analysis ready: …`).

The same prompt under a different `EARMARK_LLM_MODEL` (or after an
`ANALYSIS_SCHEMA_VERSION` bump) is a new hash and a new LLM call, and the note then has
both sections. `GET /episodes` shows the model of each analysis, so this is visible.

### 5.3 `GET /episodes` and `list_episodes`

**Source.** The artifacts under `EARMARK_DATA_DIR/episodes/`, the system of record next
to the vault. The listing never opens SQLite. It does not depend on the step 13
`episodes` table, so it is the same before and after `earmark entities rebuild`, with the
fake store, and with a deleted or empty `earmark.db`. It never scans the vault either:
the API container mounts the vault, but the systemd API unit does not have it in
`ReadWritePaths` and should not need it.

**Algorithm.**

1. Check `limit` and `offset` (`ValueError`; the route's `Query` bounds reject them
   first with 422).
2. `keys = artifacts.list_episode_keys()`: directories with an `episode.json`, sorted. A
   missing episodes directory gives `[]`, and nothing is created. An `OSError` from
   listing the directory raises the `TransientError` in 4.3.
3. For every key, read `episode.json` (`read_episode`) and `shared.json`
   (`read_share`). On `PermanentError` (corrupt or mismatched JSON) or `OSError`, log a
   WARNING `"episode %s: unreadable files under EARMARK_DATA_DIR; left out of the
   listing"` and leave the episode out. It does not count in `total`.
4. Sort: episodes with a share record by `first_shared_at` descending (compared as
   instants), ties by key ascending; then episodes without one, by key ascending. The
   order is stable, because the first share time changes only when an earlier share
   arrives.
5. `total = len(listable)`, and the page is `listable[offset : offset + limit]`.
6. For each page entry only: `has_transcript = artifacts.has_transcript(key)` (a stat,
   never a parse). Analyses: for each hash in `list_analysis_hashes(key)`,
   `read_analysis`. On `PermanentError` or `OSError`, log a WARNING naming the key and
   the hash and skip that analysis. Map the rest to `AnalysisInfo` and sort by
   `(created_at, prompt_hash)` descending (newest first, the note's order). `.parts`
   directories are never matched.
7. `next_offset = offset + limit` if that is below `total`, else `None`.

**Cost.** Two small JSON reads per stored episode per request, plus the analyses of at
most `limit` episodes. A homelab with thousands of episodes answers in well under a
second. That is acceptable for a list the user opens by hand. The route is a sync
handler, so FastAPI runs it in its thread pool.

**Pagination.** Offset-based. Earmark never deletes episodes, and new shares sort first,
so a share that arrives while a client pages shifts later pages down by one: the client
may see one entry twice, and never misses one. The docs say "newest share first" and
nothing more. A keyset cursor was considered and rejected (section 8, item 5).

**What is listed.** Every episode with an `episode.json`, including those whose
transcription failed or is still running (`has_transcript: false`, usually `analyses:
[]`), so a client can tell why a reanalyze would get 409. Skipped episodes are not
listed: `SkipEpisode` is raised before `episode.json` is written.

**Untrusted text.** Show, title, and prompts are returned as JSON strings. The CLI
cleans them. The description is not included.

### 5.4 `POST /episodes/{key}/analyze`

- **Order of checks.** The auth middleware (401) comes first, then the route's body
  limit (413), then FastAPI's body validation (422), and only then the handler's 404 and
  409. An unauthenticated request for an unknown key gets 401. A bad body for an unknown
  key gets 422.
- **404.** The key is not 20 lower-case hex characters (uppercase, 21 characters, and
  `nothex` included; never normalised), or `episodes/<key>/episode.json` is not a file.
  As on `GET /jobs/{id}`, a malformed id is "not found", not 422. The key is checked
  before any path is built.
- **409.** `episode.json` exists but `transcript.json` does not: the ingest job is still
  fetching or transcribing, or it failed before the transcript. The message says what to
  do. An episode with a transcript and no analysis (an ingest that failed in analyze) is
  accepted, since running an analysis is exactly what it needs.
- **No parse in the API.** The handler stats two files, then enqueues. A corrupt
  `episode.json` or `transcript.json` is found by the worker, which fails the job
  permanently and notifies.
- **Dedupe.** The prompt is normalised by `ReanalyzeRequest` before the dedupe key is
  computed, so `{}`, `{"prompt": null}`, `{"prompt": ""}`, and `{"prompt": "  "}` are
  all the default analysis and dedupe together, and `" why? "` dedupes with `"why?"`.
  The same key and prompt while a job is `queued` (including one waiting for a retry) or
  `running` returns that job with 202 and `created: false`. Once that job is terminal, a
  repeat creates a new job (5.2 says what it does). Reanalyze and ingest never dedupe
  with each other: a re-share and a reanalyze of the same prompt both run, and the second
  is a cache hit.
- **Body limit.** The largest valid body is about 48 KiB: 4000 characters that
  `json.dumps` escapes as 12-byte surrogate pairs. So the 64 KiB `MAX_BODY_BYTES` of
  `/ingest` is reused, and the route sets `max_body_size` itself (never app-level,
  step 04 section 2.2).
- **Writes.** The API writes nothing under `EARMARK_DATA_DIR/episodes`. A 404 must not
  create `episodes/<key>/`.

### 5.5 Concurrency

With one worker (the default and the only tested deployment), jobs run one at a time.
With several workers, an ingest and a reanalyze of the same episode can each list the
analyses before the other's analysis file exists, and the vault lock orders their stores
arbitrarily. The note and index can then miss one section until the next store of that
episode. This step does not fix that; it is recorded in the architecture's runner
section. Do not add locking for it.

### 5.6 Notifications

5.1 sets `state.episode` before the transcript is read, so a "no transcript" failure
still names the episode. A missing `episode.json` notifies with `episode=None`. The
title is then `Analysis failed: podcast episode`, and the first line is
`Episode <key>`.

### 5.7 Things that must not change

- `POST /ingest` validation messages, body limit, and responses.
- Ingest runner order, stages, warnings order, and `JobResult`, so `test_e2e.py` and the
  existing runner tests pass untouched, except for the one replaced test.
- Ingest notification text.
- `earmark analyze` (inline, writes only the analysis cache, no note).

## 6. Tests

All tests run with sockets blocked and need no fixtures from outside the repository.
Episodes are built with `tests/factories.make_episode`, written through `ArtifactStore`
(`write_episode`, `record_share`, `write_transcript`, `write_analysis`), or produced by
running an ingest through the fakes (`tests/pipeline_env.make_env`).

### 6.1 Acceptance criteria from the brief

| # | Criterion | Tests |
|---|---|---|
| A1 | A reanalyze job does not call the resolver, fetcher, or transcriber | `test_runner.py::test_reanalyze_never_calls_input_stages`: artifacts seeded directly through `ArtifactStore` (episode, share, transcript, one default analysis). The backends are `replace(fakes.backends(), resolver=Exploding(), fetcher=Exploding(), transcriber=Exploding())`, where each double raises `AssertionError` on any call. Run `enqueue_reanalyze(key, prompt="Who is quoted?")`: the job is `done`, and `CountingJobStore.stages == ["analyze", "store", "index"]`. `test_runner.py::test_reanalyze_after_ingest_uses_artifacts`: ingest with the fakes, then a reanalyze. `resolver`, `fetcher`, and `transcriber` call counts are unchanged by the second job, the analyzer is called once with the prompt, and `computed` goes up by one. |
| A2 | The note gains a second analysis section, and entities and the index are updated | `test_reanalyze.py::test_second_section_and_entities` (real `VaultStore` in `tmp_path`, an analyzer double from 6.3): ingest with no prompt, then a reanalyze with `"Who built the first compiler?"`. The episode note holds both `Default analysis` and `Analysis: Who built the first compiler?`, newest first. `Earmark/Entities/People/Grace Hopper.md` exists only after the reanalyze and lists the episode, and the episode note links it. The registry's `mentions` has rows for both prompt hashes. `test_runner.py::test_reanalyze_store_and_index_get_all_analyses`: the store call and the `FakeIndexer` call each receive both analyses, sorted by `(created_at, prompt_hash)`, and `stored` is the store's result. `test_lancedb_index.py::test_reanalyze_adds_rows_for_new_prompt` (`index` marker; real LanceDB, `FakeEmbedder`, the pipeline with `indexer=LanceIndex`): after the reanalyze the table has `summary` and `answer` rows whose `prompt_hash` is the new hash, and the transcript rows' ids are unchanged. |
| A3 | Unknown key 404; key with no transcript 409 | `test_api_episodes.py::test_reanalyze_unknown_key_404`, parametrized over a well-formed absent key, `"nothex"`, the key upper-cased, and the key plus one character. Each gets 404 `{"detail": "episode not found"}`, nothing is enqueued, and `episodes/<key>` is not created. `test_api_episodes.py::test_reanalyze_without_transcript_409`: `episode.json` and `shared.json` only, so 409 with `NO_TRANSCRIPT` and nothing enqueued. `test_api_episodes.py::test_reanalyze_transcript_without_analysis_accepted` gets 202. `test_episodes.py::test_require_transcript` covers the same three cases without HTTP. |
| A4 | `GET /episodes` lists key, show, title, date, and the prompts already run | `test_api_episodes.py::test_list_episodes_fields`: two episodes, one with a default and a prompted analysis. The JSON has `key`, `show`, `title`, `published_at`, `shared_at`, `url`, `has_transcript`, and `analyses` with `prompt` (`null` for the default), `prompt_hash`, `model`, and `created_at`, newest first. It validates as `EpisodePage`, and `total`, `offset`, `limit`, and `next_offset` are correct. |

### 6.2 Criteria added by this plan

| # | Criterion | Tests |
|---|---|---|
| B1 | Reanalyze dedupe through the API | `test_api_episodes.py::test_reanalyze_dedupe`. The same prompt twice gives the same `job_id` and `created` true then false. `" why? "` after `"why?"` is the same job. `{}`, `{"prompt": null}`, and `{"prompt": "  "}` are one job with `prompt is None`. A different prompt is a new job. After the job is completed through the store, a repeat creates a new job. An ingest for the same episode never matches (already covered by `test_jobs.py::test_reanalyze_dedupe`). |
| B2 | 202 response shape and stored job | `test_api_episodes.py::test_reanalyze_202`: `Location: /jobs/<id>`, body keys exactly `{job_id, status, created}`, the stored job is `kind == "reanalyze"`, `url is None`, `episode_key == key`, and has the normalised prompt. |
| B3 | Body validation and limits | `test_api_episodes.py::test_reanalyze_body_errors_422`, parametrized. `{"promt": "x"}` gives `unknown field 'promt' (allowed: prompt)`. A missing body and an empty body give the reanalyze missing-body message. `null` and `[]` give the JSON-object message. A body that is not JSON gives `request body is not valid JSON`. A 4001-character prompt gives the `prompt_too_long` message. Nothing is enqueued in any case. `test_reanalyze_body_too_large_413`, with a Content-Length body and a chunked body just over `MAX_BODY_BYTES`: 413, nothing enqueued. `test_reanalyze_max_valid_body_accepted` (4000 × `"😀"`, `ensure_ascii` escapes) gets 202. `test_api.py`'s existing ingest message tests stay green unchanged. |
| B4 | Auth comes first, and the store error maps to 503 | `test_api_episodes.py::test_episodes_routes_require_auth`: both routes without a token get 401 with `WWW-Authenticate`, including an unknown key with a bad body. `test_reanalyze_store_error_503`: a `JobStore` whose `enqueue_reanalyze` raises `sqlite3.OperationalError` gives 503 with `Retry-After`. |
| B5 | `GET /episodes` order, pagination, and bounds | `test_api_episodes.py::test_list_episodes_pagination`: five episodes with distinct share times, plus one without `shared.json`. With `limit=2` and offsets 0, 2, 4: newest share first, the one without a share last, and `next_offset` 2, 4, then `None`. `total == 6` each time, and an offset past the end gives an empty page with `total == 6`. `test_list_episodes_bad_params_422`: `limit=0`, `limit=201`, `offset=-1`, `offset=1000001`, and `limit=abc` each get 422 with a `str` detail that does not contain the input. |
| B6 | `GET /episodes` comes from artifacts only, tolerates damage, and creates nothing | `test_api_episodes.py::test_list_episodes_empty_creates_nothing` (no data dir gives `{"episodes": [], "total": 0, …}`, and `data/episodes` does not exist afterwards). `test_list_episodes_ignores_job_store`: `create_app` with a `JobStore` whose every method raises, so `GET /episodes` still gives 200. `test_episodes.py::test_list_independent_of_registry`: the listing is equal before and after `VaultStore.rebuild()`, and with a fresh empty `earmark.db`. `test_episodes.py::test_list_skips_unreadable`: a corrupt `episode.json` is left out of the list and of `total`, a corrupt analysis file is left out of `analyses` while the other analysis remains, both are logged at WARNING (`caplog`), and a `.parts` directory is ignored. `test_episodes.py::test_list_never_parses_transcript`: `transcript.json` holding `not json` still gives `has_transcript: true`. `test_episodes.py::test_list_unreadable_root_is_transient`: `ArtifactStore.list_episode_keys` patched to raise `PermissionError` gives `TransientError`. Through the API that is 503 with `Retry-After`. `test_episodes.py::test_list_bounds_value_error`. |
| B7 | Runner failure paths for reanalyze | `test_runner.py::test_reanalyze_missing_episode_fails`: `failed`, error `episode <key> is not stored; share it again`, `stage == "analyze"`, notified once with `episode is None`, no stage backend called. `test_reanalyze_missing_transcript_fails`: `failed`, the transcript message, notified with the episode. `test_reanalyze_transient_analyzer_error_requeues`: `analyzer.fail_next(TransientError(...))` leaves the job `queued` with no notification. The next run is `done`. |
| B8 | Already-run prompt: no LLM call, note restored | `test_runner.py::test_reanalyze_same_prompt_is_cache_hit`: after an ingest with prompt `p`, a reanalyze with `p` leaves `analyzer.computed` unchanged. The store and index are still called (with one analysis), the job is `done`, and `result.analysis_hash == prompt_hash("p", "fake-llm")`. `test_reanalyze.py::test_same_prompt_restores_deleted_note`: real `VaultStore`; after an ingest, delete the episode note, then reanalyze with the same prompt. The note is back with the same bytes at the same vault path, and `computed` is unchanged. |
| B9 | Share time and warnings | `test_runner.py::test_reanalyze_keeps_first_share_time`: the store's `shared_at` is the ingest's first share, not the reanalyze job's `shared_at`, and `shared.json` is unchanged. `test_reanalyze_without_share_record_writes_one`: with `shared.json` deleted, the store gets the job's `shared_at` and `shared.json` now holds it. `test_reanalyze_copies_warnings`: transcript warnings (fake transcriber with `warnings=`) and analysis warnings end up in `job.warnings`, transcript ones first. |
| B10 | Notifications for reanalyze | `test_ntfy.py::test_compose_reanalyze_done`: title `Analysis ready: <title>`, message starts with the show and then `Prompt: <prompt>`, and the click link is as for ingest. `test_compose_reanalyze_default_prompt`: the line `Default analysis`. `test_compose_reanalyze_failed_without_episode`: title `Analysis failed: podcast episode`, first line `Episode <key>`, then `Default analysis` or `Prompt:`, `Stage: analyze`, `Reason: …`. `test_compose_ingest_unchanged_for_kind`: an ingest job's notification equals the one built before this step, for done and failed (compare with literal expected strings). |
| B11 | `earmark reanalyze` | `test_cli.py::test_reanalyze_then_worker_once` (`fake_env`): submit and `worker --once`, then `reanalyze <key> --prompt q2` prints `^[0-9a-f]{32} queued\n$`. `worker --once` prints `job <id> done`, the key has two analysis files, and the fake note has two `##` sections. `test_reanalyze_existing_job` prints `(existing job)`. `test_reanalyze_bad_key_exits_2`, `test_reanalyze_prompt_too_long_exits_2`, `test_reanalyze_unknown_episode_exits_1`, `test_reanalyze_no_transcript_exits_1`: the stderr messages from 4.6, empty stdout, and no job enqueued. `test_reanalyze_store_error_exits_1`. |
| B12 | `earmark episodes` | `test_cli.py::test_episodes_text`: the header line with date and em dash, analysis lines with `(default analysis)`, a `no transcript yet` line, and the footer when paged with `--limit 1`. `test_episodes_json` equals `list_episodes(...).model_dump_json()`. `test_episodes_empty` prints `no episodes`. `test_episodes_cleans_control_chars`: a title with `\x1b[31m` is printed with `?`. `test_episodes_bad_limit_exits_2` for `--limit 0` and `--limit 201`. |
| B13 | Light imports | `test_cli.py::test_cli_import_is_light` and `test_import_earmark_is_light` stay green. `earmark.episodes` imports nothing heavy (no httpx2, fastapi, anthropic). Add it to the light-import list if that test enumerates modules. |
| B14 | Artifacts helpers | `test_artifacts.py::test_has_episode` (true, false, `ValueError` on a bad key). `test_list_analysis_hashes`: sorted, ignores `.parts`, temp files, and other names; `[]` without the directory; parses nothing (a corrupt file's hash is still listed). |
| B15 | `ReanalyzeRequest` | `test_models.py::test_reanalyze_request_prompt_rules`: blank becomes `None`, stripped, exactly 4000 accepted, 4001 is `prompt_too_long`, `{}` is valid, an extra key is `extra_forbidden`. |

### 6.3 Test doubles

- `Exploding` (in `tests/test_runner.py`): one class with `resolve`, `fetch`, and
  `transcribe` methods that raise `AssertionError("must not be called")`.
- `PromptedAnalyzer` (in `tests/test_reanalyze.py`): wraps `FakeAnalyzer`. It
  delegates, and when `prompt` is not `None` and the analysis was just computed, it
  rewrites it with `people=(Entity(name="Grace Hopper", context="Built a compiler.",
  timestamps_s=(0.0,)),)` added, through `ArtifactStore.write_analysis`, so the cache
  holds the rewritten one. It increments its own `computed`. Simpler alternative: a
  standalone class implementing `Analyzer` with `prompt_hash(prompt, "fake-llm")`, the
  cache check, and per-prompt entities. Either is fine. The point is that the second
  analysis brings an entity the first does not have.
- `VaultStore(vault, "Earmark", data_dir=tmp_path / "data")` with an existing
  `tmp_path / "vault"` directory, passed as `store=` in `replace(fakes.backends(), ...)`.

### 6.4 Doc-driven tests (`tests/test_docs.py`)

| # | Criterion | Test |
|---|---|---|
| D1 | The README command table lists every CLI command, and nothing else | `test_readme_commands_cover_cli`. The rows of the table under `## Commands` (rows starting `` | ` ``) are reduced to their command words: the first backticked cell, cut before the first `<`, `[`, or upper-case placeholder (`QUERY`). Compare with `{c.name for c in earmark.cli.app.registered_commands}` plus `entities rebuild` from `registered_groups`, plus `--version`. Every command must appear, and every row must name one. |
| D2 | The README's HTTP API sentence lists every route | `test_readme_http_api_lists_every_route`. Take the paragraph starting `The HTTP API is`, and its backticked spans of the form `METHOD[|METHOD] /path`. Expand to `(method, path)` pairs with `{…}` replaced by `{}`. Compare with `{(m, path) for r in build_router(store, searcher, artifacts).routes if isinstance(r, APIRoute) for m in r.methods}`, normalised the same way. The sets must be equal. Use `build_router`, not `app.routes` (2.2). |

The existing quickstart tests keep passing with the new quickstart lines (6.5): the
ingest body is still the first `-d '{…}'`, and the only URLs are
`http://127.0.0.1:8787/…` and the Apple URL.

### 6.5 README and docs text to write

README:

1. **How it works.** After the stage list, a paragraph headed **Asking again**:
   `POST /episodes/{key}/analyze` or `earmark reanalyze <key> --prompt "…"` runs a new
   prompt on an episode that is already transcribed. It downloads and transcribes
   nothing, contacts neither Apple nor the feed, and adds a section to the note, with the
   entity notes and the search index updated. `GET /episodes` or `earmark episodes` lists
   the stored episodes with their keys and the prompts already run. A prompt already run
   with the same `EARMARK_LLM_MODEL` makes no LLM call, and the job only regenerates the
   note. Sharing the episode again from the Shortcut with a new question also works; it
   looks the episode up again but does not re-download it.
2. Replace the "Planned:" line with `Planned: a weekly digest ([docs/PLAN.md](docs/PLAN.md)).`
3. **Quickstart.** Between `uv run earmark jobs` and `cat …`, add exactly:

   ```bash
   uv run earmark episodes
   uv run earmark reanalyze "$(ls "$demo/data/episodes")" --prompt "Who is interviewed?"
   uv run earmark worker --once
   curl -s http://127.0.0.1:8787/episodes -H "Authorization: Bearer $EARMARK_API_TOKEN"
   ```

   "What you see" gains: `earmark episodes` lists the fake episode with its prompt; the
   reanalyze prints a second job id, and `worker --once` prints it as `done`; `GET
   /episodes` shows both prompts; and the fake note has a section for each prompt.
   The implementer runs the whole quickstart verbatim in a fresh shell before handing
   over.
4. **Commands** table: add `episodes [--limit] [--offset] [--json]` ("List stored
   episodes, newest share first, with the prompts already run.") and `reanalyze <key>
   [--prompt]` ("Run a prompt on a stored episode: enqueue a job that starts at
   analysis."). Add one sentence after the table contrasting `analyze` (prints, writes
   no note) with `reanalyze` (a job; updates the note, entities, index, and notifies).
5. **HTTP API** sentence: `POST /ingest`, `GET /jobs/{id}`, `GET /jobs`, `GET /search`,
   `GET /episodes`, `POST /episodes/{key}/analyze`, and `GET|HEAD /healthz`.

`docs/shortcut.md`, section 5 "After you share", add a paragraph: **Asking another
question.** Share the same episode again and type the new question. Earmark does not
download or transcribe it again, and the note gains a section. From a computer,
`earmark reanalyze <key> --prompt "…"` (keys from `earmark episodes`) or `POST
/episodes/{key}/analyze` does the same without contacting Apple; see
[the README](../README.md). Do not touch the marked examples, the action list, or the
error table.

`docs/deploy.md`: no change.

## 7. Verification

```bash
uv sync
make check

# The new and changed tests
uv run pytest tests/test_runner.py tests/test_reanalyze.py tests/test_api_episodes.py \
  tests/test_episodes.py tests/test_ntfy.py tests/test_cli.py tests/test_docs.py \
  tests/test_artifacts.py tests/test_models.py -v

# The old "not supported yet" path is gone (prints nothing)
! grep -rn "not supported yet" src tests

# No SQLite, vault scan, or transcript parse in the listing (prints nothing)
! grep -nE "sqlite|open_database|EntityRegistry|locate_notes|read_transcript" \
  src/earmark/episodes.py

# Index test with the real LanceDB (needs the extra; sockets stay blocked)
uv sync --extra index
uv run --extra index pytest -m index tests/test_lancedb_index.py -k reanalyze -v
uv sync
```

Live smoke, with fake backends:

1. Run the README quickstart verbatim in a fresh shell (step 16's rule), and check the
   "What you see" list, including both prompts in the fake note and in `GET /episodes`.
2. Against the same running `earmark serve` (start it again with the quickstart's
   environment if it was stopped):

```bash
H="Authorization: Bearer $EARMARK_API_TOKEN"
key=$(ls "$demo/data/episodes")
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8787/episodes/0123456789abcdef0123/analyze \
  -H "$H" -H "Content-Type: application/json" -d '{}'                         # 404
mkdir -p "$demo/data/episodes/0123456789abcdef0124"
cp "$demo/data/episodes/$key/episode.json" "$demo/data/episodes/0123456789abcdef0124/"
# (a copied episode.json fails the key check on read, which is fine: the API only stats it)
curl -s -w '\n%{http_code}\n' -X POST http://127.0.0.1:8787/episodes/0123456789abcdef0124/analyze \
  -H "$H" -H "Content-Type: application/json" -d '{}'                         # 409
curl -s -i -X POST "http://127.0.0.1:8787/episodes/$key/analyze" \
  -H "$H" -H "Content-Type: application/json" -d '{"prompt": "Any numbers?"}' # 202, Location
curl -s -X POST "http://127.0.0.1:8787/episodes/$key/analyze" \
  -H "$H" -H "Content-Type: application/json" -d '{"prompt": " Any numbers? "}' # created false
curl -s -X POST "http://127.0.0.1:8787/episodes/$key/analyze" \
  -H "$H" -H "Content-Type: application/json" -d '{"promt": "x"}'             # 422, allowed: prompt
uv run earmark worker --once                                                   # job … done
uv run earmark episodes                                                        # three prompts
curl -s "http://127.0.0.1:8787/episodes?limit=1&offset=0" -H "$H"   # total 1: copy left out
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8787/episodes?limit=201" -H "$H"  # 422
```

The copied `episode.json` in the 409 check is listed by `GET /episodes` only if it
reads back. It does not: `read_episode` refuses a key mismatch, so the listing leaves it
out with a WARNING. That also exercises the damaged-artifact path. Remove the directory
afterwards.

Not verifiable here: real ntfy delivery of the new titles (the `network` ntfy test is
not extended), multi-worker races (5.5), and the container.

## 8. Deviations

1. **`earmark episodes` is added.** The brief names only `earmark reanalyze`. Without a
   listing command the CLI user has to `ls data/episodes` to find a key, with no titles.
   The command is a thin printer over the same `list_episodes` that `GET /episodes`
   uses. It mirrors the existing pairs `jobs`/`GET /jobs` and `search`/`GET /search`.
2. **"date" is `published_at`** (a full timestamp with its offset, or `null`), plus
   `shared_at`, rather than a bare date. A date needs a time zone, and the API has no
   reason to choose one. The CLI shows the date in `EARMARK_TZ`, as the note does.
3. **"the prompts already run" is `analyses`**, a list of `{prompt, prompt_hash, model,
   created_at}`, newest first. The prompt alone is ambiguous, because the same prompt
   under another model is a different analysis and a new LLM call.
4. **The listing reads artifacts, not the step 13 `episodes` table** (2.1, 5.3). That
   table is derived, lacks prompt text, is empty under the fake store, and omits
   untranscribed episodes.
5. **Offset pagination, not a cursor.** A keyset cursor would need the sort tuple encoded
   in an opaque, validated token. Offset with "newest share first" and no deletions can
   only repeat an entry, never skip one (5.3), which is enough for a personal list.
6. **A repeated prompt always enqueues a job.** It makes no LLM call, but it regenerates
   the note and re-checks the index (5.2), so the route doubles as "restore this note".
   The API does not predict cache hits (2.3).
7. **Two new doc-driven tests** (D1 and D2) bind the README's command table and route
   list to the code. Step 16 left both unchecked, and this step changes both.
8. **The architecture was edited with this plan**:
   - Domain models: `ReanalyzeRequest`.
   - Module layout: `episodes.py`.
   - Runner and worker: the reanalyze order and the multi-worker caveat.
   - A new "Re-running a prompt" section.
   - HTTP API table: the two routes replace the placeholder row.
   - Notifying: the reanalyze titles and lines.
   - Jobs dedupe: normalised prompts.
   - Testing: the doc tests now also cover the command table and route list.
9. **`describe_validation_errors` gains keyword parameters** with defaults equal to
   today's behaviour, so the existing callers and tests are unchanged.

## 9. Out of scope

- The weekly digest (step 18).
- A Shortcut for reanalyze. It would need a key lookup on the phone; re-sharing with a
  new question already works (6.5).
- `GET /episodes/{key}` (one episode's detail), filtering or searching the list, and note
  paths in the list (they need a vault scan; job results carry them).
- Deleting analyses or episodes, and pruning the analysis cache.
- Fixing the multi-worker race in 5.5.
- Predicting cache hits, a `--wait` flag, or progress for reanalyze jobs.
- Any change to the analyzer, prompts, schema, `ANALYSIS_SCHEMA_VERSION`, the vault
  store, the indexer, the job store, or the database schema.
