# Step 05: Pipeline runner and worker

## 1. Outcome

The walking skeleton runs. `earmark worker` claims jobs from the SQLite queue and runs
each through `Pipeline` (resolve, fetch, transcribe, analyze, store, index, notify),
heartbeating while it works and releasing the job back to `queued` on SIGTERM or
SIGINT. `pipeline/factory.py` is the only place that builds backends. In this step
only `fake` (and `none` for index and notify) are built, and every other backend name
is a configuration error at startup that names the setting. `earmark submit <url>
[--prompt]` enqueues through the same path as `POST /ingest`, and `earmark jobs` lists
jobs. With every backend set to `fake`, `earmark serve`, a `curl` ingest, and
`earmark worker --once` leave the job `done`, with the episode artifacts on disk and a
note in the vault.

Two small contract additions support this: `Backends` (the set of stage
implementations) in `pipeline/stages.py`, and a per-episode `shared.json` artifact that
keeps the first share time, so a note's `shared_at` does not move when the episode is
shared again with a new prompt.

## 2. Findings

All checks ran on 2026-11-20 in this sandbox against the locked environment (Python
3.13, typer 0.29.2, click 8.5.1, pytest 9.2.0, pytest-socket 0.8.1, ruff 0.17.6). Probe
files lived in the scratchpad and were run with
`uv run pytest -c pyproject.toml --rootdir . <file>`, so the project's addopts
(`--disable-socket`, `filterwarnings = error`) applied. No package was installed.

### 2.1 Signals under pytest

- pytest runs tests in the main thread, so `signal.signal` works inside a test.
  `threading.current_thread() is threading.main_thread()` held.
- A SIGTERM handler that raises a `BaseException` subclass, triggered by
  `os.kill(os.getpid(), signal.SIGTERM)` followed by `time.sleep(5)`, raised out of the
  sleep in well under a second. (PEP 475 retries `sleep` after a handler returns, but
  not when the handler raises.) Restoring the previous handler with `signal.signal`
  worked, and `signal.getsignal` returned the original object.
- A handler that only sets a `threading.Event`, with the signal sent from a
  `threading.Timer` thread while the main thread was in `event.wait(5)`, woke the wait
  after about 0.2 s. So an idle worker that waits on its stop event exits promptly.
- A handler raising inside an open `BEGIN IMMEDIATE` on an `autocommit=True` sqlite3
  connection left `conn.in_transaction` true, and `ROLLBACK` then succeeded.
  `db.Database.transaction()` already rolls back on `BaseException`, so a shutdown
  exception raised during a `JobStore` call leaves no half-written row.

### 2.2 Threads and `filterwarnings = error`

An exception that escapes a thread's target becomes
`PytestUnhandledThreadExceptionWarning`, which `filterwarnings = error` turns into a
test failure. The heartbeat thread must catch every `Exception` itself and log it.

### 2.3 typer

typer 0.29.2 accepts `Literal[...] | None` as an option type and rejects other values
with exit code 2 ("'bogus' is not one of ..."). `typer.Option(min=1, max=500)` rejects
`--limit 0` with exit code 2. `earmark jobs --status` can use `models.JobStatus`
directly.

### 2.4 Markdown formatting

`uv run ruff format --check docs/` reports the Markdown files under `docs/` as
formatted, so ruff formats Python code blocks inside them. The snippets in this plan
were checked with the same command.

### 2.5 mypy on the planned signatures

A scratch module with the shapes in sections 4.6 to 4.8 passed `mypy --strict` with the
project config: a `@dataclass(frozen=True, slots=True)` with Protocol-typed fields, a
PEP 695 `type Builder[T] = Callable[[BuildContext], T]` alias, `Final[Mapping[str,
Builder[Indexer | None]]]` holding a `lambda _: None`, and a keyword parameter
`interruptible: Callable[[], AbstractContextManager[None]] = nullcontext` called with a
`@contextmanager` method.

### 2.6 What the existing code gives the runner (and what it does not)

- `JobStore` (step 03) has every call the runner needs: `claim_next`, `heartbeat`,
  `set_stage`, `set_episode_key`, `complete`, `fail(transient=, retry_after_s=)`,
  `skip`, `release`, `add_warning` (works on terminal jobs, ignores exact repeats),
  `requeue_stale` (returns changed jobs), `get`. Fenced calls raise `LeaseLostError`.
  `heartbeat` and `set_stage` both refresh `heartbeat_at`.
- `fail` stores the error text unchanged; the runner owns the wording (step 03, 5.4).
- `ArtifactStore.read_transcript(key)` returns `None` when absent and raises
  `PermanentError` for a corrupt or mismatched file. `list_analyses(key)` returns
  analyses sorted by `(created_at, prompt_hash)`.
- The analyzer's cache key is `prompt_hash(prompt, model)`, and only the analyzer knows
  its model (`FakeAnalyzer.model == "fake-llm"`; the real one will use
  `EARMARK_LLM_MODEL`). The runner therefore cannot skip the analyzer call on a cache
  hit without a Protocol change; it calls the analyzer, which returns its cache without
  an LLM call. `FakeAnalyzer.computed` counts real work (step 02, 2.6, anticipated this).
- `FakeAudioFetcher` and `FakeTranscriber` do not delete audio (that is step 08's
  backend behaviour), so `audio.mp3` stays after a fake run.
- `FakeAnalyzer` defaults to a fixed clock (`FAKE_NOW`). The factory passes a real
  clock so analyses made by a running service get real `created_at` values.
- `tests/test_models.py::test_round_trip_covers_every_model` requires every model in
  `earmark.models` to appear in its `CASES` list. A new model must be added there.
- `tests/test_cli.py::test_cli_import_is_light` requires `import earmark.cli` to load
  no fastapi, starlette, or uvicorn. The new commands import the pipeline lazily,
  inside the command functions, as `serve` does.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/pipeline/stages.py` | Add the `Backends` frozen dataclass. |
| `src/earmark/pipeline/runner.py` | New. `Pipeline`, `describe_error`, `MAX_ERROR_CHARS`. |
| `src/earmark/pipeline/factory.py` | New. Backend registries, `build_backends`, `build_worker`. |
| `src/earmark/pipeline/worker.py` | New. `Worker`, `Heartbeat`, `Shutdown`, `heartbeat_interval`. |
| `src/earmark/models.py` | Add `ShareRecord`. |
| `src/earmark/artifacts.py` | Add `share_path`, `read_share`, `record_share`. |
| `src/earmark/fakes.py` | Add `FakeBackends.backends()`. |
| `src/earmark/log.py` | Add `redact(text, secrets)`; `RedactingFormatter` may use it. |
| `src/earmark/cli.py` | Add `worker [--once]`, `submit <url> [--prompt]`, `jobs [--limit] [--status] [--json]`. |
| `tests/pipeline_env.py` | New helper module (not a test file): builds a clock, store, fakes, pipeline, and worker in `tmp_path`. |
| `tests/test_runner.py` | New. |
| `tests/test_worker.py` | New. |
| `tests/test_factory.py` | New. |
| `tests/test_e2e.py` | New. API ingest, then `worker.run_once()`. |
| `tests/test_cli.py` | Add tests for the three commands. |
| `tests/test_artifacts.py` | Add share-record tests. |
| `tests/test_models.py` | Add `ShareRecord` to `CASES`. |
| `tests/test_fakes.py` | Add a test for `FakeBackends.backends()`. |
| `tests/test_log.py` | Add a test for `redact`. |
| `docs/ARCHITECTURE.md` | Runner and worker contract, `shared.json`, `Backends`. See section 8. |

No new dependency and no new setting. `.env.example` is unchanged.

## 4. Interfaces

### 4.1 `earmark.pipeline.stages.Backends`

```python
@dataclass(frozen=True, slots=True)
class Backends:
    resolver: Resolver
    fetcher: AudioFetcher
    transcriber: Transcriber
    analyzer: Analyzer
    store: Store
    indexer: Indexer | None  # None when EARMARK_INDEX_BACKEND=none
    notifier: Notifier | None  # None when EARMARK_NOTIFY_BACKEND=none
```

No defaults: every construction names all seven. Tests swap one stage with
`dataclasses.replace(backends, transcriber=...)`.

### 4.2 `earmark.models.ShareRecord`

```python
class ShareRecord(Model):
    episode_key: EpisodeKey
    first_shared_at: AwareDatetime
```

### 4.3 `earmark.artifacts.ArtifactStore` (added methods)

```python
def share_path(self, key: str) -> Path: ...  # episodes/<key>/shared.json


def read_share(self, key: str) -> ShareRecord | None: ...


def record_share(self, key: str, shared_at: datetime) -> datetime: ...
```

- `read_share`: `None` when absent; `PermanentError` (`corrupt artifact: …`) when the
  file does not validate or its `episode_key` differs from `key`, like the other reads.
- `record_share`: raises `ValueError` for a naive `shared_at`. Reads the record; if it
  is absent, or `shared_at` is strictly earlier than `first_shared_at`, writes
  `ShareRecord(episode_key=key, first_shared_at=shared_at)` atomically (same
  `_write_json` as the other artifacts). Returns the earliest of the two. When the
  existing record is earlier or equal, nothing is written (the file's bytes do not
  change).

### 4.4 `earmark.fakes.FakeBackends.backends`

```python
def backends(self) -> Backends: ...
```

Returns a `Backends` holding this instance's seven fakes (indexer and notifier
included, never `None`). `fakes.py` now imports `Backends` from
`earmark.pipeline.stages`, which imports only `earmark.models`, so the light-import
test in `test_models.py` still holds.

### 4.5 `earmark.log.redact`

```python
def redact(text: str, secrets: Iterable[str]) -> str: ...
```

Replaces every non-empty secret with `config.REDACTED`, longest first (the same rule
as `RedactingFormatter`, which may be refactored to call it).

### 4.6 `earmark.pipeline.runner`

```python
MAX_ERROR_CHARS: Final = 500


def describe_error(exc: BaseException, secrets: Iterable[str] = ()) -> str: ...


class Pipeline:
    def __init__(
        self,
        backends: Backends,
        artifacts: ArtifactStore,
        jobs: JobStore,
        *,
        secrets: Sequence[str] = (),
    ) -> None: ...

    def run(
        self,
        claim: Claim,
        *,
        interruptible: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> Job | None: ...

    def notify(self, job: Job, episode: Episode | None) -> None: ...

    def load_episode(self, job: Job) -> Episode | None: ...
```

`describe_error(exc, secrets)` builds the text stored in `job.error` and in warnings:

- For an `EarmarkError`: `exc.message`.
- For anything else:
  `f"unexpected error: {type(exc).__name__}: {message} (at {file}:{line} in {func})"`,
  where `message` is `str(exc)` and the location is the last entry of
  `traceback.extract_tb(exc.__traceback__)` with `file` reduced to its basename
  (`Path(fs.filename).name`). Omit `: {message}` when `str(exc)` is empty, and omit the
  location when there is no traceback.
- Then, in this order: collapse all whitespace runs (including newlines) to one space
  and strip; redact `secrets` with `log.redact`; cap at `MAX_ERROR_CHARS` characters,
  replacing the tail with `…` so the result is at most `MAX_ERROR_CHARS` long. If the
  result is empty, use `type(exc).__name__` (so `JobStore.fail` and `skip`, which
  reject blank text, never see it).

`Pipeline.run(claim, interruptible=...)` runs one claimed job to an outcome and
returns the job as the store now holds it: `done`, `failed`, `skipped`, or `queued`
(a transient failure with attempts left). It returns `None` if the lease was lost
(section 5.6). It never catches `BaseException` that is not an `Exception`, so the
worker's `Shutdown` and `KeyboardInterrupt` pass through. `interruptible` is a
context-manager factory the worker supplies; the runner enters it around the stage
work only (section 5.5).

`Pipeline.notify(job, episode)` calls the notifier (if any) and never raises for a
notifier problem (section 5.4). `Pipeline.load_episode(job)` reads `episode.json` for
`job.episode_key`, returning `None` when the key is unset, the file is absent, or it
is unreadable (`PermanentError` or `OSError`). The worker uses both for jobs that
`requeue_stale` failed.

### 4.7 `earmark.pipeline.worker`

```python
HEARTBEAT_MAX_S: Final = 60.0


class Shutdown(BaseException):
    """Raised in the main thread by the signal handler while a stage runs."""


def heartbeat_interval(lease_seconds: float) -> float: ...  # min(60.0, lease_seconds / 4)


class Heartbeat:
    def __init__(self, jobs: JobStore, claim: Claim, interval_s: float) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...

    @property
    def beats(self) -> int: ...  # heartbeats that succeeded


class Worker:
    def __init__(
        self,
        jobs: JobStore,
        pipeline: Pipeline,
        *,
        poll_seconds: float,
        heartbeat_seconds: float,
    ) -> None: ...

    @property
    def stopping(self) -> bool: ...

    def request_stop(self) -> None: ...

    def handle_signals(self) -> AbstractContextManager[None]: ...

    def run_once(self) -> Job | None: ...

    def run_forever(self) -> None: ...
```

- `Heartbeat`: `__enter__` starts a daemon thread named `heartbeat-<first 8 of id>`
  that loops `while not stop.wait(interval_s): jobs.heartbeat(claim)`. On
  `LeaseLostError` it logs at DEBUG and ends (this is normal once the job is terminal).
  On any other `Exception` it logs a WARNING with the traceback and keeps going.
  `__exit__` sets the stop event and joins the thread. `interval_s` must be > 0
  (`ValueError`).
- `Worker.request_stop()` sets the stop flag. It never raises.
- `Worker.handle_signals()`: a context manager. In the main thread it installs one
  handler for `SIGTERM` and `SIGINT` and restores the previous handlers on exit. Off the
  main thread it does nothing (logs at DEBUG). The handler sets the stop flag; if a job
  is inside its interruptible window it also clears the window flag and raises
  `Shutdown` (section 5.5).
- `Worker.run_once()`: one poll. Returns `None` if the worker is stopping or no job was
  claimable; otherwise the job's state after this run (section 5.7).
- `Worker.run_forever()`: under `handle_signals()`, loops until stopping: `run_once()`;
  when it returns `None`, wait `poll_seconds` on the stop event. A `sqlite3.Error`
  escaping an iteration is logged at ERROR (with traceback) and followed by the same
  wait; anything else propagates (the process supervisor restarts the worker).

### 4.8 `earmark.pipeline.factory`

```python
@dataclass(frozen=True, slots=True)
class BuildContext:
    settings: Settings
    artifacts: ArtifactStore
    clock: Clock


type Builder[T] = Callable[[BuildContext], T]

RESOLVERS: Final[Mapping[str, Builder[Resolver]]]
FETCHERS: Final[Mapping[str, Builder[AudioFetcher]]]
TRANSCRIBERS: Final[Mapping[str, Builder[Transcriber]]]
ANALYZERS: Final[Mapping[str, Builder[Analyzer]]]
STORES: Final[Mapping[str, Builder[Store]]]
INDEXERS: Final[Mapping[str, Builder[Indexer | None]]]
NOTIFIERS: Final[Mapping[str, Builder[Notifier | None]]]


def build_backends(
    settings: Settings, artifacts: ArtifactStore, *, clock: Clock = utc_now
) -> Backends: ...


def build_worker(
    settings: Settings, *, jobs: JobStore | None = None, clock: Clock = utc_now
) -> Worker: ...
```

This shape type-checks (section 2.5). Registry keys are the settings' `Literal` values; mapping
`Settings` field to registry:

| Setting field | Registry | Built in this step |
|---|---|---|
| `resolver_backend` | `RESOLVERS` | `fake` → `FakeResolver()` |
| `fetch_backend` | `FETCHERS` | `fake` → `FakeAudioFetcher(ctx.artifacts)` |
| `asr_backend` | `TRANSCRIBERS` | `fake` → `FakeTranscriber(ctx.artifacts)` |
| `llm_provider` | `ANALYZERS` | `fake` → `FakeAnalyzer(ctx.artifacts, clock=ctx.clock)` (model stays `fake-llm`) |
| `store_backend` | `STORES` | `fake` → `FakeStore(settings.vault_dir, settings.vault_subdir)` |
| `index_backend` | `INDEXERS` | `fake` → `FakeIndexer()`, `none` → `None` |
| `notify_backend` | `NOTIFIERS` | `fake` → `FakeNotifier()`, `none` → `None` |

`build_backends` looks up all seven settings, collects one message per name that has no
builder, and raises `config.ConfigError(messages)` listing all of them at once.
Message format (the value is a backend name, never a secret):

```
EARMARK_ASR_BACKEND: backend 'faster_whisper' is not built into this version of earmark (choose one of: fake)
```

The variable is `config.env_var(field_name)`; the choices are the registry's keys,
sorted, joined with `, `. Then it calls every builder and returns `Backends`. Later
steps add their backend by adding one registry entry (importing heavy modules inside
the builder function, never at module top).

`build_worker(settings, jobs=None, clock=utc_now)`:
1. `artifacts = ArtifactStore(settings.data_dir)`.
2. `backends = build_backends(settings, artifacts, clock=clock)`. A `ConfigError` here
   happens before the database or data directory is touched.
3. `jobs = jobs or open_job_store(settings, clock=clock)`.
4. `pipeline = Pipeline(backends, artifacts, jobs, secrets=settings.secret_values())`.
5. Return `Worker(jobs, pipeline, poll_seconds=settings.worker_poll_seconds,
   heartbeat_seconds=heartbeat_interval(settings.job_lease_seconds))`.

It logs one INFO line naming the seven backend choices (names only).

### 4.9 CLI

All three commands load settings with the existing `_load_or_exit()` (exit 2 on
`ConfigError`), call `configure_logging(settings.log_level, settings.secret_values())`,
and import pipeline, ingest, and jobs modules inside the function body.

`earmark worker [--once]`
- Builds the worker with `factory.build_worker(settings)`. `ConfigError` → its text on
  stderr, exit 2. `DatabaseSetupError`, `sqlite3.Error`, or `OSError` while opening the
  store → `earmark worker: cannot open the job store: {exc}` on stderr, exit 1 (same as
  `serve`).
- Without `--once`: `worker.run_forever()`; exit 0 when it returns (after a signal).
- With `--once`: `with worker.handle_signals(): job = worker.run_once()`. Prints one
  line on stdout: `no job ready` when `job is None`, else `job {job.id} {job.status}`.
  Exit 0 either way. A `sqlite3.Error` → `earmark worker: job store error: {exc}` on
  stderr, exit 1.

`earmark submit URL [--prompt TEXT]`
- Builds `IngestRequest(url=URL, prompt=TEXT)`. A pydantic `ValidationError` →
  stderr `earmark submit: ` followed by the errors' `msg` values joined with `; `
  (from `exc.errors(include_input=False, include_url=False)`), exit 2.
- Opens the store with `open_job_store(settings)` (errors as for `worker`, prefix
  `earmark submit:`), then `ingest.enqueue_ingest(store, request)`.
  `UnsupportedUrlError` → stderr `earmark submit: {exc.message}`, exit 2.
- Prints `{job.id} {job.status}` on stdout, followed by ` (existing job)` when
  `Enqueued.created` is false. Exit 0.

`earmark jobs [--limit N] [--status S] [--json]`
- `--limit`: `int`, default 20, `min=1`, `max=jobs.LIST_LIMIT_MAX`. `--status`:
  `JobStatus | None`. Reads `store.list_recent(limit, status=status)`.
- `--json`: prints `{"jobs": [...]}` where each item is `job.model_dump(mode="json")`,
  as one `json.dumps(..., ensure_ascii=False)` line. Same shape as `GET /jobs`.
- Otherwise one line per job, fields separated by two spaces:
  `{id}  {status}  {stage or "-"}  {attempts}  {created_at:%Y-%m-%dT%H:%M:%SZ}  {url or episode_key}`
  (`created_at` converted to UTC), then `  error: {error}` on its own line when set, and
  `  warning: {w}` per warning. No jobs: prints `no jobs`. Every printed field passes
  through a sanitizer that replaces each non-printable character
  (`not c.isprintable()`) with `?`, so a hostile error string cannot drive the terminal.

## 5. Behaviour and edge cases

### 5.1 Stage order in `Pipeline.run`

For an `ingest` job (`claim.job`):

1. `set_stage("resolve")`; `episode = resolver.resolve(job.url)`.
2. `artifacts.write_episode(episode)` (every run: the resolver is not cached and may
   return fresher metadata); `jobs.set_episode_key(claim, episode.key)`.
3. `shared_at = artifacts.record_share(episode.key, job.shared_at)`.
4. `transcript = artifacts.read_transcript(episode.key)`. If it is `None`:
   `set_stage("fetch")`, `audio = fetcher.fetch(episode)`, `set_stage("transcribe")`,
   `transcript = transcriber.transcribe(episode, audio)`. If it is not `None`, fetch and
   transcribe are not called and no stage is set for them (log at INFO that the
   transcript is cached).
5. `set_stage("analyze")`; `analysis = analyzer.analyze(episode, transcript, job.prompt)`.
6. `analyses = artifacts.list_analyses(episode.key)`. If none of them has
   `analysis.prompt_hash`, append `analysis` and re-sort by
   `(created_at, prompt_hash)` (defensive: a backend that forgot to write still gets a
   complete note). Pass `tuple(analyses)`.
7. `set_stage("store")`; `stored = store.store(episode, transcript, analyses, shared_at)`.
8. If `backends.indexer` is not `None`: `set_stage("index")`, then
   `indexer.index(episode, transcript, analyses, stored)` inside `try`/`except
   Exception`. A failure logs a WARNING with the traceback and adds the warning
   `f"index failed: {describe_error(exc, secrets)}"`. The job continues.
9. Warnings: after each of steps 4, 5, and 7, add every entry of `transcript.warnings`,
   `analysis.warnings` (the analysis for this job's prompt only), and `stored.warnings`
   with `jobs.add_warning(job.id, w)`, in that order. Repeats are ignored by the store.
10. `jobs.complete(claim, JobResult(note_path=stored.note_path,
    transcript_note_path=stored.transcript_note_path,
    analysis_hash=analysis.prompt_hash))`.
11. `self.notify(done_job, episode)`.

Stages are called positionally (the Protocols are positional-only). Steps 1 to 9 run
inside `with interruptible():`; steps 10 and 11, and every failure write in 5.2, run
outside it.

Two sanity checks after step 4 and step 5: a transcript whose `episode_key` differs
from `episode.key`, or an analysis whose `episode_key` differs, raises
`PermanentError("transcriber returned a transcript for another episode")` (or the
analyzer equivalent). They cost nothing and stop a buggy backend from writing a note
for the wrong episode.

A `reanalyze` job raises `PermanentError("reanalyze jobs are not supported yet")` at
the start of the stage work, before any stage runs. Step 17 replaces this. No code path
in this step creates such a job.

`job.stage` is observability only: on a failed or skipped job it names the stage that
was running when the job stopped; on a `done` job it is the last stage entered (`index`,
or `store` when indexing is off). The `notify` stage value is not set by this step,
because notification happens after the job is terminal and fenced calls no longer
apply.

### 5.2 Exception mapping

Raised from the stage work (5.1 steps 1 to 9, excluding the guarded index call):

| Exception | Store call | Notified |
|---|---|---|
| `SkipEpisode` | `skip(claim, describe_error(exc))` | yes |
| `PermanentError` | `fail(claim, describe_error(exc), transient=False)` | yes |
| `TransientError` | `fail(claim, describe_error(exc), transient=True, retry_after_s=exc.retry_after_s)` | only if the returned job is `failed` (attempts used up) |
| `LeaseLostError` | none | no (5.6) |
| any other `Exception` | `fail(claim, describe_error(exc), transient=True)` | only if `failed` |

`except` order matters: `LeaseLostError` (a `RuntimeError`) and the three
`EarmarkError` subclasses before `Exception`. `SkipEpisode`, `TransientError`, and
`PermanentError` are siblings, so their order among themselves does not.

Logging: `SkipEpisode` at INFO, `PermanentError` and `TransientError` at WARNING
(message only), an unexpected exception at ERROR with `exc_info=exc`. That ERROR
record is where the full traceback is recorded. The job's `error` carries the short
form from `describe_error`, including the raising frame's file, line, and function, so
`GET /jobs/{id}` points at the cause without the log. The `.message` of an
`EarmarkError` is shown as is (after whitespace collapsing, redaction, and the cap).

A failure in the store call itself (`sqlite3.Error` from `fail`, `skip`, or
`complete`) propagates out of `run`. The job stays `running` until its lease expires
and `requeue_stale` picks it up.

### 5.3 Caching and repeated shares

- Same episode, same prompt, after the first job is `done`: a new job is created (the
  dedupe only covers active jobs). The resolver runs again, the transcript is read from
  disk (fetcher and transcriber are not called at all), the analyzer is called and
  returns its cached file (`FakeAnalyzer.computed` does not change, so no LLM call),
  then store, index, and notify run again. The note is regenerated byte-identically.
- Same episode, new prompt: as above, except the analyzer computes a new analysis, and
  the store receives both analyses.
- `shared_at` passed to the store is the episode's first share time from
  `shared.json`, not the current job's, so it stays fixed across re-shares. A later job
  with an earlier explicit `timestamp` moves it earlier. Two workers racing on the same
  new episode could each write `shared.json`; the later write wins, which can lose the
  earlier of two near-simultaneous share times. With one worker this cannot happen.
- Switching a data directory from fake to real backends reuses the fake transcript,
  because the transcript cache has no model key. Smoke runs should use a scratch data
  directory. This is not new behaviour; it follows from the step 02 cache contract.

### 5.4 Notification

- The notifier runs once for each job that reaches `done`, `failed`, or `skipped` in
  this run, with the job as returned by `complete`, `fail`, or `skip` (so it carries the
  warnings added before the terminal write) and the episode when resolution got that
  far (else `None`).
- It does not run for a transient failure that was requeued, for a released job, or
  after a lost lease.
- `Pipeline.notify` catches any `Exception` from the notifier, logs a WARNING with the
  traceback, and adds `f"notify failed: {describe_error(exc, secrets)}"` to the job
  with `add_warning` (which works on terminal jobs). If `add_warning` itself raises,
  that is logged and swallowed. The job stays `done` (or `failed` or `skipped`).
- Jobs that `requeue_stale` turned `failed` (a crashed worker used up their attempts)
  are notified by the worker with `pipeline.load_episode(job)` as the episode.

### 5.5 Heartbeat and shutdown

- The worker holds a `Heartbeat(jobs, claim, heartbeat_seconds)` for the whole of
  `pipeline.run(...)`. With the default 900 s lease the interval is 60 s; with a short
  lease it is a quarter of it. `set_stage` also refreshes the heartbeat. The heartbeat
  measures that the worker process is alive, not that a stage is making progress: a
  stage that hangs forever keeps its job. Crashes are covered by `requeue_stale`.
- After `complete`/`fail`/`skip`, the heartbeat thread's next beat gets
  `LeaseLostError` and the thread ends quietly; `__exit__` ends it anyway.
- Interruptible window: the worker passes `interruptible=self._window` to
  `pipeline.run`. `_window()` raises `Shutdown` at once if the stop flag is already
  set, otherwise sets `self._interruptible = True` for the block and resets it in
  `finally`. The signal handler always sets the stop flag; if `_interruptible` is true
  it sets it false and raises `Shutdown`. A second signal during the release therefore
  only sets the flag again.
- So a signal during resolve, fetch, transcribe, analyze, store, or index raises
  `Shutdown` out of the stage. `Shutdown` is a `BaseException`, so the runner does not
  map it to a failure; atomic writes delete their temp files and `Database.transaction`
  rolls back on the way out. The worker catches it outside the heartbeat block, calls
  `jobs.release(claim)` (status `queued`, attempt undone, `run_after = now`; on
  `LeaseLostError` it reads the job with `get` instead), logs at INFO, and returns that
  job. `run_forever` then sees the stop flag and returns.
- A signal while the job is completing or notifying (outside the window) only sets the
  stop flag: the worker finishes that job, including its notification, and claims
  nothing more. A signal while idle wakes the poll wait (section 2.1) and the loop ends.
- Deployment note for step 16: the container stop timeout does not need to cover a full
  transcription, since the job is released, not finished. A `SIGKILL` (or a crash)
  leaves the job `running` until `JOB_LEASE_SECONDS` pass, then `requeue_stale`
  requeues it and the attempt counts.

### 5.6 Lease lost

Any fenced call (`set_stage`, `set_episode_key`, `complete`, `fail`, `skip`) can raise
`LeaseLostError` when another worker's `requeue_stale` took the job. The runner then
logs a WARNING (`lost the lease on job {id}; abandoning it`), does nothing else to the
job, does not notify, and returns `None`. Stage work already done stays on disk as
cache. The worker returns `jobs.get(job.id)` in that case.

### 5.7 `Worker.run_once`

1. If stopping, return `None` without touching the store.
2. `for job in jobs.requeue_stale(): if job.status == "failed":
   pipeline.notify(job, pipeline.load_episode(job))`. Requeued ones are only logged.
3. `claim = jobs.claim_next()`; if `None`, return `None`.
4. Log `job {id} claimed (attempt {n})`. Under `Heartbeat`, call
   `pipeline.run(claim, interruptible=self._window)`.
5. On `Shutdown`: release as in 5.5 and return the released job.
6. If `run` returned `None` (lease lost), return `jobs.get(claim.job.id)`; otherwise
   return the returned job. Log `job {id} {status} in {elapsed:.1f} s` at INFO.

`run_forever` logs `worker started` and `worker stopped` at INFO.

`run_once` never sleeps. `--once` therefore processes at most one job and exits.

### 5.8 Messages and secrets

- `job.error` and warnings are built only by `describe_error`, which redacts
  `settings.secret_values()` (API token, ASR key, LLM key, ntfy token). The Anthropic
  key lives outside `Settings` (architecture, configuration) and is not covered here;
  step 10 decides how to include it.
- Logs go through the existing `RedactingFormatter`.
- `earmark jobs` sanitizes non-printable characters on output (4.9). The API returns
  the stored text unchanged inside JSON, which is safe.

## 6. Tests

### Fixtures and helpers

`tests/pipeline_env.py` (a helper module like `tests/factories.py`, not collected):

```python
@dataclass
class PipelineEnv:
    clock: MutableClock
    jobs: JobStore
    fakes: FakeBackends
    pipeline: Pipeline
    worker: Worker
    vault: Path


def make_env(
    tmp_path: Path,
    *,
    max_attempts: int = 4,
    lease_s: float = 900.0,
    heartbeat_s: float = 60.0,
    backends: Backends | None = None,
) -> PipelineEnv: ...
```

It opens `open_database(tmp_path / "data" / "earmark.db")`, builds
`JobStore(db, clock=clock, max_attempts=..., lease=timedelta(seconds=lease_s))`,
`make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")`, a `Pipeline`
over `backends or fakes.backends()` with `secrets=("sentinel-secret-3f9a",)`, and
`Worker(jobs, pipeline, poll_seconds=0.01, heartbeat_seconds=heartbeat_s)`. Tests
enqueue with `jobs.enqueue(CANON, prompt=...)` where `CANON` is the canonical URL used
in `test_api.py` (`https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981`).

Test doubles defined in the test modules that need them (no new fixture files, nothing
to download):

- `BlockingTranscriber`: wraps a `FakeTranscriber`; `transcribe` runs a callback given
  by the test, then delegates.
- `SignalingStage`: a callable hook that runs `os.kill(os.getpid(), signal.SIGTERM)`
  then `time.sleep(5)`; used as the callback above.
- `RaisingNotifier`/`SignalingNotifier`: wrap `FakeNotifier` to raise or to send
  SIGTERM to the process, `time.sleep(0.05)`, then record completion.
- `CountingJobStore(JobStore)`: overrides `heartbeat` to call `super()`, count, and set
  a `threading.Event`; a variant whose `heartbeat` raises
  `sqlite3.OperationalError("disk I/O error")`.

Tests that install signal handlers use `worker.handle_signals()` (or `run_forever`),
which restores the previous handlers; each asserts afterwards that
`signal.getsignal(signal.SIGTERM)` is the handler it saw before. Tests that touch
logging handlers use the existing `restore_root_logging` fixture. Log assertions use
`caplog`.

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | End-to-end on fakes: `POST /ingest`, then `worker.run_once()`, leaves the job `done` with `episode.json`, `transcript.json`, `transcript.txt`, and an analysis file on disk, and the store called with the expected inputs | `test_e2e.py::test_ingest_then_run_once`: a `TestClient(create_app(settings, store=env.jobs))` with a token posts the share-sheet URL from `test_api.py` with a prompt; `env.worker.run_once()` returns a job with status `done`; `GET /jobs/{id}` shows `done`, `stage == "index"`, `error is None`, and `result == JobResult(note_path=f"Earmark/Podcasts/{key}.md", transcript_note_path=None, analysis_hash=prompt_hash(prompt, "fake-llm"))`. `episode.json`, `shared.json`, `transcript.json`, `transcript.txt`, and `analyses/<hash>.json` exist under `data/episodes/<key>/`, and `vault/Earmark/Podcasts/<key>.md` exists. `fakes.resolver.calls == [ResolveCall(CANON)]`. `fakes.store.calls == [StoreCall(episode, transcript, (analysis,), job.shared_at)]`, with the three values read back from `ArtifactStore`. `fakes.indexer.calls[0].stored` is that `StoreResult`. `fakes.notifier.calls` has one call whose job is `done` and whose episode is the resolved one. `test_e2e.py::test_run_once_empty_queue` (returns `None`, no stage called) |
| A2 | A second job for the same episode and prompt calls neither the transcriber nor the analyzer; the same episode with a new prompt calls only the analyzer | `test_runner.py::test_same_prompt_reuses_everything`: two jobs, same URL and prompt, the second enqueued after the first is `done`. After both: `fetcher.call_count == 1`, `transcriber.call_count == 1`, `analyzer.computed == 1` (the analyzer was asked and answered from its cache: no LLM work, section 8.3), `resolver.call_count == 2`, `store.call_count == 2`, and the note bytes after the second run equal those after the first. `test_runner.py::test_new_prompt_runs_only_analyzer`: first `prompt=None`, then `prompt="What papers are cited?"`. `fetcher.call_count == 1`, `transcriber.call_count == 1`, `analyzer.computed == 2`; the second `StoreCall.analyses` holds both analyses in `(created_at, prompt_hash)` order. `test_runner.py::test_cached_transcript_skips_fetch_even_without_audio`: delete `audio.mp3` after the first job; the second job still succeeds and the fetcher is not called |
| A3 | `SkipEpisode` ends the job as `skipped` with its reason and no retry | `test_runner.py::test_skip_episode`: `resolver.fail_next(SkipEpisode("subscriber-only episode"))`; `run_once` returns `skipped`, `error == "subscriber-only episode"`, `attempts == 1`, stage `resolve`, the notifier called once with that job and `episode is None`; after `clock.advance(7 * 3600)` the next `run_once` returns `None` |
| A4 | `TransientError` requeues | `test_runner.py::test_transient_requeues`: `transcriber.fail_next(TransientError("asr busy"))`; the job is `queued`, `attempts == 1`, `error == "asr busy"`, `run_after == clock.now + 60 s`, the notifier not called; `run_once` before the delay returns `None`; after `clock.advance(60)` it returns `done`. `test_transient_retry_after`: `TransientError("x", retry_after_s=600)` gives `run_after == now + 600 s`. `test_transient_exhausted_fails_and_notifies` (`max_attempts=2`, `fail_next(..., times=2)`): the second run returns `failed` and the notifier is called once, for that run |
| A5 | `PermanentError` fails | `test_runner.py::test_permanent_fails`: `analyzer.fail_next(PermanentError("bad transcript"))`; returns `failed`, `error == "bad transcript"`, stage `analyze`, `attempts == 1`, notified once with the episode; after advancing 7 h nothing is claimable |
| A6 | An unexpected exception is treated as transient with its traceback recorded | `test_runner.py::test_unexpected_exception_is_transient`: `fetcher.fail_next(ValueError("boom-7c1e"))`; the job is `queued` with `attempts == 1` and `run_after == now + 60 s`; `error` starts with `"unexpected error: ValueError: boom-7c1e (at "` and ends with `" in _enter)"` (the frame in `fakes.py` that raised; assert the `fakes.py:` part too); `caplog` has one ERROR record from `earmark.pipeline.runner` whose `exc_info[0] is ValueError` and whose formatted text contains `Traceback`; not notified. After `clock.advance(60)`, `run_once` returns `done` |
| A7 | Indexer and notifier failures leave the job `done` with a warning | `test_runner.py::test_index_failure_is_warning`: `indexer.fail_next(RuntimeError("index down"))`; `done`, `result` set, `warnings` contains a string starting with `"index failed: unexpected error: RuntimeError: index down"`; the notifier still ran and the job it received already carries that warning. `test_index_transient_is_warning` (a `TransientError` from the indexer is also only a warning; status `done`, no requeue). `test_notify_failure_is_warning`: `notifier.fail_next(RuntimeError("ntfy down"))`; `run_once` returns normally, `jobs.get(id)` is `done` with a warning starting `"notify failed: "`. `test_notify_failure_on_failed_job` (a permanent failure plus a failing notifier: the job stays `failed` with the warning) |
| A8 | The notifier runs for `done`, `failed`, and `skipped` | `test_runner.py::test_notifier_runs_for_each_terminal_status`, parametrized over a clean run, `PermanentError`, and `SkipEpisode`: exactly one notify call per job, with `call.job.status` equal to the stored status and `call.job == jobs.get(id)`. `test_worker.py::test_stale_failed_job_is_notified`: `max_attempts=1`, `lease_s=10`; claim a job by hand with `jobs.claim_next()` (a crashed worker), `set_episode_key` to a key whose `episode.json` was written, advance 11 s; `worker.run_once()` returns `None` (nothing left), the job is `failed` with the "worker stopped responding" error, and the notifier got it with the episode read from disk |
| A9 | The worker heartbeats during a long stage | `test_worker.py::test_heartbeat_keeps_long_stage_alive`: `CountingJobStore`, `lease_s=30`, `heartbeat_s=0.01`, and a `BlockingTranscriber` whose callback does `clock.advance(31)`, then waits (timeout 5 s) until two more heartbeats have completed, then calls `env.jobs.requeue_stale()` and records its result. The job ends `done`, the recorded `requeue_stale()` result is `[]`, and `beats >= 2`. Negative control in the same file, `test_without_heartbeat_long_stage_is_requeued`: `heartbeat_s=100` (no beat happens), the same callback without the wait; `requeue_stale()` returns the job, the runner then hits `LeaseLostError` and `run_once` returns the job as `queued` with the stale error, and the notifier and `complete` were not reached. `test_heartbeat_thread_survives_store_errors`: a store whose `heartbeat` raises `sqlite3.OperationalError`; the job still completes, a WARNING was logged, and no thread exception escaped (the suite's `filterwarnings = error` would fail the test). `test_heartbeat_unit`: `Heartbeat` with `interval_s=0.01` beats at least 3 times within 1 s, stops beating after `__exit__`, ends quietly after the claim is released, and rejects `interval_s=0` |
| A10 | On SIGTERM the worker either finishes the current job or releases it back to `queued` | `test_worker.py::test_sigterm_during_stage_releases`: the transcribe callback is `SignalingStage`; under `worker.handle_signals()`, `run_once()` returns within 2 s a job with `status == "queued"`, `attempts == 0`, `stage == "transcribe"`, `run_after == clock.now`; `transcript.json` does not exist; the notifier was not called; `worker.stopping` is true; the SIGTERM handler is restored after the block. A fresh worker on the same store then completes the job with `fetcher.computed == 1` (audio cache reused). `test_sigint_releases_too` (same with SIGINT). `test_sigterm_during_notify_finishes`: two jobs queued; the notifier sends SIGTERM, sleeps, records; `worker.run_forever()` returns; the first job is `done` and its notify call completed; the second job is still `queued` with `attempts == 0`. `test_sigterm_while_idle_exits`: empty queue, `poll_seconds=10`, a `threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))`; `run_forever()` returns within 2 s. `test_stop_requested_before_claim` (`request_stop()`, then `run_once()` returns `None` and the job stays `queued` with `attempts == 0`). `test_handle_signals_off_main_thread` (entered inside a `threading.Thread`: no exception, main-thread handlers unchanged) |
| A11 | An unknown backend name fails at startup with a message naming the setting | `test_factory.py::test_unbuilt_backend_names_setting`, parametrized over each of the seven fields set to its first non-fake, non-none `Literal` value (others `fake`): `build_backends` raises `ConfigError` whose `errors` has exactly one line, starting with that field's `env_var` and containing the backend name and `choose one of: fake`. `test_default_settings_list_all_unbuilt` (defaults: seven lines, one per variable). `test_cli.py::test_worker_unbuilt_backend_exits_2` (`EARMARK_ASR_BACKEND=faster_whisper`, others fake: exit 2, stderr names `EARMARK_ASR_BACKEND`, `./data` not created). `test_cli.py::test_worker_invalid_backend_name_exits_2` (`EARMARK_ASR_BACKEND=whisperx-sentinel`: exit 2 from settings validation, stderr names the variable and not the value) |
| A12 | Live smoke: with every backend `fake`, `earmark serve`, `curl` an ingest, `earmark worker --once`, and the job is `done` via `GET /jobs/{id}` | Run by the verifier, section 7 |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | Stages and side effects run in the documented order, and every stage sees the right inputs | `test_runner.py::test_stage_order_and_inputs`: a `CountingJobStore` variant that records `set_stage` values gives `["resolve", "fetch", "transcribe", "analyze", "store", "index"]`; `transcriber.calls[0].audio == artifacts.find_audio(key)`; `analyzer.calls[0].prompt == job.prompt`; `job.episode_key == key` after resolve. `test_stage_order_cached_transcript` gives `["resolve", "analyze", "store", "index"]`. `test_index_none_skips_index` (`indexer=None` via `dataclasses.replace`: stage ends `store`, job `done`) and `test_notifier_none` (no error) |
| B2 | Stage warnings reach the job, once each | `test_runner.py::test_stage_warnings_copied`: fakes built with `FakeTranscriber(..., warnings=("no segments",))` and `FakeAnalyzer(..., warnings=("dropped 2 timestamps",))`; the done job's `warnings == ("no segments", "dropped 2 timestamps")`. A second job for the same episode reports the same warnings again (cache hit), without duplicates within the job |
| B3 | The note's `shared_at` is the first share time | `test_runner.py::test_shared_at_is_first_share`: job 1 at `T0`, job 2 (new prompt) at `T0 + 1 h`: both `StoreCall.shared_at == T0`. A third job enqueued with `shared_at=T0 - 1 day` moves it to that time. `test_artifacts.py::test_record_share` (absent → writes and returns the time; later time → returns the earlier and leaves the file bytes unchanged; earlier time → rewrites; naive datetime → `ValueError`; mismatched key in the file → `PermanentError`; `read_share` absent → `None`). `test_models.py::CASES` gains a `ShareRecord` |
| B4 | A lost lease abandons the job without touching it | `test_runner.py::test_lease_lost_mid_run`: `heartbeat_s=100`, a transcribe callback that advances the clock past the lease and calls `jobs.requeue_stale()`; `pipeline.run(claim)` returns `None`; the job is `queued` with the stale error, `result is None`; the analyzer, store, and notifier were not called; a WARNING mentions the job id |
| B5 | Error text is short, single-line, and free of configured secrets | `test_runner.py::test_describe_error`: `EarmarkError` message returned as is; a multi-line `ValueError` message is collapsed to one line; an exception with empty `str()` gives `unexpected error: KeyError (at ...)`-style text without `": "` after the type; a 2000-character message is capped at exactly `MAX_ERROR_CHARS` and ends with `…`; an exception never raised (no traceback) has no `(at ...)`. `test_unexpected_error_redacts_secrets`: a stage raises `ValueError("token sentinel-secret-3f9a leaked")`; `job.error` contains `**********` and not the sentinel. `test_log.py::test_redact` (longest-first replacement, empty secrets ignored) |
| B6 | Defensive checks on backend output | `test_runner.py::test_transcript_for_other_episode_fails` (a transcriber double returning another key's transcript: `failed`, permanent, message names the transcriber). `test_analysis_not_written_still_stored` (an analyzer double that returns but does not write: the store still receives it). `test_reanalyze_job_fails_permanently` (`jobs.enqueue_reanalyze(key)`: `failed`, `error == "reanalyze jobs are not supported yet"`, no stage called) |
| B7 | The factory builds the configured fakes and the worker runs them | `test_factory.py::test_all_fake_builds_fakes` (each `Backends` field is the expected `Fake*` type; `none` gives `None` for index and notify). `test_registries_match_settings` (every registry's keys are a subset of the matching `Literal`'s `typing.get_args`, and `"fake"` is in each). `test_fake_analyzer_uses_real_clock` (a `clock` passed to `build_backends` becomes the analyzer's clock). `test_build_worker_runs_a_job`: all-fake `Settings(_env_file=None, data_dir=..., vault_dir=..., ...)`, `enqueue_ingest` a share URL, `build_worker(settings).run_once()` returns `done` and the note exists under `vault_dir/Earmark/Podcasts/`. `test_build_worker_config_error_touches_nothing` (unbuilt backend: `ConfigError`, data dir not created). `test_pipeline_modules_import_light` (subprocess `import earmark.pipeline.runner, earmark.pipeline.worker, earmark.pipeline.factory` loads none of `fastapi`, `starlette`, `uvicorn`, `httpx2`, `anthropic`, `faster_whisper`, `lancedb`, `fastembed`) |
| B8 | `earmark worker`, `submit`, and `jobs` work end to end | `test_cli.py` (all with `restore_root_logging` and every backend env var set to `fake`, `EARMARK_VAULT_DIR` set): `test_submit_then_worker_once` (`submit <share url> --prompt q` prints `<32 hex> queued`; `worker --once` prints `job <id> done`; `jobs --json` parses to one job with status `done`). `test_worker_once_empty` (`no job ready`, exit 0). `test_worker_runs_forever_without_once` (monkeypatch `Worker.run_forever` to record the call; exit 0). `test_worker_store_error_exits_1` (`EARMARK_DATA_DIR` is a file). `test_submit_dedupe` (second identical submit prints the same id and `(existing job)`). `test_submit_rejects_url`, parametrized over the show link and `not a url`: exit 2, stderr has the `MESSAGES` text, nothing enqueued. `test_submit_rejects_long_prompt` (exit 2, message from `prompt_too_long`). `test_jobs_text_output` (two jobs: lines start with their ids, newest first; `--status done` filters; `--limit 1` gives one). `test_jobs_sanitizes_control_chars` (a job failed with an error containing `\x1b[31m` and `\n`: the output has `?` in their place and no ESC byte). `test_jobs_empty` (`no jobs`). `test_cli_import_is_light` keeps passing |
| B9 | `FakeBackends.backends()` | `test_fakes.py::test_fake_backends_as_backends` (every field is the same object as on `FakeBackends`) |

## 7. Verification

`SCRATCH` is the verifier's scratchpad directory.

```bash
cd /home/sandy/dev/earmark
uv sync
make check
uv run ruff format --check docs/
uv run pytest -q tests/test_runner.py tests/test_worker.py tests/test_factory.py \
  tests/test_e2e.py tests/test_cli.py tests/test_artifacts.py tests/test_models.py
# The signal tests must be stable: run the worker tests repeatedly.
for i in 1 2 3 4 5; do uv run pytest -q tests/test_worker.py || break; done

# Light imports
uv run python -c 'import sys, earmark.cli; print(sorted(m for m in ("fastapi","starlette","uvicorn") if m in sys.modules))'
# expect: []

# The factory is the only place that constructs backends outside tests
grep -rn "Fake[A-Z][a-zA-Z]*(" src/earmark --include=*.py | grep -v "src/earmark/fakes.py"
# expect: only src/earmark/pipeline/factory.py
```

Live smoke (loopback only, no network). Run it from `$SCRATCH` so no `.env` in the
repository supplies settings. The venv's `earmark` binary is used directly so signals
reach the process without a `uv run` wrapper in between.

```bash
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
export EARMARK_API_TOKEN=smoke-token-5 EARMARK_DATA_DIR="$SCRATCH/data" \
  EARMARK_VAULT_DIR="$SCRATCH/vault" EARMARK_PORT=18788
for v in RESOLVER_BACKEND FETCH_BACKEND ASR_BACKEND LLM_PROVIDER STORE_BACKEND \
  INDEX_BACKEND NOTIFY_BACKEND; do export "EARMARK_$v=fake"; done
$E serve > "$SCRATCH/serve.log" 2>&1 &
SERVE_PID=$!
for i in $(seq 50); do curl -sf http://127.0.0.1:18788/healthz && break; sleep 0.1; done
B=http://127.0.0.1:18788
H="Authorization: Bearer $EARMARK_API_TOKEN"
URL='https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4'

curl -s -X POST $B/ingest -H "$H" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$URL\",\"prompt\":\"What papers are cited?\"}" | tee ingest.json
JOB=$(sed -E 's/.*"job_id":"([0-9a-f]{32})".*/\1/' ingest.json)
curl -s -H "$H" $B/jobs/$JOB                 # "status":"queued"
$E worker --once                             # job <JOB> done
curl -s -H "$H" $B/jobs/$JOB                 # "status":"done", "stage":"index",
                                             # result.note_path "Earmark/Podcasts/<key>.md"
ls data/episodes/*/ data/episodes/*/analyses # episode.json shared.json audio.mp3
                                             # transcript.json transcript.txt, one <hash>.json
cat vault/Earmark/Podcasts/*.md              # "# Fake episode …" and a "## What papers are cited?" section

$E submit "$URL"                             # <id> queued (no prompt: a new job)
$E jobs                                      # two jobs, newest first
$E worker > "$SCRATCH/worker.log" 2>&1 &     # runs the queued job, then idles
W=$!
sleep 2; kill -TERM $W; wait $W; echo "worker exit=$?"   # worker exit=0, promptly
$E jobs --status done                        # both jobs done
grep -h "claimed\|done" "$SCRATCH/worker.log"

EARMARK_ASR_BACKEND=faster_whisper $E worker --once; echo "exit=$?"
                                             # names EARMARK_ASR_BACKEND, exit=2
kill $SERVE_PID; wait $SERVE_PID 2>/dev/null
grep -c smoke-token-5 "$SCRATCH/serve.log" "$SCRATCH/worker.log"   # 0 and 0
unset EARMARK_API_TOKEN EARMARK_DATA_DIR EARMARK_VAULT_DIR EARMARK_PORT
for v in RESOLVER_BACKEND FETCH_BACKEND ASR_BACKEND LLM_PROVIDER STORE_BACKEND \
  INDEX_BACKEND NOTIFY_BACKEND; do unset "EARMARK_$v"; done
rm -rf "$SCRATCH/data" "$SCRATCH/vault" "$SCRATCH"/*.log "$SCRATCH/ingest.json"
```

Also check:
- `git status` shows only the files in section 3 and this plan.
- No `# type: ignore` or `noqa` in new code, and no loosened lint, type, or test
  settings.
- `docs/ARCHITECTURE.md` matches section 8.

Not verifiable in the sandbox: behaviour under a container runtime's stop signal and
timeout (step 16), and long real stages (steps 08 to 11).

## 8. Deviations

1. **`Backends` lives in `pipeline/stages.py`**, and `FakeBackends.backends()` converts
   the fakes to it. The brief does not name the type; the runner needs one value that
   holds the seven stages, and putting it next to the Protocols keeps `runner.py` free
   of factory imports.
2. **`shared.json` records the first share time** (`ShareRecord`,
   `ArtifactStore.record_share`), and the runner passes that to `Store.store`, not the
   current job's `shared_at`. Step 02 left this choice to step 05 and suggested the
   earliest ingest's share time. Keeping it in an artifact rather than a `jobs` query
   means it survives a lost database, as the architecture requires of derived state,
   and steps 17 and 18 can read it from disk.
3. **"Calls neither the transcriber nor the analyzer" is proven two ways.** The
   transcriber (and fetcher) is not invoked at all when `transcript.json` exists. The
   analyzer is invoked and answers from its own cache, so no LLM work happens
   (`FakeAnalyzer.computed` is unchanged). Skipping the call entirely would need the
   runner to know the analyzer's model for `prompt_hash`, which would change the
   `Analyzer` Protocol. The architecture's rule 1 already says the runner calls every
   stage and relies on idempotence.
4. **SIGTERM releases a job mid-stage**, by raising a `Shutdown` exception from the
   signal handler inside an interruptible window, and lets a job that is already
   completing or notifying finish. The brief allows either; releasing is the only
   option that does not hold a container stop for a whole transcription.
5. **The traceback goes to the log**, and `job.error` gets a one-line summary with the
   raising file, line, and function. Storing whole tracebacks in `job.error` would break
   the architecture's rule that error text is short (it is shown in notifications).
6. **`job.stage` is never set to `notify`.** Notification happens after the terminal
   write, when fenced calls no longer apply; a failed job keeps the stage it failed in.
7. **Commands beyond the brief's minimum:** `earmark jobs` has `--limit`, `--status`,
   and `--json` (the same filters and shape as `GET /jobs`).
8. **Architecture edits** made with this plan: a "Runner and worker" subsection under
   Stage contracts (stage order, cache skip, `shared.json`, warnings, exception
   mapping, notification, error wording, heartbeat, shutdown, lost lease); `Backends`
   and the runner, factory, and worker roles in the module layout; `ShareRecord` in the
   domain models; `shared.json` in the data layout; `FakeBackends.backends()` in the
   fakes paragraph; the Jobs section notes that the worker runs `requeue_stale` every
   poll and notifies jobs it failed; the Testing section notes that the fake store
   writes a note only when `EARMARK_VAULT_DIR` is set.

## 9. Out of scope

- Every real backend: guarded fetch and the `http` fetcher (06), the `apple` resolver
  (07), faster-whisper and remote ASR (08, 09), analysis (10, 11), the vault store
  (12), entities (13), LanceDB (14), ntfy and `doctor` (15).
- Deleting audio after transcription (step 08's backend).
- `reanalyze` jobs (step 17): the runner fails them permanently for now. The stage code
  is split so step 17 can enter at the analyze stage with the episode read from
  `episode.json` and the share time from `shared.json`.
- Container stop timeouts, systemd units, and running API and worker together (16).
- Several worker processes or threads per host. The store supports them; this step runs
  one job at a time per process.
- Pruning old jobs, `earmark jobs retry`, and a status page (backlog).
