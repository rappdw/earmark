# Step 36: Transcription without delivery: the /v1 API

The brief is in `docs/PLAN.md`, "Step 36". It builds on step 24 (uploads, `Intake`, the
streaming multipart reader, staging, dedupe), step 25 (the runner's deliver stage), step 26
(batched decoding and the ASR profile), step 33 (the transcript by job id) and step 34 (the
CLI's client; untouched here).

The step can be built as briefed, in one implementer pass. It is large, so section 3 gives
an order of work with a clean seam: if the work has to be split, **36a** is phases 1 to 4
(delivery-free jobs, `/v1` routes, purge and the contract, with no `initial_prompt` or
`language`: the form takes `file` only) and **36b** is phases 5 and 6 (decode options and the
per-options cache, an additive change to the contract). Do not split unless the work runs out
of room; one pass is the plan.

The brief leaves several product choices to the planner. They are stated first, because each
changes what a client sees:

- **P1. The `initial_prompt` cap is 1000 characters** (Unicode code points, after stripping
  surrounding whitespace). Whisper conditions on at most the **last 223 tokens** of a prompt
  (checked, section 2.1). 1000 characters is about 223 tokens of common English names or plain
  prose, and more than the window for technical terms (about 630 characters fill it) or for
  CJK (about 140). A character cap cannot match a token window exactly; this one is never
  smaller than the window for names and jargon, which is what the brief asks the prompt for.
  The contract says plainly that Whisper drops the start of a prompt longer than its window,
  so a client should put its most important terms last.
- **P2. A `/v1` job without `language` is auto-detected,** whatever `EARMARK_ASR_LANGUAGE`
  says. The brief says "otherwise auto-detect", and a contract must not depend on a server
  setting. `EARMARK_ASR_LANGUAGE` keeps applying to the unversioned routes.
- **P3. `/v1` serves only delivery-free jobs.** A mail job's id under `/v1` is 404 "job not
  found", as an unknown id is. The unversioned routes still list `/v1` jobs (`GET /jobs`,
  `GET /jobs/{id}`, `earmark jobs`), as uploads with no `to`, so the operator sees the whole
  queue; the unversioned `GET /jobs/{id}/transcript` answers 409 for a `/v1` job, pointing at
  `/v1`.
- **P4. A transcript is shared by identical requests, and a purge removes it for all of
  them.** Two `/v1` jobs with the same audio bytes, `initial_prompt` and `language` share one
  cached transcript (that is how "an identical re-send reuses the cache"). `DELETE` removes
  that transcript and marks **every finished `/v1` job that shares it** as purged, clearing
  each one's copy of the prompt; a queued or running job that shares it is left alone (it will
  write its own). The contract says so.
- **P5. `/v1` dedupes like the unversioned route.** A request identical to one that is queued,
  running, or done within `EARMARK_DEDUPE_WINDOW_S` (600 s by default) returns that job, so a
  POST retried after a lost 202 does not transcribe twice. A purged job is never returned.
- **P6. Purge is deletion, not shredding.** Files are unlinked. The database runs with
  `PRAGMA secure_delete = ON` (freed space is zeroed) and checkpoints its write-ahead log after
  a purge, so the prompt does not linger in `earmark.db` or its `-wal` (checked, section 2.6).
  The contract says that filesystem blocks and backups are outside earmark's reach.
- **P7. `GET /v1/jobs/{id}` names the id `job_id`**, as the POST answer and the transcript do,
  rather than the brief's `id`. One name across a contract.
- **P8. `DELETE` answers 200 with the job's view** (now carrying `purged_at`), and repeating it
  answers the same. A lost DELETE answer can be retried, as the brief wants a lost fetch to be.

The research questions have answers (section 2): both faster-whisper entry points take
`initial_prompt` and `language`; the heartbeat keeps the lease through a long transcription,
so no fix is needed; and the per-options cache is a new directory, `data/transcripts/<key>/`.

Rules for the implementer that are not about the code itself:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Use a `TMPDIR` under `~/.cache/`** for anything that writes temp files:
  `export TMPDIR=/home/sandy/.cache/earmark-step36-tmp && mkdir -p "$TMPDIR"`, and `rm -rf`
  it when done. `/tmp` is a small tmpfs. Never install anything into the repo `.venv`; a
  scratch venv goes under `TMPDIR`. Never run `pytest -m network`.
- **Run both gates:** `make check` and `GITHUB_ACTIONS=true make check`.
- **Placeholders only** in every file this step writes: hosts and addresses at `example.org`,
  `example.net` or `example.com` (`spark.example.com` for the Spark in SSH examples),
  `127.0.0.1` for loopback, `192.168.1.10` for a LAN address, `<…>` for the token. No real
  domain, LAN address or token.
- **`ruff format --check` covers Python code blocks in Markdown.** Keep every ```` ```python ````
  fence in docs ruff-formatted, or use `bash`/`text`.
- In `README.md` and `docs/deploy.md` the removed-wording scan bans, among others, `vault`,
  `\bdigest`, `embedding`, `\bclaude\b`, `--prompt` and `"prompt"`. Write `initial_prompt`
  (with the underscore), never a bare quoted `"prompt"`, and do not use the word "digest" in
  those two files ("hash" is fine).
- **`docs/ARCHITECTURE.md` is already updated** by this plan (section 8.10). Check the code
  against it, and change it only where the code had to differ, saying so in your report.
- `CLAUDE.md`, `README.md`, `docs/deploy.md` and `contract/` are yours to change (section 3).

## 1. Outcome

- **`POST /v1/transcriptions`**: multipart only, behind the bearer token; fields `file`,
  `initial_prompt`, `language`; anything else (`to` and `title` included) is a 422. It streams
  the upload exactly as `POST /transcriptions` does and answers 202 `{"job_id", "status"}`
  with `Location: /v1/jobs/{id}`.
- **A delivery-free job** (`delivery = 'none'` in the job row) runs the upload check and
  transcribe, then is `done`. It never composes or sends mail and never notifies, on success,
  failure, skip or a stale-lease failure. The allowlist does not apply. Its file name is not
  stored, and no log line carries the file name, the prompt or transcript text.
- **Decode options.** `initial_prompt` and `language` reach faster-whisper (sequential and
  batched) and the remote engine. They are part of the ASR profile, and the transcript is
  cached per (audio, options) under `data/transcripts/<decode_key>/`, so each job serves the
  transcript made with its own options and an identical request reuses it.
- **`GET /v1/jobs/{id}`**, **`GET /v1/jobs/{id}/transcript?format=json|text`** and
  **`DELETE /v1/jobs/{id}`**, with the views and states in section 4.
- **The contract,** `contract/api-v1.md`, pinned by hash, with every response's example
  checked against the app.
- **Docs:** ARCHITECTURE (done here), `CLAUDE.md`, the README, `docs/deploy.md` (a new
  section 20 on the SSH tunnel, the upload cap, 16 kHz mono, memory and purge),
  `contract/README.md`.

## 2. Findings

Checked on 2026-11-28 in this sandbox. faster-whisper was inspected in a scratch venv under
`TMPDIR` with the locked versions (`faster-whisper==1.2.1`, `ctranslate2==4.9.0`,
`av==18.1.0`, `onnxruntime==1.32.0`, `tokenizers==0.23.3`), never the repo `.venv` (which
has no `transcribe` extra). Models came from the existing caches (`tiny` under
`~/.cache/earmark-tests/whisper`, the `large-v3` tokenizer under `~/.cache/huggingface/hub`),
offline.

### 2.1 How faster-whisper uses `initial_prompt` (read in `faster_whisper/transcribe.py`)

- `WhisperModel.max_length` is hard-coded to **448** in `WhisperModel.__init__` (line 722),
  whatever the model. `get_prompt` keeps `previous_tokens[-(448 // 2 - 1):]`, the **last 223
  tokens**, after `<|startofprev|>`. Checked by calling `get_prompt` with a 3800-token prompt:
  227 tokens out (`sot_prev`, 223 prompt tokens, the SOT sequence) and the kept tokens equal
  the tail of the input.
- **Sequential** (`WhisperModel.transcribe`, `ASR_BATCH_SIZE=0`, the default): the prompt is
  encoded as `" " + initial_prompt.strip()` and seeds `all_tokens`. With
  `condition_on_previous_text=True` (the default, which earmark keeps) each window's output is
  appended and the window keeps the last 223 tokens, so **the prompt conditions the first
  window or two, then the transcript's own text pushes it out**. A temperature fallback above
  0.5 resets the prompt (`prompt_reset_since`), dropping it entirely.
- **Batched** (`BatchedInferencePipeline.transcribe`): `condition_on_previous_text=False`, and
  `generate_segment_batched` prepends `tokenizer.encode(initial_prompt)` (no leading space, no
  strip), truncated to the same 223 tokens, **to every chunk**. So the prompt helps
  throughout a 4-hour meeting only in batched mode. Turning batching on stays the operator's
  setting (brief, out of scope); `docs/deploy.md` section 20 says this.
- A prompt longer than the window is **truncated from the front, silently**. faster-whisper
  raises only when `len(prompt) + max_new_tokens > 448`, and earmark never sets
  `max_new_tokens`.

Characters that fill 223 tokens (binary search with the `tokenizer.json` of `tiny` and of
`large-v3`; the two agree except on the prose line):

| Text | Characters in 223 tokens | Tokens in 1000 characters |
|---|---|---|
| common English names ("Alice Johnson, Bob Smith, …") | 1007 | 223 |
| plain meeting prose | 902 (tiny), 1167 (large-v3) | 245 / 191 |
| technical terms ("Kubernetes, Terraform, gRPC, …") | 629 | 359 |
| unusual names and product names | 585 | 384 |
| Cyrillic | 743 | — |
| CJK | 142 | — |

Hence P1: 1000 characters.

### 2.2 Both entry points take `initial_prompt` and `language`

`inspect.signature` on both: `WhisperModel.transcribe` and
`BatchedInferencePipeline.transcribe` have `language: Optional[str] = None` and
`initial_prompt: Optional[Union[str, Iterable[int]]] = None`. Passing `initial_prompt=None` is
the default, so earmark can always pass it. A transcription of 110 s of audio with a
3-sentence prompt ran in both modes with `tiny` on CPU. In batched mode the prompt must be a
`str` (it calls `tokenizer.encode` on it); earmark only ever passes a `str`.

### 2.3 Language codes

`faster_whisper/tokenizer.py` checks `language not in _LANGUAGE_CODES` and raises
`ValueError("'xx' is not a valid language code (accepted language codes: …)")` (checked with
`language="xx"`). `_LANGUAGE_CODES` in 1.2.1 has **100 codes**, ending in `yue`:

```text
af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu ha haw
he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt
my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so sq sr su sv sw ta te tg th tk tl
tr tt uk ur uz vi yi yo zh yue
```

The API cannot import faster-whisper (an optional extra, and `serve` may run without it), so
earmark keeps its own copy as `models.WHISPER_LANGUAGES` in that order, and a `slow` test
pins it to the installed `_LANGUAGE_CODES`. `yue` exists only in `large-v3`'s vocabulary;
with an older model a `yue` job fails at transcription (an edge the contract does not need to
name).

### 2.4 The heartbeat keeps the lease through a long transcription: no fix needed

- The heartbeat is a daemon thread (`pipeline/worker.py`, `Heartbeat`) beating every
  `min(60 s, JOB_LEASE_SECONDS / 4)` = 60 s, and the lease is 900 s, so the lease is lost only
  if the main thread holds the GIL for about 14 minutes in one call.
- Measured with a thread ticking every 50 ms while the main thread worked (scratch script,
  `tiny`, CPU, int8): sequential transcription of 110 s of audio, max gap **0.13 s**; batched,
  **0.056 s**; decoding a 1-hour 16 kHz mono FLAC with PyAV, **0.053 s**; computing the
  log-mel features of that hour in one numpy call (15 s), **0.157 s**. CTranslate2, numpy's FFT
  and PyAV release the GIL.
- `requeue_stale` runs only in the worker's own loop, before each claim, so a single worker
  cannot requeue the job it is running; the heartbeat matters for a second worker or a crash.
- `tests/test_worker.py::test_heartbeat_keeps_long_stage_alive` already proves the mechanism
  with a blocking stage. This step adds the same test for a delivery-free job (section 6.6,
  `test_v1_long_transcription_keeps_its_lease`).
- Not verifiable here: the CUDA build on the Spark. The GIL release is in CTranslate2's Python
  bindings, the same for both devices; the operator's smoke covers it.

Side findings for the docs (section 20 of `docs/deploy.md`):

- A 1-hour 16 kHz mono FLAC of speech is **61 MB**, so a 4-hour file is about 250 MB, well under
  `EARMARK_MAX_UPLOAD_MB` (1024). 48 kHz stereo is about six times that (raw PCM at 16 bits is
  115 MB per hour per 16 kHz channel), and over the cap beyond about an hour and a half.
- Peak memory for that hour (decode plus sequential features) was **3.2 GB**, so a 4-hour file
  needs on the order of 12 GB on the worker; the Spark's 128 GB is ample.
- SIGTERM during a long transcription releases the job and the next run starts the file over;
  there is no partial progress. Each Python step between library calls is short, so the stop
  grace (2 min) is enough to reach the release.

### 2.5 The transcript cache and the ASR profile today

- `ArtifactStore` keeps one transcript per episode key: `episodes/<key>/transcript.json` and
  `transcript.txt`. `CachedTranscriber.cached(episode)` returns it only when its
  `asr_profile` stamp equals the engine's `profile`; a miss re-transcribes and **overwrites**
  it (`transcribe/base.py`).
- An upload's episode key is `content_key(sha256)`, shared with any upload, URL or
  `earmark transcribe` of the same bytes.
- So a per-request option cannot live in that one file: job B with another prompt would
  overwrite job A's transcript, and A would then serve B's. Hence a second tree keyed by audio
  and options (section 4.4), used only by delivery-free jobs. The mail path, its files and its
  profiles stay byte-for-byte as they are, so every cached transcript is still reused.
- The faster-whisper profile is
  `asr_profile("faster_whisper", {model, device, compute_type, language, beam_size,
  vad_filter[, batch_size]})`; remote is `{model, language}`; the fake is `{model, language}`.

### 2.6 Purged text in SQLite (scratch script, stdlib `sqlite3`)

A WAL database with a 900-character marker in one row, updated 50 times in other columns
(varying sizes), then the marker set to NULL: before a checkpoint **one copy** of the marker
was still in the files (the WAL); after `PRAGMA wal_checkpoint(TRUNCATE)` (result
`(0, 0, 0)`) none. That held with `secure_delete` off as well in this run, but SQLite leaves
freed cell bytes in place without `secure_delete` in general (a row that shrinks), so P6 turns
it on for every connection and checkpoints after a purge. A checkpoint with a concurrent reader
or writer can return busy; it is best effort and logged at DEBUG.

### 2.7 Other checks

- **Container users.** The API (CPU image) and the worker (GPU image) both run as
  `earmark:earmark` with `UID`/`GID` 1000 by default (`Dockerfile` and `Dockerfile.gpu`), and
  share the data volume, so the API can delete what the worker wrote. Directories are created
  0o755 and files 0o644 by `atomic_writer`.
- **The remote engine.** OpenAI's create-transcription reference (fetched 2026-11-28 from
  `developers.openai.com`) names the optional form fields `prompt` ("guides the model's style
  or continues a previous audio segment") and `language` (ISO-639-1). earmark already sends
  `language`; `/v1` adds `prompt`. Whether a given server honours it is the server's business.
- **Upload parsing.** `api/upload.py` takes its allowed text fields from the constant
  `FORM_FIELDS`; an unknown field is refused in `on_headers_finished` with
  `unknown field '<name>' (allowed: file, …)` before its data is read. Making the field list a
  parameter is enough for `/v1`.
- **Routes.** `tests/test_docs.py::test_readme_http_api_lists_every_route` collects routes from
  `build_router(...)`; `APIRouter.include_router` copies the included routes, with the prefix,
  into the parent's `routes`, so a `/v1` router included by `build_router` is seen.

### 2.8 Dead ends

- **Counting tokens at the API.** The tokenizer is the model's `tokenizer.json`, on the worker;
  the API cannot load it reliably. Hence a character cap.
- **Making the per-options transcript an `Episode`.** `Episode` validates
  `key == source_key(source, show_id, guid)`, and a new source would need a table rebuild
  (`CHECK (source IN …)`). The per-options key stays outside `Episode`.
- **Keying per job instead of per options.** It would make "an identical re-send reuses the
  cache" depend on dedupe's 10-minute window.
- **Starlette's form parser** for `/v1`: it spools files to the system temp dir (step 24).

## 3. Files

Order of work: phase 1 → 6. Run `make check` after each phase; the doc tests fail until
phase 4, because this plan already changed `docs/ARCHITECTURE.md`.

| Phase | File | Change |
|---|---|---|
| 1 | `src/earmark/models.py` | `Delivery`, `DecodeOptions`, `V1UploadFields`, `WHISPER_LANGUAGES`, `MAX_INITIAL_PROMPT_CHARS`, `V1_FORM_FIELDS`, `decode_key`, `JobResult.language`, the four `Job` fields and their validator, `Job.decode_options()`. |
| 1 | `src/earmark/db.py` | Migration `0005_v1`; `SCHEMA_VERSION` 5; `PRAGMA secure_delete = ON` in `connect`; `Database.checkpoint()`. |
| 1 | `src/earmark/jobs.py` | `NewJob` (`to` optional; `delivery`, `initial_prompt`, `language`), the delivery-free dedupe key, `_row_to_job`, `JobActiveError`, `mark_purged`, `checkpoint`. |
| 2 | `src/earmark/pipeline/runner.py` | The delivery-free path: no `episode.json`/`shared.json`, decode options to the transcriber, `complete` instead of deliver, no notify. `JobResult.language` for every job. |
| 2 | `src/earmark/pipeline/stages.py` | `Transcriber.cached`/`transcribe` take `options: DecodeOptions | None = None`. |
| 2 | `src/earmark/fakes.py` | `FakeTranscriber` with options; `TranscribeCall.options`. |
| 2 | `src/earmark/transcribe/base.py` | `TranscriptionEngine.profile_for`, `transcribe_file(..., options)`; `with_decode_options`; `CachedTranscriber` reads and writes the per-options tree. |
| 2 | `src/earmark/artifacts.py` | The `transcripts/<decode_key>/` tree: paths, read, write, remove. |
| 3 | `src/earmark/api/upload.py` | `receive_upload(..., text_fields=FORM_FIELDS)`. |
| 3 | `src/earmark/submission.py` | `Intake.submit_v1`. |
| 3 | `src/earmark/v1_jobs.py` (new) | `/v1` transcript reads and the purge. Light imports. |
| 3 | `src/earmark/api/schemas.py` | `V1Accepted`, `V1JobView`, `v1_job_view`. |
| 3 | `src/earmark/api/v1.py` (new) | `build_v1_router`. |
| 3 | `src/earmark/api/routes.py` | `build_router` includes the `/v1` router. |
| 3 | `src/earmark/transcripts.py` | `read_transcript_file` refuses a delivery-free job (409). |
| 4 | `contract/api-v1.md` (new) | The `/v1` contract (section 4.9). |
| 4 | `contract/README.md` | A section for the `/v1` seam. |
| 4 | `CLAUDE.md`, `README.md`, `docs/deploy.md` | Section 4.10. |
| 4 | tests | Sections 6.1–6.6, and the doc tests in 6.7. |
| 5 | `src/earmark/transcribe/faster_whisper.py` | `initial_prompt` through both model wrappers; `profile_for`; the language error hint. |
| 5 | `src/earmark/transcribe/remote.py` | `prompt` and `language` from options; `profile_for`; the prompt redacted from server errors. |
| 6 | tests | The decode-option tests (6.4, 6.6) and the slow test. |
| — | `tests/pipeline_env.py`, `tests/test_runner.py`, `tests/test_transcribe_base.py`, `tests/whisper_fakes.py` | Test Transcribers and engines gain the `options` parameter (mypy needs it to match the Protocols). |

No new dependency, setting, CLI command or backend. `.env.example` is unchanged.

## 4. Interfaces

Snippets are illustrative. Names, signatures, field names, messages and file names are
binding.

### 4.1 `models.py`

```python
Delivery = Literal["mail", "none"]
MAX_INITIAL_PROMPT_CHARS: Final = 1000
WHISPER_PROMPT_TOKENS: Final = 223  # documentation only: 448 // 2 - 1, faster-whisper 1.2.1
WHISPER_LANGUAGES: Final[tuple[str, ...]] = ("af", "am", "ar")  # all 100, section 2.3 order
V1_FORM_FIELDS: Final = ("initial_prompt", "language")
```

- `REQUEST_ERROR_TYPES` gains `"initial_prompt_too_long"` and `"language_invalid"`.
- `class DecodeOptions(Model)`: `initial_prompt: str | None = None`,
  `language: str | None = None`. `None` language means detect. Built only from validated
  values; no validators of its own.
- `class V1UploadFields(Model)`: the text fields of a `/v1` upload. Both default `None`.
  - `initial_prompt`: `strip()`; empty becomes `None`; longer than
    `MAX_INITIAL_PROMPT_CHARS` (`len`, code points) raises
    `PydanticCustomError("initial_prompt_too_long", "initial_prompt is too long (at most 1000 characters)")`.
    Otherwise kept verbatim (inner whitespace and newlines kept).
  - `language`: `strip().lower()`; empty becomes `None`; not in `WHISPER_LANGUAGES` raises
    `PydanticCustomError("language_invalid", "language must be a Whisper language code such as 'en'; leave it out to detect the language")`.
    `auto` is not accepted (leave the field out).
- `def decode_key(content_sha256: str, options: DecodeOptions) -> str`: the first 20 hex of
  `sha256(json.dumps(["v1", content_sha256, options.initial_prompt, options.language],
  ensure_ascii=False, separators=(",", ":")).encode("utf-8"))`. `ValueError` unless
  `content_sha256` is 64 lower-case hex.
- `JobResult` gains `language: str | None = None` (the transcript's language). Old rows
  without it still load.
- `Job` gains, all with defaults so every existing row and factory still validates:
  - `delivery: Delivery = "mail"`;
  - `initial_prompt: str | None = None`;
  - `language: str | None = None` (the requested language; `None`: detect);
  - `purged_at: AwareDatetime | None = None`.

  A new `model_validator(mode="after")`:
  - `delivery == "none"` requires `source == "upload"`, `to is None`, `instructions == ""`,
    `title is None`, `upload_name is None`, `delivery_state is None`;
  - `delivery == "mail"` requires `initial_prompt is None`, `language is None`,
    `purged_at is None`.
- `Job.decode_options(self) -> DecodeOptions | None`: `None` for a mail job, else
  `DecodeOptions(initial_prompt=self.initial_prompt, language=self.language)`.

### 4.2 `db.py`

- Append `Migration("0005_v1", (...))`:

  ```sql
  ALTER TABLE jobs ADD COLUMN delivery TEXT NOT NULL DEFAULT 'mail'
      CHECK (delivery IN ('mail', 'none'));
  ALTER TABLE jobs ADD COLUMN initial_prompt TEXT;
  ALTER TABLE jobs ADD COLUMN language TEXT;
  ALTER TABLE jobs ADD COLUMN purged_at TEXT;
  ```

  (four statements, one per tuple entry). `SCHEMA_VERSION` becomes 5. Existing rows get
  `'mail'` and `NULL`s.
- `Database.connect` runs `PRAGMA secure_delete = ON` right after `PRAGMA foreign_keys = ON`.
  It is a connection setting and writes nothing, so a refused database is still never
  modified.
- `Database.checkpoint(self) -> bool`: `PRAGMA wal_checkpoint(TRUNCATE)` on a fresh
  connection; `True` when the first column of the result is 0. Never raises for
  `sqlite3.OperationalError` busy/locked (returns `False`); other errors propagate.

### 4.3 `jobs.py`

```python
@dataclass(frozen=True, slots=True)
class NewJob:
    source: Source
    to: str | None  # mail: normalised, non-empty; delivery "none": None
    instructions: str = ""
    title: str | None = None
    url: str | None = None
    upload_file: str | None = None
    upload_name: str | None = None
    content_sha256: str | None = None
    shared_at: datetime | None = None
    delivery: Delivery = "mail"
    initial_prompt: str | None = None
    language: str | None = None


class JobActiveError(RuntimeError):
    """The job is queued or running; it cannot be purged."""

    def __init__(self, job: Job) -> None:
        super().__init__(job.id)
        self.job = job
```

- `_validate(new)`:
  - mail: as today (`to` non-empty), and `initial_prompt`, `language` must be `None`;
  - `"none"`: `source == "upload"`, `to is None`, `instructions == ""`, `title is None`,
    `upload_name is None`, `upload_file` and `content_sha256` set, no `url`. `ValueError`
    otherwise.
- **Dedupe key.** Mail keys are unchanged (the existing `_dedupe_key`). A delivery-free key is
  `sha256(json.dumps(["ingest", "upload", "none", "sha256:" + content_sha256,
  initial_prompt, language], ensure_ascii=False, separators=(",", ":")))` hex: a list of
  another length, so it can never equal a mail key. The lookup SQL is unchanged (a purged
  row's key is replaced, so it never matches).
- `enqueue` writes the four new columns (`purged_at` NULL); `_row_to_job` reads them
  (`purged_at` with `_opt_from_db`).
- `mark_purged(self, job_id: str) -> list[Job]`, in one `BEGIN IMMEDIATE`:
  1. read the row; none, or `delivery != 'none'`, raises `JobNotFoundError`;
  2. `purged_at` already set: return `[job]` unchanged;
  3. status `queued` or `running`: raise `JobActiveError(job)`;
  4. otherwise:

     ```sql
     UPDATE jobs
     SET purged_at = :now, initial_prompt = NULL, dedupe_key = 'purged:' || id,
         updated_at = :now
     WHERE delivery = 'none' AND dedupe_key = :key
       AND status IN ('done', 'failed', 'skipped') AND purged_at IS NULL
     RETURNING *
     ```

     with `:key` the job's current `dedupe_key`. Return the rows, the named job first, then
     by `seq`.
- `checkpoint(self) -> bool`: `self._db.checkpoint()`.
- `fail`, `skip`, `complete`, `release`, `requeue_stale` and the delivery writes are
  unchanged.

### 4.4 `artifacts.py`: the per-options tree

```python
DECODED_DIR: Final = "transcripts"
```

`ArtifactStore.__init__` also keeps `data_dir / DECODED_DIR`. New methods, each checking
`dkey` with `is_episode_key` (the same 20-hex shape) and raising `ValueError` otherwise:

| Method | Does |
|---|---|
| `decoded_dir(dkey) -> Path` | `<data_dir>/transcripts/<dkey>` |
| `decoded_json_path(dkey)`, `decoded_text_path(dkey)` | `…/transcript.json`, `…/transcript.txt` |
| `read_decoded(dkey, episode_key) -> Transcript \| None` | As `read_transcript`; `PermanentError("corrupt artifact: …")` when it does not parse or its `episode_key` is not `episode_key` |
| `write_decoded(dkey, transcript) -> Path` | `transcript.txt`, then `transcript.json`, both atomic (as `write_transcript`) |
| `has_decoded(dkey) -> bool` | `transcript.json` is a file |
| `remove_decoded(dkey) -> int` | Unlinks `transcript.json`, `transcript.txt` and every regular file whose name starts with `.transcript.` (atomic-writer temps) in the directory, then `rmdir` (an `OSError` from `rmdir` is ignored: something else is in it); returns the number of files unlinked; 0 when the directory does not exist. Never follows a symlink, never uses `rmtree`. Other `OSError`s propagate. |

The stored `Transcript.episode_key` is the audio's content key, as on the mail path.

### 4.5 Transcriber, engines and fake

`pipeline/stages.py`:

```python
class Transcriber(Protocol):
    def cached(
        self, episode: Episode, options: DecodeOptions | None = None, /
    ) -> Transcript | None: ...

    def transcribe(
        self, episode: Episode, audio: Path, options: DecodeOptions | None = None, /
    ) -> Transcript: ...
```

`options=None` is today's behaviour, byte for byte. With options, the episode must be an
upload or URL episode (`guid` `sha256:<hex>`); otherwise `PermanentError("decode options
apply to uploaded audio only")`.

`transcribe/base.py`:

```python
class TranscriptionEngine(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def profile(self) -> str: ...  # unchanged: profile_for(None)

    def profile_for(self, options: DecodeOptions | None, /) -> str: ...

    def transcribe_file(
        self, audio: Path, episode_key: str, options: DecodeOptions | None = None, /
    ) -> Transcript: ...


def with_decode_options(
    fields: Mapping[str, str | int | bool | None], options: DecodeOptions | None
) -> dict[str, str | int | bool | None]: ...
```

- `with_decode_options`: `options is None` returns `dict(fields)`. Otherwise a copy with
  `"language"` set to `options.language` and, when `options.initial_prompt is not None`,
  `"initial_prompt_sha256"` set to the hex sha256 of its UTF-8 bytes. The prompt text never
  goes into a profile, so `transcript.json` never holds it.
- `CachedTranscriber.cached(episode, options=None)`: `None` → as today. Otherwise
  `dkey = decode_key(<hex of episode.guid>, options)`, `read_decoded(dkey, episode.key)`,
  returned only when its `asr_profile == engine.profile_for(options)`.
- `CachedTranscriber.transcribe(episode, audio, options=None)`: the same shape as today, with
  `engine.transcribe_file(audio, key, options)`, the profile stamp
  `engine.profile_for(options)` and `write_decoded(dkey, …)` when options are given. Audio
  cleanup is unchanged (`delete_audio(episode.key)`; nothing is there for an upload).
  Its log line names the episode key and, with options, adds `(per-request options)`; never
  the prompt.

`transcribe/faster_whisper.py`:

- `WhisperModelLike.transcribe` and `BatchedPipelineLike.transcribe` gain the keyword
  `initial_prompt: str | None`. `BatchedWhisper.transcribe` passes it on. The engine always
  passes `initial_prompt=` (`None` for the mail path) and `language=` the effective language:
  `options.language` when options are given, else `self._options.language`.
- `profile_for(options)`: `asr_profile("faster_whisper", with_decode_options(<today's
  fields>, options))`; `profile` is `profile_for(None)`, unchanged byte for byte.
- `Transcript.language` is the effective language, else `info.language`.
- `_value_error`: the hint is `(check EARMARK_ASR_LANGUAGE)` only when the language came from
  the setting; with options it is `(check the request's language)`.

`transcribe/remote.py`:

- `transcribe_file(audio, key, options=None)`: the effective language as above, sent as
  `language` when not `None`; `options.initial_prompt`, when given, sent as the form field
  `prompt`. `transcript_from_response(..., language=<effective>)`.
- `profile_for(options)`: `asr_profile("remote", with_decode_options({model, language},
  options))`.
- The prompt is added to the strings redacted from the server's error text (the list
  `_status_error` gives `endpoint.error_detail`), so a server that echoes its input cannot put
  the prompt into `job.error`.

`fakes.py`:

- `TranscribeCall` gains `options: DecodeOptions | None = None` (so existing equality
  assertions hold).
- `FakeTranscriber.profile_for(options)` = `asr_profile("fake", with_decode_options({model,
  language}, options))`; `profile` unchanged. `cached`/`transcribe` take `options` and use the
  per-options tree like `CachedTranscriber`. With options, the transcript's language is
  `options.language or self.language`, and when `options.initial_prompt` is set the default
  segments get a fourth, `Segment(start=15.0, end=20.0, text=f"Prompt {h}.")` with `h` the
  first 12 hex of the prompt's sha256, so two prompts give two different transcripts without
  the prompt appearing in one. Explicit `segments=` are used as given.

### 4.6 The runner (`pipeline/runner.py`)

For `job.delivery == "none"`:

1. `resolve`: the upload branch as today (missing or changed staged file is permanent).
2. Record: `set_episode_key(claim, episode.key)` only. No `write_episode`, no `record_share`.
3. `options = job.decode_options()`; `transcriber.cached(episode, options)`; otherwise
   `set_stage(claim, "transcribe")` and `transcriber.transcribe(episode, audio, options)`.
   (No `fetch` stage: the audio is the staged upload.)
4. The check and the warnings as today.
5. **No `deliver` stage**: after the interruptible window,
   `complete(claim, JobResult(duration_s=…, language=transcript.language))`.
6. `finish(job, episode)`: `staging.release(job)` as today.

`Pipeline.notify(job, episode)` returns at once when `job.delivery == "none"`. Every path
reaches notify through it (`run`, `_finish`, the worker's `requeue_stale` handling), so one
check covers success, failure, skip and a stale lease. Mail jobs: unchanged, except that
`JobResult` now also carries `language`.

The runner's outcome log lines keep their wording; for a delivery-free job they carry the
job id and earmark's own short message (staged file names are random ids). No line names the
upload's file name (never stored), the prompt, or transcript text.

### 4.7 Intake, upload reader and views

`api/upload.py`: `receive_upload(headers, stream, staging, *, max_file_bytes, text_fields:
tuple[str, ...] = FORM_FIELDS)`. `_State` holds `text_fields`; the unknown-field message lists
`file` and `text_fields`. Everything else (caps, part limit, audio check, cleanup) is shared.

`submission.py`:

```python
def submit_v1(self, fields: V1UploadFields, staged: StagedUpload, /) -> Enqueued:
    """Enqueue a delivery-free upload. Deletes ``staged`` when this raises or finds an
    existing job. No recipient check: nothing is sent."""
```

It enqueues `NewJob(source="upload", to=None, upload_file=staged.name, upload_name=None,
content_sha256=staged.sha256, delivery="none", initial_prompt=…, language=…)` and logs
exactly `enqueued job <id>: upload of <n> bytes, no delivery (created=<bool>)`. No language,
no prompt, no file name.

`api/schemas.py`:

```python
class V1Accepted(ApiModel):
    job_id: JobId
    status: JobStatus


class V1JobView(ApiModel):
    job_id: JobId
    status: JobStatus
    stage: Stage | None
    error: str | None  # scrub_urls(job.error)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    language: str | None
    duration_s: float | None
    purged_at: AwareDatetime | None
```

`v1_job_view(job)`: `language` is `job.result.language` when there is a result, else
`job.language` (the requested one, or `None`); `duration_s` is `job.result.duration_s` or
`None`. `JobView` is unchanged (its `result` now shows `language` too).

`v1_jobs.py` (imports models, artifacts, jobs, staging; no FastAPI, no heavy imports):

```python
V1TranscriptFormat = Literal["json", "text"]


class V1SegmentView(BaseModel):  # frozen, extra="forbid"
    start: float
    end: float
    text: str


class V1TranscriptView(BaseModel):  # frozen, extra="forbid"
    job_id: JobId
    language: str | None
    duration_s: float
    segments: tuple[V1SegmentView, ...]
    warnings: tuple[str, ...]


def v1_job(store: JobStore, job_id: str) -> Job | None: ...  # None unless delivery "none"


def read_v1_transcript(
    job: Job, artifacts: ArtifactStore, fmt: V1TranscriptFormat
) -> TranscriptFile: ...  # raises TranscriptUnavailable (transcripts.py)


def purge_job(
    store: JobStore, artifacts: ArtifactStore, staging: Staging, job_id: str
) -> Job: ...  # raises JobNotFoundError, JobActiveError
```

`read_v1_transcript`, in order: purged → `TranscriptUnavailable("gone", PURGED)`; status
`queued`/`running` → `not_ready` with step 33's `NOT_YET` / `NOT_YET_AT_STAGE`; `failed` →
`not_ready`, `V1_FAILED`; `skipped` → `not_ready`, step 33's `SKIPPED`; done: `dkey =
decode_key(job.content_sha256, job.decode_options())`; missing `transcript.json` → `gone`,
`V1_GONE`; `json`: `read_decoded` (corrupt or `OSError` → `unreadable`, `V1_UNREADABLE`),
then `V1TranscriptView` dumped with `indent=2` plus `"\n"`, `application/json`; `text`:
`transcript.txt`'s bytes (missing → `gone`; `OSError` → `unreadable`), `text/plain;
charset=utf-8`. It writes nothing and logs nothing.

`purge_job`, in order (files first, so a failure leaves the job unpurged and a retry works):

1. `v1_job(store, job_id)`; `None` → `JobNotFoundError`.
2. Already purged → return it.
3. Queued or running → `JobActiveError(job)`.
4. `artifacts.remove_decoded(decode_key(job.content_sha256, job.decode_options()))`.
5. `staging.remove_upload(job.upload_file)` (normally already gone).
6. `purged = store.mark_purged(job_id)`; for each other purged job, `staging.remove_upload`,
   logging (never raising) an `OSError` by type name.
7. `store.checkpoint()`; a `False` is logged at DEBUG.
8. Log `job <id> purged (<n> job(s), <m> file(s) removed)`; return `purged[0]`.

Messages (constants in `v1_jobs.py`; the contract quotes them):

| Constant | Text |
|---|---|
| `PURGED` | `this job was deleted; its transcript is gone` |
| `V1_FAILED` | `this job has no transcript; it failed` |
| `V1_GONE` | `the transcript of this job is gone; send the audio again` |
| `V1_UNREADABLE` | `the transcript of this job is unreadable; send the audio again` |
| `STILL_ACTIVE` | `this job is still {status}; delete it once it has finished` |
| `V1_ONLY_MULTIPART` | `send a multipart form (Content-Type: multipart/form-data) with the audio in a field named 'file'` |

`transcripts.py`: `read_transcript_file` first raises `TranscriptUnavailable("not_ready",
V1_ELSEWHERE)` for `job.delivery == "none"`, with `V1_ELSEWHERE = "this job was sent to /v1;
fetch its transcript from /v1/jobs/{id}/transcript"` (literal `{id}`, not formatted). So the
unversioned route answers 409 and `earmark transcript` prints it.

### 4.8 HTTP surface (`api/v1.py`)

`build_v1_router(intake: Intake, *, artifacts: ArtifactStore, max_upload_bytes: int) ->
APIRouter` with `prefix="/v1"`; `build_router` ends with
`router.include_router(build_v1_router(...))` before returning. The bearer middleware already
covers every path but `/healthz`.

| Route | Answer |
|---|---|
| `POST /v1/transcriptions` | Media type `multipart/form-data`, else **415** `V1_ONLY_MULTIPART` (JSON included). `receive_upload(..., text_fields=V1_FORM_FIELDS)`; its `UploadRejected` statuses as today (**413** over `EARMARK_MAX_UPLOAD_MB`, **415** not audio, **422** malformed, no or two `file` parts, empty file, unknown or repeated field, field too long or not UTF-8). Then `V1UploadFields.model_validate(fields)`: **422** with `describe_validation_errors(..., body_fields=("file", *V1_FORM_FIELDS))`, the staged file aborted. Then `intake.submit_v1` in the threadpool. **202** `V1Accepted` with `Location: /v1/jobs/{id}` (dedupe hits too). A client disconnect: 400 with no body, as today. |
| `GET /v1/jobs/{job_id}` | **200** `V1JobView`; **404** `job not found` for an unknown id or a mail job. `Cache-Control: no-store`. |
| `GET /v1/jobs/{job_id}/transcript?format=json\|text` | Default `json`. **200** with `Cache-Control: no-store`; **404**; **409** not ready, failed, skipped; **410** purged or gone; **422** an unknown format (FastAPI's query validation through the existing handler: `format: Input should be 'json' or 'text'`); **500** unreadable. Logs `job <id>: /v1 transcript served as <fmt>` or `job <id>: no /v1 transcript as <fmt>: <reason>`. |
| `DELETE /v1/jobs/{job_id}` | **200** `V1JobView` of the purged job (repeatable); **404**; **409** `STILL_ACTIVE` with the status. Sync handler (threadpool). An `OSError` while deleting files is a 500 with `detail` `could not delete the transcript files; try again`, logged with the type name only. |

Every error body is `{"detail": "<message>"}`. A missing or wrong token is 401 from the
middleware, before the body is read.

### 4.9 `contract/api-v1.md`

The contract text sits between `<!-- api-v1:begin -->` and `<!-- api-v1:end -->`;
`tests/test_api_v1_contract.py` pins the sha256 of that block (stripped of leading and
trailing newlines, plus one `"\n"`, as `_contract_block` does for mail). Above the block: a
title, one paragraph saying what the file is, and that it changes only additively (a breaking
change is `/v2`, with its own file). The block must state, in this order:

1. **Version and transport.** `/v1`, version 1. Every request needs `Authorization: Bearer
   <token>`; 401 otherwise, with `WWW-Authenticate: Bearer realm="earmark"`. The service
   listens on loopback; programs reach it over an SSH tunnel to `127.0.0.1:8787` or through the
   operator's TLS proxy. Bodies are UTF-8; errors are `{"detail": "<message>"}`; clients must
   ignore unknown fields in a 200 or 202 body (that is what "additive" means).
2. **`POST /v1/transcriptions`.** Multipart only; fields `file` (required, one, an audio
   content type or an audio file name extension), `initial_prompt` (optional; at most **1000
   characters** after surrounding whitespace is stripped; empty means none), `language`
   (optional; one of the 100 codes, listed; case-insensitive; empty or absent means detect).
   Any other field, `to` and `title` included, is a 422. The size cap is the server's
   `EARMARK_MAX_UPLOAD_MB` (1024 MiB unless the operator changed it), named in the 413's
   detail. The answer: 202 `{"job_id", "status"}` and `Location`. A request identical to one
   queued, running, or done within the server's dedupe window (600 s unless changed) — the same
   bytes, `initial_prompt` and `language` — returns that job's id. The file's name is used for
   the audio check only and is not stored. Nothing is sent anywhere: no mail, no push.
3. **The prompt and Whisper's window.** Whisper conditions on at most the last 223 tokens of
   the prompt and ignores what comes before; 1000 characters is about 223 tokens of English
   names or prose and more than that for technical terms (about 630 characters) and for
   Chinese or Japanese text (about 140). Put the most important terms last. How far into the
   audio the prompt reaches depends on the server's decoding mode (the first windows with
   sequential decoding, every chunk with batched decoding). It is a hint, not a guarantee.
4. **`GET /v1/jobs/{job_id}`**: the fields of `V1JobView`, each with its meaning; `status`
   values `queued`, `running`, `done`, `failed`, `skipped`; `stage` is informational (values
   may be added); `language` (the requested one until the job is done, then the transcript's);
   `duration_s` once done; `purged_at` once deleted. 404 for an id this API did not create.
5. **`GET /v1/jobs/{job_id}/transcript`**: `format=json` (default) and `format=text`, each
   shape; segments are in order, `start` never decreases, `end >= start`, times in seconds;
   `text` is one `[HH:MM:SS] text` line per segment; the states 404, 409, 410, 422, 500 with
   their details. A fetch is repeatable until the job is deleted.
6. **`DELETE /v1/jobs/{job_id}`**: what it removes (the transcript files, any staged upload,
   the prompt); that the transcript is shared by every job with the same audio and options and
   that deleting one deletes it for all finished ones, which also become deleted; that a
   queued or running job is refused with 409 and there is no cancel; that it answers 200 with
   the job's view and can be repeated; what remains (the job row: id, status, stage, error,
   times, language, duration, `purged_at`, and a hash of the audio); and that deletion is not
   shredding (filesystem blocks and backups of the server's data directory are outside
   earmark's reach).
7. **Errors**: a table of every status and its `detail` text, the exact messages of section
   4.7 and `upload.py`, and the validation messages of section 4.1.
8. **Examples**: one fenced `json` (or `text`) block per response, each preceded by
   `<!-- api-v1-example:<name> -->`, named `accepted`, `job-queued`, `job-done`,
   `transcript-json`, `transcript-text`, `deleted`, and `error` (a 409). They are produced by
   the code from the fixed inputs of section 6.5, and pasted in exactly.

### 4.10 Docs for the implementer

- **`CLAUDE.md`**:
  - The opening: "The two seams are…" becomes three: the request, the mail contract, and the
    `/v1` API (`contract/api-v1.md`, step 36) for programs such as earshot.
  - "The service today": add one sentence: a `/v1` upload is transcribed and kept for its
    client, with no mail and no push.
  - Rules: amend "**The recipient allowlist is the gate**" to say it gates every `to`, and that
    a `/v1` job has none, sends nothing, and has the bearer token as its only gate. Add
    "**`/v1` is a contract** (step 36): `contract/api-v1.md`, pinned by hash; additive changes
    only; a breaking change is `/v2`; the unversioned routes stay as they are." Add to the leak
    rules: for a `/v1` job no log line carries the upload's file name (never stored), the
    `initial_prompt` or transcript text; logs carry ids, sizes, timings, status and errors.
- **`README.md`**: "The two seams" paragraph → three, with the anchor
  `#system-three-parts-and-three-seams`; the HTTP API sentence names all routes, including
  `POST /v1/transcriptions`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/transcript` and
  `DELETE /v1/jobs/{id}` (the route test reads it); one short paragraph under "How it works":
  `/v1` for programs, nothing leaves the Spark, pointing at `contract/api-v1.md` and
  `docs/deploy.md` section 20.
- **`docs/deploy.md`**:
  - fix the link at the end of section 8.5 to `#system-three-parts-and-three-seams`;
  - section 15: the database moves to schema 5 on first start (the `/v1` columns); an older
    image refuses it; back up first;
  - a new **`## 20. The /v1 API for programs`** with: 20.1 reaching it over an SSH tunnel
    (`ssh -N -L 8787:127.0.0.1:8787 operator@spark.example.com`, then
    `http://127.0.0.1:8787` with the token), no TLS needed on the tunnel; 20.2 a `curl`
    example with `--form-string` for `initial_prompt` and `language` and `-F file=@…` for the
    audio, then the job and transcript fetches and the DELETE; 20.3 the upload cap and audio
    format: Whisper resamples to 16 kHz mono, so send 16 kHz mono (FLAC about 60 MB per hour,
    a 4-hour file about 250 MB, under the 1024 MiB default; 48 kHz stereo is several times
    larger); 20.4 time and memory (tens of minutes on the GPU for 4 hours, on the order of
    12 GB of memory; a restart starts the file over); 20.5 the prompt's reach and
    `EARMARK_ASR_BATCH_SIZE` (section 2.1; the operator's setting); 20.6 what DELETE removes
    and what it does not.

## 5. Behaviour and edge cases

### 5.1 Nothing leaves the Spark

- The runner's delivery-free path never calls `compose_mail`, never sets stage `deliver`,
  never calls `mailer.send` and never calls `notifier.notify`, including through
  `Pipeline.finish` from `requeue_stale`.
- The API handler makes no outbound request, as before. The remote ASR backend, if the
  operator chose it, receives the audio and the prompt; that is the operator's own server and
  the contract does not promise otherwise ("the server's ASR"), but `docs/deploy.md` 20.6 says
  so.

### 5.2 What is stored for a `/v1` job

- In the row: `delivery='none'`, `source='upload'`, `upload_file` (random), `content_sha256`,
  `episode_key` (the content key, set by the runner), `initial_prompt` (until purged),
  `language`, the usual status columns, `result` (`duration_s`, `language`). `to`, `title`,
  `upload_name` and `instructions` are empty. The file name is never stored, so
  `sources.audio_title` gives `Uploaded audio` if anything ever asks.
- On disk: the staged upload while queued or running; `transcripts/<dkey>/transcript.json` and
  `transcript.txt`. No `episodes/<key>/episode.json` or `shared.json` (nothing renders a note).
- `transcript.json`'s `asr_profile` carries `initial_prompt_sha256`, not the prompt.

### 5.3 Cache keying

- `dkey = decode_key(sha256, DecodeOptions(initial_prompt, language))`. Same bytes and same
  options: one directory; another prompt or language: another directory. The mail path keeps
  `episodes/<key>/`; a `/v1` job never reads or writes it.
- The ASR profile is still checked: if the operator changes the model, the next job with those
  options re-transcribes and overwrites the directory, and an older finished job sharing it
  then serves the new transcript (as step 33's note can differ from the mailed one). The
  contract's "shared by identical requests" covers it.
- A `/v1` job with no prompt and no language and a mail job of the same bytes do not share a
  transcript (different trees). That isolation is intended.

### 5.4 Dedupe and purge together

- A purged job's `dedupe_key` becomes `purged:<id>`, so it never matches a new request; an
  identical re-send after a purge is a new job that transcribes again.
- Siblings: the finished `/v1` jobs with the same dedupe key are exactly those with the same
  bytes and options, so they are exactly the jobs sharing `dkey`. They are purged with it.
- An active sibling (queued or running) is untouched. If it had already found the cached
  transcript, it completes `done` and its fetch is 410 `V1_GONE`; if not, it transcribes and
  writes its own. Both are honest outcomes; the contract names the first.
- Files before the row: if deleting fails, nothing is marked and DELETE answers 500; a retry
  recomputes `dkey` from the intact row. Once marked, the prompt is gone and `dkey` cannot be
  recomputed, but there is nothing left to delete.

### 5.5 Validation order on POST

Auth (middleware) → media type (415) → `Content-Length` vs cap (413) → part by part: unknown
or repeated field (422), file part's audio check (415), sizes (413/422) → end of body (422 if
no file or empty) → `V1UploadFields` (422) → enqueue. Every rejection after the file part
started deletes the partial or staged file. A prompt is rejected by length only after the
body has arrived (fields can follow the file); that is fine.

### 5.6 Error and log text

- `job.error` for a `/v1` job is one of earmark's own messages (staged file names are random
  ids), or the runner's "unexpected error" summary of a library exception, which names a type,
  a scrubbed message and a source line. No library on this path puts the prompt or transcript
  text in an exception message; the remote engine's server text is redacted of the prompt
  (4.5).
- The runner still logs the traceback of an unexpected exception (the operator needs it);
  tracebacks carry code lines, not data.
- faster-whisper's own INFO lines (duration, VAD, detected language) carry no text.

### 5.7 Other

- `language` normalises `EN` to `en`; `zh-CN`, `english` and `auto` are 422.
- An `initial_prompt` of exactly 1000 characters is accepted; 1001 is 422. Count after
  `strip()`.
- The unversioned `GET /jobs/{id}` of a `/v1` job shows `source: upload`, `to: null`,
  `title: null`; the unversioned transcript route answers 409 `V1_ELSEWHERE`.
- A `/v1` job id under the unversioned `GET /jobs/{id}/transcript` is never served from a mail
  job's cache of the same bytes (4.7's early refusal).
- `JobResult.language` is now present in `JobView.result` for jobs finished from this step on;
  older rows show it as `null`.

## 6. Tests

Fixtures: none from outside. Audio bytes are `tests/multipart_helpers.audio(n)` (fake bytes
with an audio content type); the fake transcriber produces the segments. The slow test uses
the existing `tiny` model and JFK clip (`tests/slow_assets.py`). Every API test drives the real
app (`create_app` with an injected `JobStore`, through `TestClient` or
`tests/asgi_helpers.call_asgi` for streaming cases) and runs the worker in-process
(`tests/pipeline_env.py` style: a `Pipeline` over `make_fake_backends`, `worker.run_once()`).

New files: `tests/test_api_v1.py`, `tests/test_v1_jobs.py`, `tests/test_api_v1_contract.py`,
`tests/api_v1_examples.py` (the fixed-input builder for the examples).

### 6.1 Brief: "A `/v1` job runs end to end on the fakes and never reaches the mailer or the notifier, on success, failure, and skip. A test fails if either is called."

In `tests/test_api_v1.py`, with backends whose mailer and notifier raise a test-only
`CalledError(BaseException)` from `send`/`notify` (a `BaseException` so the runner's
`except Exception` cannot swallow it), plus `calls == []` on the fakes:

- `test_v1_job_done_without_mail_or_push`: POST, `run_once`, status `done`, stage
  `transcribe`, `result.duration_s` and `result.language` set, `delivery_state` null,
  no outbox file, `uploads/` empty.
- `test_v1_job_failed_without_mail_or_push`: `transcriber.fail_next(PermanentError(...))`.
- `test_v1_job_transient_exhausted_without_mail_or_push`: `max_attempts=1`, a
  `TransientError`.
- `test_v1_job_skipped_without_mail_or_push`: `transcriber.fail_next(SkipEpisode(...))`.
- `test_v1_stale_lease_failure_without_push` (`tests/test_worker.py`): a `/v1` job left
  `running` with an old heartbeat at max attempts; `run_once` fails it through
  `requeue_stale` and `Pipeline.finish` calls no notifier.
- `test_v1_ignores_recipient_allowlist`: the app built with `EARMARK_ALLOWED_RECIPIENTS` unset
  accepts a `/v1` POST (202) while `POST /transcriptions` with a `to` is refused.
- `test_mail_job_still_delivers` (`tests/test_runner.py`): a mail upload job of the same bytes
  still composes, sends and notifies once (regression).

### 6.2 Brief: "`to`, `title`, unknown fields, a missing file, an over-long `initial_prompt` and a bad `language` are each a 422 with a short message. A missing or wrong token is a 401."

`tests/test_api_v1.py`, parametrised, each asserting status, the exact `detail`, and that
`uploads/` is empty afterwards:

- `test_v1_rejects_field[to]`, `[title]`, `[instructions]`, `[timestamp]`, `[other]`:
  `unknown field '<name>' (allowed: file, initial_prompt, language)`.
- `test_v1_requires_file`: no file part → `the form must have exactly one file field named 'file'`;
  `test_v1_rejects_two_files`; `test_v1_rejects_empty_file`.
- `test_v1_initial_prompt_cap`: 1000 characters (after a leading and trailing space) → 202;
  1001 → 422 `initial_prompt is too long (at most 1000 characters)`; a 1000-character prompt of
  multi-byte characters (`é`) → 202 (counted in code points).
- `test_v1_language_rules`: `en`, `EN`, ` yue ` → 202 stored lower-case; `xx`, `english`,
  `zh-CN`, `auto` → 422 with the language message; empty → 202, stored `None`.
- `test_v1_rejects_json` (415 `V1_ONLY_MULTIPART`), `test_v1_rejects_non_audio` (415),
  `test_v1_upload_too_large` (413 naming `EARMARK_MAX_UPLOAD_MB`, with a small
  `max_upload_bytes`).
- `test_v1_routes_need_token[route]` for all four routes: no header and a wrong token → 401,
  and the POST body is never read (`call_asgi` counts `receive` calls, as step 24's tests do).
- `test_v1_accepted_shape`: 202 body has exactly `job_id` and `status`, `Location:
  /v1/jobs/<id>`.

### 6.3 Brief: "No log line carries the upload's file name, the `initial_prompt` or transcript text; a test feeds marked values through submit, run, fetch and purge and searches the logs."

`tests/test_leaks.py::test_v1_job_leaks_nothing`, with the module's `log_stream` (earmark's
real handler at DEBUG): upload with file name `MARKNAME-q7.flac`, `initial_prompt`
`MARKPROMPT-…` (padded to 1000 characters), the fake transcriber's `segments=` containing
`MARKTEXT-…`; POST, `run_once`, GET the job, GET the transcript in both formats, DELETE, GET
again. Assert: no marker in the log output; `MARKTEXT` in the fetched transcript (the test
reaches it); the job row's `upload_name` is `None`; after the purge, no marker of the name or
the prompt in the bytes of any file under the data directory (`earmark.db`, its `-wal` and
`-shm` included), and `MARKTEXT` in none either.

Also `test_v1_enqueue_log_line`: the exact enqueue line of 4.7.

### 6.4 Brief: "Two jobs of the same audio with different `initial_prompt` values each serve their own transcript, and an identical re-send reuses the cache without transcribing again."

`tests/test_api_v1.py`:

- `test_v1_prompts_get_their_own_transcripts`: the same bytes with prompts `A` and `B`; both
  run; each `GET …/transcript` returns its own `Prompt <hash>.` segment; two directories under
  `transcripts/`; `fakes.transcriber.computed == 2`.
- `test_v1_language_gets_its_own_transcript`: same bytes, `language` absent vs `de`; two
  directories; the second's `language` is `de`.
- `test_v1_identical_resend_reuses_cache`: the dedupe window set to 0 (`JobStore(...,
  dedupe_window=timedelta(0))`), the same bytes and prompt sent after the first is done → a
  new job id, `done`, `computed` still 1, the same transcript bytes.
- `test_v1_identical_resend_within_window_returns_same_job`: default window → the same
  `job_id`, the second staged upload deleted.
- `test_v1_cache_isolated_from_mail_cache`: a mail upload of the same bytes was done first; the
  `/v1` job still transcribes (`computed` 2) and `episodes/<key>/transcript.json` is
  unchanged.

`tests/test_transcribe_base.py`:

- `test_cached_with_options_reads_decoded_tree`, `test_profile_mismatch_retranscribes_decoded`,
  `test_options_need_sha256_episode` (a podcast episode with options → `PermanentError`),
  `test_with_decode_options_hashes_prompt` (no prompt text in the profile),
  `test_options_none_is_unchanged` (same files, same profile string as before the step:
  compare to a literal).

`tests/test_artifacts.py`: `test_decoded_paths_validated`, `test_write_read_decoded`,
`test_remove_decoded_counts_and_spares_other_files` (a stranger file keeps the directory),
`test_remove_decoded_ignores_symlinks` (a symlinked entry is not followed).

### 6.5 Brief: "The responses match the examples in `contract/api-v1.md`, and the contract's hash is pinned."

`tests/api_v1_examples.py` builds every example deterministically:

- clock: a `MutableClock` starting at `2026-11-28T09:00:00Z`, advanced 5 s before the claim
  and 600 s before completion (`job-done`), and 60 s before the DELETE;
- job id: `earmark.jobs.uuid.uuid4` monkeypatched to return
  `uuid.UUID("3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01")` (the claim's lease token comes from the same
  call and is never shown, so one fixed value is fine with one job);
- upload: 64 bytes of `audio()`, `filename="meeting.flac"`, `Content-Type: audio/flac`, no
  prompt, `language=en`;
- fake transcriber `segments=(Segment(start=0.0, end=4.2, text="Good morning, everyone."),
  Segment(start=4.2, end=9.8, text="Let's start with the roadmap."))`, `language="en"`.

`tests/test_api_v1_contract.py`:

- `test_api_v1_contract_is_pinned`: sha256 of the block equals `API_V1_SHA256`.
- `test_api_v1_example_matches[name]` for the seven names: the response body from
  `api_v1_examples` equals the fenced block after `<!-- api-v1-example:<name> -->`, compared
  as parsed JSON for `json` blocks and as exact text for `transcript-text`.
- `test_api_v1_error_table_matches`: every `detail` in the contract's error table is a message
  the code defines (import the constants, the `upload.py` messages and the two validation
  messages) and each constant appears in the table.
- `test_api_v1_languages_listed`: the contract lists exactly `WHISPER_LANGUAGES`.
- `test_api_v1_cap_stated`: the block contains `1000 characters` and `223 tokens`, and
  `MAX_INITIAL_PROMPT_CHARS == 1000`.
- `test_api_v1_contract_placeholders_only`: the existing `_assert_placeholders` from
  `tests/test_docs.py` (import it, or add `contract/api-v1.md` to `CONTRACT_DOCS`).

### 6.6 Criteria this plan adds

- **The other states and DELETE** (brief: "`DELETE` removes the job's files and prompt, a
  fetch afterwards is a 410, and a queued or running job is refused. A test shows what remains
  on disk."), `tests/test_v1_jobs.py`:
  - `test_purge_removes_files_and_prompt`: after DELETE, `transcripts/<dkey>/` is gone, the
    staged upload is gone, `initial_prompt` is `NULL` (read the row with plain `sqlite3`),
    `purged_at` set, GET transcript is 410 `PURGED`, GET job is 200 with `purged_at`.
  - `test_purge_leaves_only_the_row`: walk the data directory after a purge and assert the
    exact set of remaining files (`earmark.db` and, if present, `-wal`/`-shm`; empty
    `uploads/`, `downloads/` absent or empty, `transcripts/` empty or absent; no `episodes/`
    entry for the key).
  - `test_purge_refuses_queued` and `test_purge_refuses_running` (claimed, not finished): 409
    `this job is still queued; …` / `running`, nothing deleted.
  - `test_purge_is_repeatable`: a second DELETE is 200 with the same view.
  - `test_purge_marks_finished_siblings`: three identical requests (window 0): two done, one
    queued; DELETE the first → both done jobs purged, both prompts NULL, the queued one
    untouched with its prompt; the queued one then runs and serves its own transcript.
  - `test_purge_files_first`: `remove_decoded` raising `OSError` → 500, the row unchanged, a
    retry succeeds.
  - `test_purge_unknown_and_mail_job_404`.
  - `test_purge_checkpoint_busy_is_ignored`: `Database.checkpoint` returning `False` does not
    fail the DELETE.
- **States of the transcript route**: `test_v1_transcript_states` (queued 409, running 409
  with stage, failed 409 `V1_FAILED`, skipped 409, done 200, files deleted by hand 410
  `V1_GONE`, corrupt json 500 `V1_UNREADABLE`, `format=note` 422), `test_v1_transcript_json_shape`
  (exact keys; no `episode_key`, `speaker`, profile, model), `test_v1_transcript_text_bytes`
  (equals `transcript.txt`), `test_v1_transcript_no_store` (header).
- **Visibility** (P3): `test_v1_routes_hide_mail_jobs` (GET job, GET transcript, DELETE of a
  mail job → 404), `test_unversioned_routes_show_v1_job` (`GET /jobs` lists it; `GET
  /jobs/{id}` has `to: null`), `test_unversioned_transcript_refuses_v1_job` (409
  `V1_ELSEWHERE`, for every format; `earmark transcript` in local mode prints it and exits 1).
- **The view**: `test_v1_job_view_fields` (exact keys; `language` is the requested one while
  queued, the transcript's when done; `duration_s` null until done).
- **Store and schema** (`tests/test_jobs.py`, `tests/test_db.py`, `tests/test_models.py`):
  `test_enqueue_v1_validates` (each forbidden combination of 4.3 is a `ValueError`),
  `test_v1_dedupe_key_differs_from_mail`, `test_mail_dedupe_key_unchanged` (a literal),
  `test_mark_purged_*` (unknown, mail, active, already purged, siblings),
  `test_migration_0005_keeps_rows` (a version-4 database with rows migrates; rows get
  `'mail'`; `SCHEMA_VERSION == 5`), `test_secure_delete_on` (`PRAGMA secure_delete` reads 1 on a
  store connection), `test_checkpoint_truncates_wal`, `test_job_delivery_rules` (the Job
  validator), `test_v1_upload_fields_*`, `test_decode_key_stable` (a literal for fixed
  inputs), `test_job_result_language_optional` (an old JSON without it loads).
- **Engines** (`tests/test_faster_whisper.py`, `tests/whisper_fakes.py` gains
  `initial_prompt` in both fakes' signatures and recorded calls):
  `test_initial_prompt_passed_sequential`, `test_initial_prompt_passed_batched`,
  `test_mail_path_passes_none_and_setting_language`, `test_options_language_overrides_setting`
  (and `None` means detect even with `ASR_LANGUAGE=en`), `test_profile_for_options`,
  `test_profile_unchanged_without_options` (literal strings for batching off and on),
  `test_language_error_hint_names_request`. `tests/test_remote_asr.py`:
  `test_remote_sends_prompt_and_language`, `test_remote_omits_prompt_without_options`,
  `test_remote_redacts_prompt_from_server_error`, `test_remote_profile_for_options`.
- **Fakes**: `tests/test_fakes.py::test_fake_transcriber_options` (tree, profile, the prompt
  segment, language).
- **Long jobs**: `tests/test_worker.py::test_v1_long_transcription_keeps_its_lease`, the
  existing heartbeat test's shape with a `/v1` job blocking inside `transcribe` (the
  `BlockingTranscriber` of `tests/pipeline_env.py`, updated for `options`).
- **The real library** (`tests/test_faster_whisper_slow.py`, `slow`):
  `test_whisper_languages_match_faster_whisper` (`WHISPER_LANGUAGES ==
  faster_whisper.tokenizer._LANGUAGE_CODES`), `test_real_transcription_with_initial_prompt`
  (the JFK clip with `tiny`, a prompt and `language="en"`, sequential and with
  `ASR_BATCH_SIZE=2`; both produce segments).

### 6.7 Existing tests that must change

After this plan's `docs/ARCHITECTURE.md` edit, exactly four doc tests fail (checked):
`test_doc_anchors_resolve[README.md]`, `test_doc_anchors_resolve[docs/deploy.md]` (the
renamed System anchor), `test_architecture_opens_with_system_section` and
`test_architecture_system_seams`. Fix them with the docs and tests below; the repo-layout test
already passes, because `contract/api-v1.md` carries step 36, still `todo`.

- `tests/test_docs.py`: `SYSTEM_HEADING` becomes `## System: three parts and three seams`;
  `test_architecture_system_seams` reads `### The three seams` with rows `Request`, `Mail`,
  `API`, the API row containing `/v1` and `contract/api-v1.md`; the repo-layout and route
  tests pass with the new entries; add `contract/api-v1.md` to `CONTRACT_DOCS`; add a test that
  `CLAUDE.md` and the README state the `/v1` rules (`contract/api-v1.md`, "no mail and no
  push" or similar wording you choose and pin) and that `docs/deploy.md` has
  `## 20. The /v1 API for programs` with `ssh -N -L 8787:127.0.0.1:8787`, `16 kHz`,
  `EARMARK_MAX_UPLOAD_MB`, `EARMARK_ASR_BATCH_SIZE` and `DELETE`.
- `tests/test_db.py`: anything pinning schema version 4 or the column count.
- Tests building `Transcriber` stand-ins (`tests/pipeline_env.py`, `tests/test_runner.py`
  `_WrongTranscriber`) and `TranscriptionEngine` stand-ins (`tests/test_transcribe_base.py`):
  add the `options` parameter.
- Any assertion on the exact `calls` dicts of `tests/whisper_fakes.py`: add `initial_prompt`.
- `tests/test_contract_samples.py` and the mail samples: must not change (the mail path is
  byte-identical). If a v2 sample differs, the change is wrong.

## 7. Verification

```bash
export TMPDIR=/home/sandy/.cache/earmark-step36-verify && mkdir -p "$TMPDIR"
cd /home/sandy/dev/earmark
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_api_v1.py tests/test_v1_jobs.py tests/test_api_v1_contract.py -q
uv run pytest tests/test_leaks.py -k v1 -q
git diff --stat contract/samples   # must be empty
```

The slow tests need the `transcribe` extra, which the repo `.venv` lacks. Run them from a
scratch copy, never by installing into the repo venv:

```bash
cp -r /home/sandy/dev/earmark "$TMPDIR/repo" && cd "$TMPDIR/repo" && rm -rf .venv
UV_PROJECT_ENVIRONMENT="$TMPDIR/venv" uv sync --extra transcribe
UV_PROJECT_ENVIRONMENT="$TMPDIR/venv" uv run pytest -m slow \
  tests/test_faster_whisper_slow.py -k "languages or initial_prompt" -q
```

Live smoke on the fakes (all backends `fake`, as in "Testing" in `docs/ARCHITECTURE.md`):

```bash
cd "$TMPDIR" && export EARMARK_DATA_DIR="$TMPDIR/data" EARMARK_API_TOKEN=smoke-token-0123456789
export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_YOUTUBE_BACKEND=fake
export EARMARK_ASR_BACKEND=fake EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
uv --directory /home/sandy/dev/earmark run earmark serve &   # 127.0.0.1:8787
head -c 4096 /dev/urandom > "$TMPDIR/meeting.flac"
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -F "file=@$TMPDIR/meeting.flac;type=audio/flac" \
  --form-string "initial_prompt=Alice, Bob, Kubernetes" --form-string language=en \
  http://127.0.0.1:8787/v1/transcriptions          # 202 {"job_id": …, "status": "queued"}
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -F "file=@$TMPDIR/meeting.flac;type=audio/flac" --form-string to=a@example.org \
  http://127.0.0.1:8787/v1/transcriptions          # 422 unknown field 'to'
uv --directory /home/sandy/dev/earmark run earmark worker --once
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" http://127.0.0.1:8787/v1/jobs/<id>
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  "http://127.0.0.1:8787/v1/jobs/<id>/transcript?format=text"
curl -s -X DELETE -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  http://127.0.0.1:8787/v1/jobs/<id>               # 200, purged_at set
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  http://127.0.0.1:8787/v1/jobs/<id>/transcript    # 410
ls "$TMPDIR/data/outbox" 2>/dev/null               # nothing: no mail was composed
find "$TMPDIR/data" -type f                        # earmark.db (+ -wal/-shm) only
kill %1; rm -rf "$TMPDIR"
```

Check the worker's output for the absence of `meeting.flac`, the prompt and the transcript
text. Not verifiable here: a GPU transcription of a long file on the Spark (runbook), and
whether a remote ASR server honours `prompt`.

## 8. Deviations

### 8.1 Product choices P1–P8

As stated at the top. P1 sets a cap the brief left open; P2, P3, P4, P5 and P8 settle what the
brief left to the planner; P6 goes past the brief (database hygiene) because the client's data
is sensitive and it is cheap; P7 renames the brief's `id` to `job_id`.

### 8.2 The view drops `warnings`; the transcript keeps them

The brief's `/v1` job view lists no warnings. Transcript warnings are in the transcript's
`warnings`, as the brief's shape has them. Job-level warnings (notify failures) cannot occur
for a `/v1` job.

### 8.3 The `/v1` transcript has no `episode_key` or `speaker`

The brief's shape is `{job_id, language, duration_s, segments: [{start, end, text}],
warnings}`; step 33's view has two more fields. `/v1` follows the brief. Speakers arrive, if
ever, as an additive field.

### 8.4 No `fetch` stage and no `episode.json` for `/v1` jobs

The upload is already on disk, and nothing renders a note, so the runner writes only the
transcript for them. `job.stage` goes `resolve` → `transcribe`.

### 8.5 `JobResult.language` for every job

Added so the `/v1` view can show the language after a purge. The unversioned `JobView.result`
gains the field too: additive, and `earmark jobs --json` shows it.

### 8.6 The remote engine sends `prompt`

The brief researches faster-whisper only. Sending the OpenAI field keeps `/v1` meaningful with
`EARMARK_ASR_BACKEND=remote`, at the cost of three lines and a redaction.

### 8.7 `secure_delete` on every connection

A global connection setting, chosen over a purge-only one because earlier updates of the row
(heartbeats, stages) can leave old copies of it in freed space.

### 8.8 One step, with a fallback split

Planned as one implementer pass; section 3's phases give the 36a/36b seam if needed.

### 8.9 Three seams

The System section of `docs/ARCHITECTURE.md` is renamed "three parts and three seams", with
`/v1` as the third seam; its client (earshot) is outside the repo, as AMAP is.

### 8.10 `docs/ARCHITECTURE.md` changes made by this plan

- The opening and "System": three seams, the API row, `contract/` holding the `/v1` text, the
  repo layout entry `contract/api-v1.md` (step 36).
- "The service", "Decisions" (Scope, State, Requests rows), "Flow" (the `/v1` path).
- "Module layout": `v1_jobs.py`, `api/v1.py`.
- "Domain models": `DecodeOptions`, `V1UploadFields`, `decode_key`, the `Job` fields,
  `JobResult.language`.
- "Stage contracts" and "Runner and worker": the `Transcriber` options; the delivery-free
  path; notify never for `delivery = none`; the heartbeat finding.
- "Transcribing": per-request options, the profile, the per-options tree, the prompt's reach.
- "Jobs" and "Database identity": the `/v1` dedupe key, `mark_purged`, migration `0005_v1`,
  schema 5, `secure_delete`, the checkpoint.
- "Data layout": `transcripts/<decode_key>/`.
- "HTTP API": the four routes and a `/v1` subsection.
- "Security": the allowlist does not apply to `/v1`; the `/v1` leak rules; purge.
- "Testing": the `/v1` tests.

## 9. Out of scope

- Word timestamps, word and language probabilities (operator, 2026-11-28: later, additive).
- Cancelling a job; a delete-after-fetch flag.
- Other decode options (beam size, VAD, compute type, model) and URL sources under `/v1`.
- Turning batching on by default (`EARMARK_ASR_BATCH_SIZE` stays the operator's).
- A CLI for `/v1` (`earmark submit` and `earmark transcript` stay on the unversioned routes;
  `earmark transcript` of a `/v1` job says where to fetch it).
- Counting prompt tokens and warning when Whisper drops part of the prompt.
- Overwriting deleted files' blocks; purging backups.
- A send-rate or disk quota for `/v1` uploads (a leaked token could fill the disk, as it
  already could through `POST /transcriptions`).
