# earmark

earmark turns a podcast episode or a piece of audio, shared from an iPhone, into a note
filed by the operator's personal knowledge assistants (PKAs). This repository holds the
whole system, in three parts (below).

Most of this README is about part 2, the service: a self-hosted transcription and delivery
service. An iOS Shortcut, curl or `earmark submit` sends an Apple Podcasts episode link, a YouTube video link, a direct audio URL or an audio
file, plus a destination address and instructions. A worker transcribes it with faster-whisper, on the
DGX Spark's GPU in the GPU image. The transcript note and the instructions are emailed to
that address as two Markdown attachments, and an ntfy push follows. Python 3.13.

## The system

- **1. The Shortcut**, on the iPhone: sends a link or a file with the destination and the
  instructions. Built by hand from [docs/shortcut.md](docs/shortcut.md). It has never run
  on a phone, and a file to import comes in step 31.

- **2. The service**, on the DGX Spark, in `src/earmark/`: transcribes and emails the
  transcript. The rest of this README. The GPU image has never been built or run.

- **3. The receiving skill**, in each chosen PKA instance: checks an earmark email, files
  the note, and acts on the instructions within a narrow scope. It is the
  `earmark-pka` plugin in `plugins/earmark-pka/`, from this repository's plugin
  marketplace, built in step 30 and installed per PKA in runbook phase 11. It has never run
  in a PKA. Its contract checker (step 29) and its receiver are tested under Python 3.9.

The two seams are the request (`POST /transcriptions`) and the mail contract
(`contract: 2`, with `contract: 1` still accepted by the receiving skill). AMAP, outside this repository, delivers the mail to each PKA. The service
files nothing; the PKA files, through the receiving skill. The parts, the seams, the
definition of done and the repo layout are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#system-three-parts-and-two-seams).

## How it works

```
POST /transcriptions ─► jobs table (SQLite) ─► worker ─► Pipeline
  resolve ─► fetch ─► transcribe ─► deliver ─► notify
```

Every stage is a protocol with a real backend and a fake, chosen by configuration.

- **resolve**: the `apple` resolver turns the Apple link into the show's feed and the
  episode's audio URL (iTunes lookup, then the RSS feed). A direct audio URL or an upload
  needs no lookup. A YouTube link is read by yt-dlp (title, channel, date). Fake: `fake`.
- **fetch**: the `http` fetcher downloads the audio through a guarded client (scheme
  check, private-address block, size cap). Fake: `fake`. A YouTube video's audio is
  downloaded by the `ytdlp` backend (`EARMARK_YOUTUBE_BACKEND`, the `youtube` extra); fake:
  `fake`.
- **transcribe**: `faster_whisper` in the worker, or `remote`, an OpenAI-compatible speech
  server ([docs/asr.md](docs/asr.md)). Fake: `fake`.
- **deliver**: the `smtp` mailer sends the transcript note and the instructions through
  your mail provider's submission server. Fake: `fake`, which writes
  `data/outbox/<job id>.eml` and sends nothing.
- **notify**: `ntfy` sends a push notification when a job is sent or fails. Fake: `fake`;
  `none` turns it off.

**The email.** One message per job goes to the one address in the request. It has two
Markdown attachments, the transcript note (`YYYY-MM-DD-<slug>.md`) and the instructions
(`YYYY-MM-DD-<slug>.instructions.md`). The body is earmark's own text: it names the job and
both files. Delivery is at-least-once, so a PKA may see a message twice and must ignore
the duplicate. Sharing the same item again is a new job that reuses the cached transcript.
"Delivering" and the mail contract in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) have the
details.

**What the service keeps.** The job table and a transcript cache, both under
`EARMARK_DATA_DIR`. It is not a system of record: the notes live with the PKAs.

## Quickstart (fake backends)

This runs the whole service with fake backends: no network, GPU, API key, mail server or
model download. It needs [uv](https://docs.astral.sh/uv/) and `curl`, and port 8787 free.
Run it from a checkout of this repository.

<!-- quickstart:begin -->
```bash
uv sync
uv run earmark --version

demo=$(mktemp -d)
export EARMARK_API_TOKEN=demo-token
export EARMARK_ALLOWED_RECIPIENTS=you@example.org
export EARMARK_DATA_DIR="$demo/data"
export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_YOUTUBE_BACKEND=fake \
  EARMARK_ASR_BACKEND=fake EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
uv run earmark doctor

uv run earmark serve &
curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
curl -s http://127.0.0.1:8787/transcriptions \
  -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981", "to": "you@example.org", "instructions": "Which papers are mentioned?", "timestamp": "2026-11-23T21:30:00-08:00"}'
printf 'not really audio' > "$demo/memo.m4a"
curl -s http://127.0.0.1:8787/transcriptions \
  -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -F "file=@$demo/memo.m4a" \
  --form-string to=you@example.org \
  --form-string instructions="File this under ideas."
uv run earmark worker --once
uv run earmark worker --once
uv run earmark jobs
ls "$demo/data/outbox"
uv run python - "$demo"/data/outbox/*.eml <<'EOF'
import email
import email.policy
import hashlib
import re
import sys

for path in sys.argv[1:]:
    with open(path, "rb") as f:
        message = email.message_from_binary_file(f, policy=email.policy.default)
    print("Subject:", message["Subject"])
    print(message.get_body().get_content())
    for part in message.iter_attachments():
        data = part.get_payload(decode=True)
        print("=====", part.get_filename())
        print(data.decode())
        claimed = re.search(rb'transcript_sha256: "([0-9a-f]{64})"', data)
        if claimed:
            body = data[data.index(b"\n---\n", 3) + 5 :]
            ok = hashlib.sha256(body).hexdigest() == claimed[1].decode()
            print("transcript_sha256", "ok" if ok else "MISMATCH")
EOF
kill %1
wait
```
<!-- quickstart:end -->

What you see:

- `earmark doctor` passes, with warnings for the fake backends and the short token.
- `{"status":"ok"}` from the health check, then
  `{"job_id": …, "status": "queued", "created": true}` for each of the two requests (202).
- `job <id> done` from each `worker --once`.
- `earmark jobs` lists two `done` jobs at stage `deliver`.
- Two `.eml` files in the outbox, one per job. For each, the reader prints the subject
  (`[earmark] <title>`), the body earmark generated, the transcript note with its
  `[00:00:00]` paragraph and `transcript_sha256 ok`, and the instructions file.

The podcast note's dates use the request's `-08:00` offset, from its `timestamp`. The upload
sent none, so its dates are UTC. The fake
resolver never fetches the Apple URL, and the fake transcriber accepts any bytes (a real
one needs audio). `kill %1` stops the server, and `wait` lets it finish shutting down.
Everything lives in a temporary directory, so nothing is left in the repository.

**Real backends.** `uv sync --extra transcribe --extra youtube` installs local faster-whisper
and yt-dlp with its Deno runtime ([docs/deploy.md](docs/deploy.md) section 18). Set
`EARMARK_ALLOWED_RECIPIENTS` and the `EARMARK_SMTP_*` settings
([docs/deploy.md](docs/deploy.md) section 8). On the Spark, use compose with the GPU image
(section 7). [docs/deploy.md](docs/deploy.md) also covers systemd and how the phone
reaches the API.

## Guides

- [docs/deploy.md](docs/deploy.md): Docker Compose, systemd, reaching the API from the
  iPhone, GPU transcription, the sending mailbox, recipients, notifications, upgrading
  from v0.1.0, backups.
- [docs/shortcut.md](docs/shortcut.md): building the iOS Shortcut for links and files.
- [docs/runbook-spark.md](docs/runbook-spark.md): a step-by-step first setup on a DGX
  Spark, with a check after each phase.
- [docs/asr.md](docs/asr.md): remote transcription servers.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the system, the service's design, contracts,
  and security rules.
- [docs/PLAN.md](docs/PLAN.md): the build plan and what is built so far.

## Commands

Run them as `uv run earmark <command>`.

| Command | What it does |
|---|---|
| `serve` | Run the API on `EARMARK_HOST:EARMARK_PORT`. |
| `worker [--once]` | Run the job worker; `--once` processes at most one job and exits. |
| `submit <url-or-file> --to <address> [--instructions] [--title]` | Enqueue a transcription, the same way `POST /transcriptions` does. |
| `jobs [--status] [--limit] [--json]` | List recent jobs, newest first. |
| `transcript <job_id> [--format]` | Print a job's transcript from the cache: the note as mailed (the default), `text` or `json`. |
| `resolve <url>` | Resolve an episode URL to its feed, GUID, and audio URL. |
| `transcribe <file> [--json]` | Transcribe a local audio file and print the result. |
| `doctor [--online]` | Check the configuration, directories, database, backends, and credentials. |
| `config` | Show the effective configuration with secrets redacted. |
| `--version` | Print the version. |

The HTTP API is `POST /transcriptions`, `GET /jobs/{id}`, `GET /jobs/{id}/transcript`, `GET /jobs`, and `GET|HEAD /healthz`.
Everything except `/healthz` needs `Authorization: Bearer $EARMARK_API_TOKEN`. See "HTTP API" in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Configuration

Settings come from `EARMARK_*` environment variables or from `.env` in the working
directory. Real environment variables win over `.env`, and an empty value means unset.
Copy `.env.example` to start.

<!-- settings:begin -->
### API

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_API_TOKEN` | unset | Bearer token the Shortcut sends (secret). Required to serve. |
| `EARMARK_HOST` | `127.0.0.1` | Address the API binds to. Keep it on loopback and put a proxy in front ([docs/deploy.md](docs/deploy.md)). |
| `EARMARK_PORT` | `8787` | Port the API binds to. |

### Storage

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_DATA_DIR` | `./data` | Jobs database, transcript cache, upload and download staging, and the fake mailer's outbox. A local filesystem. |

### Backends

`fake` exists for tests and demos; `none` disables notifications.

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_RESOLVER_BACKEND` | `apple` | `apple` or `fake`. |
| `EARMARK_FETCH_BACKEND` | `http` | `http` or `fake`. |
| `EARMARK_YOUTUBE_BACKEND` | `ytdlp` | `ytdlp` (needs the `youtube` extra) or `fake`. |
| `EARMARK_ASR_BACKEND` | `faster_whisper` | `faster_whisper`, `remote`, or `fake`. |
| `EARMARK_MAIL_BACKEND` | `smtp` | `smtp` or `fake`. The fake sends nothing and writes `data/outbox/<job id>.eml`. |
| `EARMARK_NOTIFY_BACKEND` | `ntfy` | `ntfy`, `fake`, or `none`. |

### Transcription

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_ASR_MODEL` | `large-v3` | Whisper model name (also the model id sent to a remote server). |
| `EARMARK_ASR_DEVICE` | `auto` | `auto`, `cpu`, or `cuda`. |
| `EARMARK_ASR_COMPUTE_TYPE` | `auto` | CTranslate2 compute type, such as `int8` or `float16`. |
| `EARMARK_ASR_BATCH_SIZE` | `0` | faster-whisper's batched pipeline: chunks decoded together (8–16 on a GPU, at most 64). `0` is off. Changes the transcript cache key. |
| `EARMARK_ASR_LANGUAGE` | unset | Force a language code such as `en`; unset or `auto` detects it. |
| `EARMARK_ASR_MODEL_DIR` | unset | Where models are downloaded (default: the Hugging Face cache). |
| `EARMARK_KEEP_AUDIO` | `false` | Keep the downloaded audio after transcription. |

### Remote ASR

See [docs/asr.md](docs/asr.md). Used when `EARMARK_ASR_BACKEND=remote`.

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_ASR_BASE_URL` | unset | The server's base URL, ending in `/v1`. Required for `remote`. |
| `EARMARK_ASR_API_KEY` | unset | Bearer key for the server (secret). |
| `EARMARK_ASR_MAX_UPLOAD_MB` | unset | Refuse uploads larger than this, before sending. Unset means no limit. |
| `EARMARK_ASR_TIMEOUT_SECONDS` | `10800` | How long to wait for the server's answer. |

### Mail

The transcript note and the instructions go to the request's destination as two Markdown
attachments. Used when `EARMARK_MAIL_BACKEND=smtp`, which needs the host, the sender, the
username and the password. TLS is always verified and the server must offer a login; there is
no plaintext mode.

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_SMTP_HOST` | unset | The submission server's host name. Required for `smtp`. |
| `EARMARK_SMTP_PORT` | unset | Unset means 587 for `starttls` and 465 for `tls`. |
| `EARMARK_SMTP_SECURITY` | `starttls` | `starttls` (the server must offer it) or `tls` (implicit TLS). |
| `EARMARK_SMTP_USERNAME` | unset | The login name. Required for `smtp`. |
| `EARMARK_SMTP_PASSWORD` | unset | The login password, printable ASCII (secret). Required for `smtp`. |
| `EARMARK_SMTP_FROM` | unset | The sender address. Its domain is also the EHLO name and the `Message-ID` domain. Required for `smtp`. |
| `EARMARK_SMTP_TIMEOUT_S` | `60.0` | The whole SMTP session must finish within this many seconds (at most 90). |
| `EARMARK_MAX_ATTACHMENT_MB` | `10` | The transcript and the instructions together may not exceed this (MiB). Nothing is truncated or split. |

### Notifications

See "Notifications" in [docs/deploy.md](docs/deploy.md).

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_NTFY_URL` | unset | The ntfy topic URL (secret: on public ntfy.sh the topic name is the password). |
| `EARMARK_NTFY_TOKEN` | unset | ntfy access token, sent as a bearer token (secret). |

### Requests

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_ALLOWED_RECIPIENTS` | unset | Who may receive transcripts: comma-separated exact addresses, or `all` (any address: see [docs/deploy.md](docs/deploy.md) section 9). Unset refuses every request. |
| `EARMARK_MAX_UPLOAD_MB` | `1024` | Largest audio upload accepted. |
| `EARMARK_DEDUPE_WINDOW_S` | `600` | Seconds a finished job answers a repeat of the same request. |

### Fetching

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_MAX_AUDIO_MB` | `1024` | Audio download size cap. |
| `EARMARK_MAX_FEED_MB` | `20` | Feed download size cap. |
| `EARMARK_ALLOW_PRIVATE_FETCH` | `false` | Allow fetches to private addresses. Turns off a protection; see [docs/deploy.md](docs/deploy.md). |

### Worker

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_JOB_MAX_ATTEMPTS` | `4` | Attempts before a job fails. |
| `EARMARK_JOB_LEASE_SECONDS` | `900` | How long a running job is held before it is requeued after a crash. |
| `EARMARK_WORKER_POLL_SECONDS` | `2.0` | How often an idle worker looks for a job. |

### Misc

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
<!-- settings:end -->

The variables that only Docker Compose uses (`API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
`HF_HUB_DISABLE_TELEMETRY`) are listed in [docs/deploy.md](docs/deploy.md) section 4.

## Development

```bash
uv sync                      # core and dev dependencies
make check                   # ruff, ruff format (including Markdown code blocks), mypy strict, pytest
make samples                 # regenerate contract/samples/v1/ from the mail code
uv run pytest -m network     # opt-in: live Apple and feed endpoints
```

The default suite blocks sockets and needs no GPU, API key or mail server. The markers are
`network` (live Apple and feed endpoints), `slow` (model downloads), `gpu` and `smtp` (a
real mail server).

CI runs on GitHub Actions, on linux/arm64:

- **CI** (`make check (linux/arm64)`): the only required check on `main`.
- **Live tests** (`network.yml`): weekly and on demand, jobs `network` and `slow`.
- **Container image** (`container.yml`): builds the CPU image, lints `Dockerfile.gpu` with
  `docker build --check`, and runs one-off checks. Not a required check.

Steps land on the `impl` branch, and `main` is fast-forwarded after green CI.
