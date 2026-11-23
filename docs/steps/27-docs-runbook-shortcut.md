# Step 27: Docs, runbook and Shortcut for the transcription service

The brief is in `docs/PLAN.md`, "Step 27". This step changes Markdown documents and the
doc-driven tests. It changes no source code, no deploy file, no workflow and no
`.env.example`. The step can be done as briefed. No product decision is needed.

Two rules for the implementer that are not about the docs themselves:

- **Do not run any `git stash` command.** The operator keeps local runbook phase-0 ticks in a
  stash. This rewrite supersedes them, but the stash is theirs.
- **Placeholders only**, in every file this step touches: addresses at `example.org`,
  `example.net` or `example.com`; the IPs `192.168.1.10`, `192.168.1.0/24`, `127.0.0.1`,
  `0.0.0.0` and `1.1.1.1`; the hosts `spark`, `earmark.example.com`, `smtp.example.org`,
  `ntfy.example.com` and `spark.tailnet-name.ts.net`; and `<…>` for every secret. Never put
  a real LAN address, host name, mail address or token in a doc, a test or this plan.
  Section 6 adds a test that enforces this.

## 1. Outcome

When the step is done:

- **The README** describes the transcription and delivery service. Its quickstart runs
  verbatim on fake backends. It posts a JSON request with a `timestamp` and a multipart
  upload, runs the worker, and decodes the fake mailer's `.eml` files from
  `data/outbox/`. It exports only variables that `Settings` knows.
- **`docs/shortcut.md`** builds one iOS Shortcut. "Choose from Menu" picks Personal or Work
  and fills `to`. "Ask for Input" gives `instructions`. "Format Date" turns Current Date into
  ISO 8601 for `timestamp`. A shared file is posted to `POST /transcriptions` as a `Form`
  (multipart) upload, and anything else as JSON with the URL. Its JSON example and its
  upload field table are both posted to the real app by the tests and accepted.
- **`docs/deploy.md`** describes the service as built. It covers the CPU API image and the
  GPU worker image, the sending mailbox (SPF, DKIM, DMARC, app password, the PKA
  allowlisting), `EARMARK_ALLOWED_RECIPIENTS` and `all`, upgrading from v0.1.0, large
  uploads through the proxy, and a worker on a host without a GPU. Section numbers stay as
  they are. Nothing in it describes the vault, analysis, search, the digest or vLLM. The
  dangling sentence in section 4 is gone.
- **`docs/runbook-spark.md`** is rewritten for the GPU image. It has phases 0–10, each
  ending in a **Check**. It includes the step-26 Spark smoke test, the email setup, and a
  real delivery to the operator's own address whose check opens both attachments and
  confirms the body names them. It also covers the PKA recipients, upgrading from v0.1.0,
  reachability, ntfy and the Shortcut.
- **`docs/asr.md`** and the 2026-11-20 decision record no longer call the vLLM server the
  Spark's GPU path.
- **`tests/test_docs.py`** and **`tests/doc_helpers.py`** test all of this: the doc tests
  pass, and they would fail if a doc drifted from the code.

## 2. Findings

### 2.1 The code as it is (read on 2026-11-23, commit `14bb218`)

- **Settings fields** (`uv run python -c …`): `API_TOKEN, HOST, PORT, DATA_DIR,
  RESOLVER_BACKEND, FETCH_BACKEND, ASR_BACKEND, NOTIFY_BACKEND, MAIL_BACKEND, ASR_MODEL,
  ASR_DEVICE, ASR_COMPUTE_TYPE, ASR_BATCH_SIZE, ASR_LANGUAGE, ASR_MODEL_DIR, ASR_BASE_URL,
  ASR_API_KEY, ASR_MAX_UPLOAD_MB, ASR_TIMEOUT_SECONDS, KEEP_AUDIO, ALLOWED_RECIPIENTS,
  MAX_UPLOAD_MB, DEDUPE_WINDOW_S, NTFY_URL, NTFY_TOKEN, MAX_AUDIO_MB, MAX_FEED_MB,
  ALLOW_PRIVATE_FETCH, JOB_MAX_ATTEMPTS, JOB_LEASE_SECONDS, WORKER_POLL_SECONDS, SMTP_HOST,
  SMTP_PORT, SMTP_SECURITY, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM, SMTP_TIMEOUT_S,
  MAX_ATTACHMENT_MB, LOG_LEVEL`, all prefixed `EARMARK_`. The README settings table already
  covers them, and `tests/test_docs.py` passes at HEAD (169 tests across `test_docs`,
  `test_deploy` and `test_config`).
- **CLI commands:** `config doctor serve worker submit jobs resolve transcribe`, plus
  `--version`. `serve`'s help still says "Run the ingest API". That is code, so it is out of
  scope; the README's own wording is "Run the API".
- **The README quickstart is stale.** It exports `EARMARK_VAULT_DIR`,
  `EARMARK_LLM_PROVIDER`, `EARMARK_STORE_BACKEND` and `EARMARK_INDEX_BACKEND`, and it runs
  `earmark episodes`, `reanalyze` and `digest`, which no longer exist, plus
  `GET /episodes`. The current doc tests do not catch any of this. Section 6 adds the tests
  that do.
- **`docs/deploy.md`** still describes the vault, analysis (section 8), search (section 9)
  and the digest (section 14). The Shortcut URLs end in `/ingest` (6.1.7, 6.2), and section
  4 ends with a dangling "It runs as a non-root user…". Sections 1, 4, 7 and 17 were
  partly updated in step 26.
- **Section numbers that files outside `docs/` refer to.**
  - `Dockerfile.gpu` line 4 and `compose.yaml` line 3 say "docs/deploy.md section 7"
    (the GPU), and `tests/test_deploy_gpu.py` line 3 says the same.
  - `.env.example` line 86 says "docs/deploy.md section 17" (telemetry).
  - `tests/test_docs.py::test_deploy_doc_hf_telemetry_section` needs the headings
    `## 17. Hugging Face telemetry and logs` after `## 16. Troubleshooting`.
  - `docs/shortcut.md` refers to sections 6, 6.1.6 and 10.

  So the numbering is kept, and this step needs no edit outside `docs/` and `tests/`.
- **The runbook has a dangling link.** It points to
  `deploy.md#7b-the-gpu-profile-the-recommended-gpu-path-untested`, which step 26 removed.
  `relative_links` strips `#…`, so no test caught it. Section 6 adds an anchor test.
- **API error texts**, exact (`api/auth.py`, `api/routes.py`, `api/app.py`,
  `api/upload.py`, `recipients.py`, `resolve/apple.py`):
  - `missing or invalid API token`
  - `request body is larger than 64 KiB`
  - `send JSON (Content-Type: application/json) or a multipart form (Content-Type: multipart/form-data)`
  - `request body is missing; send a JSON object with url and to`
  - `the upload is larger than the {limit} limit (EARMARK_MAX_UPLOAD_MB)`, where `{limit}`
    is `fetch.limit_text`, for example `1024 MiB`
  - `the file does not look like audio: send it with an audio content type such as …`
    (`upload.py` line 45)
  - `the form must have exactly one file field named 'file'`
  - `no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server`
  - `to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)`
  - `to must be one email address`
  - the `UnsupportedUrlError` messages for `malformed`, `show_link`, `not_episode` and
    `credentials`
  - `the job store is unavailable; try again shortly` (503)

  The current Shortcut doc shows `Content Too Large` for 413, which is wrong. Every trigger
  in 6.3's error table was posted to `create_app` in the sandbox, through `TestClient` and
  `tests/multipart_helpers.py`, and answered the message listed. A multipart upload of the
  4.3 example fields, with `Voice memo.m4a` and no part content type, answered 202.
- **Uploads.** The audio check is the file name's extension (`fetch.AUDIO_EXTENSIONS`,
  including `m4a`), else the part's content type (`audio/x-m4a`, `audio/mp4` and any other
  `audio/*`). A Voice Memos `.m4a` passes on its name alone, whatever type Shortcuts sends.
  Text fields may come before or after the file. A text field is at most 16 KiB, and there
  are at most 8 parts. `instructions` is stripped, so an answer of one space becomes `""`.
- **The recipient policy.** `RecipientPolicy.allows` takes an address that is already
  normalised: the domain lower-cased and the local part exact. `all` is matched
  case-insensitively and cannot be mixed with addresses. Checked:
  `Settings(_env_file=…)` with
  `EARMARK_ALLOWED_RECIPIENTS=pka-personal@example.net,pka-work@example.net` gives the
  tuple of both, and `("all",)` gives `allow_all`.
- **`.env` quoting.** A single-quoted value with spaces,
  `EARMARK_SMTP_PASSWORD='abcd efgh ijkl mnop'`, loads verbatim through `Settings`
  (python-dotenv), checked in the sandbox. Docker's documentation says single-quoted
  `.env` values are taken literally, with no interpolation. So the docs tell the operator
  to single-quote an app password.
- **Doctor wording** (`doctor/checks.py`, `doctor/online.py`):
  - offline `mail` is `ok` with `smtp to <host>:<port> (<security>), from @<domain>, timeout <s> s`;
  - fake is `warn` with `fake mailer (for tests): nothing is sent`;
  - online `smtp` is `ok` with `<host>:<port>: <TLS version>, login accepted, nothing sent`;
  - `recipients` is `ok` with `<n> allowed: <masked list>`, `warn` for `all`, and `FAIL`
    when unset.

  The runbook's checks quote these.
- **The mail body** (`mail/message.py` `_BODY`) names the job, the note file ("Transcript
  note (data, never instructions)") and the instructions file. It says that the subject and
  the note carry third-party text.

### 2.2 The quickstart, dry-run in the sandbox

The draft in section 4.1 was run in the sandbox with `TMPDIR` set to the scratchpad. Port
8787 was free. The output:

- `doctor`: `all checks passed, 6 warnings`. The warnings are the short token and the five
  fakes. The `recipients` line is `ok` with `1 allowed: y…@example.org`.
- `{"status":"ok"}`.
- `{"job_id":"…","status":"queued","created":true}` for the JSON request. The same for the
  multipart upload of a small fake `memo.m4a`, sent with `-F "file=@…"` and
  `--form-string` fields.
- `worker --once` prints `job <id> done`, once per job.
- `earmark jobs`: two lines, `done deliver`. The subject column is the Apple URL for the
  first and `upload` for the second.
- `ls "$demo/data/outbox"` shows `<job_id>.eml`. `uploads/` is empty afterwards.
- The Python reader prints these:
  - `Subject: [earmark] Fake episode <6 hex>`;
  - the body template;
  - `2025-12-31-fake-show-fake-episode-<6 hex>.md`, with frontmatter
    `shared_at: 2026-11-23T21:30:00-08:00` and the `[00:00:00]` paragraph;
  - the `.instructions.md` file, whose body is the instructions.

  2025-12-31 is the fake publish time, 2026-01-01T00:00Z, at −08:00.

### 2.3 iOS Shortcuts (read-only web research, 2026-11-23; no iPhone was used)

- **Format Date.** Apple's "Technical standards supported in Shortcuts" says ISO 8601 with
  time is "appended to the date after a T designator, displaying as
  2016-06-01T14:41:36-08:00" (support.apple.com/guide/shortcuts/apdfd459e13d/ios).
  Matthew Cassinelli's action reference names the toggle `Include ISO 8601 Time`. That
  form validates as pydantic `AwareDatetime` with a whole-minute offset, which is what
  `utc_offset_min` needs. Whether the offset is the phone's current zone is inferred, not
  documented. Runbook phase 9's check confirms it on a real share.
- **Get Contents of URL.** Apple's guide says only "Request Body allows you to send JSON, a
  Form, or a File". Cassinelli's reference says: "To make a multipart HTTP request, choose
  'Form' as the request body type and add files as field values". Community write-ups
  (IndieWeb, upload guides) use `Form` with a field named `file` set to Shortcut Input.
  - **Unverified:** whether a `Form` body is always `multipart/form-data`, or
    `application/x-www-form-urlencoded` when it has no file. Ours always has the file, and
    a urlencoded body would get the 415 message, which the Shortcut shows.
  - **Unverified:** the file part's content type. That does not matter here: the `.m4a`
    name passes.
  - **Dead end:** an Apple Community thread reports that a `Content-Type` header added by
    hand is ignored for Form bodies. So the doc keeps "`Authorization` is the only header to
    add".
- **Get Type.** The action "returns the type of every item passed as input" (Cassinelli,
  `is.workflow.actions.getitemtype`). Its result is Text, and for a URL it is "URL". No
  source lists the value for a shared file. "File" is the expected value, and the doc says
  how to check it (shortcut.md section 6).
- **Choose from Menu.** Cassinelli's reference says the action "passes input through as
  output". It does not document that the menu's output is the last action in the chosen
  branch. **Dead end: do not rely on "Menu Result".** Each option sets a variable
  (`Text` → `Set Variable` `Recipient`). That is unambiguous.
- **Ask for Input** gives `Provided Input`. Whether it accepts an empty answer is still
  unverified. The doc's fallback is a Default Answer of one space, which the API strips to
  `""` (2.1).
- **"If Result"**, the output of an If block, is used by the final **Show Result**. It is
  widely used but not in Apple's guide, so the doc marks it unverified. Its fallback is a
  Show Result in each branch.

### 2.4 Caddy and large uploads (caddyserver.com docs, 2026-11-23)

- **Timeouts.** The global `servers { timeouts { … } }` options default to: `read_body`
  "no timeout", `write` "no timeout", `read_header` 1 m, `idle` 5 min.
- **Body size.** `request_body` `max_size` has no documented default, and the example
  Caddyfile sets none.
- **Buffering.** `reverse_proxy`'s `request_buffers` is opt-in, and the docs call it
  "very inefficient". So by default the proxy streams the request body to earmark.

So the example Caddyfile needs no change for uploads up to `EARMARK_MAX_UPLOAD_MB`. None of
this has been run. How long Shortcuts waits for a large upload is unknown. The API answers
202 only after the whole body has arrived.

### 2.5 Compose override for a host without a GPU (docs.docker.com, merge reference)

- **`!reset`.** It removes a value set by the base file, for example `!reset null` or
  `!reset []`.
- **Mappings** are merged key by key, so `build.dockerfile` and single `environment` keys
  can be overridden.
- **`compose.override.yaml`** is merged automatically.

The page does not say which Compose version introduced `!reset`. The worker already needs
2.30 or later, and the override is only for other hosts. It has not been run. Its check is
`docker compose config`, which must show no `gpus` under `worker`.

### 2.6 Email authentication (general knowledge, stated as such in the docs)

- **What earmark sends.** It uses `EARMARK_SMTP_FROM` as both the `From` header and the
  envelope sender, and authenticates to the provider's submission server.
- **DKIM.** DMARC passes when the provider signs with a DKIM key for the From domain
  (DKIM alignment).
- **SPF** alignment depends on whether the provider uses the operator's domain as the
  envelope sender. Many rewrite it, so DKIM alignment is what to rely on.
- **Records.** The exact SPF include and DKIM records come from the provider. The docs give
  their shape with placeholders.
- **The check** is the receiving mailbox's `Authentication-Results` header showing
  `dkim=pass` for `header.d=example.org` and `dmarc=pass`.

### 2.7 Not verifiable here

- The Shortcut on a phone.
- The GPU image, the Spark smoke test and the compose override.
- TLS against a real SMTP server, DMARC at a real recipient, and a PKA reading the
  attachments.
- Caddy forwarding a large upload.

The docs say so in each place. The runbook turns each of them into an operator check.

## 3. Files

| File | Change |
|---|---|
| `README.md` | Rewrite (4.1). |
| `docs/deploy.md` | Rewrite, keeping section numbers 1–17 (4.2). |
| `docs/shortcut.md` | Rewrite (4.3). |
| `docs/runbook-spark.md` | Rewrite (4.4). |
| `docs/asr.md` | Two small edits (4.5). |
| `docs/decisions/2026-11-20-gpu-and-reachability.md` | A dated "superseded" note at the top (4.6). |
| `docs/ARCHITECTURE.md` | Already edited by this planner (section 8, D9). The implementer changes it only if a test name or marker below changes. |
| `tests/doc_helpers.py` | New helpers (5.1). |
| `tests/test_doc_helpers.py` | Tests for the new helpers. |
| `tests/test_docs.py` | Updated and new tests (section 6). |

Nothing else changes. That includes `src/`, `Dockerfile*`, `compose.yaml`, `deploy/`,
`.github/`, `.env.example`, `CLAUDE.md` and `docs/PLAN.md`.

## 4. Interfaces: the documents

The markers, headings and phrases named here are what the tests read. Wording elsewhere is
the implementer's, in the house style: short declarative sentences, no marketing, "not
verified" said plainly.

Placeholders used throughout:

| Placeholder | Meaning |
|---|---|
| `spark`, `192.168.1.10` | The Spark and its LAN address |
| `earmark.example.com`, zone `example.com` | The API's name |
| `example.org` | The operator's mail domain |
| `earmark@example.org` | The sending mailbox |
| `smtp.example.org` | The provider's submission host |
| `you@example.org` | The operator's own inbox |
| `pka-personal@example.net`, `pka-work@example.net` | The two PKA addresses |

### 4.1 `README.md`

Sections, in order:

1. **`# earmark`** and an intro.
   - It is a self-hosted transcription and delivery service.
   - An iOS Shortcut, curl or `earmark submit` sends an Apple Podcasts episode link, a
     direct audio URL or an audio file, plus a destination address and instructions.
   - The worker transcribes it with faster-whisper, on the DGX Spark's GPU in the GPU
     image.
   - The transcript note and the instructions are emailed to that address as two Markdown
     attachments, and an ntfy push follows.
   - The operator's PKA instances do the analysis and filing. Python 3.13.
2. **`## How it works`.**
   - A diagram: `POST /transcriptions ─► jobs table (SQLite) ─► worker`, then
     `resolve ─► fetch ─► transcribe ─► deliver ─► notify`.
   - One bullet per stage, with its real and fake backends.
   - A short paragraph on the email:
     - the two files and their names (`YYYY-MM-DD-<slug>.md` and `.instructions.md`);
     - that the body is earmark's own text;
     - at-least-once delivery;
     - that a re-share is a new job reusing the cached transcript.

     It points to "Delivering" and the mail contract in `docs/ARCHITECTURE.md`.
   - It says what earmark keeps: the job table and a transcript cache. It is not a system
     of record.
3. **`## Quickstart (fake backends)`.** The fenced `bash` block between
   `<!-- quickstart:begin -->` and `<!-- quickstart:end -->`, exactly in this shape. The
   implementer may adjust wording in comments, but not the commands' effect.

   ````text
   ```bash
   uv sync
   uv run earmark --version

   demo=$(mktemp -d)
   export EARMARK_API_TOKEN=demo-token
   export EARMARK_ALLOWED_RECIPIENTS=you@example.org
   export EARMARK_DATA_DIR="$demo/data"
   export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_ASR_BACKEND=fake \
     EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
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
   ````

   Then **What you see**, matching 2.2:
   - doctor passes, with warnings for the fakes and the short token;
   - two 202 responses;
   - `job <id> done` twice;
   - two `done` jobs at stage `deliver`;
   - two `.eml` files;
   - for each, the subject, the generated body, the note with its `[00:00:00]` paragraph
     and `transcript_sha256 ok`, and the instructions file.

   Also say:
   - the note's dates use the request's `-08:00` offset;
   - the fake resolver never fetches the Apple URL;
   - the fake transcriber accepts any bytes, and a real one needs audio;
   - `kill %1` and `wait` stop the server;
   - everything lives in the temp directory.

   **The heredoc is Python inside a bash fence.** `ruff format --check` formats only
   `python` fences, so it is not formatted, but keep it ruff-clean anyway.
4. **Real backends.**
   - `uv sync --extra transcribe` for local faster-whisper.
   - Set `EARMARK_ALLOWED_RECIPIENTS` and the `EARMARK_SMTP_*` settings
     ([docs/deploy.md](docs/deploy.md) section 8).
   - On the Spark, use compose with the GPU image (section 7).
5. **`## Guides`.** Links to deploy, shortcut, runbook, asr, ARCHITECTURE and PLAN, each
   with a one-line description. Do not mention search, the vault or notes in a vault.
6. **`## Commands`.** The existing table, with `serve` as "Run the API". Keep the HTTP API
   paragraph that starts `The HTTP API is` exactly, with its four routes.
7. **`## Configuration`.** The existing `<!-- settings:begin/end -->` block, kept
   table-for-table: one row per field, secrets "unset" with "secret" in the meaning. Fix
   these:
   - `EARMARK_DATA_DIR`: "Jobs database, transcript cache, upload and download staging, and
     the fake mailer's outbox. A local filesystem."
   - The Backends intro: "`fake` exists for tests and demos; `none` disables
     notifications."
   - `EARMARK_ALLOWED_RECIPIENTS`: "… comma-separated exact addresses, or `all` (any
     address: see [docs/deploy.md](docs/deploy.md) section 9). Unset refuses every request."

   Then replace the paragraph after the block (the Claude key and `VAULT_PATH`) with one
   sentence: the compose-only variables (`API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
   `HF_HUB_DISABLE_TELEMETRY`) are listed in [docs/deploy.md](docs/deploy.md) section 4.
8. **`## Development`.**
   - The markers are `network`, `slow`, `gpu` and `smtp`. Drop `llm` and `index`.
   - The workflows are **CI** (`make check (linux/arm64)`, the only required check),
     **Live tests** (`network.yml`, weekly and on demand, jobs `network` and `slow`), and
     **Container image** (`container.yml`: builds the CPU image, lints `Dockerfile.gpu` with
     `docker build --check`, runs one-off checks; not required).
   - Drop "Index tests" and "LLM test".

### 4.2 `docs/deploy.md`

Keep every section number and the headings that tests read. The section list:

1. **`## 1. What is tested`.** Keep these phrases: "build-tested in CI", "not
   run-tested", "GPU image is untested", "only real check".
   - Drop "and timer" and the digest sentence.
   - Add two bullets:
     - Email delivery is tested only against an in-process SMTP stub. TLS against your
       server, DMARC at the recipient, and a PKA reading the attachments are operator checks
       (runbook phases 5 and 6).
     - The Shortcut has never run on a phone.
2. **`## 2. Overview`.**
   - The API and the worker share one data volume.
   - The worker sends mail through your provider's submission server, and pushes through
     ntfy.
   - "The API is never exposed publicly" stays.
   - Extend the diagram with a line `worker ──(SMTP submission, TLS)──► smtp.example.org ──►
     PKA mailboxes`.
   - Remove the vault.
3. **`## 3. Requirements`.**
   - aarch64 or x86_64 Linux.
   - Docker Engine with Compose 2.30 or later for the GPU worker (`gpus: all`), or uv for a
     native, CPU-only install.
   - A local filesystem for the data.
   - A sending mailbox on a domain you control (section 8).
   - Disk, as listed:
     - the CPU image is about 1 GB;
     - the GPU build pulls a CUDA devel base of about 6 GB compressed;
     - the runtime base is about 3 GB compressed;
     - Whisper `large-v3` is about 3 GB.

     Say the total build footprint was not measured.
   - Remove the vault and embedding lines.
4. **`## 4. Install with Docker Compose`.**
   - The `.env` bullets: `EARMARK_API_TOKEN` (the generator command),
     `EARMARK_ALLOWED_RECIPIENTS` (section 9), the `EARMARK_SMTP_*` settings (section 8),
     and optionally ntfy (section 10). `APP_UID` and `APP_GID` are only needed if 1000 is
     unsuitable.
   - Remove `EARMARK_LLM_API_KEY`, `VAULT_PATH`, `EARMARK_TZ` and
     `EARMARK_OBSIDIAN_VAULT_NAME`.
   - Keep the build, doctor, up and logs commands.
   - Replace the paragraph about `compose.yaml` and the vault mount:
     - `compose.yaml` defines `api` (`earmark serve`, the CPU image `earmark:local`) and
       `worker` (`earmark worker`, the GPU image `earmark:gpu`, `gpus: all`, section 7);
     - both share the named volume `data` at `/data`.
   - **The compose-only table** keeps exactly the rows `API_BIND`, `APP_UID`, `APP_GID`,
     `EXTRAS` and `HF_HUB_DISABLE_TELEMETRY`. `tests/test_deploy.py` needs `` | `NAME` | ``
     rows.
     - `APP_UID` is "User id both images run as; the data volume belongs to it."
     - `EXTRAS` is "Optional extras in the CPU image (`api`). `serve` needs none, but a
       CPU-only worker (7.2) needs `transcribe`."
   - **Things to know**, updated:
     - **What compose overrides.** `EARMARK_DATA_DIR`, `EARMARK_HOST`, `EARMARK_PORT` and
       `EARMARK_ASR_MODEL_DIR` for both services. For the worker, also
       `EARMARK_ASR_DEVICE=cuda` (forced) and `EARMARK_ASR_COMPUTE_TYPE` (default
       `float16`).
     - **The data volume** holds the database, the transcript cache, `uploads/`,
       `downloads/`, `outbox/` (fake mailer only) and `huggingface/`.
     - **Ownership:** keep the `chown` fix.
     - Rootless Docker and `userns-remap` are not covered.
     - No `$` in `.env` values, and **single-quote a value that has spaces**, such as an app
       password.
     - **The environment is exported:** every `.env` line, the SMTP password included,
       reaches both containers. The Cloudflare token must never be in this file. Drop the
       Anthropic sentence.
     - `API_BIND` stays loopback: keep the text.
   - **Replace the dangling paragraph** at the end of the section with: "Both images run as
     the non-root user `earmark`, have no health check of their own (compose checks the
     API), and contain no `.env` file; compose passes the settings in."
5. **`## 5. Install natively with systemd`.**
   - `uv sync --locked --no-dev --extra transcribe` (drop `--extra index`).
   - `.env` as in section 4, plus `EARMARK_HOST=127.0.0.1` and
     `EARMARK_DATA_DIR=/srv/earmark/data`.
   - Copy the two units. Remove the vault `ReadWritePaths=` edit.
   - Keep the telemetry sentence and the doctor check.
   - Add: a native worker runs on the CPU, because the PyPI CTranslate2 wheel for
     linux/aarch64 has no CUDA. Set `EARMARK_ASR_DEVICE=cpu` and a smaller
     `EARMARK_ASR_MODEL` such as `small`, or use the container for the GPU.
6. **`## 6. Reaching the API from the iPhone and the Mac`.** Keep 6.1.1–6.1.6 as they are.
   Every phrase `test_deploy_doc_reachability` reads must survive.
   - **6.1.7 Test:** the Shortcut URL is `https://earmark.example.com/transcriptions`.
   - **6.2:** the Tailscale URL is `https://spark.tailnet-name.ts.net/transcriptions`.
   - **New `### 6.3 Uploads through the proxy`**, from 2.4:
     - Caddy's defaults set no body size limit and no body read or write timeout, and its
       proxy streams the request body;
     - the example Caddyfile adds nothing that limits uploads;
     - the limit is `EARMARK_MAX_UPLOAD_MB` (413 above it);
     - the API answers only after the whole file has arrived, so a large voice memo on
       cellular takes as long as the upload;
     - how long the Shortcut waits is not documented;
     - a Caddy `request_body { max_size … }` lower than the earmark cap would turn big
       uploads into proxy errors;
     - Tailscale Serve's limits are not verified.
7. **`## 7. Transcription on the DGX Spark`.** Keep 7.1, 7.3 (the `gpu-smoke` markers and
   content, unchanged) and 7.4.
   - In 7.1, after the notes, add: "Record the build time and the image size; runbook phase
     3 asks for them."
   - **Rewrite 7.2 "Without a GPU"** fully:
     - **Remote.** The `remote` backend for a GPU server elsewhere
       ([docs/asr.md](asr.md)).
     - **The CPU image.** For a worker on a host without a GPU, create
       `compose.override.yaml` next to `compose.yaml`. Compose reads it automatically. Use
       this fenced `yaml` block, after a `<!-- cpu-worker-override -->` marker:

       ```yaml
       services:
         worker:
           image: earmark:local
           build:
             dockerfile: Dockerfile
           gpus: !reset null
           environment:
             EARMARK_ASR_DEVICE: cpu
             EARMARK_ASR_COMPUTE_TYPE: int8
       ```

     - The worker then uses the CPU image (with `EXTRAS` including `transcribe`), and
       `int8` on the CPU.
     - **Check:** `docker compose config` shows the worker with `image: earmark:local` and
       no `gpus`.
     - Say it has not been run: `!reset` needs a recent Compose, and the worker's GPU
       setup already needs 2.30.
     - Use a smaller `EARMARK_ASR_MODEL` on a CPU (`small`, `distil-large-v3`).
     - Remove "step 27 covers it fully".
8. **`## 8. Email delivery`** (replaces "Analysis"). Subsections:
   - **8.1 What earmark sends.**
     - One message per job, to the one `to`, from `EARMARK_SMTP_FROM`.
     - Two Markdown attachments, the note and the instructions file (named as in the README).
     - A body that earmark generates, naming both files and the job id.
     - The subject is `[earmark] <title>`.
     - Delivery is at-least-once, and a resend keeps the `Message-ID` and the job id. The
       PKA ignores a duplicate.
     - Point to the mail contract in ARCHITECTURE. Say it is **DRAFT** until you approve
       the PKA's receiving procedure.
   - **8.2 A sending mailbox on your domain.**
     - Create `earmark@example.org` at a provider that offers SMTP submission (587 with
       STARTTLS, or 465 with TLS) and per-app passwords.
     - Use a dedicated mailbox, not your personal one: the PKAs allowlist this sender.
     - Name no provider.
   - **8.3 SPF, DKIM and DMARC.** Why: each PKA's runtime reads attachments only from an
     allowlisted sender whose mail passes DMARC. Give the record shapes with placeholders,
     and say the provider supplies the exact values:
     - SPF: `example.org TXT "v=spf1 include:<provider's SPF domain> -all"`. Keep one SPF
       record per domain, merged with any existing one.
     - DKIM: the provider's records at `<selector>._domainkey.example.org`, usually CNAMEs.
     - DMARC: `_dmarc.example.org TXT "v=DMARC1; p=quarantine; rua=mailto:<reports address>"`.
       Use `p=none` only while testing, and move to `quarantine` or `reject` once a test
       passes.

     Then the alignment note from 2.6. **Check:** `dig +short TXT example.org`,
     `dig +short TXT _dmarc.example.org` and `dig +short CNAME <selector>._domainkey.example.org`
     show the records. Then, after a real send (runbook phase 5), the received message's
     `Authentication-Results` header shows `dkim=pass` with `header.d=example.org` and
     `dmarc=pass`.
   - **8.4 The app password and the settings.** A fenced `bash` block of `.env` lines:

     ```bash
     EARMARK_MAIL_BACKEND=smtp
     EARMARK_SMTP_HOST=smtp.example.org
     EARMARK_SMTP_SECURITY=starttls
     EARMARK_SMTP_USERNAME=earmark@example.org
     EARMARK_SMTP_PASSWORD='<app password>'
     EARMARK_SMTP_FROM=earmark@example.org
     ```

     Also say:
     - `EARMARK_SMTP_PORT` defaults to 587 or 465;
     - `EARMARK_SMTP_TIMEOUT_S` defaults to 60, at most 90;
     - `EARMARK_MAX_ATTACHMENT_MB` defaults to 10 MiB, and a three-hour transcript is about
       300 KB. Set it at or below any attachment cap the PKAs' mail lane has;
     - the password is printable ASCII, and spaces are allowed: single-quote it;
     - TLS is always verified, AUTH happens only after TLS, and there is no plaintext mode;
     - a server without STARTTLS fails delivery.

     **Check:** `docker compose run --rm worker doctor` shows `ok mail smtp to
     smtp.example.org:587 (starttls), from @example.org, timeout 60 s`. `doctor --online`
     shows `ok smtp smtp.example.org:587: TLSv1.3, login accepted, nothing sent` (the TLS
     version is the server's). Doctor never sends mail.
   - **8.5 The PKA side.**
     - Ask each PKA to allowlist `earmark@example.org` at its runtime. The operator arranges
       this with those agents. earmark cannot do it, and cannot see whether it was done:
       SMTP's 250 reply is all it learns.
     - The test is a PKA confirming that it read both attachments.
   - **8.6 Reading a message without sending.**
     - `EARMARK_MAIL_BACKEND=fake` writes each message to `data/outbox/<job_id>.eml` (mode
       0600) and sends nothing.
     - Under compose, copy them out with `docker compose cp api:/data/outbox ./outbox`
       while `api` runs.
     - Read them with the README quickstart's Python reader, run with `python3` on the host.
       Do not copy the reader here: link to the README quickstart.
9. **`## 9. Recipients: EARMARK_ALLOWED_RECIPIENTS`** (replaces "Search").
   - Every request names one `to`, and earmark never defaults it.
   - The setting is a comma-separated list of exact addresses. The local part is
     case-sensitive and the domain is not, so `Pka-Work@example.net` is a different address.
   - Unset refuses every request with `no recipients are allowed yet: set
     EARMARK_ALLOWED_RECIPIENTS on the server`, and doctor FAILs.
   - Then the allowlist example, a marker `<!-- allowlist-example -->` followed by a fenced
     `bash` block containing exactly one line:
     `EARMARK_ALLOWED_RECIPIENTS=pka-personal@example.net,pka-work@example.net`.
   - **`all`**, in its own paragraph:
     - it accepts any single, syntactically valid address (ASCII, at most 254 characters);
     - it is your written acceptance of a risk: anyone holding the API token can have
       earmark mail anyone from your domain, which costs your domain's reputation, and can
       instruct both PKAs;
     - `serve` logs a WARNING at startup, and doctor warns;
     - there is no send-rate cap (backlog);
     - `all` cannot be mixed with addresses.
   - **Changing it.** Recreate both containers (`docker compose up -d`): the API checks `to`
     at request time, and the worker checks again at delivery. A job queued for an address
     you removed fails with "the destination is no longer allowed by
     EARMARK_ALLOWED_RECIPIENTS; nothing was sent".
   - Logs and pushes show addresses masked (`p…@example.net`). `GET /jobs` shows them in
     full to token holders.
10. **`## 10. Notifications (ntfy)`.**
    - Drop the Obsidian bullet.
    - Add what a push says, from ARCHITECTURE "Notifying":
      - `Sent: <title>` with `To: <masked>` and the duration;
      - `Failed: <title>` with the stage and the reason, and "The email may already have
        been delivered." when that applies;
      - "Resent after an interruption.";
      - no URL path, instructions, full address or guid.
11. **`## 11. Fetching and DNS filtering`.**
    - Direct audio URLs from requests go through the same guard as enclosures.
    - The last bullet becomes "The remote ASR, SMTP and ntfy servers do not need it."
12. **`## 12. Stopping, restarts, and leases`.** Add that a delivery in progress finishes
    first, within `EARMARK_SMTP_TIMEOUT_S`, and the grace periods (2 min compose, 120 s
    unit) cover it.
13. **`## 13. Checking the setup`.** Mention the `recipients`, `mail`, `asr cuda` and
    online `smtp` lines.
14. **`## 14. Upgrading from v0.1.0`** (replaces "Weekly digest"). The whole body of the
    section sits between `<!-- upgrade-v0.1.0:begin -->` and
    `<!-- upgrade-v0.1.0:end -->`. It covers:
    - **What changed.** earmark no longer analyses, writes a vault, indexes or digests. It
      refuses a v0.1.0 database and does not migrate it. **The vault is no longer touched:**
      your notes stay as they are, and nothing reads or writes the vault.
    - **Compose**, as a fenced `bash` block:

      ```bash
      docker compose down --remove-orphans
      git pull && docker compose build
      docker compose run --rm --no-deps --entrypoint sh api -c '
        cd /data && mkdir -p v0.1.0 &&
        for f in earmark.db earmark.db-wal earmark.db-shm; do [ ! -e "$f" ] || mv "$f" v0.1.0/; done &&
        rm -rf episodes/*/analyses lancedb models/embed digests vault.lock index.lock &&
        ls -A /data /data/v0.1.0'
      docker volume rm earmark_asr-models 2>/dev/null || true
      ```

      Explain each line:
      - the old database moves aside with its `-wal` and `-shm` files;
      - `analyses/`, the `lancedb/` index, the `models/embed/` embedding model, `digests/`
        and the lock files are deleted;
      - `huggingface/` (the Whisper models) stays;
      - the `asr-models` volume came from the old `gpu` profile.

      Also say that transcripts cached by v0.1.0 carry no ASR profile and are never reused.
      Delete `episodes/` too if you do not want their text.
    - **Native**, the same under `/srv/earmark/data` with `sudo -u earmark sh -c '…'`, plus:
      - `sudo systemctl disable --now earmark-digest.timer`;
      - delete `/etc/systemd/system/earmark-digest.service` and `.timer`;
      - copy the two new units;
      - `daemon-reload`.
    - **`.env`.** Delete these lines; they are ignored, but some now mislead:
      - `EARMARK_LLM_*`, `ANTHROPIC_API_KEY`;
      - `EARMARK_VAULT_DIR`, `EARMARK_VAULT_SUBDIR`, `EARMARK_OBSIDIAN_VAULT_NAME`;
      - `EARMARK_STORE_BACKEND`, `EARMARK_INDEX_BACKEND`, `EARMARK_EMBED_MODEL`,
        `EARMARK_EMBED_MODEL_DIR`;
      - `EARMARK_TZ`, `VAULT_PATH`, `COMPOSE_PROFILES`, `ASR_GPU_MEMORY_UTILIZATION`;
      - the vLLM lines: `EARMARK_ASR_BACKEND=remote`,
        `EARMARK_ASR_BASE_URL=http://asr:8000/v1`, `EARMARK_ASR_MODEL=openai/…`,
        `EARMARK_ASR_API_KEY`, `EARMARK_ASR_MAX_UPLOAD_MB`. They would send the worker to a
        server that no longer exists.

      Then add the new settings (sections 8 and 9).
    - **Check:** `docker compose run --rm worker doctor` no longer shows `FAIL database …
      was created by earmark 0.1.0`. The `database` line reads "not created yet" until the
      first start. This `grep` prints nothing:

      ```bash
      grep -nE '^(EARMARK_(LLM|VAULT|OBSIDIAN|STORE|INDEX|EMBED|TZ)|ANTHROPIC|VAULT_PATH|COMPOSE_PROFILES|ASR_GPU)' .env
      ```
15. **`## 15. Backups and upgrades`.**
    - **Back up `.env`** (secrets) somewhere safe.
    - earmark holds no system of record: the notes live with the PKAs. The data volume holds
      the job history and a transcript cache, which can be recreated by transcribing again.
    - **Upgrades:** `git pull`, `docker compose build` (the GPU image rebuilds CTranslate2,
      so it takes long), `docker compose up -d`. Migrations run on start. After a rebuild,
      repeat smoke steps 2 and 4 (section 7.3).
    - Drop `entities rebuild` and `EARMARK_TZ`.
16. **`## 16. Troubleshooting`.**
    - Drop the vault, LLM-key and `/search` rows.
    - Add rows:

      | Symptom | Cause |
      |---|---|
      | 422 `to is not an allowed recipient` | Section 9 |
      | 422 `no recipients are allowed yet` | Section 9 |
      | doctor `FAIL mail` naming `EARMARK_SMTP_*` | A missing setting |
      | job failed, naming `EARMARK_SMTP_USERNAME` and `EARMARK_SMTP_PASSWORD` | Login refused: check the app password |
      | job failed, naming `EARMARK_SMTP_HOST` | Certificate not verified |
      | job failed: the server does not offer STARTTLS | Use `EARMARK_SMTP_SECURITY=tls` if the provider uses port 465 |
      | job failed, naming `EARMARK_MAX_ATTACHMENT_MB` | The attachments are over the cap |
      | mail arrives, but the PKA does not file it | The sender is not allowlisted there, or DMARC fails (8.3 check) |
      | a PKA got the same message twice | At-least-once; the PKA's dedupe drops it |
      | doctor or startup: `was created by earmark 0.1.0` | Section 14 |
      | the worker exits "sees no CUDA device" | Section 7.4 |

    - Keep the reachability rows and the HF row.
17. **`## 17. Hugging Face telemetry and logs`.** Keep it.
    - Drop `earmark-digest` from the `systemctl edit` line.
    - Replace the `earmark reindex` example with `earmark transcribe`, which downloads the
      Whisper model.
    - Keep every needle the telemetry test reads.

### 4.3 `docs/shortcut.md`

1. **Intro.**
   - A Shortcut named "Earmark" in the share sheet of Apple Podcasts, the Files app and
     other apps that share audio files.
   - It asks where the transcript goes (Personal or Work) and for instructions, then posts
     to `POST /transcriptions`.
   - The PKA receives the email, and ntfy pushes "Sent".
   - Keep the "Not verified by the earmark build" paragraph, updated to name what is unknown
     (2.3).
2. **`## 1. Before you start`.**
   - The API reachable over HTTPS (deploy section 6), with the Safari `/healthz` check.
   - The token.
   - The two destination addresses, both on the server's allowlist (deploy section 9):
     `pka-personal@example.net` and `pka-work@example.net`.
   - The Shortcuts app.
3. **`## 2. Build the Shortcut`.**
   - Create it, name it, and turn on **Show in Share Sheet**.
   - Then the actions between `<!-- shortcut-actions:begin -->` and
     `<!-- shortcut-actions:end -->`, as a numbered list.
   - **List format.** Each top-level item starts at column 0 as `N. **Action name**:`.
     Anything nested is a `-` bullet indented three spaces, never a nested numbered list,
     because the test splits on `^\d+\. `.
   - The items, in this order:
     1. **Receive input from Share Sheet**: the row at the top. Accept **URLs**, **Text**
        and **Files**. If there is no input: Stop and Respond.
     2. **Choose from Menu**: prompt "Send the transcript to", options **Personal** and
        **Work**.
        - Under Personal: a **Text** action containing `pka-personal@example.net`, then
          **Set Variable**, named `Recipient`.
        - Under Work: the same with `pka-work@example.net`.
        - Say why it is a variable: the menu's own output is not documented, while a
          variable is unambiguous.
        - To add a destination, add an option here and the address to
          `EARMARK_ALLOWED_RECIPIENTS`.
     3. **Ask for Input**: type Text, with the prompt "Instructions for this transcript
        (optional)". Its answer is Provided Input.
        - Leaving it blank sends no instructions, which means "file it, no analysis".
        - Whether Shortcuts accepts an empty answer is unverified. If it refuses one, set the
          Default Answer to a single space: earmark strips it.
     4. **Format Date**: Date = **Current Date**, Date Format **ISO 8601**, with **Include
        ISO 8601 Time** on. The result, Formatted Date, looks like
        `2026-11-23T21:30:00-08:00`: the date, the time and your UTC offset.
        - That offset dates the note and its file name in your time zone. Without
          `timestamp`, every date is UTC.
     5. **Get Type**: of Shortcut Input. For a shared file it is expected to give `File`
        (unverified; section 6 shows how to check).
     6. **If**: Type **is** `File`.
     7. **Get Contents of URL**: URL `https://earmark.example.com/transcriptions`. Under
        Show More:
        - Method **POST**;
        - Headers: `Authorization` = `Bearer <your token>`;
        - Request Body **Form**, with these fields:
          - `file` (type File) = Shortcut Input;
          - `to` (Text) = Recipient;
          - `instructions` (Text) = Provided Input;
          - `timestamp` (Text) = Formatted Date.

        This posts the file as a multipart upload. Then the sentence
        "`Authorization` is the only header to add": Shortcuts sets the `Content-Type`
        itself. No header row may be left empty.
     8. **Otherwise**: added with the If. Actions 9–11 go here.
     9. **Get URLs from Input**: the input is Shortcut Input.
     10. **Get Item from List**: First Item, from URLs. A share can hold more than one URL,
         and the API takes one.
     11. **Get Contents of URL**: the same URL, Method and header as action 7, with Request
         Body **JSON**, and Text fields:
         - `url` = Item from List;
         - `to` = Recipient;
         - `instructions` = Provided Input;
         - `timestamp` = Formatted Date.

         Repeat "`Authorization` is the only header to add" and the `Content-Type` remark.
     12. **End If**: added with the If.
     13. **Show Result**: If Result. This shows the API's JSON (`job_id`, or `detail` on an
         error). If "If Result" is not offered, put a Show Result after action 7 and
         another after action 11 instead.
   - After the list:
     - Variations: Show Notification instead of Show Result; an extra Ask for Input for a
       `title` on uploads (podcasts ignore `title`).
     - Keep the token warning.
     - The Tailscale URL alternative.
4. **`## 3. The requests`.**
   - **For a link**, the marker `<!-- request-example -->` followed by a `json` fence with
     exactly the keys `url`, `to`, `instructions` and `timestamp`:

     ```json
     {
       "url": "https://podcasts.apple.com/us/podcast/the-daily/id1200361736?i=1000802907981",
       "to": "pka-personal@example.net",
       "instructions": "Which studies are cited, and who ran them?",
       "timestamp": "2026-11-23T21:30:00-08:00"
     }
     ```

   - **The response**, the marker `<!-- response-example -->` and the existing json
     line. Keep the `created` paragraph, adding that a repeat within
     `EARMARK_DEDUPE_WINDOW_S` (10 minutes) of a sent job also returns it.
   - **For a file**, a table between `<!-- upload-fields:begin -->` and
     `<!-- upload-fields:end -->`:

     | Field | Type in the Form | Value in the Shortcut | Example |
     |---|---|---|---|
     | `file` | File | Shortcut Input | `Voice memo.m4a` |
     | `to` | Text | Recipient | `pka-work@example.net` |
     | `instructions` | Text | Provided Input | `File this under ideas.` |
     | `timestamp` | Text | Formatted Date | `2026-11-23T21:30:00-08:00` |

     Then a sentence: the API checks the file's name extension or its content type, and
     an `.m4a` name is enough. The cap is `EARMARK_MAX_UPLOAD_MB` (default 1024 MiB).
   - **Laptop tests.** The JSON `curl` with `-d @body.json`, and the upload:

     ```bash
     curl -s https://earmark.example.com/transcriptions \
       -H "Authorization: Bearer $EARMARK_API_TOKEN" \
       -F 'file=@Voice memo.m4a' \
       --form-string to=pka-work@example.net \
       --form-string 'instructions=File this under ideas.' \
       --form-string timestamp="$(date -Iseconds)"
     ```

     Note: `--form-string` for text values, so a leading `@` or `<` is never read as a file.
5. **`## 4. Errors the Shortcut can show`.**
   - The table: `| Status | Message | Meaning |`, with the message in backticks.
   - `<placeholder>` is allowed inside a message, for example `<limit>`.
   - The rows are exactly those listed in 6.3, `test_shortcut_error_table_matches_api`,
     and in that order.
   - Then keep "Problems on the way to the API", adding: "**A 415 with a valid file**: the
     Form was not sent as `multipart/form-data`. Report it (section 6)."
6. **`## 5. After you share`.**
   - The job runs in the background.
   - The email reaches the chosen PKA, and the push says `Sent: <title>` with the masked
     address.
   - `GET /jobs/{id}` shows `delivery_state` `sent` and `sent_at`.
   - **A new question**: share the same item again with new instructions. That is a new job
     with a new job id and the same transcript, which is not transcribed again. The PKA
     keeps one note and runs the new instructions.
   - A direct audio URL is downloaded again each time, but not transcribed again.
   - Remove the `reanalyze` and `/episodes` paragraph.
7. **`## 6. What the share sheet sends (unverified)`.**
   - Keep the URL forms and the "Not accepted" paragraph, and the capture and fixture
     procedure unchanged. The fixture is for links only.
   - Add **"Capturing a file share"**:
     - temporarily add **Show Result** (Type) after action 5;
     - share a voice memo and a file from Files;
     - note what it shows. If it is not `File`, change action 6 to match and send it back
       (runbook "Send back").

     There is no fixture for files.

### 4.4 `docs/runbook-spark.md`

**Format.**

- Phases are `## Phase N: Title` for N = 0…10, in order, then `## Send back`.
- Each phase ends with a paragraph starting `**Check:**`, exactly once in the phase, after
  the phase's commands and with no `###` heading after it.
- Checks may contain commands and bullets.
- Link to deploy sections with anchors that exist; the anchor test checks them.
- At the top:
  - a short intro: the order of the choices, and stop at the first failed check;
  - "What has never run": none of this has run on real hardware;
  - "Your values", the placeholder table at the top of section 4, plus `/path/to/earmark`,
    with an empty "Yours" column.

The phases:

0. **`## Phase 0: Before you start`.** Checkboxes:
   - shell with sudo;
   - `nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader` prints
     the GB10 and `12.1`;
   - the NVIDIA Container Toolkit (`nvidia-ctk --version`, and `docker info` lists the
     `nvidia` runtime);
   - `docker compose version` 2.30 or later;
   - disk (`df -h /var/lib/docker`): about 30 GB free, since the build footprint is
     unmeasured;
   - a domain you control with DNS access, and a mail provider with SMTP submission and app
     passwords;
   - the two PKA addresses, and a channel to ask each PKA to allowlist a sender;
   - the Cloudflare zone and a WireGuard server;
   - the ntfy app.

   **Check:** each command prints what is listed. Say these ticks replace any earlier
   phase-0 list.
1. **`## Phase 1: Upgrading from v0.1.0`.** "Skip on a new install." Its body is between
   `<!-- upgrade-v0.1.0:begin -->` and `<!-- upgrade-v0.1.0:end -->`. It summarises and
   links deploy section 14 (`deploy.md#14-upgrading-from-v010`). The commands may be
   repeated or linked. It must name:
   - moving the old database aside with its `-wal` and `-shm` files;
   - deleting `analyses/`, the index (`lancedb/`) and the embedding model
     (`models/embed/`);
   - "the vault is no longer touched";
   - the `.env` cleanup.

   **Check:** as deploy section 14.
2. **`## Phase 2: Get the code and write .env`.**
   - `git clone <this repository's URL> earmark && cd earmark`, `git checkout main`,
     `cp .env.example .env`, `chmod 600 .env`, and the token command.
   - The first-run `.env` lines:

     ```bash
     EARMARK_API_TOKEN=<the token printed above>
     EARMARK_ALLOWED_RECIPIENTS=you@example.org
     EARMARK_MAIL_BACKEND=fake
     ```

     Say what each is for:
     - `you@example.org` is your own inbox, for now;
     - `fake` writes messages to the outbox until phase 5;
     - notifications stay off while `EARMARK_NTFY_URL` is unset;
     - the worker defaults to `large-v3` on `cuda` with `float16`.
   - The file rules: no `$`, single quotes for spaces, never the Cloudflare token, and
     `API_BIND` and `EARMARK_ALLOW_PRIVATE_FETCH` left at their defaults.

   **Check:** `ls -l .env` shows `-rw-------`, and `docker compose config --quiet` prints
   nothing.
3. **`## Phase 3: Build the images and run the Spark smoke test`.**
   - `docker compose build api`.
   - Then run deploy section 7.3 (`deploy.md#73-the-spark-smoke-test`) steps 0–4 as
     written. Say it is "the only real check of the GPU path". On failure, use 7.4.
   - Then `docker compose run --rm worker doctor` and `docker compose run --rm worker
     doctor --online`.

   **Check:**
   - step 2 prints `4.9.0`, a path under `/app/.venv/`, `1` and a list with `float16`;
   - `no uv`;
   - doctor shows `asr cuda ok "1 CUDA device, float16 supported"`, `recipients ok 1
     allowed`, and `mail warn` (fake). It exits 0;
   - step 4 logs `loaded Whisper model 'large-v3' in … s on cuda (float16)` and prints the
     JFK text;
   - `doctor --online` shows the Apple lookup ok.

   Write down the build time, `docker image ls earmark:gpu`, and the "x real time" figure
   with and without `-e EARMARK_ASR_BATCH_SIZE=8`.
4. **`## Phase 4: Start, then transcribe one episode and one upload`.**
   - `docker compose up -d`, `docker compose ps`, `curl -s http://127.0.0.1:8787/healthz`.
   - `TOKEN=$(grep '^EARMARK_API_TOKEN=' .env | cut -d= -f2-)`.
   - A `body.json` written with a heredoc:
     - the 13-minute Up First episode from the old runbook,
       `https://podcasts.apple.com/us/podcast/id1222114325?i=1000803102998`;
     - `"to": "you@example.org"`;
     - instructions;
     - `"timestamp": "$(date -Iseconds)"`, using an unquoted heredoc delimiter so it
       expands.
   - The JSON `curl`.
   - The multipart upload of phase 3's `jfk.flac`, with `--form-string` fields.
   - `docker compose logs -f worker` until two "done" lines.
   - `curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/jobs`.
   - `docker compose cp api:/data/outbox ./outbox`, then the README reader over
     `./outbox/*.eml` with `python3`.

   **Check:**
   - both jobs are `done`, with `delivery_state` `sent` (the fake's);
   - the worker log shows the transcription on `cuda` and how many times real time it ran;
   - each message has the two attachments, and `transcript_sha256 ok`;
   - the podcast note has `origin: "apple_podcasts"`, a `feed:` or `apple:` guid, and real
     text in `[hh:mm:ss]` paragraphs;
   - the upload note has `origin: "upload"`, a `sha256:` guid and the JFK text;
   - `shared_at` carries the Spark's offset.
5. **`## Phase 5: Email: the sending mailbox and a real delivery`.**
   - Deploy sections 8.2–8.4: the mailbox, the DNS records and the app password.
   - In `.env`, replace the `EARMARK_MAIL_BACKEND=fake` line with the 8.4 lines.
   - `docker compose up -d`, then `docker compose run --rm worker doctor --online`.
   - Then the real delivery: re-send phase 4's `body.json` with new `instructions` (edit
     the file). The transcript is cached, so the job is quick, and new instructions mean a
     new job (dedupe).

   **Check.** It must contain each of these strings: `you@example.org`, `.instructions.md`,
   `both attachments`, `body`, `Authentication-Results` and `dmarc=pass`.
   - doctor shows `mail ok` and `smtp ok … login accepted, nothing sent`;
   - within a minute a message reaches `you@example.org` with the subject `[earmark] <the
     episode title>`;
   - its body names both attachments (`….md` as the transcript note, and
     `….instructions.md`) and the job id;
   - both attachments open, on the phone and on the Mac. The note shows the transcript, and
     the instructions file shows your new instructions;
   - `GET /jobs/<job id>` shows `delivery_state` `sent` with `sent_at`;
   - the original message's `Authentication-Results` header shows `dkim=pass` with
     `header.d=example.org`, and `dmarc=pass`. If DMARC fails, fix it before phase 6: the
     PKAs will ignore this sender.
6. **`## Phase 6: The PKA recipients`.**
   - Ask each PKA to allowlist `earmark@example.org` (deploy 8.5).
   - Set `EARMARK_ALLOWED_RECIPIENTS=pka-personal@example.net,pka-work@example.net` (keep
     `you@example.org` in the list if you want test mail).
   - Recreate the containers. Explain `all` in one paragraph, and link deploy section 9.
   - Send one transcription to each PKA, with instructions such as "File this; no analysis".
   - Note that the mail contract is DRAFT until you approve the PKA's receiving procedure.

   **Check:**
   - doctor shows `recipients ok 2 allowed: p…@example.net, p…@example.net` (or 3);
   - a request to `stranger@example.com` gets 422 `to is not an allowed recipient
     (EARMARK_ALLOWED_RECIPIENTS)`;
   - each PKA confirms it read both attachments and filed the note.
7. **`## Phase 7: Reaching the API from the phone and the Mac`.** Carry the old phase 5
   over: DNS record, token, Caddy, config and WireGuard, with the same anchors into
   deploy 6.1.

   **Check:** the old checks, plus an upload through the proxy from the Mac: the phase-4
   upload `curl` against `https://earmark.example.com/transcriptions` answers 202. Point to
   deploy 6.3.
8. **`## Phase 8: Notifications (ntfy)`.** Carry the old phase 6 over.

   **Check:**
   - `doctor --online` shows ntfy ok;
   - a re-share brings a `Sent: <title>` push with `To: p…@example.net` and the duration;
   - a request for `https://example.com/not-audio.mp3` (to an allowed address) fails, and a
     `Failed:` push names the host and the reason. Nothing is mailed.
9. **`## Phase 9: The Shortcut`.**
   - Build it from shortcut.md section 2.
   - First capture the share sheet (shortcut.md section 6), for a podcast link and for a
     file.
   - Share an episode and choose Personal, then share a voice memo or an audio file from
     Files and choose Work.

   **Check:**
   - the Shortcut shows a `job_id` each time;
   - the pushes arrive;
   - each PKA gets its message;
   - the note's `shared_at` ends in your phone's offset, such as `-08:00`, not `+00:00`:
     this proves `timestamp` is sent;
   - a second share of the same episode with new instructions makes a new job, and the
     worker log shows no new transcription;
   - an identical second share within 10 minutes answers `"created": false`.
10. **`## Phase 10: Backups, upgrades and stopping`.**
    - Deploy sections 12 and 15.
    - Keep `.env` in a password manager.
    - Upgrades: `git pull && docker compose build && docker compose up -d`, then smoke steps
      2 and 4.
    - `docker compose stop` releases a running job.

    **Check:** after `docker compose stop` and `docker compose up -d`, `docker compose ps`
    shows `api` healthy, and `GET /jobs` still lists the earlier jobs.

**`## Send back`**, a numbered list:

1. the share-sheet captures, for links (a fixture entry) and for files (what `Get Type`
   showed, and the file's name);
2. the phase 3 figures: build time, image size, and the times real time with and without
   batching;
3. the `Authentication-Results` line from phase 5, with addresses removed;
4. anything that differed from this runbook, with `docker compose logs worker --since 1h`
   and the doctor output.

Drop the LLM-environment paragraph. Do not mention vLLM anywhere in the runbook.

### 4.5 `docs/asr.md`

- **Lines 7–8.** Replace them with: "On the DGX Spark the worker transcribes on the GPU
  itself, from the GPU image (`Dockerfile.gpu`, [docs/deploy.md](deploy.md) section 7). A
  remote server is for a GPU on another machine, or for another engine."
- **Section 3's title** becomes `## 3. vLLM with Whisper`, without "(recommended)". Its first
  sentence says "On a GPU host, for example another Spark, …". Nothing else changes. vLLM is
  still a valid remote server.

### 4.6 `docs/decisions/2026-11-20-gpu-and-reachability.md`

After the title, insert one paragraph: "**Decision 1 superseded on 2026-11-23** by step 26:
the worker now runs a GPU image with CTranslate2 built for `sm_121`
(`docs/decisions/2026-11-23-gpu-build-recipe.md`, `docs/steps/26-gpu-image.md`), and the
vLLM `asr` service and `gpu` profile are gone. Decision 2 stands." Leave the rest of the
record as history.

## 5. Interfaces: test helpers

### 5.1 `tests/doc_helpers.py` additions

```python
def table_rows(text: str) -> list[list[str]]:
    """Cells of every Markdown table row outside fences, stripped, header and separator
    rows excluded (a separator row is all '-', ':' and spaces; the header is the row
    directly above it)."""


def heading_anchors(text: str) -> set[str]:
    """GitHub anchors of the ATX headings outside fences: text lower-cased, every character
    that is not a word character, '-' or ' ' removed, spaces replaced by '-'; a repeated
    anchor gets '-1', '-2', ... in order."""


def anchored_links(text: str) -> list[tuple[str, str]]:
    """(path, anchor) for inline links outside fences that carry a '#anchor' and no scheme;
    path is '' for a same-document link."""


def without_blocks(text: str, name: str) -> str:
    """text with every '<!-- name:begin -->' … '<!-- name:end -->' span removed (markers
    included); AssertionError if they are unbalanced or nested."""


def section(text: str, heading: str) -> str:
    """From the heading line that starts with `heading` (for example '## 8. ') to the next
    heading of the same or a higher level; AssertionError unless exactly one matches."""
```

Expected anchors, used in `tests/test_doc_helpers.py`:

| Heading | Anchor |
|---|---|
| `### 7.3 The Spark smoke test` | `73-the-spark-smoke-test` |
| `## 14. Upgrading from v0.1.0` | `14-upgrading-from-v010` |
| `## Phase 2: Get the code and write .env` | `phase-2-get-the-code-and-write-env` |
| `#### 6.1.6 DNS rebinding protection (a gotcha)` | `616-dns-rebinding-protection-a-gotcha` |

`anchored_links("[a](x.md#b) [c](#d) [e](https://h/#f)")` is `[("x.md", "b"), ("", "d")]`.
Fences are ignored by every helper.

## 6. Tests

All tests are in `tests/test_docs.py` unless named otherwise. Fixtures are the documents
themselves, `tests/multipart_helpers.py` (`multipart_body`, `Part`, `content_type`,
`audio`), and the app built by `create_app`. Nothing is downloaded, and no socket is used:
`TestClient` runs over the ASGI interface.

Shared setup:

- `DOCS = ["README.md", "docs/deploy.md", "docs/shortcut.md", "docs/runbook-spark.md",
  "docs/asr.md"]`.
- `ALLOWLIST` is parsed from deploy's `allowlist-example` fence. Tests that post Shortcut
  examples use `allowed_recipients` = the `ALLOWLIST` addresses plus `you@example.org`.
- **`_post`** is generalised to
  `_post(body: bytes, tmp_path, content_type="application/json", *, settings_overrides=None,
  headers=AUTH)`. It returns status, JSON and store.

### 6.1 Brief criterion: "The doc-driven tests pass, updated for the new route, fields and settings. The README settings table covers every `Settings` field."

| Test | Proves |
|---|---|
| `test_readme_settings_reference_covers_every_field` (kept) | every field, once |
| `test_readme_settings_reference_marks_secrets` (kept) | secrets marked |
| `test_readme_commands_cover_cli`, `test_readme_http_api_lists_every_route` (kept) | commands and routes |
| `test_quickstart_block_present`, `test_quickstart_uses_fake_backends`, `test_quickstart_runs_no_network_command`, `test_quickstart_exports_allowed_recipients` (kept) | as today |
| `test_quickstart_exports_only_settings` (new) | every `EARMARK_[A-Z0-9_]+=` assignment in the script names a `Settings` field. This fails on today's `EARMARK_LLM_PROVIDER` |
| `test_quickstart_commands_exist` (new) | every `uv run earmark <word>` uses a registered command or `--version` |
| `test_quickstart_request_body_valid` (updated) | the `-d '…'` body validates, its keys are `{url, to, instructions, timestamp}`, and `timestamp.utcoffset()` is not `None` |
| `test_quickstart_upload_valid` (new) | from the second `curl`, the `--form-string` pairs (via `shlex.split` after joining `\`-continued lines) validate as `UploadFields`, `to` is allowed by the exported allowlist, and the `-F file=@…` path's name gives a non-`None` `sources.upload_extension(name, None)` |
| `test_quickstart_reads_outbox` (new) | the script has `EARMARK_MAIL_BACKEND=fake`, `outbox` and `.eml`, and runs `worker --once` once per POST |

### 6.2 Brief criterion: "The Shortcut example body validates against the new request model."

| Test | Proves |
|---|---|
| `test_shortcut_request_example_validates` (updated) | keys `{url, to, instructions, timestamp}`; `TranscriptionRequest` validates; `parse_apple_url` accepts the URL; `timestamp` matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$` (Apple's documented ISO 8601 form) |
| `test_shortcut_request_example_accepted_by_api` (updated) | 202; the job's `url` is canonical, `to` equals the example's, `instructions` equals it, and `utc_offset_min == -480` |
| `test_shortcut_response_example_matches_api` (markers renamed) | as today |
| `test_shortcut_upload_fields_match_api` (new) | the `upload-fields` table's first cells are exactly `file, to, instructions, timestamp`, in order, each in `("file", *models.FORM_FIELDS)`; the text examples validate as `UploadFields` |
| `test_shortcut_upload_example_accepted_by_api[file_first, file_last]` (new) | `multipart_body` with `Part("file", audio(), filename=<example>)` (no content type) and the text parts, in both orders, gives 202, and the job has `source == "upload"`, `upload_name == "Voice memo.m4a"`, the example's `to` and `instructions`, `utc_offset_min == -480`; `data/uploads/` holds exactly one file |
| `test_shortcut_examples_match_actions` (new) | the backticked field names in action 11 (JSON) equal the JSON example's keys; those in action 7 (Form) equal the table's names |

### 6.3 Brief criteria on the Shortcut's build: menu, Ask for Input, `POST /transcriptions`; carry-over: `timestamp`

- `ACTIONS = ["Receive input from Share Sheet", "Choose from Menu", "Ask for Input",
  "Format Date", "Get Type", "If", "Get Contents of URL", "Otherwise", "Get URLs from
  Input", "Get Item from List", "Get Contents of URL", "End If", "Show Result"]`.
- `_action_items()` stays: it splits on `^\d+\. `.

| Test | Proves |
|---|---|
| `test_shortcut_actions_in_order` (updated) | the bold names equal `ACTIONS` |
| `test_shortcut_menu_fills_to` (new) | item 2 has `Personal`, `Work`, `Set Variable` and `` `Recipient` ``; its two backticked addresses are allowed by `ALLOWLIST`; the JSON example's and the upload table's `to` are each one of them |
| `test_shortcut_ask_for_input_optional` (new) | item 3 has "optional" and "Provided Input" |
| `test_shortcut_formats_timestamp` (new) | item 4 has `Current Date`, `ISO 8601` and `Include ISO 8601 Time` |
| `test_shortcut_branches_on_type` (new) | item 5 has "Shortcut Input"; item 6 has `` `File` `` |
| `test_shortcut_post_actions_send_fields` (replaces `test_shortcut_post_action_details`) | items 7 and 11 both have `/transcriptions`, `POST`; item 7 has `Form`, `` `file` ``, `` `to` ``, `` `instructions` ``, `` `timestamp` ``; item 11 has `JSON`, `` `url` ``, `` `to` ``, `` `instructions` ``, `` `timestamp` ``; both name `Recipient`, `Provided Input` and `Formatted Date`; no item mentions `/ingest` |
| `test_shortcut_sends_only_bearer_header` (updated) | items 7 and 11 each contain "`Authorization` is the only header to add" and `Content-Type`; item 7 contains `Bearer` |

**`test_shortcut_error_table_matches_api`** (new). The error table's `(status, message)`
rows equal this list, in order. Each is triggered against the app, except 503, and the API's
`detail` must match the row. Placeholders `<…>` become `.+` in a regex; everything else
matches literally.

| # | Status | Message (as documented) | Trigger |
|---|---|---|---|
| 1 | 401 | `missing or invalid API token` | no `Authorization` header |
| 2 | 413 | `request body is larger than 64 KiB` | a JSON body of 64 KiB + 1 |
| 3 | 413 | `the upload is larger than the <limit> limit (EARMARK_MAX_UPLOAD_MB)` | `create_app(…, max_upload_bytes=1024)` and a 2 KiB file |
| 4 | 415 | `send JSON (Content-Type: application/json) or a multipart form (Content-Type: multipart/form-data)` | `Content-Type: application/x-www-form-urlencoded` |
| 5 | 415 | `the file does not look like audio: <…>` | file part `notes.txt`, `text/plain` |
| 6 | 422 | `That is not a valid link. Share an episode from Apple Podcasts.` | `url: "hello"` |
| 7 | 422 | `This links to a whole show. Share a single episode instead.` | `https://podcasts.apple.com/us/podcast/id1200361736` |
| 8 | 422 | `This Apple link is not a podcast episode.` | `https://www.apple.com/podcasts/` |
| 9 | 422 | `to must be one email address` | `to: "not an address"` |
| 10 | 422 | `to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)` | `to: "stranger@example.com"` |
| 11 | 422 | `no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server` | settings with `allowed_recipients=()` |
| 12 | 422 | `unknown field 'x' (allowed: url, to, instructions, title, timestamp)` | JSON with `"x": 1` |
| 13 | 422 | `request body is not valid JSON` | body `{` |
| 14 | 422 | `the form must have exactly one file field named 'file'` | multipart with only `to` |
| 15 | 503 | `the job store is unavailable; try again shortly` | not triggered: compared with `api.app.STORE_UNAVAILABLE_DETAIL` |

Every trigger was run against `create_app` in the sandbox (2.1) and gave the message shown.
Row 3 then reads `… the 1024 bytes limit …`, which the regex covers. Rows 6–8 use an allowed
`to`. Row 8 goes through `sources.parse_source_url`, as the API does. `parse_apple_url` alone
would answer "Only Apple Podcasts episode links are supported.", which the API can no longer
return, so that row must not come back. The implementer may reword the "Meaning" cells freely.

### 6.4 Brief criterion: "The runbook's phases end in checks. One check sends a real transcription to an address the operator controls, and confirms that both attachments open and the body names them."

| Test | Proves |
|---|---|
| `test_runbook_phases_end_in_checks` (new) | the runbook has `## Phase 0:` … `## Phase 10:` in order, then `## Send back`; each phase contains `**Check:**` exactly once, not as its first paragraph, and has no `### ` heading after it |
| `test_runbook_real_delivery_check` (new) | some phase's text after `**Check:**` contains `you@example.org`, `.instructions.md`, `both attachments`, `body`, `Authentication-Results` and `dmarc=pass`, and that phase sets `EARMARK_MAIL_BACKEND=smtp` or links `deploy.md#84-…` |
| `test_runbook_covers_the_brief` (new) | the runbook has `deploy.md#73-the-spark-smoke-test`, `Dockerfile.gpu` or "GPU image", `SPF`, `DKIM`, `DMARC`, `app password`, `EARMARK_ALLOWED_RECIPIENTS`, `` `all` ``, `-wal`, `-shm`, `analyses/`, `lancedb/`, `models/embed/` and "vault is no longer touched" |
| `test_deploy_doc_mail_section` (new) | `section(deploy, "## 8. ")` names every `EARMARK_SMTP_*` field and `EARMARK_MAX_ATTACHMENT_MB`, `SPF`, `DKIM`, `DMARC`, `_dmarc.example.org`, `_domainkey`, `app password`, `Authentication-Results`, `allowlist`, `DRAFT` and `docker compose cp` |
| `test_deploy_doc_recipients_section` (new) | section 9 has the `allowlist-example` fence with one `EARMARK_ALLOWED_RECIPIENTS=` line that `Settings` parses into a policy with two addresses; the section mentions `all`, "API token", "WARNING" and the unset message |
| `test_deploy_doc_upgrade_section` (new) | section 14 has the `upgrade-v0.1.0` markers, `earmark.db-wal`, `earmark.db-shm`, `analyses`, `lancedb`, `models/embed`, `digests`, `--remove-orphans`, "vault is no longer touched" and `VAULT_PATH` |
| `test_deploy_doc_cpu_worker_override` (new) | the `yaml` fence after `<!-- cpu-worker-override -->` loads with a `yaml.SafeLoader` subclass with constructors for `!reset` and `!override` (returning sentinels). `services.worker` has `gpus` = the reset sentinel, `image == "earmark:local"`, `build.dockerfile == "Dockerfile"`, and `environment.EARMARK_ASR_DEVICE == "cpu"`. Every `EARMARK_*` key is a `Settings` field |
| `test_deploy_doc_reachability` (updated) | as today, plus `/transcriptions` appears in section 6, `/ingest` does not, and section 6.3 exists with `EARMARK_MAX_UPLOAD_MB` |

### 6.5 Brief criterion: "Nothing in the docs describes removed behaviour"; carry-overs: placeholders, deploy section 4

| Test | Proves |
|---|---|
| `test_docs_describe_no_removed_behaviour[doc]` (new, over `DOCS`) | after `without_blocks(text, "upgrade-v0.1.0")`, no case-insensitive match of the patterns in the list below |
| `test_docs_use_placeholders_only[doc]` (new, over `DOCS` and this step plan) | see the rules below |
| `test_deploy_doc_section_4_complete` (new) | section 4 has no line ending in "It runs as a non-root user" without the rest of the sentence; concretely, it contains "Both images run as the non-root user `earmark`", and has no `VAULT_PATH`, `/vault`, `EARMARK_LLM` or `EARMARK_TZ` |
| `test_doc_relative_links_resolve[doc]` (`DOCS` extended) | as today, for five docs |
| `test_doc_anchors_resolve[doc]` (new) | every `anchored_links` pair resolves: `heading_anchors` of the target (or of the same doc) contains the anchor |
| `test_no_cloudflare_tunnel_or_access_headers` (`DOCS` extended) | as today |
| `test_deploy_doc_states_test_status`, `test_deploy_doc_gpu_smoke`, `test_deploy_doc_has_no_gpu_profile`, `test_deploy_doc_hf_telemetry_section` (kept) | as today |
| the share-sheet tests (kept) | as today |

**The patterns for `test_docs_describe_no_removed_behaviour`:**

- `\bvault\b`, `obsidian`, `/ingest\b`, `reanalyze`, `\bdigest`, `\bentit(y|ies)\b`,
  `lancedb`, `fastembed`, `embedding`;
- `EARMARK_(LLM|VAULT|OBSIDIAN|STORE|INDEX|EMBED|TZ)`, `anthropic`, `\bclaude\b`;
- `asr:8000`, `COMPOSE_PROFILES`, `profile gpu`, `gpu profile`;
- `GET /episodes`, `/search\b`, `earmark (episodes|search|reindex|analy[sz]e)`;
- `--prompt`, `"prompt"`, `Note ready`, `--extra index`;
- `\bvllm\b` for every doc except `docs/asr.md`.

The word "analysis" is allowed: the PKAs do it, and the body says "no analysis".

**The rules for `test_docs_use_placeholders_only`:**

- Every email address matching `[\w.+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b`
  has a domain in `{example.org, example.net, example.com}`.
- Every IPv4 literal matching `\b(\d{1,3}\.){3}\d{1,3}\b` is in
  `{127.0.0.1, 0.0.0.0, 1.1.1.1, 192.168.1.10, 192.168.1.0}`.
- Every `*.ts.net` host ends in `tailnet-name.ts.net`.
- No `tk_[A-Za-z0-9]{8,}`.
- In fences, a line `(export )?EARMARK_(API_TOKEN|SMTP_PASSWORD|NTFY_TOKEN|ASR_API_KEY)=…`
  has a value that is empty, contains `<`, or is `demo-token`.

### 6.6 Criteria this plan adds

| Criterion | Test |
|---|---|
| The doc helpers behave as documented | `tests/test_doc_helpers.py`: `test_table_rows`, `test_heading_anchors` (the table in 5.1, duplicates, headings in fences ignored), `test_anchored_links`, `test_without_blocks` (one block, two blocks, unbalanced raises), `test_section` (stops at the same level, includes deeper levels, unmatched raises) |
| No file outside `docs/` and `tests/` changed | verifier's `git diff` (section 7) |

Remove `test_shortcut_post_action_details`. Rename the markers `ingest-request-example` and
`ingest-response-example` to `request-example` and `response-example`, in the doc and the
tests together.

## 7. Verification

The verifier runs, from the repository:

```bash
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_docs.py tests/test_doc_helpers.py tests/test_deploy.py \
  tests/test_deploy_gpu.py tests/test_config.py -q
git status --porcelain -- src Dockerfile Dockerfile.gpu compose.yaml deploy .github \
  .env.example CLAUDE.md docs/PLAN.md                    # expect no output
git stash list | wc -l                                    # same count as before the step
grep -rnoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' README.md docs/deploy.md docs/shortcut.md \
  docs/runbook-spark.md docs/asr.md docs/steps/27-docs-runbook-shortcut.md \
  | grep -vE ':(127\.0\.0\.1|0\.0\.0\.0|1\.1\.1\.1|192\.168\.1\.(10|0))$'   # expect no output
grep -rniE 'vllm|asr:8000|COMPOSE_PROFILES|/ingest|obsidian|EARMARK_LLM|reanalyze|lancedb' \
  README.md docs/deploy.md docs/shortcut.md docs/runbook-spark.md
#   expect only lines inside the upgrade-v0.1.0 blocks (deploy section 14, runbook phase 1)
```

**Live smoke: the README quickstart, verbatim.**

- Copy the fenced block from `README.md` into a file in the scratchpad.
- Run it with `TMPDIR` set to the scratchpad (the sandbox's `/tmp` is small) and port 8787
  free: `TMPDIR=<scratchpad> bash quickstart.sh`.
- Expect what 2.2 lists, including:
  - two `job <id> done` lines;
  - two `.eml` files;
  - `transcript_sha256 ok` twice, for the two notes;
  - `shared_at: 2026-11-23T21:30:00-08:00` in the podcast note.

Nothing in it touches the network.

**Static review.**

- Read `docs/shortcut.md` section 2 against 4.3. No phone is available.
- Read the runbook phase by phase against 4.4, checking that each command is copy-pastable:
  quoting, the heredoc delimiter, `--form-string`.
- Read deploy section 14's shell snippet for correctness. It cannot be run here, because
  there is no Docker.
- Say plainly in the report that the following were not run:
  - the Shortcut;
  - Caddy with a large upload;
  - the compose override;
  - the Spark smoke;
  - a real SMTP delivery.

## 8. Deviations

- **D1: The runbook's phases are renumbered and reordered.** The brief asks for a rewrite.
  The GPU smoke and a fake-outbox run come before email, so each layer is proven before the
  next.
- **D2: `docs/deploy.md` keeps its numbering, and its sections 8, 9 and 14 are
  repurposed.** Sections 7 and 17 are named by files outside `docs/` (2.1). Renumbering
  would force edits to `Dockerfile.gpu`, `compose.yaml`, `.env.example` and
  `tests/test_deploy_gpu.py` for no gain.
- **D3: One Shortcut, branching on `Get Type`.** The alternative is two Shortcuts, one
  accepting links and one accepting files. The share sheet would filter them by type, and
  no type detection would be needed. But the menu, the question and the date would have to
  be built twice. The carry-over describes one Shortcut that branches. The doc gives
  `Get Type`'s value as unverified and says how to check it. If it proves unreliable,
  splitting into two is the fallback.
- **D4: `to` is set through `Set Variable` inside each menu option**, rather than through
  the menu's output, which is not documented (2.3).
- **D5: Two small edits outside the brief's four documents:** `docs/asr.md` (4.5) and the
  2026-11-20 decision record (4.6). The acceptance criterion says "nothing in the docs
  describes removed behaviour", and both still call vLLM the Spark's GPU path.
- **D6: The doc helpers and tests grow** (5.1, section 6). They add an anchor check, which
  would have caught the runbook's dangling `#7b-…` link, a placeholder scan, a
  removed-feature scan, and an error table checked against the live app. The brief asks
  only that the doc tests pass. These make them fail when a doc drifts.
- **D7: The quickstart is checked statically and run by the verifier**, not by a default
  test. Its server listens on TCP, which the default suite blocks.
- **D8: Placeholder PKA addresses are at `example.net`**, the sender at `example.org`. That
  keeps "your mail domain" and "the PKAs' addresses" apart in the text. The quickstart keeps
  `you@example.org`.
- **D9: `docs/ARCHITECTURE.md` was edited by this planner:**
  - "What `tests/test_docs.py` checks" is rewritten for section 6;
  - Known risk 4 gains the Shortcut's and the compose override's unverified points;
  - the "History" pointer names deploy section 14 and runbook phase 1;
  - "Settled questions" gains "Settled by step 27", and its intro and the "How to read" line
    say 24–27;
  - the step-25 settled bullet now says the Shortcut sends `timestamp`.

  No contract changed.
- **No dependency is added.** PyYAML, a dev dependency, already parses YAML in tests.

## 9. Out of scope

- **`serve`'s CLI help text** ("Run the ingest API") and any other wording in code.
- **`CLAUDE.md`'s "Today (after step 25)" paragraph.** It is accurate apart from the step
  number, and `CLAUDE.md` is not in the brief.
- **`docs/PLAN.md`.** Its status table is the orchestrator's.
- **Running anything on the Spark, a phone or a real SMTP server.** Those are the runbook's
  operator checks.
- **A `--timestamp` option for `earmark submit`** (backlog).
- **A send-rate cap** for `all` (backlog).
- **Testing the compose override in CI** (`container.yml`). That would change a workflow,
  which this documentation step does not.
