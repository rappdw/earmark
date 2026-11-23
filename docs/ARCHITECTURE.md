# Architecture

earmark is a self-hosted transcription and delivery service. A request (step 24) carries an
Apple Podcasts episode link, a direct audio URL, or an uploaded audio file, plus a
destination address checked against an allowlist, free-form instructions, and optionally a
title. The worker resolves or downloads the audio and transcribes it, then caches the
transcript. It emails the transcript note and the instructions to the destination as two
Markdown attachments, in the mail contract's format (step 25). Last, it sends an ntfy push.
The operator's PKA instances do the analysis and filing.

Step 26 adds GPU transcription in the image. Its contract is in "Planned (step 26)".

How to read this document:

- The sections before "Planned (step 26)" describe the code as it is.
- "Planned (step 26)" is what step 26 builds against.
- "History: removed in step 22" lists what step 22 removed.

When a step changes a contract described here, it changes this file in the same commit.
Steps 01–21 built a larger pipeline, tagged `v0.1.0-full-pipeline`.

## Decisions

The original sketch left several choices open. These are the defaults the plan builds
on. Each is isolated behind config or a stage interface, so reversing one touches a
backend, not the pipeline.

| Question | Decision | How to change it |
|---|---|---|
| Scope | earmark transcribes, and delivers the transcript by email (step 25). The operator's PKA instances do the analysis and filing. | See "Re-scope decisions" and "Delivering". |
| State | The job table (SQLite, WAL), including each job's delivery state (step 25). The per-episode cache under `DATA_DIR` (`episode.json`, `shared.json`, `transcript.json`, `transcript.txt`, podcast audio while it is needed). Per-job staging files for uploads and direct URLs (`uploads/`, `downloads/`, deleted when the job ends; step 24). earmark is not a system of record. A database from earlier than step 22 is refused, not migrated (see "Database identity"). | A new migration in `db.py`. |
| Delivery | Ordinary email over SMTP submission (stdlib `smtplib`, verified TLS, AUTH) from a sending mailbox on the operator's domain. It carries two Markdown attachments in the pka-skills mail contract (`contract: 1`). Delivery is at-least-once: a resend keeps the `Message-ID` and `job_id` (step 25). | Implement another `Mailer`. |
| Notification | ntfy, off unless `EARMARK_NTFY_URL` is set: one JSON publish to the server root per finished job, with no link (steps 15, 22, 24, 25). A delivered job's push reads `Sent: <label>`, with the masked recipient and the duration. Works with public ntfy.sh or a self-hosted server on the LAN or tailnet. The Shortcut can also poll `GET /jobs/{id}`. | Implement another `Notifier`. |
| Requests | One route, `POST /transcriptions`, taking JSON or a streamed multipart upload; every request names one `to` from `EARMARK_ALLOWED_RECIPIENTS` (step 24). | See "HTTP API" and `docs/steps/24-transcription-requests.md`. |
| Queue | A SQLite job table and a separate worker process. No Redis. | Swap `JobStore`. |
| Transcription | faster-whisper `large-v3` in the worker process (the `transcribe` extra), with a remote OpenAI-compatible ASR endpoint as the alternative (`remote`, step 09). The PyPI CTranslate2 wheel for linux/aarch64 is CPU-only (step 08), so on the DGX Spark GPU transcription means a remote server on the Spark (recommended: vLLM serving Whisper; see `docs/asr.md`) or a self-built CUDA CTranslate2, which step 26 plans inside the image. | `EARMARK_ASR_BACKEND`, `EARMARK_ASR_MODEL`, `EARMARK_ASR_BASE_URL`. |
| Reachability | Private network only: the API is never exposed publicly. It listens on `127.0.0.1:8787` (compose publishes it only there). A reverse proxy on the host's LAN interface (Caddy, `deploy/caddy/Caddyfile`) terminates TLS for a name such as `earmark.example.com`. That name is a DNS-only (never proxied) Cloudflare `A` record for the server's private address, with a Let's Encrypt certificate from the DNS-01 challenge; the Cloudflare token lives only with the proxy. Away from home the phone and Mac reach that address through a WireGuard split tunnel. Tailscale Serve is the alternative; `tailscale funnel` is never used (step 16). | See `docs/deploy.md` (step 16). |
| Deployment | One container image (CPU, extras by build argument, `transcribe` by default) running either `earmark serve` or `earmark worker`; `compose.yaml` with a shared data volume; GPU transcription through a vLLM Whisper service in the compose `gpu` profile and the `remote` ASR backend; systemd units for the API and the worker (steps 16, 22). | See "Deployment" below and `docs/deploy.md`. |
| Language and tooling | Python 3.13 (`requires-python = ">=3.13,<3.14"`, chosen in step 01 from wheel availability on aarch64), uv with the `uv_build` backend, FastAPI, pydantic, httpx2 (the maintained successor of httpx, imported as `httpx2`) with its httpcore2 for the guarded network backend (step 06), typer, stdlib `sqlite3`, defusedxml for RSS (step 07), python-multipart's streaming `MultipartParser` for uploads (step 24; Starlette's own form parser spools files to the system temp dir, so it is not used), the stdlib `email`, `smtplib` and `ssl` for delivery (step 25). PyYAML is a dev dependency only: tests parse the workflows and `compose.yaml` with it (step 22). Sync code throughout the worker. | Widen `requires-python` once every heavy extra ships wheels for the new version on linux/aarch64; `uv lock` enforces this through `tool.uv.required-environments`. |

## Flow

```
iPhone Shortcut, curl, `earmark submit`
   │  POST /transcriptions   Authorization: Bearer <token>
   │    JSON {url, to, instructions?, title?, timestamp?}
   │    or multipart: file + to, instructions?, title?, timestamp?  (streamed to data/uploads/)
   ▼
API (FastAPI) ── validates, checks `to`, enqueues, returns 202 {job_id} ──► jobs table (SQLite, WAL)
                                                                              │ claim
                                                                              ▼
Worker ── Pipeline.run(job)
   resolve     apple_podcasts: Apple URL → iTunes Lookup (or the episode page) → RSS → Episode
               upload: the staged file's sha256 → Episode (key = file_key of the bytes)
               url: download (guarded fetch) → data/downloads/<job_id>.<ext> → sha256 → Episode
   fetch       podcasts only: enclosure → data/episodes/<key>/audio.<ext>
   transcribe  audio → transcript.json + transcript.txt (skipped when cached with this ASR profile)
   deliver     render the note and instructions (mail contract) → MIME message (inside the
               interruptible window); then, outside it: delivery_state=sending → SMTP →
               delivery_state=sent + done with JobResult(duration_s), in one write
   finish      delete upload/download staging files; notify via ntfy (never fails the job)
```

The API handler makes no outbound network request. It validates, streams an upload to disk,
and enqueues, so the Shortcut gets its 202 as soon as the body has arrived.

`to`, `instructions` and `title` are stored with the job (step 24), and so is the offset of
the request's `timestamp` (step 25). `to` and `instructions` are part of the dedupe key.
After enqueue, only delivery reads `to` and `instructions` (step 25). `title` names uploads
and direct URLs, and is ignored for podcasts.

## Module layout

```
src/earmark/
  __init__.py          __version__ only; importing earmark loads nothing heavy
  __main__.py          `python -m earmark`
  config.py            Settings (pydantic-settings, EARMARK_* env, .env), load_settings, ConfigError
  log.py               configure_logging: stderr handler with a secret-redacting, URL-scrubbing
                       formatter and a wire-logger gate; scrub_urls; adopt_library_loggers
  models.py            domain models
  errors.py            TransientError, PermanentError, SkipEpisode; MaybeDelivered (a
                       TransientError: the SMTP outcome is unknown; step 25)
  artifacts.py         ArtifactStore: per-episode cache paths, atomic writes
  db.py                SQLite connection, migrations (PRAGMA user_version), and the lineage
                       check (PRAGMA application_id, step 22)
  jobs.py              JobStore
  fetch.py             GuardedFetcher: guarded GET and streaming download, address policy,
                       pinned-address network backend (step 06)
  audio.py             HttpAudioFetcher, the `http` AudioFetcher backend (step 06)
  submission.py        Intake: recipient check, URL classification, enqueue (API and CLI; step 24)
  recipients.py        normalize_address, mask_address, RecipientPolicy, RecipientRefused;
                       standard library only (step 24)
  sources.py           parse_source_url (Apple link or direct URL), display_url, upload_extension,
                       clean_upload_name, audio_title, sha256_file, audio_episode (step 24)
  staging.py           Staging: data/uploads/ (UploadWriter) and data/downloads/, release at a
                       terminal state, startup sweeps (step 24)
  sanitize.py          untrusted text to file names and YAML, fit_chars, and the frontmatter
                       emitter; standard library only (step 22)
  endpoint.py          host_label, error_detail: shared by the operator-endpoint clients
                       (remote ASR, ntfy)
  fakes.py             a fake backend for each of the five stages (tests and smoke runs)
  mail/                note.py (the mail contract: note, instructions file, slug, guid, dates,
                       paragraphs), message.py (compose_mail: the MIME message and the
                       attachment cap), smtp.py (SmtpMailer, the `smtp` Mailer; check_login
                       for doctor); step 25
  cli.py               `earmark` entry point
  api/                 app.py (create_app), auth.py (bearer middleware), routes.py, schemas.py
                       (responses and JobView), upload.py (receive_upload: the streaming
                       multipart reader; step 24)
  pipeline/
    stages.py          stage Protocols, Backends (the four stage implementations)
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
  notify/              ntfy.py (split_topic_url, clean_field, compose, NtfyClient,
                       NtfyNotifier: the `ntfy` Notifier; step 15)
  doctor/              report.py (CheckResult, render, exit code), checks.py (offline checks),
                       online.py (--online reachability checks), __init__.py (run_doctor,
                       Probes); `earmark doctor` (step 15)
tests/                 mirrors src; fixtures/ holds recorded Apple JSON and RSS,
                       share_sheet.json (real iOS share-sheet captures, added by hand) and
                       tls/ (a test CA and an `smtp.test` certificate, with make-certs.sh;
                       step 25); legacy_db.py builds a database shaped like v0.1.0's for the
                       refusal tests (step 22); smtp_stub.py is the SMTP server for the
                       delivery tests (step 25)
Dockerfile, .dockerignore, compose.yaml    the container image and services (step 16)
deploy/systemd/        earmark-api.service and earmark-worker.service (step 16)
deploy/caddy/Caddyfile the example reverse proxy (step 16)
```

## Domain models

Defined in `models.py` (step 02; exact types and validators in
`docs/steps/02-contracts.md`). Steps may add fields, with defaults so existing artifacts
still load. All models are pydantic, frozen, and `extra="forbid"`; sequences are tuples
(pass `tuple(...)`: mypy rejects a list for a tuple field); datetimes are timezone-aware;
seconds are finite and non-negative. The shared `Model` base replaces every lone UTF-16
surrogate (U+D800–U+DFFF) in any string field, including inside tuples, with U+FFFD before
validation (step 13): JSON decoders hand them over, plain `str` fields would accept them,
and then the artifact JSON and SQLite writes would fail with a `ValueError` that the
runner retries. `models.scrub_surrogates` is the helper; `runner.describe_error` applies it
to job text.

- `RequestFields` (step 24), the fields every request carries. Unknown keys are rejected,
  and messages never echo input.
  - `to`: one email address, normalised by `recipients.normalize_address`: ASCII, a
    dot-atom local part kept exactly, a lower-cased domain of two or more labels, at most
    254 characters.
  - `instructions`: stripped; `null` or missing becomes `""`; at most
    `MAX_INSTRUCTIONS_CHARS` (4000); otherwise verbatim.
  - `title`: stripped; blank becomes `None`; at most `MAX_TITLE_CHARS` (200).
  - `timestamp`: optional, and must convert to UTC. It sets `shared_at`, and its UTC offset
    (whole minutes; pydantic refuses seconds offsets) is stored as the job's
    `utc_offset_min`. That offset is the requester's for the note's dates (step 25).
- `TranscriptionRequest(RequestFields)` adds `url` (stripped, at most `MAX_URL_CHARS`, 2048).
  It is the JSON body of `POST /transcriptions`. `UploadFields(RequestFields)` holds the
  multipart text fields. Both replaced step 04's `IngestRequest` and its `prompt` in step 24.
- `EpisodeRef`: `source` (`"apple_podcasts"`), `show_id`, `episode_id`, `country`, `url`.
  Returned by `resolve.apple.parse_apple_url`; `url` is the canonical
  `https://podcasts.apple.com/{country}/podcast/id{show_id}?i={episode_id}` (country
  lower-case, `us` when the link has none; slug, tracking and share-from-time
  parameters dropped; legacy `itunes.apple.com` and `http` mapped to it).
- `Episode`: `key`, `source`, `show_id`, `episode_id`, `guid`, `title`, `show_title`,
  `published_at | None`, `duration_s | None`, `audio_url`, `feed_url | None`, `url`,
  `description`. A validator requires `key == source_key(source, show_id, guid)`.
  `Source` is `apple_podcasts`, `upload` or `url` (step 24).
  - **Upload and URL episodes** come from `sources.audio_episode(job, sha256_hex)`. They
    have `show_id = "audio"` (synthetic), `guid = "sha256:<hex of the audio bytes>"`, and
    `key = content_key(hex)`, which is `episode_key("local", "file", hex)`. That is exactly
    `transcribe.faster_whisper.file_key` of the same bytes, so an upload, a direct URL and
    `earmark transcribe` of identical bytes share one cache directory.
  - Their `title` is `sources.audio_title(job)`: the request's `title`, else the upload's
    file-name stem, else the URL's last path segment, else `Uploaded audio` or the URL's
    host.
  - Their `episode_id`, `show_title`, `audio_url` and `url` are `""`, and `published_at`,
    `duration_s` and `feed_url` are `None`.
  From the `apple` resolver (step 07): `guid` is Apple's episode GUID when Apple gives
  one, else the matched feed item's, else `apple-podcasts:<episode_id>`; `title` and
  `show_title` are Apple's, cleaned of control characters and capped (500 characters);
  `audio_url` is the feed enclosure, else Apple's public audio URL; `feed_url` is the
  lookup's `feedUrl`; `description` is plain text, capped at 20 000 characters.
  - **Step 25 added two fields with defaults**, so old `episode.json` files still load.
    - `feed_guid: str | None` is the matched feed item's `<guid>`: stripped, at most 1000
      characters, no control characters. It is `None` when the feed does not list the
      episode or the item has no guid. It feeds the note's `feed:` guid only, and is not
      part of the key.
    - `published_date_only: bool` is true when `published_at` is Apple's calendar date
      (midnight UTC from the episode page's JSON-LD).
- `Segment`: `start`, `end`, `text`, `speaker: str | None`.
- `Transcript`: `episode_key`, `language | None`, `duration_s`, `model`, `segments`,
  `warnings`, `asr_profile | None` (step 24: the profile it was made with, see
  "Transcribing"; `None` on transcripts written earlier). `to_text()` renders one
  `[HH:MM:SS] text` line per segment (whitespace collapsed, hours unbounded, trailing
  newline); it is the content of `transcript.txt`.
- `JobResult`: `duration_s` (required), the transcript's duration (step 22).
- `ShareRecord`: the content of `shared.json`, the earliest share time of the episode,
  recorded by the runner on every job (earliest wins). `record_share` returns it, and the
  note's `shared_at` is that time (step 25).
- `Job`: `id` (`uuid4().hex`), `kind` (`ingest`, the only `JobKind`), `status`, `stage`
  (`Stage` is `resolve`, `fetch`, `transcribe`, `deliver` or `notify`; `notify` is never set),
  `source`, `url`, `to | None`, `instructions` (`""` when none), `title | None`,
  `upload_file | None`, `upload_name | None`, `content_sha256 | None`, `episode_key | None`
  (set after resolve), `attempts`, `run_after`, `error` (failure message or skip reason),
  `warnings`, `result: JobResult | None`, `shared_at`, `created_at`, `updated_at`,
  `heartbeat_at`. Step 24 added `source` through `content_sha256` and removed `prompt`
  (carried over into `instructions` by migration `0002`).
  - **Step 25 added these fields** (migration `0003`):
    - `utc_offset_min: int | None`, in ±1439, the request's offset;
    - `delivery_state: "sending" | "sent" | None`;
    - `message_id`, required in both delivery states;
    - `sent_at`, required when `sent`;
    - `resent: bool`, set when a delivery starts while an earlier one was left `sending`.
  - **Per source:**
    - `apple_podcasts`: `url` is the canonical episode URL, so `Episode.url` is canonical
      too.
    - `url`: `url` is the client's direct URL without its fragment.
    - `upload`: `url` is `None`. `upload_file` is the staged file's name under
      `data/uploads/` (`<32 hex>.<ext>`), `upload_name` the client's cleaned file name, and
      `content_sha256` the hex sha256 of the staged bytes.
  - **`to`** is `None` only on jobs enqueued before step 24. Delivery fails such a job
    permanently: "this request has no destination address; share it again".
  - **The view.** The API never returns a `Job`. It returns a `JobView` (see "HTTP API").

**Episode key.** `sha256("{source}:{show_id}:{guid}")` over UTF-8, first 20 hex
characters, from `models.episode_key`. The Apple show id is used instead of the feed URL
because feeds move and show ids do not. Upload and URL episodes use
`content_key(hex) = episode_key("local", "file", hex)` instead (step 24), through
`models.source_key`. The key names the cache directory, so the same episode shared twice,
or the same bytes sent twice, is transcribed once. Parts are not normalised (the resolver strips
the GUID); `source` and `show_id` may not contain `:`. Keys are checked with
`re.fullmatch("[0-9a-f]{20}")` wherever they reach the filesystem.

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
    def download(self, url: str, destination: Callable[[str], Path], /) -> Path: ...  # step 24


class Transcriber(Protocol):
    def cached(self, episode: Episode, /) -> Transcript | None: ...  # step 24
    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript: ...


class Mailer(Protocol):  # step 25
    @property
    def sender(self) -> str: ...  # the From address
    def send(self, mail: OutgoingMail, /) -> None: ...  # Transient/Permanent/MaybeDelivered


class Notifier(Protocol):
    def notify(self, job: Job, episode: Episode | None, /) -> None: ...
```

`Notifier` receives the job in its terminal state. `OutgoingMail` (a frozen dataclass in
`stages.py`) holds `job_id`, `message_id`, `sender`, `recipient` (the one envelope
recipient, the job's validated `to`), `data` (the whole message: ASCII, CRLF), and the two
attachment file names.

- `AudioFetcher.download` fetches a client-supplied direct audio URL to `destination(ext)`
  and returns the path. It always downloads, with no cache. Only the runner calls it, for
  `url` jobs.
- `Transcriber.cached` returns the cached transcript only when it was made with this
  transcriber's ASR profile, and never loads a model.
- `Resolver.resolve` is called only for `apple_podcasts` jobs, so nothing else reaches it.

Every stage has a fake in `fakes.py` that records each call (`calls`), can be told to
raise (`fail_next(exc, times)`), and, for the cache-aware stages (fetch, transcribe),
counts the calls that did real work (`computed`). `FakeAudioFetcher` also counts direct
downloads (`downloads`), and serves `payloads.get(url, payload)`, so two URLs can serve
the same bytes. `FakeMailer` records each `OutgoingMail`. When the factory builds it (with
`EARMARK_MAIL_BACKEND=fake`), it also writes each message to `data/outbox/<job_id>.eml`
(mode `0o600`). Its sender is `EARMARK_SMTP_FROM` or the placeholder
`earmark@example.org`, because it sends nothing (step 25). The cache-aware fakes write real
artifacts through `ArtifactStore`. `make_fake_backends(data_dir)` builds the set, and
`FakeBackends.backends()` turns it into the `Backends` the runner takes.

Rules every backend follows:

1. **Idempotent and cache-aware.** If the output artifact exists, return it without
   doing the work. The runner does not track per-stage checkpoints in the database; it
   calls every stage and relies on this. `job.stage` exists for observability only.
   One exception: audio is deleted after transcription, so when
   `transcriber.cached(episode)` finds a transcript (with this ASR profile), the runner
   skips `fetch` and `transcribe`. The resolver is not cached; the runner writes
   `episode.json` after it returns. A direct URL is downloaded on every attempt, and is
   hashed before the cache is consulted (step 24).
2. **Atomic writes.** Write to a temp file in the same directory, then rename. Use
   `artifacts.atomic_writer` / `atomic_write_bytes` / `atomic_write_text` (temp files
   are `.<name>.<random>.tmp`, or another suffix such as `.part`; final files are mode
   `0o644`).
3. **Retry intent in the exception.** `TransientError` requeues with backoff until
   `max_attempts`. `PermanentError` fails the job immediately. `SkipEpisode` ends the job
   as `skipped` with a reason. Any other exception is treated as transient and its
   traceback is recorded.
4. **Notify failures never fail the job.** They are recorded in `job.warnings`.
5. **Non-fatal problems travel as data.** A stage that succeeds with a caveat (a
   transcript without segments) puts a message in the `warnings` of the `Transcript` it
   returns. The runner copies those into `job.warnings`. They are stored in the artifact,
   so a cache hit reports them again.

### Runner and worker

Step 05; exact signatures in `docs/steps/05-worker.md`.

- **Backends.** `Backends` (in `stages.py`) holds the five stages (resolver, fetcher,
  transcriber, mailer, notifier); `notifier` is `None` when its backend is `none`. `factory.build_backends` is the
  only code that constructs backends: one registry per setting maps a backend name to a
  builder, and a name with no builder is a `ConfigError` naming the variable, raised
  before the database is opened. Real backends import their heavy modules inside the
  builder.
- **Order.** `Pipeline.run(claim)` runs the stages below. Each stage sets `job.stage` before
  it runs. Every job that completes ends at `deliver` (step 25); one with a cached
  transcript goes straight from `resolve` (or `fetch` for a direct URL) to `deliver`. A
  failed job keeps the stage it failed in, and `notify` is never set as a stage.
  1. **The episode** (`resolve`), by source (step 24):
     - `apple_podcasts`: `resolver.resolve(job.url)`.
     - `upload`: the staged file `data/uploads/<upload_file>`. It must exist, else a
       permanent failure: "the uploaded audio is missing; upload it again". Its sha256 must
       equal `content_sha256`, else permanent. The episode is
       `audio_episode(job, content_sha256)`.
     - `url`: set stage `fetch`, clear this job's old downloads, then
       `fetcher.download(job.url, staging.download_destination(job.id))`, then
       `audio_episode(job, sha256_file(path))`.
  2. **Record** it: write `episode.json`, record the episode key on the job, and record the
     share time in `shared.json`. Keep the earliest share time it returns for the note.
  3. **Cache.** If `transcriber.cached(episode)` returns a transcript, skip fetch and
     transcribe.
  4. **Transcribe.** Otherwise, for a podcast only, `fetch`. Then `transcribe` the
     podcast's audio, or the staged upload or download in place: it is never moved into
     `episodes/<key>/`.
  5. **Check.** Refuse a transcript for another episode (permanent). Copy the transcript's
     warnings to the job.
  6. **Compose** (stage `deliver`, still inside the interruptible window).
     - A job without `to` fails permanently.
     - So does a `to` the allowlist no longer allows: "the destination is no longer allowed
       by EARMARK_ALLOWED_RECIPIENTS; nothing was sent". `build_worker` passes the policy;
       `None` skips this check, for tests only.
     - Otherwise `mail.message.compose_mail` renders the note and the instructions file and
       builds the message (see "Delivering").
  7. **Deliver**, outside the window.
     1. `mark_sending(claim, message_id)`.
     2. `mailer.send(mail)`. On a definite failure, `clear_sending(claim)` and re-raise. On
        `MaybeDelivered`, re-raise and leave `sending`.
     3. `complete_sent(claim, JobResult(duration_s))` writes `sent`, `sent_at` and `done`
        in one fenced update.

     A claimed job already `sent` (only possible by hand) completes without sending.
  8. **Finish:** `staging.release(job)`, then notify.
- **Retention** (step 24). `Pipeline.finish(job, episode)` releases the staging files of an
  upload (`uploads/<upload_file>`) or a direct URL (`downloads/<job_id>.*`), then notifies.
  - **When it runs:** for every job that reaches `done`, `failed` or `skipped`, including
    jobs that `requeue_stale` fails.
  - **What it ignores:** `EARMARK_KEEP_AUDIO` does not keep uploaded or downloaded audio. A
    requeued retry keeps the staging file. Release errors are logged, never raised.
  - **Podcasts:** their audio follows `CachedTranscriber`'s rules (see "Transcribing").
- **Outcomes.** `SkipEpisode` → `skipped`; `PermanentError` → `failed`;
  `TransientError` → requeued with backoff (honouring `retry_after_s`) until attempts
  run out; any other `Exception` → transient, with the traceback logged at ERROR and a
  one-line summary naming the raising file, line, and function in `job.error`. Error
  and warning text is whitespace-collapsed, redacted of every `Settings` secret, and
  capped at 500 characters; the text of a non-earmark exception also has URL paths and
  queries scrubbed (`log.scrub_urls`, step 20). Notifier failures are warnings
  (`notify failed: …`) and never change the status.
- **Notify.** Exactly once per job that reaches `done`, `failed`, or `skipped`, with the
  job as written (warnings included) and the episode if resolution got that far. Not for
  a requeued retry, a released job, or a lost lease.
- **Heartbeat.** A thread heartbeats every `min(60 s, JOB_LEASE_SECONDS / 4)` while a
  job runs. It shows the process is alive, not that a stage progresses.
- **Shutdown.** SIGTERM and SIGINT stop the worker from claiming. During stage work and
  composing, the handler raises a `Shutdown` (`BaseException`) out of the stage, and the
  worker `release`s the job (back to `queued`, attempt undone). Once the job is delivering,
  completing or notifying, the worker finishes it, then exits.
  - Delivery is bounded by `EARMARK_SMTP_TIMEOUT_S` (at most 90 s).
  - The worker's stop grace (compose `stop_grace_period` 2m, systemd `TimeoutStopSec` 120)
    must be at least that plus `config.WORKER_STOP_MARGIN_S` (30 s, for the push and the
    job store). `tests/test_deploy.py` pins this.
- **Lost lease.** A fenced call raising `LeaseLostError` makes the runner abandon the
  job without writing to it or notifying.
- **Each poll** the worker first runs `requeue_stale` and passes the jobs it failed to
  `Pipeline.finish`.
- **Commands.**
  - `earmark worker --once` processes at most one job.
  - `earmark submit SOURCE --to … [--instructions …] [--title …]` enqueues through
    `submission.Intake`, like `POST /transcriptions`. SOURCE is an existing file, which is
    copied into `data/uploads/` under `EARMARK_MAX_UPLOAD_MB`, or a URL.
  - `earmark jobs` lists recent jobs as `JobView`s.
- **Startup.** `build_worker` sweeps `data/downloads/` of every file not belonging to a
  `running` job, before the first poll.

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
- **Direct URLs** (step 24). `HttpAudioFetcher.download(url, destination)` downloads a
  client-supplied URL through the same `GuardedFetcher.download`, with the same rules and
  `MAX_AUDIO_MB`. That includes the address guard on every hop, the refusal of a textual
  content type, and host-only messages. It logs the host only.

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
- **ASR profile, the transcript cache key** (step 24).
  - **The engine.** Every `TranscriptionEngine` has a `profile`:
    `asr_profile(backend, options)`, that is `<backend> <JSON of the options, keys sorted>`.
    - `faster_whisper`: model, device, compute type, language (`null` for auto), beam size
      and VAD filter.
    - `remote`: model and language.
    - The fake: model and language.

    Step 26's batch size joins the `faster_whisper` options.
  - **The stamp.** `CachedTranscriber` stamps it on every transcript it writes
    (`Transcript.asr_profile`). `cached(episode)` returns `transcript.json` only when the
    stamp equals the engine's profile.
  - **A miss** (another model or options, or a transcript from before step 24)
    re-transcribes. That overwrites the episode's one `transcript.json` and
    `transcript.txt`, so a `tiny` dry-run transcript is never reused for `large-v3`.
  - **`earmark transcribe`** writes nothing and is unaffected.
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

### Delivering

Step 25; exact rules in `docs/steps/25-delivery.md` sections 4 and 5.

- **The note and the instructions file** (`mail/note.py`) are rendered to the mail contract
  below.
  - **Strings.** Every feed- or caller-supplied frontmatter string goes through
    `contract_text`: NFC, whitespace becomes a space, categories Cc, Cf, Co, Cn, Zl and Zp
    are dropped (except ZWJ and ZWNJ), runs collapse, and the result is capped with
    `sanitize.fit_chars` (`title` 200, others 300). `sanitize.emit_frontmatter` then quotes
    every string; omits `None` keys; writes integers, `YYYY-MM-DD` dates, and RFC 3339
    timestamps to the second with an offset; and refuses empty values.
  - **The requester's offset** is the job's `utc_offset_min`, else UTC. `shared_at` (the
    note's: the earliest share of the episode; the instructions': this request's) and the
    filename date use it.
  - **The publish date** is `published_at` in the requester's offset, or its calendar date
    when `published_date_only`. The filename date is the publish date, else the
    first-share date.
  - **The slug.** NFKD, combining marks dropped, ASCII, lower-case, every run of characters
    outside `[a-z0-9]` becomes `-`. A podcast joins the show and title slugs. The whole slug
    is cut to 80 characters. When it is empty, the fallback is `podcast-<episode id>` or
    `audio-<12 hex of the sha256>`.
  - **The guid.**
    - Audio: `sha256:<hex>`.
    - Podcast: `feed:<Episode.feed_guid>` when that is verbatim (cleaning would not change
      it) and at most 300 characters, else `apple:<episode id>`.
    - Apple's `episodeGuid`, which still keys the cache, is not used for the note: it can
      differ from the feed's.
  - **`source_url`** (`origin: url` only) is `sources.display_url`. It is omitted when the
    URL has a `?`, or when the value is over 300 characters or would be changed by cleaning.
  - **`language`** is kept only when it looks like BCP 47 (`[a-z]{2,3}(-[A-Za-z0-9]{2,8})*`).
  - **The body.** `# <show> — <title>` (podcast) or `# <title>`, then `## Transcript`, then
    paragraphs. A new paragraph starts on a speaker change, once the current one spans
    30 s, or when the next segment would take it past 60 s. A longer single segment stands
    alone and is never split. Each paragraph starts with `[hh:mm:ss]` of its first segment.
    Segment text has whitespace collapsed and Cc characters removed, and nothing else
    changed. `transcript_sha256` is the sha256 of the bytes after the closing `---\n`.
  - **The instructions body** is `job.instructions` with CRLF and CR turned into LF, and no
    trailing newline added.
- **The message** (`mail/message.py` `compose_mail`).
  - **Headers:** `From`, `To`, `Subject`, `Date` (UTC, from the injected clock) and
    `Message-ID: <earmark.<job_id>@<from domain>>`, in that order, plus the MIME headers.
    There are no others.
  - **The subject** is `[earmark] ` plus the note's title, cleaned with `=?` defused (an
    encoded word in a header value is decoded when set), and capped at 120 characters. The
    `email` policy (`email.policy.SMTP`) encodes non-ASCII as RFC 2047 words.
  - **The text part** is a fixed ASCII template (`cte="7bit"`). Its only variable parts are
    the job id and the two file names, which are `[0-9a-z.-]`.
  - **Attachments.** Both are added as bytes: base64, `text/markdown; charset="utf-8"`,
    `Content-Disposition: attachment; filename=…`. That keeps LF and the hash exact; the
    `str` path would rewrite line endings.
  - **The boundary** is `=_earmark_<job_id>`, injectable. It cannot occur in base64 or in
    the template.
  - **The size cap.** The two attachments' raw bytes together must not exceed
    `EARMARK_MAX_ATTACHMENT_MB`, else the job fails permanently with a message naming the
    setting. Nothing is truncated or split.
  - **No job without `to`.** A job without `to` fails permanently.
- **SMTP** (`mail/smtp.py` `SmtpMailer`, the `smtp` backend).
  - **Settings.** `EARMARK_SMTP_HOST`, `EARMARK_SMTP_FROM`, `EARMARK_SMTP_USERNAME` and
    `EARMARK_SMTP_PASSWORD` are required. Missing ones are a `ConfigError` at startup that
    names each of them. The builder makes no connection.
  - **Connection.**
    - `smtplib.SMTP` (STARTTLS, port 587 by default) or `smtplib.SMTP_SSL` (`tls`, 465),
      always constructed with the host, so `server_hostname` is set.
    - `local_hostname` is the domain of `EARMARK_SMTP_FROM`, so `socket.getfqdn()` is never
      called.
    - The context comes from `ssl.create_default_context()`: `CERT_REQUIRED`,
      `check_hostname`, strict X.509.
    - No debug level.
  - **STARTTLS** mode fails permanently when STARTTLS is not offered. EHLO is sent again
    over TLS. AUTH happens only after TLS.
  - **The transaction.** earmark drives it itself: `MAIL FROM`, one `RCPT TO` (the job's
    `to`), `DATA`, the dot-stuffed payload, then the final reply. `sendmail` cannot tell a
    loss before the end of data from a loss while waiting for the reply.
  - **The deadline.** `EARMARK_SMTP_TIMEOUT_S` (default 60, at most 90) is both the
    per-operation socket timeout and a whole-session deadline. A watchdog timer shuts the
    socket down through a duplicate of its descriptor (`socket.fromfd`), so a dripping
    server cannot hold a stopping worker.
  - **The only private hook** is `_get_socket`, overridden to take an injected socket
    factory (the tests' stub) and to arm the watchdog.
- **Delivery state, at-least-once.**
  - **`sending`.** `mark_sending` records `sending` and the `Message-ID` before the
    conversation, and sets `resent` when the state was already `sending`.
  - **`sent`.** After a 2xx to the end of data, `complete_sent` writes `sent`, `sent_at` and
    `done` together. Nothing claims a `done` job, so a `sent` job is never sent again on
    retry, restart or lease expiry.
  - **Definite failures** clear `sending`. These are 4xx and 5xx replies, a connection lost
    before the payload is fully written, auth, TLS, missing STARTTLS, and any exception
    other than `MaybeDelivered`.
  - **Ambiguous failures** leave it. These are a disconnect, timeout or unreadable reply
    after the payload (`MaybeDelivered`), a crash, or a lost lease. The next attempt resends
    with the same `Message-ID` and `job_id`, and its push says "Resent after an
    interruption."
- **Failure classes.**
  - 4xx replies are transient, and 5xx replies are permanent.
  - A connection error before the payload is complete is transient.
  - A disconnect after the payload with no reply is `MaybeDelivered` (transient).
  - AUTH failures: 5xx is permanent, naming `EARMARK_SMTP_USERNAME` and
    `EARMARK_SMTP_PASSWORD`; 4xx is transient.
  - A certificate that does not verify is permanent, naming `EARMARK_SMTP_HOST`.
  - **Job errors** carry the host, the phase, the reply code and the enhanced status code.
    They never carry the server's reply text, which echoes addresses: errors made from
    smtplib exceptions are raised `from None`.

#### The mail contract (pka-skills, `contract: 1`, 2026-11-23)

Status: **DRAFT**. It becomes final once the operator approves the PKA's receiving
procedure. A revision would come with a contract version bump, and would change
`docs/PLAN.md` "Re-scope" and this copy together. Step 25 built to this text.

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

### Notifying

Step 15; exact rules in `docs/steps/15-notify-doctor.md` sections 4 and 5, with step 25's
wording in `docs/steps/25-delivery.md` section 5.8.

- **`ntfy` Notifier.** `notify.ntfy.NtfyNotifier` is built by `build_ntfy_notifier(settings)`.
  It is `None` (nothing sent, worker starts) when `EARMARK_NTFY_URL` is unset, and the build
  makes no request. For each job that ends `done`, `failed`, or `skipped` it sends one
  `POST` of JSON to the server root: the topic URL minus its last segment. The body is
  `topic`, `title`, `message`, `tags`, and `priority`. The only headers
  are fixed, plus `Authorization: Bearer EARMARK_NTFY_TOKEN` when a token is set. No
  untrusted text goes into a header, the URL, or a query string. `markdown` is never set.
- **Content.**
  - Titles are `Sent: <label>` (step 25; it was `Transcribed:`), `Failed: <label>`, and
    `Skipped: <label>`. Tags are `white_check_mark`, `x`, and `fast_forward`; priority is 4
    for failed and 3 otherwise.
  - **Labels and the first message line, by source** (step 24):
    - podcast: the episode title (`podcast episode` when unknown), then the show
      (`Apple Podcasts episode` when unknown);
    - upload: the request's title, else the episode title, else the file-name stem, else
      `uploaded audio`, then `Uploaded audio`;
    - direct URL: the request's title, else `audio from <host>`, then `Audio from <host>`.
      A URL job's episode title is never used, because it can come from the URL path.
  - The rest of the message:
    - Done (sent) adds `To: <masked recipient>`, `Duration: HH:MM:SS`, `Resent after an
      interruption.` when the job's `resent` is set, and the warning count with the first
      warning.
    - Failed adds `To: <masked recipient>` when the job has one, the stage, `Reason:
      <job.error>`, and `Gave up after N attempts.` when there was more than one. It adds
      `The email may already have been delivered.` when the job was left `sending`.
    - Skipped adds the reason.

    Reasons and warnings pass through `log.scrub_urls` as well.
  - No push carries a URL path or query, the instructions, the full recipient address (only
    `recipients.mask_address`), the guid, the subject, the `Message-ID`, or a path. None
    has a `click` link.
  - Every value is cleaned: NFC; control, format (except ZWJ/ZWNJ), private-use,
    unassigned, and line or paragraph separator characters removed; whitespace collapsed;
    `=?` defused, because ntfy RFC-2047-decodes titles even in JSON mode; secrets
    redacted. It is then cut at a safe boundary: title 250 bytes, message 2000 bytes, so
    the message is never turned into an attachment.
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
- **Offline checks**, in order.
  - The API token.
  - `recipients` (step 24): `FAIL` when `EARMARK_ALLOWED_RECIPIENTS` is unset (every request
    would be refused), `warn` for `all`, otherwise `ok` with the count and the masked
    addresses.
  - `DATA_DIR` writability, by creating and removing one random temp file.
  - The database: schema version, application id, journal mode, `quick_check`, and job
    counts, read without migrating. A missing file is never created. A database from before
    step 22, or another application's file, is `FAIL` with the refusal message of "Database
    identity".
  - Each selected backend (resolver, fetch, asr), built through the same factory registry
    as the worker. That imports its extra and runs the CUDA check.
  - For `faster_whisper` only: `asr libraries` (step 19): the installed `faster-whisper`
    and `av` versions, read from package metadata (`importlib.metadata`, through
    `Probes.installed`), checked against `transcribe.compat.KNOWN_INCOMPATIBLE`. It
    warns on a known-bad pair (today faster-whisper <= 1.2.1 with av >= 19), or when a
    version cannot be read. A package installed from a source tree (PEP 610
    `vcs_info`/`dir_info`) is not checked by version. Then `asr model`: whether the Whisper
    model is local or in the cache, through faster-whisper's
    `download_model(local_files_only=True)`.
  - `mail` (step 25), built through the `MAILERS` registry:
    - `warn` for `fake`;
    - `FAIL` naming the missing `EARMARK_SMTP_*` settings;
    - otherwise `ok`, with the host, port, security, from domain and timeout.
  - `notify`.

  Offline doctor makes no network call, loads no model, downloads nothing, and creates no
  directory or database. Fake backends are warnings.
- **`--online` checks.** Each has a 10 s timeout (connect 5 s), follows no redirects,
  ignores proxy variables, and spends nothing:
  - the iTunes lookup (`?id=1&entity=podcast`) through `fetch.py`, so the address guard
    applies;
  - `GET {ASR_BASE_URL}/models` for the `remote` backend;
  - ntfy `GET /v1/health` and, with a token, `GET /v1/account`;
  - `smtp` (step 25), for the `smtp` backend: `SmtpMailer.check_login` connects,
    negotiates TLS, logs in and quits. It sends no `MAIL`, `RCPT` or `DATA`, and it reports
    the TLS version. Its timeout is the smaller of 10 s and `EARMARK_SMTP_TIMEOUT_S`.

  Doctor never publishes a notification and never sends mail. `Probes` injects the SMTP
  socket and TLS context factories for tests.
- **Redaction.** Details never contain secret values, their prefixes, or their lengths.
  Endpoints appear as host labels only, and server error text goes through
  `endpoint.error_detail`. Every line is passed through `log.redact` with all `Settings`
  secrets and cleaned of non-printable characters.

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
- **Delivery writes** (step 25) are fenced the same way.
  - `mark_sending(claim, message_id)` sets `sending` and the `Message-ID`, and sets
    `resent` when the state was already `sending`.
  - `clear_sending(claim)` clears `sending` after a definite failure.
  - `complete_sent(claim, result)` requires `sending` and writes `sent`, `sent_at`, `done`
    and the result in one update.
  - `release`, `fail`, `requeue_stale` and `complete` leave the delivery columns alone, so a
    crashed or stale `sending` job is resent on its next claim.
- **Attempts and retries.** `attempts` counts claims. `fail(transient=True)` requeues
  with `run_after = now + retry_delay(attempts, retry_after_s)` (60 s × 4ⁿ⁻¹, capped
  at 6 h, raised to `retry_after_s`) until `attempts` reaches `JOB_MAX_ATTEMPTS`, then
  fails. `fail(transient=False)` fails at once. `release` (SIGTERM) requeues
  immediately and undoes the attempt.
- **Leases.** The worker heartbeats while a stage runs. A `running` job whose heartbeat
  is older than `JOB_LEASE_SECONDS` is requeued by `requeue_stale` (or failed, if it has
  used its attempts), which covers a crashed worker. `requeue_stale` returns the jobs it
  changed; the worker calls it on every poll and notifies the ones it failed.
- **Enqueue** (step 24). `enqueue(NewJob)` takes the source, `to`, `instructions`,
  `title`, the URL or the staged upload's name, file name and sha256, and `shared_at`.
- **Dedupe** (step 24).
  - **The key** is `sha256(json [kind, source, subject, to, instructions])`, where the
    subject is the canonical Apple URL, the direct URL, or `sha256:<hex>` for an upload.
    `title` and `shared_at` are not part of it.
  - **A repeat** returns the existing job (`Enqueued.created` is `False`) while it is
    `queued` or `running`, or while it is `done` and its `updated_at` is less than
    `EARMARK_DEDUPE_WINDOW_S` (default 600) old. So a double tap sends one email.
  - **Not deduped.** A `failed` or `skipped` job is never returned, so sharing again
    retries. A new `to` or new `instructions` makes a new job, which reuses the cached
    transcript.
  - **The index.** A partial unique index on `dedupe_key` still enforces one active job per
    request.
  - **Normalisation.** `to` is normalised and `instructions` stripped before the key is
    computed.
  - **Content.** The same bytes as an upload and as a URL are different requests, because a
    URL's bytes are unknown until the worker downloads them. They still share one
    transcript through the content key.
- **Staging queries** (step 24). `active_upload_files()` returns the `upload_file` of the
  queued and running jobs, which the API's startup sweep keeps. `running_job_ids()` is
  used by the worker's.
- **Warnings.** `add_warning(job_id, message)` appends unless the same text is already
  there, and works on terminal jobs (notifier failures are recorded after `done`).
- **Time.** Clocks are injected so tests control time. Timestamps are stored as
  fixed-width UTC ISO strings, so SQL compares them as text; `shared_at` is kept as an
  instant, read back in UTC. The request's offset is stored separately as
  `utc_offset_min` (step 25), or `NULL` when the request had no `timestamp`.

### Database identity

Step 22; exact rules in `docs/steps/22-rescope-remove.md` section 4.2.

- The migration list restarted in step 22 with `0001_jobs` (the `jobs` table with
  `CHECK (kind IN ('ingest'))`, `jobs_active_dedupe`, `jobs_by_status`). Migrations stay
  append-only.
- Step 24 appended `0002_requests` (schema version 2). It adds these columns:
  - `source` (`NOT NULL DEFAULT 'apple_podcasts'`, `CHECK` in the three sources);
  - `recipient`, which is the `Job.to` field (`TO` is an SQL keyword);
  - `instructions` (`NOT NULL DEFAULT ''`);
  - `title`, `upload_file`, `upload_name` and `content_sha256`.

  It copies `prompt` into `instructions`, drops `prompt`, and adds
  `jobs_by_dedupe (dedupe_key, status, updated_at)`. Rows from before it keep `recipient`
  NULL and their status.
- Step 25 appended `0003_delivery`, so `SCHEMA_VERSION` is 3. It adds these columns:
  - `utc_offset_min` (`INTEGER`, `CHECK` in ±1439);
  - `delivery_state` (`CHECK IN ('sending', 'sent')`);
  - `message_id` and `sent_at`;
  - `resent` (`INTEGER NOT NULL DEFAULT 0`, `CHECK IN (0, 1)`).

  Earlier rows get `NULL` and `0`.
- The lineage is marked by `PRAGMA application_id = 0x45524D4B` (`b"ERMK"`), stamped in
  the same transaction that creates `jobs`.
- `identity_error(path, application_id, user_version)` decides:

  | `(application_id, user_version)` | Result |
  |---|---|
  | `(0, 0)` | A new or empty file: adopted. |
  | `(0, >0)` | `LegacyDatabaseError`. earmark 0.1.0 wrote `(0, 2)`; dev databases of steps 03–12 read `(0, 1)`. |
  | `APPLICATION_ID`, any version | Accepted; then the newer-than-supported check applies. |
  | anything else | `ForeignDatabaseError`. |

- The two messages, as in `db.py`:
  - Legacy: `<path> was created by earmark 0.1.0 (schema version <n>), which this version
    cannot use and does not migrate. Move it aside, with its -wal and -shm files if
    present, and start again.`
  - Foreign: `<path> is not an earmark database (application id <hex>). Move it aside or set
    EARMARK_DATA_DIR to another directory.`
- `open_database` runs `check_identity()` before `ensure_wal()` and `migrate()`, and
  `migrate` checks again inside its `BEGIN IMMEDIATE`, so a refused file is never
  modified: version, application id, journal mode and rows unchanged.
- Who opens it: `serve`, `worker`, `submit` and `jobs` refuse with `earmark <cmd>: cannot
  open the job store: <message>` and exit 1; `config`, `resolve` and `transcribe` never
  open it; `doctor` reports `FAIL database <message>`. Nothing is migrated.

## Data layout

```
$EARMARK_DATA_DIR/               a local filesystem: WAL needs shared memory (no NFS/SMB)
  earmark.db                    jobs (application id ERMK, schema 3); WAL mode
  outbox/<job_id>.eml           only with EARMARK_MAIL_BACKEND=fake: the message the fake
                                mailer "sent" (0o600; step 25)
  uploads/                      staging.Staging, owned by the API (mode 0o700; step 24)
    .<upload_id>.<ext>.part     an upload still streaming (0o600); swept at API startup
    <upload_id>.<ext>           a received upload (0o600); deleted when its job ends, or by the
                                API's startup sweep when no queued or running job names it
  downloads/                    staging.Staging, owned by the worker (step 24)
    <job_id>.<ext>              a direct URL's audio for one job; deleted when the job ends
    .<job_id>.<ext>.<random>.part  download in progress; swept at worker startup unless the
                                job is running
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

An episode key is checked against `[0-9a-f]{20}`, an upload name against
`[0-9a-f]{32}\.[a-z0-9]{1,5}`, and a job id against `[0-9a-f]{32}`, before any of them
becomes a path. Nothing outside `DATA_DIR` is written, and uploads never touch the system
temp dir. For an upload or URL episode (a content key), `episode.json` is rewritten by each
job, so it holds the last job's title. Its audio is never stored under `episodes/`.

## HTTP API

Every path except `/healthz` requires `Authorization: Bearer $EARMARK_API_TOKEN`. A pure
ASGI middleware (`api/auth.py`) checks it before routing or body parsing, comparing
SHA-256 hashes with `hmac.compare_digest`, so an unauthenticated request gets 401 with
`WWW-Authenticate: Bearer realm="earmark"` whatever its path or body. The service
refuses to start with an empty or whitespace-only token, or one that is not printable
ASCII. Interactive docs and `/openapi.json` are off. Errors are `{"detail": "<short
message>"}`, written to be shown by the Shortcut as is; validation errors never echo
input. A `sqlite3.Error` is 503 with `Retry-After`. Details in `docs/steps/04-api.md`.

| Route | Purpose |
|---|---|
| `POST /transcriptions` | JSON `{url, to, instructions?, title?, timestamp?}` (`TranscriptionRequest`, at most 64 KiB), or `multipart/form-data` with one `file` part plus the text fields `to`, `instructions`, `title` and `timestamp` (the file at most `EARMARK_MAX_UPLOAD_MB`). The answer is 202 `{job_id, status, created}` with `Location: /jobs/{id}`; `created` is `false` when dedupe returned an existing job. 401 bad token. 413 too large. 415 a content type that is neither, or an upload that does not look like audio. 422 an invalid body, a missing, invalid or not-allowed `to`, or a bad URL or show link, with a message the Shortcut can display. Step 24; it replaced `POST /ingest`. |
| `GET /jobs/{id}` | The job's `JobView`. 404 if unknown. |
| `GET /jobs` | `?limit=1..500` (default 20), `?status=`. `{"jobs": [JobView, …]}`, newest first. |
| `GET`, `HEAD /healthz` | Liveness, no auth, `{"status": "ok"}`. |

**`POST /transcriptions`** (step 24; exact rules in `docs/steps/24-transcription-requests.md`
section 5).

- **No automatic body handling.** The route is `async` and has no body parameters, so
  FastAPI reads nothing. It sets no route `max_body_size` and enforces both caps itself.
- **Dispatch.** It branches on the media type:
  - `application/json`: at most 64 KiB, checked against `Content-Length` and against the
    bytes read.
  - `multipart/form-data`:
    - **Streaming.** `api.upload.receive_upload` feeds `request.stream()` to
      python-multipart's streaming `MultipartParser`, and writes the `file` part's bytes to
      `data/uploads/.<id>.<ext>.part` through the threadpool, hashing as it goes.
    - **Never spooled.** Starlette's form parser is not used, because it spools files to
      memory and the system temp dir.
    - **Size.** The declared `Content-Length` is checked against the cap plus 128 KiB
      before reading. The file bytes are checked against the exact cap while reading.
    - **Fields.** Text fields may come before or after the file. Each is at most 16 KiB.
      There are at most 8 parts, and unknown or repeated fields are refused.
    - **The audio check** is the part's declared content type or its file name's extension
      only (`fetch.AUDIO_EXTENSIONS`, `CONTENT_TYPE_EXTENSIONS`, any `audio/*`).
    - **Cleanup.** Any rejection, a malformed or truncated body, or a client disconnect
      deletes the partial file. A validation failure after the file deletes it too, and so
      does a dedupe hit or a store error at enqueue. A partial or rejected upload therefore
      leaves nothing in `uploads/`.
- **URLs.** An Apple Podcasts link (`podcasts.apple.com`, `itunes.apple.com`) is
  canonicalised as before, with the same messages for malformed links, show links and
  non-episodes.
  - Any other `http://` or `https://` URL is a direct audio URL, stored without its
    fragment.
  - Other `*.apple.com` hosts are refused as not an episode, and URLs with credentials are
    refused.
  - A direct URL needs its scheme spelled out. It is not audio-checked at the API, which
    makes no request.
- **Recipients.** `to` is normalised and checked against `EARMARK_ALLOWED_RECIPIENTS`
  after the body validates. Refusals name the setting, never the address.
- **Startup.** `create_app` sweeps `data/uploads/` of everything not named by a queued or
  running job, and logs a WARNING when the allowlist is `all`. `create_app(…,
  max_upload_bytes=…)` overrides the cap for tests.

**`JobView`** (step 24) is the only job shape the API and `earmark jobs` return. It has:

- `id`, `status`, `stage`, `source`;
- `url`: the canonical Apple URL whole; for a direct URL, scheme, host, port and path only;
  for an upload, `null`;
- `to`, in full;
- `instructions`, cut to 200 characters (199 and `…`);
- `title`, `episode_key`, `attempts`, `run_after`;
- `error` and `warnings`, passed through `log.scrub_urls`;
- `result`, `shared_at`, `created_at`, `updated_at`;
- `delivery_state` and `sent_at` (step 25).

It never carries `heartbeat_at`, the lease token, the staged file name, the upload's file
name, the content hash, the `Message-ID` or `resent`.

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
| `DATA_DIR` | `./data` | Database and episode cache |
| `RESOLVER_BACKEND` | `apple` | `apple`, `fake` |
| `FETCH_BACKEND` | `http` | `http`, `fake` |
| `ASR_BACKEND` | `faster_whisper` | `faster_whisper`, `remote`, `fake` |
| `MAIL_BACKEND` | `smtp` | `smtp`, `fake` (step 25). `smtp` without its required settings is a startup `ConfigError`; `fake` sends nothing and writes `data/outbox/<job_id>.eml` |
| `NOTIFY_BACKEND` | `ntfy` | `ntfy`, `fake`, `none`. `ntfy` without `NTFY_URL` sends nothing (the worker still starts) |
| `ASR_MODEL`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, `ASR_LANGUAGE` | `large-v3`, `auto`, `auto`, unset (auto-detect) | faster-whisper options (step 08); `remote` also uses `ASR_MODEL` (sent as the server's model id, e.g. `openai/whisper-large-v3`) and `ASR_LANGUAGE`. Model: a size such as `tiny`, `small`, `large-v3`, `distil-large-v3`, `large-v3-turbo`, a Hugging Face repo id, or a local directory. Device: `auto`, `cpu`, `cuda`. Compute type: `auto`, `default`, `int8`, `int8_float32`, `int8_float16`, `int8_bfloat16`, `int16`, `float16`, `bfloat16`, `float32` (device and compute type case-insensitive). Language: a lower-case code such as `en`; `auto` means detect |
| `ASR_MODEL_DIR` | unset (the Hugging Face cache, `$HF_HOME/hub` or `~/.cache/huggingface/hub`) | Where faster-whisper downloads models |
| `ASR_BASE_URL`, `ASR_API_KEY`, `ASR_MAX_UPLOAD_MB` | unset, unset, unset (no upload limit) | Remote ASR backend (step 09). Base URL: the server's OpenAI-compatible base, ending in `/v1` (http or https, a host, no credentials, query, or fragment; a trailing `/` is dropped). Key: secret, printable ASCII without spaces, sent as a bearer token. Upload limit in MiB |
| `ASR_TIMEOUT_SECONDS` | `10800` | How long the remote backend waits for the server's answer (read and write timeout) |
| `KEEP_AUDIO` | `false` | Keep audio after transcription |
| `NTFY_URL`, `NTFY_TOKEN` | unset | ntfy topic URL and optional access token (both secret; step 15). URL: http(s), a host, optional path prefix, ending in the topic (`[-_A-Za-z0-9]{1,64}`), such as `https://ntfy.sh/my-earmark-topic` or `http://ntfy.tailnet-name.ts.net/earmark`; no credentials, query, or fragment. On public ntfy.sh the topic name is the only protection unless the topic is reserved, so pick a long random one. Token: printable ASCII without spaces (`tk_…`), sent as a bearer token |
| `MAX_AUDIO_MB`, `MAX_FEED_MB`, `ALLOW_PRIVATE_FETCH` | `1024`, `20`, `false` | Fetch limits in MiB (× 1024 × 1024); `MAX_AUDIO_MB` also caps direct-URL downloads; `MAX_FEED_MB` caps the lookup, the episode page, and the feed. `ALLOW_PRIVATE_FETCH` turns off the address check only |
| `ALLOWED_RECIPIENTS` | unset (every request refused) | Comma-separated exact addresses, or `all` (any address; a written acceptance of risk, warned about at `serve` startup and by doctor). Matching is exact on the local part and case-insensitive on the domain. Cannot mix `all` with addresses. Shown masked by `earmark config` (step 24) |
| `MAX_UPLOAD_MB` | `1024` | Upload size cap in MiB, for `POST /transcriptions` and `earmark submit FILE` (step 24) |
| `DEDUPE_WINDOW_S` | `600` | Seconds a `done` job is returned for an identical request (same source, `to` and `instructions`); `0` dedupes active jobs only (step 24) |
| `SMTP_HOST` | unset; required for `smtp` | The SMTP submission server (a host name, no scheme) (step 25) |
| `SMTP_PORT` | unset: `587` for `starttls`, `465` for `tls` | SMTP port |
| `SMTP_SECURITY` | `starttls` | `starttls` (STARTTLS required; delivery fails if the server does not offer it) or `tls` (implicit TLS). There is no plaintext option |
| `SMTP_USERNAME` | unset; required for `smtp` | Login name: printable ASCII without spaces |
| `SMTP_PASSWORD` | unset; required for `smtp` | Login password (secret): printable ASCII, spaces allowed (app passwords), no control characters |
| `SMTP_FROM` | unset; required for `smtp`, never defaulted | The sending address (`From` and envelope sender). Its domain is the EHLO name and the `Message-ID` domain |
| `SMTP_TIMEOUT_S` | `60` | Whole-session deadline and socket timeout for one delivery, at most 90 so a stopping worker finishes within its grace period |
| `MAX_ATTACHMENT_MB` | `10` | Cap in MiB on the two attachments' raw bytes together; a job over it fails permanently |
| `JOB_MAX_ATTEMPTS`, `JOB_LEASE_SECONDS`, `WORKER_POLL_SECONDS` | `4`, `900`, `2` | Queue behaviour |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive) |

Backend names are `Literal` types, so a misspelt name fails when settings load, naming
the variable. A name that is valid but whose backend is not built yet is rejected by
`pipeline/factory.py` (step 05).

Variables left from older versions are ignored like any unknown `EARMARK_*` key.

## Deployment

Step 16; exact files in `docs/steps/16-deploy-shortcut.md` section 4, operator guide in
`docs/deploy.md`.

- **Image.** The `Dockerfile` has stages `uv`, `build`, and `runtime`.
  - Base images: `python:3.13.18-slim-trixie` and `ghcr.io/astral-sh/uv:0.13.9`, pinned
    by digest. The Python tag follows `.python-version`; the uv minor follows the
    `uv_build` bound.
  - Dependencies: `uv sync --locked --no-dev` plus the extras in the build argument
    `EXTRAS` (default `transcribe`), with the project installed non-editable into
    `/app/.venv`.
  - User: `earmark`, with UID and GID from build arguments (default 1000, 0 refused).
    `/data` is owned by it.
  - Environment: `EARMARK_HOST=0.0.0.0`, `EARMARK_DATA_DIR=/data`,
    `HF_HOME=/data/huggingface` (so Whisper models persist on the volume),
    `HF_HUB_DISABLE_TELEMETRY=1` (Hugging Face telemetry off, step 21; see "Model
    downloads" under Security).
  - Entrypoint `earmark`, default command `serve`.
  - It installs `ca-certificates`, and has no `HEALTHCHECK`, no `VOLUME`, and no `.env`.
  - `.dockerignore` is an allow-list: `pyproject.toml`, `uv.lock`, `.python-version`,
    `README.md`, `src/`.
- **Compose.**
  - `api` (`serve`) and `worker` (`worker`) share the image (built with
    `EXTRAS: ${EXTRAS:-transcribe}`) and the named volume `data` at `/data`.
  - Settings come from `env_file: .env`. `environment:` forces the in-container
    `DATA_DIR`, `HOST`, `PORT`, an empty (so default) `ASR_MODEL_DIR`, and
    `HF_HUB_DISABLE_TELEMETRY`.
  - `api`, `worker`, and `asr` set `HF_HUB_DISABLE_TELEMETRY: ${HF_HUB_DISABLE_TELEMETRY:-1}`
    (step 21). Compose interpolates it from the shell or `.env`, so a
    `HF_HUB_DISABLE_TELEMETRY=0` line in `.env` turns telemetry back on, and an empty or
    missing value keeps it off. A literal `"1"` would beat `env_file` and could only be
    overridden by editing `compose.yaml`.
  - Exposure: only `api` publishes a port, `${API_BIND:-127.0.0.1:8787}`. Hardening:
    `init: true`, `no-new-privileges`, `cap_drop: ALL`. `api` has a healthcheck that
    fetches `/healthz`.
  - The worker gets `stop_grace_period: 2m`. SIGTERM releases the running job, or lets a
    delivery in progress finish (at most `EARMARK_SMTP_TIMEOUT_S`, 90 s, plus a 30 s
    margin; step 25).
  - The `gpu` profile adds `asr`: NVIDIA's arm64 CUDA 13 vLLM container (pinned by digest)
    serving `EARMARK_ASR_MODEL`, with its API key from `EARMARK_ASR_API_KEY`, raised
    audio limits, a GPU reservation, and no published port. The worker uses it with
    `EARMARK_ASR_BACKEND=remote` and `EARMARK_ASR_BASE_URL=http://asr:8000/v1`.
  - Compose-only variables (`API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
    `ASR_GPU_MEMORY_UTILIZATION`, `HF_HUB_DISABLE_TELEMETRY`) are unprefixed, so `Settings`
    ignores them. `HF_HUB_DISABLE_TELEMETRY` is read by Hugging Face's libraries inside the
    containers, not by earmark.
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
  `ProtectSystem=strict`, and `ReadWritePaths=/srv/earmark/data`; `TimeoutStopSec` is 30
  for the API and 120 for the worker. Both set `Environment=HF_HUB_DISABLE_TELEMETRY=1`
  (step 21). A `systemctl edit` drop-in with `Environment=HF_HUB_DISABLE_TELEMETRY=0`
  overrides it. A line in `/srv/earmark/.env` does not, because earmark reads that file
  itself and never exports it. Commands run by hand outside the units do not get the
  setting.
- **Not verified.** No container has run in the build sandbox (no Docker). CI builds the
  image and runs only one-off commands in it. The `gpu` profile has never been built or
  run. A CUDA build of CTranslate2 in the image is documented in `docs/deploy.md` as an
  unverified outline, not provided; step 26 plans it.

## Security

The service runs on a private network, but it fetches URLs that third parties control
and handles text they wrote.

- **Requests: Apple links** (step 24). An Apple Podcasts link (`podcasts.apple.com`, legacy
  `itunes.apple.com`, exact match) is parsed into its ids. A show link without `?i=` is
  rejected with a clear message. The API stores the canonical URL it built from those ids,
  never the client's string, so re-shares dedupe and no client-controlled URL reaches the
  resolver. The resolver accepts only Apple Podcasts episode links, and the runner calls it
  only for them.
- **Requests: direct URLs.** Any other client URL is a direct audio URL. The guarded fetch
  (`fetch.py`, below) is its only path, as `AudioFetcher.download` in the worker. So it
  gets http(s) only, no credentials (also refused at the API), the private-address guard
  on every connection and redirect, the `MAX_AUDIO_MB` cap, and the textual-type refusal.
  The handler makes no outbound request.
- **Requests: uploads.**
  - Auth comes first. Uploads are authenticated before the body is read: the middleware
    answers 401 without calling `receive`.
  - They are streamed to `data/uploads/` under `EARMARK_MAX_UPLOAD_MB`, checked against
    the declared length and the bytes read. They are never held whole in memory and never
    written to the system temp dir.
  - They are deleted when the request is rejected at any point, or when the job ends.
    Leftovers are swept at startup.
  - The upload's file name is untrusted. It is cleaned (base name only, control and format
    characters removed, 255 characters) and used only as a title fallback. The staged file
    is named by a random id, never by the client.
- **Untrusted request text.** Client-supplied URLs, uploaded bytes, upload file names,
  titles and instructions are all untrusted. Anything that becomes a file name or YAML goes
  through `earmark.sanitize`. Validation errors never echo input.
- **The recipient allowlist is the gate** (step 24).
  - Every `to` is validated as one ASCII address, which leaves no room for CR, LF, comma,
    space, quotes or angle brackets. It is then checked against
    `EARMARK_ALLOWED_RECIPIENTS`, exact on the local part and case-insensitive on the
    domain.
  - Unset or empty refuses every request.
  - `all` is the operator's written acceptance of a risk. Anyone holding the API token can
    then have earmark mail anyone, and can instruct both PKAs. `serve` logs a
    WARNING at startup, and doctor reports it. earmark never defaults `to`.
- **Leak rules** (step 24). Each has a test that captures the log, the push or the API
  response (`tests/test_leaks.py`, `tests/test_api.py`):
  - `instructions` never appears in logs or pushes;
  - `to` appears in logs only masked: the first character of the local part, then `…@`,
    then the domain (`recipients.mask_address`);
  - a direct URL appears in logs and pushes host-only;
  - pushes carry no instructions, no URL path or query, and no address except in masked
    form;
  - the API's `JobView` shows `to` in full, `instructions` cut to 200 characters, a direct
    URL as scheme, host and path only, and scrubbed errors.

  earmark's one enqueue log line names the source (the canonical Apple URL, `audio URL on
  <host>`, or `upload of <n> bytes`), the masked `to`, and whether instructions were given.
- **Leak rules for delivery** (step 25), each tested (`tests/test_leaks.py`,
  `tests/test_delivery.py`):
  - No job error, push or log line contains the recipient address unmasked, or the SMTP
    server's reply text. The reply text echoes addresses, so errors carry codes only.
  - `EARMARK_SMTP_PASSWORD` never appears in logs, job errors, pushes, `earmark config` or
    doctor output.
  - The guid appears only inside the attachments: not in the body, the subject or a push.
- **Untrusted text in mail** (step 25).
  - Titles, show names, upload names, URL path segments, feed guids and transcripts reach
    the attachments only, through `mail.note.contract_text` and `yaml_quote`. The one
    exception is the cleaned title in the subject.
  - Instructions reach only the instructions attachment, verbatim, base64-encoded.
  - The body is a fixed ASCII template (job id and slug file names), and says that the
    subject and the note are third-party text.
  - Header values are cleaned of control and separator characters, and `=?` is defused.
    The `email` package also refuses CR or LF in a header value. So no header can be
    injected.
  - The envelope has exactly one recipient, the validated `to`. Delivery checks it against
    the allowlist again before sending.
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
    `httpcore`, `httpx2`, `httpcore2`, `urllib3`, and `huggingface_hub` (and their
    children) are dropped by a handler filter, and the loggers' levels are also set to
    WARNING or above. These loggers print request URLs and redirect `Location` headers
    (relative paths a URL pattern cannot recognise). The filter is what holds: libraries
    reset their own levels at import (`HF_HUB_VERBOSITY` sets `huggingface_hub`'s, which
    earmark imports lazily, after logging is configured). `LOG_LEVEL=DEBUG` does not show
    these records.
  - *Third-party text is scrubbed of URL paths.* For every record from a logger outside
    `earmark.*`, and for the traceback and stack text of every record, `log.scrub_urls`
    rewrites each `scheme://[userinfo@]host[:port]/path?query#fragment` to
    `scheme://host[:port]/…` and drops the query from an HTTP request line
    (`"GET /jobs?status=failed&limit=5 HTTP/1.1"` becomes `"GET /jobs HTTP/1.1"`). So a
    `huggingface_hub` retry warning keeps its host and reason, uvicorn's access line keeps
    the method, route, and status but not the query, and an unexpected library
    exception logged by the runner loses its URL path. earmark's own messages are left as
    written: they name hosts only, except the canonical Apple URL that the enqueue line
    logs, which earmark builds from parsed ids.
  - *Libraries do not print around the handler.* `huggingface_hub` installs its own stderr
    `StreamHandler` on its logger at import and still propagates, so each of its warnings
    printed twice, once unformatted and unredacted. `log.adopt_library_loggers()` removes
    every non-`NullHandler` from the loggers in `log.LIBRARY_HANDLER_LOGGERS`
    (`huggingface_hub`). It runs at the end of `configure_logging` and right after each
    lazy import that can load `huggingface_hub` (`transcribe.faster_whisper.load_runtime`
    and `model_cache_status`).
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
  `Authorization` header. The response is untrusted text, validated before use. The ntfy
  server (step 15, `EARMARK_NTFY_URL`) is the second operator endpoint, reached by its own
  httpx2 client in `notify/ntfy.py` under the same rules (responses capped at 64 KiB),
  because it may be public ntfy.sh or a self-hosted server on a private address. Both
  share `endpoint.error_detail` and `endpoint.host_label`. `earmark doctor --online`
  reaches both read-only, and the iTunes lookup through `fetch.py`, guard included.
- **The SMTP server** (step 25, `EARMARK_SMTP_HOST`) is the third operator endpoint,
  reached by `smtplib` in `mail/smtp.py`, without the address guard.
  - **TLS.** It is always TLS-protected, with `ssl.create_default_context()`, so the
    certificate and host name are verified. STARTTLS is mandatory in `starttls` mode, and
    there is no plaintext mode.
  - **AUTH** happens only after TLS.
  - **No `socket.getfqdn()`.** The EHLO name is the From domain.
  - **No debug output.** `set_debuglevel` is never called, so smtplib prints nothing.
  - **Bounded.** One session is bounded by `EARMARK_SMTP_TIMEOUT_S`.
  - **Replies are untrusted**, and only their codes are kept.
- **Notifications** (step 15). Episode and show titles come from feeds, and `job.error` and
  warnings can quote them.
  - Every value is cleaned before it is sent (control, format, and separator characters
    removed, whitespace collapsed, RFC 2047 `=?` defused, byte-capped) and redacted.
  - Values travel only in the JSON body, never in a header, the URL, or a query string,
    so there is no header injection.
  - Notifications carry no transcript text, and no secret, topic, URL path or query,
    instructions, recipient address, or file path. There is no `click` link. A direct URL
    job is labelled by its host only (step 24).
- **Untrusted text to names and YAML.** `earmark.sanitize` (`clean_unicode`, `fit_bytes`,
  `fit_chars`, `file_component`, `yaml_quote`, `emit_frontmatter`; standard library only,
  step 22). The ntfy client uses `clean_unicode` and `fit_bytes`; the note and the
  instructions file are rendered with `yaml_quote` and `emit_frontmatter` (step 25). File
  names in mail are slugs (`[a-z0-9-]`), not `file_component`.
- **Model downloads.** faster-whisper fetches models from Hugging Face with
  `huggingface_hub`, not through `fetch.py`: the repository is chosen by the operator
  (`ASR_MODEL`), not by a third party, and it honours the usual `HF_*` and proxy
  environment variables. Weights are CTranslate2 binaries plus JSON and text; nothing is
  unpickled. A cached model loads offline.
  - *Tokens.* earmark never asks for, reads, or passes a Hugging Face token; public models
    download anonymously. `huggingface_hub` itself uses `HF_TOKEN` or its token file if
    the operator provides one. The Hub answers anonymous requests with an `X-HF-Warning`
    header ("You are sending unauthenticated requests to the HF Hub…"), which
    `huggingface_hub` logs at WARNING once per process; it reaches the log once, through
    earmark's handler (step 20).
  - *hf_xet.* Xet-backed files (Whisper `model.bin`) are fetched by `hf_xet`, whose Rust
    logger writes JSON lines at INFO to its own files under `$HF_HOME/xet/logs/` (default
    `~/.cache/huggingface/xet/logs/`), outside earmark's handler. In a real download of
    `tiny` (step 20, 2026-11-21) those files held hosts, request ids, hashes, and local
    paths, but no URL path or query. hf_xet prunes them itself (at most 250 MB, 14 days).
    earmark leaves the logs where the library puts them, which is inside the data
    directory wherever earmark sets `HF_HOME`. `HF_XET_LOG_DEST` moves them to another
    directory (trailing `/`) or file, and an empty value is ignored (step 21).
  - *Telemetry, off by default* (step 21, the operator's decision). Unless disabled, two
    things are sent. hf_xet POSTs a `xet_download_summary` to the CAS server's
    `/v1/telemetry` after each Xet download. `huggingface_hub` fetches
    `/api/agent-harnesses` once a day per `HF_HOME` and adds the torch version and any
    detected agent to its User-Agent. `HF_HUB_DISABLE_TELEMETRY=1` stops all three (tested
    live with hf_xet 1.8.0 and huggingface_hub 1.33.0). hf_xet reads the process
    environment directly, so the variable must be set in the environment, not patched
    into `huggingface_hub.constants`. `DO_NOT_TRACK=1`, `DISABLE_TELEMETRY=1`, and the
    xet-only `HF_XET_TELEMETRY_ENABLED=false` also stop the summary. earmark sets only
    `HF_HUB_DISABLE_TELEMETRY=1`, in the image, the compose services, and the systemd
    units (see Deployment). Setting it to `0` (or leaving it empty outside compose) turns
    telemetry back on, unless one of the other variables is set. earmark's Python code does
    not set it.
- **Secrets.** `API_TOKEN`, `ASR_API_KEY`, `NTFY_URL`, `NTFY_TOKEN` and `SMTP_PASSWORD`
  (step 25) are `SecretStr`,
  redacted in `earmark config` and `earmark doctor`, and never logged. `EARMARK_NTFY_URL`
  is a `SecretStr` because on public ntfy.sh the topic is the password. Doctor prints only
  its host. `Settings.secret_values()` also returns the bare topic when it is at least 12
  characters (step 16), so a job error or log line that quotes only the topic is redacted
  too. Shorter topics are left alone because masking a common word (`earmark`) would
  corrupt every log line; doctor redacts the topic at any length.

## Testing

`make check` runs ruff, ruff format check, mypy (strict), and pytest. The default suite
needs no network, GPU, or credentials; `pytest-socket` blocks sockets so a test that
reaches out fails. Unix-domain sockets stay allowed (`--allow-unix-socket`) because the
asyncio event loop, and so FastAPI's `TestClient`, needs a socketpair. Tests carrying
`network`, `slow` or `smtp` get sockets back automatically (`tests/conftest.py`). The
default `-m` expression deselects all four opt-in markers; passing `-m` on the command line
replaces it, so `-m network` runs only network tests. Opt-in markers:

- `network`: hits real Apple and feed endpoints. These tests call
  `tests/http_helpers.dns_intercepted()` and pass `allow_private=True` when the local
  DNS answers non-public addresses (the dev sandbox, Known risk 5).
  `tests/test_remote_asr_live.py` is also `network`: it transcribes the `slow` tests' clip
  on a real ASR server and is skipped unless `ASR_TEST_BASE_URL` (with `ASR_TEST_MODEL`,
  optional `ASR_TEST_API_KEY`) is set, so CI skips it and the operator runs it on the
  homelab. `tests/test_ntfy_live.py` (step 15) is `network`: it publishes one notification
  and polls it back, to `NTFY_TEST_URL` (optional `NTFY_TEST_TOKEN`) or else to a fresh
  random topic on public ntfy.sh.
- `slow`: downloads the `tiny` Whisper model and a public-domain clip (JFK's 1961
  inaugural address, 11 s, pinned by commit and sha256 in `tests/slow_assets.py`) into
  `$SLOW_TEST_CACHE` (default `~/.cache/earmark-tests`), then transcribes it on CPU.
  Needs the `transcribe` extra and fails (does not skip) without it.
  `tests/test_log_libraries.py` is `slow` too (step 22): it drives `huggingface_hub`
  through `httpx.MockTransport` and `httpx`/`httpcore` against `tests/uds_http.py`, with
  sockets blocked, and checks that no URL path or query reaches earmark's log output; the
  default suite does the same for `httpx2`/`httpcore2` and uvicorn.
- `gpu`: needs CUDA; it does not get sockets back.
- `smtp` (step 25): `tests/test_smtp_live.py` logs in to a real SMTP server and sends one
  message, to `SMTP_TEST_TO`, from the `SMTP_TEST_*` settings. Those are separate names
  because `conftest` strips `EARMARK_*`. It is skipped unless they are all set, gets
  sockets back, and no workflow runs it. Only the operator runs it.

The default `-m` expression deselects `network`, `slow`, `gpu` and `smtp`.

For every test without `network` or `slow`, `tests/conftest.py` sets `HF_HUB_OFFLINE=1`,
so no default test can download a model even if the extra is installed. Every test starts
with no `EARMARK_*` variables, in its own temp directory.

The remote ASR tests use `httpx2.MockTransport`, a recording transport that iterates the
request body (to prove streaming), and `tests/asr_stub.py`, a threaded stdlib HTTP server
on a unix socket reached with `httpx2.HTTPTransport(uds=...)`, which the socket block
allows. The ntfy client (step 15) is tested with `httpx2.MockTransport` and
`tests/ntfy_stub.py`, a unix-socket ntfy server with recorded ntfy.sh bodies. Doctor is
tested through `doctor.Probes`: injected transports, an injected `GuardedFetcher`, and
missing extras simulated with `sys.modules` stubs. `tests/legacy_db.py` builds the
v0.1.0-shaped database for the refusal tests.

Upload tests (step 24) use two helpers:

- `tests/asgi_helpers.call_asgi` drives the ASGI app directly. It delivers scripted
  `http.request` chunks, optionally disconnects, runs a hook before each `receive`, and
  counts the calls.
- `tests/multipart_helpers.multipart_body` builds exact multipart bytes in any part order.

`TestClient` reads a request body whole and sends it as one message, so it cannot test
streaming, a lying `Content-Length`, a mid-body disconnect, or that `receive` was never
called.

Delivery tests (step 25) run against `tests/smtp_stub.py`. It is a hand-written SMTP server
on a `socket.socketpair()`, served from a thread, so it works with sockets blocked. The
stdlib `smtpd` is gone in 3.13.

- **TLS.** It speaks real TLS, implicit or after STARTTLS, with the committed test
  certificate for `smtp.test` in `tests/fixtures/tls/`. The client trusts it through an
  injected `ssl.create_default_context(cadata=…)`.
- **Scripting.** Every reply can be scripted, and so can a disconnect at each phase, a stall
  and a dripping reply.
- **Recording.** It records the EHLO names, the commands with their TLS state, the AUTH
  credentials, the envelope and the message bytes.
- **Exact bytes.** Message tests compare exact bytes, with an injected date, job id and
  boundary. The leak tests configure earmark's real handler on a `StringIO` at DEBUG, and
capture ntfy bodies with `httpx2.MockTransport`.

Documents are tested against the code (step 16).

- **Markers.** `tests/doc_helpers.py` extracts content by HTML-comment markers
  (`<!-- name:begin -->` … `<!-- name:end -->`, or a single `<!-- name -->` before a
  fence) and plain regular expressions; there is no Markdown library.
- **What `tests/test_docs.py` checks.**
  - The README's settings reference lists every `Settings` field exactly once, one
    variable per table row.
  - The README quickstart sets all five backends to `fake`.
  - `docs/shortcut.md`'s example request body validates against `TranscriptionRequest`
    and is accepted by `POST /transcriptions`, and the example response matches the route
    (step 24).
  - The Shortcut's actions appear in a fixed order.
  - Relative links in the README, `docs/deploy.md` and `docs/shortcut.md` resolve.
  - The README's command table names every CLI command, and its HTTP API sentence names
    every route of `build_router` (step 17).
  - Real share-sheet captures in `tests/fixtures/share_sheet.json` canonicalise as
    recorded.
  - The deploy guide's reachability and telemetry sections say what they should.
  - This file contains `HF_HUB_DISABLE_TELEMETRY=1` and `HF_HUB_DISABLE_TELEMETRY=0`.
- **What `tests/test_deploy.py` checks**, statically: the Dockerfile, `.dockerignore`,
  `compose.yaml`, and the two systemd units, including that each one sets
  `HF_HUB_DISABLE_TELEMETRY` (step 21).
- **The gate.** `ruff format --check .` also formats Python code blocks in Markdown, so
  those must be ruff-clean.

With every backend set to `fake` and `EARMARK_ALLOWED_RECIPIENTS` set, the whole service
runs in the sandbox:

1. `earmark serve`;
2. `curl` a `POST /transcriptions`, with a podcast link, a direct URL (the fake fetcher
   serves it), or a multipart upload;
3. `earmark worker --once`;
4. `GET /jobs/{id}` shows the job `done` with `result.duration_s`, `delivery_state` `sent`
   and `sent_at`. The transcript is cached under `data/episodes/<key>/`, and `data/uploads/`
   and `data/downloads/` are empty. The fake mailer's message is `data/outbox/<job_id>.eml`,
   and its note's `transcript_sha256` verifies (step 25).

Verifiers use this as the live smoke test from step 05 onward.

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and `impl` and on every pull
request. One job on a GitHub-hosted `ubuntu-24.04-arm` runner (linux/aarch64, the
deployment architecture; free for public repositories) checks out the code, installs uv
with `astral-sh/setup-uv` (uv cache and the managed Python cached, keyed on `uv.lock`),
then runs exactly `uv sync --locked` and `make check`. Python comes from
`.python-version` through uv, never from the workflow. A newer push to the same ref
cancels the run in progress, and the job times out after 15 minutes.

CI runs the default suite only. It does **not** run the opt-in markers (`network`, `slow`,
`gpu`, `smtp`), does not install the `transcribe` extra, never runs on a GPU, and does not build or
run the container. There are exactly three workflows
(`tests/test_ci.py::test_workflow_files_are_the_known_three`):

- `ci.yml`, above.
- `network.yml` ("Live tests") runs every Monday at 05:23 UTC and on `workflow_dispatch`,
  with `contents: read` and no secrets, in two jobs: `network` (step 07) runs `uv run
  --locked pytest -m network -v -rs` with the same pinned actions and setup as `ci.yml`;
  `slow` (step 08) runs `uv sync --locked --extra transcribe` and `uv run --locked --extra
  transcribe pytest -m slow -v -rs`, with setup-uv's `cache-suffix: slow` and a pinned
  `actions/cache` step for `~/.cache/earmark-tests` (the Whisper model and the clip).
  GitHub runs `schedule` and `workflow_dispatch` only for workflow files on the default
  branch, so it starts once the file is on `main`.
- `container.yml` ("Container image", step 16) runs on the same pushes and pull requests
  as `ci.yml`, with `contents: read`, no secrets, and the same pinned checkout as its only
  action. Its job `docker build (linux/arm64)`:
  - validates `compose.yaml` with `docker compose config` (with and without `--profile
    gpu`) against a `.env` copied from `.env.example`, and checks the telemetry variable
    in all three services;
  - builds the image with plain `docker build` (no push, no login, no Docker actions);
  - runs `earmark --version`, an import of `ctranslate2` and `faster_whisper`, an `id -u`
    check, and a check of `HF_HUB_DISABLE_TELEMETRY` in the image.

  It never starts the API or the worker.

No workflow references secrets (`test_no_workflow_references_secrets`). None of the
scheduled or container workflows is a required check: their failures usually mean an
outside service changed, or, for the container, that the image needs a fix.

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
   Step 26 plans the in-image CUDA build from
   `docs/decisions/2026-11-23-gpu-build-recipe.md`.
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
3. **Wheels on aarch64 and Python 3.13** for `ctranslate2`, `av` and `faster-whisper`
   (the `transcribe` extra). Resolved in step 01 (2026-11-19): cp313 manylinux aarch64
   wheels install with `--only-binary :all:` and import on this sandbox. Python is pinned
   to 3.13. See `docs/steps/01-scaffold.md`.
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

## Re-scope decisions (2026-11-23)

These are the operator's decisions and the inputs that re-scoped earmark into a
transcription and delivery service. They are copied from `docs/PLAN.md` "Re-scope", and a
revision changes both files in the same commit. Each bullet says where it was built.

### What changes

earmark stops analysing and storing knowledge. It becomes a transcription service on the
DGX Spark, and the operator's PKA instances do the analysis and filing, each with its own
context. earmark's job is:

1. Accept a request carrying one of: an audio file, a direct audio URL, or an Apple
   Podcasts episode link. The request also carries a destination email address, free-form
   instructions for the recipient ("analyse this for …", "file this under …"), and
   optionally a title.
2. Transcribe the audio on the Spark's GPU.
3. Email two Markdown attachments to that address. One is the transcript note, which is
   data. The other holds the requester's instructions. The body carries only text earmark
   generates, and the subject carries only the sanitised, capped title.
4. Send an ntfy push saying the transcript was sent, or why the job failed.

### Operator decisions (2026-11-23)

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
  Built in step 24: see "The recipient allowlist is the gate" under Security. Step 25
  checks it again at delivery.
- **Delivery is at-least-once.** A crash after the SMTP server accepts the message, but
  before earmark records it as sent, cannot be told apart from a failure. In that case
  earmark resends with the same `Message-ID` and the same `job_id`. The PKA treats the
  same `guid` plus the same `job_id` as a duplicate, and ignores it.
  Built in step 25: see "Delivering" under Stage contracts.
- **Re-shares resend the whole transcript.** Email is stateless, and 300 KB is small. A
  re-share is a new job, so it gets a new `job_id` and the same `guid`. The PKA keeps one
  note and runs the new instructions against it. earmark stays send-only and reads no
  mail.
- **Direct URLs are identified by content.** A URL is downloaded again on every request
  and hashed. Transcription, the expensive part, is cached by that hash. Uploads work the
  same way. Podcasts keep their episode identity and their audio cache.
  Built in step 24: see "Episode key" under Domain models and "Order" under Runner and
  worker.
- **Old databases are refused.** No v0.1.0 deployment exists (the operator is at
  runbook phase 0). A database created by `v0.1.0-full-pipeline` makes startup fail with
  a clear message saying to move it aside. It is not migrated.
  Built in step 22: see "Database identity" under Jobs.
- **No "analysis done" relay for now.** ntfy stays for "sent" and "failed".
  Built in step 25: see "Notifying".
- **No PKA-side skill is needed from earmark.** PKA is gaining inbound email.
- **The attachment size cap is configurable.** Built in step 25:
  `EARMARK_MAX_ATTACHMENT_MB`.
- **Removed code is deleted.** It stays recoverable from the `v0.1.0-full-pipeline` tag.
  Done in step 22: see "History: removed in step 22".

### Inputs from the other agents

- amap-spec: mail to an agent's bound address arrives on the mail lane. earmark learns
  nothing beyond the SMTP reply: there is no receipt and no dedupe. Attachment caps are
  set per deployment.
- earshot: its offload is not an API. Its build recipe for CTranslate2 with CUDA on the
  GB10 is in `docs/decisions/2026-11-23-gpu-build-recipe.md`, with what was measured and
  what was not.

## Planned (step 26)

Nothing in this section is built yet. It is what step 26 builds against, restated from
its brief in `docs/PLAN.md`. Its planner may settle what is listed under "Open questions",
and records the choice here. Steps 24 and 25 are built. Their contracts moved from here
into the sections above. `docs/steps/24-transcription-requests.md` and
`docs/steps/25-delivery.md` have the exact rules.

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

### Open questions

- **Settled by step 24** (see `docs/steps/24-transcription-requests.md` section 8):
  - the caps: `instructions` 4000 characters, `title` 200;
  - `EARMARK_MAX_UPLOAD_MB` defaults to 1024;
  - the synthetic show id is `audio`;
  - the job stores its request through migration `0002`, with the column `recipient`;
  - `prompt` became `instructions`, carried over;
  - for uploads and URLs, the resolve stage builds the episode from the job and the bytes;
  - the dedupe window applies to `done` jobs only.
- **Settled by step 25** (see `docs/steps/25-delivery.md` section 8):
  - **The time zone.** It is the offset of the request's `timestamp`, stored as
    `utc_offset_min`, or UTC when there is none. No setting and no zone database. The
    Shortcut must send `timestamp` (step 27), and `earmark submit` sends none.
  - **The `published` date** is shown in the requester's offset, with Apple's date-only
    values kept.
  - **The feed guid.** It is the matched feed item's `<guid>`, in the new
    `Episode.feed_guid`, used as `feed:<guid>` only when verbatim and at most 300
    characters. Otherwise it is `apple:<episode id>`. Episode keys are unchanged. An
    episode that drops out of its feed between two shares changes from `feed:` to
    `apple:`.
  - **Names.** The stage is `deliver`. The columns are `utc_offset_min`, `delivery_state`,
    `message_id`, `sent_at` and `resent` (migration `0003_delivery`).
  - **Defaults.** `EARMARK_SMTP_PORT` defaults to 587 for `starttls` and 465 for `tls`.
    `EARMARK_SMTP_TIMEOUT_S` defaults to 60, at most 90. `EARMARK_MAX_ATTACHMENT_MB`
    defaults to 10.
  - **Paragraphs.** A new one starts on a speaker change, once the current one spans 30 s,
    or before a segment that would take it past 60 s.
  - **Push wording.** "Sent: …" with `To:` masked, and the resent and
    may-already-be-delivered lines.
  - **Still open:** the contract is final only once the operator approves the PKA's
    receiving procedure.
- **Step 26:** the base image, the slim `nvidia/cuda` devel and runtime pair (nobody has
  tried it) or the NGC base (earshot proved it), with the reason; whether CI builds the GPU
  image at all, given the free runner's roughly 14 GB of disk; whether it is a separate
  file or a build target.

### Not planned

`docs/PLAN.md` "Backlog" lists what the design leaves room for: a send-rate cap, re-shares
as a reference only, an "analysis done" push, speaker diarization, other sources, and an
async transcription API for other callers. None is designed here.

## History: removed in step 22

- Steps 01–21 built a pipeline that also analysed each transcript with Claude or a local
  LLM, wrote episode, transcript and entity notes into an Obsidian vault, indexed them in
  LanceDB for search, re-ran prompts on stored episodes, and wrote a weekly digest. It is
  tagged `v0.1.0-full-pipeline`; `git show v0.1.0-full-pipeline:docs/ARCHITECTURE.md`
  shows its design.
- Step 22 (`docs/steps/22-rescope-remove.md`) deleted:
  - the `analyze/`, `vault/`, `index/` and `digest/` packages and `episodes.py`;
  - the `anthropic`, `lancedb`, `fastembed` and `tzdata` dependencies and the `index`
    extra (PyYAML moved to the dev group);
  - the routes `GET /search`, `GET /episodes` and `POST /episodes/{key}/analyze`;
  - the commands `analyze`, `reanalyze`, `episodes`, `digest`, `entities rebuild`,
    `search` and `reindex`;
  - the `reanalyze` job kind;
  - the `llm` and `index` markers and workflows;
  - the digest systemd service and timer, and `docs/llm.md`;
  - the settings `VAULT_DIR`, `VAULT_SUBDIR`, `OBSIDIAN_VAULT_NAME`, `LLM_PROVIDER`,
    `STORE_BACKEND`, `INDEX_BACKEND`, `LLM_MODEL`, `LLM_EFFORT`, `LLM_FALLBACKS`,
    `LLM_API_KEY`, `LLM_CONTEXT_TOKENS`, `LLM_BASE_URL`, `LLM_TIMEOUT_SECONDS`,
    `LLM_RESPONSE_FORMAT`, `EMBED_MODEL`, `EMBED_MODEL_DIR` and `TZ`.
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
