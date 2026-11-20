# Architecture

Earmark turns "share this podcast episode" into a research note. This document is the
design the build follows. When a step needs to change a contract described here, it
changes this file in the same commit.

## Decisions

The original sketch left several choices open. These are the defaults the plan builds
on. Each is isolated behind config or a stage interface, so reversing one touches a
backend, not the pipeline.

| Question | Decision | How to change it |
|---|---|---|
| System of record | An Obsidian-compatible Markdown vault. Files are the truth; SQLite and the vector index are derived and rebuildable. | Implement another `Store` backend. |
| LLM for analysis | Hosted is allowed: Claude (`claude-opus-5-5`) through the Anthropic API by default. Transcription and embeddings always run locally. | `EARMARK_LLM_PROVIDER=openai_compat` points analysis at a local server (step 11). |
| Notification | ntfy, off unless `EARMARK_NTFY_URL` is set. The Shortcut can also poll `GET /jobs/{id}`. | Implement another `Notifier`. |
| Queue | A SQLite job table and a separate worker process. No Redis. | Swap `JobStore`. |
| Vector store | LanceDB, embedded, no server. | Swap the `Indexer` backend. |
| Transcription | faster-whisper `large-v3` in the worker process, with a remote OpenAI-compatible ASR endpoint as the alternative (step 09). | `EARMARK_ASR_BACKEND`. |
| Reachability | Tailscale. The API binds to the tailnet and is never exposed publicly. | See `docs/deploy.md` (step 16). |
| Language and tooling | Python 3.13 (`requires-python = ">=3.13,<3.14"`, chosen in step 01 from wheel availability on aarch64), uv with the `uv_build` backend, FastAPI, pydantic, httpx2 (the maintained successor of httpx, imported as `httpx2`) with its httpcore2 for the guarded network backend (step 06), typer, stdlib `sqlite3`, defusedxml for RSS (step 07). Sync code throughout the worker. | Widen `requires-python` once every heavy extra ships wheels for the new version on linux/aarch64; `uv lock` enforces this through `tool.uv.required-environments`. |

## Flow

```
iPhone Shortcut
   │  POST /ingest {url, prompt?, timestamp?}   Authorization: Bearer <token>
   ▼
API (FastAPI) ── validates, enqueues, returns 202 {job_id} ──► jobs table (SQLite, WAL)
                                                                   │ claim
                                                                   ▼
Worker ── Pipeline.run(job)
   resolve     Apple URL → iTunes Lookup (or the episode page) → RSS feed → Episode
   fetch       enclosure → data/episodes/<key>/audio.<ext>
   transcribe  audio → transcript.json + transcript.txt
   analyze     transcript + prompt → analyses/<prompt_hash>.json
   store       analysis + transcript → Markdown notes in the vault, entity links
   index       transcript chunks + analysis items → LanceDB
   notify      ntfy (never fails the job)
```

The API handler does no network I/O and no heavy work. It validates and enqueues, so the
Shortcut always gets its 202 in milliseconds.

## Module layout

```
src/earmark/
  __init__.py          __version__ only; importing earmark loads nothing heavy
  __main__.py          `python -m earmark`
  config.py            Settings (pydantic-settings, EARMARK_* env, .env), load_settings, ConfigError
  log.py               configure_logging: stderr handler with a secret-redacting formatter
  models.py            domain models
  errors.py            TransientError, PermanentError, SkipEpisode
  artifacts.py         ArtifactStore: per-episode cache paths, atomic writes
  db.py                SQLite connection and migrations (PRAGMA user_version)
  jobs.py              JobStore
  fetch.py             GuardedFetcher: guarded GET and streaming download, address policy,
                       pinned-address network backend (step 06)
  audio.py             HttpAudioFetcher, the `http` AudioFetcher backend (step 06)
  ingest.py            enqueue_ingest: IngestRequest → canonical URL → JobStore (API and CLI)
  fakes.py             fake backend for every stage (tests and smoke runs)
  cli.py               `earmark` entry point
  api/                 app.py (create_app), auth.py (bearer middleware), routes.py, schemas.py
  pipeline/
    stages.py          stage Protocols, Backends (the set of seven stage implementations)
    runner.py          Pipeline: runs one claimed job, maps exceptions to job outcomes
    factory.py         composition root: Settings → backends (registries), build_worker
    worker.py          Worker (claim/run loop), Heartbeat thread, SIGTERM/SIGINT handling
  resolve/             apple.py (URL parsing, light), itunes.py (lookup JSON), apple_page.py
                       (episode page JSON), rss.py (feed parsing, matching), text.py,
                       resolver.py (AppleResolver, the `apple` backend; step 07)
  transcribe/          faster_whisper.py, remote.py
  analyze/             schema.py, prompts.py, render.py, analyzer.py, chunking.py, llm/
  vault/               notes.py, sanitize.py, entities.py
  index/               chunks.py, embed.py, lancedb_index.py
  notify/              ntfy.py
tests/                 mirrors src; fixtures/ holds recorded Apple JSON and RSS
```

## Domain models

Defined in `models.py` (step 02; exact types and validators in
`docs/steps/02-contracts.md`). Steps may add fields, with defaults so existing artifacts
still load. All models are pydantic, frozen, and `extra="forbid"`; sequences are tuples
(pass `tuple(...)`: mypy rejects a list for a tuple field); datetimes are timezone-aware;
seconds are finite and non-negative.

- `IngestRequest`: `url`, `prompt: str | None`, `timestamp: datetime | None`. The
  `POST /ingest` body; unknown keys are rejected (a typo such as `promt` must not
  silently drop the user's question). Validators normalise: `url` stripped, at most
  `MAX_URL_CHARS` (2048); `prompt` stripped, blank or whitespace-only becomes `None`,
  at most `MAX_PROMPT_CHARS` (4000) after stripping; `timestamp` must convert to UTC
  (its offset is kept). Step 04.
- `EpisodeRef`: `source` (`"apple_podcasts"`), `show_id`, `episode_id`, `country`, `url`.
  Returned by `resolve.apple.parse_apple_url`; `url` is the canonical
  `https://podcasts.apple.com/{country}/podcast/id{show_id}?i={episode_id}` (country
  lower-case, `us` when the link has none; slug, tracking and share-from-time
  parameters dropped; legacy `itunes.apple.com` and `http` mapped to it).
- `Episode`: `key`, `source`, `show_id`, `episode_id`, `guid`, `title`, `show_title`,
  `published_at | None`, `duration_s | None`, `audio_url`, `feed_url | None`, `url`,
  `description`. A validator requires `key == episode_key(source, show_id, guid)`.
  From the `apple` resolver (step 07): `guid` is Apple's episode GUID when Apple gives
  one, else the matched feed item's, else `apple-podcasts:<episode_id>`; `title` and
  `show_title` are Apple's, cleaned of control characters and capped (500 characters);
  `audio_url` is the feed enclosure, else Apple's public audio URL; `feed_url` is the
  lookup's `feedUrl`; `description` is plain text, capped at 20 000 characters.
- `Segment`: `start`, `end`, `text`, `speaker: str | None`.
- `Transcript`: `episode_key`, `language | None`, `duration_s`, `model`, `segments`,
  `warnings`. `to_text()` renders one `[HH:MM:SS] text` line per segment (whitespace
  collapsed, hours unbounded, trailing newline); it is the content of `transcript.txt`.
- `Analysis`: `episode_key`, `prompt` (`None` = default), `prompt_hash`, `model`,
  `schema_version`, `created_at`, and the result: `summary`, `key_claims: Claim`,
  `people`, `papers`, `companies`, `tools`, `topics` (all `Entity`), `open_questions:
  str`, `research_leads: ResearchLead`, `prompt_response | None`, and `warnings`.
  `Claim` is `text`, `timestamp_s | None`. `Entity` is `name`, `context`,
  `timestamps_s`. `ResearchLead` is `text`, `rationale`, `timestamp_s | None`.
- `StoreResult`: `note_path`, `transcript_note_path | None`, `warnings`. Paths are POSIX,
  relative to `VAULT_DIR` (so they start with `VAULT_SUBDIR`), usable directly as the
  `file=` of an `obsidian://open` link.
- `JobResult`: `note_path`, `transcript_note_path`, `analysis_hash`, all optional.
- `ShareRecord`: `episode_key`, `first_shared_at`. The content of `shared.json`; the
  earliest share time of the episode, passed to `Store.store` (step 05).
- `Job`: `id` (`uuid4().hex`), `kind` (`ingest`, `reanalyze`), `status`, `stage`,
  `url | None` (required for `ingest`; the canonical episode URL, so `Episode.url` is
  canonical too), `prompt`, `episode_key | None` (required for `reanalyze`), `attempts`,
  `run_after`, `error` (failure message or skip reason), `warnings`,
  `result: JobResult | None`, `shared_at`, `created_at`, `updated_at`, `heartbeat_at`.

**Episode key.** `sha256("{source}:{show_id}:{guid}")` over UTF-8, first 20 hex
characters, from `models.episode_key`. The Apple show id is used instead of the feed URL
because feeds move and show ids do not. The key names the cache directory, so the same
episode shared twice is transcribed once. Parts are not normalised (the resolver strips
the GUID); `source` and `show_id` may not contain `:`. Keys are checked with
`re.fullmatch("[0-9a-f]{20}")` wherever they reach the filesystem.

**Prompt hash.** `models.prompt_hash(prompt, model, schema_version)`: sha256 of the
UTF-8 canonical JSON `{"model":…,"prompt":…,"schema_version":…}` (sorted keys, no
spaces, `ensure_ascii=False`), first 16 hex characters. `None` (the default prompt) and
`""` differ. `ANALYSIS_SCHEMA_VERSION` in `models.py` is bumped whenever the output
schema, the standing system prompt, or the default prompt changes in a way that should
invalidate cached analyses.

**Errors.** `errors.py` defines `EarmarkError` and three sibling subclasses:
`TransientError(message, *, retry_after_s=None)`, `PermanentError(message)`, and
`SkipEpisode(reason)`. Messages are short, human-readable, and free of secrets and
untrusted bulk text, because they are stored in `job.error` and shown in notifications.

## Stage contracts

Defined as `typing.Protocol` in `pipeline/stages.py`, fixed in step 02. Parameters are
positional-only, because mypy does not check parameter names when matching a Protocol;
the runner calls stages positionally and implementations may name parameters freely.

```python
class Resolver(Protocol):
    def resolve(self, url: str, /) -> Episode: ...  # raises SkipEpisode if no public feed


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

`Store` receives every analysis of the episode (`ArtifactStore.list_analyses`), so the
note is regenerated whole. `Indexer` receives the `StoreResult` so search hits can carry
the note path. `Notifier` receives the job in its terminal state.

Every stage has a fake in `fakes.py` that records each call (`calls`), can be told to
raise (`fail_next(exc, times)`), and, for the cache-aware stages (fetch, transcribe,
analyze), counts the calls that did real work (`computed`). The cache-aware fakes write
real artifacts through `ArtifactStore`. `make_fake_backends(data_dir, vault_dir=...)`
builds the whole set, and `FakeBackends.backends()` turns it into the `Backends` the
runner takes.

Rules every backend follows:

1. **Idempotent and cache-aware.** If the output artifact exists, return it without
   doing the work. The runner does not track per-stage checkpoints in the database; it
   calls every stage and relies on this. `job.stage` exists for observability only.
   One exception: audio is deleted after transcription, so when `transcript.json`
   exists the runner skips `fetch` and `transcribe` and reads the transcript from
   `ArtifactStore`. The resolver is not cached; the runner writes `episode.json` after
   it returns.
2. **Atomic writes.** Write to a temp file in the same directory, then rename. Use
   `artifacts.atomic_writer` / `atomic_write_bytes` / `atomic_write_text` (temp files
   are `.<name>.<random>.tmp`, or another suffix such as `.part`; final files are mode
   `0o644`).
3. **Retry intent in the exception.** `TransientError` requeues with backoff until
   `max_attempts`. `PermanentError` fails the job immediately. `SkipEpisode` ends the job
   as `skipped` with a reason. Any other exception is treated as transient and its
   traceback is recorded.
4. **Index and notify failures never fail the job.** They are recorded in
   `job.warnings`. The note is the product.
5. **Non-fatal problems travel as data.** A stage that succeeds with a caveat (a
   transcript without segments, dropped out-of-range timestamps) puts a message in the
   `warnings` of the `Transcript`, `Analysis`, or `StoreResult` it returns. The runner
   copies those into `job.warnings`. They are stored in the artifact, so a cache hit
   reports them again.

### Runner and worker

Step 05; exact signatures in `docs/steps/05-worker.md`.

- **Backends.** `Backends` (in `stages.py`) holds the seven stages; `indexer` and
  `notifier` are `None` when their backend is `none`. `factory.build_backends` is the
  only code that constructs backends: one registry per setting maps a backend name to a
  builder, and a name with no builder is a `ConfigError` naming the variable, raised
  before the database is opened. Real backends import their heavy modules inside the
  builder.
- **Order.** `Pipeline.run(claim)`: resolve → write `episode.json`, record the episode
  key on the job, record the share time in `shared.json` → if `transcript.json` exists,
  read it and skip fetch and transcribe, else fetch → transcribe → analyze with
  `job.prompt` → store with every analysis on disk and the episode's first share time →
  index (if configured) → `complete` with a `JobResult` → notify. Stages are called
  positionally. Each stage sets `job.stage` before it runs; a failed job keeps the
  stage it failed in, and `notify` is never set as a stage.
- **Outcomes.** `SkipEpisode` → `skipped`; `PermanentError` → `failed`;
  `TransientError` → requeued with backoff (honouring `retry_after_s`) until attempts
  run out; any other `Exception` → transient, with the traceback logged at ERROR and a
  one-line summary naming the raising file, line, and function in `job.error`. Error
  and warning text is whitespace-collapsed, redacted of every `Settings` secret, and
  capped at 500 characters. Index failures, and notifier failures, are warnings
  (`index failed: …`, `notify failed: …`) and never change the status.
- **Notify.** Exactly once per job that reaches `done`, `failed`, or `skipped`, with the
  job as written (warnings included) and the episode if resolution got that far. Not for
  a requeued retry, a released job, or a lost lease.
- **Heartbeat.** A thread heartbeats every `min(60 s, JOB_LEASE_SECONDS / 4)` while a
  job runs. It shows the process is alive, not that a stage progresses.
- **Shutdown.** SIGTERM and SIGINT stop the worker from claiming. During stage work the
  handler raises a `Shutdown` (`BaseException`) out of the stage, and the worker
  `release`s the job (back to `queued`, attempt undone). Once the job is completing or
  notifying, the worker finishes it, then exits.
- **Lost lease.** A fenced call raising `LeaseLostError` makes the runner abandon the
  job without writing to it or notifying.
- **Each poll** the worker first runs `requeue_stale` and notifies the jobs it failed.
  `earmark worker --once` processes at most one job; `earmark submit` enqueues through
  `ingest.enqueue_ingest`, like `POST /ingest`.

### Fetching

Step 06; exact signatures in `docs/steps/06-fetch.md`.

- **One client.** `fetch.GuardedFetcher` is the only code that requests third-party
  URLs. `get(url, *, max_bytes, purpose, accept, params)` returns a `Document` (final
  URL, decoded bytes, media type, redirect count) for feeds and lookups;
  `download(url, destination, *, max_bytes, ...)` streams media to
  `destination(ext)` through `atomic_writer(..., temp_suffix=".part")` and returns a
  `Downloaded`. `fetch.guarded_fetcher(settings)` builds one from `Settings`. It is
  not thread-safe; each process builds its own. The httpx2 client is created on first
  use.
- **Errors.** `PermanentFetchError(PermanentError)` and
  `TransientFetchError(TransientError)` carry `host` and `status`;
  `BlockedAddressError` is a `PermanentFetchError`. 200 and 203 succeed; 408, 429, and
  5xx are transient (with `Retry-After` as `retry_after_s`); other 4xx and unexpected
  statuses are permanent. Timeouts, connection and protocol errors, DNS failures, a
  truncated or empty download, and the whole-call deadline (2 min for `get`, 1 h for
  `download`) are transient. A size cap, a refused scheme or address, an invalid URL or
  redirect, more than 20 redirects, and a textual content type on a download are
  permanent. Messages name the host, never the URL path or query.
- **`http` AudioFetcher.** `audio.HttpAudioFetcher` returns `find_audio(key)` without a
  request when it exists; otherwise it removes `.part` files older than 2 h, downloads
  `episode.audio_url` with `MAX_AUDIO_MB`, and names the file `audio.<ext>`: the
  extension of the final URL's path, else the `Content-Type`'s, else the original URL
  path's, else `bin`.

### Resolving

Step 07; exact signatures in `docs/steps/07-resolver.md`.

- **`apple` Resolver.** `resolve.resolver.AppleResolver` (built by
  `build_apple_resolver(settings)` with `fetch.guarded_fetcher(settings)` and
  `MAX_FEED_MB`) parses the canonical URL, then calls the iTunes Lookup
  `https://itunes.apple.com/lookup?id=<show>&entity=podcastEpisode&limit=200&country=<cc>`
  (200 is Apple's maximum; there is no paging, and some shows list far fewer).
  `resolve_details(url)` also reports where the episode was identified (`lookup` or
  `page`) and the feed match tier; `earmark resolve <url>` prints it.
- **Out-of-window episodes.** `lookup?id=<episode id>` returns nothing, so an episode
  not in the lookup result is identified from its Apple Podcasts web page (the canonical
  URL): the `serialized-server-data` JSON (GUID, title, date, duration, show, public
  `STDQ` audio URL), else the schema.org `PodcastEpisode` JSON-LD (title, date,
  duration). The page's show must equal the URL's show.
- **Feed matching**, first hit wins: GUID; enclosure host and path (query and scheme
  ignored, unique only); normalized title (NFKC, casefold, punctuation and symbols
  removed; nearest date breaks ties); publish date, which must equal Apple's to the
  minute (or, when Apple gives only a calendar date, fall within one day of it) with
  durations agreeing within max(30 s, 2 %) where known (required for date-only), and
  must identify exactly one item. There is no "nearest" choice: on daily shows the
  previous episode is about 24 h away, and a false match is worse than none. Audio comes
  from the matched item's enclosure; when the feed does not list the episode, from
  Apple's audio URL.
- **Outcomes.** No show record → `PermanentError`. No public `feedUrl` → `SkipEpisode`.
  Matched item without a usable enclosure, or an unmatched episode whose only Apple
  stream is for subscribers → `SkipEpisode`. Both skip reasons name subscriber-only and
  Apple-exclusive content. Unmatched with no Apple audio URL, an episode Apple does not
  list, or a page for another show → `PermanentError`. Lookup 403 (rate limiting) and
  an unreadable lookup body are transient; lookup 400 (bad store) is permanent; other
  statuses follow `fetch.py`. A malformed feed, one declaring entities, or a non-RSS
  root is permanent.
- **No cache.** Two requests in the window (lookup, feed), three outside it; the
  runner writes `episode.json` afterwards.

## Jobs

States: `queued → running → done | failed | skipped`. Job ids are `uuid4().hex`, so
they cannot be guessed or enumerated through `GET /jobs/{id}`.

`jobs.JobStore` over `db.Database` (exact signatures in `docs/steps/03-jobs.md`):

- **Connections.** `db.py` opens one short-lived connection per operation
  (`autocommit=True`, busy timeout, always closed), so a store can be shared by threads
  and each process builds its own. Every read-then-write runs in `BEGIN IMMEDIATE`;
  deferred transactions fail with `SQLITE_BUSY_SNAPSHOT` under contention. WAL is set
  once (retrying on `SQLITE_BUSY`), and migrations are an append-only tuple in `db.py`
  applied in one transaction under `PRAGMA user_version`. A database newer than the
  code is refused.
- **Claim.** `claim_next` is one `UPDATE jobs … WHERE seq = (SELECT seq … WHERE status
  = 'queued' AND run_after <= now ORDER BY created_at, seq LIMIT 1) RETURNING *` inside
  `BEGIN IMMEDIATE`. (`UPDATE … LIMIT` is not available in the SQLite that uv-managed
  CPython ships.) It returns a `Claim`: the job plus a random lease token stored in the
  row but not in `Job`. `heartbeat`, `set_stage`, `set_episode_key`, `complete`,
  `fail`, `skip`, and `release` take the `Claim` and raise `LeaseLostError` if the
  token no longer matches, so a worker that lost its lease cannot touch the job.
- **Attempts and retries.** `attempts` counts claims. `fail(transient=True)` requeues
  with `run_after = now + retry_delay(attempts, retry_after_s)` (60 s × 4ⁿ⁻¹, capped
  at 6 h, raised to `retry_after_s`) until `attempts` reaches `JOB_MAX_ATTEMPTS`, then
  fails. `fail(transient=False)` fails at once. `release` (SIGTERM) requeues
  immediately and undoes the attempt.
- **Leases.** The worker heartbeats while a stage runs. A `running` job whose heartbeat
  is older than `JOB_LEASE_SECONDS` is requeued by `requeue_stale` (or failed, if it has
  used its attempts), which covers a crashed worker. `requeue_stale` returns the jobs it
  changed; the worker calls it on every poll and notifies the ones it failed.
- **Dedupe.** Enqueueing the same `(kind, url or episode_key, prompt)` while an
  identical job is `queued` or `running` returns the existing job (`Enqueued.created`
  is `False`). A partial unique index on a `dedupe_key` column enforces one active job
  per request. A double-tapped share creates one job.
- **Warnings.** `add_warning(job_id, message)` appends unless the same text is already
  there, and works on terminal jobs (notifier failures are recorded after `done`).
- **Time.** Clocks are injected so tests control time. Timestamps are stored as
  fixed-width UTC ISO strings, so SQL compares them as text; a `shared_at` offset is
  kept as an instant, read back in UTC.

## Data layout

```
$EARMARK_DATA_DIR/               a local filesystem: WAL needs shared memory (no NFS/SMB)
  earmark.db                    jobs (step 03), entity registry (step 13); WAL mode
  episodes/<episode_key>/       paths owned by artifacts.ArtifactStore
    episode.json                written by the runner after resolve
    shared.json                 ShareRecord: first share time, kept by the runner (earliest wins)
    audio.<ext>                 deleted after transcription unless EARMARK_KEEP_AUDIO
    .audio.<ext>.<random>.part  download in progress; renamed to audio.<ext> when complete
    transcript.json             written last; its existence marks the transcript cached
    transcript.txt              Transcript.to_text()
    analyses/<prompt_hash>.json   16 hex chars over prompt + model + schema version
  lancedb/

$EARMARK_VAULT_DIR/$EARMARK_VAULT_SUBDIR/        earmark writes nowhere else in the vault
  Podcasts/<Show>/<YYYY-MM-DD> <Title>.md
  Podcasts/<Show>/transcripts/<YYYY-MM-DD> <Title> (transcript).md
  Entities/{People,Papers,Companies,Tools,Topics}/<Name>.md
  Digests/<ISO-year>-W<week>.md
  aliases.yaml                  optional, user-maintained entity merges
```

**Notes.** One note per episode, regenerated from artifacts on every store, so storing
twice produces the same bytes. Frontmatter: `show`, `episode`, `date`, `url`, `guid`,
`episode_key`, `duration`, `prompts`, `tags`, `shared_at`, `model`. The body has one
section per analysis, newest first, each showing its prompt. Generated content sits
between `<!-- earmark:begin -->` and `<!-- earmark:end -->`; anything the user writes
outside those markers survives regeneration.

**Entities.** People, papers, companies, tools, and topics are normalized (NFKC,
casefold, collapsed whitespace, stripped punctuation), recorded in SQLite (`entities`,
`aliases`, `mentions`), and linked from episode notes with wikilinks. Each entity note
lists the episodes that mention it, so recurring names accumulate. The registry is
rebuildable from `analyses/*.json`.

## HTTP API

Every path except `/healthz` requires `Authorization: Bearer $EARMARK_API_TOKEN`. A pure
ASGI middleware (`api/auth.py`) checks it before routing or body parsing, comparing
SHA-256 digests with `hmac.compare_digest`, so an unauthenticated request gets 401 with
`WWW-Authenticate: Bearer realm="earmark"` whatever its path or body. The service
refuses to start with an empty or whitespace-only token, or one that is not printable
ASCII. Interactive docs and `/openapi.json` are off. Errors are `{"detail": "<short
message>"}`, written to be shown by the Shortcut as is; validation errors never echo
input. A `sqlite3.Error` is 503 with `Retry-After`. Details in `docs/steps/04-api.md`.

| Route | Purpose |
|---|---|
| `POST /ingest` | Body `{url, prompt?, timestamp?}` (`IngestRequest`, JSON, at most 64 KiB, else 413). Enqueues the canonical episode URL. 202 `{job_id, status, created}` with `Location: /jobs/{id}`; `created` is `false` when an identical active job was returned (dedupe). 401 bad token. 422 unsupported or malformed URL, show link, or invalid body, with a message the Shortcut can display. |
| `GET /jobs/{id}` | The `Job` as JSON: status, stage, error, warnings, result (note path). 404 if unknown. |
| `GET /jobs` | `?limit=1..500` (default 20), `?status=`. `{"jobs": [Job, …]}`, newest first. |
| `GET`, `HEAD /healthz` | Liveness, no auth, `{"status": "ok"}`. |
| `GET /search?q=` | Vector search (step 14). |
| `GET /episodes`, `POST /episodes/{key}/analyze` | Re-run a prompt on a stored episode (step 17). |

## Configuration

Environment variables, `EARMARK_` prefix, also read from `.env` in the working
directory (real environment variables win over `.env`). Step 01 defines all of them with
defaults so later steps only read settings. An empty value (`EARMARK_X=`) means unset
and takes the default. Unknown `EARMARK_*` keys are ignored, not rejected, because a
shared `.env` may hold other tools' keys. `Settings` is frozen.

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN` | unset; required to serve (step 04) | Bearer token for the API (secret) |
| `HOST`, `PORT` | `127.0.0.1`, `8787` | API bind address |
| `DATA_DIR` | `./data` | Database, artifacts, index |
| `VAULT_DIR`, `VAULT_SUBDIR` | unset (required for the vault store), `Earmark` | Where notes go. The subdir is a relative path without `..` |
| `OBSIDIAN_VAULT_NAME` | unset | Enables `obsidian://` links in notifications |
| `RESOLVER_BACKEND` | `apple` | `apple`, `fake` |
| `FETCH_BACKEND` | `http` | `http`, `fake` |
| `ASR_BACKEND` | `faster_whisper` | `faster_whisper`, `remote`, `fake` |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai_compat`, `fake` |
| `STORE_BACKEND` | `vault` | `vault`, `fake` |
| `INDEX_BACKEND` | `lancedb` | `lancedb`, `fake`, `none` |
| `NOTIFY_BACKEND` | `ntfy` | `ntfy`, `fake`, `none`. `ntfy` without `NTFY_URL` sends nothing |
| `ASR_MODEL`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, `ASR_LANGUAGE` | `large-v3`, `auto`, `auto`, unset (auto-detect) | faster-whisper options |
| `ASR_BASE_URL`, `ASR_API_KEY`, `ASR_MAX_UPLOAD_MB` | unset (no upload limit) | Remote ASR backend (key is secret) |
| `KEEP_AUDIO` | `false` | Keep audio after transcription |
| `LLM_MODEL` | `claude-opus-5-5` | Analysis model |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_CONTEXT_TOKENS` | unset | Local LLM endpoint and its context budget (key is secret) |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model (fastembed's default; step 14 may revise) |
| `NTFY_URL`, `NTFY_TOKEN` | unset | ntfy topic URL and optional token (secret) |
| `MAX_AUDIO_MB`, `MAX_FEED_MB`, `ALLOW_PRIVATE_FETCH` | `1024`, `20`, `false` | Fetch limits in MiB (× 1024 × 1024); `MAX_FEED_MB` caps the lookup, the episode page, and the feed. `ALLOW_PRIVATE_FETCH` turns off the address check only |
| `JOB_MAX_ATTEMPTS`, `JOB_LEASE_SECONDS`, `WORKER_POLL_SECONDS` | `4`, `900`, `2` | Queue behaviour |
| `TZ` | unset (system) | IANA zone for note dates and digests, validated at load |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive) |

Backend names are `Literal` types, so a misspelt name fails when settings load, naming
the variable. A name that is valid but whose backend is not built yet is rejected by
`pipeline/factory.py` (step 05).

The Anthropic credential is read by the `anthropic` SDK from its own environment
(`ANTHROPIC_API_KEY` or a stored profile), not from an `EARMARK_` variable. Note that
pydantic-settings does not export `.env` entries to the process environment, so an
`ANTHROPIC_API_KEY` line in `.env` is not seen by the SDK unless step 10 arranges it.

## Security

The service runs on a private network, but it fetches URLs that third parties control
and writes into a vault the user cares about.

- **Ingest.** Only hosts with a registered source are accepted (`podcasts.apple.com`,
  legacy `itunes.apple.com`, exact match). A show link without `?i=` is rejected with a
  clear message. The API stores the canonical URL it built from the parsed ids, never
  the client's string, so re-shares dedupe and no client-controlled URL reaches the
  resolver. The handler does no outbound I/O.
- **Outbound fetches.** Feed and enclosure URLs come from podcast publishers. Every such
  fetch goes through `fetch.py`:
  - http and https only, checked on the first URL and on every redirect target; URLs
    with credentials are refused. At most 20 redirects.
  - Addresses are checked when a connection opens, inside a custom httpcore2
    `NetworkBackend`: it resolves the host once, refuses the connection if **any**
    address is not public, and connects to the exact address it checked (TLS still
    verifies the hostname). So every redirect hop is checked, and DNS rebinding cannot
    change the address between check and connect. "Public" means `is_global` and not
    multicast or site-local, with IPv4-mapped and NAT64 addresses judged by their
    embedded IPv4 address and the IPv4-compatible and IPv4-translated ranges refused.
    That excludes loopback, RFC 1918, link-local (cloud metadata), CGNAT and tailnet
    `100.64.0.0/10`, ULA, documentation, and reserved ranges. Numeric shorthand hosts
    (`0x7f000001`, `127.1`) are caught because the resolved address is checked.
    `ALLOW_PRIVATE_FETCH=true` skips the check (the host is then resolved by the
    socket layer as usual). The guard trusts the system resolver's answer.
  - Size caps apply to `Content-Length` and to the bytes actually read (decoded bytes
    for documents, so a gzip bomb stops at the cap).
  - No proxies from the environment, no cookies kept between fetches, and a generic
    `User-Agent: earmark/<version>`. URL paths and queries (which can carry session
    ids) are not logged or put in job errors. `log.configure_logging` keeps the `httpx2` and
    `httpcore2` loggers at WARNING or above, because they print every request URL at INFO.
- **XML.** Feeds are parsed only by `resolve/rss.py`, with `defusedxml.ElementTree`
  (entity declarations and external references refused, so no expansion bombs and no
  external entities; a DOCTYPE alone is allowed and never fetched). The stdlib parser
  is not used on feeds: with expat 2.9 it still expands internal entities. Apple's
  episode page is read with the stdlib `html.parser` and `json`, nothing executed.
- **Resolver requests.** The lookup and episode-page URLs are built from the parsed ids
  and country; the feed URL comes from Apple's lookup. All three go through `fetch.py`.
- **Vault writes.** Titles and names from feeds and from the LLM are sanitized before
  they become paths or YAML. Resolved paths are checked to be inside the vault subdir.
- **LLM.** The transcript is untrusted text. The analysis call has no tools, and its
  output is validated against a schema before anything is written.
- **Secrets.** Tokens are `SecretStr`, redacted in `earmark config` and `earmark doctor`,
  and never logged.

## Testing

`make check` runs ruff, ruff format check, mypy (strict), and pytest. The default suite
needs no network, GPU, or credentials; `pytest-socket` blocks sockets so a test that
reaches out fails. Unix-domain sockets stay allowed (`--allow-unix-socket`) because the
asyncio event loop, and so FastAPI's `TestClient`, needs a socketpair. Tests carrying
`network`, `slow`, or `llm` get sockets back automatically (`tests/conftest.py`). The
default `-m` expression deselects all four markers; passing `-m` on the command line
replaces it, so `-m network` runs only network tests. Opt-in markers:

- `network`: hits real Apple and feed endpoints. These tests call
  `tests/http_helpers.dns_intercepted()` and pass `allow_private=True` when the local
  DNS answers non-public addresses (the dev sandbox, Known risk 5).
- `slow`: downloads a small Whisper model and transcribes a short clip on CPU.
- `llm`: calls the real analysis model.
- `gpu`: needs CUDA.

With every backend set to `fake`, the whole service runs in the sandbox:
`earmark serve`, `curl` an ingest, `earmark worker --once`, and a note appears (the fake
store writes a minimal note when `EARMARK_VAULT_DIR` is set). Verifiers
use this as the live smoke test from step 05 onward.

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and `impl` and on every pull
request. One job on a GitHub-hosted `ubuntu-24.04-arm` runner (linux/aarch64, the
deployment architecture; free for public repositories) checks out the code, installs uv
with `astral-sh/setup-uv` (uv cache and the managed Python cached, keyed on `uv.lock`),
then runs exactly `uv sync --locked` and `make check`. Python comes from
`.python-version` through uv, never from the workflow. A newer push to the same ref
cancels the run in progress, and the job times out after 15 minutes.

CI runs the default suite only. It does **not** run the opt-in markers (`network`,
`slow`, `llm`, `gpu`), does not install the `transcribe` or `index` extras, never runs
on a GPU, and does not build or run the container. Separate workflows:
`.github/workflows/network.yml` (step 07) runs `uv run --locked pytest -m network -v -rs`
every Monday at 05:23 UTC and on `workflow_dispatch`, with the same pinned actions and
setup as `ci.yml`, `contents: read`, and no secrets. GitHub runs `schedule` and
`workflow_dispatch` only for workflow files on the default branch, so it starts once the
file is on `main`. Later steps add `-m slow` with the `transcribe` extra (step 08), a
manual-only `-m llm` run that reads its key from a GitHub environment secret (step 10),
and an arm64 container build without push (step 16). None of those is a required check:
their failures usually mean an outside service changed.

Rules for every workflow under `.github/workflows/`, enforced by `tests/test_ci.py`: it
passes `actionlint` (installed by the `actionlint-py` dev dependency and run offline by
the test suite), every `uses:` of a remote action is pinned to a full 40-character commit
SHA followed by a `# vX.Y.Z` comment, it declares a top-level `permissions` block, and it
never uses `pull_request_target`. `ci.yml` additionally gets only `contents: read` and
references no secrets. Dependabot (`.github/dependabot.yml`) proposes weekly updates for
the `uv` and `github-actions` ecosystems; it reads its config from the default branch
(`main`) but opens pull requests against `impl` (`target-branch`), so updates pass the
same gate as a step and `main` stays a fast-forward of `impl`.

Branches: steps land on `impl`. After a step's CI run is green, `/step` fast-forwards
`main` to that commit (`git push origin <sha>:main`), so `main` is always the latest
verified, green step and the scheduled workflows test current code. `main` is protected
(required check `make check (linux/arm64)`, no force-push, no deletion); nothing is
merged into `main` that did not come through `impl`.

## Known risks

Step planners should check these against current documentation instead of assuming.

1. **faster-whisper on the DGX Spark.** CTranslate2's published aarch64 wheels may be
   CPU-only, in which case GPU transcription on the Spark needs a source build or a
   different engine. Step 09's remote ASR backend exists so the worker can call whatever
   GPU server runs well there (a Whisper server, or Parakeet). Step 01 found (2026-11-19)
   that the `ctranslate2 4.9.0` cp313 manylinux aarch64 wheel links no CUDA libraries
   (`readelf -d` lists only libc, libstdc++, libgomp, and similar) and reports
   `get_supported_compute_types('cpu') == {float32, int8, int8_float32}`. Step 08 should
   treat the PyPI wheel as CPU-only on aarch64.
2. **iTunes Lookup window.** Verified on 2026-11-19 and 2026-11-20:
   `lookup?id=<show>&entity=podcastEpisode` returns the show record with `feedUrl`, then
   episode records with `trackId` (the `?i=` value), `episodeGuid`, `episodeUrl`,
   `trackName`, `releaseDate`, and `trackTimeMillis`, newest first. `limit` is at most
   200 (default 50; no paging), and some shows list far fewer (The Daily: 44 of 2785).
   `lookup?id=<episode id>` returns nothing. Older episodes are identified from the
   episode web page's embedded JSON, which is undocumented and may change; the weekly
   `network` workflow resolves one such episode so a change shows up. Feeds can carry a
   different GUID and title than Apple for the same episode (art19), which is why
   matching has four tiers. See `docs/steps/07-resolver.md` section 2.
3. **Wheels on aarch64 and Python 3.13** for ctranslate2, lancedb, onnxruntime, and
   fastembed. Resolved in step 01 (2026-11-19): all have cp313 (or abi3/pure) manylinux
   aarch64 wheels, install with `--only-binary :all:`, and import on this sandbox. Python
   is pinned to 3.13. onnxruntime 1.32 has no cp315 wheels, which is why
   `requires-python` carries an upper bound. See `docs/steps/01-scaffold.md`.
4. **Container and GPU paths cannot be exercised in the dev sandbox.** They are reviewed
   statically and verified on the homelab.
5. **The dev sandbox intercepts DNS.** Found in step 06 (2026-11-20): every name,
   including `.invalid` ones, resolves to `10.200.9.2`, so the address guard refuses
   every real fetch there. That is correct behaviour; `network` tests detect it and run
   their transfer checks with `allow_private=True`. The guard against real public DNS
   is exercised on CI's scheduled network run and on the homelab.
6. **Filtering resolvers.** A Pi-hole-style resolver that answers `0.0.0.0` for
   tracker domains (podcast enclosures usually start at one, such as
   `dts.podtrac.com`) makes the download fail as a refused non-public address. The job
   error names the host and `EARMARK_ALLOW_PRIVATE_FETCH`; allow-listing the tracker
   in the resolver is the better fix.
