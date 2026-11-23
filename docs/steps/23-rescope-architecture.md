# Step 23: Re-scope II: architecture and CLAUDE.md

## 1. Outcome

When this step is done, `docs/ARCHITECTURE.md` describes earmark as step 22 left it: an
API that takes an Apple Podcasts episode link, a job store and worker, the resolver behind
the guarded fetch, local and remote transcription with the transcript cache, ntfy, and
doctor. Nothing in the part before "Planned (steps 24–26)" describes analysis, the vault,
entities, search, re-runs or the digest, and nothing there describes planned behaviour
except through one-line pointers that name the step.

A new section, **"Planned (steps 24–26)"**, holds the contracts that steps 24–26 build
against. It carries, verbatim from `docs/PLAN.md` "Re-scope", the "What changes" list, the
operator's ten decisions, the mail contract (`contract: 1`, DRAFT) and the inputs from the
other agents. It also restates the briefs of steps 24–26 as contracts: the request model,
source identity and caching, the allowlist, de-duplication and retention, the leak rules
and API view model, security for client-supplied input, delivery, the at-least-once state
machine, failure classes, pushes, and the GPU image. Choices the PLAN leaves to a later
step's planner are listed as open, each with its owning step. A last section, **"History:
removed in step 22"**, records what went and where to find it.

`CLAUDE.md` is replaced by the text in section 4.5. No code, test, config, deploy or
workflow file changes, and `docs/PLAN.md` is not edited.

## 2. Findings

### 2.1 How it was checked

- Read `CLAUDE.md`, `docs/PLAN.md` (Re-scope section, lines 607–734; briefs 22–27, lines
  736–1157), `docs/ARCHITECTURE.md` (all 1664 lines), `docs/steps/22-rescope-remove.md`,
  `docs/decisions/2026-11-23-gpu-build-recipe.md`, and the code at `HEAD` (`6230408`).
- The code matches the step 22 plan in every point this step describes. Checked directly:
  `models.py` (`Source = Literal["apple_podcasts"]`, `JobKind = Literal["ingest"]`,
  `Stage = Literal["resolve", "fetch", "transcribe", "notify"]`, `JobResult(duration_s)`),
  `pipeline/stages.py` (four Protocols, four-field `Backends`), `pipeline/runner.py`,
  `pipeline/factory.py` (four registries; log line `backends: resolver=%s fetch=%s asr=%s
  notify=%s`), `db.py` (`APPLICATION_ID = 0x45524D4B`, one migration `0001_jobs`,
  `identity_error`, `open_database` order `check_identity` → `ensure_wal` → `migrate`),
  `config.py` (27 fields), `notify/ntfy.py` (`TITLE_PREFIX` Transcribed/Failed/Skipped, no
  `click`), `api/routes.py` (four routes), `cli.py` (eight commands: `config`, `doctor`,
  `serve`, `worker`, `submit`, `jobs`, `resolve`, `transcribe`), `doctor/checks.py` (checks
  `api token`, `data dir`, `database`, `resolver`, `fetch`, `asr`, `asr libraries`, `asr
  model`, `notify`), `sanitize.py`, `log.py` (`QUIET_LOGGERS` = httpx, httpcore, httpx2,
  httpcore2, urllib3, huggingface_hub), the deploy files and the three workflows.
- Deploy facts the Planned section relies on: `compose.yaml` gives the worker
  `stop_grace_period: 2m`; `deploy/systemd/earmark-worker.service` has
  `TimeoutStopSec=120` (the API unit has 30). Step 25's `EARMARK_SMTP_TIMEOUT_S` must stay
  below both.
- `uv.lock` today: `ctranslate2` 4.9.0, `faster-whisper` 1.2.1, `av` 18.1.0,
  `huggingface-hub` 1.33.0, `hf-xet` 1.8.0.
- `ruff format --check .` formats Python fences in Markdown (ruff 0.17.6). The existing
  Stage-contracts Python block, reduced to the four Protocols, was checked ruff-clean.
- `tests/test_docs.py` reads `docs/ARCHITECTURE.md` only in
  `test_deploy_doc_hf_telemetry_section`, which requires the literal strings
  `HF_HUB_DISABLE_TELEMETRY=1` and `HF_HUB_DISABLE_TELEMETRY=0`. No test reads `CLAUDE.md`.
- `README.md` points into ARCHITECTURE by section name: "Data layout" (line 53) and "HTTP
  API" (line 152). Both headings must stay.
- The verification scripts in section 7 were run against the current files and against
  `docs/PLAN.md` itself: the verbatim check passes on PLAN (65/65 items) and fails on the
  current ARCHITECTURE (0/65). The removed-behaviour check, run on a copy of the current
  ARCHITECTURE with a Planned heading appended, finds 163 problem chunks. It finds 8 in the
  current `CLAUDE.md`, and none in the `CLAUDE.md` text of section 4.5. The facts check
  fails on the current file for the reasons it should: extra settings rows, missing
  headings, no `earmark jobs`, no `0x45524D4B`.

### 2.2 Things in the sources that later planners need, recorded as open

- **The contract's feed guid is not today's `Episode.guid`.** The contract wants
  `guid: feed:<feed guid verbatim>`, else `apple:<i= episode id>`. Today
  `resolve/resolver.py` line 176 takes Apple's `episodeGuid` first, then the matched feed
  item's GUID, then `apple-podcasts:<episode_id>`; Apple's and the feed's GUIDs can differ
  (art19, Known risk 2), and `Episode.guid` also feeds the episode key. This plan records it
  as open for step 25. It does not decide it.
- **The time zone.** The contract dates files and `shared_at` in the requester's offset.
  Today `IngestRequest.timestamp` keeps its offset only until enqueue: the job stores
  `shared_at` as a UTC instant. Step 22 removed `EARMARK_TZ` and `tzdata`. Step 25 decides
  (the PLAN lists the request, a re-added setting, or UTC).
- **The frontmatter emitter needs adapting.** `sanitize.emit_frontmatter` writes `null` for
  `None` (the contract omits unknown keys instead) and refuses aware datetimes
  (`shared_at` is RFC 3339 with an offset). Step 22 said step 25 adapts it.

### 2.3 One inconsistency inside PLAN's Re-scope section

"What changes", item 3, says "The body and subject carry only text earmark generates." The
mail contract, which step 25 "builds to", says the subject is `[earmark] <title>` (the
title is feed- or caller-supplied text) and that the body "says that the subject and the
note contain third-party text". The contract is the more specific text and is internally
consistent, so the Planned section copies both verbatim and adds one note under the mail
contract: the subject carries the sanitised title, which is third-party text, as the
contract says. That is reading the contract, not a new decision. The orchestrator should
mention it to the operator, who may want item 3 reworded in PLAN.

### 2.4 Dead ends

- **Do not mention removed things in the current part, even as history.** "the vault
  writer was removed", "`docs/steps/10-analysis.md`", "unlike the LLM client" all trip the
  check in section 7.2. Everything about what was removed goes in "History: removed in step
  22". Watch for incidental words: *knowledge*, *vector*, *embedding*, *analysis*, *LLM*,
  *digest note*, *Obsidian*, and the old step-plan file names `10-analysis.md`,
  `11-local-llm-map-reduce.md`, `12-vault-writer.md`, `13-entities.md`,
  `14-vector-index.md`, `17-reanalyze.md`, `18-digest.md`. The word "analysis" may appear in
  the current part only in a paragraph that also says "PKA" (the PKAs do it).
- **Do not describe planned behaviour in the current part.** A paragraph, bullet or table
  row there that mentions mail, instructions, recipients, uploads, `POST /transcriptions`,
  SMTP, a PKA or any other planned term must also say "step 24", "step 25" or "step 26"
  and be a pointer, not a description.
- **Do not paraphrase the verbatim blocks.** The check in section 7.3 compares
  whitespace-collapsed text. Rewrapping and re-indenting are fine; changing a word, a
  backtick or a dash is not.
- **Do not copy the briefs' build instructions as contracts.** "Update
  `tests/test_deploy.py`", "Update `docs/shortcut.md` only as far as…", "Run this step
  after step 25" are instructions to the implementer of that step, not design. Section
  4.3 says what to carry.
- **Do not invent defaults.** No value the PLAN leaves unset (caps, ports, timeouts,
  column names, file names, the base image) gets one here. Each is listed as open.
- **The heading uses an en dash**: `## Planned (steps 24–26)` (U+2013), as in PLAN. A hyphen
  breaks every check.
- **Do not edit `docs/PLAN.md`**, its status table included. The orchestrator owns it.
- **Do not run `pytest -m network`.** It publishes to public ntfy.sh. This step needs only
  `make check`.

## 3. Files

| File | Change |
|---|---|
| `docs/ARCHITECTURE.md` | Rewritten per section 4.1–4.4. |
| `CLAUDE.md` | Replaced by the text in section 4.5, exactly. |
| `docs/steps/23-rescope-architecture.md` | This plan (already written). |

Nothing else changes. `docs/PLAN.md`, `README.md`, `docs/deploy.md`, `docs/shortcut.md`,
`docs/runbook-spark.md`, `docs/asr.md`, the decision records, older step plans, and
`.claude/` stay as they are.

## 4. Interfaces

The "interfaces" of a docs step are the document structure and the content each part must
carry.

### 4.1 Structure of `docs/ARCHITECTURE.md`

Top-level sections, exactly these and in this order (section 7.4 checks it):

1. `## Decisions`
2. `## Flow`
3. `## Module layout`
4. `## Domain models`
5. `## Stage contracts`, with `### Runner and worker`, `### Fetching`, `### Resolving`,
   `### Transcribing`, `### Notifying`, `### Doctor`
6. `## Jobs`, with `### Database identity`
7. `## Data layout`
8. `## HTTP API`
9. `## Configuration`
10. `## Deployment`
11. `## Security`
12. `## Testing`, with `### Continuous integration`
13. `## Known risks`
14. `## Planned (steps 24–26)`, with the 17 subsections of section 4.3, in order
15. `## History: removed in step 22`

No other `##` or `###` headings outside code fences. "Everything before `## Planned
(steps 24–26)`" is called **the current part** below.

### 4.2 The current part, section by section

"Keep" means keep the existing text verbatim. "Edit" lists every change. Anything not
mentioned in a kept section stays.

**Title and introduction** (replace the opening paragraph):

- `# Architecture`.
- earmark is a self-hosted transcription service. Today a request is an Apple Podcasts
  episode link; the worker resolves the audio, transcribes it, caches the transcript and
  sends an ntfy push.
- One pointer paragraph: steps 24–26 add uploads and direct audio URLs with a destination
  address and instructions, email delivery to the operator's PKA instances, which do the
  analysis and filing, and GPU transcription in the image; their contracts are in "Planned
  (steps 24–26)".
- How to read it: the sections before "Planned (steps 24–26)" describe the code as it is;
  "Planned" is what steps 24–26 build against; "History: removed in step 22" lists what step
  22 removed. When a step changes a contract described here, it changes this file in the
  same commit. Steps 01–21 built a larger pipeline, tagged `v0.1.0-full-pipeline`.

**Decisions** (the table; header and "How to change it" column as now):

- Delete the rows "System of record", "LLM for analysis" and "Vector store".
- New first row **Scope**: earmark transcribes, and from step 25 delivers the transcript by
  email; analysis and filing are done by the operator's PKA instances. "How to change it":
  see "Planned (steps 24–26)".
- New row **State**: the job table (SQLite, WAL) and the per-episode cache under
  `DATA_DIR` (`episode.json`, `shared.json`, `transcript.json`, `transcript.txt`, audio
  while it is needed). earmark is not a system of record. A database from earlier than
  step 22 is refused, not migrated (see "Database identity"). "How to change it": a new
  migration in `db.py`.
- **Notification**: ntfy, off unless `EARMARK_NTFY_URL` is set: one JSON publish to the
  server root per finished job, `Transcribed: <title>` with the duration, and no link
  (steps 15, 22). Works with public ntfy.sh or a self-hosted server on the LAN or tailnet.
  The Shortcut can also poll `GET /jobs/{id}`. Change: another `Notifier`.
- **Queue**: keep.
- **Transcription**: keep the first sentence and the CPU-only wheel sentence; end with
  "or a self-built CUDA CTranslate2, which step 26 plans inside the image."
- **Reachability**: keep.
- **Deployment**: one container image (CPU, extras by build argument, `transcribe` by
  default) running either `earmark serve` or `earmark worker`; `compose.yaml` with a shared
  data volume; GPU transcription through a vLLM Whisper service in the compose `gpu`
  profile and the `remote` ASR backend; systemd units for the API and the worker (steps 16,
  22).
- **Language and tooling**: Python 3.13 (as now), uv with `uv_build`, FastAPI, pydantic,
  httpx2 (as now), typer, stdlib `sqlite3`, defusedxml for RSS (step 07). PyYAML is a dev
  dependency only: tests parse the workflows and `compose.yaml` with it (step 22). Sync code
  throughout the worker. Keep the "How to change it" cell.

**Flow**: replace the diagram with:

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
   complete    JobResult(duration_s)
   notify      ntfy (never fails the job)
```

Keep the paragraph under it. Add: `prompt` is stored with the job and is part of its dedupe
key; nothing reads it after enqueue (step 22 kept it so the Shortcut keeps working).

**Module layout**: rewrite the tree for the files that exist. Every `.py` file under
`src/earmark/` except `__init__.py` must be named (section 7.4 checks it). Entries:

- `__init__.py`, `__main__.py`, `config.py`, `log.py` ("…scrub_urls; adopt_library_loggers"),
  `models.py`, `errors.py`, `artifacts.py`: as now.
- `db.py`: SQLite connection, migrations (`PRAGMA user_version`), and the lineage check
  (`PRAGMA application_id`, step 22).
- `jobs.py`, `fetch.py`, `audio.py`, `ingest.py`: as now.
- `sanitize.py`: untrusted text to file names and YAML, and the frontmatter emitter;
  standard library only (step 22).
- `endpoint.py`: `host_label`, `error_detail`, shared by the operator-endpoint clients
  (remote ASR, ntfy).
- `fakes.py`: a fake backend for each of the four stages (tests and smoke runs).
- `cli.py`, `api/`: as now.
- `pipeline/`: `stages.py` "stage Protocols, Backends (the four stage implementations)";
  `runner.py`, `factory.py`, `worker.py` as now.
- `resolve/`, `transcribe/`: as now.
- `notify/`: `ntfy.py` (`split_topic_url`, `clean_field`, `compose`, `NtfyClient`,
  `NtfyNotifier`: the `ntfy` Notifier; step 15).
- `doctor/`: as now.
- `tests/`: mirrors src; `fixtures/` holds recorded Apple JSON and RSS, and
  `share_sheet.json`; `legacy_db.py` builds a database shaped like v0.1.0's for the refusal
  tests (step 22).
- `Dockerfile, .dockerignore, compose.yaml`: as now.
- `deploy/systemd/`: `earmark-api.service` and `earmark-worker.service` (step 16).
- `deploy/caddy/Caddyfile`: the example reverse proxy (step 16).

**Domain models**:

- Intro paragraph: keep, except "then the artifact JSON, SQLite, and vault writes would
  fail" becomes "then the artifact JSON and SQLite writes would fail", and the last sentence
  becomes "`models.scrub_surrogates` is the helper; `runner.describe_error` applies it to
  job text."
- `IngestRequest`: keep; add that `prompt` is stored on the job and is part of its dedupe
  key, and nothing reads it after enqueue (step 22). Step 24 replaces this request.
- Delete `ReanalyzeRequest`, `Analysis` (and `Claim`, `Entity`, `ResearchLead`) and
  `StoreResult`.
- `EpisodeRef`, `Episode`, `Segment`, `Transcript`: keep. In `Episode`, `source` is
  `apple_podcasts`, the only `Source` today.
- `JobResult`: `duration_s` (required), the transcript's duration (step 22).
- `ShareRecord`: the content of `shared.json`, the earliest share time of the episode,
  recorded by the runner on every job (earliest wins). Nothing reads it yet; it is kept for
  the note's `shared_at` (step 25).
- `Job`: `id` (`uuid4().hex`), `kind` (`ingest`, the only `JobKind`), `status`, `stage`
  (`Stage` is `resolve`, `fetch`, `transcribe` or `notify`; `notify` is never set), `url`
  (required: the canonical episode URL, so `Episode.url` is canonical too), `prompt`,
  `episode_key | None` (set after resolve), `attempts`, `run_after`, `error` (failure
  message or skip reason), `warnings`, `result: JobResult | None`, `shared_at`,
  `created_at`, `updated_at`, `heartbeat_at`.
- **Episode key**: keep.
- Delete **Entity key** and **Prompt hash**.
- **Errors**: keep the first two sentences about the three subclasses and their messages.
  Delete the `NeedsChunking` sentences.

**Stage contracts**:

- Intro: keep.
- The Python block: the existing block with `Analyzer`, `Store` and `Indexer` removed:
  `Resolver`, `AudioFetcher`, `Transcriber`, `Notifier`, two blank lines between classes,
  `...` bodies, the `# raises SkipEpisode if no public feed` comment kept. It must stay
  ruff-clean.
- The paragraph under it: "`Notifier` receives the job in its terminal state." only.
- Fakes paragraph: every stage has a fake in `fakes.py` that records each call (`calls`),
  can be told to raise (`fail_next(exc, times)`), and, for the cache-aware stages (fetch,
  transcribe), counts the calls that did real work (`computed`). The cache-aware fakes write
  real artifacts through `ArtifactStore`. `make_fake_backends(data_dir)` builds the set, and
  `FakeBackends.backends()` turns it into the `Backends` the runner takes.
- Rules 1–3: keep. Rule 4 becomes "**Notify failures never fail the job.** They are
  recorded in `job.warnings`." Rule 5: "A stage that succeeds with a caveat (a transcript
  without segments) puts a message in the `warnings` of the `Transcript` it returns. The
  runner copies those into `job.warnings`. They are stored in the artifact, so a cache hit
  reports them again."

**Runner and worker**:

- **Backends**: `Backends` holds the four stages; `notifier` is `None` when its backend is
  `none`. Keep the rest of the bullet.
- **Order**: `Pipeline.run(claim)`: resolve → write `episode.json`, record the episode key
  on the job, record the share time in `shared.json` → if `transcript.json` exists, read it
  and skip fetch and transcribe, else fetch → transcribe → refuse a transcript for another
  episode (permanent) → copy the transcript's warnings to the job → `complete` with
  `JobResult(duration_s)` → notify. Each stage sets `job.stage` before it runs, so a fresh
  job ends at `transcribe` and a job with a cached transcript at `resolve`; a failed job
  keeps the stage it failed in, and `notify` is never set as a stage.
- Delete **Reanalyze order**.
- **Outcomes**: keep, except the last sentence becomes "Notifier failures are warnings
  (`notify failed: …`) and never change the status."
- **Notify**, **Heartbeat**, **Shutdown**, **Lost lease**, **Each poll**: keep.

**Fetching**, **Resolving**, **Transcribing**: keep verbatim.

**Notifying**:

- **`ntfy` Notifier** bullet: keep.
- **Content**:
  - Titles are `Transcribed: <episode>`, `Failed: <episode>` and `Skipped: <episode>`
    (`podcast episode` when unknown). Tags `white_check_mark`, `x`, `fast_forward`; priority
    4 for failed, 3 otherwise.
  - The message starts with the show, or `Apple Podcasts episode` when the episode is
    unknown. Done adds `Duration: HH:MM:SS` and the warning count with the first warning.
    Failed adds the stage, `Reason: <job.error>`, and `Gave up after N attempts.` when there
    was more than one. Skipped adds the reason.
  - No push carries a URL, the prompt, or a path, and none has a `click` link.
  - Keep the "Every value is cleaned" sub-bullet verbatim.
- Delete the **Link** bullet. **Errors**: keep.
- Add one pointer line: this wording is interim; step 25 changes it to "sent" or "failed".

**Doctor**:

- First bullet: keep.
- **Offline checks**, in order: the API token; `DATA_DIR` writability (one random temp
  file, created and removed); the database (schema version, application id, journal mode,
  `quick_check` and job counts, read without migrating; a missing file is never created; a
  database from before step 22 or another application's file is `FAIL` with the refusal
  message of "Database identity"); each selected backend built through the factory registry
  (resolver, fetch, asr: imports its extra and runs the CUDA check); for `faster_whisper`
  only, `asr libraries` (keep the existing sub-bullet) and `asr model` (whether the Whisper
  model is local or in the cache, through faster-whisper's
  `download_model(local_files_only=True)`); `notify`. Keep "Offline doctor makes no network
  call…", with "Fake backends are warnings."
- **`--online` checks**: keep the timeout sentence; the checks are the iTunes lookup through
  `fetch.py`, `GET {ASR_BASE_URL}/models` for the `remote` backend, and ntfy `GET
  /v1/health` plus, with a token, `GET /v1/account`. Doctor never publishes a notification.
- Delete **Digest notifications**. **Redaction**: keep.

**Delete** the sections Analyzing, Storing, Indexing and search, Re-running a prompt, and
Digest.

**Jobs**:

- Keep the states line and every bullet, with these edits:
  - **Connections**: keep.
  - **Dedupe**: "Enqueueing the same `(url, prompt)` while an identical job is `queued` or
    `running` returns the existing job (`Enqueued.created` is `False`). A partial unique
    index on a `dedupe_key` column enforces one active job per request, so a double-tapped
    share creates one job. Prompts are normalised before the key is computed (stripped,
    blank is `None`)." Nothing about other job kinds.
- New `### Database identity` (step 22; exact rules in `docs/steps/22-rescope-remove.md`
  section 4.2):
  - The migration list restarted in step 22 with one migration, `0001_jobs` (the `jobs`
    table with `CHECK (kind IN ('ingest'))`, `jobs_active_dedupe`, `jobs_by_status`), so
    `SCHEMA_VERSION` is 1. Migrations stay append-only from here.
  - The lineage is marked by `PRAGMA application_id = 0x45524D4B` (`b"ERMK"`), stamped in
    the same transaction that creates `jobs`.
  - The table of `identity_error(path, application_id, user_version)`: `(0, 0)` new or empty
    file, adopted; `(0, >0)` `LegacyDatabaseError` (earmark 0.1.0 wrote `(0, 2)`; dev
    databases of steps 03–12 read `(0, 1)`); `APPLICATION_ID`, any version: accepted, then
    the newer-than-supported check applies; anything else `ForeignDatabaseError`.
  - Both messages, exactly as in `db.py`.
  - `open_database` runs `check_identity()` before `ensure_wal()` and `migrate()`, and
    `migrate` checks again inside its `BEGIN IMMEDIATE`, so a refused file is never
    modified: version, application id, journal mode and rows unchanged.
  - Who opens it: `serve`, `worker`, `submit` and `jobs` refuse with `earmark <cmd>: cannot
    open the job store: <message>` and exit 1; `config`, `resolve` and `transcribe` never
    open it; `doctor` reports `FAIL database <message>`. Nothing is migrated.

**Data layout**: replace the tree with:

```
$EARMARK_DATA_DIR/               a local filesystem: WAL needs shared memory (no NFS/SMB)
  earmark.db                    jobs (application id ERMK, schema 1); WAL mode
  episodes/<episode_key>/       paths owned by artifacts.ArtifactStore
    episode.json                written by the runner after resolve
    shared.json                 ShareRecord: first share time, kept by the runner (earliest wins)
    audio.<ext>                 deleted after transcription (or when it proves undecodable)
                                unless EARMARK_KEEP_AUDIO; kept on every other failure
    .audio.<ext>.<random>.part  download in progress; renamed to audio.<ext> when complete
    transcript.json             written last; its existence marks the transcript cached
    transcript.txt              Transcript.to_text()
  huggingface/hub/              in the container image only (HF_HOME=/data/huggingface):
                                faster-whisper's model cache when ASR_MODEL_DIR is unset (step 16)
```

Delete everything after the tree in the old section (the vault tree, Names, Identity,
Notes, Managed regions, Entities). Add one sentence: an episode key is checked against
`[0-9a-f]{20}` before it becomes a path, and nothing outside `DATA_DIR` is written.

**HTTP API**:

- Keep the auth paragraph.
- Table rows, exactly four: `POST /ingest` (keep), `GET /jobs/{id}` ("The `Job` as JSON:
  status, stage, error, warnings, `url`, `prompt`, and `result` (`{duration_s}`). 404 if
  unknown."), `GET /jobs` (keep), `` `GET`, `HEAD /healthz` `` (keep the existing cell).
- One pointer line: step 24 replaces `POST /ingest` with `POST /transcriptions` and the raw
  `Job` with a view model (see "Planned (steps 24–26)").

**Configuration**:

- Keep the first paragraph.
- Table rows for exactly the 27 `Settings` fields (section 7.4 checks the set). Keep the
  existing rows for `API_TOKEN`; `HOST`, `PORT`; `RESOLVER_BACKEND`; `FETCH_BACKEND`;
  `ASR_BACKEND`; `NOTIFY_BACKEND`; the `ASR_MODEL`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`,
  `ASR_LANGUAGE` row; `ASR_MODEL_DIR`; the `ASR_BASE_URL`, `ASR_API_KEY`,
  `ASR_MAX_UPLOAD_MB` row; `ASR_TIMEOUT_SECONDS`; `KEEP_AUDIO`; `NTFY_URL`, `NTFY_TOKEN`;
  `MAX_AUDIO_MB`, `MAX_FEED_MB`, `ALLOW_PRIVATE_FETCH`; `JOB_MAX_ATTEMPTS`,
  `JOB_LEASE_SECONDS`, `WORKER_POLL_SECONDS`; `LOG_LEVEL`. `DATA_DIR`'s meaning becomes
  "Database and episode cache". Delete every other row.
- Keep the backend-names paragraph. Delete the Anthropic credential paragraph.
- Add: variables left from older versions are ignored like any unknown `EARMARK_*` key.

**Deployment**:

- **Image**: keep, except dependencies use the extras in `EXTRAS` (default `transcribe`);
  "`/data` is owned by it"; the environment is `EARMARK_HOST=0.0.0.0`,
  `EARMARK_DATA_DIR=/data`, `HF_HOME=/data/huggingface`, `HF_HUB_DISABLE_TELEMETRY=1`.
- **Compose**:
  - `api` (`serve`) and `worker` (`worker`) share the image (built with
    `EXTRAS: ${EXTRAS:-transcribe}`) and the named volume `data` at `/data`.
  - Settings from `env_file: .env`; `environment:` forces `DATA_DIR`, `HOST`, `PORT`, an
    empty (so default) `ASR_MODEL_DIR`, and `HF_HUB_DISABLE_TELEMETRY`.
  - Keep the `HF_HUB_DISABLE_TELEMETRY: ${HF_HUB_DISABLE_TELEMETRY:-1}` bullet verbatim (it
    holds the `HF_HUB_DISABLE_TELEMETRY=0` string the doc test needs).
  - Keep Exposure and Hardening. `api` has a healthcheck that fetches `/healthz`.
  - The worker gets `stop_grace_period: 2m`; SIGTERM releases the running job.
  - Keep the `gpu` profile bullet.
  - Compose-only variables: `API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
    `ASR_GPU_MEMORY_UTILIZATION`, `HF_HUB_DISABLE_TELEMETRY` (keep the rest of the bullet).
- **Reverse proxy**: keep.
- **Native**: `deploy/systemd/earmark-{api,worker}.service` run
  `/srv/earmark/.venv/bin/earmark serve|worker` as user `earmark`, with
  `WorkingDirectory=/srv/earmark`, `KillSignal=SIGTERM`, `ProtectSystem=strict`, and
  `ReadWritePaths=/srv/earmark/data`; `TimeoutStopSec` is 30 for the API and 120 for the
  worker. Both set `Environment=HF_HUB_DISABLE_TELEMETRY=1` (step 21). Keep the three
  sentences about overriding it (they hold `Environment=HF_HUB_DISABLE_TELEMETRY=0`).
- **Not verified**: keep, ending "… documented in `docs/deploy.md` as an unverified outline,
  not provided; step 26 plans it."

**Security**:

- Intro: "The service runs on a private network, but it fetches URLs that third parties
  control and handles text they wrote."
- **Ingest**: keep, and add "The resolver accepts only Apple Podcasts episode links." Then
  one pointer sentence: the rules for client-supplied direct URLs, uploads, recipients and
  instructions come with step 24 and are in "Planned (steps 24–26)".
- **Outbound fetches**: keep.
- **Third-party logging**:
  - Wire loggers: `httpx`, `httpcore`, `httpx2`, `httpcore2`, `urllib3` and
    `huggingface_hub` (and their children). They print request URLs and redirect `Location`
    headers. The filter is what holds: libraries reset their own levels at import
    (`HF_HUB_VERBOSITY` sets `huggingface_hub`'s, which earmark imports lazily, after
    logging is configured). `LOG_LEVEL=DEBUG` does not show these records.
  - URL scrubbing: keep, with the request-line example `"GET /jobs?status=failed&limit=5
    HTTP/1.1"` becoming `"GET /jobs HTTP/1.1"`, and "uvicorn's access line keeps the
    method, route, and status but not the query".
  - Handlers: keep, with the lazy imports being `transcribe.faster_whisper.load_runtime`
    and `model_cache_status` only.
  - Keep the `describe_error` sentence.
- **XML**, **Resolver requests**: keep.
- **Operator-configured endpoints**: the remote ASR server (step 09) and the ntfy server
  (step 15) only. Keep the ASR rules and the ntfy sentence; both share
  `endpoint.error_detail` and `endpoint.host_label`. `earmark doctor --online` reaches both
  read-only, and the iTunes lookup through `fetch.py`, guard included.
- **Notifications**: titles come from feeds, and `job.error` and warnings can quote them.
  Keep the cleaning and "JSON body only" sub-bullets. Notifications carry no transcript
  text, and no secret, topic, URL, prompt, or path. There is no `click` link.
- New **Untrusted text to names and YAML**: `earmark.sanitize` (`clean_unicode`,
  `fit_bytes`, `file_component`, `yaml_quote`, `emit_frontmatter`; standard library only,
  step 22). Today only the ntfy client uses it (`clean_unicode`, `fit_bytes`); step 25
  renders the note and the instructions file with it.
- **Model downloads**: keep the first sentences up to "A cached model loads offline.",
  delete the embedding-model sentences, keep *Tokens*, *hf_xet* and *Telemetry, off by
  default* (it holds `HF_HUB_DISABLE_TELEMETRY=1`).
- **Secrets**: `API_TOKEN`, `ASR_API_KEY`, `NTFY_URL` and `NTFY_TOKEN` are `SecretStr`,
  redacted in `earmark config` and `earmark doctor`, and never logged. Keep the ntfy topic
  sentences.
- Delete **Search**, **Vault writes** and **LLM**.

**Testing**:

- First paragraph: markers `network`, `slow` and `gpu`; tests carrying `network` or `slow`
  get sockets back (`tests/conftest.py`); the default `-m` expression deselects all three.
  Keep the rest.
- `network`: keep the Apple-and-feeds sentence, the remote ASR live test sentence, and the
  ntfy live test sentence (it publishes to public ntfy.sh unless `NTFY_TEST_URL` is set).
  Delete the local LLM live test sentence.
- `slow`: keep, and add that `tests/test_log_libraries.py` is `slow` too (step 22): it
  drives `huggingface_hub` through `httpx.MockTransport` and `httpx`/`httpcore` against
  `tests/uds_http.py`, with sockets blocked, and checks that no URL path or query reaches
  earmark's log output; the default suite does the same for `httpx2`/`httpcore2` and
  uvicorn.
- `gpu`: needs CUDA; it does not get sockets back.
- Delete the `llm` and `index` bullets and the `FASTEMBED_CACHE_PATH` sentence. For every
  test without `network` or `slow`, `tests/conftest.py` sets `HF_HUB_OFFLINE=1`, and every
  test starts with no `EARMARK_*` variables, in its own temp directory.
- Stub paragraph: the remote ASR tests (keep the sentence); the ntfy client with
  `httpx2.MockTransport` and `tests/ntfy_stub.py`; doctor through `doctor.Probes`
  (injected transports and an injected `GuardedFetcher`; missing extras simulated with
  `sys.modules` stubs). `tests/legacy_db.py` builds the v0.1.0-shaped database for the
  refusal tests.
- Documents: keep the **Markers** bullet. **What `tests/test_docs.py` checks**: the README
  settings table lists every `Settings` field once; the quickstart sets all four backends to
  `fake`; `docs/shortcut.md`'s example body validates against `IngestRequest` and is
  accepted by `POST /ingest`, and the example response matches; the Shortcut actions'
  order; relative links in the README, `docs/deploy.md` and `docs/shortcut.md`; the
  README command table names every CLI command and its HTTP API sentence every route; the
  share-sheet captures; the deploy guide's reachability and telemetry sections; and that
  this file contains `HF_HUB_DISABLE_TELEMETRY=1` and `HF_HUB_DISABLE_TELEMETRY=0`.
  **`tests/test_deploy.py`**: keep, naming the two systemd units. **The gate**: keep.
- Live smoke: with every backend `fake`, `earmark serve`, `curl` an ingest, `earmark worker
  --once`, and `GET /jobs/{id}` shows the job `done` with `result.duration_s`; the
  transcript is cached under `data/episodes/<key>/`.
- **Continuous integration**: keep the `ci.yml` paragraph. CI runs the default suite only;
  it does not run `network`, `slow` or `gpu`, does not install the `transcribe` extra, never
  runs on a GPU, and does not build or run the container. There are exactly three workflows
  (`tests/test_ci.py::test_workflow_files_are_the_known_three`): `ci.yml`, `network.yml`
  (jobs `network` and `slow`; the slow job installs `--extra transcribe`, runs `uv run
  --locked --extra transcribe pytest -m slow -v -rs`, and caches `~/.cache/earmark-tests`)
  and `container.yml` (validates `compose.yaml` with and without `--profile gpu`, checks the
  telemetry variable in all three services, builds the image, runs `earmark --version`,
  imports `ctranslate2` and `faster_whisper`, and checks the UID and
  `HF_HUB_DISABLE_TELEMETRY`). No workflow references secrets
  (`test_no_workflow_references_secrets`). Keep the workflow rules, Dependabot and
  branches paragraphs.

**Known risks**:

- 1: keep, and add: step 26 plans the in-image CUDA build from
  `docs/decisions/2026-11-23-gpu-build-recipe.md`.
- 2: keep.
- 3: "**Wheels on aarch64 and Python 3.13** for `ctranslate2`, `av` and `faster-whisper`
  (the `transcribe` extra). Resolved in step 01 (2026-11-19): cp313 manylinux aarch64
  wheels install with `--only-binary :all:` and import on this sandbox. Python is pinned to
  3.13. See `docs/steps/01-scaffold.md`."
- 4: keep, without the share-sheet sentence change.
- 5, 6, 7: keep.
- Delete 8, 9 and 10.

### 4.3 The Planned section

Write `## Planned (steps 24–26)` followed by the text below, from "Nothing in this section"
to the end of "Not planned". Where it says **[verbatim …]**, paste the named lines of
`docs/PLAN.md` exactly (rewrapping and re-indenting allowed, nothing else). Where it says
**[note …]**, add that sentence after the pasted item. The rest may be lightly reworded,
but must not add or drop a rule; section 6.2 maps every item to its source.

~~~markdown
Nothing in this section is built yet. It is what steps 24–26 build against. It carries the
operator's decisions and the mail contract from `docs/PLAN.md` "Re-scope" verbatim, and
restates the briefs of steps 24–26 as contracts. A revision changes both files in the same
commit. Each step's planner may settle what is listed under "Open questions", and records
its choice here.

### What changes

earmark stops analysing and storing knowledge. It becomes a transcription service on the
DGX Spark, and the operator's PKA instances do the analysis and filing, each with its own
context. earmark's job is:

[verbatim: PLAN.md lines 613–621, the numbered list "1. Accept a request …" to "4. Send an
ntfy push …"]

### Operator decisions (2026-11-23)

[verbatim: PLAN.md lines 625–653, the ten bullets "- **Not part of AMAP.** …" to
"- **Removed code is deleted.** …"]
[note after "Old databases are refused": Built in step 22: see "Database identity" under
Jobs.]
[note after "Removed code is deleted": Done in step 22: see "History: removed in step 22".]

### Request model (step 24)

- `POST /transcriptions` replaces `POST /ingest`. It accepts either JSON `{url, to,
  instructions?, title?}`, or a multipart form with an audio file plus the same `to`,
  `instructions` and `title` fields. Multipart adds the `python-multipart` dependency.
- `url`: an Apple Podcasts episode link goes through the resolver. A direct http(s) audio
  URL goes through the guarded fetch. Nothing else reaches the resolver.
- `to`: one email address, required, checked against `EARMARK_ALLOWED_RECIPIENTS` (see
  "Recipients allowlist").
- `instructions`: optional free text with a length cap.
- `title`: optional, with a cap. It names uploads and direct URLs, and is ignored for
  podcasts.
- Uploads stream to a staging directory on the data volume, `data/uploads/`, never the
  system temp dir, under `EARMARK_MAX_UPLOAD_MB`. The route overrides the 64 KiB JSON body
  cap only for multipart. The byte limit is injectable, so tests stay small. Nothing is held
  in memory. The fields may arrive after the file part. A request that fails validation
  after staging deletes its staged file.
- The audio check at the API is the declared content type or extension only. Decoding is
  the real test: undecodable audio ends the job as a permanent failure, with a clear reason.
- Auth is checked before the body is read.
- Errors: a missing or invalid `to`, or a `to` not on the allowlist, gives 422 with a
  message the Shortcut can show. A bad URL, or a show link with no episode, gives 422 with
  the existing messages. An oversize upload gives 413, checked against the declared length
  and against the bytes actually read. A partial or rejected upload leaves nothing in
  staging.
- The job stores the source, `to`, `instructions` and `title`.
- `earmark submit` takes `--to`, `--instructions`, `--title`, and a URL or a file.
- Each input form creates a job that transcribes on fakes.

### Source identity and caching (step 24)

- Podcasts keep their episode identity and their audio cache.
- Uploads and direct URLs are keyed by the sha256 of their bytes, reusing `file_key` from
  the transcriber (`transcribe.faster_whisper.file_key`, which `earmark transcribe` uses
  today). A direct URL is downloaded again on each request and hashed; transcription, the
  expensive part, is cached by that hash. Two different direct URLs serving the same bytes
  share one cached transcript.
- `Source` and `Episode` are generalised: new `Source` values `upload` and `url`, with a
  synthetic show id and the `sha256:` guid. `Transcriber`, `AudioFetcher` and `Notifier`
  keep working on an `Episode`.
- The transcript cache key includes the ASR backend, model and options, so a `tiny` dry-run
  transcript is never reused for `large-v3`. Step 26's `EARMARK_ASR_BATCH_SIZE` feeds it
  too.

### Recipients allowlist (step 24)

- `EARMARK_ALLOWED_RECIPIENTS` is a comma-separated list of exact addresses, or the literal
  `all`.
- Matching is case-insensitive on the domain, and exact on the local part.
- Unset or empty means every request is refused.
- `all` accepts any address. Startup logs a warning and doctor reports it. Setting it is
  the operator's written record of an accepted risk: anyone holding the API token can then
  mail anyone from the operator's domain, and can instruct both PKAs.
- The caller chooses the destination, personal or work, since the Shortcut fills in `to`.
  earmark never defaults `to`. The sender, `EARMARK_SMTP_FROM` (step 25), has no default
  either; the repo's placeholder is `earmark@example.org`.

### De-duplication and retention (step 24)

- The same source, `to` and `instructions` returns the existing job while it is active, and
  for `EARMARK_DEDUPE_WINDOW_S` after it finishes (default 600), so a double tap sends one
  email.
- Any change to `to` or `instructions` makes a new job that reuses the cached transcript.
  That new job downloads a podcast once and transcribes once, and a direct URL is downloaded
  again and not transcribed again.
- A re-share is a new job: a new `job_id`, the same `guid`, and the whole transcript sent
  again.
- Audio from uploads and direct URLs is deleted when its job reaches a terminal state,
  `done` or `failed`. The transcript cache stays. Staging files left by a crash are swept at
  startup. Podcast audio keeps today's behaviour (deleted after transcription unless
  `EARMARK_KEEP_AUDIO`).

### Leak rules and the API view model (step 24)

Each leak rule has a test that captures the log, the push or the API response:

- `instructions` never appears in logs or pushes;
- `to` appears in logs only masked, as the first character of the local part, then `…@`,
  then the domain;
- a direct URL appears in logs and pushes host-only;
- pushes carry no instructions, no URL path or query, and no address except in masked
  form.

Step 25 adds two more: no job error contains the recipient address or the SMTP server's
reply text, and `EARMARK_SMTP_PASSWORD` never appears in logs, job errors, `earmark config`
or doctor output. The mail contract keeps the guid out of the mail body.

`GET /jobs` and `GET /jobs/{id}` return a dedicated view model, never the raw `Job`:

- `to` is shown in full;
- `instructions` is truncated to 200 characters;
- a direct URL is shown as scheme, host and path only;
- errors are scrubbed.

### Security for client-supplied input (step 24)

- The guarded fetch (`fetch.py`, see "Fetching" and "Security") is the only path to a
  direct URL: http(s) only, the private-address guard on every connection and redirect, and
  its size caps.
- The resolver accepts only Apple Podcasts episode links. A direct URL never reaches it, and
  nothing else does either.
- Client-supplied URLs, uploaded bytes, upload file names, titles and instructions are
  untrusted. Anything that becomes a file name or YAML goes through `earmark.sanitize`.
- Recipients and instructions are data that must not leak: see "Leak rules and the API view
  model".
- Uploads are authenticated before the body is read, streamed to `data/uploads/` under
  `EARMARK_MAX_UPLOAD_MB`, never held in memory, deleted when validation fails, and swept at
  startup.

### Delivery (step 25)

Step 25 builds to the mail contract below, which is `contract: 1` and still a draft. If
pka-skills sends a revision before step 25 is dispatched, the orchestrator updates PLAN's
"Re-scope" section first, and this copy with it.

- **Where it runs.** Delivery runs after transcription and before the job completes; the
  push then says the transcript was sent. The stage's name is step 25's.
- **Ordinary email.** earmark is not part of AMAP. It sends ordinary email from a sending
  mailbox on a domain the operator owns. Each PKA runtime reads an attachment only from an
  allowlisted sender that passes DMARC. earmark is send-only and reads no mail; it learns
  nothing beyond the SMTP reply.
- **The mail backend.** `EARMARK_MAIL_BACKEND=smtp|fake` selects it, through a `Mailer`
  stage Protocol with a fake, registered in the factory. An smtp backend that is missing any
  required setting is a `ConfigError` at startup. The fake records messages for tests and
  the quickstart.
- **The note and the instructions file** are rendered exactly to the contract: every key
  and its required or omit rule, the slug and date rules, the guid prefixes, stripping and
  the query-omission rule for `source_url`, the `transcript_sha256` scope, and the paragraph
  merge.
- **SMTP settings.**
  - `EARMARK_SMTP_HOST` and `EARMARK_SMTP_PORT`.
  - `EARMARK_SMTP_SECURITY`, which is `starttls` or `tls`. There is no plaintext option.
  - `EARMARK_SMTP_USERNAME`.
  - `EARMARK_SMTP_PASSWORD`, a secret. It has its own validator, which allows the spaces
    app passwords contain and rejects control characters.
  - `EARMARK_SMTP_FROM`, which has no default.
  - `EARMARK_SMTP_TIMEOUT_S`, which must stay below the stop grace period.
  - `EARMARK_MAX_ATTACHMENT_MB`.
- **TLS and identity.**
  - `ssl.create_default_context()` for both `SMTP_SSL(context=…)` and
    `starttls(context=…)`, so the certificate and hostname are verified.
  - If STARTTLS is not offered, delivery fails. It never continues in plaintext.
  - AUTH happens only after TLS is up.
  - `local_hostname` is passed explicitly, set to the domain of `SMTP_FROM`, so EHLO never
    calls `socket.getfqdn()`.
  - The debug level is never set.
  - The envelope recipient is passed explicitly: the one validated `to`.
- **The message.**
  - `Message-ID` is deterministic per job, `<earmark.<job_id>@<from domain>>`, never
    `make_msgid()`.
  - The subject follows the contract, encoded by the email policy for non-ASCII text.
  - The body is earmark-generated only, as the contract says.
  - Both attachments are base64-encoded `text/markdown; charset=utf-8`, so the hash survives
    transport.
  - `EARMARK_MAX_ATTACHMENT_MB` caps the raw size of the two attachments together, before
    encoding. A job over it fails permanently with a message naming the setting. Nothing is
    truncated or split.
  - A title, guid or instructions text cannot inject email headers. Untrusted text never
    appears in the body.
- **Doctor.** `earmark doctor --online` connects, negotiates TLS, logs in and quits,
  sending nothing.
- **Tests.** They run against a hand-written SMTP stub on a socketpair or unix socket, after
  `tests/uds_http.py`, with sockets blocked (the stdlib `smtpd` is gone in 3.13), and assert
  on the exact bytes of the message, with an injected clock, `Message-ID` and MIME boundary.
  An opt-in test under a new `smtp` marker sends one message to an operator-supplied
  address; it never runs by default. Only the operator can check TLS against the real
  server, DMARC passing at the recipient, and the PKA reading both attachments.

### Delivery state, at-least-once (step 25)

- A migration adds delivery columns: state `sending`, with the `Message-ID` recorded before
  DATA, then `sent`, with a timestamp, after the 250 reply. They are written through
  lease-fenced updates.
- Delivery runs outside the worker's interruptible window. A SIGTERM waits for it, bounded
  by `EARMARK_SMTP_TIMEOUT_S`, which is kept below compose's `stop_grace_period` (2m today)
  and systemd's `TimeoutStopSec` (120 s for the worker unit today). A test pins that
  relation.
- A job found in `sending` after a crash or lease expiry is resent with the same
  `Message-ID` and `job_id`, and its push says "resent after an interruption".
- A `sent` job is never sent again, on retry, restart or lease expiry.
- So delivery is at-least-once: a crash after the server accepts the message but before
  `sent` is recorded cannot be told apart from a failure, and the PKA treats the same `guid`
  with the same `job_id` as a duplicate.

### Failure classes (step 25)

- A 4xx reply, or a connection error before DATA completes, is transient.
- A disconnect after DATA, with no reply, is ambiguous. It is retried, which is
  at-least-once.
- A 5xx reply is permanent.
- An authentication failure is permanent, with a message naming the settings.
- Job errors store the reply code, the enhanced status code and an earmark-written summary,
  never the server's reply text, which echoes addresses.

### The mail contract (pka-skills, `contract: 1`, 2026-11-23)

[verbatim: from "Status: **DRAFT**." to "Step 25 builds to this text." in PLAN.md lines
655–657]

[verbatim: PLAN.md lines 659–725, from "- **Both files**" to the last "PKA
de-duplication" bullet, the note-keys table included]

Notes for step 25 (not part of the contract):

- The subject carries the sanitised title, which is feed- or caller-supplied text, as the
  contract says; "What changes" item 3 is read with that.
- `earmark.sanitize.emit_frontmatter` writes `null` for `None` and refuses aware
  datetimes today, so it needs adapting to "the key is omitted" and RFC 3339 timestamps.
- Where the verbatim feed guid comes from is open (see "Open questions").

### Pushes after step 25

- The ntfy push says the transcript was sent, with the title and the masked recipient, or
  says the job failed and why. The step-24 leak rules apply.
- A job resent from `sending` says "resent after an interruption".
- There is no "analysis done" relay for now; ntfy stays for "sent" and "failed".

### GPU transcription in the image (step 26)

- **Goal.** The worker transcribes on the Spark's GB10 GPU in-process, without the vLLM
  service. Step 26 runs after step 25.
- **The GPU image** is a GPU build of the image, for example `Dockerfile.gpu` or a build
  target. It compiles CTranslate2 with CUDA for `sm_121` following
  `docs/decisions/2026-11-23-gpu-build-recipe.md`, with the earshot attribution comment.
  - It builds against the venv's Python 3.13 and swaps the built bindings into
    `/app/.venv`. The build stage has the Python 3.13 headers and pybind11, from a
    uv-managed 3.13 if the base ships an older Python.
  - The `CT2_VERSION` build argument defaults to the locked `ctranslate2` version (4.9.0
    today). `v4.6.0`, the version proven on the GB10, is the documented fallback if the
    Spark smoke fails.
  - A build-time check imports `ctranslate2` and `faster_whisper` and asserts the swapped
    version.
  - Nothing at runtime invokes `uv sync` or `uv run` without `--no-sync`, because either
    would put the locked PyPI wheel back. A static test pins this, and no `pip install`
    may pull a CPU `ctranslate2` over the build.
  - Static tests check the cmake flags from the recipe, `CMAKE_CUDA_ARCHITECTURES` taken
    from the build argument, the version default matching `uv.lock`, and the attribution
    comment.
  - The NGC base images are under NVIDIA's container licence: pulling them is fine, but a
    built image is not published without checking those terms.
- **The worker** runs `faster_whisper` with `EARMARK_ASR_DEVICE=cuda`. faster-whisper's
  batched pipeline is an option, `EARMARK_ASR_BATCH_SIZE`, with 0 or unset meaning off. The
  option feeds the transcript cache key. With `EARMARK_ASR_DEVICE=cuda` and no CUDA device,
  the worker refuses to start with a clear message, as it does today.
- **Compose.** The `asr` (vLLM) service and its `gpu` profile are removed, and replaced with
  the GPU image and `gpus: all`. The `remote` backend stays as the generic option for a
  server elsewhere.
- **Doctor** reports the CUDA device count and whether `float16` is among
  `ctranslate2.get_supported_compute_types("cuda")`.
- New settings go in `Settings`, `.env.example` and the README settings table.
- The CPU image still builds in CI, unchanged in behaviour.
- **Spark smoke**, the only real check of the GPU path, done by the operator: build;
  `ctranslate2.get_cuda_device_count() >= 1`, with `ctranslate2.__file__` inside the venv,
  checked after any uv command; `doctor`; transcribe a short public clip on `cuda` and see
  `float16` in the log.

### Inputs from the other agents

[verbatim: PLAN.md lines 729–734, the two bullets "- amap-spec: …" and "- earshot: …"]

### Open questions, owned by later steps

- **Step 24:** the length caps of `instructions` and `title`; the default of
  `EARMARK_MAX_UPLOAD_MB`; the synthetic show id; how the job stores the source, `to`,
  `instructions` and `title` (a migration on the new lineage); what becomes of today's
  `prompt` field; what the resolve stage does for uploads and direct URLs.
- **Step 25:**
  - The time zone: where the requester's offset comes from, the request (the Shortcut can
    send it), a re-added setting, or UTC. Today the job stores `shared_at` as a UTC
    instant, and step 22 removed `EARMARK_TZ` and `tzdata`.
  - The feed guid: the contract wants `feed:<feed guid verbatim>`, else `apple:<i= episode
    id>`. Today `Episode.guid` prefers Apple's `episodeGuid` over the matched feed item's
    GUID and falls back to `apple-podcasts:<episode_id>`; Apple's and the feed's GUIDs can
    differ (Known risk 2), and `Episode.guid` feeds the episode key.
  - The delivery stage and column names; the defaults of `EARMARK_SMTP_PORT`,
    `EARMARK_SMTP_TIMEOUT_S` and `EARMARK_MAX_ATTACHMENT_MB`; how paragraphs are merged
    within the 30–60 seconds or speaker-turn rule; the exact push wording.
  - The contract is final only once the operator approves the PKA's receiving procedure.
- **Step 26:** the base image, the slim `nvidia/cuda` devel and runtime pair (nobody has
  tried it) or the NGC base (earshot proved it), with the reason; whether CI builds the GPU
  image at all, given the free runner's roughly 14 GB of disk; whether it is a separate
  file or a build target.

### Not planned

`docs/PLAN.md` "Backlog" lists what the design leaves room for: a send-rate cap, re-shares
as a reference only, an "analysis done" push, speaker diarization, other sources, and an
async transcription API for other callers. None is designed here.
~~~

### 4.4 History: removed in step 22

`## History: removed in step 22`, the last section, with these facts (wording free; this
is the one place in the current-and-history text where removed things are named):

- Steps 01–21 built a pipeline that also analysed each transcript with Claude or a local
  LLM, wrote episode, transcript and entity notes into an Obsidian vault, indexed them in
  LanceDB for search, re-ran prompts on stored episodes, and wrote a weekly digest. It is
  tagged `v0.1.0-full-pipeline`; `git show v0.1.0-full-pipeline:docs/ARCHITECTURE.md`
  shows its design.
- Step 22 (`docs/steps/22-rescope-remove.md`) deleted: the `analyze/`, `vault/`, `index/`
  and `digest/` packages and `episodes.py`; the `anthropic`, `lancedb`, `fastembed` and
  `tzdata` dependencies and the `index` extra (PyYAML moved to the dev group); the routes
  `GET /search`, `GET /episodes` and `POST /episodes/{key}/analyze`; the commands
  `analyze`, `reanalyze`, `episodes`, `digest`, `entities rebuild`, `search` and
  `reindex`; the `reanalyze` job kind; the `llm` and `index` markers and workflows; the
  digest systemd service and timer; `docs/llm.md`; and the settings `VAULT_DIR`,
  `VAULT_SUBDIR`, `OBSIDIAN_VAULT_NAME`, `LLM_PROVIDER`, `STORE_BACKEND`, `INDEX_BACKEND`,
  `LLM_MODEL`, `LLM_EFFORT`, `LLM_FALLBACKS`, `LLM_API_KEY`, `LLM_CONTEXT_TOKENS`,
  `LLM_BASE_URL`, `LLM_TIMEOUT_SECONDS`, `LLM_RESPONSE_FORMAT`, `EMBED_MODEL`,
  `EMBED_MODEL_DIR` and `TZ`.
- `vault/sanitize.py` and the frontmatter emitter moved to `earmark/sanitize.py`, for ntfy
  and for step 25's note.
- `GET /episodes` was removed rather than kept, for the reasons in step 22's plan section
  5.7.
- `EARMARK_TZ` and `tzdata` went because only the vault and digest used them (step 22
  section 5.8); the time zone for the note is step 25's to settle.
- Left on disk by an old install and ignored: `episodes/<key>/analyses/`, `lancedb/`,
  `models/embed/`, `digests/`, `vault.lock` and `index.lock` under the data dir. Step 27's
  upgrade notes cover deleting them. The vault is never touched.
- `huggingface-hub` stays at 1.33.0 in `uv.lock`; the fastembed constraint that held it
  below 2.0 left with the `index` extra.
- The GitHub environment `llm` and its `ANTHROPIC_API_KEY` secret are no longer used by any
  workflow; deleting them happens outside the repo.
- Known risks 8–10 of v0.1.0 (local LLM context and Ollama's silent truncation, duplicate
  basenames in Obsidian, fastembed's hub pin) went with the code.

### 4.5 `CLAUDE.md`

Replace the whole file with exactly this text (between the `~~~~` lines):

~~~~markdown
# earmark

Self-hosted transcription and delivery service on the homelab's DGX Spark. An iOS
Shortcut sends it a podcast episode or a piece of audio, a worker transcribes it, and the
transcript goes to the operator's PKA instances, which do the analysis and filing, each
with its own context. earmark itself files nothing.

Today (after step 22) a request is an Apple Podcasts episode link. The worker resolves
the audio, transcribes it, caches the transcript, and sends an ntfy push. Steps 24–26 add
uploads and direct audio URLs with a destination address and instructions, delivery by
email as two Markdown attachments, and GPU transcription inside the image. Their
contracts are in "Planned (steps 24–26)" in `docs/ARCHITECTURE.md`.

## Status

Being built step by step from `docs/PLAN.md`. Check the status table there before
assuming a module exists.

- `docs/ARCHITECTURE.md`: the design. Decisions, module layout, contracts, data layout,
  config, security rules. This is the source of truth for how pieces fit; update it in
  the same change whenever a contract changes. Its "Planned (steps 24–26)" section holds
  what those steps build against, including the operator's decisions and the mail
  contract.
- `docs/PLAN.md`: the ordered steps, their acceptance criteria, and the build loop. Its
  "Re-scope" section is the record of the operator's decisions and the mail contract.
- `docs/steps/NN-<slug>.md`: the detailed plan written for each step when it is dispatched.

## Build loop

Each step runs as plan → implement → verify, with a separate agent per role:
`step-planner` (Opus), `step-implementer` (Sonnet), `step-verifier` (Sonnet), defined in
`.claude/agents/`. `/step NN` runs the loop from step NN onward (`--one` for a single
step); the protocol is in `docs/PLAN.md`. One commit per verified step on the `impl`
branch, pushed by `/step` once verified, with CI green before the next step starts; on
green, `main` is fast-forwarded to that commit. Do not push otherwise without being asked.

## Commands

```bash
uv sync                      # install (add --extra transcribe for local Whisper)
make check                   # the gate: ruff check + ruff format --check + mypy + pytest
uv run pytest tests/test_jobs.py -k claim     # one test
uv run pytest -m network     # opt-in tests (markers: network, slow, gpu); publishes to ntfy.sh
uv run earmark serve         # the API
uv run earmark worker        # job runner (--once to drain one job)
uv run earmark doctor        # check config and backends
```

`make check` must pass before a step is called done. Do not loosen lint, type, or test
configuration to make it pass.

## Shape of the code

```
POST /ingest ─► jobs table (SQLite) ─► worker ─► Pipeline
  resolve ─► fetch ─► transcribe ─► notify
```

Planned: `POST /transcriptions` replaces `POST /ingest` and also takes uploads and direct
audio URLs (step 24), and email delivery runs between transcribe and notify (step 25).

- Every stage is a `Protocol` in `earmark/pipeline/stages.py` with a real backend and a
  fake in `earmark/fakes.py`. Backends are chosen by config in
  `earmark/pipeline/factory.py`, the only place that constructs them.
- Every stage is idempotent and cache-aware: outputs live under
  `data/episodes/<episode_key>/`, and a stage whose output already exists returns it
  without redoing the work. Retries and re-shares rely on this.
- earmark is not a system of record. Its state is the job table and the transcript cache
  under `EARMARK_DATA_DIR`. A database from `v0.1.0-full-pipeline` is refused at startup,
  never migrated.

## Rules that are easy to get wrong

- **Tests run with no network, GPU, or credentials.** Sockets are blocked in the default
  suite. Use recorded fixtures, stubs on unix sockets, and the fakes; put anything live
  behind a marker.
- **Heavy dependencies are optional extras, imported lazily** (faster-whisper and
  CTranslate2, the `transcribe` extra). `import earmark` and the default test suite must
  work without them.
- **Errors carry retry intent.** Raise `TransientError` (retry with backoff),
  `PermanentError` (fail now), or `SkipEpisode` (e.g. subscriber-only episode, no public
  feed). The worker treats anything else as transient.
- **Feeds, enclosure URLs, titles, and transcripts are untrusted**, and from step 24 so
  are client-supplied URLs, uploads, their file names, titles, and instructions. All
  third-party fetches go through `earmark/fetch.py` (scheme check, private-address block,
  size cap); it is also the only path to a direct audio URL, and the resolver takes only
  Apple Podcasts episode links. Anything that becomes a file name or YAML goes through
  `earmark/sanitize.py`.
- **Never log or print a secret**: the API token, the ASR key, the ntfy URL and token,
  and from step 25 the SMTP password. `Settings` holds them as `SecretStr`.
- **Recipients and instructions must not leak** (step 24). Instructions never appear in
  logs or pushes. An address appears in logs and pushes only masked (`j…@example.org`).
  A direct URL appears in logs and pushes host-only.
- **The recipient allowlist is the gate** (step 24). Every `to` is checked against
  `EARMARK_ALLOWED_RECIPIENTS`. Unset refuses every request. `all` is the operator's
  written acceptance of a risk, warned about at startup and by doctor. earmark never
  defaults the recipient or the sender.
- **Mail follows the contract exactly** (step 25): pka-skills `contract: 1`, a draft,
  copied in `docs/ARCHITECTURE.md`. A change to a key, file name, format, or rule is a
  contract revision with a version bump, made in `docs/PLAN.md` and
  `docs/ARCHITECTURE.md` together, never a quiet edit. Delivery is at-least-once: a
  resend keeps its `Message-ID` and `job_id`.
- **Untrusted text in mail** (step 25). The body carries only text earmark generates.
  The subject carries the sanitised title, and the body says that the subject and the
  note contain third-party text. A title, guid, or instructions text can never inject a
  header. In the email, the requester's instructions appear only in the instructions
  attachment, verbatim, and the guid appears only inside the attachments.
- **Never truncate a transcript.** A transcript over the attachment cap fails the job
  with a message naming the setting; nothing is cut or split.
- **Removed code stays removed.** What step 22 deleted is recoverable from the
  `v0.1.0-full-pipeline` tag. Bring nothing back without a plan step.

## Environment

This sandbox is aarch64 Linux with Python 3.13 and uv, and has no GPU, Docker, or ffmpeg.
The deployment target is a homelab with an NVIDIA GPU (DGX Spark, also aarch64). GPU
transcription and container builds cannot be verified here; say so rather than claiming
they work. The GPU image of step 26 is checked only by the operator's smoke test on the
Spark.
~~~~

What changed against the current file, for the verifier:

- **Opening description**: rewritten for the transcription service, with today's flow and a
  pointer to the planned steps.
- **Status**: the stale "Until step 01 lands…" sentence is gone, and both bullets now point
  at where the decisions and the contract live.
- **Commands**: no `--extra index`; markers `network, slow, gpu`; a warning that
  `-m network` publishes to ntfy.sh.
- **Shape of the code**: the four-stage pipeline, a pointer to the planned changes, and
  "not a system of record" in place of the vault rule.
- **Rules**:
  - removed: the vault-writing clauses, "LLM keys", "Chunk it", and the Claude-SDK rule;
  - changed: extras, untrusted input, secrets, truncation;
  - added: leaks, allowlist, mail contract, untrusted text in mail, removed code.
- **Environment**: one sentence about step 26.

## 5. Behaviour and edge cases

- **The current part must pass the two mechanical checks of section 7.2.** The removed
  check is per chunk: a paragraph, bullet, numbered item, table row, heading or fence line,
  with continuation lines joined. "analysis" is allowed only in a chunk that also says
  "PKA". The planned check: a chunk with a planned term must also say "step 24", "step 25",
  "step 26" (or "steps 24–26"). Since "PKA" is a planned term, the introduction's PKA
  sentence needs its step pointer in the same paragraph.
- **Kept text sometimes needs a pointer.** For example, the Transcription row of Decisions
  says "step 26 plans" the CUDA build. That is a pointer and passes, because the row names
  the step.
- **Verbatim means verbatim.** Copy from `docs/PLAN.md` at this commit. Keep the inline
  backticks, the bold markers, the em dash in `# <show> — <title>`, the en dash in
  `30–60 seconds`, and the `…` characters. The table inside the contract keeps its header
  and separator rows.
- **Notes after verbatim bullets** go after the bullet's last sentence, never inside it.
- **Open is open.** If the implementer finds a contract detail that the PLAN does not
  state, it goes under "Open questions" with its step, or is left out. It is never given a
  value.
- **The Python fence** under Stage contracts is the only Python fence in ARCHITECTURE. It
  must stay ruff-clean. Data layout, Flow and CLAUDE.md's diagrams use plain fences.
- **No new facts about code.** Every current-part statement must be true of `HEAD`. Where
  this plan says "keep", the existing text is already true; where it gives new text, the
  facts come from section 2.1. If the implementer finds a statement this plan keeps that is
  false of the code, it stops and reports.
- **Section names other docs use**: "Data layout" and "HTTP API" (README). Do not rename
  them.

## 6. Tests

No test file changes. The acceptance criteria are checked by `make check` and the scripts
in section 7.

### 6.1 Acceptance criteria

| # | Criterion (brief) | Proof |
|---|---|---|
| A1 | Neither file describes removed behaviour as current; the planner lists the grep | Section 7.2: `current.sh` prints `0 problem(s)` and exits 0. It covers the current part of ARCHITECTURE (removed terms and unmarked planned terms) and all of `CLAUDE.md` (removed terms). Then the verifier reads the History section and the Planned section once, to confirm neither presents removed behaviour as current |
| A2 | Each decision in "Re-scope" appears in `docs/ARCHITECTURE.md` | Section 7.3: `verbatim.sh` reports 4/4, 10/10, 1/1, 48/48 and 2/2 and exits 0. Then the checklist of section 6.2, row by row |
| A3 | `make check` passes, since the doc tests still apply | Section 7.1 |

### 6.2 Checklist: every Re-scope item and brief contract, and where it lands

The verifier marks each row with the ARCHITECTURE line number where it found the item.
"Verbatim" rows are also proven by `verbatim.sh`. Abbreviations: **P** is "Planned (steps
24–26)", **C** is the current part.

**Re-scope "What changes" and decisions** (PLAN 609–653)

| ID | Item | Where |
|---|---|---|
| W0 | earmark stops analysing and storing; the PKAs analyse and file | P › What changes (lead sentence); C › introduction and Decisions › Scope |
| W1 | Request: audio file, direct audio URL, or Apple link, plus destination address, instructions, optional title | P › What changes (verbatim); P › Request model |
| W2 | Transcribe on the Spark's GPU | P › What changes; P › GPU transcription in the image |
| W3 | Two Markdown attachments; body and subject earmark-generated | P › What changes; P › The mail contract and its notes; P › Delivery › The message |
| W4 | ntfy push "sent" or why it failed | P › What changes; P › Pushes after step 25 |
| D1 | Not part of AMAP; ordinary email; DMARC-passing allowlisted sender; mailbox on the operator's domain; placeholder `earmark@example.org`; sender never defaulted | P › Operator decisions (verbatim); P › Delivery › Ordinary email and SMTP settings; P › Recipients allowlist (last bullet) |
| D2 | Allowlist, `all`, accepted risk, unset refuses all, caller chooses, never defaults `to` | P › Operator decisions (verbatim); P › Recipients allowlist |
| D3 | At-least-once; same `Message-ID` and `job_id`; PKA dedupe | P › Operator decisions (verbatim); P › Delivery state; contract › PKA de-duplication |
| D4 | Re-shares resend the whole transcript; new `job_id`, same `guid`; send-only, reads no mail | P › Operator decisions (verbatim); P › De-duplication and retention; P › Delivery › Ordinary email |
| D5 | Direct URLs and uploads identified by content; transcription cached by hash; podcasts keep identity and audio cache | P › Operator decisions (verbatim); P › Source identity and caching |
| D6 | Old databases refused, not migrated | P › Operator decisions (verbatim, with the step-22 note); C › Jobs › Database identity |
| D7 | No "analysis done" relay for now | P › Operator decisions (verbatim); P › Pushes after step 25 |
| D8 | No PKA-side skill needed from earmark | P › Operator decisions (verbatim) |
| D9 | Attachment size cap configurable | P › Operator decisions (verbatim); P › Delivery › SMTP settings and The message (`EARMARK_MAX_ATTACHMENT_MB`) |
| D10 | Removed code deleted, recoverable from the tag | P › Operator decisions (verbatim, with the note); History |

**Mail contract and inputs** (PLAN 655–734)

| ID | Item | Where |
|---|---|---|
| C0 | `contract: 1`, DRAFT, final on the operator's approval, revisions bump the version | P › The mail contract (heading and verbatim status); P › Open questions (step 25, last bullet) |
| C1 | Both files: encoding, frontmatter, strings, unknown values, formats, filenames, filename date | P › The mail contract (verbatim) |
| C2 | Note keys table, 16 keys | P › The mail contract (verbatim) |
| C3 | Note body, `transcript_sha256` scope, no audio hash for podcasts | P › The mail contract (verbatim) |
| C4 | Instructions file: keys, body, always sent | P › The mail contract (verbatim) |
| C5 | Subject and body; the guid out of the body | P › The mail contract (verbatim); its notes |
| C6 | PKA de-duplication, three rules | P › The mail contract (verbatim) |
| I1 | amap-spec: no receipt, no dedupe, per-deployment caps | P › Inputs (verbatim); P › Delivery › Ordinary email |
| I2 | earshot: not an API; the recipe document | P › Inputs (verbatim); P › GPU transcription in the image |

**The step 23 brief's list for "Planned"** (PLAN 827–838)

| ID | Item | Where |
|---|---|---|
| B1 | The request model | P › Request model |
| B2 | Source identity and caching | P › Source identity and caching |
| B3 | The at-least-once delivery state machine | P › Delivery state, at-least-once; P › Failure classes |
| B4 | The mail format | P › The mail contract; P › Delivery › The message |
| B5 | The allowlist | P › Recipients allowlist |
| B6 | The four leak rules of step 24 | P › Leak rules and the API view model (the four bullets, as in PLAN 932–939) |
| B7 | Security: the guarded fetch is the only path to a direct URL | P › Security for client-supplied input; C › Security › Ingest (pointer) |
| B8 | Security: the resolver accepts only Apple Podcasts episode links | C › Security › Ingest; P › Security for client-supplied input |
| B9 | Security: recipients and instructions are data that must not leak | P › Security for client-supplied input; P › Leak rules |
| B10 | `CLAUDE.md`: opening, shape, stage list, rules (vault and Claude-SDK rules out; mail contract, untrusted text in mail, allowlist in) | `CLAUDE.md` equals section 4.5 (section 7.5) |

**Brief 24 contract items** (PLAN 856–940). Each is in P › Request model, Source identity,
Recipients allowlist, De-duplication and retention, Leak rules, or Security for
client-supplied input:

- the route and both body forms;
- `python-multipart`;
- `url` routing;
- `to` and the matching rules;
- the `instructions` and `title` caps;
- the upload staging rules;
- the API audio check;
- identity and the `Source` values;
- the cache key;
- retention;
- the dedupe window and its default;
- the job's stored fields;
- the view model;
- the CLI flags;
- the 422 and 413 cases;
- auth before body;
- `all` logged and reported.

Not carried, because it is a build instruction: the `docs/shortcut.md` update.

**Brief 25 contract items** (PLAN 942–1059), in P › Delivery, Delivery state, Failure
classes, Pushes, and the mail contract notes:

- the dispatch gate;
- the mail backend and its `ConfigError`;
- the renderer's coverage list;
- the time zone (open);
- the seven SMTP settings;
- the six TLS and identity rules;
- the message rules;
- the size cap;
- the state columns and resend rules;
- the SIGTERM bound;
- the five failure classes;
- doctor `--online`;
- the push;
- the test harness;
- the `smtp` marker;
- the operator-only checks.

**Brief 26 contract items** (PLAN 1061–1119), in P › GPU transcription in the image and
Open questions:

- the GPU image and the recipe;
- the attribution;
- the venv Python and the swap;
- the `CT2_VERSION` default and fallback;
- the headers and pybind11;
- the build-time check;
- no runtime `uv sync` or `uv run` without `--no-sync`;
- the static tests;
- the worker on `cuda`;
- `EARMARK_ASR_BATCH_SIZE`;
- the compose change;
- `remote` kept;
- the doctor report;
- the CPU image unchanged;
- the Spark smoke;
- the CUDA refusal kept;
- the open base image and CI build.

Not carried, because they are build instructions: "update the tests and the workflow that
pin the old service", "run this step after step 25" (kept only as a sentence of the
Goal).

**Step 22 fold-ins** (step 22 plan, Deviation 1), all in C

| ID | Item | Where |
|---|---|---|
| S1 | Four-stage `Backends` | Stage contracts (Python block); Runner and worker › Backends |
| S2 | `JobResult(duration_s)`, `JobKind = "ingest"` | Domain models › JobResult, Job |
| S3 | The `Stage` literal | Domain models › Job; Runner and worker › Order |
| S4 | `application_id` lineage and refusal rules | Jobs › Database identity; Doctor (database check) |
| S5 | Removed routes and commands; `GET /episodes` removed | HTTP API (four rows); History |
| S6 | `earmark.sanitize` | Module layout; Security › Untrusted text to names and YAML; History |
| S7 | The interim push wording | Notifying › Content; Decisions › Notification |
| S8 | The removed settings, including `EARMARK_TZ` | Configuration (27 rows); History |
| S9 | The `slow` marker also covers the library-logging tests | Testing › `slow` |

**Open items** (owned by later steps), in P › Open questions:

| ID | Item | Owner |
|---|---|---|
| O1 | Time zone source | 25 |
| O2 | The verbatim feed guid versus today's `Episode.guid` | 25 |
| O3 | Caps, defaults, synthetic show id, job storage, `prompt`, the resolve stage for new sources | 24 |
| O4 | Delivery stage and column names; SMTP and attachment defaults; paragraph merge; push wording; contract finality | 25 |
| O5 | GPU base image; CI build of the GPU image; file or build target | 26 |

### 6.3 Criteria added by the planner

| # | Criterion | Proof |
|---|---|---|
| P1 | The structure is exactly section 4.1, so every reference resolves | `facts.sh` (section 7.4): heading lists |
| P2 | The Configuration table lists exactly the 27 `Settings` fields | `facts.sh` |
| P3 | The Module layout names every module under `src/earmark/` | `facts.sh` |
| P4 | The HTTP API table lists the four routes; the current part names all eight commands | `facts.sh` |
| P5 | The doc-test strings and the lineage id are present in the current part | `facts.sh` (`HF_HUB_DISABLE_TELEMETRY=1`, `=0`, `0x45524D4B`) |
| P6 | `CLAUDE.md` is exactly the text of section 4.5 | Section 7.5 |
| P7 | Only `docs/ARCHITECTURE.md`, `CLAUDE.md` and this plan change | Section 7.6 |
| P8 | The current part makes no false claim about the code | Section 7.7 spot checks |

No fixtures are needed.

## 7. Verification

Run everything from the repo root. The scripts write nothing.

### 7.1 The gate

```bash
cd /home/sandy/dev/earmark
make check
GITHUB_ACTIONS=true make check
```

### 7.2 Removed and planned behaviour in the current part (`current.sh`)

Save as a file in a scratch directory and run with `bash <file>`. It must print
`0 problem(s)` and exit 0.

```bash
cd /home/sandy/dev/earmark && uv run --locked python - "$@" <<'EOF'
import re
import sys
from pathlib import Path

REMOVED = (
    r"anthropic|\bclaude\b|lancedb|fastembed|obsidian|vault|reanaly[sz]|weekly digest|"
    r"digest note|earmark digest|digests/|Digest[A-Z]|DIGEST_|entities rebuild|Entities/|"
    r"entity (key|id|note|registry|link)|aliases|embedding|embedder|embed_model|\bllm|llm_|"
    r"map.?reduce|/search\b|(GET|POST) /episodes|/episodes/\{|--extra index|"
    r"index extra|vector|search index|index_backend|store_backend|EARMARK_TZ|`TZ`|tzdata|"
    r"zoneinfo|note_path|StoreResult|\bAnalyzer\b|\bIndexer\b|\bSearcher\b|\bThemer\b|"
    r"NeedsChunking|prompt_hash|research note|knowledge|wikilink|managed region|Note ready"
)
ANALYSIS = re.compile(r"analy[sz]", re.I)  # allowed only where the PKA does it
PLANNED = re.compile(
    r"POST /transcriptions|ALLOWED_RECIPIENTS|SMTP|Mailer|mail|instructions|DEDUPE_WINDOW|"
    r"EARMARK_MAX_UPLOAD_MB|\bMAX_UPLOAD_MB|MAX_ATTACHMENT_MB|ASR_BATCH_SIZE|Dockerfile\.gpu|"
    r"python-multipart|data/uploads|Message-ID|\bpka\b|contract: 1",
    re.I,
)
POINTER = re.compile(r"steps? 2[4-6]|Planned \(steps 24–26\)", re.I)
START = re.compile(r"\s*(?:$|#|\||- |\d+\. |```)")
HEAD = "\n## Planned (steps 24–26)\n"


def chunks(lines: list[str]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for n, line in enumerate(lines, 1):
        if START.match(line) or not out:
            out.append((n, line))
        else:
            out[-1] = (out[-1][0], out[-1][1] + " " + line.strip())
    return out


def removed(name: str, lines: list[str], pattern: re.Pattern[str]) -> int:
    bad = 0
    for n, chunk in chunks(lines):
        if pattern.search(chunk) or (ANALYSIS.search(chunk) and "PKA" not in chunk):
            bad += 1
            print(f"{name}: removed behaviour, from line {n}: {chunk.strip()[:110]}")
    return bad


arch_path = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/ARCHITECTURE.md")
claude_path = Path(sys.argv[2] if len(sys.argv) > 2 else "CLAUDE.md")
text = arch_path.read_text()
if HEAD not in text:
    sys.exit("no '## Planned (steps 24–26)' section")
current = text.split(HEAD)[0].splitlines()
bad = removed("ARCHITECTURE", current, re.compile(REMOVED, re.I))
for n, chunk in chunks(current):
    if PLANNED.search(chunk) and not POINTER.search(chunk):
        bad += 1
        print(f"ARCHITECTURE: planned behaviour without a step pointer, from line {n}: "
              f"{chunk.strip()[:90]}")
claude_removed = re.compile(REMOVED.replace(r"\bclaude\b|", "claude-api|claude-opus|"), re.I)
bad += removed("CLAUDE.md", claude_path.read_text().splitlines(), claude_removed)
print(f"{bad} problem(s)")
sys.exit(1 if bad else 0)
EOF
```

Then a plain grep over the two parts the script skips, read by eye. Every hit must be a
statement about history (in History) or about the PKA or a planned contract (in Planned),
never a claim that earmark does it now:

```bash
cd /home/sandy/dev/earmark
awk '/^## Planned \(steps 24–26\)$/{p=1} p' docs/ARCHITECTURE.md \
  | grep -n -i -E 'vault|obsidian|anthropic|claude|lancedb|fastembed|digest|reanaly|analy[sz]|llm|search'
```

### 7.3 The verbatim carry (`verbatim.sh`)

It must report 4/4, 10/10, 1/1, 48/48 and 2/2 and exit 0. Run against `docs/PLAN.md` as
a self-check, it reports the same; that shows the extraction works.

```bash
cd /home/sandy/dev/earmark && uv run --locked python - "${1:-docs/ARCHITECTURE.md}" <<'EOF'
import re
import sys
from pathlib import Path

plan = Path("docs/PLAN.md").read_text().splitlines()
arch = " ".join(Path(sys.argv[1]).read_text().split())


def block(start: str, stop: str) -> list[str]:
    i = next(n for n, line in enumerate(plan) if line.lstrip().startswith(start))
    j = next(n for n in range(i + 1, len(plan)) if plan[n].lstrip().startswith(stop))
    return plan[i:j]


def items(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = re.match(r"(?:- |\d+\. )(.*)", s)
        if m or s.startswith("|") or not out:
            out.append(m[1] if m else s)
        else:
            out[-1] += " " + s
    return [" ".join(i.split()) for i in out]


blocks = {
    "what changes": block("1. Accept a request", "**Decisions"),
    "decisions": block("- **Not part of AMAP.**", "**The mail contract"),
    "contract status": [
        line.split("Status: ", 1)[1] if "Status: " in line else line
        for line in block("**The mail contract", "- **Both files**")
    ],
    "mail contract": block("- **Both files**", "**Inputs from the other agents"),
    "inputs": block("- amap-spec:", "## Step 22"),
}
missing = 0
for name, lines in blocks.items():
    its = items(lines)
    bad = [i for i in its if i not in arch]
    missing += len(bad)
    print(f"{name}: {len(its) - len(bad)}/{len(its)} present")
    for i in bad:
        print("   MISSING:", i[:110])
sys.exit(1 if missing else 0)
EOF
```

The verifier also checks by eye that the verbatim blocks sit under the right Planned
subsections, not elsewhere.

### 7.4 Structure and facts (`facts.sh`)

It must print `facts ok` and exit 0.

```bash
cd /home/sandy/dev/earmark && uv run --locked python - <<'EOF'
import re
import sys
from pathlib import Path

from earmark.cli import app
from earmark.config import Settings

text = Path("docs/ARCHITECTURE.md").read_text()
problems: list[str] = []
H2 = [
    "Decisions", "Flow", "Module layout", "Domain models", "Stage contracts", "Jobs",
    "Data layout", "HTTP API", "Configuration", "Deployment", "Security", "Testing",
    "Known risks", "Planned (steps 24–26)", "History: removed in step 22",
]
H3 = [
    "Runner and worker", "Fetching", "Resolving", "Transcribing", "Notifying", "Doctor",
    "Database identity", "Continuous integration",
    "What changes", "Operator decisions (2026-11-23)", "Request model (step 24)",
    "Source identity and caching (step 24)", "Recipients allowlist (step 24)",
    "De-duplication and retention (step 24)", "Leak rules and the API view model (step 24)",
    "Security for client-supplied input (step 24)", "Delivery (step 25)",
    "Delivery state, at-least-once (step 25)", "Failure classes (step 25)",
    "The mail contract (pka-skills, `contract: 1`, 2026-11-23)", "Pushes after step 25",
    "GPU transcription in the image (step 26)", "Inputs from the other agents",
    "Open questions, owned by later steps", "Not planned",
]
outside = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
h2 = re.findall(r"^## (.+)$", outside, flags=re.M)
h3 = re.findall(r"^### (.+)$", outside, flags=re.M)
if h2 != H2:
    problems.append(f"## headings are {h2}")
if h3 != H3:
    problems.append(f"### headings are {h3}")


def section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## )", text, flags=re.M | re.S)
    return m[1] if m else ""


rows = [ln for ln in section("Configuration").splitlines() if ln.startswith("| `")]
listed = {n for r in rows for n in re.findall(r"`([A-Z][A-Z0-9_]*)`", r.split("|")[1])}
fields = {f.upper() for f in Settings.model_fields}
if listed != fields:
    problems.append(f"config table: missing {sorted(fields - listed)}, extra {sorted(listed - fields)}")
layout = section("Module layout")
for p in sorted(Path("src/earmark").rglob("*.py")):
    if p.name != "__init__.py" and p.name not in layout:
        problems.append(f"module layout does not name {p.relative_to('src')}")
api = section("HTTP API")
for route in ("`POST /ingest`", "`GET /jobs/{id}`", "`GET /jobs`", "`GET`, `HEAD /healthz`"):
    if route not in api:
        problems.append(f"HTTP API table does not list {route}")
current = text.split("\n## Planned (steps 24–26)\n")[0]
for c in sorted(c.name for c in app.registered_commands if c.name):
    if f"earmark {c}" not in current:
        problems.append(f"the current part never names `earmark {c}`")
for needle in ("HF_HUB_DISABLE_TELEMETRY=1", "HF_HUB_DISABLE_TELEMETRY=0", "0x45524D4B"):
    if needle not in current:
        problems.append(f"the current part lacks {needle}")
print("\n".join(problems) or "facts ok")
sys.exit(1 if problems else 0)
EOF
```

The HTTP API check also confirms that no other route row exists: the verifier counts the
table rows (exactly four).

### 7.5 `CLAUDE.md` is the planned text

Extract the text between the `~~~~markdown` and `~~~~` lines of section 4.5 into a scratch
file, then `diff` it against `CLAUDE.md`. No output means it matches. The plan path is
`docs/steps/23-rescope-architecture.md`.

```bash
cd /home/sandy/dev/earmark
awk '/^~~~~markdown$/{p=1; next} /^~~~~$/{p=0} p' docs/steps/23-rescope-architecture.md \
  | diff - CLAUDE.md
```

### 7.6 Scope

```bash
cd /home/sandy/dev/earmark
git status --porcelain
```

It shows only `CLAUDE.md` and `docs/ARCHITECTURE.md` modified, and this plan added. `git
diff --stat -- src tests .github deploy compose.yaml Dockerfile pyproject.toml uv.lock
docs/PLAN.md` prints nothing.

### 7.7 Spot checks against the code

The verifier picks at least these and confirms each in the source:

- the identity table and both messages (`src/earmark/db.py`);
- the push titles and lines (`src/earmark/notify/ntfy.py`, `compose`);
- the doctor check names and order (`src/earmark/doctor/checks.py`, `run`);
- the runner order and the stage at the end of a fresh and a cached job
  (`src/earmark/pipeline/runner.py`);
- the compose environment keys and volumes (`compose.yaml`);
- the systemd `ReadWritePaths` and `TimeoutStopSec` values;
- the `QUIET_LOGGERS` list (`src/earmark/log.py`);
- the three workflow files and the slow job's commands.

### 7.8 What cannot be verified here

Nothing in this step runs outside the sandbox. No live smoke applies: no behaviour
changes.

## 8. Deviations

1. **Client-supplied-input security lives in Planned.** The brief says to rewrite the
   security section for client-supplied URLs and uploads. Those inputs do not exist until
   step 24, and the orchestrator asked for current behaviour and planned contracts to be
   kept apart. So the Security section states the current rules (the resolver accepts only
   Apple links, the guarded fetch is the only fetch path) and points to "Security for
   client-supplied input (step 24)" in Planned, which holds the rest.
2. **A "History: removed in step 22" section is added.** The brief does not ask for one.
   It keeps every mention of removed things out of the current part, so the grep of
   section 7.2 can be strict (zero hits) instead of relying on judgement.
3. **The Re-scope text is duplicated, verbatim.** "What changes", the decisions, the mail
   contract and the inputs appear in both `docs/PLAN.md` and `docs/ARCHITECTURE.md`.
   Verbatim copying is the only way to carry them "faithfully" in a checkable way. The new
   `CLAUDE.md` rule says a contract revision changes both files together.
4. **The subject wording.** "What changes" item 3 and the contract disagree on whether the
   subject carries only earmark text (section 2.3). Both are copied as written, and a
   note under the contract follows the contract. That is not a new product decision; the
   operator may want PLAN's item 3 reworded.
5. **Delivery sits before completion.** The Planned section says delivery runs after
   transcription and before the job completes. The PLAN does not say so in one sentence,
   but step 25's resend rules ("a `sent` job is never sent again on retry, restart or lease
   expiry") and the "sent" push require it. The stage name is left to step 25.
6. **Small `CLAUDE.md` edits beyond the brief's list:** the stale "Until step 01 lands"
   sentence, the extras and markers in Commands, a note that `-m network` publishes to
   ntfy.sh, and one Environment sentence about step 26. They fix text that was false
   after step 22.

No contract changes, so `docs/ARCHITECTURE.md` gets no edits beyond this rewrite. No
dependency is added.

## 9. Out of scope

- **Steps 24–26:** every planned behaviour, and settling the open questions.
- **Step 27:**
  - the README (its quickstart still sets `EARMARK_VAULT_DIR`, `EARMARK_LLM_*` and a
    `prompt`);
  - `docs/deploy.md`, `docs/shortcut.md` and `docs/runbook-spark.md`;
  - the upgrade notes.
- **`docs/asr.md`:** it still describes the vLLM path, which is current until step 26.
- **The orchestrator:**
  - the PLAN status table;
  - whether to ask the operator to reword "What changes" item 3;
  - the `.claude/agents/` definitions. They name no removed module.
