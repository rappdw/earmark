# earmark

Share an Apple Podcasts episode from your iPhone and get a Markdown research note in your
Obsidian vault. Self-hosted: an iOS Shortcut posts the episode link (and an optional
question) to a small API on your own machine, a worker transcribes the episode locally,
analyzes the transcript with an LLM, writes the note, and indexes it for search. Python
3.13.

## How it works

```
POST /ingest ─► jobs table (SQLite) ─► worker ─► Pipeline
  resolve ─► fetch ─► transcribe ─► analyze ─► store ─► index ─► notify
```

Every stage is a protocol with a real backend and a fake, chosen by configuration.

- **resolve**: the `apple` resolver turns the Apple link into the show's feed and the
  episode's audio URL (iTunes lookup, then the RSS feed).
- **fetch**: the `http` fetcher downloads the audio through a guarded client (scheme
  check, private-address block, size cap).
- **transcribe**: `faster_whisper` in the worker, or `remote`, an OpenAI-compatible speech
  server such as vLLM serving Whisper ([docs/asr.md](docs/asr.md)).
- **analyze**: `anthropic` (Claude) or `openai_compat`, a local server
  ([docs/llm.md](docs/llm.md)). Long transcripts are chunked, never truncated.
- **store**: the `vault` store writes the episode note, a transcript note, and entity
  notes for the people, papers, companies, tools and topics it finds.
- **index**: `lancedb` with local embeddings, for `earmark search` and `GET /search`.
- **notify**: `ntfy` sends a push notification when a job finishes or fails.

**Asking again.** `POST /episodes/{key}/analyze` or `earmark reanalyze <key> --prompt "…"`
runs a new prompt on an episode that is already transcribed. It downloads and transcribes
nothing, contacts neither Apple nor the feed, and adds a section to the note, with the
entity notes and the search index updated. `GET /episodes` or `earmark episodes` lists the
stored episodes with their keys and the prompts already run. A prompt already run with the
same `EARMARK_LLM_MODEL` makes no LLM call, and the job only regenerates the note (a note
whose managed region you edited by hand is backed up to `Backups/` first, as for any
store). Sharing the episode again from the Shortcut with a new question also works; it
looks the episode up again but does not re-download it.

The Markdown vault is the system of record. SQLite and the search index are derived and
can be rebuilt from the files on disk. Earmark writes only inside
`<vault>/<EARMARK_VAULT_SUBDIR>/`:

```
Earmark/
  Podcasts/<Show>/<YYYY-MM-DD> <Title>.md
  Podcasts/<Show>/transcripts/<YYYY-MM-DD> <Title> (transcript).md
  Entities/{People,Papers,Companies,Tools,Topics}/<Name>.md
  Backups/
  aliases.yaml          optional, yours: entity merges
```

See "Data layout" in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Planned: a weekly digest ([docs/PLAN.md](docs/PLAN.md)).

## Quickstart (fake backends)

This runs the whole service with fake backends: no network, GPU, API key, or model
download. It needs [uv](https://docs.astral.sh/uv/) and `curl`, and port 8787 free. Run it
from a checkout of this repository.

<!-- quickstart:begin -->
```bash
uv sync
uv run earmark --version

demo=$(mktemp -d)
mkdir "$demo/vault"
export EARMARK_API_TOKEN=demo-token
export EARMARK_DATA_DIR="$demo/data" EARMARK_VAULT_DIR="$demo/vault"
export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_ASR_BACKEND=fake \
  EARMARK_LLM_PROVIDER=fake EARMARK_STORE_BACKEND=fake EARMARK_INDEX_BACKEND=fake \
  EARMARK_NOTIFY_BACKEND=fake
uv run earmark doctor

uv run earmark serve &
curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
curl -s http://127.0.0.1:8787/ingest \
  -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981", "prompt": "Which papers are mentioned?"}'
uv run earmark worker --once
uv run earmark jobs
uv run earmark episodes
uv run earmark reanalyze "$(ls "$demo/data/episodes")" --prompt "Who is interviewed?"
uv run earmark worker --once
curl -s http://127.0.0.1:8787/episodes -H "Authorization: Bearer $EARMARK_API_TOKEN"
cat "$demo"/vault/Earmark/Podcasts/*.md
kill %1
wait
```
<!-- quickstart:end -->

What you see:

- `earmark doctor` passes, with warnings for the fake backends and the short token.
- `{"status":"ok"}` from the health check, then
  `{"job_id": …, "status": "queued", "created": true}` for the ingest.
- `job <id> done` from `worker --once`, and the job listed as `done` by `earmark jobs`.
- `earmark episodes` lists the fake episode with its prompt.
- The reanalyze prints a second job id, and `worker --once` prints it as `done`.
- `GET /episodes` shows both prompts.
- The fake note's Markdown, with a section for each prompt.

The episode URL is never fetched, because the resolver is fake. `kill %1` stops the
server, and `wait` lets it finish shutting down. Everything lives in a temporary
directory, so nothing is left in the repository.

**Real backends.** Install the heavy extras with `uv sync --extra transcribe --extra
index`, point `EARMARK_VAULT_DIR` at an Obsidian vault, and set `EARMARK_LLM_API_KEY`.
[docs/deploy.md](docs/deploy.md) covers the container, systemd, and how the phone
reaches the API.

## Guides

- [docs/deploy.md](docs/deploy.md): Docker Compose, systemd, reaching the API from the
  iPhone, GPU transcription, search, notifications, backups.
- [docs/shortcut.md](docs/shortcut.md): building the iOS Shortcut.
- [docs/asr.md](docs/asr.md): remote transcription servers.
- [docs/llm.md](docs/llm.md): running the analysis on a local model.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the design, contracts, and security rules.
- [docs/PLAN.md](docs/PLAN.md): the build plan and what is built so far.

## Commands

Run them as `uv run earmark <command>`.

| Command | What it does |
|---|---|
| `serve` | Run the ingest API on `EARMARK_HOST:EARMARK_PORT`. |
| `worker [--once]` | Run the job worker; `--once` processes at most one job and exits. |
| `submit <url> [--prompt]` | Enqueue an episode, the same way `POST /ingest` does. |
| `jobs [--status] [--limit] [--json]` | List recent jobs, newest first. |
| `resolve <url>` | Resolve an episode URL to its feed, GUID, and audio URL. |
| `transcribe <file> [--json]` | Transcribe a local audio file and print the result. |
| `analyze <key> [--prompt] [--json]` | Analyze an episode's stored transcript and print the analysis. |
| `episodes [--limit] [--offset] [--json]` | List stored episodes, newest share first, with the prompts already run. |
| `reanalyze <key> [--prompt]` | Run a prompt on a stored episode: enqueue a job that starts at analysis. |
| `entities rebuild [--prune]` | Rebuild the entity notes from the stored analyses. |
| `search QUERY [--limit] [--kind] [--episode] [--json]` | Search transcripts and analyses of processed episodes. |
| `reindex [--episode KEY]` | Rebuild the search index from the episode files and the vault's notes. |
| `doctor [--online]` | Check the configuration, directories, database, backends, and credentials. |
| `config` | Show the effective configuration with secrets redacted. |
| `--version` | Print the version. |

`analyze` prints an analysis and writes no note. `reanalyze` is a job: it updates the
note, the entities, and the index, and notifies.

The HTTP API is `POST /ingest`, `GET /jobs/{id}`, `GET /jobs`, `GET /search`,
`GET /episodes`, `POST /episodes/{key}/analyze`, and `GET|HEAD /healthz`. Everything
except `/healthz` needs `Authorization: Bearer $EARMARK_API_TOKEN`. See "HTTP API" in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
| `EARMARK_DATA_DIR` | `./data` | Jobs database, episode artifacts, index. A local filesystem. |
| `EARMARK_VAULT_DIR` | unset | Root of the Obsidian vault. Must exist. Required for the vault store. |
| `EARMARK_VAULT_SUBDIR` | `Earmark` | Folder inside the vault that earmark owns. |
| `EARMARK_OBSIDIAN_VAULT_NAME` | unset | The vault's name in Obsidian, for `obsidian://` links in notifications. |

### Backends

`fake` exists for tests and demos; `none` disables the index or notifications.

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_RESOLVER_BACKEND` | `apple` | `apple` or `fake`. |
| `EARMARK_FETCH_BACKEND` | `http` | `http` or `fake`. |
| `EARMARK_ASR_BACKEND` | `faster_whisper` | `faster_whisper`, `remote`, or `fake`. |
| `EARMARK_LLM_PROVIDER` | `anthropic` | `anthropic`, `openai_compat`, or `fake`. |
| `EARMARK_STORE_BACKEND` | `vault` | `vault` or `fake`. |
| `EARMARK_INDEX_BACKEND` | `lancedb` | `lancedb`, `fake`, or `none`. |
| `EARMARK_NOTIFY_BACKEND` | `ntfy` | `ntfy`, `fake`, or `none`. |

### Transcription

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_ASR_MODEL` | `large-v3` | Whisper model name (also the model id sent to a remote server). |
| `EARMARK_ASR_DEVICE` | `auto` | `auto`, `cpu`, or `cuda`. |
| `EARMARK_ASR_COMPUTE_TYPE` | `auto` | CTranslate2 compute type, such as `int8` or `float16`. |
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

### Analysis

See [docs/llm.md](docs/llm.md) for local servers.

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_LLM_MODEL` | `claude-opus-5-5` | Model id. Part of the analysis cache key. |
| `EARMARK_LLM_EFFORT` | `medium` | Claude effort: `low`, `medium`, `high`, `xhigh`, `max`. Claude only. |
| `EARMARK_LLM_FALLBACKS` | `true` | Retry a refused Claude request on the recommended fallback model. Claude only. |
| `EARMARK_LLM_BASE_URL` | unset | A local server's base URL, ending in `/v1`. Required for `openai_compat`. |
| `EARMARK_LLM_API_KEY` | unset | Anthropic API key, or the local server's key (secret). |
| `EARMARK_LLM_CONTEXT_TOKENS` | unset | Context window in tokens. Required for `openai_compat`; Anthropic defaults to 1,000,000. |
| `EARMARK_LLM_TIMEOUT_SECONDS` | `3600` | How long to wait for a local server's answer. |
| `EARMARK_LLM_RESPONSE_FORMAT` | `json_schema` | `json_schema` or `json_object`, for `openai_compat`. |

### Index

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model. Changing it needs `earmark reindex`. |
| `EARMARK_EMBED_MODEL_DIR` | unset | Where embedding models are downloaded (default: `<DATA_DIR>/models/embed`). |

### Notifications

See "Notifications" in [docs/deploy.md](docs/deploy.md).

| Variable | Default | Meaning |
|---|---|---|
| `EARMARK_NTFY_URL` | unset | The ntfy topic URL (secret: on public ntfy.sh the topic name is the password). |
| `EARMARK_NTFY_TOKEN` | unset | ntfy access token, sent as a bearer token (secret). |

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
| `EARMARK_TZ` | unset | IANA time zone for note dates, such as `Europe/Berlin`. Unset uses the machine's zone (UTC in a container). |
| `EARMARK_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
<!-- settings:end -->

For Claude, `EARMARK_LLM_API_KEY` is the API key. `ANTHROPIC_API_KEY` in the process
environment also works, but a line for it in `.env` is not read. The variables that only
Docker Compose uses (`VAULT_PATH`, `API_BIND`, and others) are listed in
[docs/deploy.md](docs/deploy.md).

## Development

```bash
uv sync                      # core and dev dependencies
make check                   # ruff, ruff format (including Markdown code blocks), mypy strict, pytest
uv run pytest -m network     # opt-in: live Apple and feed endpoints
```

The default suite blocks sockets and needs no GPU or API key. Other markers are `slow`
(model downloads), `llm` (the real Claude API), `gpu`, and `index` (real LanceDB).

CI runs on GitHub Actions, on linux/arm64:

- **CI** (`make check (linux/arm64)`): the only required check on `main`.
- **Index tests**: the LanceDB tests with the `index` extra.
- **Live tests** (`network.yml`): weekly and on demand, jobs `network` and `slow`.
- **LLM test** (`llm.yml`): on demand only, in the `llm` environment.
- **Container image** (`container.yml`): builds the image without pushing it and runs three
  one-off checks in it. Not a required check.

Steps land on the `impl` branch, and `main` is fast-forwarded after green CI.
