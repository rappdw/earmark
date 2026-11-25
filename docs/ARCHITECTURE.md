# Architecture

This repo holds a small system in three parts, the iOS Shortcut, the service and the
receiving skill, joined at two seams. "System" below says how they fit. The rest of this
document describes part 2, the service.

## System: three parts and two seams

One repo holds the whole system. That is the operator's decision of 2026-11-24: the project
is too small to split, one plan and one runbook cover the system, and a change across a seam
lands in one commit. AMAP, the mail system that carries the service's mail to each PKA, is
outside this repo.

### The three parts

| Part | What it is | Runs on | Lives in | Built and run |
|---|---|---|---|---|
| 1. The Shortcut | The iOS Shortcut that sends audio or a link to the service | the operator's iPhone | `docs/shortcut.md`, which says how to build it by hand; `shortcut/earmark.shortcut` from step 31 | Never run on a phone. The file is not built (step 31). |
| 2. The service | API, worker, GPU transcription, mail delivery | the DGX Spark | `src/earmark/`, with its deployment files at the top level | Built in steps 01–27 and 32 (a request may also be a YouTube video link) and tested in CI on fakes and stubs. Never run on the Spark: the GPU image has never been built, and no real mail has been sent. |
| 3. The receiving skill | Handles an earmark email that AMAP has delivered: checks it, files the note, acts on the instructions within a narrow scope | each PKA instance the operator chooses | `plugins/earmark-pka/`, listed in this repo's plugin marketplace (steps 29, 30) | Built in step 30 (version 1.1.0 since step 32): the `earmark-pka` plugin, its marketplace and the skill, tested on the contract 1 and contract 2 samples under CPython 3.9.6 and 3.13. It has never run in a PKA (runbook phase 12). |

### The two seams

| Seam | Between | Contract | Tested by |
|---|---|---|---|
| Request | Shortcut → service | `POST /transcriptions`: the request model in code, and "HTTP API" below | the service's API tests (`tests/test_api.py`), and the doc tests that post `docs/shortcut.md`'s JSON example and upload fields to the app; no Shortcut has sent one yet |
| Mail | service → AMAP → receiving skill | the mail contract: `contract: 2` (step 32), in `contract/mail-v2.md`, which the service sends; and `contract: 1`, in `contract/mail-v1.md`, which the receiving skill still accepts ("The mail contract" below) | the sample emails in `contract/samples/v2/` (step 32), which the service's tests reproduce byte for byte from the real mail code, and `contract/samples/v1/` (step 29, frozen since step 32 and pinned by hash); the checker passes on both on CPython 3.9.6 and 3.13; the receiving skill's tests (step 30), which run its recognising, checking and de-duplicating over every sample |

- The request seam has no folder: its contract is the request model in code plus this
  document.
- The mail seam's folder, `contract/`, covers that seam only (step 29).

### The mail seam

`contract/` (step 29) holds the seam: `mail-v1.md` is the `contract: 1` text and `samples/v1/`
holds its ten sample emails; `mail-v2.md` is the `contract: 2` text (step 32), written as a
list of changes to contract 1, and `samples/v2/` holds thirteen samples; `README.md` says
what the seam is and what its checker cannot check. The service sends `contract: 2` for all
mail. The v2 samples are built by the real `compose_mail` from the fixed inputs in
`tests/contract_samples.py`; `make samples` regenerates them, and a test fails when the mail
code's output differs from one by a byte. The v1 samples can no longer be rebuilt (the mail
code sends version 2), so they are frozen: a test pins each file's sha256, and `make
samples` never writes that folder. They are wire format (CRLF), so `.gitattributes` keeps git
from converting them. A revision is a new `mail-vN.md` with its own `samples/vN/`; the older
files stay, because mail sent under them can still be waiting in an inbox. The checker and
the receiver accept both versions.

The contract checker has one copy, in `plugins/earmark-pka/skills/earmark-receive/scripts/`
(the plugin and the skill around it are step 30's, and the receiver beside it imports it;
a test enforces the one copy). It is two files: `check_mail.py`, a launcher in old syntax whose version guard
prints a clear message on a Python older than 3.9, and `earmark_mail_contract.py`, the
checker, standard library only and written to Python 3.9. It runs as `python3 -I` in three
forms: `message` checks a whole `.eml`, `files` checks the parts a PKA's inbox tools return
(it never creates a file under an untrusted name, and takes subject and body from files),
and `rules` lists the rules. It checks every mechanically checkable rule of the contract
and exits 0 (every rule that ran passed), 1 (a rule failed, or the checker itself failed),
2 (a prerequisite is missing) or 64 (usage), as pka-tools does. Its output never quotes
text from the mail. What it cannot check is listed in `contract/README.md`.

The 3.9 floor is enforced by the gate: ruff lints `plugins/` at `py39` through a per-file target version, mypy 1.20.2
type-checks it at `--python-version 3.9` from its own locked project in `tools/py39/`
(mypy 2 cannot target 3.9, so that pin is never bumped), and every checker test runs under
CPython 3.13, 3.9.6 and 3.8 (3.8 only for the version guard). The interpreters come from
`uv python install --no-bin`, which `make check` runs.

### How mail reaches a PKA

AMAP is outside this repo, and earmark only relies on it.

1. The service sends ordinary SMTP email to the PKA instance's email address. earmark is not
   an AMAP fleet member, just an outside sender.
2. AMAP takes the email delivered to that address and provides it to the PKA through AMMP
   (the Agent Mediated Messaging Protocol). The AMAP router applies the sender allowlist and
   DMARC. Attachments are readable (disposition `clean`) only for an allowlisted sender that
   passes DMARC.
3. Inside the PKA's sandbox, AMAP rings a content-free doorbell in the live session: "you
   have mail; use the inbox read tool". With no session running, the mail waits for the next
   one.
4. The PKA reads the message and its attachments with the `inbox` tools. The receiving skill
   takes over from there (step 30; "The receiving skill" below).

- **Intake is AMAP's job.** No earmark code reads, polls or selects mail. The service sends
  plain email, embeds no agent, and is deterministic apart from ASR output.
- **The sender allowlist is AMAP router config**, set by the operator for each PKA's address.
  It is not a PKA's responsibility, and not something to ask a PKA for. earmark cannot see
  it: SMTP's reply is all it learns.
- **AMAP's attachment limit is not known yet.** `EARMARK_MAX_ATTACHMENT_MB` (10) must not
  exceed it. If AMAP counts the whole message, base64 makes the message about 1.37 times the
  attachments. Runbook phase 6 checks it.
- **Trust.** earmark is not an enrolled peer, so the `inbox` tools' strict rule applies to
  its mail. The receiving skill acts on the instructions only within the narrow scope the
  operator granted as an explicit exception (`docs/PLAN.md` "Trust"), and the transcript note
  is always data. That rests on the sending address being used for nothing but earmark.

### The receiving skill

The plugin is `plugins/earmark-pka/`, listed by `.claude-plugin/marketplace.json` (marketplace
`earmark`, plugin `earmark-pka`, version 1.1.0 in its `plugin.json`, which accepts
`contract: 1` and `contract: 2`). It holds exactly six
files: `plugin.json`, `skills/earmark-receive/SKILL.md`, the checker's two files, and the
receiver's two (`receive.py`, a launcher in old syntax, and `earmark_receive.py`). A plugin
installs only its own folder, so everything the skill runs is in it.

- **Install.** At project scope on each chosen PKA, from the PKA's workspace:
  `claude plugin marketplace add rappdw/earmark --scope project` and
  `claude plugin install earmark-pka@earmark --scope project`. The scope is explicit because
  the command line installs at user scope by default, which would enable the skill in every
  instance; the operator ruled that out. Only the workspace's own settings enable it, and it
  loads only after that folder has been trusted once.
- **Updates.** Auto-update is off for a third-party marketplace, and a new copy is fetched
  only when the plugin's version changes. The version is pinned and bumped by hand, and
  `PLUGIN_RELEASES` in `tests/test_plugin.py` holds each release's tree digest, so a change
  to any file in the plugin without a new version fails the gate. The operator then runs
  `claude plugin marketplace update earmark` and
  `claude plugin update earmark-pka@earmark --scope project`; a session picks it up at its
  next start or after `/reload-plugins`, and a session that already loaded the old copy keeps
  it.
- **What the skill does, in order.** A shape check on what `read_message` showed; preflight;
  `read_attachment` for both attachments; `classify`; one path per outcome (`new` files the
  note, `reshare` and `retranscription` find it); acting on the instructions within the
  narrow scope; `record`; `pka-tools log add`; a list of every file changed. It reads, polls
  and fetches no mail, never writes to `team-inbox/`, never runs version control and commits
  nothing. `SKILL.md` states the narrow scope as the operator's explicit exception
  (`docs/PLAN.md` "Trust"); changing that scope is the operator's decision.
- **The receiver.** `python3 -I receive.py` has seven subcommands. `preflight` runs
  `.pka/bin/pka-tools version --json` and needs 1.8.0 or newer, the first release whose
  `post-route` commits nothing. `classify` recognises earmark mail, checks it with the step 29
  checker (the media-type rule is skipped, because AMAP's media types are sender-chosen text),
  and de-duplicates it. `file`, `rewrite`, `move`, `annotate` and `record` are the guarded
  writes: each re-runs `classify` first, accepts only the note that came with the message,
  and `--dry-run` writes nothing. Exit codes are pka-tools': 0, 1 (refused or failed), 2
  (prerequisite missing), 3 (workspace not ready), 64 (usage). Every result is one ASCII JSON
  line, and no output quotes text from the mail.
- **The bytes.** The skill never retypes an attachment. It passes the `path:` of each
  attachment that the `inbox` tool's `read_attachment` returns (the sidecar file holding the
  exact bytes, inbox-lab-vol 0.1), and the receiver reads it; `note.hash` would catch any
  change. Subject and body reach the receiver as files. If a connector gives no `path:`, the
  skill stops.
- **The record.** `.pka/earmark/received.jsonl`, one JSON object per line, appended with an
  fsync: `v`, `at`, `event` (`filed`, `rewritten`, `moved` or `done`), `guid`, `job_id`,
  `message_id` (the inbox id, since `read_message` shows no `Message-ID`), `path`,
  `shared_at` and `transcript_sha256`. It is a cache of what the workspace shows; if it is
  lost, notes are found again by their frontmatter and only the `done` lines and the order of
  bodies are lost.
- **De-duplicating.** A `done` line with the same `job_id` makes the message a duplicate (a
  resend). Otherwise the note is found by guid, first by the record's last path, then by
  scanning the workspace. None found: `new`. Several: `ambiguous`. Found with the same
  `transcript_sha256`: `reshare`, so the instructions are applied again to the existing
  note. Found with a different one: `retranscription`, and the body is rewritten only when
  this request's instructions `shared_at` is later than the request whose body is in the note
  (`rewrite_body` true); an older request keeps the body, and an unknown order is the
  owner's call.
- **Where it writes.** The filed note, written once with exclusive create into a folder the
  receiver checks (never `team-inbox/`, `owner-inbox/`, a hidden folder, a symlink or an
  earshot capture), and `.pka/earmark/`. It starts one process, `pka-tools version`.
- **The manifests** are checked by tests against the documented field tables, since no
  official JSON Schema exists. `claude plugin validate --strict` is run by hand, not by the
  gate, because the CLI is not in CI.

### Definition of done

One episode shared on the phone is transcribed on the Spark, emailed, read by a PKA through
the receiving skill, and filed, with the instructions acted on. The runbook's last phase
(phase 12) is this check.

A step being `done` means its part's code and tests pass; it does not mean the system works.

The system has not met this yet. Part 1's file is not built (step 31), part 3 has never run
in a PKA, and the system has never run end to end.

### Repo layout

<!-- repo-layout -->
```text
.claude-plugin/marketplace.json   the marketplace "earmark", listing ./plugins/earmark-pka
plugins/earmark-pka/              part 3: the plugin
  .claude-plugin/plugin.json
  skills/earmark-receive/SKILL.md
  skills/earmark-receive/scripts/   the contract checker (step 29) and the receiver (step 30)
contract/                         the mail seam only (step 29)
  README.md                         what the seam is, its versions, where the checker lives
  mail-v1.md                        the contract 1 text
  mail-v2.md                        the contract 2 text (step 32): YouTube, `type: video`
  samples/v1/                       sample emails, frozen since step 32
  samples/v2/                       sample emails the mail code reproduces (step 32)
shortcut/earmark.shortcut         part 1: the signed export, the folder's only file (step 31)
src/earmark/                      part 2: the service
deploy/, Dockerfile, Dockerfile.gpu, compose.yaml   part 2: deployment
tests/                            every part's tests, one gate
tools/py39/                       the pinned mypy that checks the checker against Python 3.9 (step 29)
docs/                             architecture, plan, runbook, deploy and Shortcut docs
.github/                          CI: the gate, the live tests, the container build
.claude/                          the build loop's agents and /step (development only)
```

- Planned paths carry the step that creates them; a path without one exists now.
- The service keeps its conventional layout (`src/earmark/` with its deployment files at the
  top level). It is not moved into a part folder.

## The service

The service (part 2) is a self-hosted transcription and delivery service. A request (step
24) carries an Apple Podcasts episode link, a YouTube video link (step 32), a direct audio
URL, or an uploaded audio file,
plus a destination address checked against an allowlist, free-form instructions, and
optionally a title. The worker resolves or downloads the audio and transcribes it, then
caches the transcript. It emails the transcript note and the instructions to the destination
as two Markdown attachments, in the mail contract's format (step 25). Last, it sends an ntfy
push. The operator's PKA instances do the analysis and filing, through the receiving skill
(part 3).

On the DGX Spark the worker transcribes on the GB10 GPU in-process, from a GPU build of the
image that compiles CTranslate2 with CUDA (step 26).

How to read this document:

- "System" describes the whole repo, including parts that later steps build, each marked
  with its step.
- The sections after "The service", up to "Settled questions", describe the service's code
  as it is.
- "Settled questions" records what the planners of steps 24–28 decided, and why.
- "History: removed in step 22" lists what step 22 removed.

When a step changes a contract described here, it changes this file in the same commit.
Steps 01–21 built a larger pipeline, tagged `v0.1.0-full-pipeline`.

## Decisions

The original sketch left several choices open. These are the defaults the plan builds
on. Each is isolated behind config or a stage interface, so reversing one touches a
backend, not the pipeline.

| Question | Decision | How to change it |
|---|---|---|
| Scope | The service transcribes, and delivers the transcript by email (step 25). Its sources are Apple Podcasts episodes, YouTube videos (step 32), direct audio URLs and uploads. It files nothing: the PKA files, through the receiving skill (part 3, steps 29–30). | See "System", "Re-scope decisions" and "Delivering". |
| State | The job table (SQLite, WAL), including each job's delivery state (step 25). The per-episode cache under `DATA_DIR` (`episode.json`, `shared.json`, `transcript.json`, `transcript.txt`, podcast audio while it is needed). Per-job staging files for uploads, direct URLs and YouTube audio (`uploads/`, `downloads/`, deleted when the job ends; steps 24, 32), and Deno's small cache (`cache/deno/`, step 32). earmark is not a system of record. A database from earlier than step 22 is refused, not migrated (see "Database identity"). | A new migration in `db.py`. |
| Delivery | Ordinary email over SMTP submission (stdlib `smtplib`, verified TLS, AUTH) from a sending mailbox on the operator's domain. It carries two Markdown attachments in the mail contract (`contract: 2` since step 32, drafted by pka-skills, owned by this repo; the text is `contract/mail-v2.md`, a list of changes to `contract/mail-v1.md`, which the receiving skill still accepts). AMAP, outside this repo, carries the mail to the PKA. Delivery is at-least-once: a resend keeps the `Message-ID` and `job_id` (step 25). | Implement another `Mailer`. |
| Notification | ntfy, off unless `EARMARK_NTFY_URL` is set: one JSON publish to the server root per finished job, with no link (steps 15, 22, 24, 25). A delivered job's push reads `Sent: <label>`, with the masked recipient and the duration. Works with public ntfy.sh or a self-hosted server on the LAN or tailnet. The Shortcut can also poll `GET /jobs/{id}`. | Implement another `Notifier`. |
| Requests | One route, `POST /transcriptions`, taking JSON or a streamed multipart upload; every request names one `to` from `EARMARK_ALLOWED_RECIPIENTS` (step 24). | See "HTTP API" and `docs/steps/24-transcription-requests.md`. |
| Queue | A SQLite job table and a separate worker process. No Redis. | Swap `JobStore`. |
| Transcription | faster-whisper `large-v3` in the worker process (the `transcribe` extra), with a remote OpenAI-compatible ASR endpoint as the alternative for a server elsewhere (`remote`, step 09; see `docs/asr.md`). The PyPI CTranslate2 wheel for linux/aarch64 is CPU-only (step 08), so the GPU image (`Dockerfile.gpu`, step 26) builds CTranslate2 from source with CUDA for `sm_121` and swaps it into the venv; the compose worker runs it with `EARMARK_ASR_DEVICE=cuda`. faster-whisper's batched pipeline is optional (`EARMARK_ASR_BATCH_SIZE`). | `EARMARK_ASR_BACKEND`, `EARMARK_ASR_MODEL`, `EARMARK_ASR_DEVICE`, `EARMARK_ASR_BATCH_SIZE`, `EARMARK_ASR_BASE_URL`. |
| Reachability | Private network only: the API is never exposed publicly. It listens on `127.0.0.1:8787` (compose publishes it only there). A reverse proxy on the host's LAN interface (Caddy, `deploy/caddy/Caddyfile`) terminates TLS for a name such as `earmark.example.com`. That name is a DNS-only (never proxied) Cloudflare `A` record for the server's private address, with a Let's Encrypt certificate from the DNS-01 challenge; the Cloudflare token lives only with the proxy. Away from home the phone and Mac reach that address through a WireGuard split tunnel. Tailscale Serve is the alternative; `tailscale funnel` is never used (step 16). | See `docs/deploy.md` (step 16). |
| Deployment | Two images. The CPU image (`Dockerfile`, extras by build argument, `transcribe youtube` by default) runs `earmark serve`, or anything on a host without a GPU. The GPU image (`Dockerfile.gpu`: `nvidia/cuda` 12.9.1 cuDNN devel and runtime images, uv-managed Python 3.13, CTranslate2 built for `sm_121`) runs `earmark worker` with `gpus: all`. `compose.yaml` has a shared data volume and no vLLM service or `gpu` profile any more. systemd units for the API and the worker (steps 16, 22, 26). | See "Deployment" below and `docs/deploy.md`. |
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
               youtube: yt-dlp reads the metadata (title, channel, date) → Episode (key = the video id)
   fetch       podcasts: enclosure → data/episodes/<key>/audio.<ext>
               youtube: yt-dlp downloads an audio-only stream → data/downloads/<job_id>.<ext>
   transcribe  audio → transcript.json + transcript.txt (skipped when cached with this ASR profile)
   deliver     render the note and instructions (mail contract) → MIME message (inside the
               interruptible window); then, outside it: delivery_state=sending → SMTP →
               delivery_state=sent + done with JobResult(duration_s), in one write
   finish      delete upload/download/YouTube staging files; notify via ntfy (never fails the job)
```

The API handler makes no outbound network request. It validates, streams an upload to disk,
and enqueues, so the Shortcut gets its 202 as soon as the body has arrived.

`to`, `instructions` and `title` are stored with the job (step 24), and so is the offset of
the request's `timestamp` (step 25). `to` and `instructions` are part of the dedupe key.
After enqueue, only delivery reads `to` and `instructions` (step 25). `title` names uploads
and direct URLs, and is ignored for podcasts and YouTube videos.

## Module layout

These are the service's modules. The whole repo's layout is "Repo layout" under "System".

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
  sources.py           parse_source_url (Apple link, YouTube link or direct URL), display_url, upload_extension,
                       clean_upload_name, audio_title, sha256_file, audio_episode (step 24)
  transcripts.py       a job's cached transcript as the note, the text or the JSON view:
                       read_transcript_file, TranscriptFile, TranscriptView,
                       TranscriptUnavailable, episode_for_job; reads the cache only (the
                       transcript route and `earmark transcript`; step 33)
  staging.py           Staging: data/uploads/ (UploadWriter) and data/downloads/, release at a
                       terminal state, startup sweeps (step 24)
  sanitize.py          untrusted text to file names and YAML, fit_chars, and the frontmatter
                       emitter; standard library only (step 22)
  endpoint.py          host_label, error_detail: shared by the operator-endpoint clients
                       (remote ASR, ntfy)
  fakes.py             a fake backend for each of the six stages (tests and smoke runs)
  youtube/             links.py (link forms, canonical URL, youtube_episode; light),
                       ytdlp.py (YtDlpYoutubeSource, the `ytdlp` YoutubeSource; imports yt-dlp
                       only in load_runtime; step 32)
  mail/                note.py (the mail contract: note, instructions file, slug, guid, dates,
                       paragraphs), message.py (compose_mail: the MIME message and the
                       attachment cap), smtp.py (SmtpMailer, the `smtp` Mailer; check_login
                       for doctor); step 25
  cli.py               `earmark` entry point
  api/                 app.py (create_app), auth.py (bearer middleware), routes.py, schemas.py
                       (responses and JobView), upload.py (receive_upload: the streaming
                       multipart reader; step 24)
  pipeline/
    stages.py          stage Protocols, Backends (the six stage implementations)
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
Dockerfile, .dockerignore, compose.yaml    the CPU image and the services (step 16)
Dockerfile.gpu         the GPU image: CTranslate2 built with CUDA, swapped into the venv (step 26)
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
  `Source` is `apple_podcasts`, `upload`, `url` (step 24) or `youtube` (step 32).
  - **YouTube episodes** (step 32) come from `youtube.links.youtube_episode`. They have
    `show_id = "youtube"` (synthetic), `episode_id` the 11-character video id,
    `guid = "youtube:<id>"` and `key = youtube_key(id)`, which is
    `episode_key("youtube", "youtube", "youtube:<id>")`, through `models.source_key`.
    `title` and `show_title` (the channel; `""` when unknown) are yt-dlp's, cleaned and capped
    at 500 characters. `published_at` is the upload time, else YouTube's calendar date with
    `published_date_only`. `audio_url` is `""` (stream URLs expire and carry the server's
    address), `feed_url` and `feed_guid` are `None`, `url` is
    `https://www.youtube.com/watch?v=<id>` and `description` is `""`.
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
    - `youtube` (step 32): `url` is the canonical `https://www.youtube.com/watch?v=<id>`,
      whatever form the client sent.
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
`models.source_key`; YouTube episodes use `youtube_key(id)` (step 32), so every link form of
one video has one key. The key names the cache directory, so the same episode shared twice,
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


class YoutubeSource(Protocol):  # step 32
    def resolve(self, video_id: str, /) -> Episode: ...  # PermanentError for a refused video
    def download(self, video_id: str, destination: Callable[[str], Path], /) -> Path: ...


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
- `YoutubeSource.resolve` reads a video's metadata as an `Episode` and refuses what earmark
  will not take (live, not yet aired, private, removed, members-only, age-gated) with a
  `PermanentError` that says which. `download` writes an audio-only stream to
  `destination(ext)` and reuses the extraction of the `resolve` just before it. It never
  caches. Only the runner calls them, for `youtube` jobs (step 32). `Backends` has a sixth
  field, `youtube`.
- `Transcriber.cached` returns the cached transcript only when it was made with this
  transcriber's ASR profile, and never loads a model.
- `Resolver.resolve` is called only for `apple_podcasts` jobs, so nothing else reaches it.

Every stage has a fake in `fakes.py` that records each call (`calls`), can be told to
raise (`fail_next(exc, times)`), and, for the cache-aware stages (fetch, transcribe),
counts the calls that did real work (`computed`). `FakeYoutubeSource` records resolves and
downloads, counts the downloads (`downloads`) and serves `payloads.get(id, payload)`.
`FakeAudioFetcher` also counts direct downloads (`downloads`), and serves `payloads.get(url, payload)`, so two URLs can serve
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

- **Backends.** `Backends` (in `stages.py`) holds the six stages (resolver, fetcher,
  youtube, transcriber, mailer, notifier); `notifier` is `None` when its backend is `none`. `factory.build_backends` is the
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
     - `youtube` (step 32): `youtube.resolve(video_id)` with the id from the canonical URL.
       It runs on every job, so a video that has become private fails a new job even with a
       cached transcript. A result for another video is permanent.
  2. **Record** it: write `episode.json`, record the episode key on the job, and record the
     share time in `shared.json`. Keep the earliest share time it returns for the note.
  3. **Cache.** If `transcriber.cached(episode)` returns a transcript, skip fetch and
     transcribe.
  4. **Transcribe.** Otherwise, for a podcast, `fetch`; for a YouTube video, set stage `fetch`,
     clear this job's old downloads and `youtube.download(video_id,
     staging.download_destination(job.id))`. Then `transcribe` the podcast's audio, or the
     staged upload or download in place: it is never moved into `episodes/<key>/`.
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
  upload (`uploads/<upload_file>`), a direct URL or a YouTube video (`downloads/<job_id>.*`),
  then notifies.
  - **When it runs:** for every job that reaches `done`, `failed` or `skipped`, including
    jobs that `requeue_stale` fails.
  - **What it ignores:** `EARMARK_KEEP_AUDIO` does not keep uploaded, downloaded or YouTube
    audio. A
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

### YouTube

Step 32; exact signatures in `docs/steps/32-youtube-source.md`.

- **Links.** `youtube.links` recognises a video in any common form (`youtu.be/<id>`,
  `youtube.com/watch?v=<id>` on `www`, `m` and `music`, `/shorts/`, `/live/`, `/embed/` and
  `/v/`, and `youtube-nocookie.com/embed/`), keeps only the 11-character id (`A-Za-z0-9_-`,
  case kept) and drops every other parameter. `sources.parse_source_url` stores
  `https://www.youtube.com/watch?v=<id>` with the source `youtube`. A channel, playlist,
  search or other page, and the playlist and live-page embeds, are refused at the API with
  422 ("This YouTube link is not a single video. …"); a link with no valid id is refused with
  "This YouTube link has no valid video id." The API makes no request: a well-formed link to
  a private or removed video is accepted with 202 and fails in the worker.
- **One identity.** The episode key, the guid and the dedupe subject come from the video id
  alone, so every link form of one video is one episode key and one cached transcript. A
  re-share with new instructions is a new job that skips the download.
- **yt-dlp downloads the audio itself.** A plain GET of a YouTube stream is throttled to
  about twice real time (one hour of audio would take half an hour); yt-dlp's chunked ranged
  downloader with the per-format headers fetched a 10 MB stream in under a second. Doing it
  in `fetch.py` would be new code that tracks YouTube's behaviour, and still needs yt-dlp to
  solve the stream URL, so `fetch.py` is not used for media. The address guard is replaced by
  the narrower rules below; the size cap is kept.
- **The backend** (`youtube.ytdlp.YtDlpYoutubeSource`, the `ytdlp` backend of
  `EARMARK_YOUTUBE_BACKEND`). `load_runtime` imports yt-dlp by name (it is optional and
  untyped), clears yt-dlp's plugin directories, and finds Deno in the interpreter's `bin/`.
  A missing extra or Deno is a `ConfigError` naming `uv sync --extra youtube`. `resolve`
  builds a session from fixed options and extracts the metadata once, with
  `ignore_no_formats_error` so an unplayable video's reason is kept; `download` reuses that
  extraction on a second session whose output template is the job's staging file.
- **The options** are exactly: only the `youtube` extractor (`allowed_extractors` and
  `ie_key`), `noplaylist`, `bestaudio[protocol=https][vcodec=none][audio_channels<=2][abr<=192]`
  with a fallback to any https audio-only stream, no post-processors and no format fix-up (so
  no ffmpeg), no cache directory, no environment proxy, no cookies, config files, netrc or
  extractor arguments, only the venv's Deno as the JavaScript runtime and no remote
  components, a 30 s socket timeout and 3 retries. A test compares the key set.
- **Deno.** YouTube's player challenges are solved by `yt-dlp-ejs`, which yt-dlp pins to its
  own version, in Deno. Deno comes from the `deno` PyPI wheel (the `deno` extra of yt-dlp),
  so the lock installs it on aarch64 with no system package. It runs the player's JavaScript
  with no permission flags (no file, network, environment or subprocess access). Around each
  yt-dlp call earmark sets `DENO_DIR=<data dir>/cache/deno` (unless the operator set it) and
  `DENO_NO_UPDATE_CHECK=1`, and restores both: under the systemd unit `~/.cache` is
  read-only.
- **Refusals.** A video that is live, not yet aired, just ended and still processing,
  private, members-only, Premium-only, age-restricted, needing a sign-in, removed, or
  blocked in the server's country fails permanently with a fixed sentence that says which
  (`REFUSALS` in `ytdlp.py`; no URL, no id, no yt-dlp text). A bot check is permanent too: a
  retry from the same address rarely clears it and adds requests. YouTube's "try again later"
  is transient with an hour's delay. Network failures, HTTP 408, 429 and 5xx, a cut-short or
  empty download and a download over one hour are transient. Anything that means yt-dlp is
  out of date (an unexpected extractor failure, an exception from inside yt-dlp, HTTP 403, no
  audio-only stream, no file written) is permanent and points at `docs/deploy.md` section 18.
  An `OSError` (a full disk) is not mapped: it reaches the runner, which retries it.
- **The size cap** is `EARMARK_MAX_AUDIO_MB`, applied three ways: the size YouTube declares,
  before any download request; the bytes counted by a progress hook, which cancels the
  download; and the file on disk. yt-dlp's own `max_filesize` is not used, because it skips
  silently. There is no duration cap: the size cap bounds a download (1024 MiB is about 17
  hours at 128 kbps), and live streams are refused.
- **Metadata is untrusted.** Title and channel go through `resolve.text.clean_text` (500
  characters); the date is the upload timestamp when it is a sane number, else the upload
  calendar date; the duration is a finite non-negative number. Nothing else of yt-dlp's
  output is kept.
- **The exception to "all third-party fetches go through `fetch.py`"** (see "Security"):
  yt-dlp makes its own requests, for YouTube only.
- **Logging.** `YtDlpLog` sends yt-dlp's debug and info lines to the `yt_dlp` logger at
  DEBUG and its warnings at WARNING, and keeps the warning texts to classify a refusal.
  `yt_dlp` is in `QUIET_LOGGERS`, so the DEBUG lines (which carry stream URLs with the
  server's address) never print. yt-dlp writes `WARNING: Unknown codec …` straight to stderr
  for some formats; it carries a codec name only.

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
      and VAD filter, plus `batch_size` only when `ASR_BATCH_SIZE` is above 0 (step 26). So
      the profile with batching off is byte-for-byte the one from before step 26, and
      transcripts cached then are still reused.
    - `remote`: model and language.
    - The fake: model and language.
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
  CTranslate2 releases the GIL, so the heartbeat thread keeps beating. The load is logged as
  `loaded Whisper model '<model>' in <s> s on <device> (<compute type>)`, read from the
  CTranslate2 model, so the log shows what `auto` resolved to.
- **Batched decoding** (step 26; `docs/steps/26-gpu-image.md` section 4.10). With
  `ASR_BATCH_SIZE` above 0, `load_runtime()`'s model loader wraps the `WhisperModel` in
  faster-whisper's `BatchedInferencePipeline` and passes `batch_size` on every
  `transcribe(...)` call. It takes the same language, beam size and VAD filter. Batched mode
  decodes VAD chunks of up to 30 s without conditioning on the previous text and with
  chunk-level timestamps (faster-whisper 1.2.1's defaults), so its text can differ from
  sequential decoding; that is why the batch size is part of the cache key. `0`, the
  default, is sequential decoding as before.
- **CUDA.** `WhisperRuntime` exposes `cuda_device_count()` and
  `supported_compute_types(device)` (step 26). `cuda_report(runtime)` gives doctor the device
  count, and the CUDA compute types when there is a device. The PyPI wheel raises
  `ValueError` ("not compiled with CUDA support") for `get_supported_compute_types("cuda")`,
  so the report asks only when the count is above 0, and records the exception's type name
  if it raises anyway. `get_cuda_device_count()` is CUDA's `cudaGetDeviceCount`: it is 0
  for a build without CUDA or with no device visible, but a build for the wrong GPU
  architecture still counts the device and fails at the first kernel ("no kernel image is
  available"). Only a real transcription proves the build.
- **Errors.** Model not found on Hugging Face, or a `ValueError` at load (bad model
  name, unsupported compute type) → `PermanentError` naming the settings; other
  `OSError`s while loading (download failures) → `TransientError`. `ASR_DEVICE=cuda` with
  no CUDA device visible to CTranslate2 is a startup `ConfigError`. Its message names
  `EARMARK_ASR_DEVICE`, says the PyPI wheels for linux/aarch64 are CPU-only, and points at
  the GPU image with GPU access (compose `gpus: all`), `auto` or `cpu`, or
  `EARMARK_ASR_BACKEND=remote`. Messages carry file base names and setting names, never full
  paths, URLs, or library error text.
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
    outside `[a-z0-9]` becomes `-`. A podcast joins the show and title slugs, and a video
    with a channel joins the channel and title slugs (step 32). The whole slug is cut to 80
    characters. When it is empty, the fallback is `podcast-<episode id>`,
    `youtube-<video id slug>` or `audio-<12 hex of the sha256>`.
  - **The guid.**
    - Audio: `sha256:<hex>`.
    - Video (step 32): `youtube:<video id>`.
    - Podcast: `feed:<Episode.feed_guid>` when that is verbatim (cleaning would not change
      it) and at most 300 characters, else `apple:<episode id>`.
    - Apple's `episodeGuid`, which still keys the cache, is not used for the note: it can
      differ from the feed's.
  - **`source_url`** (`origin: url`) is `sources.display_url`. It is omitted when the
    URL has a `?`, or when the value is over 300 characters or would be changed by cleaning.
    For `origin: youtube` (step 32) it is always `https://youtu.be/<video id>`.
  - **A video note** (step 32, `episode.source == "youtube"`): `type: "video"`,
    `origin: "youtube"`, `title` (`YouTube video <id>` when YouTube gave none), `channel`
    when known, `published` (the upload day in the requester's offset, or YouTube's calendar
    date), `source_url`, `guid`, `tags: ["video"]`. The key order is the podcast's with
    `channel` after `show`, and a video has neither `show` nor `apple_url`. A podcast note is
    `type: "podcast"` only for `apple_podcasts` (it used to be anything that was not audio).
    `CONTRACT` is 2 in both files of every message, so a podcast or audio note differs from
    its contract 1 form only in that line; its `transcript_sha256` covers the body, so a
    note filed from a version 1 message and a re-share that arrives as version 2 classify
    as a re-share.
  - **`language`** is kept only when it looks like BCP 47 (`[a-z]{2,3}(-[A-Za-z0-9]{2,8})*`).
  - **The body.** `# <show> — <title>` (podcast), `# <channel> — <title>` (a video with a
    channel) or `# <title>`, then `## Transcript`, then
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

#### The mail contract (`contract: 2`)

- **Provenance.** Drafted by pka-skills on 2026-11-23. Owned by this repo since 2026-11-24
  (the operator's decision; pka-skills confirmed that `contract: 1` stands as drafted). Since
  step 29 its one source of truth is `contract/mail-v1.md`, moved there unchanged from this
  document and from `docs/PLAN.md` "Re-scope", which link to it. Step 32 (2026-11-25) added
  `contract/mail-v2.md` for YouTube, as a list of changes to contract 1; the service sends
  `contract: 2` for all mail (the operator's decision), and the receiving skill accepts both.
- Status: **DRAFT**, both versions. A version becomes final once the operator approves the
  PKA's receiving procedure, which is now the receiving skill (step 30). A revision comes
  with a contract version bump, because mail sent under one version can still be waiting in
  an inbox when the next one ships. Step 25 built to version 1, and step 32 to version 2.

The texts: [contract/mail-v1.md](../contract/mail-v1.md) and
[contract/mail-v2.md](../contract/mail-v2.md). A test pins each by hash.

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
    - YouTube (step 32): the video's title (`YouTube video` when unknown), then
      `YouTube: <channel>` (`YouTube video` when the channel is unknown). No video id and no
      URL.
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
  - Each selected backend (resolver, fetch, youtube, asr), built through the same factory
    registry as the worker. That imports its extra and runs the CUDA check.
  - `youtube` (step 32), after `fetch`: `warn` for `fake`; for `ytdlp`, `FAIL` naming
    `uv sync --extra youtube` when yt-dlp or its Deno is missing, otherwise `ok` with
    `yt-dlp <v>, yt-dlp-ejs <v>, deno <v>` (`unknown` for a version that cannot be read). It
    makes no request and starts no process.
  - For `faster_whisper` only: `asr libraries` (step 19): the installed `faster-whisper`
    and `av` versions, read from package metadata (`importlib.metadata`, through
    `Probes.installed`), checked against `transcribe.compat.KNOWN_INCOMPATIBLE`. It
    warns on a known-bad pair (today faster-whisper <= 1.2.1 with av >= 19), or when a
    version cannot be read. A package installed from a source tree (PEP 610
    `vcs_info`/`dir_info`) is not checked by version. Then `asr model`: whether the Whisper
    model is local or in the cache, through faster-whisper's
    `download_model(local_files_only=True)`. Then `asr cuda` (step 26): the CUDA device count
    and whether `float16` is among `ctranslate2.get_supported_compute_types("cuda")`, through
    `cuda_report`. It fails when the configured device cannot work (`cuda` with no device,
    the compute types unreadable, or `float16` configured and unsupported), and is `ok` with
    no device for `auto` and `cpu`. Exact wording in `docs/steps/26-gpu-image.md` section 4.11.
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
    subject is the canonical Apple URL, the canonical YouTube URL (step 32, so two forms of
    one video are one request), the direct URL, or `sha256:<hex>` for an upload.
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
  - `source` (`NOT NULL DEFAULT 'apple_podcasts'`, `CHECK` in the three sources; step 32's
    rebuild allows a fourth, `youtube`);
  - `recipient`, which is the `Job.to` field (`TO` is an SQL keyword);
  - `instructions` (`NOT NULL DEFAULT ''`);
  - `title`, `upload_file`, `upload_name` and `content_sha256`.

  It copies `prompt` into `instructions`, drops `prompt`, and adds
  `jobs_by_dedupe (dedupe_key, status, updated_at)`. Rows from before it keep `recipient`
  NULL and their status.
- Step 25 appended `0003_delivery` (schema version 3). It adds these columns:
  - `utc_offset_min` (`INTEGER`, `CHECK` in ±1439);
  - `delivery_state` (`CHECK IN ('sending', 'sent')`);
  - `message_id` and `sent_at`;
  - `resent` (`INTEGER NOT NULL DEFAULT 0`, `CHECK IN (0, 1)`).

  Earlier rows get `NULL` and `0`.
- Step 32 appended `0004_youtube`, so `SCHEMA_VERSION` is 4. SQLite cannot alter a `CHECK`,
  so it rebuilds `jobs` with `source` allowing `youtube`: it creates `jobs_0004` with the
  same 30 columns in the same order, copies every row by an explicit column list, drops
  `jobs`, renames the new table, and recreates the three indexes, all inside the
  migration's `BEGIN IMMEDIATE`. Rows, the application id and the partial unique index are
  kept. An image from before step 32 then refuses the database as newer than it supports
  (`docs/deploy.md` section 15 says to back it up first).
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
  earmark.db                    jobs (application id ERMK, schema 4); WAL mode
  outbox/<job_id>.eml           only with EARMARK_MAIL_BACKEND=fake: the message the fake
                                mailer "sent" (0o600; step 25)
  uploads/                      staging.Staging, owned by the API (mode 0o700; step 24)
    .<upload_id>.<ext>.part     an upload still streaming (0o600); swept at API startup
    <upload_id>.<ext>           a received upload (0o600); deleted when its job ends, or by the
                                API's startup sweep when no queued or running job names it
  downloads/                    staging.Staging, owned by the worker (step 24)
    <job_id>.<ext>              a direct URL's or YouTube video's audio for one job; deleted when
                                the job ends
    .<job_id>.<ext>.<random>.part  download in progress; swept at worker startup unless the
                                job is running
  cache/deno/                   Deno's analysis cache for yt-dlp (DENO_DIR; step 32); Deno creates
                                it, and it holds nothing that matters
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
| `POST /transcriptions` | JSON `{url, to, instructions?, title?, timestamp?}` (`TranscriptionRequest`, at most 64 KiB), or `multipart/form-data` with one `file` part plus the text fields `to`, `instructions`, `title` and `timestamp` (the file at most `EARMARK_MAX_UPLOAD_MB`). The answer is 202 `{job_id, status, created}` with `Location: /jobs/{id}`; `created` is `false` when dedupe returned an existing job. 401 bad token. 413 too large. 415 a content type that is neither, or an upload that does not look like audio. 422 an invalid body, a missing, invalid or not-allowed `to`, or a bad URL, show link or YouTube link (not one video, or no valid id), with a message the Shortcut can display. Step 24; it replaced `POST /ingest`. |
| `GET /jobs/{id}` | The job's `JobView`. 404 if unknown. |
| `GET /jobs/{id}/transcript` | The job's transcript from the cache (step 33). `?format=note` (the default: the note exactly as the mail code renders it, `text/markdown`, with the attachment's file name in `Content-Disposition`), `text` (`transcript.txt`, `text/plain`) or `json` (`TranscriptView`, `application/json`). 404 unknown job. 409 no transcript yet, or the job failed or was skipped before it had one. 410 the cache is gone. 422 an unknown format. 500 an unreadable cache file. |
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
  - A YouTube link (step 32; `youtu.be`, `youtube.com` and its subdomains,
    `youtube-nocookie.com`; with or without a scheme) is reduced to its 11-character video id
    and stored as `https://www.youtube.com/watch?v=<id>` with the source `youtube`. A link that
    is not one video is refused: "This YouTube link is not a single video. Share a video, not
    a channel, playlist or search." A link with no valid id: "This YouTube link has no valid
    video id." Credentials and a port are refused like Apple's.
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
- `url`: the canonical Apple or YouTube URL whole; for a direct URL, scheme, host, port and
  path only;
  for an upload, `null`;
- `to`, in full;
- `instructions`, cut to 200 characters (199 and `…`);
- `title`, `episode_key`, `attempts`, `run_after`;
- `error` and `warnings`, passed through `log.scrub_urls`;
- `result`, `shared_at`, `created_at`, `updated_at`;
- `delivery_state` and `sent_at` (step 25).

It never carries `heartbeat_at`, the lease token, the staged file name, the upload's file
name, the content hash, the `Message-ID` or `resent`.

**`GET /jobs/{id}/transcript`** (step 33; exact rules in
`docs/steps/33-transcript-by-job-id.md` sections 4 and 5). `transcripts.read_transcript_file`
does the work, for the route and for `earmark transcript` alike, so both give the same bytes
and the same messages.

- **It reads, and does nothing else.** It reads the job row and the files under
  `episodes/<episode_key>/`. It writes nothing (not `shared.json` either), starts no stage and
  makes no network request. The handler is sync and runs in the threadpool.
- **When a job has a transcript:** its status is `done`, or its stage is `deliver` (running,
  queued for another delivery attempt, or failed while delivering, for example on
  `EARMARK_MAX_ATTACHMENT_MB`). Otherwise the answer is 409 with a `detail` naming the
  status (and the stage while it is queued or running). A failed or skipped job that never
  reached `deliver` is 409 even when another job cached the same episode since.
- **Formats.** All three bodies are UTF-8. Every 200 carries `Cache-Control: no-store`.
  - `note`: `mail.note.render_note` on the cached `episode.json`, `shared.json` and
    `transcript.json`, so it is the mailed note attachment, byte for byte, while that
    episode's cache is unchanged. For an upload or a URL job the episode is rebuilt with
    `sources.audio_episode(job, sha256)`, as the runner built it, not taken from
    `episode.json` (which holds the last job's title). The note is rendered per request and
    never stored. `Content-Type: text/markdown; charset=utf-8` and
    `Content-Disposition: attachment; filename="<the attachment's file name>"`.
  - `text`: the bytes of `transcript.txt`, `text/plain; charset=utf-8`.
  - `json`: `TranscriptView`, `{job_id, episode_key, language, duration_s, segments: [{start,
    end, text, speaker}], warnings}`, indented by 2 and ending in a newline, `application/json`.
    It never carries the ASR model or profile, the guid, `to` or `instructions`.
- **The cache.** A missing `transcript.json` is 410 for every format; a missing
  `transcript.txt` is 410 for `text`; a missing `episode.json` (podcast, YouTube, URL) or
  `shared.json` is 410 for `note`. A file that does not parse is 500, with a `detail` naming
  the file, and is logged without its content or the validation error.
- **When the note differs from the mail.** It is rendered from today's cache and mail code.
  It differs from what was sent when, since then, a later share of the same episode
  re-resolved it with new metadata, a share carrying an earlier `timestamp` moved
  `shared.json`'s first share, a new ASR profile re-transcribed it, or a contract revision
  changed the mail code.
- **Logs** name the job and the format, and the reason code of a refusal. Never the
  content, the recipient or the instructions.

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
| `YOUTUBE_BACKEND` | `ytdlp` | `ytdlp` (needs the `youtube` extra; a worker without it refuses to start, naming it), `fake` (step 32) |
| `ASR_BACKEND` | `faster_whisper` | `faster_whisper`, `remote`, `fake` |
| `MAIL_BACKEND` | `smtp` | `smtp`, `fake` (step 25). `smtp` without its required settings is a startup `ConfigError`; `fake` sends nothing and writes `data/outbox/<job_id>.eml` |
| `NOTIFY_BACKEND` | `ntfy` | `ntfy`, `fake`, `none`. `ntfy` without `NTFY_URL` sends nothing (the worker still starts) |
| `ASR_MODEL`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, `ASR_LANGUAGE` | `large-v3`, `auto`, `auto`, unset (auto-detect) | faster-whisper options (step 08); `remote` also uses `ASR_MODEL` (sent as the server's model id, e.g. `openai/whisper-large-v3`) and `ASR_LANGUAGE`. Model: a size such as `tiny`, `small`, `large-v3`, `distil-large-v3`, `large-v3-turbo`, a Hugging Face repo id, or a local directory. Device: `auto`, `cpu`, `cuda`. Compute type: `auto`, `default`, `int8`, `int8_float32`, `int8_float16`, `int8_bfloat16`, `int16`, `float16`, `bfloat16`, `float32` (device and compute type case-insensitive). Language: a lower-case code such as `en`; `auto` means detect |
| `ASR_BATCH_SIZE` | `0` (off) | faster-whisper's batched pipeline: chunks decoded together, `0`–`64`; `0` or unset is sequential decoding. Part of the transcript cache key when above 0 (step 26) |
| `ASR_MODEL_DIR` | unset (the Hugging Face cache, `$HF_HOME/hub` or `~/.cache/huggingface/hub`) | Where faster-whisper downloads models |
| `ASR_BASE_URL`, `ASR_API_KEY`, `ASR_MAX_UPLOAD_MB` | unset, unset, unset (no upload limit) | Remote ASR backend (step 09). Base URL: the server's OpenAI-compatible base, ending in `/v1` (http or https, a host, no credentials, query, or fragment; a trailing `/` is dropped). Key: secret, printable ASCII without spaces, sent as a bearer token. Upload limit in MiB |
| `ASR_TIMEOUT_SECONDS` | `10800` | How long the remote backend waits for the server's answer (read and write timeout) |
| `KEEP_AUDIO` | `false` | Keep audio after transcription |
| `NTFY_URL`, `NTFY_TOKEN` | unset | ntfy topic URL and optional access token (both secret; step 15). URL: http(s), a host, optional path prefix, ending in the topic (`[-_A-Za-z0-9]{1,64}`), such as `https://ntfy.sh/my-earmark-topic` or `http://ntfy.tailnet-name.ts.net/earmark`; no credentials, query, or fragment. On public ntfy.sh the topic name is the only protection unless the topic is reserved, so pick a long random one. Token: printable ASCII without spaces (`tk_…`), sent as a bearer token |
| `MAX_AUDIO_MB`, `MAX_FEED_MB`, `ALLOW_PRIVATE_FETCH` | `1024`, `20`, `false` | Fetch limits in MiB (× 1024 × 1024); `MAX_AUDIO_MB` also caps direct-URL and YouTube downloads; `MAX_FEED_MB` caps the lookup, the episode page, and the feed. `ALLOW_PRIVATE_FETCH` turns off the address check only |
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
    `EXTRAS` (default `transcribe youtube`, step 32), with the project installed non-editable
    into `/app/.venv`. The `youtube` extra brings yt-dlp and a Deno wheel, about 45 MB, so a
    CPU worker handles YouTube links too.
  - User: `earmark`, with UID and GID from build arguments (default 1000, 0 refused).
    `/data` is owned by it.
  - Environment: `EARMARK_HOST=0.0.0.0`, `EARMARK_DATA_DIR=/data`,
    `HF_HOME=/data/huggingface` (so Whisper models persist on the volume),
    `HF_HUB_DISABLE_TELEMETRY=1` (Hugging Face telemetry off, step 21; see "Model
    downloads" under Security).
  - Entrypoint `earmark`, default command `serve`.
  - It installs `ca-certificates`, and has no `HEALTHCHECK`, no `VOLUME`, and no `.env`.
  - `.dockerignore` is an allow-list: `pyproject.toml`, `uv.lock`, `.python-version`,
    `README.md`, `src/`. Both Dockerfiles use it.
- **GPU image** (step 26; exact instructions in `docs/steps/26-gpu-image.md` section 4.1).
  `Dockerfile.gpu` has stages `uv`, `build` and `runtime`, and carries earshot's
  attribution comment.
  - Bases, pinned by digest: `nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04` to build and
    `nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04` to run (CUDA 12.9.1, cuBLAS 12.9.1.4, cuDNN
    9.10.2.21; CUDA 12.9 is the first toolkit with `sm_121`), and the same uv image as the
    CPU `Dockerfile`. The NGC PyTorch base that earshot used was not taken: see the step
    plan, section 2.
  - Python: uv installs CPython 3.13.18, the CPU image's version, into `/opt/python`, which
    the runtime copies. It has the headers the bindings need; Ubuntu 24.04's own Python is
    3.12 and is not used.
  - The venv: `uv sync --locked --no-dev --no-editable --extra transcribe --extra youtube`
    into `/app/.venv`, as in the CPU image.
  - CTranslate2: the tag `CT2_VERSION` (a global build argument, default `v` plus the
    locked version, `v4.9.0`) is cloned and built with the recipe's flags and
    `-DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}"` (default `121`). It is installed under
    `/opt/ctranslate2`. Its Python wheel is built against the venv's interpreter, with the
    tag's own `install_requirements.txt` (pybind11) in a throwaway build venv. Then it is
    swapped in with `uv pip install --python /app/.venv/bin/python --no-deps --reinstall`,
    which also removes the PyPI wheel's bundled `ctranslate2.libs/`. No uv command runs
    after the swap, and the runtime stage has no uv at all, because `uv sync` and `uv run`
    put the locked PyPI wheel back (checked in step 26's research). No `pip` runs anywhere.
  - Runtime: `/opt/python`, `/app/.venv` and `/opt/ctranslate2/lib` are copied from the
    build stage, with `libopenblas0-pthread`, `libgomp1` and `ca-certificates` installed.
    The base image's `ubuntu` user (UID 1000) is removed. User, `/data`, environment,
    entrypoint and command are the CPU image's.
  - Build-time check, in the runtime stage, with no GPU: `ctranslate2` and `faster_whisper`
    import (with `BatchedInferencePipeline`), `ctranslate2.__version__` equals
    `CT2_VERSION`, `ctranslate2.__file__` is under `/app/.venv/`, and the loaded
    `libctranslate2` is the one in `/opt/ctranslate2/lib/`, not a wheel's bundled copy. A
    second runtime-stage `RUN` (step 32) runs the venv's `deno --version` and imports
    `yt_dlp` and `yt_dlp_ejs`, with no uv and no network.
  - `v4.6.0`, earshot's proven tag, is the fallback (`--build-arg CT2_VERSION=v4.6.0`). Its
    CMake ignores `CMAKE_CUDA_ARCHITECTURES` and, with no GPU at build time, builds the
    common architectures up to `8.6+PTX`, so the GB10 JIT-compiles the PTX.
  - NVIDIA's images are under NVIDIA's licence terms: a built image is not published.
  - Not built in CI (see "Continuous integration"). Only the operator's Spark smoke in
    `docs/deploy.md` section 7 checks it.
- **Compose.**
  - `api` (`serve`) runs the CPU image `earmark:local` (built with
    `EXTRAS: ${EXTRAS:-transcribe youtube}`). `worker` (`worker`) runs the GPU image `earmark:gpu`
    (`dockerfile: Dockerfile.gpu`) with `gpus: all`, which needs an NVIDIA GPU, the NVIDIA
    Container Toolkit and Compose 2.30 or later. Both share the named volume `data` at
    `/data` and get `APP_UID`/`APP_GID` as build arguments.
  - Settings come from `env_file: .env`. `environment:` forces the in-container
    `DATA_DIR`, `HOST`, `PORT`, an empty (so default) `ASR_MODEL_DIR`, and
    `HF_HUB_DISABLE_TELEMETRY`. The worker also gets `EARMARK_ASR_DEVICE: cuda` (forced) and
    `EARMARK_ASR_COMPUTE_TYPE: ${EARMARK_ASR_COMPUTE_TYPE:-float16}` (`.env` can override
    it; `.env.example` leaves it commented out for that reason).
  - `api` and `worker` set `HF_HUB_DISABLE_TELEMETRY: ${HF_HUB_DISABLE_TELEMETRY:-1}`
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
  - There is no `asr` (vLLM) service, `asr-models` volume or `gpu` profile any more (step
    26). The `remote` ASR backend stays for a server elsewhere (`docs/asr.md`).
  - Compose-only variables (`API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
    `HF_HUB_DISABLE_TELEMETRY`) are unprefixed, so `Settings` ignores them.
    `HF_HUB_DISABLE_TELEMETRY` is read by Hugging Face's libraries inside the containers, not
    by earmark. The GPU build arguments (`CT2_VERSION`, `CUDA_ARCH`) are not interpolated;
    `docker compose build --build-arg CT2_VERSION=v4.6.0 worker` overrides one.
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
  CPU image and runs only one-off commands in it. The GPU image has never been built
  anywhere by earmark's build: CI only runs `docker build --check` on it. The operator's
  Spark smoke (`docs/deploy.md` section 7) is the only real check of the GPU path.

## Security

The service runs on a private network, but it fetches URLs that third parties control
and handles text they wrote.

- **Requests: Apple links** (step 24). An Apple Podcasts link (`podcasts.apple.com`, legacy
  `itunes.apple.com`, exact match) is parsed into its ids. A show link without `?i=` is
  rejected with a clear message. The API stores the canonical URL it built from those ids,
  never the client's string, so re-shares dedupe and no client-controlled URL reaches the
  resolver. The resolver accepts only Apple Podcasts episode links, and the runner calls it
  only for them.
- **Requests: YouTube links** (step 32). A link on a YouTube host is reduced to its
  11-character video id, validated against `[A-Za-z0-9_-]{11}`, and stored as the canonical
  `https://www.youtube.com/watch?v=<id>`. Only that id reaches yt-dlp, as a URL earmark
  builds; never the client's string. A link that is not one video is refused at the API.
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
- **Leak rules for the transcript route** (step 33), tested in `tests/test_leaks.py`: the
  note, the text and the JSON view carry neither `to` nor `instructions`, and the route's
  log lines carry no transcript text, recipient or instructions, nor an error's text. The
  instructions attachment is never served.
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
  fetch goes through `fetch.py`, with one exception, YouTube (step 32), which is described
  after this list. The rules of `fetch.py`:
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
- **The exception: yt-dlp** (step 32). yt-dlp makes its own requests, for YouTube only,
  because its chunked, ranged downloader and per-format headers are what YouTube serves at
  full speed (see "YouTube").
  - yt-dlp is given one URL, built by earmark from a validated id. Only the `youtube`
    extractor is loaded: no generic extractor, no playlists, no plugins, no config files, no
    cookies kept (a new session per call, an in-memory jar), no environment proxies and no
    remote components (`yt-dlp-ejs` comes from the lock, not from npm or GitHub).
  - Before anything is downloaded the chosen stream must be `https` on a `googlevideo.com`
    host. yt-dlp follows that host's redirects itself.
  - yt-dlp's requests to YouTube's hosts do not pass the private-address guard,
    `EARMARK_ALLOW_PRIVATE_FETCH` or `MAX_FEED_MB`: a resolver that points those names at a
    private address is not caught. The size cap (`EARMARK_MAX_AUDIO_MB`) and the no-proxy rule
    are kept.
  - YouTube's player JavaScript runs in Deno with no permissions: no file, network,
    environment or subprocess access.
  - Job errors and pushes carry only fixed sentences (no URL, no id, no yt-dlp text). A YouTube
    job is logged by its video id; pushes carry the title and channel only.
- **Third-party logging** (step 20). Every log record goes through earmark's one handler
  (`log.configure_logging`), which applies three rules on top of secret redaction:
  - *Wire loggers are gated at the handler.* Records below WARNING from `httpx`,
    `httpcore`, `httpx2`, `httpcore2`, `urllib3`, `huggingface_hub` and `yt_dlp` (and their
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
  job errors and warnings follow the same rule. One line escapes the handler: yt-dlp's
  `parse_codecs` writes `WARNING: Unknown codec …` straight to stderr for some formats (step
  32); it carries a codec name and no URL.
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
  `tests/test_ytdlp_real.py` (step 32) is `slow` too: it loads the real yt-dlp offline, with
  sockets blocked (the options construct a real session loading one extractor, the real
  error classes map as designed, Deno runs). It needs the `youtube` extra and fails without
  it; the weekly job installs both extras. `tests/test_youtube_live.py` is `network` and
  skipped unless `YOUTUBE_TEST=1`: it downloads one 19-second public video from YouTube, so
  CI never runs it (GitHub's runners get bot checks) and the operator runs it from the
  Spark.
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

YouTube tests (step 32): `tests/test_youtube_links.py` covers the link forms, the refusals and
the one identity of a video; `tests/test_ytdlp.py` runs the backend on `tests/ytdlp_fakes.py`
(stand-in yt-dlp classes and a scripted session) with metadata recorded in
`tests/fixtures/youtube/`, and asserts the exact options passed to yt-dlp; `import
earmark.pipeline.factory` loads no yt-dlp.

Receiving skill tests (step 30): `tests/test_plugin.py` checks both manifests against the
documented fields, that the plugin holds exactly its six files, the release pin
(`PLUGIN_RELEASES`), and the statements `SKILL.md` must make (the exception, the preflight
first, no mail read by the skill, no version control, the outcomes). `tests/test_receiver.py`
runs the receiver as a subprocess under CPython 3.13 and 3.9.6 against a fake `pka-tools`:
every sample of both contract versions classifies `new`, a re-share across versions is a
re-share, and the resend, re-share, re-transcription and their
out-of-order arrival, a lost record, a moved or deleted note and an ambiguous one each give
the documented outcome; the write commands are checked for exact bytes, refused destinations,
and touching only the note. `tests/test_checker_floor.py` covers both scripts. Whether a PKA
follows `SKILL.md` is not tested; it is runbook phase 12.

Contract tests (steps 29, 32): `tests/test_contract_samples.py` proves the mail code reproduces
each v2 sample byte for byte, and that each v1 file still has the sha256 it had when the mail
code last built it, and the sample properties, the resend and the placeholders-only
rule. `tests/test_mail_checker.py` runs the checker as a subprocess, as a PKA would, under
every interpreter in `MAIL_CHECKER_PYTHONS` (CPython 3.9.6 and 3.8 under `make check`): every
sample of both versions passes in `message` and `files` mode, a mutation per rule fails that
rule (a rule that only contract 2 has, such as the video's `source_url`, included), and the exit
codes 0, 1, 2 and 64 and the output are checked. `tests/test_checker_floor.py` proves the
checker exists once, imports only the standard library, writes no bytecode, and that the
gate rejects 3.10+ code (ruff at `py39` and the pinned mypy on probe files).
`MAIL_CHECKER_REQUIRE_FLOOR=1`, which `make check` sets, fails the run if the 3.9 and
pre-3.9 interpreters are missing.

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
- **What `tests/test_docs.py` checks** (rewritten in step 27, extended in steps 28, 29 and 30; exact
  lists in `docs/steps/27-docs-runbook-shortcut.md` and `docs/steps/28-system-scope.md`
  section 6).
  - This file opens with "System": the three parts with what is not built or never run, the
    two seams, how mail reaches a PKA through AMAP, and the definition of done.
  - The repo layout's paths exist or carry a `todo` step from the `docs/PLAN.md` status
    table, and it names every top-level directory.
  - While a planned path does not exist, no doc names it in a paragraph without the step
    that builds it.
  - The sender allowlist is AMAP router config in every doc, never something to ask a PKA
    for.
  - `CLAUDE.md` and the README say the service files nothing and the PKA files.
  - The README's settings reference lists every `Settings` field exactly once, one
    variable per table row.
  - The README quickstart sets all five backends to `fake`, exports only `Settings`
    variables, runs only registered commands, posts a valid JSON request (with a
    `timestamp`) and a valid upload, and reads the fake mailer's outbox.
  - `docs/shortcut.md`'s JSON example and its upload field table are posted to
    `POST /transcriptions` (JSON and multipart) and accepted, with the `timestamp` offset
    stored. The example response matches the route. The error table matches what the API
    answers.
  - The Shortcut's actions appear in a fixed order, the menu's addresses are allowed by the
    allowlist example in `docs/deploy.md`, and both POST actions send `timestamp`.
  - Relative links and their `#anchors` in the README, `docs/deploy.md`,
    `docs/shortcut.md`, `docs/runbook-spark.md` and `docs/asr.md` resolve.
  - Those documents, `CLAUDE.md`, this file's System section and the step plans 27 to 30
    use placeholders only (addresses at `example.org`, `example.net` or
    `example.com`, the listed example IPs, placeholder secrets), and outside their marked
    "upgrading from v0.1.0" blocks name no removed feature, route, setting or service.
  - Every runbook phase ends in a check, and one check is a real delivery to the
    operator's own address; phase 11 is the receiving skill's install (step 30) and the
    last phase, 12, is the one-episode check of the whole system.
  - The README's command table names every CLI command, and its HTTP API sentence names
    every route of `build_router` (step 17).
  - Real share-sheet captures in `tests/fixtures/share_sheet.json` canonicalise as
    recorded.
  - The deploy guide's reachability, mail, allowlist, upgrade and telemetry sections say
    what they should.
  - This file contains `HF_HUB_DISABLE_TELEMETRY=1` and `HF_HUB_DISABLE_TELEMETRY=0`.
  - The `contract: 1` text in `contract/mail-v1.md` is pinned by its sha256, and no other
    Markdown file in the repo holds a line of it; `contract/README.md` names the checker,
    its exit codes and its floor.
  - Runbook phase 11 installs the receiving skill at project scope and ends in a check. The
    literals `claude plugin ` and `.claude/settings.json` are the one exception to the scan for
    removed wording, in phase 11 only, and a test confines them there.
  - The step plans scanned for placeholders are 27 to 30.
- **What `tests/test_deploy.py` checks**, statically: the Dockerfile, `.dockerignore`,
  `compose.yaml`, and the two systemd units, including that each one sets
  `HF_HUB_DISABLE_TELEMETRY` (step 21). `tests/test_deploy_gpu.py` (step 26) checks
  `Dockerfile.gpu` the same way:
  - the recipe's cmake flags, with `CMAKE_CUDA_ARCHITECTURES` from the build argument;
  - `CT2_VERSION` matching `uv.lock`;
  - the attribution;
  - the swap;
  - no `pip install`, and no uv after the swap or at runtime;
  - the build-time check;
  - parity with the CPU runtime stage.

  `tests/test_docs.py` checks that `docs/deploy.md` documents the Spark smoke.
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
with `astral-sh/setup-uv` (uv cache and the managed Python cached, keyed on `uv.lock` and `tools/py39/uv.lock`),
then runs exactly `uv sync --locked` and `make check`. `make check` also installs the
uv-managed CPython 3.9.6 and 3.8 (`--no-bin`, cached by setup-uv's `cache-python`) and runs
the floor mypy, which syncs `tools/py39/.venv`. Python comes from
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
  `slow` (step 08) runs `uv sync --locked --extra transcribe --extra youtube` and `uv run --locked --extra
  transcribe --extra youtube pytest -m slow -v -rs`, with setup-uv's `cache-suffix: slow` and a pinned
  `actions/cache` step for `~/.cache/earmark-tests` (the Whisper model and the clip).
  GitHub runs `schedule` and `workflow_dispatch` only for workflow files on the default
  branch, so it starts once the file is on `main`.
- `container.yml` ("Container image", step 16) runs on the same pushes and pull requests
  as `ci.yml`, with `contents: read`, no secrets, and the same pinned checkout as its only
  action. Its job `docker build (linux/arm64)`:
  - validates `compose.yaml` with `docker compose config` against a `.env` copied from
    `.env.example`, and checks the telemetry variable in both services and the worker's
    `float16` default;
  - runs `docker build --check -f Dockerfile.gpu .` (step 26). That runs BuildKit's
    Dockerfile checks and resolves the pinned base images' metadata, without building;
  - builds the CPU image with plain `docker build` (no push, no login, no Docker actions);
  - runs `earmark --version`, an import of `ctranslate2` and `faster_whisper`, an `id -u`
    check, and a check of `HF_HUB_DISABLE_TELEMETRY` in the image.

  It never starts the API or the worker, and never builds the GPU image. The free arm64
  runner has about 14 GB of disk, and the CUDA devel base alone is 5.94 GB compressed. Its
  4 cores would take a long time over the CUDA compile, and it has no GPU to run the checks
  that matter. See `docs/steps/26-gpu-image.md` section 2.

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
changes. Whether Dependabot also reads `Dockerfile.gpu` was not verified; if it does, the
`images` group bumps both `nvidia/cuda` stages together, and `tests/test_deploy_gpu.py`
fails a pull request that bumps only one. Dependabot reads its config from the default branch
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
   Step 26 (2026-11-23) replaced the vLLM profile with the GPU image: CTranslate2 built
   from source with CUDA 12.9.1 for `sm_121`, after
   `docs/decisions/2026-11-23-gpu-build-recipe.md`. CUDA 12.9 is the first toolkit with
   `sm_121`. Since 4.9.0 (#2067), CTranslate2 uses CMake's own CUDA support, so
   `CMAKE_CUDA_ARCHITECTURES` takes effect. 4.6.0 used `FindCUDA` and `CUDA_ARCH_LIST`. With no
   GPU at build time, that falls back to the common architectures up to `8.6+PTX`. So
   earshot's 4.6.0 most likely ran on the GB10 through PTX JIT; this is inferred, not
   measured. Upstream's aarch64 wheels stay CPU-only, and its CUDA wheels are x86_64 with
   CUDA 12.8. So no one upstream builds 4.9.0 for aarch64 with CUDA. The image has never
   been built; the operator's Spark smoke is the only check.
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
   builds the CPU image on linux/arm64 and runs one-off commands in it (`container.yml`),
   but no service has ever run in a container. The GPU image (step 26) has never been
   built: CI only lints it with `docker build --check`. The reachability setup has not been run either: WireGuard, the DNS-only record,
   resolvers' DNS rebinding protection, and Caddy's DNS-01 issuance. Only the example
   Caddyfile was checked, with `caddy validate`. What the iOS share sheet hands a Shortcut
   is also unverified (no iPhone);
   `docs/shortcut.md` says how to capture it into `tests/fixtures/share_sheet.json`.
   The Shortcut itself has never run (step 27): its action names come from Apple's
   Shortcuts User Guide and community references. Unverified on a phone: what `Get Type`
   reports for a shared audio file, that a `Form` body with a file field is sent as
   `multipart/form-data`, the file part's content type, whether `Ask for Input` accepts an
   empty answer, and how long `Get Contents of URL` waits on a large upload. The CPU-only
   worker override in `docs/deploy.md` section 7.2 (`gpus: !reset null`) has not run either.
   The Shortcut file does not exist yet (step 31), and the receiving skill (step 30) has never run
   in a PKA.
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

8. **AMAP, outside this repo.** earmark relies on AMAP to deliver its mail to each PKA: the
   router's sender allowlist and DMARC, `clean` attachments, the doorbell. None of it can be
   exercised here, and earmark learns only the SMTP reply. AMAP's attachment limit is not
   known; `EARMARK_MAX_ATTACHMENT_MB` must not exceed it, and a whole-message limit needs
   about 1.37 times the cap (`docs/steps/28-system-scope.md` section 2.4). Runbook phases 6
   and 12 check it.

9. **The checker's 3.9 floor rests on a frozen mypy and on the tests.** mypy 1.20.2 will not
   get newer typeshed, so a 3.10+ API in a branch the tests never run could pass both. If a
   CPython 3.13 patch release changes how `email` folds headers, the sample test fails and
   the samples must be regenerated, after checking that the diff is only folding.

10. **The receiving skill depends on what the PKA does and on AMAP's connector.** The PKA
    following `SKILL.md` is untested until runbook phase 12. The receiver relies on
    `read_attachment` giving a `path:` (inbox-lab-vol 0.1). A pka-tools release could change
    `post-route`'s behaviour without the receiver knowing; 1.8.0 is the minimum and 1.11.0
    was the version read.
11. **YouTube changes break yt-dlp.** They do so every few weeks (step 32); the remedy is an
    upgrade, `docs/deploy.md` section 18.2. Dependabot proposes yt-dlp updates weekly, and the
    weekly `slow` job runs the real library offline. YouTube is moving formats to SABR-only
    streaming for some clients (yt-dlp warned of skipped `web_embedded` https formats on an
    age-gated video), which may one day leave no https audio stream: the job then fails with
    "no audio-only stream". Datacenter addresses get bot checks, so CI never downloads from
    YouTube; the Spark's own line is what matters, and runbook phase 4 checks it. The address
    guard does not cover yt-dlp ("Security").

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
  *Superseded on 2026-11-24:* the receiving skill is part of this repo (step 30). See
  "System" above.
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

## Settled questions

The planners of steps 24–28 settled these. Each step plan, section 8, has the reasons.

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
    Shortcut sends `timestamp` (step 27, `docs/shortcut.md`), and `earmark submit` sends
    none.
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
  - **Still open:** the contract is final only once the operator approves the receiving
    procedure, now the receiving skill (step 30).
- **Settled by step 26** (see `docs/steps/26-gpu-image.md` sections 2 and 8):
  - **The base** is the slim pair `nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04` and
    `-cudnn-runtime-ubuntu24.04`, not the NGC PyTorch image:
    - it is the same CUDA 12.9.1 and Ubuntu 24.04 that earshot's NGC 25.06 build ran on;
    - it is the pattern upstream CTranslate2's own Dockerfile uses;
    - earmark needs no torch;
    - NGC's Python is 3.12, so it would need a uv-managed 3.13 anyway;
    - the runtime base is 3.09 GB compressed on arm64.
  - **CI does not build the GPU image.** It runs `docker build --check` on it, and the
    static tests run in `make check`.
  - **A separate file**, `Dockerfile.gpu`, so the CPU `Dockerfile` is untouched.
  - **Compose:** the API stays on the CPU image; the worker is the GPU image with
    `EARMARK_ASR_DEVICE` forced to `cuda` and the compute type defaulting to `float16`.
  - **The cache key** gains `batch_size` only when batching is on.
  - **Doctor's check** is a new `asr cuda` line after `asr model`.
- **Settled by step 27** (see `docs/steps/27-docs-runbook-shortcut.md` section 8):
  - **One Shortcut** for links and files. It picks `to` with "Choose from Menu" and
    `Set Variable`, asks for the instructions, formats the current date as ISO 8601 with
    time for `timestamp`, and branches on `Get Type` of the share-sheet input: a file is
    posted as a `Form` (multipart) upload, anything else as JSON with the first URL.
  - **`docs/deploy.md` keeps its section numbers.** Sections 7 and 17 are named by
    `Dockerfile.gpu`, `compose.yaml`, `.env.example` and the tests. The removed
    sections 8, 9 and 14 now hold email delivery, recipients and upgrading from v0.1.0.
  - **The runbook's order:** the GPU smoke and a real transcription into the fake outbox
    first, then the sending mailbox and a real delivery to the operator's own address,
    then the PKA recipients, reachability, ntfy and the Shortcut.
  - **No worker without a GPU in `compose.yaml`.** A host without one uses a local
    `compose.override.yaml`, documented in `docs/deploy.md` section 7.2.
- **Settled by step 30** (see `docs/steps/30-pka-receiving-skill.md` section 8):
  - the install commands name `--scope project`, because the command line defaults to user
    scope;
  - the deterministic work is a second script beside the checker (`receive.py`), so the
    scripts folder holds four files;
  - the plugin's version is pinned and bumped by hand, enforced by a release pin, because
    `--strict` needs a version and a pinned version is the only way a commit reaches no PKA
    unannounced;
  - recognition reads "matching `guid` and `job_id`" as: the same guid line in both
    attachments, `for` naming the note, and the `job_id` in the body, since the note has no
    `job_id`;
  - "the newer body wins" is ordered by the instructions' `shared_at`; an unknown order is
    the owner's call;
  - the manifests are checked against the documented field tables, not a schema;
  - the record has `v`, `event` and `shared_at` beyond pka-skills' list and is rebuilt by
    scanning, not by a command;
  - preflight is a subcommand and compares versions as numbers;
  - runbook phase 11 may name `claude plugin` and `.claude/settings.json`, nowhere else;
  - the media-type rule is skipped in the PKA;
  - the skill reads bytes from `read_attachment`'s `path:`;
  - the minimum pka-tools is 1.8.0;
  - the write subcommands (`file`, `rewrite`, `move`, `annotate`, `record`) confine each
    allowed action to the note that came with the message.
- **Settled by step 29** (see `docs/steps/29-mail-contract-seam.md` section 8):
  - the 3.9 type check runs on mypy 1.20.2 in its own locked project, `tools/py39/`, because
    the locked mypy 2.4.1 cannot target 3.9; Dependabot does not cover it;
  - `make check` installs the floor interpreters (`--no-bin`), so CI and the local gate stay
    one command;
  - the checker is a launcher plus a module, because the version guard must precede the
    `from __future__` import and parse on old interpreters;
  - the checker has a `files` mode, JSON output and a `rules` subcommand, because a PKA has
    no raw message;
  - the checker accepts the frontmatter subset earmark emits, stricter than YAML;
  - samples are compared byte for byte, nothing masked;
  - three samples beyond the minimum: re-share, re-transcription and a direct URL with a
    query;
  - the checker reads the contract's wording as: `type` and `origin` agree, `date` follows
    `published` or `shared_at`, an `apple:` guid is digits, and the third-party notice is the
    word `third-party`;
  - step 28's planned-names test keys the plugin aliases on the files step 30 creates;
  - lint gains the `FA` rules, and `.gitattributes` keeps the CRLF samples unconverted.
- **Settled by step 32** (see `docs/steps/32-youtube-source.md` section 8):
  - the service sends `contract: 2` for all mail, not only for YouTube (the operator's
    decision of 2026-11-25); the v1 text, its pin and its samples stay frozen;
  - yt-dlp downloads the audio itself, which is the one exception to "every third-party fetch
    goes through `fetch.py`", narrowed as in "Security";
  - a new stage, `YoutubeSource`, with its own setting and registry, because the resolver takes
    only Apple Podcasts links and its backend needs an extra;
  - a YouTube note is `type: video` with a `channel` key (the operator's decision), not a
    podcast with a `show`;
  - private, members-only, Premium, age-restricted and removed videos are refused, since earmark
    never signs in; a bot check fails the job and is not retried; there is no duration cap
    (the operator's decision);
  - the canonical form is `https://www.youtube.com/watch?v=<id>` in the job and
    `https://youtu.be/<id>` in the note;
  - the `youtube` extra is on by default in both images, and a worker without it refuses to
    start (the operator's decision);
  - the dependency is `yt-dlp[default,deno]`: `default` pins the matching `yt-dlp-ejs`, and
    `deno` is a locked aarch64 wheel with no system package; Node, QuickJS and Bun were not
    chosen;
  - migration `0004` rebuilds `jobs`, and an older image then refuses the database;
  - the weekly `slow` job installs the `youtube` extra; the live download stays opt-in.
- **Settled by step 33** (see `docs/steps/33-transcript-by-job-id.md` section 8):
  - the note is rendered per request from the cache, not stored, so it can differ from the
    mailed note after the episode's cache changes (listed under "HTTP API");
  - the note carries no `job_id`, because adding one is a contract revision; the JSON view
    carries the job's own `job_id`;
  - a job has a transcript when it is `done` or its stage is `deliver`, whatever its status;
  - the JSON view is a public `TranscriptView`, not the cached `Transcript`, so the ASR model
    and profile stay private and the cache format can change;
  - `earmark transcript` writes the exact bytes when its output is not a terminal, and only
    printable characters, newlines and tabs when it is.
- **Settled by step 28** (see `docs/steps/28-system-scope.md` section 8):
  - the runbook keeps phases 0–10 and appends phase 11 (installing the receiving skill,
    step 30) and phase 12 (the one-episode check), so no existing phase number changes;
  - the repo layout is a `text` fence after `<!-- repo-layout -->`, checked against the
    repo and the status table, and it also names `.github/`;
  - AMAP's attachment limit is checked both ways: decoded attachments against the cap, or
    the whole message against about 1.37 times the cap;
  - `deploy.md` keeps section 8.5's heading, so its anchor stays.

## Not planned

`docs/PLAN.md` "Backlog" lists what the design leaves room for: a send-rate cap, re-shares
as a reference only, an "analysis done" push, speaker diarization, other sources (other
podcast platforms and video files; YouTube is built, step 32), and an async transcription API
for other callers. None is designed here.

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
  `models/embed/`, `digests/`, `vault.lock` and `index.lock` under the data dir.
  `docs/deploy.md` section 14 and runbook phase 1 (step 27) cover moving the old database
  aside and deleting them. The vault is never touched.
- `huggingface-hub` stays at 1.33.0 in `uv.lock`; the fastembed constraint that held it
  below 2.0 left with the `index` extra.
- The GitHub environment `llm` and its `ANTHROPIC_API_KEY` secret are no longer used by any
  workflow; deleting them happens outside the repo.
- Known risks 8–10 of v0.1.0 (local LLM context and Ollama's silent truncation, duplicate
  basenames in Obsidian, fastembed's hub pin) went with the code.
