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
| LLM for analysis | Hosted is allowed: Claude (`claude-opus-5-5`) through the Anthropic API by default, via the official `anthropic` SDK (2.x): one streamed request with adaptive thinking, an explicit effort (`medium`), a JSON-schema structured output, and server-side refusal fallback (step 10). Transcription and embeddings always run locally. | `EARMARK_LLM_MODEL`, `EARMARK_LLM_EFFORT`, `EARMARK_LLM_FALLBACKS`; `EARMARK_LLM_PROVIDER=openai_compat` with `EARMARK_LLM_BASE_URL`, `EARMARK_LLM_MODEL`, `EARMARK_LLM_CONTEXT_TOKENS` points analysis at a self-hosted OpenAI-compatible server (vLLM, llama.cpp, Ollama, LM Studio; step 11, `docs/llm.md`). Transcripts longer than one request are map-reduced on either provider (step 11). |
| Notification | ntfy, off unless `EARMARK_NTFY_URL` is set: one JSON publish to the server root per finished job, with an `obsidian://` link to the note when `EARMARK_OBSIDIAN_VAULT_NAME` is set (step 15). Works with public ntfy.sh or a self-hosted server on the LAN or tailnet. The Shortcut can also poll `GET /jobs/{id}`. | Implement another `Notifier`. |
| Queue | A SQLite job table and a separate worker process. No Redis. | Swap `JobStore`. |
| Vector store | LanceDB (`lancedb` 0.41), embedded, no server: one `chunks` table under `DATA_DIR/lancedb`, exact (exhaustive) cosine search, no ANN index. Embeddings run locally with fastembed (ONNX on CPU), `BAAI/bge-small-en-v1.5` by default. Both are the `index` extra (step 14). | `EARMARK_INDEX_BACKEND`, `EARMARK_EMBED_MODEL` (then `earmark reindex`); swap the `Indexer` and `Searcher` backend. |
| Transcription | faster-whisper `large-v3` in the worker process (the `transcribe` extra), with a remote OpenAI-compatible ASR endpoint as the alternative (`remote`, step 09). The PyPI CTranslate2 wheel for linux/aarch64 is CPU-only (step 08), so on the DGX Spark GPU transcription means a remote server on the Spark (recommended: vLLM serving Whisper; see `docs/asr.md`) or a self-built CUDA CTranslate2. | `EARMARK_ASR_BACKEND`, `EARMARK_ASR_MODEL`, `EARMARK_ASR_BASE_URL`. |
| Reachability | Private network only: the API is never exposed publicly. It listens on `127.0.0.1:8787` (compose publishes it only there). A reverse proxy on the host's LAN interface (Caddy, `deploy/caddy/Caddyfile`) terminates TLS for a name such as `earmark.example.com`. That name is a DNS-only (never proxied) Cloudflare `A` record for the server's private address, with a Let's Encrypt certificate from the DNS-01 challenge; the Cloudflare token lives only with the proxy. Away from home the phone and Mac reach that address through a WireGuard split tunnel. Tailscale Serve is the alternative; `tailscale funnel` is never used (step 16). | See `docs/deploy.md` (step 16). |
| Deployment | One container image (CPU, extras by build argument) running either `earmark serve` or `earmark worker`; `compose.yaml` with a shared data volume and a vault bind mount; GPU transcription through a vLLM Whisper service in the compose `gpu` profile and the `remote` ASR backend; systemd units for a native install (step 16). | See "Deployment" below and `docs/deploy.md`. |
| Language and tooling | Python 3.13 (`requires-python = ">=3.13,<3.14"`, chosen in step 01 from wheel availability on aarch64), uv with the `uv_build` backend, FastAPI, pydantic, httpx2 (the maintained successor of httpx, imported as `httpx2`) with its httpcore2 for the guarded network backend (step 06), typer, stdlib `sqlite3`, defusedxml for RSS (step 07), PyYAML for the user-edited `aliases.yaml` only (step 13: `yaml.compose` node walk, no object construction, YAML aliases refused, imported lazily), the `anthropic` SDK (a core dependency, itself built on httpx2, imported only by the analyzer's builder; step 10). Sync code throughout the worker. | Widen `requires-python` once every heavy extra ships wheels for the new version on linux/aarch64; `uv lock` enforces this through `tool.uv.required-environments`. |

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
  log.py               configure_logging: stderr handler with a secret-redacting, URL-scrubbing
                       formatter and a wire-logger gate; scrub_urls; adopt_library_loggers
  models.py            domain models
  errors.py            TransientError, PermanentError, SkipEpisode
  artifacts.py         ArtifactStore: per-episode cache paths, atomic writes
  db.py                SQLite connection and migrations (PRAGMA user_version)
  jobs.py              JobStore
  fetch.py             GuardedFetcher: guarded GET and streaming download, address policy,
                       pinned-address network backend (step 06)
  audio.py             HttpAudioFetcher, the `http` AudioFetcher backend (step 06)
  ingest.py            enqueue_ingest: IngestRequest → canonical URL → JobStore (API and CLI)
  episodes.py          stored episodes: list_episodes (GET /episodes, `earmark episodes`),
                       require_transcript and enqueue_reanalyze (POST /episodes/{key}/analyze,
                       `earmark reanalyze`); reads artifacts only (step 17)
  fakes.py             fake backend for every stage (tests and smoke runs); FakeThemer and
                       FakeStore.write_digest for digests (step 18)
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
  transcribe/          base.py (CachedTranscriber: cache, transcript files, audio deletion;
                       TranscriptionEngine; segment_from), faster_whisper.py (the
                       `faster_whisper` engine, step 08), remote.py (the `remote` engine:
                       OpenAI-compatible /v1/audio/transcriptions client, step 09),
                       compat.py (known-incompatible extra versions from package
                       metadata, light; step 19)
  endpoint.py          host_label, error_detail: shared by the operator-endpoint clients
                       (remote ASR, local LLM, ntfy; step 11)
  analyze/             schema.py (output JSON schema, wire models, build_analysis), prompts.py
                       (one-shot, map, reduce, and repair prompts and message builders),
                       render.py (render_analysis), analyzer.py (LlmAnalyzer, the `anthropic`
                       and `openai_compat` Analyzer: strategy selection), calls.py (ModelCaller:
                       fit, completion retry, parse and repair), chunking.py (token estimate,
                       part planning), merge.py (entity merging, PartNote), mapreduce.py
                       (run_map_reduce; step 11), llm/ (base.py: the LLMClient Protocol;
                       anthropic.py: AnthropicClient, step 10; openai_compat.py:
                       OpenAICompatClient, step 11)
  vault/               sanitize.py (file names, fold keys, Markdown and YAML escaping), fs.py
                       (Vault, VaultDir: fd-based symlink-refusing I/O, vault_lock), document.py
                       (frontmatter emit/merge/identity, managed regions), notes.py
                       (VaultStore, the `vault` Store; step 12; write_digest, the `vault`
                       DigestWriter, step 18), entities.py (kinds,
                       entity ids, mentions, EntityRegistry over SQLite, entity note
                       rendering; step 13), aliases.py (aliases.yaml parsing; step 13)
  index/               search.py (SearchQuery, SearchHit, SearchResults, the Searcher Protocol,
                       search errors, snippets), chunks.py (index rows: transcript chunks and
                       analysis items, digest; pure), embed.py (Embedder Protocol,
                       FastEmbedEmbedder, build_embedder), lancedb_index.py (LanceIndex: the
                       `lancedb` Indexer and Searcher), rebuild.py (reindex); step 14
  notify/              ntfy.py (split_topic_url, obsidian_url, compose, NtfyClient, NtfyNotifier:
                       the `ntfy` Notifier; step 15)
  doctor/              report.py (CheckResult, render, exit code), checks.py (offline checks),
                       online.py (--online reachability checks), __init__.py (run_doctor,
                       Probes); `earmark doctor` (step 15)
  digest/              window.py (DigestWindow, --week/--since parsing, local midnights),
                       model.py (digest types; the Themer and DigestWriter Protocols),
                       collect.py (episodes and leads in a window, input hash), prompts.py
                       (theme prompts and schema, DIGEST_SCHEMA_VERSION), themes.py
                       (LlmThemer: one request, or batches merged level by level), cache.py
                       (DigestCache), render.py (the digest note region), run.py
                       (run_digest); `earmark digest` (step 18)
tests/                 mirrors src; fixtures/ holds recorded Apple JSON and RSS, and
                       share_sheet.json (real iOS share-sheet captures, added by hand)
Dockerfile, .dockerignore, compose.yaml    the container image and services (step 16)
deploy/systemd/        example units for a native install (step 16); the digest service and
                       weekly timer (step 18)
```

## Domain models

Defined in `models.py` (step 02; exact types and validators in
`docs/steps/02-contracts.md`). Steps may add fields, with defaults so existing artifacts
still load. All models are pydantic, frozen, and `extra="forbid"`; sequences are tuples
(pass `tuple(...)`: mypy rejects a list for a tuple field); datetimes are timezone-aware;
seconds are finite and non-negative. The shared `Model` base replaces every lone UTF-16
surrogate (U+D800–U+DFFF) in any string field, including inside tuples, with U+FFFD before
validation (step 13): JSON decoders hand them over, plain `str` fields would accept them,
and then the artifact JSON, SQLite, and vault writes would fail with a `ValueError` that the
runner retries. `models.scrub_surrogates` is the helper; `analyze/calls.py` applies it to
the model's answer and `runner.describe_error` to job text.

- `IngestRequest`: `url`, `prompt: str | None`, `timestamp: datetime | None`. The
  `POST /ingest` body; unknown keys are rejected (a typo such as `promt` must not
  silently drop the user's question). Validators normalise: `url` stripped, at most
  `MAX_URL_CHARS` (2048); `prompt` stripped, blank or whitespace-only becomes `None`,
  at most `MAX_PROMPT_CHARS` (4000) after stripping; `timestamp` must convert to UTC
  (its offset is kept). Step 04.
- `ReanalyzeRequest`: `prompt: str | None`, with exactly `IngestRequest`'s prompt rules and
  unknown keys rejected. The `POST /episodes/{key}/analyze` body; `{}` is the default
  analysis. Step 17.
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

**Entity key.** `models.entity_key(name)`: NFKC, casefold, `+` and `#` kept right after a
letter, digit, or kept `+`/`#` (so `C`, `C++`, `C#` differ), every other punctuation or
symbol character a space, whitespace collapsed. It is the only name normaliser: map-reduce
merging (step 11) and the entity registry (step 13) both use it. `entity_id(kind, key)` is
`sha256("{kind}:{key}")[:16]`.

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
`NeedsChunking(PermanentError)` (step 10) means a transcript does not fit one LLM request
(it carries `model`, `input_tokens`, `limit_tokens`); since step 11 the analyzer catches it
and switches to map-reduce, so it no longer fails a job. Transcripts are never truncated.

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
runner takes. Step 14 adds `FakeEmbedder` (deterministic hashed bag of words, so shared
words rank higher) and `FakeSearcher` (returns configured hits); `FakeIndexer` only
records calls.

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
- **Reanalyze order** (step 17). A `reanalyze` job enters at `analyze`: it reads
  `episode.json` and `transcript.json` for `job.episode_key` (either missing is a
  `PermanentError`), takes the share time from `record_share` (which keeps the stored first
  share), and then runs the same analyze → store → index → complete → notify tail. It
  never calls the resolver, fetcher, or transcriber, and never writes `episode.json`.
  Stages entered: `analyze`, `store`, `index`. With several workers, an ingest and a
  reanalyze of one episode can each store without the other's analysis; the next store of
  that episode repairs the note. One worker, the default, cannot hit this.
- **Outcomes.** `SkipEpisode` → `skipped`; `PermanentError` → `failed`;
  `TransientError` → requeued with backoff (honouring `retry_after_s`) until attempts
  run out; any other `Exception` → transient, with the traceback logged at ERROR and a
  one-line summary naming the raising file, line, and function in `job.error`. Error
  and warning text is whitespace-collapsed, redacted of every `Settings` secret, and
  capped at 500 characters; the text of a non-earmark exception also has URL paths and
  queries scrubbed (`log.scrub_urls`, step 20). Index failures, and notifier failures,
  are warnings (`index failed: …`, `notify failed: …`) and never change the status.
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

### Transcribing

Step 08; exact signatures in `docs/steps/08-transcribe.md`.

- **Shell and engine.** `transcribe.base.CachedTranscriber(artifacts, engine,
  keep_audio=...)` is the `Transcriber`: an existing `transcript.json` is returned without
  calling the engine (and leftover audio is deleted unless `KEEP_AUDIO`); otherwise
  `engine.transcribe_file(audio, key)` returns a `Transcript`, which is written with
  `ArtifactStore.write_transcript` (`transcript.txt`, then `transcript.json`, both
  atomic), and the episode's `audio.*` is deleted unless `EARMARK_KEEP_AUDIO`. On any
  failure the audio is kept, except `UnusableAudioError` (a `PermanentError`: the file is
  not decodable audio, or decodes to no samples), which deletes it unless `KEEP_AUDIO`
  so a re-share downloads it again. "No speech found" is `NoSpeechError` (a
  `PermanentError`, not an `UnusableAudioError`): the job fails and the audio is kept,
  because on a podcast it more likely means a broken engine than a silent file (step
  19). `base.segment_from` is the shared monotonic mapping (starts never decrease,
  `end >= start`, all within the duration; blank segments dropped).
- **`faster_whisper` engine.** `transcribe.faster_whisper.FasterWhisperEngine` imports
  `faster_whisper` and `ctranslate2` only through `load_runtime()`
  (`importlib.import_module`), which the factory's builder calls; without the extra the
  worker refuses to start with a `ConfigError` naming the extra. The model is loaded on
  the first transcription and kept for the life of the process (`ASR_MODEL`,
  `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, downloaded to `ASR_MODEL_DIR`). Audio is decoded with
  PyAV (bundled FFmpeg; whole file in memory, about 230 MB per hour), transcribed with
  beam size 5 and VAD filtering, and segments are made monotonic (starts never decrease,
  `end >= start`, all within the duration; blank segments dropped). `ASR_LANGUAGE`
  forces a language, else Whisper detects it. Progress is logged about once a minute;
  CTranslate2 releases the GIL, so the heartbeat thread keeps beating.
- **Errors.** Model not found on Hugging Face, or a `ValueError` at load (bad model
  name, unsupported compute type) → `PermanentError` naming the settings; other
  `OSError`s while loading (download failures) → `TransientError`. `ASR_DEVICE=cuda` with
  no CUDA device visible to CTranslate2 is a startup `ConfigError`. Messages carry file
  base names and setting names, never full paths, URLs, or library error text.
- **Library errors are not bad audio** (step 19; `docs/steps/19-library-errors.md`).
  `load_runtime()` also imports `av`. Its `decode_audio` raises
  `transcribe.faster_whisper.AudioDecodeError` for bad input only:
  - PyAV's `av.error.InvalidDataError` or `av.error.EOFError`;
  - an `IndexError` from a container that a plain `av.open` probe confirms has no
    audio stream.

  The engine turns `AudioDecodeError` and a zero-sample decode into
  `UnusableAudioError`. Every other exception from decoding or the model propagates
  unchanged to the runner's unexpected-exception path (transient, traceback logged,
  audio kept). That covers PyAV 19's `TypeError` with faster-whisper 1.2.1, `OSError`s
  (PyAV's included), `MemoryError`, `DecoderNotFoundError` and PyAV's other
  capability errors, and code bugs. Of the model's `ValueError`s, only "not a valid
  language code" from the `transcribe(...)` call is a `PermanentError` (check
  `ASR_LANGUAGE`).
- **`remote` engine** (step 09; exact signatures in `docs/steps/09-remote-asr.md`).
  `transcribe.remote.RemoteAsrEngine` POSTs the audio as streamed `multipart/form-data`
  (64 KiB chunks, `Content-Length` from the file) to `{ASR_BASE_URL}/audio/transcriptions`
  with `file=audio.<ext>`, `model=ASR_MODEL`, `response_format=verbose_json`,
  `timestamp_granularities[]=segment`, `language` when `ASR_LANGUAGE` is set, and
  `Authorization: Bearer ASR_API_KEY` only when the key is set. `Transcript.model` is
  `remote:<ASR_MODEL>`. Segments map through `segment_from`; a response with text but no
  segment timestamps (no segments, or none with an end above 0) becomes one segment with
  the warning "the ASR server returned no segment timestamps; …". Errors: file larger
  than `ASR_MAX_UPLOAD_MB` → `PermanentError` before any request (audio kept); empty file
  → `UnusableAudioError`; empty text → `NoSpeechError` (audio kept); 408, 429, 5xx,
  timeouts, connection and protocol errors → transient (`Retry-After` honoured); 3xx
  (never followed), other 4xx, and an unreadable or oversized (64 MiB) response →
  permanent. A 200 body without a string `text` field (for example `{}` or
  `{"error": ...}`) is unreadable: "not a transcription". Messages name the host and the
  setting to check, and carry at most 200 characters of the server's error message with
  the key redacted. Read and write timeouts are `ASR_TIMEOUT_SECONDS`. Needs no extra;
  `ASR_BACKEND=remote` without `ASR_BASE_URL` is a startup `ConfigError`.
- **`earmark transcribe <file> [--json]`** runs the remote engine when
  `EARMARK_ASR_BACKEND=remote`, otherwise the local faster-whisper engine with the
  `EARMARK_ASR_*` settings; prints `[HH:MM:SS] text` lines or the `Transcript` JSON
  (warnings on stderr), and writes nothing outside the model cache.

### Analyzing

Step 10; exact signatures in `docs/steps/10-analysis.md`.

- **Shell and client.** `analyze.analyzer.LlmAnalyzer(artifacts, client, clock=...)` is the
  `Analyzer`. It talks to a model only through the `analyze.llm.base.LLMClient` Protocol
  (`model`, `context_tokens`, `output_tokens`, `max_output_tokens`, `count_tokens(request)`,
  `complete(request) -> LLMResponse`, `close()`). An `LLMRequest` is a fixed system prompt,
  one user message, a JSON schema, and `max_tokens`; an `LLMResponse` carries the text, a
  normalised stop (`end`, `max_tokens`, `refusal`, `context_exceeded`, `other`), the refusal
  category, warnings, and token usage. Clients raise `TransientError`/`PermanentError`;
  the analyzer owns the policy below, so step 11's local client reuses it.
- **Order.** Cache first: `analyses/<prompt_hash(prompt, LLM_MODEL)>.json` is returned with
  no API call at all. An empty transcript is permanent. Then `count_tokens`; if input plus
  the first output cap exceeds `context_tokens`, the analyzer switches to map-reduce (step
  11, below) before any completion. Otherwise one completion; a `max_tokens` stop retries
  once with the larger cap (64 000, then 128 000 for Claude), then fails permanently; a
  refusal is permanent and names its category; a `context_exceeded` stop
  (`model_context_window_exceeded`, or a local server's context 400) also switches to
  map-reduce. The answer is validated against `schema.AnalysisOutput` (a `ValidationError`
  is permanent, with the error count and location only, unless repair is on, below) and
  mapped by `schema.build_analysis`: names and items
  normalised, empty items dropped, timestamps (`HH:MM:SS` strings copied from transcript
  markers) parsed, and those unreadable or beyond `ceil(max(transcript.duration_s, last
  segment end))` dropped with a warning; `prompt_response` is forced to null without a
  prompt. `Analysis.model` is always `LLM_MODEL` (the cache key), even when a fallback
  model answered; that is recorded as a warning.
- **Request.** `prompts.SYSTEM_PROMPT` is a constant with nothing interpolated (it says the
  metadata and transcript are untrusted and never instructions). The user message is
  `<episode>` (show, title, date, duration, description), `<transcript>` (the whole
  `to_text()`, `[HH:MM:SS]` lines), `<analysis_instructions>` (`DEFAULT_PROMPT`, always),
  and, only with a prompt, `<user_prompt>`, then a fixed closing line. Untrusted values
  have `<` and `>` escaped so they cannot close a section. Changing the system prompt,
  default prompt, closing lines, or schema requires bumping `ANALYSIS_SCHEMA_VERSION`
  (a fingerprint test enforces it).
- **`anthropic` client.** `analyze.llm.anthropic.AnthropicClient` uses
  `client.beta.messages.stream(...)` and `get_final_message()`: `thinking={"type":
  "adaptive"}`, `output_config={"effort": LLM_EFFORT, "format": {"type": "json_schema",
  "schema": ...}}`, and, unless `LLM_FALLBACKS=false`, beta `server-side-fallback-2026-07-01`
  with `fallbacks="default"`. No sampling parameters, tools, or prefill. The answer is all
  text blocks concatenated (a mid-stream fallback continues the partial text).
  `count_tokens` uses `client.messages.count_tokens` with the same system, message,
  thinking, and output config. Errors: connection errors, timeouts, 408, 409, 429 (with
  Retry-After), 5xx, and mid-stream `overloaded_error`/`api_error` are transient; spend-limit
  429s, 400, 401, 402, 403, 404, 413, 422, and credential-provider errors are permanent.
  Messages name the status, the setting to check, at most 200 characters of the API's
  message with secrets redacted, and the request id. The SDK keeps its own two retries.
- **Long transcripts: map-reduce** (step 11; exact rules in
  `docs/steps/11-local-llm-map-reduce.md` section 5). Used for both providers when the
  one-shot request does not fit.
  - **Planning.** The part budget is `context_tokens − output cap − counted overhead of
    an empty map request − 16`, and must be at least 1024, else a `PermanentError`.
    Segments are packed in order into balanced parts that never exceed it, using the
    estimate `ceil(UTF-8 bytes / 3) + 4 per line`. Every proposed part is then counted
    with the client's `count_tokens` (exact for Anthropic) and halved until it fits.
  - **Coverage and timestamps.** Parts do not overlap, and each segment is in exactly
    one. A single segment too long for a part is split at sentence boundaries; every
    piece keeps its start, and a warning is added. Lines keep their absolute
    `[HH:MM:SS]` markers, and the part tag carries the absolute range.
  - **Map.** Each part gets `MAP_SYSTEM_PROMPT`, the episode metadata without the
    description, and the part's transcript, and answers with the same output schema.
    Timestamps outside the part are dropped with a warning. Answers are cached under
    `analyses/<prompt_hash>.parts/<sha256(request)[:16]>.json` until the final analysis is
    written, so a retried job skips finished parts. A part that hits the output cap with
    no larger cap left is split in two.
  - **Reduce.** Entities are merged in code by `entity_key` (NFKC, casefold, punctuation
    and symbols removed, whitespace collapsed; first name, first non-blank context,
    union of timestamps; within one kind). `REDUCE_SYSTEM_PROMPT` combines the part notes
    (summary, claims, questions, leads, answers) and the merged entities into one
    `AnalysisOutput`. If they do not fit one request, groups are reduced level by level.
    The final entity lists are merged again and their timestamps unioned with the
    part-level ones.
  - **Warning.** The analysis records that it was analyzed in N parts. The cache key and
    `ANALYSIS_SCHEMA_VERSION` do not change; the map and reduce prompts are pinned by a
    second fingerprint test.
- **Repair.** With `repair_invalid_json` (on for `openai_compat`, off for Anthropic), an
  answer that fails schema validation gets exactly one repair request:
  `REPAIR_SYSTEM_PROMPT`, the escaped invalid answer, and the pydantic error locations
  and types (no model text). A second failure is permanent.
- **`openai_compat` client.** `analyze.llm.openai_compat.OpenAICompatClient` (step 11).
  - **Request.** It sends one non-streaming `POST {LLM_BASE_URL}/chat/completions` with
    `model`, `[system, user]` messages, `max_tokens`, `stream: false`, and
    `response_format`. That is `{"type": "json_schema", "json_schema": {"name", "strict":
    true, "schema"}}` by default, the one form vLLM, llama.cpp, Ollama, and LM Studio all
    accept. `LLM_RESPONSE_FORMAT=json_object` sends `{"type": "json_object"}` and appends
    the schema to the user message. No sampling or reasoning parameters are sent.
  - **Tokens.** `count_tokens` is the local estimate plus the schema and 64 tokens; it
    makes no request. The output cap is `min(16384, context/4)`, and the retry cap
    `min(32768, context/2)`.
  - **Response.** The answer is `choices[0].message.content`, with a leading
    `<think>…</think>` block and one surrounding code fence stripped. `reasoning` and
    `reasoning_content` are ignored. `finish_reason` maps `stop` → end, `length` →
    max_tokens, `content_filter` (or a `refusal` field) → refusal.
  - **Errors.** A 400 or 413 whose body names the context (`exceed_context_size_error`,
    "maximum context length", …) is the `context_exceeded` stop. 408, 429, 5xx,
    timeouts, and transport errors are transient. 401, 403, 404, other 4xx, 3xx (never
    followed), and an unreadable or oversized (16 MiB) body are permanent. Messages name
    the host and the setting to check.
  - **Configuration.** Read timeout `LLM_TIMEOUT_SECONDS`. The builder requires
    `LLM_BASE_URL` and `LLM_CONTEXT_TOKENS` (at least 8192), and refuses `claude-*` model
    ids (Claude goes through the SDK only).
- **Credentials.** `EARMARK_LLM_API_KEY` is passed to the SDK as `api_key` when set (then
  no `ANTHROPIC_*` credential variable or profile is consulted); otherwise the SDK's chain
  applies (`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the process environment, an
  `ant auth login` profile, workload identity). With no credential the builder raises a
  `ConfigError` naming `EARMARK_LLM_API_KEY`, so the worker refuses to start.
- **`render.render_analysis(analysis, heading_level=2)`** returns the deterministic Markdown
  section step 12 places in the note: a title (`Default analysis` or `Analysis: <prompt>`),
  the prompt quoted, the summary, `Answer`, then Key claims, People, Papers, Companies,
  Tools, Topics, Open questions, Research leads (empty ones omitted), timestamps as
  `[HH:MM:SS]`, `<` escaped, and leading Markdown block syntax backslash-escaped.
- **`earmark analyze <episode_key> [--prompt] [--json]`** analyzes a stored transcript with
  the configured provider (writing or reusing the analysis cache, not the note) and prints
  the rendered section or the `Analysis` JSON; warnings go to stderr.

### Storing

Step 12; exact rules in `docs/steps/12-vault-writer.md` section 5.

- **`vault` Store.** `vault.notes.VaultStore(vault_dir, subdir, data_dir=, tz=)`, built by
  `build_vault_store(settings)`. It needs `VAULT_DIR`, which must exist; otherwise the
  worker refuses to start with a `ConfigError`. The build creates nothing. Each `store`
  holds `<DATA_DIR>/vault.lock` (`flock`; a 60 s wait is transient). It scans the subdir
  once for note identities, settles the episode-note and transcript-note paths, writes
  the transcript note and then the episode note, and returns both vault-relative paths
  plus any warnings. Note contents depend only on the inputs, `TZ`, and the paths on
  disk, never on a clock. A store whose output equals the file writes nothing.
- **Filesystem.** `vault.fs` reaches everything below `VAULT_DIR` only through directory
  fds with `O_NOFOLLOW` (`VAULT_DIR` itself may be a symlink). A symlink or
  non-directory on the write path is a `PermanentError` (`SymlinkRefused`). A symlink at
  a note's name only makes that name occupied. Reads use `O_NONBLOCK` and require a
  regular file of at most 16 MiB. Writes go to `.earmark-<16 hex>.tmp`, are fsynced,
  checked against the stamp of the earlier read, and renamed (`NoteChanged`, a
  transient error, after 3 attempts). Nothing in the vault is ever deleted or renamed,
  except earmark's own temp files older than one hour.
- **Errors.** A missing `VAULT_DIR`, `ENOSPC`, `EDQUOT`, and other `OSError`s are
  transient. `EACCES`, `EPERM`, `EROFS`, `ENAMETOOLONG`, symlinks, and an exhausted
  collision suffix are permanent. Messages name vault-relative paths and errno names
  only.
- **Entities** (step 13; exact rules in `docs/steps/13-entities.md` section 5). Each
  `store` also updates the entity registry and the entity notes, under the same lock. It
  scans every note identity (`earmark: episode|transcript|entity`), reads `aliases.yaml`,
  replaces the episode's mentions in SQLite, and resolves entities through the aliases. It
  writes the transcript note, then every entity note whose content digest changed, then
  the notes of other episodes whose links went stale (a renamed or moved entity or episode
  note), and the episode note last. Then it records paths and digests. Links are full
  vault-relative paths regenerated from the paths on disk, so they resolve after renames
  and moves. A store with unchanged inputs and vault writes nothing. Registry errors:
  `sqlite3.OperationalError` and other `sqlite3.Error` are transient; a database newer than
  the code is permanent. Alias problems and failed relinks are warnings.
- **`earmark entities rebuild [--prune]`** holds the lock, empties the registry, refills
  it from `episodes/*/{episode,transcript,shared}.json` and `analyses/*.json`, and
  regenerates every earmark note. On an intact vault that writes nothing. It refuses (exit
  1) when `aliases.yaml` is unreadable, or when episode notes in the vault have no
  artifacts, unless `--prune`. Run it once after upgrading from step 12, and after
  changing `TZ`.
- **Frontmatter** is written by a constrained emitter (`vault.document`), not by a YAML
  library: fixed keys, every string a one-line double-quoted scalar with `\uXXXX`
  escapes, unquoted dates, and naive local date-times. Existing frontmatter is never
  parsed as YAML; it is split into lines and top-level keys.
- **`VaultStore.locate_notes(keys)`** (step 14) scans the subdir read-only (no lock, no
  registry, nothing created, through `Vault.open_existing_subdir`) and returns each
  episode's episode-note and transcript-note paths, the smallest path among duplicates.
  `earmark reindex` uses it.

### Indexing and search

Step 14; exact rules in `docs/steps/14-vector-index.md` sections 4 and 5.

- **Extra and imports.** `lancedb` (with pyarrow) and `fastembed` (with onnxruntime) are the
  `index` extra, imported only through `importlib` in `lancedb_index.load_lance_runtime()`
  and `embed.load_fastembed_runtime()`. A missing extra is a `ConfigError` naming
  `EARMARK_INDEX_BACKEND` and `uv sync --extra index`: the worker refuses to start, while
  `earmark serve` starts and answers `/search` with 503 and that reason.
- **Rows.** `index.chunks.build_rows` turns an episode into rows with a `kind`:
  `transcript` chunks (runs of whole segments, at most 1000 characters and 120 s, start =
  first segment start, end = largest segment end; a segment too long for one row is split
  at sentence, then word, boundaries and every piece keeps the segment's times), and per
  analysis `summary`, `claim`, `lead` (text and rationale), `question`, and `answer` (the
  prompt response). Claims and leads carry their timestamp. Every row text must pass the
  embedder's `fits` (no tokenizer truncation: bge-small stops at 512 tokens, other models
  at 128), so nothing is silently cut. Rows carry the episode's show, title, date, URL,
  prompt hash, prompt (200 characters), and note paths. Ids are
  `<key>:<kind>:<prompt_hash or ->:<ordinal>`; a `source_digest` over all rows, the model,
  and the dimension is stored in each.
- **Embedder.** `embed.FastEmbedEmbedder` validates `EMBED_MODEL` against fastembed's model
  list at build time (no I/O) and constructs the model on first use (fastembed looks up or
  downloads the model in its constructor). The worker and `earmark reindex` may download it
  into `EMBED_MODEL_DIR` (default `<DATA_DIR>/models/embed`); the API and `earmark search`
  never download, and answer 503 / exit 1 until the model is there.
- **Table.** One LanceDB table, `chunks`, under `<DATA_DIR>/lancedb`. Its schema metadata
  records `earmark.index_version` (1), `earmark.embed_model`, and `earmark.embed_dim`. A
  table whose metadata or vector size differs is refused on write and on search with
  `IndexMismatchError`, whose message names both models and says to run `earmark reindex`;
  the check runs before the model is loaded.
- **Writes.** `LanceIndex.index` (the `Indexer`) builds the rows, skips everything when the
  stored digest is unchanged (a re-share embeds nothing), else embeds and replaces the
  episode's rows in one `merge_insert` commit (update, insert, delete-by-source for that
  episode). Writers hold `<DATA_DIR>/index.lock` (`flock`, 300 s, transient on timeout) for
  one episode at a time. Every 20th table version is followed by `optimize` with a one-hour
  cleanup window. LanceDB failures are transient with short messages; on the worker every
  failure is an `index failed: …` warning.
- **Search.** `LanceIndex.search(SearchQuery)` (the `Searcher`) never creates files (a
  missing directory or table is an empty result with `index_exists=False`), checks the
  metadata, embeds the query, and runs an exact cosine search with an optional prefilter
  on `kind` and `episode_key`, built only from validated values. Hits are ranked by
  distance, scored as cosine similarity, and carry the episode, show, kind, timestamp
  (`HH:MM:SS`), a 300-character snippet, the note paths, and the Apple URL. Queries are
  1–500 characters (whitespace collapsed); limits 1–50, default 10.
- **Reindex.** `earmark reindex` drops and recreates the table, then re-indexes every
  episode under `data/episodes/` that has a transcript and at least one analysis, reading
  its artifacts inside the lock, with note paths from `locate_notes`; then optimizes.
  Search returns fewer hits while it runs (LanceDB OSS has no `rename_table`, so there is
  no swap). `--episode KEY` re-indexes one episode without the drop. Rows equal what the
  worker writes for the same note paths.
- **`earmark search QUERY [--limit] [--kind ...] [--episode] [--json]`** prints ranked hits
  (cleaned of control characters) or `SearchResults` JSON.

### Notifying

Step 15; exact rules in `docs/steps/15-notify-doctor.md` sections 4 and 5.

- **`ntfy` Notifier.** `notify.ntfy.NtfyNotifier` is built by `build_ntfy_notifier(settings)`.
  It is `None` (nothing sent, worker starts) when `EARMARK_NTFY_URL` is unset, and the build
  makes no request. For each job that ends `done`, `failed`, or `skipped` it sends one
  `POST` of JSON to the server root: the topic URL minus its last segment. The body is
  `topic`, `title`, `message`, `tags`, `priority`, and an optional `click`. The only headers
  are fixed, plus `Authorization: Bearer EARMARK_NTFY_TOKEN` when a token is set. No
  untrusted text goes into a header, the URL, or a query string. `markdown` is never set.
- **Content.**
  - Titles are `Note ready: <episode>`, `Failed: <episode>`, and `Skipped: <episode>`
    (`podcast episode` when unknown). Tags are `white_check_mark`, `x`, and
    `fast_forward`; priority is 4 for failed and 3 otherwise.
  - The message starts with the show (or the canonical Apple URL). Done adds the prompt,
    the warning count and first warning, and the note path when there is no link. Failed
    adds the stage, `Reason: <job.error>`, and the attempts. Skipped adds the reason.
  - Reanalyze jobs (step 17) are titled `Analysis ready: <episode>`, `Analysis failed:
    <episode>`, and `Analysis skipped: <episode>`. Every one of their messages names the
    prompt (`Prompt: …`, or `Default analysis`), and the show line falls back to `Episode
    <key>`. Ingest notifications are unchanged.
  - Every value is cleaned: NFC; control, format (except ZWJ/ZWNJ), private-use,
    unassigned, and line or paragraph separator characters removed; whitespace collapsed;
    `=?` defused, because ntfy RFC-2047-decodes titles even in JSON mode; secrets
    redacted. It is then cut at a safe boundary: title 250 bytes, message 2000 bytes, so
    the message is never turned into an attachment.
- **Link.** Done notifications with `EARMARK_OBSIDIAN_VAULT_NAME` and a note path carry
  `click = obsidian://open?vault=<quote(name, safe="")>&file=<quote(note_path, safe="")>`.
  Everything but `A–Z a–z 0–9 _ . - ~` is percent-encoded (`/` as `%2F`, space as `%20`),
  the `.md` is kept, and neither value is normalised. The link is omitted when the path
  contains `#` (Obsidian reads it as a heading) or the URL exceeds 2000 characters.
- **Errors.** 3xx (never followed), 401, 403, 404, and other 4xx are permanent; 408, 429
  (`Retry-After`), 5xx, timeouts (connect 5 s, read, write, and pool 10 s), and transport
  errors are transient. Messages name the host and the setting to check, never the topic or
  token. There is one attempt, with no retry. The notifier raises; `Pipeline.notify` logs
  the failure and records `notify failed: …` as a job warning, so a notification problem
  never changes a job's status or escapes the worker.

### Doctor

Step 15; exact checks in `docs/steps/15-notify-doctor.md` sections 5.5–5.7.

- **`earmark doctor`** prints one line per check (`ok`, `warn`, `FAIL`, `skip`) and a
  summary. It exits 0 when nothing failed (warnings allowed), 1 when a check failed, and 2
  when settings do not load.
- **Offline checks.**
  - The API token.
  - `DATA_DIR` and the vault subdir writability, by creating and removing one random
    temp file each. The vault probe goes inside the subdir only, through `vault.fs`; when
    the subdir does not exist yet, `os.access` is used on `VAULT_DIR`.
  - The database: schema version, journal mode, `quick_check`, and job counts, read
    without migrating. A missing file is never created.
  - The `obsidian://` vault name.
  - Each selected backend, built through the same factory registry as the worker. That
    imports its extra, runs the CUDA check, and checks credentials (the Anthropic SDK
    constructor finds the credential chain).
  - Whether the Whisper and embedding models are in the cache: faster-whisper's
    `download_model(local_files_only=True)` and `huggingface_hub.try_to_load_from_cache`.
  - `asr libraries` (step 19, `faster_whisper` only): the installed `faster-whisper` and
    `av` versions, read from package metadata (`importlib.metadata`, through
    `Probes.installed`), checked against `transcribe.compat.KNOWN_INCOMPATIBLE`. It
    warns on a known-bad pair (today faster-whisper <= 1.2.1 with av >= 19), or when a
    version cannot be read. A package installed from a source tree (PEP 610
    `vcs_info`/`dir_info`) is not checked by version.
  - Whether an existing index matches `EMBED_MODEL`.

  Offline doctor makes no network call, loads no model, downloads nothing, and creates no
  directory or database. Fake backends are warnings.
- **`--online` checks.** Each has a 10 s timeout (connect 5 s), follows no redirects,
  ignores proxy variables, and spends nothing:
  - the iTunes lookup (`?id=1&entity=podcast`) through `fetch.py`, so the address guard
    applies;
  - `GET {ASR_BASE_URL}/models` and `GET {LLM_BASE_URL}/models` (the configured model
    listed?);
  - Anthropic `GET /v1/models/{LLM_MODEL}` through the SDK with `max_retries=0`. It is a
    free metadata read; the model's lifecycle, deprecated or retired, is reported. No
    completion or token-count request is sent.
  - ntfy `GET /v1/health` and, with a token, `GET /v1/account`.

  Doctor never publishes a notification.
- **Digest notifications** (step 18) come from `earmark digest`, not from the `Notifier`:
  `compose_digest` builds `Digest ready: <label>` with counts and the note link, cleaned like
  job notifications, and `NtfyClient.publish` sends it once.
- **Redaction.** Details never contain secret values, their prefixes, or their lengths.
  Endpoints appear as host labels only, and server error text goes through
  `endpoint.error_detail`. Every line is passed through `log.redact` with all `Settings`
  secrets and cleaned of non-printable characters.

### Re-running a prompt

Step 17; exact rules in `docs/steps/17-reanalyze.md` section 5.

- **Enqueue.** `episodes.enqueue_reanalyze(jobs, artifacts, key, request)` is shared by
  `POST /episodes/{key}/analyze` and `earmark reanalyze`. It checks the key
  (`is_episode_key`) before building a path, then only stats files: no `episode.json` is
  "not found" (404, CLI exit 1); `episode.json` without `transcript.json` is "no transcript
  yet" (409, CLI exit 1). It parses nothing and writes nothing under `DATA_DIR`. Dedupe is
  the job store's: the same key and normalised prompt while a job is active returns it.
- **Already run.** There is no shortcut: the job always runs. A cached prompt (same
  prompt, `LLM_MODEL`, and schema version) makes no LLM call; the store regenerates the
  note (restoring it if it was deleted, writing nothing if it is intact), and the index
  skips an unchanged digest.
- **Listing.** `episodes.list_episodes(artifacts, limit=, offset=)` reads
  `episodes/*/episode.json` and `shared.json` for every episode, to sort by first share
  time, newest first, with episodes that have no share record last. Then it reads the
  analyses of the page only, one file at a time. Unreadable files are logged and left
  out. It never opens SQLite, scans the vault, or parses a transcript (`has_transcript` is
  a stat), so it is the same before and after `earmark entities rebuild` and with any
  store backend. Offset pagination: `limit` 1–200 (default 50), `offset` 0–1 000 000;
  `total` and `next_offset` in the response.
- **`earmark reanalyze KEY [--prompt]`** prints `<job_id> <status>` (plus ` (existing
  job)`), like `earmark submit`. **`earmark episodes [--limit] [--offset] [--json]`**
  prints one line per episode (key, date in `TZ`, show — title), one per analysis (hash,
  model, prompt), and a paging footer, or the `EpisodePage` JSON.

### Digest

Step 18; exact rules in `docs/steps/18-digest.md` sections 4 and 5. A CLI command only; the
worker never runs it.

- **Window.** Episodes are selected by their first share time (`shared.json`), as instants:
  `start <= first_shared_at < end`, start inclusive, end exclusive. Both bounds are local
  midnights in `TZ`: the wall-clock 00:00 with `fold=0`, which is the first instant of the
  day across DST gaps and repeats.
  - `--week 2026-W47` is that ISO week, Monday to Monday.
  - No option means the last complete ISO week.
  - `--since Nd` (1–366) is the N local calendar days ending today, today included:
    `[midnight(today − N + 1), midnight(today + 1))`.
- **Leads.** Every readable analysis of each episode in the window contributes, oldest
  first, its `research_leads`, deduplicated within the episode by `entity_key(text)`.
  - Episodes are ordered by `(first_shared_at, key)`, and leads get ids `L1…Ln` in that
    order.
  - Unreadable files are skipped with a warning.
  - An episode without analyses is listed and adds no leads.
- **Themes.** `Themer.themes(ThemeRequest) -> ThemeRecord`. `LlmThemer` runs on the
  configured `LLMClient` (`anthropic` or `openai_compat`, built by
  `factory.build_themer`), through `analyze.calls.ModelCaller`, so the fit check, the
  `max_tokens` retry, refusals, and repair behave as in analysis.
  - One request carries the constant `DIGEST_SYSTEM_PROMPT`, the leads as escaped
    `<item id="L…">` sections, and a fixed schema (`themes: [{title, summary, items}]`,
    no per-request enum).
  - The answer is validated in code: ids not offered are ignored, an id placed twice
    stays in its first theme, and unplaced leads go to "Other leads". Each case is counted
    in a warning.
  - When the leads do not fit one request, they are packed into batches (estimated, then
    counted and halved until each fits). The batches' themes are merged by further
    requests, level by level, until one request holds them.
  - A level that does not shrink, a single lead too long for a request, or a part budget
    under 1024 tokens is a `PermanentError`. Nothing is truncated.
  - `fake` is `FakeThemer`: one theme per show.
- **Cache.** `DATA_DIR/digests/<input_hash>.json` (`ThemeRecord`) is keyed by
  `sha256({v: DIGEST_SCHEMA_VERSION, model, leads: [[id, key, text, rationale, show,
  title], …]})[:16]`. It is read before any model request, so a rerun with the same inputs
  makes no request at all (not even a token count).
  - Answers of an unfinished multi-request run are kept under
    `digests/<input_hash>.calls/` until the record is written.
  - `--refresh` bypasses both.
  - The prompts and schema are pinned to `DIGEST_SCHEMA_VERSION` by a fingerprint test.
- **Note.** `DigestWriter.write_digest(DigestNote)`: the `vault` store's
  `VaultStore.write_digest`, or `FakeStore.write_digest` for `fake`.
  - Under `vault.lock` it scans the subdir, takes every episode's episode-note and
    transcript-note paths from that scan (smallest path among duplicates), and renders
    the region from them. Every link is code-built from a path that exists.
  - It then writes `Digests/<file stem>.md` through `_write_note`: owned frontmatter,
    managed region, backups of hand edits, user text kept.
  - An existing note is found by identity first. A fold-equal name taken by a non-digest
    file gets ` (digest)`.
  - The region has the title, a counts line, one `##` section per theme (escaped title,
    escaped summary, one line per lead), "Other leads", "Episodes", and a details callout
    with no run time.
  - Each lead line links the episode note (`note_link`) and, when the lead has a
    timestamp, the transcript note labelled `HH:MM:SS`. Episodes without a note found
    are plain text.
  - Model text (titles, summaries, lead text) goes through `escape_markdown` (summaries
    also through `render.escape_block_start`), so it cannot form a link, a block, or a
    marker.
- **Empty window.** No episodes means no themer call, no vault access, and no note. No
  leads means no themer call, and the note lists the episodes.
- **Notification.** A created or changed digest is published once through
  `NtfyClient.publish` when ntfy is configured, unless `--no-notify`:
  - title `Digest ready: <label>`;
  - counts only;
  - tag `newspaper`;
  - the `obsidian://` link.

  A failure is a warning.
- **`earmark digest [--week YYYY-Www | --since Nd] [--refresh] [--no-notify]`.**
  - Prints one line: `wrote <path>: …`, `<path> is up to date: …`, or `no episodes were
    first shared in <label> …; nothing written`. Warnings go to stderr. A `--week` window
    that has not ended yet adds `; the window is not over yet`. A `--since` window never
    does (step 20): it always includes today, so the note would be on every run.
  - Exits 0 on success, including an empty window and damaged markers. Exits 1 on model,
    vault, or data errors, and 2 on bad options or configuration.
- **Scheduling.** `deploy/systemd/earmark-digest.timer` (Mondays 07:00, `Persistent=true`)
  starts the oneshot `earmark-digest.service`. Under compose, a host timer or cron runs
  `docker compose run --rm --no-deps worker digest` (`docs/deploy.md` section 14).

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
  per request. A double-tapped share creates one job. Prompts are normalised before the
  key is computed (stripped, blank is `None`), so `{}` and `{"prompt": " "}` are one
  reanalyze job. Ingest and reanalyze jobs never dedupe with each other.
- **Warnings.** `add_warning(job_id, message)` appends unless the same text is already
  there, and works on terminal jobs (notifier failures are recorded after `done`).
- **Time.** Clocks are injected so tests control time. Timestamps are stored as
  fixed-width UTC ISO strings, so SQL compares them as text; a `shared_at` offset is
  kept as an instant, read back in UTC.

## Data layout

```
$EARMARK_DATA_DIR/               a local filesystem: WAL needs shared memory (no NFS/SMB)
  earmark.db                    jobs (step 03); entity registry tables episodes, mentions,
                                aliases, entities (step 13, derived: `earmark entities
                                rebuild`); WAL mode
  vault.lock                    flock held while a process writes the vault (step 12)
  episodes/<episode_key>/       paths owned by artifacts.ArtifactStore
    episode.json                written by the runner after resolve
    shared.json                 ShareRecord: first share time, kept by the runner (earliest wins)
    audio.<ext>                 deleted after transcription (or when it proves undecodable)
                                unless EARMARK_KEEP_AUDIO; kept on every other failure
    .audio.<ext>.<random>.part  download in progress; renamed to audio.<ext> when complete
    transcript.json             written last; its existence marks the transcript cached
    transcript.txt              Transcript.to_text()
    analyses/<prompt_hash>.json   16 hex chars over prompt + model + schema version
    analyses/<prompt_hash>.parts/<part_id>.json   map answers of an unfinished map-reduce
                                (step 11); removed when the analysis is written
  digests/<input_hash>.json     ThemeRecord: the themes of one digest input (step 18, derived;
                                safe to delete, the next run asks the model again)
  digests/<input_hash>.calls/<call_id>.json   answers of an unfinished multi-request digest;
                                removed when the record is written
  index.lock                    flock held while a process writes the search index (step 14)
  lancedb/chunks.lance/         the search index (step 14, derived: `earmark reindex`);
                                created by the first index write, never by a search
  models/embed/                 fastembed model cache (Hugging Face layout) unless
                                EARMARK_EMBED_MODEL_DIR is set; created on first model load
  huggingface/hub/              in the container image only (HF_HOME=/data/huggingface):
                                faster-whisper's model cache when ASR_MODEL_DIR is unset (step 16)

$EARMARK_VAULT_DIR/$EARMARK_VAULT_SUBDIR/        earmark writes nowhere else in the vault
  Podcasts/<Show>/<YYYY-MM-DD> <Title>.md                     where a new episode note goes
  Podcasts/<Show>/transcripts/<YYYY-MM-DD> <Title> (transcript).md
  Backups/<note stem> (<region digest>).md   a note's previous version, before hand edits
                                inside its markers were replaced (step 12)
  Entities/{People,Papers,Companies,Tools,Topics}/<Name>.md   one note per entity (step 13);
                                a taken name gets ` (<entity_id[:8]>)`, then ` (<entity_id>)`
  Digests/<ISO-year>-W<week>.md      weekly digest (step 18), e.g. 2026-W47.md; ISO year and
                                zero-padded week
  Digests/<YYYY-MM-DD> to <YYYY-MM-DD>.md   a `--since Nd` digest (first and last local day);
                                a taken name gets ` (digest)`
  aliases.yaml                  optional, user-maintained entity merges (step 13); earmark
                                reads it and never writes it
```

**Names.** `<Show>` and `<Title>` come from untrusted feed text through
`vault.sanitize.file_component`:

- NFC; control, format (except ZWJ/ZWNJ), and unassigned characters removed;
- `/ \ |` → `-`, `: ` → ` - `, other `:` → `-`, `"` → `'`, `[ ]` → `( )`, and
  `* ? < > # ^` removed;
- no leading or trailing `.` or space, and Windows device names get `_`;
- Unicode kept, capped at 100 (show) or 120 (title) UTF-8 bytes at a safe boundary,
  with `…`; every name at most 200 bytes.

The date is the publish date in `TZ`, or the share date when unknown. A name is taken
when any entry in the directory has the same fold key (NFKC + casefold), so the vault
stays valid on case- and normalization-insensitive filesystems (APFS, iCloud). A taken
name gets ` (<key[:8]>)`, then ` (<key>)`. A fold-equal existing directory is reused.
The transcript note is named after the episode note's actual stem, plus
` (transcript)`, in `transcripts/` beside it.

**Identity.** A note is found again by `earmark: episode|transcript` and `episode_key`
in its frontmatter, by scanning the subdir (not `Backups/`, dot entries, or symlinks).
So a note keeps its first path when the episode is retitled, and the user may rename or
move it anywhere in the subdir. Duplicates (sync conflict copies) give a warning; the
candidate-named or smallest path is updated. Entity notes are found by `earmark: entity` and
`entity_id` (step 13), and digest notes by `earmark: digest` and `digest`, whose value is
`YYYY-Www` or `YYYY-MM-DD..YYYY-MM-DD` (step 18; `NoteKind` includes `digest`).

**Notes.** One note per episode, regenerated from artifacts on every store, so storing
twice produces the same bytes (and writes nothing).

- **Episode note frontmatter**, owned by earmark and rewritten on every store:
  `earmark` (`episode`), `show`, `episode`, `date` (YAML date or null), `url`, `guid`,
  `episode_key`, `duration` (`HH:MM:SS`), `prompts` (non-default prompts, newest first,
  capped at 200 characters), `shared_at` (first share, local time in `TZ`, no offset,
  Obsidian's date-time format), `model` (newest analysis), and `transcript` (a quoted
  wikilink).
- `tags` (`earmark`, `podcast`) is seeded when absent and then belongs to the user.
- Every other frontmatter line (user properties, comments) is kept verbatim after the
  owned keys.
- **Episode note body**: a title, show, date, and duration line; links to Apple
  Podcasts and the transcript note; then one `render_analysis` section per analysis,
  newest first, each showing its prompt and followed by a folded callout with the
  model, the creation time, and the analysis warnings.
- **The transcript note** has its own owned keys (`earmark: transcript`, show, episode,
  date, url, guid, episode_key, duration, language, model, `episode_note`), a link
  back, and one `[HH:MM:SS] text` paragraph per segment.
- Untrusted text in bodies is whitespace-collapsed and Markdown-escaped (`<` →
  `&lt;`; `` \ ` * _ [ ] | ~ = $ % ^ # `` backslash-escaped). Links use full
  vault-relative wikilinks, or percent-encoded Markdown links when a path contains a
  character that breaks wikilinks.

**Managed regions.** Generated body content sits between
`<!-- earmark:begin hash=<16 hex> -->` and `<!-- earmark:end -->`. The hash is the
sha256 prefix of the region as earmark wrote it, and a bare `<!-- earmark:begin -->` is
accepted. Text outside the markers survives regeneration byte for byte, invalid UTF-8
and CRLF included. If the region was edited by hand (the hash does not match and the
content differs from the new one), the previous file is copied to `Backups/` and the
region is replaced, with a job warning. If the markers are missing, duplicated, or out
of order, the note is not touched and a warning says how to restore or regenerate it.
Edits that only change link targets (Obsidian rewrites links when a note is renamed) are
not hand edits (step 13): regions are compared with wikilink and Markdown link
destinations removed. Generated regions have lone surrogates replaced, and
`escape_markdown` NFC-normalises its input.
That is also how a user takes a note over. Steps 13 and 18 use the same region and
frontmatter functions.

**Entities** (step 13). People, papers, companies, tools, and topics are kinds `person`,
`paper`, `company`, `tool`, `topic`, never merged with each other. An entity is `(kind,
canonical key)`, where the key is `entity_key(name)` mapped through `aliases.yaml`. Its
display name is the alias entry's name, else the name of its earliest mention. Names with
an empty key (`???`) are not registered and stay unlinked. Mentions (one per episode,
analysis, kind, and key) are recorded in SQLite (`episodes`, `mentions`, `aliases`,
`entities`; derived and rebuildable from the artifacts, `aliases.yaml`, and the vault scan).

- Every entity line of an analysis section is a wikilink `[[<full path>|<name as the
  analysis wrote it>]]`.
- An entity note has owned keys `earmark: entity`, `entity_id`, `kind`, `name`, and the
  seeded `tags`. Its region holds a heading, the kind and episode count, the other names
  seen, and one line per episode whose note exists, newest first: date, episode link,
  show, context, timestamps.
- Entity notes are found by `entity_id`, so the user may rename or move them.
- A note whose entity no longer has mentions (merged away by an alias) becomes a stub,
  "Merged into …" or "No episode mentions this entity any more.", with its frontmatter
  kept. It is never deleted.
- Names become file names through `file_component` (120 bytes, fallback `Unnamed`), and
  a collision with any entry in the kind folder, a user note included, gets the id
  suffix.
- **`aliases.yaml`**: a mapping of kinds (`people`, `papers`, `companies`, `tools`,
  `topics`), each a mapping of display name to a name or a list of other names. A null
  value only pins the display name. Each name appears once per kind (first wins; no
  chains).
- It is parsed with `yaml.compose`, so every name is its raw text and nothing is
  constructed. YAML aliases, more than one document, over 256 KiB, non-UTF-8, and
  symlinks are whole-file errors: a store warns and keeps the aliases loaded before, and
  `rebuild` refuses. Bad entries are skipped with a warning naming the line.

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
| `GET /search?q=` | Vector search (step 14). `q` (1–500 characters after collapsing whitespace), `limit` (1–50, default 10), repeatable `kind` (`transcript`, `summary`, `claim`, `lead`, `question`, `answer`), `episode` (an episode key). 200 `{"hits": [SearchHit, …]}` ranked by score (`{"hits": []}` before the first index write). 422 invalid parameters (never echoed). 409 the index was built with another embedding model or dimension (run `earmark reindex`). 503 search unavailable in this process (extra missing, `INDEX_BACKEND=none`), model not downloaded yet, or index read failure (`Retry-After` for transient ones). |
| `GET /episodes` | Stored episodes, by first share time, newest first (step 17). `limit` (1–200, default 50), `offset` (0–1 000 000). 200 `{"episodes": [{key, show, title, published_at, shared_at, url, has_transcript, analyses: [{prompt, prompt_hash, model, created_at}, …] newest first}, …], "total", "offset", "limit", "next_offset"}`. Read from the artifacts on disk, never from SQLite. 422 invalid parameters. 503 the episodes directory cannot be read. |
| `POST /episodes/{key}/analyze` | Run a prompt on a stored episode (step 17). Body `{prompt?}` (`ReanalyzeRequest`, JSON, at most 64 KiB, else 413; `{}` is the default analysis). 202 `{job_id, status, created}` with `Location: /jobs/{id}`, as `/ingest`; `created` is `false` for an active identical job. 404 unknown or malformed key. 409 the episode has no transcript yet. 422 invalid body, naming the allowed field `prompt`. |

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
| `VAULT_DIR`, `VAULT_SUBDIR` | unset (required for the vault store), `Earmark` | Where notes go. `VAULT_DIR` is the Obsidian vault root and must exist (it may be a symlink); the subdir is a relative path without `..`, created if missing, and must not be a symlink |
| `OBSIDIAN_VAULT_NAME` | unset | Enables `obsidian://open` links to the note in done notifications (step 15). The vault's name as Obsidian shows it on the phone, normally the vault folder's name; at most 200 characters, no control characters |
| `RESOLVER_BACKEND` | `apple` | `apple`, `fake` |
| `FETCH_BACKEND` | `http` | `http`, `fake` |
| `ASR_BACKEND` | `faster_whisper` | `faster_whisper`, `remote`, `fake` |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai_compat`, `fake` |
| `STORE_BACKEND` | `vault` | `vault`, `fake` |
| `INDEX_BACKEND` | `lancedb` | `lancedb`, `fake`, `none` |
| `NOTIFY_BACKEND` | `ntfy` | `ntfy`, `fake`, `none`. `ntfy` without `NTFY_URL` sends nothing (the worker still starts) |
| `ASR_MODEL`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, `ASR_LANGUAGE` | `large-v3`, `auto`, `auto`, unset (auto-detect) | faster-whisper options (step 08); `remote` also uses `ASR_MODEL` (sent as the server's model id, e.g. `openai/whisper-large-v3`) and `ASR_LANGUAGE`. Model: a size such as `tiny`, `small`, `large-v3`, `distil-large-v3`, `large-v3-turbo`, a Hugging Face repo id, or a local directory. Device: `auto`, `cpu`, `cuda`. Compute type: `auto`, `default`, `int8`, `int8_float32`, `int8_float16`, `int8_bfloat16`, `int16`, `float16`, `bfloat16`, `float32` (device and compute type case-insensitive). Language: a lower-case code such as `en`; `auto` means detect |
| `ASR_MODEL_DIR` | unset (the Hugging Face cache, `$HF_HOME/hub` or `~/.cache/huggingface/hub`) | Where faster-whisper downloads models |
| `ASR_BASE_URL`, `ASR_API_KEY`, `ASR_MAX_UPLOAD_MB` | unset, unset, unset (no upload limit) | Remote ASR backend (step 09). Base URL: the server's OpenAI-compatible base, ending in `/v1` (http or https, a host, no credentials, query, or fragment; a trailing `/` is dropped). Key: secret, printable ASCII without spaces, sent as a bearer token. Upload limit in MiB |
| `ASR_TIMEOUT_SECONDS` | `10800` | How long the remote backend waits for the server's answer (read and write timeout) |
| `KEEP_AUDIO` | `false` | Keep audio after transcription |
| `LLM_MODEL` | `claude-opus-5-5` | Analysis model id; part of the analysis cache key (`prompt_hash`). For `openai_compat`, the server's model id (`claude-*` is refused) |
| `LLM_EFFORT` | `medium` | Claude effort: `low`, `medium`, `high`, `xhigh`, `max` (case-insensitive). Not part of the cache key |
| `LLM_FALLBACKS` | `true` | Claude server-side refusal fallback (`fallbacks: "default"`); set `false` for models without it (Opus 4.8 and older). Not part of the cache key |
| `LLM_API_KEY` | unset | Analysis API key (secret, printable ASCII without spaces): the Anthropic key, or the local endpoint's key, sent as a bearer token (step 11) |
| `LLM_CONTEXT_TOKENS` | unset (Anthropic: 1 000 000; **required** for `openai_compat`, at least 8192) | Context window for the fit check and the part budget of map-reduce. For a local server, the window it runs the model with (per request); see `docs/llm.md` |
| `LLM_BASE_URL` | unset | Local OpenAI-compatible base URL ending in `/v1`, validated like `ASR_BASE_URL` (`openai_compat` only, required there; the Anthropic client ignores it and honours `ANTHROPIC_BASE_URL`) |
| `LLM_TIMEOUT_SECONDS` | `3600` | How long `openai_compat` waits for one answer (read and write timeout) |
| `LLM_RESPONSE_FORMAT` | `json_schema` | `openai_compat` JSON request: `json_schema` (schema-constrained) or `json_object` (JSON mode, schema in the prompt). Not part of the cache key |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model: a name from fastembed's `TextEmbedding.list_supported_models()` (case-insensitive; checked when the index backend is built). Recorded in the index; changing it needs `earmark reindex`. For non-English shows, `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (step 14) |
| `EMBED_MODEL_DIR` | unset (`<DATA_DIR>/models/embed`) | Where fastembed downloads embedding models (step 14). fastembed's own default, the system temp directory, is not used |
| `NTFY_URL`, `NTFY_TOKEN` | unset | ntfy topic URL and optional access token (both secret; step 15). URL: http(s), a host, optional path prefix, ending in the topic (`[-_A-Za-z0-9]{1,64}`), such as `https://ntfy.sh/my-earmark-topic` or `http://ntfy.tailnet-name.ts.net/earmark`; no credentials, query, or fragment. On public ntfy.sh the topic name is the only protection unless the topic is reserved, so pick a long random one. Token: printable ASCII without spaces (`tk_…`), sent as a bearer token |
| `MAX_AUDIO_MB`, `MAX_FEED_MB`, `ALLOW_PRIVATE_FETCH` | `1024`, `20`, `false` | Fetch limits in MiB (× 1024 × 1024); `MAX_FEED_MB` caps the lookup, the episode page, and the feed. `ALLOW_PRIVATE_FETCH` turns off the address check only |
| `JOB_MAX_ATTEMPTS`, `JOB_LEASE_SECONDS`, `WORKER_POLL_SECONDS` | `4`, `900`, `2` | Queue behaviour |
| `TZ` | unset (system) | IANA zone for note dates and digests, validated at load. Note file names, `date`, `shared_at`, and analysis times use it (step 12) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive) |

Backend names are `Literal` types, so a misspelt name fails when settings load, naming
the variable. A name that is valid but whose backend is not built yet is rejected by
`pipeline/factory.py` (step 05).

The Anthropic credential (step 10): `EARMARK_LLM_API_KEY` (environment or `.env`) is
passed to the SDK explicitly. When it is unset, the SDK resolves its own credential
(`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` from the process environment, an `ant auth
login` profile, workload identity). pydantic-settings does not export `.env` entries, so an
`ANTHROPIC_API_KEY` line in `.env` is not seen; use `EARMARK_LLM_API_KEY` there. No
credential at all is a startup `ConfigError`.

## Deployment

Step 16; exact files in `docs/steps/16-deploy-shortcut.md` section 4, operator guide in
`docs/deploy.md`.

- **Image.** The `Dockerfile` has stages `uv`, `build`, and `runtime`.
  - Base images: `python:3.13.18-slim-trixie` and `ghcr.io/astral-sh/uv:0.13.9`, pinned
    by digest. The Python tag follows `.python-version`; the uv minor follows the
    `uv_build` bound.
  - Dependencies: `uv sync --locked --no-dev` plus the extras in the build argument
    `EXTRAS` (default `transcribe index`), with the project installed non-editable into
    `/app/.venv`.
  - User: `earmark`, with UID and GID from build arguments (default 1000, 0 refused).
    `/data` and `/vault` are owned by it.
  - Environment: `EARMARK_HOST=0.0.0.0`, `EARMARK_DATA_DIR=/data`,
    `EARMARK_VAULT_DIR=/vault`, `HF_HOME=/data/huggingface` (so Whisper models persist on
    the volume).
  - Entrypoint `earmark`, default command `serve`.
  - It installs `ca-certificates`, and has no `HEALTHCHECK`, no `VOLUME`, and no `.env`.
  - `.dockerignore` is an allow-list: `pyproject.toml`, `uv.lock`, `.python-version`,
    `README.md`, `src/`.
- **Compose.**
  - `api` (`serve`) and `worker` (`worker`) share the image, a named volume `data` at
    `/data`, and the vault bind mount `${VAULT_PATH}` at `/vault`.
  - Settings come from `env_file: .env`. `environment:` forces the in-container
    `DATA_DIR`, `VAULT_DIR`, `HOST`, `PORT`, and empty (so default) model directories.
  - Exposure: only `api` publishes a port, `${API_BIND:-127.0.0.1:8787}`.
  - Hardening: `init: true`, `no-new-privileges`, `cap_drop: ALL`.
  - The worker gets `stop_grace_period: 2m`; SIGTERM releases the running job.
  - The `gpu` profile adds `asr`: NVIDIA's arm64 CUDA 13 vLLM container (pinned by digest)
    serving `EARMARK_ASR_MODEL`, with `VLLM_API_KEY` from `EARMARK_ASR_API_KEY`, raised
    audio limits, a GPU reservation, and no published port. The worker uses it with
    `EARMARK_ASR_BACKEND=remote` and `EARMARK_ASR_BASE_URL=http://asr:8000/v1`.
  - Compose-only variables (`VAULT_PATH`, `API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
    `ASR_GPU_MEMORY_UTILIZATION`) are unprefixed, so `Settings` ignores them.
- **Reverse proxy.** It is not part of compose. `deploy/caddy/Caddyfile` is an example
  for Caddy on the host:
  - it binds only the private LAN address, on 443, with no port 80;
  - it gets a certificate through DNS-01 with `dns cloudflare {env.CF_API_TOKEN}` (a
    Caddy build with `caddy-dns/cloudflare`; the token needs Zone:DNS:Edit and
    Zone:Zone:Read, limited to the one zone);
  - it sets `resolvers 1.1.1.1`;
  - it forwards to `127.0.0.1:8787`.

  The token is never in earmark's `.env`, which compose exports into earmark's
  containers. Nothing binds the API to a non-loopback host address by default;
  `EARMARK_HOST=0.0.0.0` exists only inside the container.
- **Native.** `deploy/systemd/earmark-{api,worker}.service` run
  `/srv/earmark/.venv/bin/earmark serve|worker` as user `earmark`, with
  `WorkingDirectory=/srv/earmark` (where `.env` is read), `KillSignal=SIGTERM`,
  `ProtectSystem=strict`, and `ReadWritePaths` for the data directory (and the vault for
  the worker). `earmark-digest.service` (`Type=oneshot`, `earmark digest`, the worker's
  hardening and paths, no `[Install]`) is started by `earmark-digest.timer`
  (`OnCalendar=Mon *-*-* 07:00:00`, `Persistent=true`, `WantedBy=timers.target`; step 18).
- **Not verified.** No container has run in the build sandbox (no Docker). CI builds the
  image and runs only one-off commands in it. The `gpu` profile has never been built or
  run. A CUDA build of CTranslate2 in the image is documented in `docs/deploy.md` as an
  unverified outline, not provided.

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
    ids) are not logged or put in job errors.
- **Third-party logging** (step 20). Every log record goes through earmark's one handler
  (`log.configure_logging`), which applies three rules on top of secret redaction:
  - *Wire loggers are gated at the handler.* Records below WARNING from `httpx`,
    `httpcore`, `httpx2`, `httpcore2`, `urllib3`, `anthropic`, and `huggingface_hub` (and
    their children) are dropped by a handler filter, and the loggers' levels are also set
    to WARNING or above. These loggers print request URLs, redirect `Location` headers
    (relative paths a URL pattern cannot recognise), and, for `anthropic`, whole request
    bodies. The filter is what holds: libraries reset their own levels at import
    (`ANTHROPIC_LOG=debug` sets `anthropic` and `httpx2` to DEBUG when the SDK is
    imported, which earmark does lazily, after logging is configured; `HF_HUB_VERBOSITY`
    does the same for `huggingface_hub`). `LOG_LEVEL=DEBUG` does not show these records.
  - *Third-party text is scrubbed of URL paths.* For every record from a logger outside
    `earmark.*`, and for the traceback and stack text of every record, `log.scrub_urls`
    rewrites each `scheme://[userinfo@]host[:port]/path?query#fragment` to
    `scheme://host[:port]/…` and drops the query from an HTTP request line
    (`"GET /search?q=… HTTP/1.1"` becomes `"GET /search HTTP/1.1"`). So a
    `huggingface_hub` retry warning keeps its host and reason, uvicorn's access line keeps
    the method, route, and status but not the search query, and an unexpected library
    exception logged by the runner loses its URL path. earmark's own messages are left as
    written: they name hosts only, except the canonical Apple URL that ingest logs, which
    earmark builds from parsed ids.
  - *Libraries do not print around the handler.* `huggingface_hub` installs its own stderr
    `StreamHandler` on its logger at import and still propagates, so each of its warnings
    printed twice, once unformatted and unredacted. `log.adopt_library_loggers()` removes
    every non-`NullHandler` from the loggers in `log.LIBRARY_HANDLER_LOGGERS`
    (`huggingface_hub`). It runs at the end of `configure_logging` and right after each
    lazy import that can load `huggingface_hub` (`transcribe.faster_whisper.load_runtime`
    and `model_cache_status`, `index.embed.load_fastembed_runtime` and `model_cached`).
    Because `huggingface_hub` installs the handler only when `huggingface_hub.utils.logging`
    is first imported, which its lazy package can delay until the first download, the
    function imports that module first whenever `huggingface_hub` is already loaded (9 ms,
    no `httpx`). It never imports `huggingface_hub` itself.

  `runner.describe_error` applies `scrub_urls` to the text of non-earmark exceptions, so
  job errors and warnings follow the same rule.
- **XML.** Feeds are parsed only by `resolve/rss.py`, with `defusedxml.ElementTree`
  (entity declarations and external references refused, so no expansion bombs and no
  external entities; a DOCTYPE alone is allowed and never fetched). The stdlib parser
  is not used on feeds: with expat 2.9 it still expands internal entities. Apple's
  episode page is read with the stdlib `html.parser` and `json`, nothing executed.
- **Resolver requests.** The lookup and episode-page URLs are built from the parsed ids
  and country; the feed URL comes from Apple's lookup. All three go through `fetch.py`.
- **Operator-configured endpoints.** The remote ASR server (step 09) is reached by its
  own httpx2 client in `transcribe/remote.py`, not through `fetch.py`, and without the
  address guard: its URL comes only from `EARMARK_ASR_BASE_URL`, and it is expected to
  be on the LAN or tailnet, which the guard refuses. Routing it through `fetch.py` would
  force `ALLOW_PRIVATE_FETCH=true` and so disable the guard for every feed;
  `ALLOW_PRIVATE_FETCH` does not affect the ASR client. The client keeps the other
  rules: http(s) only and no credentials in the URL (checked at settings load), no
  redirects followed, no proxies from the environment, cookies cleared per request,
  responses capped (64 MiB), host-only messages, the API key only in the per-request
  `Authorization` header. The response is untrusted text, validated before use. The local
  LLM server (step 11, `EARMARK_LLM_BASE_URL`) is reached the same way by its own httpx2
  client in `analyze/llm/openai_compat.py`, with the same rules (responses capped at
  16 MiB). Both share `endpoint.error_detail` and `endpoint.host_label`. The ntfy server
  (step 15, `EARMARK_NTFY_URL`) is the third operator endpoint, reached by its own httpx2
  client in `notify/ntfy.py` under the same rules (responses capped at 64 KiB), because it
  may be public ntfy.sh or a self-hosted server on a private address. `earmark doctor
  --online` reaches the ASR, LLM, and ntfy servers the same way, read-only. It reaches the
  iTunes lookup through `fetch.py`, guard included.
- **Notifications** (step 15). Episode and show titles come from feeds, and `job.error` and
  warnings can quote them.
  - Every value is cleaned before it is sent (control, format, and separator characters
    removed, whitespace collapsed, RFC 2047 `=?` defused, byte-capped) and redacted.
  - Values travel only in the JSON body, never in a header, the URL, or a query string,
    so there is no header injection.
  - Notifications carry no transcript or analysis text, and no secret, topic, or URL path.
  - The `click` link is built only from the configured vault name and the note path, both
    percent-encoded.
- **Model downloads.** faster-whisper fetches models from Hugging Face with
  `huggingface_hub`, not through `fetch.py`: the repository is chosen by the operator
  (`ASR_MODEL`), not by a third party, and it honours the usual `HF_*` and proxy
  environment variables. Weights are CTranslate2 binaries plus JSON and text; nothing is
  unpickled. A cached model loads offline. The embedding model (step 14) is fetched the
  same way by fastembed (`huggingface_hub`, operator-chosen `EMBED_MODEL`, ONNX graph plus
  tokenizer JSON, `HF_*` variables honoured), only by the worker and `earmark reindex`;
  fastembed's loguru output is disabled so its messages cannot bypass the log redaction.
  - *Tokens.* earmark never asks for, reads, or passes a Hugging Face token; public models
    download anonymously. `huggingface_hub` itself uses `HF_TOKEN` or its token file if
    the operator provides one. The Hub answers anonymous requests with an `X-HF-Warning`
    header ("You are sending unauthenticated requests to the HF Hub…"), which
    `huggingface_hub` logs at WARNING once per process; it reaches the log once, through
    earmark's handler (step 20).
  - *hf_xet.* Xet-backed files (Whisper `model.bin`) are fetched by `hf_xet`, whose Rust
    logger writes JSON lines at INFO to its own files under `$HF_HOME/xet/logs/` (default
    `~/.cache/huggingface/xet/logs/`), outside earmark's handler, and which reports a
    download summary to the Hub (`telemetry accepted … xet_download_summary`). In a real
    download of `tiny` (step 20, 2026-11-21) those files held hosts, request ids, hashes,
    and local paths, but no URL path or query. earmark leaves both as the library ships
    them. The operator's knobs are `HF_XET_LOG_DEST`, which upstream's config maps to a
    file or directory (an empty value is ignored), and `HF_HUB_DISABLE_TELEMETRY` or
    `DO_NOT_TRACK`, which the binary reads. Neither knob has been tested here.
- **Search** (step 14). `GET /search` is behind the bearer middleware like every route but
  `/healthz`. The query only reaches the embedder: it is never logged (uvicorn's access
  line has its query string dropped, step 20), never put in a filter string, and never
  echoed in an error. LanceDB filter strings are built only from
  `kind` literals and episode keys matched against `[0-9a-f]{20}`. Query length (500
  characters) and result count (50) are capped. Hits carry untrusted transcript and model
  text as JSON strings; the CLI strips control characters before printing.
- **Vault writes.** Titles and names from feeds and from the LLM are sanitized before
  they become paths (`vault.sanitize.file_component`: no separators, no leading dot,
  NFC, byte-capped), and are always double-quoted and escaped in YAML. Containment is
  enforced by construction rather than by resolving paths. Every component below
  `VAULT_DIR` is opened relative to its parent's fd with `O_NOFOLLOW`. A symlink on the
  write path fails the job, and a symlink at a note's name is treated as an occupied
  name. Existing files are never opened for writing, only replaced by rename, so
  neither symlinks nor hard links can redirect a write. earmark writes only inside
  `<VAULT_DIR>/<VAULT_SUBDIR>/`: its own notes, its managed regions and owned
  frontmatter keys in them, and `Backups/`. It deletes nothing but its own stale temp
  files. Entity names from the LLM get the same treatment as titles (step 13), and wikilink
  labels are cleaned of `[ ] |`, backslashes, newlines, and `<`. `aliases.yaml` is user
  input: it is read through the same fd-based, symlink-refusing reader, capped at 256 KiB,
  and parsed into a PyYAML node tree without constructing objects or expanding aliases.
- **LLM.** The transcript, feed metadata, and the model's answer are untrusted text. The
  analysis call has no tools; the fixed system prompt tells the model that metadata and
  transcript are material, never instructions; untrusted sections have `<` and `>`
  escaped; the answer is validated against a schema before anything is written, and
  rendered with `<` escaped. The digest request (step 18) follows the same rules: a constant
  system prompt, escaped `<item>` sections, a fixed schema, no tools. Its answer can only
  name lead ids that code offered (others are ignored). Its titles and summaries are
  Markdown-escaped, and every link in the digest note is built by code from vault paths,
  never from model output. With `LLM_FALLBACKS=true` a refused request is re-run by
  Anthropic on another Claude model, recorded as an analysis warning. The Anthropic API is
  an operator endpoint reached by the SDK's own httpx2 client, not through `fetch.py`
  (base URL from the SDK default or `ANTHROPIC_BASE_URL`; the SDK honours proxy
  environment variables). The `anthropic` logger is a gated wire logger (see Third-party
  logging), because its DEBUG records contain the whole request body.
- **Secrets.** Tokens are `SecretStr`, redacted in `earmark config` and `earmark doctor`,
  and never logged. `EARMARK_NTFY_URL` is a `SecretStr` too (step 15), because on public
  ntfy.sh the topic is the password. Doctor prints only its host. `Settings.secret_values()`
  also returns the bare topic when it is at least 12 characters (step 16), so a job error or
  log line that quotes only the topic is redacted too. Shorter topics are left alone because
  masking a common word (`earmark`) would corrupt every log line; doctor redacts the topic
  at any length. The LLM key lives only in `AnthropicOptions.api_key` and the SDK
  client, and a test shows it absent from DEBUG logs even without the redacting
  formatter.

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
  `tests/test_remote_asr_live.py` is also `network`: it transcribes the `slow` tests' clip
  on a real ASR server and is skipped unless `ASR_TEST_BASE_URL` (with `ASR_TEST_MODEL`,
  optional `ASR_TEST_API_KEY`) is set, so CI skips it and the operator runs it on the
  homelab. `tests/test_openai_compat_live.py` (step 11) is `network` too: a one-shot and a
  map-reduce analysis against a real local LLM server, skipped unless `LLM_TEST_BASE_URL`
  (with `LLM_TEST_MODEL`, optional `LLM_TEST_CONTEXT_TOKENS`, `LLM_TEST_API_KEY`,
  `LLM_TEST_RESPONSE_FORMAT`) is set. `tests/test_ntfy_live.py` (step 15) is `network`:
  it publishes one notification and polls it back, to `NTFY_TEST_URL` (optional
  `NTFY_TEST_TOKEN`) or else to a fresh random topic on public ntfy.sh.
- `slow`: downloads the `tiny` Whisper model and a public-domain clip (JFK's 1961
  inaugural address, 11 s, pinned by commit and sha256 in `tests/slow_assets.py`) into
  `$SLOW_TEST_CACHE` (default `~/.cache/earmark-tests`), then transcribes it on CPU.
  Needs the `transcribe` extra and fails (does not skip) without it.
- `llm`: calls the real analysis model (`tests/test_analyze_live.py`: a short synthetic
  transcript, effort `low`, about two requests; `tests/test_digest_live.py`, step 18: eight
  fixed leads grouped into themes, about two requests). Needs `ANTHROPIC_API_KEY` or an `ant
  auth login` profile and fails (does not skip) without one. For every test without this
  marker, `tests/conftest.py` deletes all `ANTHROPIC_*` variables and points
  `ANTHROPIC_CONFIG_DIR` at an empty directory, so a developer's key, profile, or base URL
  never reaches the default suite.
- `gpu`: needs CUDA.
- `index` (step 14): needs the `index` extra and runs a real LanceDB in a temp directory
  with `FakeEmbedder`. Sockets stay blocked (the marker does not re-enable them), so these
  tests also prove the index makes no network call. Fails (does not skip) without the
  extra. `tests/test_index_slow.py` is `slow` instead: it downloads `BAAI/bge-small-en-v1.5`
  into `$SLOW_TEST_CACHE/fastembed` and checks semantic ranking with the real model.
  `tests/test_log_libraries.py` (step 20) is `index` too: it drives `huggingface_hub`
  (through `httpx.MockTransport`), `httpx`/`httpcore` and `urllib3` (against
  `tests/uds_http.py`, a unix-socket HTTP server), fastembed, and LanceDB with sockets
  blocked, and checks that no URL path or query reaches earmark's log output. The
  default suite does the same for `httpx2`/`httpcore2`, the anthropic SDK, and uvicorn.

For every test without `network`, `slow`, or `llm`, `tests/conftest.py` also sets
`HF_HUB_OFFLINE=1` and points `FASTEMBED_CACHE_PATH` at an empty directory, so no default
test can download a model even if the extra is installed.

The analysis client tests drive the real SDK through `httpx2.MockTransport`
(`tests/anthropic_stub.py` builds the SSE streams and error bodies); the analyzer tests use
a scripted `LLMClient` (`tests/llm_fakes.py`). The remote ASR tests use `httpx2.MockTransport`, a recording transport that iterates the
request body (to prove streaming), and `tests/asr_stub.py`, a threaded stdlib HTTP server
on a unix socket reached with `httpx2.HTTPTransport(uds=...)`, which the socket block
allows. The local LLM client (step 11) is tested the same way with `tests/llm_stub.py`, an
OpenAI-style chat server on a unix socket, and map-reduce with synthetic multi-hour
transcripts (`tests/long_transcripts.py`) and the scripted `LLMClient`.

The ntfy client (step 15) is tested with `httpx2.MockTransport` and `tests/ntfy_stub.py`,
a unix-socket ntfy server with recorded ntfy.sh bodies. Doctor is tested through
`doctor.Probes`: injected transports, an injected `GuardedFetcher`, and an SDK
`http_client`. Missing extras are simulated with `sys.modules` stubs. A real LanceDB
doctor test carries the `index` marker.

Documents are tested against the code (step 16).

- **Markers.** `tests/doc_helpers.py` extracts content by HTML-comment markers
  (`<!-- name:begin -->` … `<!-- name:end -->`, or a single `<!-- name -->` before a
  fence) and plain regular expressions; there is no Markdown library.
- **What `tests/test_docs.py` checks.**
  - The README's settings reference lists every `Settings` field exactly once, one
    variable per table row.
  - The README quickstart sets every backend to `fake`.
  - `docs/shortcut.md`'s example request body validates against `IngestRequest` and is
    accepted by `POST /ingest`, and the example response matches the route.
  - The Shortcut's actions appear in a fixed order.
  - Relative links resolve.
  - The README's command table names every CLI command, and its HTTP API sentence names
    every route of `build_router` (step 17).
  - Real share-sheet captures in `tests/fixtures/share_sheet.json` canonicalise as
    recorded.
- **What `tests/test_deploy.py` checks**, statically: the Dockerfile, `.dockerignore`,
  `compose.yaml`, and the systemd units.
- **The gate.** `ruff format --check .` also formats Python code blocks in Markdown, so
  those must be ruff-clean.

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
`slow`, `llm`, `gpu`, `index`), does not install the `transcribe` or `index` extras, never
runs on a GPU, and does not build or run the container. Separate workflows:
`.github/workflows/index.yml` ("Index tests", step 14) runs on the same pushes and pull
requests as `ci.yml`, with the same pinned checkout and setup-uv (plus `cache-suffix:
index`), `contents: read`, and no secrets, in one job `index` that runs `uv sync --locked
--extra index` and `uv run --locked --extra index pytest -m index -v -rs`. It is not a
required check unless the operator adds `pytest -m index (linux/arm64)` to `main`'s
protection.
`.github/workflows/network.yml` ("Live tests") runs every Monday at 05:23 UTC and on
`workflow_dispatch`, with `contents: read` and no secrets, in two jobs: `network` (step
07) runs `uv run --locked pytest -m network -v -rs` with the same pinned actions and setup
as `ci.yml`; `slow` (step 08; both extras since step 14) runs `uv sync --locked --extra
transcribe --extra index` and `uv run --locked --extra transcribe --extra index pytest -m
slow -v -rs`, with setup-uv's `cache-suffix: slow` and a pinned `actions/cache` step for
`~/.cache/earmark-tests` (the Whisper and embedding models and the clip). GitHub runs
`schedule` and
`workflow_dispatch` only for workflow files on the default branch, so it starts once the
file is on `main`. `.github/workflows/llm.yml` ("LLM test", step 10) is triggered only by
`workflow_dispatch` (never by pushes, schedules, or pull requests; the job also checks
`github.event_name`), has `contents: read`, runs in the GitHub environment `llm`, and
passes that environment's `ANTHROPIC_API_KEY` secret only to its last step, `uv run
--locked pytest -m llm -v -rs`, after the same pinned checkout, setup-uv, and `uv sync
--locked` as `ci.yml`. The operator creates the environment and secret, and should restrict
its deployment branches to `main` and `impl`. `.github/workflows/container.yml`
("Container image", step 16) runs on the same pushes and pull requests as `ci.yml`, with
`contents: read`, no secrets, and the same pinned checkout as its only action. Its job
`docker build (linux/arm64)`:

- validates `compose.yaml` with `docker compose config` (with and without `--profile gpu`)
  against a `.env` copied from `.env.example`;
- builds the image with plain `docker build` (no push, no login, no Docker actions);
- runs `earmark --version`, an import of the extras, and an `id -u` check in the image.

It never starts the API or the worker. None of those is a required check: their failures
usually mean an outside service changed, or, for the container, that the image needs a
fix.

Rules for every workflow under `.github/workflows/`, enforced by `tests/test_ci.py`: it
passes `actionlint` (installed by the `actionlint-py` dev dependency and run offline by
the test suite), every `uses:` of a remote action is pinned to a full 40-character commit
SHA followed by a `# vX.Y.Z` comment, it declares a top-level `permissions` block, and it
never uses `pull_request_target`. `ci.yml` additionally gets only `contents: read` and
references no secrets. Dependabot (`.github/dependabot.yml`) proposes weekly updates for
the `uv` and `github-actions` ecosystems, and (step 16) `docker` for the Dockerfile's base
images, limited to patch and digest updates because Python 3.14 and uv 0.14 need project
changes. The vLLM tag in `compose.yaml` is bumped by hand. Dependabot reads its config from the default branch
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
   `get_supported_compute_types('cpu') == {float32, int8, int8_float32}`. Step 08
   (2026-11-20) confirmed it: `get_cuda_device_count() == 0` and `device="cuda"` raises
   "not compiled with CUDA support". Upstream ships CUDA 12 wheels for x86_64 only and
   leaves CUDA 13 (which the GB10, sm_121, needs) to source builds (OpenNMT/CTranslate2
   #1933, open); field reports show source builds working on GB10. So on the Spark this
   backend runs on CPU unless the image builds CTranslate2 with CUDA (step 16), and step 09
   is the supported GPU path. `large-v3` on CPU is slow for long episodes; a smaller
   `ASR_MODEL` is the stopgap. See `docs/steps/08-transcribe.md` section 2.1. Step 09
   (2026-11-20) surveyed servers: vLLM (NVIDIA's arm64 CUDA 13 container) serves Whisper
   with `verbose_json` segments and absolute timestamps across its 30 s chunks, but its
   default upload and decode limits (25 MB, 600 s, 256 MiB PCM) must be raised for
   podcasts; whisper.cpp's server has segments but needs a source build for sm_121 and
   has no auth; LocalAI ships CUDA 13 arm64 builds of whisper.cpp and Parakeet
   (`parakeet-cpp`); speaches' arm64 CUDA image is CPU-only (speaches#620); NVIDIA's
   Parakeet NIM HTTP API returns no timestamps and takes no MP3. None was run in the
   sandbox. See `docs/asr.md` and `docs/steps/09-remote-asr.md` section 2.2.

   Step 16 (2026-11-20) made the vLLM path the compose `gpu` profile. It pins
   `nvcr.io/nvidia/vllm:26.10-py3` (vLLM 0.31.0, CUDA 13.4, arm64 manifest present; its
   `PATH` includes an ffmpeg build). The image itself stays CPU-only. CTranslate2 4.9.0
   documents an aarch64 CUDA source build (`-DWITH_CUDA=ON -DWITH_CUDNN=ON -DWITH_MKL=OFF
   -DOPENMP_RUNTIME=COMP`, architecture through `CMAKE_CUDA_ARCHITECTURES`), and
   `docs/deploy.md` outlines it for sm_121 as unverified. Upstream's CUDA 13 issue #1933 is
   still open. None of this was run.
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
   `requires-python` carries an upper bound. See `docs/steps/01-scaffold.md`. Step 14
   (2026-11-20) used them: lancedb 0.41.0 and fastembed 0.9.1 (onnxruntime 1.32.0,
   pyarrow 26.0.1) install from wheels, and create, merge, search, optimize, and drop a
   table and embed with `BAAI/bge-small-en-v1.5` here, with sockets blocked once the model
   is cached. The extra adds about 440 MB installed (lancedb 160 MB, pyarrow 151 MB,
   onnxruntime 59 MB, numpy 54 MB), plus 65 MB for the model. See
   `docs/steps/14-vector-index.md` section 2.
4. **Container and GPU paths cannot be exercised in the dev sandbox.** They are reviewed
   statically (`tests/test_deploy.py`) and verified on the homelab. Since step 16, CI
   builds the image on linux/arm64 and runs one-off commands in it (`container.yml`), but
   no service has ever run in a container, and the `gpu` profile has never been built or
   run. The reachability setup has not been run either: WireGuard, the DNS-only record,
   resolvers' DNS rebinding protection, and Caddy's DNS-01 issuance. Only the example
   Caddyfile was checked, with `caddy validate`. What the iOS share sheet hands a Shortcut
   is also unverified (no iPhone);
   `docs/shortcut.md` says how to capture it into `tests/fixtures/share_sheet.json`.
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
7. **PyAV and faster-whisper.** faster-whisper 1.2.1 passes `metadata_errors` to
   `av.open`, which PyAV 19.0.0 removed, so every transcription fails with a `TypeError`.
   The `transcribe` extra pins `av>=18.1,<19` until a faster-whisper release includes the
   fix already on its `master`. The weekly `slow` job would catch a regression.
8. **Local LLM context and token counts** (step 11, 2026-11-20). OpenAI-compatible servers
   have no common token-counting endpoint. earmark estimates conservatively
   (`ceil(UTF-8 bytes / 3) + 4 per line`, plus the schema), which over-counts modern
   large-vocabulary tokenizers but can under-count old small-vocabulary ones (Llama 2,
   byte fallback for CJK). vLLM and llama.cpp reject an overflow with a 400, which earmark
   reports. **Ollama's OpenAI API truncates an over-long prompt silently** (`truncate` and
   `shift` default to true and cannot be set through `/v1`), so `OLLAMA_CONTEXT_LENGTH`
   (or `num_ctx`) must be at least `EARMARK_LLM_CONTEXT_TOKENS`. earmark cannot detect it.
   None of the four servers was run in the sandbox. See `docs/llm.md` and
   `docs/steps/11-local-llm-map-reduce.md` section 2.
9. **Duplicate basenames in Obsidian** (step 13, unverified). earmark's links are full paths,
   but once `Earmark/Entities/People/Ada Lovelace.md` exists, a user's own short link
   `[[Ada Lovelace]]` elsewhere in the vault may become ambiguous. Obsidian's help pages do
   not document which file it then picks. earmark does not scan outside its subdir.
10. **fastembed pins huggingface-hub below 2.0** (step 14, 2026-11-20). The universal lock
   therefore gives the `transcribe` extra huggingface-hub 1.33.0 instead of 2.4.0.
   faster-whisper 1.2.1 imports with it and uses only `snapshot_download`; a full
   transcription with 1.33 is exercised by the weekly `slow` job, not in the sandbox. When
   fastembed allows hub 2.x, the lock moves back on its own. Also from step 14: with
   lancedb imported, `os.fork` warns of a possible deadlock (earmark never forks), and
   LanceDB keeps old table versions until `optimize` cleans them up (one-hour window).
