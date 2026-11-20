# Implementation plan

Nineteen steps, each small enough for one implementer and checkable in the dev sandbox.
Read `docs/ARCHITECTURE.md` first; this file says what to build in what order, that file
says how the pieces fit.

## Status

Update this table when a step passes verification. Each step is one commit titled
`step NN: <title>`, so `git log --grep '^step'` gives the history.

| Step | Title | Depends on | Status |
|---|---|---|---|
| 01 | Scaffold, tooling, config | — | done |
| 01a | CI: the gate on every push | 01 | done |
| 02 | Core contracts and fakes | 01 | done |
| 03 | Job store | 02 | done |
| 04 | Ingest API | 03 | done |
| 05 | Pipeline runner and worker | 04 | done |
| 06 | Guarded fetch and audio download | 05 | done |
| 07 | Resolver: Apple → iTunes → RSS | 06 | done |
| 08 | Transcription: faster-whisper | 05 | done |
| 09 | Transcription: remote ASR backend | 08 | done |
| 10 | Analysis I: schema, prompts, Claude, one-shot | 05 | done |
| 11 | Analysis II: local LLM backend, map-reduce | 10 | done |
| 12 | Vault writer | 05 | done |
| 13 | Entity registry and linking | 10, 12 | done |
| 14 | Vector index and search | 05 | done |
| 15 | Notifications and doctor | 05 | done |
| 16 | Deployment and Shortcut docs | 06–15 | done |
| 17 | Extension: re-run a prompt | 10, 12 | done |
| 18 | Extension: weekly digest | 10, 12 | todo |

Steps 01–05 are strictly sequential and end with a walking skeleton: the whole pipeline
running end to end on fakes. After that, each step replaces one fake with a real
backend. The chains 06→07, 08→09, 10→11, 12→13, and the single steps 14 and 15 are
independent of each other and can run in any order, or in parallel in separate worktrees.

Step 01a was added after step 01 was built, and is numbered that way so the other step
numbers, which the architecture and step plans refer to, stay stable. It blocks nothing,
but run it before step 02 so every later step lands on a branch with CI. Later steps
that introduce opt-in tests (07, 08, 10) or the container (16) extend CI themselves.

## The build loop

Each step runs three agents with separate contexts. `/step NN` runs this for one step.

1. **Plan** (`step-planner`, Opus). Reads `CLAUDE.md`, `docs/ARCHITECTURE.md`, the step
   brief below, and the current code. Researches anything the brief marks as unverified
   against live documentation or live endpoints. Writes `docs/steps/NN-<slug>.md`: files
   to create or change, public signatures, each acceptance criterion mapped to the tests
   that prove it, edge cases, and any deviation from the brief or the architecture with
   its reason. If a contract changes, the planner edits `docs/ARCHITECTURE.md` too. It
   writes no source code.
2. **Implement** (`step-implementer`, Sonnet). Follows the step plan, writes code and
   tests, and runs `make check` until it passes. It does not commit. If the plan turns
   out to be wrong or impossible, it stops and reports instead of improvising.
3. **Verify** (`step-verifier`, Sonnet). Gets the step brief, the step plan, and the
   working tree, but not the implementer's summary. Runs `make check` itself, checks
   every acceptance criterion with evidence, reads the tests for ones that cannot fail,
   runs the live smoke where one applies, and looks for scope creep and loosened lint,
   type, or test settings. Ends with `VERDICT: PASS` or `VERDICT: FAIL` and numbered
   findings. It changes no tracked files.
4. **On FAIL**, the findings go back to the implementer, then to a fresh verifier. After
   two failed rounds, stop and bring the findings to the user; the plan is probably
   wrong and needs replanning.
5. **On PASS**, the orchestrator updates the status table and commits the step as one
   commit on the `impl` branch (`step NN: <title>`), pushes it, and waits for CI. On
   green it moves on to the next `todo` step whose dependencies are done. A red CI run,
   a product decision from the planner, or two failed verify rounds stops the loop.

The orchestrator relays the verifier's non-blocking notes and anything unverifiable in
the sandbox (GPU, Docker, live credentials) to the user after each step.

## Conventions for every step

- `make check` passes, with no loosening of lint, type, or test configuration.
- Every acceptance criterion has at least one test that would fail if the behaviour
  were removed.
- Default tests use no network, GPU, or credentials. Live checks sit behind a marker.
- New backends are registered in `pipeline/factory.py` and selected by config. An
  unknown or not-yet-built backend name is a clear configuration error at startup.
- New settings are added to `Settings` and `.env.example` together.
- Stay inside the step. Work that belongs to a later step is left for it.

---

## Step 01: Scaffold, tooling, config

**Goal.** A clean clone can `uv sync && make check` and get green.

**Build.**
- `pyproject.toml` with a src layout, the `earmark` console script, runtime dependencies
  (fastapi, uvicorn, pydantic, pydantic-settings, httpx2, typer), a dev group (pytest,
  pytest-socket, ruff, mypy), and empty `transcribe` and `index` extras to be filled later.
- `Makefile` with `check`, `test`, `lint`, `fmt`, `typecheck`.
- `earmark.config.Settings` with every variable in the architecture's configuration
  table, secrets as `SecretStr`.
- Logging setup, `.env.example`, extended `.gitignore`.
- CLI skeleton: `earmark --version` and `earmark config` (effective config, secrets redacted).

**Acceptance.**
- From a clean clone, `uv sync && make check` passes.
- `uv run earmark --version` prints the version.
- `uv run earmark config` never prints a secret value, proven by a test that sets a
  token and searches the output for it.
- A test shows that opening a socket inside the default suite raises.
- The markers `network`, `slow`, `llm`, and `gpu` are registered and deselected by default.
- mypy runs in strict mode over `src` and `tests`.
- Every `Settings` field appears in `.env.example`, enforced by a test.

**Research.** Confirm which Python version to pin given wheel availability for the later
heavy dependencies (architecture risk 3). Prefer 3.13; fall back to 3.12.

## Step 01a: CI: the gate on every push

**Goal.** Every push and pull request runs `make check` on the architecture the service
deploys to, so the gate the build loop uses locally is also enforced on GitHub.

**Build.**
- `.github/workflows/ci.yml`: runs on pushes to `main` and `impl` and on pull requests.
  One job on an arm64 Linux runner: check out, install uv with caching,
  `uv sync --locked`, `make check`.
- `.github/dependabot.yml`: weekly updates for the `uv` and `github-actions` ecosystems.

**Acceptance.**
- The workflow's install and test commands are exactly `uv sync --locked` and
  `make check`, enforced by a test that reads the workflow file, so CI and the local
  gate cannot drift apart.
- The job runs on an arm64 runner, and the Python version comes from `.python-version`,
  not from the workflow.
- The workflow declares `permissions: contents: read`, references no secrets, and uses
  `pull_request`, never `pull_request_target`.
- Every third-party action is pinned to a full commit SHA, with its version in a comment.
- A newer push to the same branch or pull request cancels the run in progress, and the
  job has a timeout.
- The workflow file passes a workflow linter or schema check, run inside `make check` or
  by a test.
- `docs/ARCHITECTURE.md`'s testing section says what CI runs and what it does not.

**Research.** Current release and commit SHA of `actions/checkout` and
`astral-sh/setup-uv`; the current arm64 runner label and whether it is available to
public repositories at no cost; whether `setup-uv` picks up `.python-version` on its
own; how to lint a workflow in this sandbox (for example a PyPI-packaged `actionlint`).

**Cannot be verified in the sandbox.** GitHub Actions does not run here. The verifier
checks the files statically and lists the live run as unverified. The real test is the
first push of `impl`, which the user does; if that run fails, fixing it is a follow-up
to this step.

## Step 02: Core contracts and fakes

**Goal.** The shared vocabulary every later step builds against.

**Build.** `models.py`, `errors.py`, `artifacts.py`, `pipeline/stages.py`, `fakes.py`, as
described in the architecture.

**Acceptance.**
- Every model round-trips through JSON unchanged.
- `episode_key` is deterministic, checked against a golden value in a test.
- `Transcript.to_text()` renders `[HH:MM:SS] text` lines, including for episodes over
  ten hours and for empty segments.
- `ArtifactStore` writes atomically (a failed write leaves no partial file at the final
  path) and refuses keys that are not plain hex.
- Each fake satisfies its Protocol under mypy, proven by typed assignments in a test module.
- Fakes are deterministic and count their calls, so tests can assert that a stage was skipped.
- `prompt_hash` changes when the prompt, model, or schema version changes, and is stable otherwise.

## Step 03: Job store

**Goal.** A durable queue in SQLite that survives crashes and concurrent workers.

**Build.** `db.py` (connection, WAL, migrations by `PRAGMA user_version`) and `jobs.py`
with `enqueue`, `claim_next`, `heartbeat`, `set_stage`, `complete`, `fail`, `skip`,
`add_warning`, `requeue_stale`, `get`, `list_recent`. The clock is injected.

**Acceptance.**
- With several threads and several processes claiming at once, each job is claimed
  exactly once.
- A transient failure requeues with increasing `run_after` and becomes `failed` at
  `JOB_MAX_ATTEMPTS`. A permanent failure is `failed` immediately.
- A `running` job whose heartbeat is older than the lease is requeued; one with a fresh
  heartbeat is not.
- Enqueueing the same `(url, prompt)` while the first is `queued` or `running` returns
  the same id. After the first reaches a terminal state, a new job is created.
- Reopening an existing database runs no migration twice and loses no rows.
- Jobs come out oldest first, and a job with a future `run_after` is not claimed.

## Step 04: Ingest API

**Goal.** The endpoint the Shortcut calls.

**Build.** `api/` with an app factory, bearer auth, `POST /ingest`, `GET /jobs/{id}`,
`GET /jobs`, `GET /healthz`; `resolve/apple.py` with `parse_apple_url(url) -> EpisodeRef`;
`earmark serve`.

**Acceptance.**
- A missing or wrong token returns 401 with `WWW-Authenticate`. The comparison is
  constant-time.
- A valid request returns 202 with a `job_id`, and that job is `queued` in the store.
- The handler performs no outbound I/O, shown by the suite's socket block.
- A non-Apple host, a show link without `?i=`, and a malformed URL each return 422 with
  a short human-readable message.
- `parse_apple_url` handles URLs with and without a slug, with a country code, with
  extra query parameters, and the legacy `itunes.apple.com` host.
- A blank or whitespace-only prompt is stored as null. Over-long prompts and oversized
  bodies are rejected.
- `/healthz` returns 200 without auth. Interactive docs are off by default.
- `earmark serve` refuses to start with an empty token and says why.

## Step 05: Pipeline runner and worker

**Goal.** The walking skeleton. The whole flow runs end to end on fakes.

**Build.** `pipeline/runner.py`, `pipeline/factory.py`, `pipeline/worker.py`;
`earmark worker [--once]`, `earmark submit <url> [--prompt]`, `earmark jobs`.

**Acceptance.**
- End-to-end test on fakes: `POST /ingest`, then `worker.run_once()`, leaves the job
  `done` with `episode.json`, `transcript.json`, `transcript.txt`, and an analysis file
  on disk, and the store called with the expected inputs.
- A second job for the same episode and prompt calls neither the transcriber nor the
  analyzer. The same episode with a new prompt calls only the analyzer.
- `SkipEpisode` ends the job as `skipped` with its reason and no retry.
  `TransientError` requeues. `PermanentError` fails. An unexpected exception is treated
  as transient with its traceback recorded.
- Indexer and notifier failures leave the job `done` with a warning. The notifier runs
  for `done`, `failed`, and `skipped`.
- The worker heartbeats during a long stage, and on SIGTERM it either finishes the
  current job or releases it back to `queued`.
- An unknown backend name fails at startup with a message naming the setting.
- Live smoke, run by the verifier: with every backend `fake`, start `earmark serve`,
  `curl` an ingest, run `earmark worker --once`, and see the job `done` via `GET /jobs/{id}`.

## Step 06: Guarded fetch and audio download

**Goal.** One safe way to fetch third-party URLs, and the audio stage built on it.

**Build.** `fetch.py` (guarded GET for small documents, streaming download for audio)
and the `http` `AudioFetcher` backend.

**Acceptance.**
- Non-http(s) schemes are refused.
- A host resolving to a loopback, private, link-local, or metadata address is refused,
  including when reached through a redirect. DNS resolution is injectable for tests.
  `ALLOW_PRIVATE_FETCH` lifts this.
- Redirects are followed up to a cap that tolerates real tracker chains (at least 10).
- Downloads stream to a `.part` file and are renamed on completion. An interrupted
  download leaves nothing at the final path.
- The size cap is enforced against `Content-Length` and against bytes actually read.
- Timeouts, 5xx, 408, and 429 raise `TransientError`. Other 4xx raise `PermanentError`.
- If the audio file already exists, no request is made.
- Requests send a generic client `User-Agent` with no personal details.
- The file extension is taken from the URL path or `Content-Type`.

**Research.** Decide whether to pin the resolved address for the connection to close the
DNS-rebinding gap, or to document it as a known limitation.

## Step 07: Resolver: Apple → iTunes → RSS

**Goal.** Turn a shared Apple Podcasts URL into an `Episode` with a downloadable enclosure.

**Build.** `resolve/itunes.py`, `resolve/rss.py`, `resolve/resolver.py`, registered as
the `apple` resolver; `earmark resolve <url>`; recorded fixtures.

**Acceptance.**
- With recorded fixtures, an episode present in the lookup result resolves to an
  `Episode` with guid, title, show title, publish date, duration, and audio URL.
- The audio URL comes from the RSS enclosure, with the lookup's `episodeUrl` as the
  fallback when the feed no longer lists the item.
- Feed matching tries GUID, then normalized title, then publish date within a day, with
  a test for each.
- A show with no `feedUrl`, or a matched item with no enclosure, raises `SkipEpisode`
  with a reason naming subscriber-only or exclusive content.
- An episode outside the lookup window follows the fallback the planner chooses after
  live research, or raises `PermanentError` with a clear message.
- 5xx and timeouts raise `TransientError`. An unknown show raises `PermanentError`.
- The URL's country code is passed to the lookup.
- Malformed XML is handled, and the parser is safe against entity attacks.
- The feed is fetched through `fetch.py`.
- An opt-in `network` test resolves a real, recent episode.
- A scheduled (weekly) and manually triggerable workflow runs `pytest -m network`. It is
  not a required check, since its failures mean an outside service changed.

**Research.** Architecture risk 2. Probe the live API for the maximum `limit`, whether
`lookup?id=<episode id>` works directly, and what Apple URLs the share sheet produces
today.

## Step 08: Transcription: faster-whisper

**Goal.** Local transcription with timestamps, cached per episode.

**Build.** `transcribe/faster_whisper.py` behind the `transcribe` extra;
`earmark transcribe <audio-file>`.

**Acceptance.**
- `import earmark` and the default suite work without the extra installed. Selecting
  the backend without it gives an error naming the extra.
- The model loads lazily, once per worker process.
- Model, device, compute type, and language come from settings.
- Segments map to `Transcript` with monotonic timestamps. `transcript.json` and
  `transcript.txt` are written atomically.
- An existing transcript is returned without loading the model.
- Progress is logged during long transcriptions so the worker's heartbeat stays fresh.
- Audio is deleted after success unless `KEEP_AUDIO` is set.
- Unit tests inject a fake model. An opt-in `slow` test transcribes a short clip with
  the `tiny` model on CPU and gets non-empty text.
- The scheduled workflow from step 07 (created here if step 07 has not run) also runs
  `pytest -m slow` with the `transcribe` extra installed and the model download cached.

**Research.** Architecture risk 1: check ctranslate2 wheel availability for aarch64 and
the pinned Python, and whether CUDA works on the DGX Spark. Record the finding in
`docs/steps/08-*.md` even if the answer is "use step 09 there". Choose a short test clip
with a clear licence, or fetch one inside the `slow` test.

## Step 09: Transcription: remote ASR backend

**Goal.** Let the worker use a GPU ASR server elsewhere on the homelab.

**Build.** `transcribe/remote.py` for an OpenAI-compatible `/v1/audio/transcriptions`
endpoint; `docs/asr.md` on running a compatible server.

**Acceptance.**
- Against a mock server, audio is uploaded as streamed multipart, and a response with
  segments maps to a `Transcript` with timestamps.
- A response with text but no segments yields a single-segment transcript and a job warning.
- Audio larger than `ASR_MAX_UPLOAD_MB` raises `PermanentError` naming the setting.
- The API key header is sent only when configured. The timeout suits hour-long audio.
- Connection errors and 5xx are transient. 4xx is permanent.
- Caching and file outputs match step 08.

**Research.** Which self-hosted servers currently expose this endpoint with segment
timestamps, including options for Parakeet on NVIDIA hardware.

## Step 10: Analysis I: schema, prompts, Claude, one-shot

**Goal.** A transcript and a prompt become a validated `Analysis`.

**Build.** `analyze/schema.py`, `prompts.py`, `render.py`, `analyzer.py`, `llm/base.py`
(the `LLMClient` protocol), `llm/anthropic.py`; `earmark analyze <episode_key> [--prompt]`.

**Acceptance.**
- The standing system prompt is a constant with nothing interpolated into it.
- The request carries episode metadata and the transcript with `[HH:MM:SS]` markers.
  The user's prompt sits in its own delimited section. A missing prompt uses the default,
  which covers summary, key claims, people, papers, companies and tools, topics, open
  questions, and research leads with timestamps.
- Output is validated against the schema. Timestamps beyond the episode's duration are
  dropped with a warning.
- The result is cached at `analyses/<prompt_hash>.json`, and a cache hit makes no LLM call.
- `render.py` produces the Markdown body for an analysis, deterministically.
- A refusal raises `PermanentError` with its category. Hitting the output cap retries
  once with a larger cap. Rate limits, 5xx, and connection errors are transient. 400,
  401, and 404 are permanent.
- A transcript too large for one request raises a distinct `NeedsChunking` error and is
  never truncated.
- Tests cover the analyzer with a fake `LLMClient` and the Anthropic client's request
  construction with a stubbed SDK client. An opt-in `llm` test runs a short fixture
  transcript against the real model.
- A workflow that runs `pytest -m llm` exists and is triggered only by hand
  (`workflow_dispatch`). It reads the API key from a GitHub environment secret and can
  never run for a pull request.

**Research.** Load the `claude-api` skill and read its Python README before planning.
Use the official `anthropic` SDK, `claude-opus-5-5` unless configured otherwise, adaptive
thinking with effort set explicitly, structured output, streaming for long inputs, and
the refusal handling the skill describes. Take every request shape from the skill.

## Step 11: Analysis II: local LLM backend, map-reduce

**Goal.** Local-only analysis, and episodes longer than the model's context.

**Build.** `llm/openai_compat.py`, `analyze/chunking.py`, and strategy selection in the
analyzer: one request if the transcript fits `LLM_CONTEXT_TOKENS`, otherwise map-reduce.

**Acceptance.**
- Chunks break only at segment boundaries, cover every segment, and keep absolute timestamps.
- With a fake LLM and a small context budget, a three-hour synthetic transcript makes
  several map calls and one reduce call, and the result validates.
- Entities repeated across chunks are merged in the reduce step.
- Invalid JSON from the local model gets one repair attempt, then `PermanentError`.
- The one-shot path from step 10 behaves as before.
- The local client is tested against a mock HTTP server, covering both schema-constrained
  responses and plain JSON mode.

## Step 12: Vault writer

**Goal.** The note the user actually reads.

**Build.** `vault/notes.py`, `vault/sanitize.py`, registered as the `vault` store.

**Acceptance.**
- Notes land at the paths in the architecture. Filenames are sanitized, length-capped,
  keep Unicode, and collisions get a short key suffix.
- Frontmatter parses as YAML and carries the documented fields, including for titles
  containing colons, quotes, `---`, and newlines.
- The body has one section per analysis, newest first, each showing its prompt, with
  timestamps as `[HH:MM:SS]`.
- The transcript is a separate note, linked from the episode note.
- Text the user adds outside the managed markers survives regeneration.
- Storing the same inputs twice yields byte-identical files.
- A hostile title such as `../../x` cannot produce a path outside the vault subdir.
- Writes are atomic, and the note path is recorded in the job result.

## Step 13: Entity registry and linking

**Goal.** Recurring people and topics accumulate across episodes.

**Build.** `vault/entities.py`, the `entities`, `aliases`, and `mentions` tables, entity
notes, wikilinks in episode notes; `earmark entities rebuild`.

**Acceptance.**
- Two episodes naming the same person with different casing or spacing produce one
  entity note listing both episodes with date, context, and timestamp.
- Re-storing an episode does not duplicate its mentions.
- `aliases.yaml` merges are honoured.
- Episode notes link each entity, and every link resolves to an existing file.
- User text outside the managed markers in an entity note survives regeneration.
- `earmark entities rebuild` reconstructs the registry from `analyses/*.json` and
  yields the same notes.
- Entity names are sanitized like titles.

## Step 14: Vector index and search

**Goal.** Semantic search across everything shared.

**Build.** `index/chunks.py`, `index/embed.py`, `index/lancedb_index.py` behind the
`index` extra; `earmark search`, `earmark reindex`, `GET /search`.

**Acceptance.**
- Transcript chunks follow segment boundaries and carry start and end times. Summary,
  claims, and leads are indexed as their own rows with a `kind`.
- Reindexing an episode replaces its rows without duplicates.
- Search returns ranked hits with episode, timestamp, snippet, and note path, from both
  the CLI and the authenticated endpoint.
- Tests run a real LanceDB in a temp directory with a deterministic fake embedder.
- A table built with a different embedding model or dimension is detected, with an
  error pointing to `earmark reindex`.
- `earmark reindex` rebuilds the index from artifacts.
- The default suite passes without the extra installed.

**Research.** Architecture risk 3 for lancedb and the embedding library. If LanceDB is
unusable on this platform, choose another store from the original sketch and record why.

## Step 15: Notifications and doctor

**Goal.** Know when a share is done, and diagnose a broken setup quickly.

**Build.** `notify/ntfy.py`; `earmark doctor [--online]`.

**Acceptance.**
- Done, failed, and skipped jobs each send a distinct title and message. Failures
  include the reason.
- With `OBSIDIAN_VAULT_NAME` set, the notification carries an `obsidian://` link to the note.
- The ntfy token is sent only when configured. Notification errors are logged and never raised.
- `doctor` checks the token, data directory and vault writability, the database, that
  the selected backends' extras import, and credentials. `--online` adds reachability
  checks. It exits non-zero on any failure and redacts secrets.

## Step 16: Deployment and Shortcut docs

**Goal.** Someone can deploy this on the homelab and build the Shortcut from the docs.

**Build.** `Dockerfile`, `compose.yaml` (API and worker, shared data volume, vault bind
mount, GPU profile for the worker), systemd unit examples, `docs/deploy.md` (Tailscale
first, Cloudflare Tunnel as the alternative), `docs/shortcut.md`, and the `README.md`.

**Acceptance.**
- The README quickstart runs verbatim in the sandbox with fake backends. The verifier
  executes it.
- The example request body in `docs/shortcut.md` validates against `IngestRequest`, by a
  test that extracts it from the doc.
- `docs/shortcut.md` lists each Shortcut action in order: accept URLs from the share
  sheet, ask for an optional prompt, POST JSON with the bearer header, show the response.
- The README's configuration reference covers every `Settings` field, enforced by a test.
- CI builds the container image on an arm64 runner without pushing it. The sandbox has
  no Docker, so the verifier reviews the container files statically, and
  `docs/deploy.md` says the image is build-tested in CI but not run-tested, and that
  the GPU profile is untested.

## Step 17: Extension: re-run a prompt

**Goal.** Ask a new question of an episode already stored.

**Build.** A `reanalyze` job kind that starts at the analyze stage;
`POST /episodes/{key}/analyze`, `GET /episodes`; `earmark reanalyze`.

**Acceptance.**
- A reanalyze job does not call the resolver, fetcher, or transcriber.
- The note gains a second analysis section, and entities and index are updated.
- An unknown key returns 404. A key with no transcript returns 409.
- `GET /episodes` lists key, show, title, date, and the prompts already run.

## Step 18: Extension: weekly digest

**Goal.** One note a week pulling together research leads across everything shared.

**Build.** `earmark digest [--since 7d | --week 2026-W47]`, writing
`Digests/<ISO-year>-W<week>.md`; scheduling notes in `docs/deploy.md`.

**Acceptance.**
- The window is selected by `shared_at` in the configured timezone, with boundary tests.
- With a fake LLM, the digest groups leads into themes and links each to its episode
  note and timestamp. Every link resolves.
- An empty window makes no LLM call and writes no note.
- Running twice for the same week yields the same file, and user text outside the
  managed markers survives.

## Backlog

Not scheduled. The design leaves room for each.

- Speaker diarization (pyannote). `Segment.speaker` already exists.
- Other sources: YouTube, articles, PDFs. The seam is the source registry behind
  `POST /ingest` and the `Resolver` protocol.
- `earmark ask`: question answering over the index.
- A Shortcut callback or a small status page.
