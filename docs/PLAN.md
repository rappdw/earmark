# Implementation plan

The steps are each small enough for one implementer and checkable in the dev sandbox.
Read `docs/ARCHITECTURE.md` first: this file says what to build in what order, and that
file says how the pieces fit.

**Re-scoped on 2026-11-23 (steps 22–27).** Steps 01–21 built the full pipeline. That
pipeline resolved an episode, transcribed it, and analysed it with Claude. It then wrote
the result into a vault, linked entities, indexed it for search, and produced a weekly
digest. It is tagged `v0.1.0-full-pipeline`. See "Re-scope: a transcription and delivery
service" below.

**Widened on 2026-11-24 (steps 28–31).** The repo becomes the whole system: the iOS
Shortcut, the service, and the PKA receiving skill, with the two seams between them. See
"System scope: one repo for the whole system" below.

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
| 18 | Extension: weekly digest | 10, 12 | done |
| 19 | Fix: library errors are not bad audio | 08 | done |
| 20 | Fix: dry-run findings (third-party URL logging, noisy output) | 19 | done |
| 21 | Hugging Face telemetry off by default | 20 | done |
| 22 | Re-scope I: remove analysis, vault, entities, search, re-run, digest | 21 | done |
| 23 | Re-scope II: architecture and CLAUDE.md | 22 | done |
| 24 | Transcription requests: audio upload or URL, destination, instructions | 23 | done |
| 25 | Delivery: the transcript note and the email | 24 | done |
| 26 | GPU transcription inside the image | 25 | done |
| 27 | Docs, runbook and Shortcut for the transcription service | 26 | done |
| 28 | System scope: the three parts and the two seams | 27 | todo |
| 29 | The mail contract as a tested seam | 28 | todo |
| 30 | The PKA marketplace and the receiving skill | 29 | todo |
| 31 | The Shortcut as a file | 28 | todo |

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

## Step 19: Fix: library errors are not bad audio

**Goal.** A broken or incompatible dependency must not be mistaken for unusable audio.

**Why.** `transcribe/faster_whisper.py` wraps any exception raised while decoding in
`UnusableAudioError`, which fails the job permanently and deletes its audio. With
PyAV 19.0.2 and faster-whisper 1.2.1, every file fails with
`could not decode <file>: TypeError` (reproduced with the real `tiny` model on
2026-11-20, when Dependabot proposed the bump). If that upgrade had landed, every
episode would have failed permanently and lost its download.

**Acceptance.**
- Only genuine decode failures count as unusable audio: PyAV's decode and FFmpeg
  errors for bad input, and a file that decodes to no samples. They stay permanent and
  delete the audio unless `KEEP_AUDIO` is set.
- Any other exception from the decoder or the model is not a permanent audio error. It
  takes the worker's normal unexpected-exception path (transient, with a traceback), and
  the audio file is kept. A test shows the PyAV-19-style `TypeError`, raised from the
  decoder, keeps the audio and requeues the job.
- The planner audits the other stages for the same kind of broad catch, where an
  exception from the code or a library becomes a permanent, data-destroying outcome.
  Each one found is fixed in this step or listed with its reason.
- `earmark doctor` warns when the installed `av` and `faster-whisper` versions are a
  known-incompatible pair, without loading a model.

## Step 20: Fix: dry-run findings (third-party URL logging, noisy output)

**Goal.** Fix what the first end-to-end dry run found. That run, on 2026-11-21, used a
real Apple URL, the real resolver and download, faster-whisper `tiny` on CPU, the fake
analyzer, and the vault store.

**Findings.**
1. `huggingface_hub` makes its requests with plain `httpx`, and the `httpx` logger
   prints every request URL at INFO, query strings included. For example:
   `INFO httpx: HTTP Request: GET https://huggingface.co/api/resolve-cache/...?...&etag=...`.
   Step 07 quieted only `httpx2`, `httpcore2` and `anthropic`, so the architecture's
   "URL paths and queries are not logged" rule is broken by a third-party logger.
2. The Hugging Face "You are sending unauthenticated requests to the HF Hub" warning
   prints twice during a model download: once on stderr and once as a log record.
3. Every `earmark digest --since …` run ends with "; the window is not over yet",
   because a `--since` window always ends tomorrow at midnight. The note is only
   useful for an in-progress `--week`.

**Acceptance.**
- No third-party logger records a URL path or query at INFO or below. The planner
  audits every library earmark or its extras can load, and quiets or filters each one
  that does: at least `httpx`, `huggingface_hub`, and any used by faster-whisper,
  fastembed (which uses loguru), lancedb, and the anthropic SDK. A test drives a
  representative request through each library that can run offline, with sockets
  blocked, and asserts that no URL path or query reaches the log output. Earmark's own
  host-only messages remain.
- A model download shows the Hugging Face authentication hint at most once, or not at
  all. Earmark never asks for or reads an HF token unless the operator sets one.
- `--since` output no longer says the window is not over. `--week` still says it for
  an in-progress week.
- The opt-in `slow` and `index` tests still pass where they can run.

## Step 21: Hugging Face telemetry off by default

**Goal.** Earmark's own deployments send no Hugging Face telemetry. This is the
operator's decision of 2026-11-21.

**Why.** Step 20 found that `hf_xet` sends a download summary to the Hub
(`telemetry accepted … xet_download_summary`) and writes its own log files under
`$HF_HOME/xet/logs/`. `huggingface_hub` has telemetry of its own. Step 20 documented the
settings but left them unset.

**Acceptance.**
- Every shipped way of running earmark sets `HF_HUB_DISABLE_TELEMETRY=1`: the
  container image, both compose services, and every systemd unit, including the digest
  service. An operator can still override it.
- With the setting applied, a real model download in this sandbox, using the
  `transcribe` extra in a scratch venv, shows whether `hf_xet` still sends its summary.
  The planner records the result. If `HF_HUB_DISABLE_TELEMETRY` alone does not stop it,
  the plan adds whatever setting does, such as `DO_NOT_TRACK=1`, or documents that
  nothing does.
- The planner decides whether `HF_XET_LOG_DEST` should also be set by default, and
  records why either way.
- `docs/deploy.md` and `docs/ARCHITECTURE.md` say telemetry is off by default and how
  to turn it back on. A test pins the setting in each deploy file.

---

## Re-scope: a transcription and delivery service

**What changes.** earmark stops analysing and storing knowledge. It becomes a
transcription service on the DGX Spark, and the operator's PKA instances do the analysis
and filing, each with its own context. earmark's job is:

1. Accept a request carrying one of: an audio file, a direct audio URL, or an Apple
   Podcasts episode link. The request also carries a destination email address, free-form
   instructions for the recipient ("analyse this for …", "file this under …"), and
   optionally a title.
2. Transcribe the audio on the Spark's GPU.
3. Email two Markdown attachments to that address. One is the transcript note, which is
   data. The other holds the requester's instructions. The body carries only text earmark
   generates, and the subject carries only the sanitised, capped title.
4. Send an ntfy push saying the transcript was sent, or why the job failed.

**Decisions (the operator's, 2026-11-23).**

- **Not part of AMAP.** earmark is not enrolled in the AMAP fleet. It sends ordinary
  email. Each PKA instance already has an address, and its runtime treats an attachment
  as readable only from an allowlisted sender that passes DMARC. So setup includes a
  sending mailbox on a domain the operator owns. The repo uses `earmark@example.org` as
  its placeholder and never defaults the sender.
- **Recipients: an allowlist, with `all` as the open setting.** `EARMARK_ALLOWED_RECIPIENTS`
  is a comma-separated list of exact addresses, or the literal `all`. Setting `all` is
  the operator's written record of an accepted risk: anyone holding the API token can
  then mail anyone from the operator's domain, and can instruct both PKAs. When the
  setting is unset, every request is refused. The caller chooses personal or work, since
  the Shortcut fills in `to`; earmark never defaults it.
- **Delivery is at-least-once.** A crash after the SMTP server accepts the message, but
  before earmark records it as sent, cannot be told apart from a failure. In that case
  earmark resends with the same `Message-ID` and the same `job_id`. The PKA treats the
  same `guid` plus the same `job_id` as a duplicate, and ignores it.
- **Re-shares resend the whole transcript.** Email is stateless, and 300 KB is small. A
  re-share is a new job, so it gets a new `job_id` and the same `guid`. The PKA keeps one
  note and runs the new instructions against it. earmark stays send-only and reads no
  mail.
- **Direct URLs are identified by content.** A URL is downloaded again on every request
  and hashed. Transcription, the expensive part, is cached by that hash. Uploads work the
  same way. Podcasts keep their episode identity and their audio cache.
- **Old databases are refused.** No v0.1.0 deployment exists (the operator is at
  runbook phase 0). A database created by `v0.1.0-full-pipeline` makes startup fail with
  a clear message saying to move it aside. It is not migrated.
- **No "analysis done" relay for now.** ntfy stays for "sent" and "failed".
- **No PKA-side skill is needed from earmark.** PKA is gaining inbound email.
  *Superseded on 2026-11-24:* the receiving skill is part of this repo (step 30). See
  "System scope" below.
- **The attachment size cap is configurable.**
- **Removed code is deleted.** It stays recoverable from the `v0.1.0-full-pipeline` tag.

**The mail contract (pka-skills, `contract: 1`, 2026-11-23).** Status: **DRAFT**. It
becomes final once the operator approves the PKA's receiving procedure. A revision would
come with a contract version bump. Step 25 builds to this text.

- **Both files**
  - **Encoding:** UTF-8 with no BOM and LF line endings. The extension is a lower-case
    `.md`, and the media type is `text/markdown`.
  - **Frontmatter:** YAML between `---` lines at the very top of the file.
  - **Feed- or caller-supplied strings:** double-quoted and escaped, on one line, with
    control characters stripped. They are capped at 300 characters, and `title` at 200.
  - **Unknown values:** the key is omitted. A value is never an empty string or null.
  - **Formats:** timestamps are RFC 3339 with an offset, and dates are `YYYY-MM-DD`.
  - **Filenames:** `YYYY-MM-DD-<slug>.md`. The slug is lower-case ASCII `a-z0-9` joined
    by single hyphens, at most 80 characters. A podcast slug is
    `<show-slug>-<title-slug>`, truncated as a whole.
  - **Filename date:** the publish date when known, otherwise the date of `shared_at` in
    the requester's offset.
- **Attachment 1, the note: `YYYY-MM-DD-<slug>.md`.** Podcasts and audio share one
  schema.

  | key | type | podcast | audio | notes |
  |---|---|---|---|---|
  | `type` | string | required | required | `podcast` or `audio` |
  | `contract` | integer | required | required | `1` |
  | `source` | string | required | required | `earmark` |
  | `origin` | string | required | required | `apple_podcasts`, `upload` or `url` |
  | `title` | string | required | required | podcast: the episode title; audio: the request's `title`, else the upload's filename stem or the URL's last path segment, sanitised |
  | `show` | string | required | omit | |
  | `date` | date | required | required | the filename date |
  | `published` | date | optional | optional | only when actually known |
  | `duration` | string | required | required | `hh:mm:ss` |
  | `language` | string | optional | optional | BCP 47 |
  | `apple_url` | string | optional | omit | |
  | `source_url` | string | omit | optional | `origin: url` only, without userinfo, query or fragment; omitted entirely when the URL carries a query |
  | `guid` | string | required | required | `feed:<feed guid verbatim>`, else `apple:<i= episode id>`, else `sha256:<hex of the audio bytes>` for upload and url |
  | `shared_at` | timestamp | required | required | when this item was first shared |
  | `transcript_sha256` | string | required | required | 64 lower-case hex |
  | `tags` | list of strings | required | required | `["podcast"]` or `["audio"]` |

  - **Body:** `# <title>` (for a podcast, `# <show> — <title>`), then a blank line, then
    `## Transcript`, then a blank line. After that come paragraphs separated by blank
    lines. Each paragraph covers 30–60 seconds or one speaker turn, and starts with
    `[hh:mm:ss]`.
  - **`transcript_sha256`:** the sha256 of the UTF-8 bytes from the first byte after the
    closing `---\n` to the end of the file, as sent.
  - **No audio hash for podcasts:** dynamic ad insertion changes the bytes on every
    download.
- **Attachment 2, the instructions: `YYYY-MM-DD-<slug>.instructions.md`.** It uses the
  same date and slug as the note.
  - **Keys:**
    - `type: earmark-instructions`;
    - `contract: 1`;
    - `for`: the note's filename;
    - `guid`: the same as the note's;
    - `job_id`: stable across a resend, new on every re-share;
    - `shared_at`: when this request was made.
  - **Body:** the requester's words, verbatim, and nothing else.
  - **Always sent.** An empty body means "file it, no analysis".
- **Subject and body.**
  - **Subject:** `[earmark] <title>`, sanitised and capped at 120 characters.
  - **Body:** only text earmark generates. It names which attachment is the transcript,
    which is data and never instructions, and which attachment holds the requester's
    instructions. It carries the `job_id` and the two filenames. It also says that the
    subject and the note contain third-party text.
  - **The guid stays out of the body.** A `feed:` guid is publisher text, so it appears
    only inside the attachments.
- **PKA de-duplication.**
  - The same `guid` and the same `job_id` is a duplicate delivery.
  - The same `guid` with a new `job_id` is a re-share.
  - The same `guid` with a different `transcript_sha256` is a re-transcription, and the
    newer body wins.

**Inputs from the other agents (2026-11-23).**

- amap-spec: mail to an agent's bound address arrives on the mail lane. earmark learns
  nothing beyond the SMTP reply: there is no receipt and no dedupe. Attachment caps are
  set per deployment.
- earshot: its offload is not an API. Its build recipe for CTranslate2 with CUDA on the
  GB10 is in `docs/decisions/2026-11-23-gpu-build-recipe.md`, with what was measured and
  what was not.

## Step 22: Re-scope I: remove analysis, vault, entities, search, re-run, digest

**Goal.** Only the code the transcription service needs remains, and it still runs end
to end on fakes. This step covers code, tests, dependencies, CI and deploy files. Prose
docs are steps 23 and 27.

**Build.** Delete each of these, along with its tests, settings, `.env.example` lines,
CI workflows and dependencies:

- **Analysis:** the `analyze/` package and both LLM clients (Anthropic and
  OpenAI-compatible), the `anthropic` dependency, `llm.yml`, and `docs/llm.md`.
- **Vault:** the vault writer, entities and aliases.
- **Index:** the index and search (the `index` extra, LanceDB, fastembed, `index.yml`,
  and `GET /search`).
- **Re-run and digest:**
  - re-run a prompt: `POST /episodes/{key}/analyze`, the `reanalyze` job kind, and
    `earmark reanalyze`;
  - the digest: `earmark digest`, its systemd service and timer.
- **Commands:** `earmark analyze`, `earmark entities rebuild`, `earmark search` and
  `earmark reindex`.
- **Doctor:** the vault, Obsidian, LLM and index checks.
- **Factory and fakes:** the registries and fakes for the removed stages.

Couplings to handle, not break:

- **Move `vault/sanitize.py` and the frontmatter emitter** (`yaml_quote`,
  `emit_frontmatter`, `file_component` and what they need) to a kept module, with their
  tests. ntfy imports them today, and step 25 needs them for the note.
- **PyYAML moves to the dev dependency group.** `tests/test_ci.py` and
  `tests/test_deploy.py` parse YAML with it.
- **Deploy files.**
  - compose: drop `VAULT_PATH` and the vault mount, and build with `EXTRAS: transcribe`.
  - Dockerfile: drop `/vault`, `EARMARK_VAULT_DIR`, and the `index` default in `EXTRAS`.
  - systemd: delete the digest service and timer.
  - `container.yml`: stop importing `fastembed` and `lancedb`.
  - `network.yml`: stop installing `--extra index`.
  - Update `tests/test_deploy.py` and `tests/test_ci.py` to match.
- **Test markers.** Removing the `llm` and `index` pytest markers, and the Anthropic
  isolation in `tests/conftest.py`, is authorised. It removes configuration for code
  that no longer exists; it does not loosen it. Socket blocking and every other marker
  stay.
- **The interim push.** Until step 25, a finished job pushes "Transcribed: `<title>`"
  with the duration. The push carries no URL, prompt or path.
- **Old databases.** Startup of `serve`, `worker` and every command that opens the
  database refuses a database created by `v0.1.0-full-pipeline` with a clear message
  that names the file and says to move it aside. Detect it by schema version or table
  shape. Take the v0.1.0 DDL from `git show v0.1.0-full-pipeline:src/earmark/db.py` for
  the test fixture.
- **Docs in this step.** Change only the docs whose tests force it. For example, the
  README settings table must still cover every `Settings` field. Delete `docs/llm.md`.
  The README, `docs/deploy.md`, `docs/shortcut.md` and the runbook otherwise wait for
  step 27, and `docs/ARCHITECTURE.md` and `CLAUDE.md` for step 23. Until then they may
  describe removed behaviour. The verifier must not fail this step for that.

Keep:

- the API and its auth;
- the job store and worker;
- the resolver and guarded fetch;
- local and remote transcription, and the transcript cache;
- ntfy;
- doctor;
- the CI gate and the network/slow workflow;
- all the safety properties already built: URL scrubbing in logs, secret redaction, the
  private-address guard, and library errors not counting as bad audio.

Until step 25, the pipeline ends after transcription and notifies.

**Acceptance.**

- `make check` and `GITHUB_ACTIONS=true make check` pass.
- No source, test, workflow, deploy file or `.env.example` line refers to a removed
  module, setting, route, command, marker or dependency. The planner lists the grep that
  proves it, excluding `docs/` and `CLAUDE.md`.
- `uv.lock` no longer contains `anthropic`, `lancedb` or `fastembed`. PyYAML is a
  dev-only dependency.
- A job runs resolve, fetch, transcribe and notify on fakes. It ends `done` with the
  transcript cached, and the push carries no URL.
- A fixture database built from the v0.1.0 DDL, holding a done ingest job, a done
  reanalyze job and a queued reanalyze job, is refused by `serve`, `worker` and
  `earmark jobs`. The message names the path. A fresh database works.
- `GET /episodes` is either kept, as a list of transcribed episodes, or removed. The
  planner decides and records why.

## Step 23: Re-scope II: architecture and CLAUDE.md

**Goal.** The design documents describe the transcription service as built after step 22,
and record the contracts that steps 24–26 build against.

**Build.**

- Rewrite `docs/ARCHITECTURE.md` for the service as it is now. Add a section,
  "Planned (steps 24–26)", that records the decisions and the mail contract from
  "Re-scope" above. It covers:
  - the request model;
  - source identity and caching;
  - the at-least-once delivery state machine;
  - the mail format;
  - the allowlist;
  - the leak rules listed in step 24.
- Rewrite the security section for client-supplied URLs and uploads. The guarded fetch is
  the only path to a direct URL. The resolver accepts only Apple Podcasts episode links.
  Recipients and instructions are data that must not leak.
- Update `CLAUDE.md`:
  - the opening description;
  - "Shape of the code";
  - the stage list;
  - "Rules that are easy to get wrong". This means removing the vault and Claude-SDK
    rules, and adding rules for the mail contract, untrusted text in mail, and the
    allowlist.
- Change no code.

**Acceptance.**

- Neither file describes removed behaviour as current. The planner lists the grep.
- Each decision in "Re-scope" above appears in `docs/ARCHITECTURE.md`.
- `make check` passes, since the doc tests still apply.

## Step 24: Transcription requests: audio upload or URL, destination, instructions

**Goal.** One API call asks for a transcription and says where it goes.

**Build.**

- **The route.** `POST /transcriptions` replaces `POST /ingest`. It accepts either:
  - JSON `{url, to, instructions?, title?}`;
  - a multipart form with an audio file plus the same `to`, `instructions` and `title`
    fields. This adds the `python-multipart` dependency.
- **`url`.** An Apple Podcasts episode link goes through the resolver. A direct
  http(s) audio URL goes through the guarded fetch. Nothing else reaches the resolver.
- **`to`.** One email address, required, checked against `EARMARK_ALLOWED_RECIPIENTS`.
  - The setting is a comma-separated list of exact addresses, or `all`.
  - Matching is case-insensitive on the domain, and exact on the local part.
  - Unset or empty means every request is refused.
  - With `all`, startup logs a warning and doctor reports it.
- **`instructions`.** Optional free text with a length cap.
- **`title`.** Optional, with a cap. It names uploads and direct URLs, and is ignored
  for podcasts.
- **Uploads.**
  - Stream to a staging directory on the data volume (`data/uploads/`), never the system
    temp dir, under `EARMARK_MAX_UPLOAD_MB`.
  - The route overrides the 64 KiB JSON body cap only for multipart.
  - The byte limit is injectable, so tests stay small.
  - Nothing is held in memory.
  - The fields may arrive after the file part. A request that fails validation after
    staging deletes its staged file.
- **The audio check at the API** is the declared content type or extension only.
  Decoding is the real test: undecodable audio ends the job as a permanent failure, with
  a clear reason.
- **Identity.**
  - Podcasts keep their episode identity and their audio cache.
  - Uploads and direct URLs are keyed by the sha256 of their bytes, reusing `file_key`
    from the transcriber. A direct URL is downloaded again on each request.
  - Generalise `Source` and `Episode`: new `Source` values `upload` and `url`, with a
    synthetic show id and the `sha256:` guid. `Transcriber`, `AudioFetcher` and
    `Notifier` keep working on an `Episode`.
- **The transcript cache key** includes the ASR backend, model and options, so a `tiny`
  dry-run transcript is never reused for `large-v3`.
- **Retention.** Audio from uploads and direct URLs is deleted when its job reaches a
  terminal state, `done` or `failed`. The transcript cache stays. Staging files left by a
  crash are swept at startup. Podcast audio keeps today's behaviour.
- **De-duplication.** The same source, `to` and `instructions` returns the existing
  job while it is active, and for `EARMARK_DEDUPE_WINDOW_S` after it finishes (default
  600), so a double tap sends one email. Any change to `to` or `instructions` makes a new
  job that reuses the cached transcript.
- **The job** stores the source, `to`, `instructions` and `title`.
- **API view model.** `GET /jobs` and `GET /jobs/{id}` return a dedicated view model,
  never the raw `Job`:
  - `to` is shown in full;
  - `instructions` is truncated to 200 characters;
  - a direct URL is shown as scheme, host and path only;
  - errors are scrubbed.
- **The CLI.** `earmark submit` takes `--to`, `--instructions`, `--title`, and a URL or
  a file.
- **The Shortcut doc.** Update `docs/shortcut.md` and its example body only as far as
  the doc tests need for the new route and fields. The full rewrite is step 27.

**Acceptance.**

- Each input form creates a job, and a test proves each one transcribes on fakes.
- **Request errors.**
  - A missing or invalid `to`, or a `to` not on the allowlist, gives 422 with a message
    the Shortcut can show.
  - A bad URL, or a show link with no episode, gives 422 with the existing messages.
- **Upload limits.**
  - An oversize upload gives 413. The size is checked against the declared length and
    against the bytes actually read.
  - A partial or rejected upload leaves nothing in staging.
- Auth is checked before the body is read.
- **De-duplication.**
  - A repeat within the window returns the same job.
  - A new `to` or new `instructions` makes a new job.
  - That new job downloads a podcast once and transcribes once, and a direct URL is
    downloaded again and not transcribed again.
- Two different direct URLs serving the same bytes share one cached transcript.
- Audio from a failed upload is deleted, and a test proves it.
- **Leak rules.** Each rule has a test that captures the log, the push or the API
  response:
  - `instructions` never appears in logs or pushes;
  - `to` appears in logs only masked, as the first character of the local part, then
    `…@`, then the domain;
  - a direct URL appears in logs and pushes host-only;
  - pushes carry no instructions, no URL path or query, and no address except in
    masked form.
- `EARMARK_ALLOWED_RECIPIENTS=all` accepts any address and logs the warning at startup.

## Step 25: Delivery: the transcript note and the email

**Goal.** A finished transcription reaches the destination as an email a PKA can file.

**Dispatch gate.** Build to the mail contract under "Re-scope" above. It is `contract: 1`
and still a draft. If pka-skills sends a revision before this step is dispatched, the
orchestrator updates that section first.

**Build.**

- **The mail backend.**
  - `EARMARK_MAIL_BACKEND=smtp|fake` selects it, through a `Mailer` stage Protocol with a
    fake, registered in the factory.
  - An smtp backend that is missing any required setting is a `ConfigError` at startup.
  - The fake records messages for tests and the quickstart.
- **The note and the instructions file**, rendered exactly to the contract. This
  covers:
  - every key and its required or omit rule;
  - slug and date rules;
  - guid prefixes;
  - stripping and the query-omission rule for `source_url`;
  - the `transcript_sha256` scope;
  - the paragraph merge.
- **Time zone.** The contract dates files and `shared_at` in the requester's offset.
  Step 22 removed `EARMARK_TZ`, so the planner decides where that offset comes from:
  the request (the Shortcut can send it), a re-added setting, or UTC. It records why.
- **SMTP settings.**
  - `EARMARK_SMTP_HOST` and `EARMARK_SMTP_PORT`.
  - `EARMARK_SMTP_SECURITY`, which is `starttls` or `tls`. There is no plaintext option.
  - `EARMARK_SMTP_USERNAME`.
  - `EARMARK_SMTP_PASSWORD`, a secret. It has its own validator, which allows the
    spaces app passwords contain and rejects control characters.
  - `EARMARK_SMTP_FROM`, which has no default.
  - `EARMARK_SMTP_TIMEOUT_S`, which must stay below the stop grace period.
  - `EARMARK_MAX_ATTACHMENT_MB`.
- **TLS and identity.**
  - Use `ssl.create_default_context()` for both `SMTP_SSL(context=…)` and
    `starttls(context=…)`, so the certificate and hostname are verified.
  - If STARTTLS is not offered, delivery fails. It never continues in plaintext.
  - AUTH happens only after TLS is up.
  - Pass `local_hostname` explicitly, set to the domain of `SMTP_FROM`, so EHLO never
    calls `socket.getfqdn()`.
  - The debug level is never set.
  - The envelope recipient is passed explicitly: the one validated `to`.
- **The message.**
  - `Message-ID` is deterministic per job, `<earmark.<job_id>@<from domain>>`, never
    `make_msgid()`.
  - The subject follows the contract, encoded by the email policy for non-ASCII text.
  - The body is earmark-generated only, as the contract says.
  - Both attachments are base64-encoded `text/markdown; charset=utf-8`, so the hash
    survives transport.
  - `EARMARK_MAX_ATTACHMENT_MB` caps the raw size of the two attachments together,
    before encoding.
- **Delivery state, at-least-once.**
  - Delivery runs outside the worker's interruptible window. A SIGTERM waits for it,
    bounded by `EARMARK_SMTP_TIMEOUT_S`, which is kept below compose's `stop_grace_period`
    and systemd's `TimeoutStopSec`. A test pins that relation.
  - A migration adds delivery columns: state `sending` with the `Message-ID` recorded
    before DATA, then `sent` with a timestamp after the 250 reply. They are written
    through lease-fenced updates.
  - A job found in `sending` after a crash or lease expiry is resent with the same
    `Message-ID` and `job_id`, and its push says "resent after an interruption".
  - A `sent` job is never sent again.
- **Failure classes.**
  - A 4xx reply, or a connection error before DATA completes, is transient.
  - A disconnect after DATA, with no reply, is ambiguous. It is retried, which is
    at-least-once.
  - A 5xx reply is permanent.
  - An authentication failure is permanent, with a message naming the settings.
  - Job errors store the reply code, the enhanced status code and an earmark-written
    summary, never the server's reply text, which echoes addresses.
- **Doctor.** `earmark doctor --online` connects, negotiates TLS, logs in and quits,
  sending nothing.
- **The ntfy push** says the transcript was sent, with the title and the masked
  recipient, or says the job failed and why. The step-24 leak rules apply.

**Acceptance.**

- **The note.**
  - The frontmatter parses as YAML and round-trips hostile titles.
  - Unknown keys are omitted.
  - Every contract key and rule has a test, including both guid fallbacks and the
    `source_url` rules.
  - The paragraphs carry absolute `[hh:mm:ss]` markers and cover every segment, and no
    text is truncated.
  - `transcript_sha256` matches a recomputation over the decoded attachment bytes.
- **The message.**
  - A title, guid or instructions text cannot inject email headers.
  - Untrusted text never appears in the body.
  - The subject is encoded correctly for non-ASCII text.
- **Size cap.** A job over `EARMARK_MAX_ATTACHMENT_MB` fails permanently with a message
  naming the setting. Nothing is truncated or split.
- **Resends.**
  - A crash simulated between DATA and the `sent` write resends with the same
    `Message-ID`.
  - A job already `sent` is not sent again on retry, restart or lease expiry.
  - A SIGTERM during delivery lets it finish.
- **TLS.**
  - A test asserts `verify_mode == CERT_REQUIRED` and `check_hostname` on the context
    used.
  - A server without STARTTLS fails delivery.
  - EHLO uses the configured hostname.
- **Failure classes.**
  - Each class above has a test.
  - No job error contains the recipient address or the server's reply text.
- **Secrets.** The SMTP password never appears in logs, job errors, `earmark config` or
  doctor output.
- **Tests.**
  - They run against a hand-written SMTP stub on a socketpair or unix socket, after
    `tests/uds_http.py`, with sockets blocked. The stdlib `smtpd` is gone in 3.13.
  - They assert on the exact bytes of the message, with an injected clock, `Message-ID`
    and MIME boundary.
- **Live test.** An opt-in test under a new `smtp` marker sends one message to an
  operator-supplied address. It never runs by default.
- **Operator checks only:**
  - TLS against the real server;
  - DMARC passing at the recipient;
  - the PKA reading both attachments.

## Step 26: GPU transcription inside the image

**Goal.** The worker transcribes on the Spark's GB10 GPU in-process, without the vLLM
service.

**Build.**

- **The GPU image.** Add a GPU build of the image, for example `Dockerfile.gpu` or a
  build target. It compiles CTranslate2 with CUDA for `sm_121` following
  `docs/decisions/2026-11-23-gpu-build-recipe.md`, with the earshot attribution comment.
  - Build against the venv's Python 3.13 and swap the built bindings into `/app/.venv`.
    The planner chooses the base: the slim `nvidia/cuda` devel and runtime pair, which
    nobody has tried, or the NGC base, which earshot proved. It records why.
  - The `CT2_VERSION` build argument defaults to the locked `ctranslate2` version.
    `v4.6.0` is the documented fallback if the Spark smoke fails.
  - The build stage has the Python 3.13 headers and pybind11. Use a uv-managed 3.13
    if the base ships an older Python.
  - A build-time check imports `ctranslate2` and `faster_whisper` and asserts the swapped
    version.
  - Nothing at runtime invokes `uv sync` or `uv run` without `--no-sync`. Either would
    put the locked PyPI wheel back. A static test pins this.
- **The worker.**
  - It runs `faster_whisper` with `EARMARK_ASR_DEVICE=cuda`.
  - faster-whisper's batched pipeline is an option, `EARMARK_ASR_BATCH_SIZE`, with 0 or
    unset meaning off. The option feeds the transcript cache key.
- **Compose.** Remove the `asr` (vLLM) service and its `gpu` profile, and replace them
  with the GPU image and `gpus: all`. Keep the `remote` backend as the generic option for
  a server elsewhere.
- **Tests and workflows.** Update the tests and the workflow that pin the old service:
  `tests/test_deploy.py`, around the `asr` service and gpu profile, and
  `.github/workflows/container.yml`.
- **Settings.** New settings go in `Settings`, `.env.example` and the README settings
  table.
- **Doctor** reports the CUDA device count and whether `float16` is among
  `ctranslate2.get_supported_compute_types("cuda")`.
- Run this step after step 25, serially. It touches compose and the deploy docs.

**Acceptance.**

- The CPU image still builds in CI, unchanged in behaviour.
- **Static tests** check the GPU build file:
  - the cmake flags from the recipe;
  - `CMAKE_CUDA_ARCHITECTURES` taken from the build argument;
  - the version default matching `uv.lock`;
  - the attribution comment;
  - no `pip install` that could pull a CPU `ctranslate2` over the build.
  
  The planner decides whether CI builds the GPU image at all, given the free runner's
  roughly 14 GB of disk, and records why.
- **Spark smoke.** The operator gets a documented smoke test for the Spark:
  1. build;
  2. `ctranslate2.get_cuda_device_count() >= 1`, with `ctranslate2.__file__` inside
     the venv, checked after any uv command;
  3. `doctor`;
  4. transcribe a short public clip on `cuda`, and see `float16` in the log.
  
  The plan says plainly that this smoke test is the only real check of the GPU path.
- With `EARMARK_ASR_DEVICE=cuda` and no CUDA device, the worker refuses to start with a
  clear message. That behaviour already exists and stays.

## Step 27: Docs, runbook and Shortcut for the transcription service

**Goal.** The operator can set up the service, the sender address and the Shortcut from
the docs alone.

**Build.** Rewrite the README, `docs/deploy.md`, `docs/shortcut.md` and
`docs/runbook-spark.md` for the new service.

- **The Shortcut.**
  - A "Choose from Menu" gives personal or work and fills in `to`.
  - "Ask for Input" provides `instructions`.
  - It posts to `POST /transcriptions`.
- **The runbook** drops the vLLM path and uses the GPU image from step 26. Its phases
  each end in a check.
- **Email setup:**
  - a sending mailbox on the operator's domain, with SPF, DKIM and DMARC;
  - an app password for SMTP submission;
  - the sender being allowlisted at each PKA's runtime, which the operator arranges with
    those agents;
  - `EARMARK_ALLOWED_RECIPIENTS`, and what setting it to `all` accepts.
- **Upgrading from v0.1.0:**
  - move the old database aside;
  - delete the old `analyses/`, index and embedding-model directories under the data
    dir;
  - the vault is no longer touched.
- **Carried over:** reachability, ntfy and the share-sheet capture.
- **Placeholders only:** no real addresses, hosts or tokens.

**Acceptance.**

- The doc-driven tests pass, updated for the new route, fields and settings. The README
  settings table covers every `Settings` field.
- The Shortcut example body validates against the new request model.
- The runbook's phases end in checks. One check sends a real transcription to an
  address the operator controls, and confirms that both attachments open and the body
  names them.
- Nothing in the docs describes removed behaviour.

## System scope: one repo for the whole system

**What changes.** Steps 22–27 built one part, the service, and the plan's status table
measured only that part. From 2026-11-24 this repo holds the whole system. That is three
largely independent parts, and the two seams where they meet:

| Part | What it is | Runs on | Lives in |
|---|---|---|---|
| 1. The Shortcut | The iOS Shortcut that sends audio or a link | the operator's iPhone | `shortcut/` (step 31); `docs/shortcut.md` until then |
| 2. The service | API, worker, GPU transcription, mail delivery | the DGX Spark | `src/earmark/` (unchanged) |
| 3. The receiving skill | Handles an earmark email that AMAP has delivered: checks it, files the note, acts on the instructions | each PKA instance | a Claude Code plugin in this repo's marketplace (step 30) |

| Seam | Between | Contract | Tested by |
|---|---|---|---|
| Request | Shortcut → service | `POST /transcriptions`, in `docs/ARCHITECTURE.md` | the service's API tests, and the Shortcut doc tests checking the Shortcut's requests against the request model |
| Mail | service → (AMAP) → receiving skill | the mail contract, `contract: 1` | sample emails in `contract/` that both sides test against (steps 29, 30) |

**How mail reaches a PKA.** AMAP is outside this repo, and earmark only relies on it:

1. The service sends ordinary SMTP email to the PKA instance's email address. earmark
   is not an AMAP fleet member, just an outside sender.
2. AMAP takes the email delivered to that address and provides it to the PKA through
   AMMP (the Agent Mediated Messaging Protocol).
   - The AMAP router applies the sender allowlist and DMARC.
   - Attachments are readable (disposition `clean`) only for an allowlisted sender that
     passes DMARC.
3. Inside the PKA's sandbox, AMAP rings a content-free doorbell in the live session:
   "you have mail; use the inbox read tool". With no session running, the mail waits
   for the next one.
4. The PKA reads the message and its attachments with the `inbox` tools. The receiving
   skill takes over from there.

**Decisions (the operator's, 2026-11-24).**

- **One repo.** The project is too small to split. One plan and one runbook cover the
  system, and a change across a seam lands in one commit.
- **The receiving skill is distributed as a plugin marketplace in this repo.** For the
  first cut, the repo root carries `.claude-plugin/marketplace.json`. A PKA instance adds
  the marketplace from `rappdw/earmark` and installs the plugin from it. pka-skills was
  told on 2026-11-24 not to build an earmark-specific receiving procedure, and was
  asked how PKA instances install plugins, how inbound mail reaches a skill, which
  filing conventions to reuse, and how a PKA comes to act on one sender's instructions.
- **The mail contract moves here.** pka-skills drafted `contract: 1`, and its text is
  unchanged. From step 29 the source of truth is `contract/`, and `docs/PLAN.md` and
  `docs/ARCHITECTURE.md` point to it. A change is still a version bump, because mail
  sent under one version can still be sitting in an inbox when the next one ships.
- **Definition of done for the system:** one episode shared on the phone is
  transcribed on the Spark, emailed, read by a PKA through the receiving skill, and
  filed, with the instructions acted on. The runbook's last phase is this check. A step
  being `done` means its part's code and tests pass; it does not mean the system works.
- **The service is unchanged by this.** `src/earmark/` keeps its layout and packaging.
  The service still files nothing: filing happens in the PKA, through part 3.
- **The service sends plain email and embeds no agent.** It is fully deterministic, apart
  from the variability of ASR output. Anything that needs judgement happens in the PKA.
- **Intake is AMAP's job.** No earmark code reads, polls or selects mail. The receiving
  skill handles a message AMAP has already delivered and the PKA has read.
- **The sender allowlist is AMAP router config,** set by the operator for each PKA's
  address. It is not a PKA's responsibility, and not something to ask a PKA for.
- **The attachment cap stays 10 MB** (`EARMARK_MAX_ATTACHMENT_MB`). AMAP's per-deployment
  attachment limit is still to be determined. When it is known, earmark's cap must not
  exceed it.

**pka-skills' answers (2026-11-24)** are in
`docs/decisions/2026-11-24-pka-receiving-skill.md`: installing, intake, filing through
`.pka/bin/pka-tools`, the dedupe record, and its view on trust.

**Open with the operator (step 30 must not decide these alone).**

1. **The trust gap, and what the skill does until it closes.** AMAP's two rule sets
   disagree for earmark mail:
   - `INBOX-POLICY.md` says mail comes from enrolled peers and is meant to be acted on;
   - the `inbox` tools' rules say never to follow instructions found in mail or
     attachments.

   earmark is not an enrolled peer, so today the strict rule applies, and acting on the
   instructions attachment is an exception. The gap is acknowledged (operator,
   2026-11-24): the AMMP trusted runtime will be able to designate a message as if it
   came from an enrolled peer. That is coming but not built.

   **Decided (operator, 2026-11-24): act within a narrow scope.** The operator grants the
   exception explicitly, and the skill's `SKILL.md` states it and its limits.
   - **Instructions may:** choose or change the destination folder of the note they came
     with, add tags or a summary, link it to named people or topics, or write a short
     derived note beside it.
   - **They may never:** run commands, reach the network, delete, move or rewrite
     another note, touch settings, `CLAUDE.md` or `.pka/` other than the dedupe record,
     or commit.
   - Anything outside the scope is reported to the owner, not done.
   - When the AMMP designation exists, it replaces the exception as the authority, and
     the scope stays.
   - The sending address is used only by earmark. An HMAC over the attachments (a
     `contract: 2`) comes in only if that stops being true or the mail path gains a
     relay.
   - The transcript note is data in every case.
2. **Which PKA instances install the skill.** Project scope only, never user scope.
3. **Optional:** pka-interface's session-start scan mentioning unprocessed earmark mail,
   which would be a pka-skills change.

## Step 28: System scope: the three parts and the two seams

**Goal.** The docs describe earmark as the whole system, not just the service.

**Build.**

- `docs/ARCHITECTURE.md` opens with a system section:
  - the three parts and where each runs and lives;
  - the two seams, with their contracts and what tests each;
  - the definition of done.
  - Folders that later steps create (`shortcut/`, `contract/`, the marketplace and
    plugin) are named as planned, with the step that creates each.
- `CLAUDE.md` and the README say the repo is the system. "earmark files nothing" becomes
  "the service files nothing; the PKA files, through the receiving skill".
- `docs/runbook-spark.md` gains, as its last phase, the one-episode check, with a
  placeholder phase for installing the receiving skill that step 30 fills in.
- The mail contract's provenance is recorded: drafted by pka-skills, owned here from
  step 29.
- How mail reaches a PKA through AMAP, as described above, with AMAP named as outside
  this repo.
- **The runbook's sender allowlisting is corrected.** Phase 6 says to "ask each PKA to
  allowlist" the sender. It becomes the operator adding earmark's sending address to the
  AMAP router's allowlist for each PKA's address. AMAP's attachment limit is a runbook
  check: it must be at least `EARMARK_MAX_ATTACHMENT_MB`.

**Acceptance.**

- The doc tests pin the system section: the three parts, the two seams, and the
  definition of done.
- The runbook's last phase is the one-episode check, and it ends in a **Check:**.
- No doc claims a part works that has not been run. The Shortcut file, the receiving
  skill and the GPU image are described as unbuilt or unverified.

## Step 29: The mail contract as a tested seam

**Goal.** The mail contract has one source of truth, with sample emails that both sides
test against.

**Build.**

- `contract/` holds the contract text for `contract: 1`, moved from `docs/PLAN.md` and
  `docs/ARCHITECTURE.md`, which link to it.
- `contract/samples/` holds sample emails built from fixed inputs by the real mail
  code. At least:
  - a podcast with a `feed:` guid;
  - a podcast with an `apple:` guid;
  - an upload, and a direct URL (`sha256:` guids);
  - empty instructions;
  - a title that needs sanitising;
  - a resend, which keeps the `Message-ID`.
- A stdlib-only checker validates an email against the contract: file names,
  frontmatter keys and types, `transcript_sha256`, the body rules, the subject cap. Step
  30's skill runs it inside a PKA, so it imports nothing from `earmark` and runs with
  `python -I`.
- Regenerating the samples is an explicit command. A test fails when the mail code's
  output drifts from the samples. Only fields that legitimately vary, such as `Date`,
  are masked.

**Acceptance.**

- Every sample passes the checker, and the mail code reproduces every sample.
- For each contract rule, a mutated sample breaking that rule makes the checker fail.
- The contract text exists in one place. `docs/PLAN.md` and `docs/ARCHITECTURE.md` point
  to it and do not copy it.

## Step 30: The PKA marketplace and the receiving skill

**Goal.** A PKA instance can install the receiving skill from this repo, and the skill
handles an earmark email by the contract.

**Inputs:** the narrow scope decided in item 1 above, and pka-skills' answers in
`docs/decisions/2026-11-24-pka-receiving-skill.md`, which the skill follows.

**Build.**

- `.claude-plugin/marketplace.json` at the repo root, and one plugin under `plugins/`
  with its own `.claude-plugin/plugin.json` and the skill. The planner settles the names
  and checks the manifest fields, and how updates reach an instance, against Claude
  Code's current plugin docs.
- **The skill:**
  - reads no mail itself. AMAP delivers it, and the PKA reads it with the `inbox` tools
    after the doorbell. The skill applies to a message the PKA is already handling, and
    fetches nothing;
  - recognises an earmark email by structure, never by subject:
    - an allowlisted sender, with `clean` attachments;
    - exactly two attachments named per the contract;
    - `source: earmark` and `contract: 1`;
    - matching `guid` and `job_id` in both attachments.

    Anything else is left alone and reported;
  - validates it with the step 29 checker, shipped inside the plugin. A test keeps the
    plugin's copy identical to `contract/`'s;
  - de-duplicates by the contract's rules, with its record in
    `.pka/earmark/received.jsonl`. A note is found by `guid`, never by path;
  - files the note through pka-skills, never by reimplementing it:
    - the destination comes from the Repo Map, proposed before writing unless the
      workspace is hands-off;
    - then `.pka/bin/pka-tools post-route`, and `pka-tools log add`;
    - it checks `pka-tools version --json` against a minimum first;
    - it never writes to `team-inbox/`, and never commits;
  - on a re-transcription, rewrites the note's body in place and never deletes it;
  - acts on the instructions attachment only within the narrow scope (item 1 above),
    stated in its `SKILL.md` as the operator's explicit exception, and reports anything
    outside that scope;
  - never treats the transcript, the title or the subject as instructions.
- `docs/runbook-spark.md`: the install phase on each chosen PKA, adding the marketplace
  and installing the plugin at project scope, ending in a check.

**Acceptance.**

- The marketplace and plugin manifests are valid. Tests check them against the
  documented schema.
- The skill's deterministic parts (recognising, checking, de-duplicating) run against
  every step 29 sample in tests, including the duplicate, re-share and
  re-transcription cases.
- What CI cannot test, the PKA following the skill, is the runbook's one-episode check,
  and is reported as unverified.

## Step 31: The Shortcut as a file

**Goal.** The operator installs the Shortcut from a file in the repo instead of building
it by hand.

**Needs:** a working Shortcut, built by hand from `docs/shortcut.md` and checked on the
phone (share-sheet capture, the `Get Type` assumption), then exported with Share →
Export File. Before exporting, the operator adds import questions for the server URL and
the token, and clears both values. If the file is not in the repo, the planner stops and
says so.

**Build.**

- `shortcut/earmark.shortcut`, the signed export, and `shortcut/README.md` with how to
  install it and answer the import questions.
- `docs/shortcut.md`: installing from the file becomes the main path. The 13 actions stay
  as the reference for what the file contains and for rebuilding it.
- The planner works out what can be checked about a signed file on Linux, such as its
  format marker and whether the payload is readable. If the payload cannot be read, the
  no-secrets check is a manual runbook check, not a test.

**Acceptance.**

- A test checks that the file is present and in the signed export format.
- The file carries no token and no real hostname: checked by a test if the payload can
  be read, otherwise by a runbook check, which the step plan says plainly.
- The runbook's Shortcut phase installs from the file and ends in a check.

## Backlog

Not scheduled. The design leaves room for each.

- **A send-rate cap.** It limits the damage of a leaked token when
  `EARMARK_ALLOWED_RECIPIENTS=all`.
- **Re-shares as a reference only.** Send `guid` plus the new instructions instead of
  the whole transcript. This needs earmark to remember what it sent where, and to read
  replies such as "not found".
- **An "analysis done" push.** The PKA would reply, and earmark would relay the reply as
  a push. This needs earmark to read mail.
- **Speaker diarization.** earshot runs pyannote 3.1 on the GPU, and needs `HF_TOKEN`
  with the gated licences accepted. `Segment.speaker` already exists, and the contract
  allows a paragraph per speaker turn.
- **Other sources:** YouTube, other podcast platforms, and video files.
- **An async transcription API for other callers**, such as earshot.
