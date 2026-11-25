# Step 33: The transcript by job id

The brief is in `docs/PLAN.md`, "Step 33". It builds on step 04 (the API and its bearer
middleware), step 24 (jobs, sources, `JobView`), step 25 (the mail code) and step 32 (contract
2, YouTube). Step 34 (the CLI as an API client) calls this route next, so the route is shaped
for it: the 200 body is exactly what `earmark transcript` prints in local mode, and every
error is `{"detail": "<the message the CLI prints>"}`.

The step can be built as briefed. One acceptance line could not be met as first written;
the operator decided it, and the brief now says so:

- **Decision P1, decided by the operator on 2026-11-25: option A, keep contract 2. The
  note carries no `job_id`.** The brief's original acceptance
  says a re-share "serves the same transcript, with its own `job_id` in the note". Under
  contract 2 the `job_id` is in the instructions attachment only; the note has none, and
  adding one is a contract revision (contract 3, with new samples, checker and receiver
  changes). This plan keeps the contract unchanged. The re-share's own `job_id` is in the
  `json` format instead, and its note is byte-identical to the one mailed for it. Section 8.1
  has the details and what option B (contract 3) would involve.

The other choices are engineering ones, made here (section 8): the note is rendered per
request and never stored (D2), a job "has a transcript" when it is `done` or its stage is
`deliver` (D3), and the `json` format is a public view rather than the cached model (D4).

Rules for the implementer that are not about the code itself:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Leave `deploy/caddy/install-cert.sh` alone**, an untracked file of the operator's.
- **Placeholders only** in every file this step writes: addresses at `example.org`,
  `example.net` or `example.com`, `earmark.example.com` for the API's name, `127.0.0.1` for
  loopback, `$EARMARK_API_TOKEN` for the token. No real host name, LAN address or token.
- **`docs/ARCHITECTURE.md` is already updated** by this plan ("Module layout", "HTTP API" and
  its new `GET /jobs/{id}/transcript` paragraph, "Security", "Settled questions"). Check that
  the code matches it. Change it only if the code had to differ, and then say so.
- **Never copy a line of contract 1** into any Markdown file (`test_contract_text_exists_once`).
- In `README.md` and `docs/deploy.md` the removed-wording scan bans, among others,
  `\bdigest`, `embedding`, `\bentit(y|ies)\b`, `\bclaude\b` and `vault`. Write "sha256" or
  "hash".
- Use `TMPDIR` under `~/.cache/` for anything that writes temp files.

## 1. Outcome

- `GET /jobs/{job_id}/transcript?format=note|text|json`, behind the bearer token, serves a
  job's transcript from the cache under `data/episodes/<episode_key>/`:
  - `note` (the default) is the transcript note exactly as the mail code renders it for that
    job: the same bytes as the mailed note attachment, with that attachment's file name in
    `Content-Disposition`;
  - `text` is `transcript.txt`;
  - `json` is a public `TranscriptView` of the segments.
- Unknown job 404, no transcript yet (or failed or skipped before one) 409, cache gone 410,
  unknown format 422, unreadable cache file 500.
- A new module, `earmark/transcripts.py`, does the work for the route and for the new
  `earmark transcript <job_id> [--format note|text|json]` command, so both give the same bytes
  and the same messages.
- The route and the command read only the job row and the cache. They write nothing, start no
  stage, make no request, and never log content, the recipient or the instructions.
- The README and `docs/deploy.md` document the route and the command, with a `curl` example.

## 2. Findings

### 2.1 What a job's note is rendered from

Read in `pipeline/runner.py`, `mail/note.py`, `mail/message.py`, `artifacts.py`,
`sources.py` and `jobs.py` (as of commit `e9e5660`).

- `render_note(job, episode, transcript, *, first_shared_at)` is pure. Of the job it reads
  `source`, `url`, `utc_offset_min` and, for the instructions file only, `id`, `shared_at` and
  `instructions`. It never reads `to`. The note itself carries **no `job_id`**: its keys are
  `type, contract, source, origin, title, show, channel, date, published, duration, language,
  apple_url, source_url, guid, shared_at, transcript_sha256, tags`.
- The runner's inputs at delivery time, and where the route finds the same values:
  - `job`: `claim.job`, read from the DB by `_row_to_job`. The route's `store.get(job_id)` is
    the same row (delivery fields aside, which rendering ignores).
  - `episode`:
    - podcast and YouTube: the freshly resolved episode, written to `episode.json` just
      before (`write_episode`). The route reads `episode.json`.
    - upload: `audio_episode(job, job.content_sha256)`. The route builds the same.
    - URL: `audio_episode(job, sha256_file(audio))`. The sha256 is not on the job row, so the
      route takes it from `episode.json`'s guid (`sha256:<hex>`) and calls
      `audio_episode(job, hex)`. **It must not use `episode.json` as is**: for a content key it
      is rewritten by every job with the same bytes, so it holds the last job's title and
      possibly `source: upload`. `Pipeline.load_episode` today copies the title over
      `episode.json`, which happens to give the same note but keeps the other job's `source`.
  - `first_shared_at`: the return value of `record_share`, which is `shared.json`'s
    `first_shared_at` (or the job's `shared_at`, which it just wrote there). The route calls
    `read_share`, **never `record_share`, which writes**.
  - `transcript`: the cached transcript or the one just written to `transcript.json`. The
    route reads `transcript.json`.
- A prototype (a scratch script driving `tests.pipeline_env.make_env` with the fakes; nothing
  written to the repo) rebuilt the note from the cache for a podcast job, two URL jobs with the
  same bytes and different titles, and a YouTube job. Each was byte-identical to the note
  attachment decoded from the fake mailer's message. The JSON round trip of `Episode`,
  `ShareRecord` and `Transcript` loses nothing the note uses.
- The same prototype showed a drift case. A re-share whose `timestamp` was earlier than the
  first job's moved `shared.json`'s first share back. The first job's rebuilt note then
  differed from its mailed one (`shared_at`, and the date of the file name). The other drift
  cases follow from the code:
  - a later share of a podcast or video re-resolves it and rewrites `episode.json`;
  - a new ASR profile re-transcribes on the next share and rewrites `transcript.json`;
  - a contract revision changes the mail code. A job mailed as contract 1 before step 32 now
    renders as contract 2, which is what the orchestrator asked for ("the note this route
    serves is whatever the current mail code renders").

  The brief allows storing the note "if the planner finds a reason". These cases are rare,
  and storing would not fix the ASR one for `text` and `json`. So it is not stored (D2), and
  the cases are documented in `docs/ARCHITECTURE.md`.

### 2.2 When a job has a transcript

Read in `jobs.py` and `runner.py`.

- The runner sets the stage to `deliver` right after `_stage_work` returns the transcript,
  before composing. `fail`, `requeue_stale` and `release` change the status but **never the
  stage**, and `complete`/`complete_sent` leave it at `deliver`.
- So `job.stage == "deliver"` means a transcript was produced for this job in some attempt:
  - while composing or sending (running);
  - after a transient delivery failure (queued, waiting for a retry);
  - after a permanent delivery failure, including the attachment cap ("more than
    EARMARK_MAX_ATTACHMENT_MB ... allows; nothing was sent"), the no-longer-allowed recipient,
    and an SMTP 5xx (failed).
- This answers the brief's question: **a failed job can have a transcript**, when it failed at
  `deliver`. The route serves it. That is also the one way to get a transcript that is too large
  to mail.
- A job claimed for another attempt keeps `deliver` until its first `set_stage("resolve")`, a
  few statements later; then it is `resolve` again until it reaches `deliver`. A 409 during
  that window is correct ("running (stage: resolve)").
- Every `done` job has stage `deliver` today. The rule still accepts `done` on its own
  (D3), so that no future path through `complete` breaks it.

### 2.3 FastAPI and Starlette behaviour (checked, installed versions)

Installed: fastapi 0.165.0, starlette 1.10.0, pydantic 2.14.4, typer 0.29.2, click 8.5.1.
Checked with a scratch app under `TestClient`, using the repo's own `describe_validation_errors`
handler:

- `fmt: Annotated[Literal["note","text","json"], Query(alias="format")] = "note"`:
  - no parameter gives `note`;
  - `?format=xml`, `?format=NOTE` and `?format=` give 422 with
    `{"detail": "format: Input should be 'note', 'text' or 'json'"}`, so the existing handler
    never echoes the input;
  - `?format=text&format=json` takes the last value (`json`), and an unknown parameter
    (`?fmt=json`) is ignored, as on `GET /jobs`.
  - The alias keeps the Python name off the builtin `format` (ruff `A002`).
- `Response(content=bytes, media_type="text/markdown")` sends
  `text/markdown; charset=utf-8`, because Starlette adds the charset to `text/*`. Passing
  `"text/markdown; charset=utf-8"` explicitly leaves it as is.
  `media_type="application/json"` is sent without a charset (RFC 8259 defines none; JSON is
  UTF-8). A `Content-Disposition` header passes through unchanged.
- pydantic `model_dump_json(indent=2)` writes non-ASCII as raw UTF-8 and escapes control
  characters (`\u001b`).

### 2.4 The CLI's output (checked)

- Under typer's `CliRunner`, `sys.stdout.buffer.write(bytes)` lands in `result.stdout_bytes`,
  and `sys.stdout.isatty()` is `False`.
- **Dead end: `click.get_binary_stream("stdout")`** works, but it is deprecated in click 8.5 and
  raises a `DeprecationWarning`. Under the suite's `filterwarnings = error` that fails the test.
  Use `sys.stdout.buffer`.
- A `Literal` option (`--format`) works in typer 0.29, as `--status` already does. A bad value
  exits 2 with "Invalid value for '--format'".
- **`docker compose exec` allocates a TTY by default.** On a TTY the line discipline turns
  `\n` into `\r\n`, and the command's terminal filter (section 5.7) applies. The docs must say
  `docker compose exec -T api earmark transcript <job_id> > note.md`.

### 2.5 The repo, where this step touches it

- `api/routes.py` `build_router(intake, *, max_upload_bytes)` has no `ArtifactStore`. It gains
  a keyword `artifacts`, and the two other callers are updated: `tests/test_api.py`
  `test_routes_are_exactly_the_kept_ones` and `tests/test_docs.py`
  `test_readme_http_api_lists_every_route`.
- `tests/test_api.py::test_routes_are_exactly_the_kept_ones` pins the route set (and
  `len(router.routes) == 4`). `tests/test_docs.py::test_readme_http_api_lists_every_route`
  requires the README's "The HTTP API is …" paragraph to list every route.
  `test_readme_commands_cover_cli` requires a README row for every command.
- `FakeTranscriber(artifacts, segments=…)` takes fixed segments, so tests can put a sentinel
  phrase into the transcript. `FakeBackends` is a mutable dataclass, so a test can swap its
  transcriber before `make_env(tmp_path, fakes=…)`.
- The fake mail backend writes `data/outbox/<job_id>.eml` in the CLI tests' `fake_env`, which
  gives the CLI tests the mailed note to compare against.
- The `jobs` command opens the store with `open_job_store`, which creates the database if it is
  missing. `transcript` does the same here. Step 34 changes both (section 9).

No new dependency, setting or migration.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/models.py` | Add `TranscriptFormat = Literal["note", "text", "json"]`, next to `JobStatus`. The CLI imports it at module level, and `models` keeps the CLI's import path light. |
| `src/earmark/transcripts.py` | **New.** `read_transcript_file`, `TranscriptFile`, `TranscriptView`, `SegmentView`, `TranscriptUnavailable`, `has_transcript`, `episode_for_job`, and the message constants (section 4.2). It imports `artifacts`, `errors`, `models`, `sources` and `mail.note` only: no `fastapi`, `httpx2` or `pipeline`. |
| `src/earmark/api/routes.py` | `build_router(intake, *, artifacts, max_upload_bytes)`, the new route (section 4.3), and `JOB_NOT_FOUND` for both 404s. |
| `src/earmark/api/app.py` | `create_app` builds `ArtifactStore(settings.data_dir)` and passes it to `build_router`. |
| `src/earmark/pipeline/runner.py` | `Pipeline.load_episode` delegates to `transcripts.episode_for_job`, keeping its contract (None on a missing or unreadable cache). A URL job's episode is then rebuilt as the runner built it. |
| `src/earmark/cli.py` | The `transcript` command, `terminal_text`, and `_stdout_is_terminal` (section 4.4). |
| `tests/test_transcripts.py` | **New.** Unit tests of the module (section 6). |
| `tests/test_api_transcript.py` | **New.** The route: formats, states, headers, token, byte equality with the mail. |
| `tests/test_api.py` | Add the route to `test_missing_token_401`'s cases and to `test_routes_are_exactly_the_kept_ones` (5 routes); pass `artifacts=` to `build_router`. |
| `tests/test_docs.py` | Pass `artifacts=` to `build_router` in `test_readme_http_api_lists_every_route`. Nothing else. |
| `tests/test_cli.py` | The `transcript` command's tests. |
| `tests/test_leaks.py` | `test_transcript_route_leaks_nothing` and `test_unreadable_cache_logs_no_content`. |
| `tests/test_runner.py` | Extend `test_load_episode_per_source`: a URL episode keeps `source == "url"` when an upload of the same bytes rewrote `episode.json` (section 6.8). |
| `README.md` | The HTTP API paragraph lists `GET /jobs/{id}/transcript`. A commands row for `transcript <job_id> [--format]`. |
| `docs/deploy.md` | A new section 8.7, "A transcript without the email" (section 4.5). |
| `docs/ARCHITECTURE.md` | Done by this plan; see the rules above. |

`CLAUDE.md`, `docs/PLAN.md` and `docs/shortcut.md` do not change (the orchestrator updates the
status table).

## 4. Interfaces

### 4.1 `models.py`

```python
TranscriptFormat = Literal["note", "text", "json"]
```

### 4.2 `transcripts.py`

```python
"""A job's cached transcript as the mailed note, the plain text or the JSON view.

Reads the job and the files under episodes/<key>/ only: writes nothing, starts no stage,
makes no request, logs nothing. The API route and `earmark transcript` share it.
"""

TRANSCRIPT_FORMATS: Final[tuple[TranscriptFormat, ...]] = ("note", "text", "json")
NOTE_MEDIA_TYPE: Final = "text/markdown; charset=utf-8"
TEXT_MEDIA_TYPE: Final = "text/plain; charset=utf-8"
JSON_MEDIA_TYPE: Final = "application/json"
JOB_NOT_FOUND: Final = "job not found"

UnavailableReason = Literal["not_ready", "gone", "unreadable"]


class TranscriptUnavailable(Exception):  # noqa: N818  # an outcome, like SkipEpisode
    """No transcript to serve. ``detail`` is the message for the API and the CLI."""

    def __init__(self, reason: UnavailableReason, detail: str) -> None: ...

    reason: UnavailableReason
    detail: str


@dataclass(frozen=True, slots=True)
class TranscriptFile:
    format: TranscriptFormat
    data: bytes  # UTF-8
    media_type: str  # one of the three constants above
    filename: str | None  # "note": the note attachment's file name; else None


class SegmentView(BaseModel):  # frozen, extra="forbid"
    start: float
    end: float
    text: str
    speaker: str | None


class TranscriptView(BaseModel):  # frozen, extra="forbid"
    job_id: JobId
    episode_key: EpisodeKey
    language: str | None
    duration_s: float
    segments: tuple[SegmentView, ...]
    warnings: tuple[str, ...]


def has_transcript(job: Job) -> bool:
    """True when this job produced a transcript in some attempt."""
    return job.status == "done" or job.stage == "deliver"


def episode_for_job(job: Job, artifacts: ArtifactStore) -> Episode | None:
    """The Episode the runner rendered this job's note from, rebuilt from the cache.

    upload: audio_episode(job, job.content_sha256), no file read.
    url: audio_episode(job, <hex of episode.json's sha256: guid>).
    apple_podcasts, youtube: episode.json as stored.
    None when job.episode_key is None or episode.json is missing.
    Raises PermanentError when episode.json is corrupt (ArtifactStore's error), or when a URL
    job's episode.json has no sha256 guid or a key other than the job's.
    """


def read_transcript_file(
    job: Job, artifacts: ArtifactStore, fmt: TranscriptFormat
) -> TranscriptFile:
    """The job's transcript in ``fmt``. Raises TranscriptUnavailable."""
```

The messages, exactly (`{key}` is the job's episode key, `{file}` a file name such as
`transcript.json`):

| Constant or case | `reason` | `detail` |
|---|---|---|
| queued or running, no stage | `not_ready` | `this job has no transcript yet; it is {status}` |
| queued or running, with a stage | `not_ready` | `this job has no transcript yet; it is {status} (stage: {stage})` |
| failed, stage not `deliver` | `not_ready` | `this job has no transcript; it failed before the transcript was made` |
| skipped | `not_ready` | `this job has no transcript; it was skipped` |
| a file is missing | `gone` | `the cache of this job is gone: episodes/{key}/{file} is missing; share it again` |
| `has_transcript` but no `episode_key` | `gone` | `the cache of this job is gone: it has no episode key; share it again` |
| a file does not parse, or cannot be read | `unreadable` | `the cache of this job is unreadable: episodes/{key}/{file}; delete episodes/{key}/ and share it again` |

Put the templates in module constants (`NOT_YET`, `NOT_YET_AT_STAGE`, `FAILED_BEFORE`,
`SKIPPED`, `GONE`, `GONE_NO_KEY`, `UNREADABLE`) so tests and step 34 can import them.

`read_transcript_file`, in this order:

1. If not `has_transcript(job)`: `not_ready`, with the detail for its status.
2. If `job.episode_key is None`: `gone` (`GONE_NO_KEY`).
3. If not `artifacts.has_transcript(key)`: `gone`, `transcript.json`. Every format needs it,
   because its existence marks the transcript as cached (the runner writes it last).
4. `text`: `artifacts.transcript_text_path(key).read_bytes()`. `FileNotFoundError` is `gone`
   (`transcript.txt`), and any other `OSError` is `unreadable`. Return
   `TranscriptFile("text", data, TEXT_MEDIA_TYPE, None)`.
5. `artifacts.read_transcript(key)`. `None` (deleted since step 3) is `gone`; `PermanentError`
   or `OSError` is `unreadable` (`transcript.json`).
6. `json`: build `TranscriptView` from the job's id and the transcript's `episode_key`,
   `language`, `duration_s`, `segments` and `warnings`. Its bytes are
   `(view.model_dump_json(indent=2) + "\n").encode("utf-8")`. Return
   `TranscriptFile("json", data, JSON_MEDIA_TYPE, None)`.
7. `note`:
   - `episode_for_job(job, artifacts)`: `None` is `gone` (`episode.json`), and
     `PermanentError` or `OSError` is `unreadable` (`episode.json`).
   - `artifacts.read_share(key)`: `None` is `gone` (`shared.json`), and `PermanentError` or
     `OSError` is `unreadable` (`shared.json`).
   - `rendered = render_note(job, episode, transcript, first_shared_at=share.first_shared_at)`.
   - Return `TranscriptFile("note", rendered.note.data, NOTE_MEDIA_TYPE,
     rendered.note.filename)`.
   - The instructions attachment is computed by `render_note` and dropped. It is never
     returned.

Every `TranscriptUnavailable` raised while handling another exception is raised
`from None` (section 5.6).

### 4.3 The route (`api/routes.py`)

```python
def build_router(intake: Intake, *, artifacts: ArtifactStore, max_upload_bytes: int) -> APIRouter

@router.get("/jobs/{job_id}/transcript")
def get_transcript(
    job_id: str,
    fmt: Annotated[TranscriptFormat, Query(alias="format")] = "note",
) -> Response
```

- It is a sync `def` (the threadpool runs it), like `get_job`.
- `store.get(job_id)` is `None` (including any `job_id` that is not 32 lowercase hex): 404
  `{"detail": "job not found"}`. `get_job` uses the same constant. Nothing is logged.
- On `TranscriptUnavailable`, raise `HTTPException(status_code, detail=exc.detail)`, with
  `not_ready` 409, `gone` 410 and `unreadable` 500. Log one line, with no traceback:
  - `not_ready`, `gone`: INFO, `job %s: no transcript as %s: %s` with the id, the format and
    the reason.
  - `unreadable`: WARNING, `job %s: no transcript as %s: %s: %s` with the id, the format, the
    reason and the detail. The detail names a file and the key only.
- On success, log INFO `job %s: transcript served as %s` with the id and the format. Return
  `Response(content=f.data, media_type=f.media_type, headers=h)`, where `h` is:
  - `Cache-Control: no-store`, always;
  - for `note`, also `Content-Disposition: attachment; filename="<f.filename>"`.
- The file name is a contract slug, `YYYY-MM-DD-[a-z0-9-]+.md`. Before building the header,
  check it against `[0-9a-z][0-9a-z.-]*\.md` and raise `ValueError` if it does not match. This
  cannot happen: it guards the header against a future change of the slug rules.
- 401 comes from the existing middleware. A `sqlite3.Error` gives 503 through the existing
  handler, and an invalid `format` gives 422 through the existing validation handler. FastAPI
  validates the query before the handler runs, so a bad format is 422 even for an unknown
  job.
- The status codes as the HTTP surface:

| Status | When | Body |
|---|---|---|
| 200 | transcript available | the bytes; `Content-Type` per format |
| 401 | missing or wrong token | `{"detail": "missing or invalid API token"}` |
| 404 | no such job | `{"detail": "job not found"}` |
| 409 | `not_ready` | `{"detail": "<NOT_YET…, FAILED_BEFORE or SKIPPED>"}` |
| 410 | `gone` | `{"detail": "<GONE…>"}` |
| 422 | bad `format` | `{"detail": "format: Input should be 'note', 'text' or 'json'"}` |
| 500 | `unreadable` | `{"detail": "<UNREADABLE>"}` |
| 503 | job store error | `{"detail": "the job store is unavailable; try again shortly"}` |

### 4.4 The CLI (`cli.py`)

```python
@app.command("transcript")
def transcript_command(
    job_id: Annotated[str, typer.Argument(help="The job id that POST /transcriptions returned.")],
    fmt: Annotated[
        TranscriptFormat,
        typer.Option("--format", help="note (the mailed note; the default), text or json."),
    ] = "note",
) -> None:
    """Print a job's transcript from the cache, as GET /jobs/{id}/transcript does."""


def terminal_text(data: bytes) -> str:
    """UTF-8 bytes as text that cannot drive a terminal: every character that is not
    printable, except newline and tab, becomes '?'. Undecodable bytes become U+FFFD."""


def _stdout_is_terminal() -> bool:  # a seam for tests
    return sys.stdout.isatty()
```

The command, step by step:

1. `settings = _load_or_exit()`, then `configure_logging(settings.log_level,
   settings.secret_values())`.
2. Lazy imports: `ArtifactStore`, `open_job_store`, and from `transcripts`
   `JOB_NOT_FOUND`, `TranscriptUnavailable` and `read_transcript_file`.
3. `job = open_job_store(settings).get(job_id)`. A `DatabaseSetupError`, `sqlite3.Error` or
   `OSError` prints `earmark transcript: cannot open the job store: {exc}` on stderr and
   exits 1, as `jobs` does.
4. `job is None` prints `earmark transcript: job not found` on stderr and exits 1.
5. `read_transcript_file(job, ArtifactStore(settings.data_dir), fmt)`. A
   `TranscriptUnavailable` prints `earmark transcript: {exc.detail}` on stderr and exits 1,
   for every reason.
6. Output:
   - if `_stdout_is_terminal()`: `typer.echo(terminal_text(f.data), nl=False)`;
   - else `sys.stdout.flush()`, `sys.stdout.buffer.write(f.data)`, `sys.stdout.buffer.flush()`.
     These are the exact bytes, with no newline added (every format already ends in one).
7. Nothing else goes to stdout, and nothing is logged. A bad `--format` exits 2 (typer).

Exit codes: 0 success, 1 no transcript or no job or store error, 2 usage or config. Step 34
keeps these for client mode.

### 4.5 Docs

- **`README.md`**:
  - the paragraph becomes "The HTTP API is `POST /transcriptions`, `GET /jobs/{id}`,
    `GET /jobs/{id}/transcript`, `GET /jobs`, and `GET|HEAD /healthz`." Keep `?format=`
    outside the backticked span, because the doc test parses it;
  - add the row `| \`transcript <job_id> [--format]\` | Print a job's transcript from the cache:
    the note as mailed (the default), \`text\` or \`json\`. |` after `jobs`.
- **`docs/deploy.md`, a new "### 8.7 A transcript without the email"**, after 8.6:
  - what the three formats are, and that `note` is the mailed attachment while the episode's
    cache is unchanged (with a link to "HTTP API" in ARCHITECTURE for the exceptions);
  - from the Mac or the phone's network:
    ```bash
    curl -fsS -OJ -H "Authorization: Bearer $EARMARK_API_TOKEN" \
      "https://earmark.example.com/jobs/<job_id>/transcript"
    curl -fsS -H "Authorization: Bearer $EARMARK_API_TOKEN" \
      "https://earmark.example.com/jobs/<job_id>/transcript?format=text" -o transcript.txt
    ```
    `-OJ` saves under the note's own file name;
  - on the Spark, inside the container:
    `docker compose exec -T api earmark transcript <job_id> > note.md`. Say why `-T` matters:
    without it compose allocates a terminal, which turns line endings into CRLF and makes the
    command filter control characters;
  - the answers: 409 means not transcribed yet, or failed or skipped before; 410 means the
    cache was deleted (share it again); and a job that failed on `EARMARK_MAX_ATTACHMENT_MB`
    still has its transcript here.

## 5. Behaviour and edge cases

### 5.1 It reads, and nothing else

- The route and the command read the job row and up to four files under
  `episodes/<key>/`. They never call `record_share`, `write_*`, a stage or the factory.
  `ArtifactStore` read methods create no directory.
- SQLite in WAL mode may touch `earmark.db-shm` on read. The "writes nothing" test excludes
  the `earmark.db*` files, and checks the job row instead (section 6.3).

### 5.2 A note per job, from the cache

- The note is rendered on every request and never stored. Rendering is deterministic: no
  clock, no randomness. Two requests give the same bytes.
- **An upload or URL job's episode comes from `audio_episode(job, sha256)`, never from
  `episode.json` as is.** For a URL job the sha256 is read from `episode.json`'s guid.
  `episode.json` must have `source in AUDIO_SOURCES`, a `sha256:` guid, and a key equal to
  `job.episode_key`; otherwise it is `unreadable`.
- The note differs from the mailed one only in the documented cases (section 2.1, and
  "HTTP API" in ARCHITECTURE). There is no attempt to detect that.

### 5.3 States

- `has_transcript` decides alone. A job that failed at `resolve` or `fetch`, or was skipped,
  is 409 even when another job has since cached the same episode. The cache is the episode's,
  and this job never reached it.
- A running job at stage `deliver` is served. Its note matches the message about to be sent.
- A queued job at stage `deliver` (a transient delivery failure) is served (D3).
- The brief's "queued" case means a job that has not run yet, or whose last attempt stopped
  before `deliver`. Those are 409.

### 5.4 Formats

- `text` is the bytes of `transcript.txt`, read after checking that `transcript.json` exists.
  It is not regenerated from the JSON.
- `json` is `TranscriptView`. It never carries `model`, `asr_profile`, the guid, `to` or
  `instructions`. `job_id` is the requested job's, so a re-share's view has its own id while
  its segments equal the first job's.
- `note` returns only the note attachment.
- All bodies are UTF-8. JSON escapes control characters. The note drops C0 controls from
  paragraphs and every Cc/Cf character from frontmatter strings (the mail code's rules).
  `transcript.txt` is the cached text as is.

### 5.5 Headers

- `Cache-Control: no-store` on every 200: a transcript is private and passes through Caddy.
- `Content-Disposition` only for `note`. `text` and `json` have none.
- No `ETag`, no `HEAD` route.

### 5.6 What must not leak

- `to` and `instructions` are never read by this code, apart from `render_note` building the
  instructions attachment, which is dropped.
- Log lines carry the job id, the format, the reason code and, for `unreadable`, the detail
  (a file name and the key). Never transcript text, titles, the recipient or the instructions.
- **No traceback for a corrupt cache file.** `ArtifactStore._read_json` raises
  `PermanentError("corrupt artifact: <absolute path>") from ValidationError`, and pydantic's
  `ValidationError` text includes the input value, which can be transcript text. So:
  - `TranscriptUnavailable` is raised `from None`;
  - the route logs without `exc_info`;
  - the detail names `episodes/<key>/<file>`, not the absolute path.
- The 409, 410 and 500 details contain no title, recipient, instructions or job error text.
  The job's error stays on `GET /jobs/{id}`, scrubbed as before.

### 5.7 The command at a terminal

- Piped or redirected, the command writes the exact bytes, so
  `earmark transcript <id> > note.md` reproduces the attachment.
- At a terminal, `terminal_text` replaces every non-printable character except `\n` and `\t`
  with `?`. This is the rule `_clean` already applies to stored text, extended to keep line
  breaks. It matters for `text`, whose cached segments could hold an escape sequence from a
  remote ASR server. The note and the JSON cannot carry one.

### 5.8 `Pipeline.load_episode`

- It becomes:

  ```python
  try:
      return episode_for_job(job, self._artifacts)
  except (PermanentError, OSError):
      return None
  ```

- Its existing tests keep passing: an upload's title is from its own name, and a URL's title
  is from its own job. The one change: a URL job's episode now has `source == "url"` even when
  an upload of the same bytes wrote `episode.json` last. The notifier sees that episode for
  stale jobs.

## 6. Tests

Fixtures: none recorded. Everything runs on the fakes through
`tests.pipeline_env.make_env(tmp_path)` and a `TestClient` on
`create_app(settings, store=env.jobs)`, with `settings.data_dir = tmp_path / "data"` (as in
`tests/test_e2e.py`). The mailed note comes from
`tests.mail_helpers.parse_mail(env.fakes.mailer.calls[i].data).note`. For sentinel transcript
text, replace `fakes.transcriber` with
`FakeTranscriber(fakes.artifacts, segments=[Segment(start=0, end=5, text="SPOKEN-TOKEN-6e1f …")])`
before `make_env(tmp_path, fakes=fakes)`. Timestamps go in the request body
(`"timestamp": "2026-01-01T09:00:00-08:00"`).

### 6.1 Brief: "Tests cover each format … and that the `note` format equals the note attachment of the job's message (from the fake mailer) byte for byte."

`tests/test_api_transcript.py`:

- `test_note_equals_mailed_attachment[apple_podcasts|youtube|url|upload]`: POST (JSON for the
  three links, multipart for the upload), `worker.run_once()`, then `GET …/transcript`. The
  body `==` the mailed note bytes. `Content-Disposition` `==`
  `f'attachment; filename="{parsed.note_name}"'`. `Content-Type` `==`
  `text/markdown; charset=utf-8`.
- `test_note_is_the_default_format`: no `format` gives the same bytes as `?format=note`.
- `test_url_notes_follow_their_own_job`: two URL jobs with the same fake payload, different
  URLs and titles. Each job's note equals its own mailed note, so the second job's
  `episode.json` title does not leak into the first.
- `test_text_is_transcript_txt`: the body `==` `episodes/<key>/transcript.txt` bytes, and
  `Content-Type` `==` `text/plain; charset=utf-8`, with no `Content-Disposition`.
- `test_json_view_shape`: `Content-Type` `==` `application/json`. The body ends in `\n` and
  parses to exactly the keys `job_id, episode_key, language, duration_s, segments, warnings`.
  Each segment has exactly `start, end, text, speaker`. The values equal the cached
  `Transcript`'s, and `model` and `asr_profile` are absent.
- `test_bodies_are_utf8`: a podcast whose fake episode has a non-ASCII title (pass
  `FakeResolver({CANON: make_episode(...)})`-style episodes) and a segment `"Grüße"`. The note
  and the text contain the UTF-8 bytes. The JSON contains `"Grüße"` raw, not `ü`.
- `test_every_200_is_no_store[note|text|json]`.
- `test_note_is_deterministic`: two GETs, with `env.clock.advance(3600)` between them, give
  equal bytes.

### 6.2 Brief: "… each state …"

`tests/test_api_transcript.py`; each asserts the status code and the exact `detail` from the
module's constants:

- `test_unknown_job_404[aaaa…(32)|not-hex|"A"*32]`: `{"detail": "job not found"}`.
- `test_queued_job_409`: posted, not run. Detail `this job has no transcript yet; it is
  queued`.
- `test_running_job_before_deliver_409`: `claim = env.jobs.claim_next()`,
  `env.jobs.set_stage(claim, "fetch")`. Detail `…; it is running (stage: fetch)`.
- `test_running_job_at_deliver_is_served`: run job A to done. Post a re-share B (new
  instructions), claim it, `set_episode_key(claim, key)`, `set_stage(claim, "deliver")`. B's
  note is 200 and equals A's mailed note (same tz, later share).
- `test_queued_after_transient_delivery_failure_is_served`:
  `env.fakes.mailer.fail_next(TransientError("smtp down"))`, run once. The job is `queued` at
  stage `deliver`, and the note is 200. Advance the clock past the retry delay and run again:
  the job is `done`, and the mailed note equals the bytes served before.
- `test_failed_before_transcript_409`: `env.fakes.resolver.fail_next(PermanentError("gone"))`.
  The job is `failed` at stage `resolve`. Detail `this job has no transcript; it failed before
  the transcript was made`.
- `test_failed_job_409_even_when_episode_cached_since`: job A done; re-share B with
  `resolver.fail_next(PermanentError(...))`. B is 409 although A's cache exists.
- `test_skipped_job_409`: `resolver.fail_next(SkipEpisode("subscriber-only"))`. Detail `this
  job has no transcript; it was skipped`.
- `test_failed_at_delivery_is_served[note|text|json]`:
  `mailer.fail_next(PermanentError("rejected"))`. The job is `failed` at `deliver`, and every
  format is 200.
- `test_attachment_cap_failure_is_served`: `make_env(tmp_path, max_attachment_bytes=100)`.
  The job fails with the `EARMARK_MAX_ATTACHMENT_MB` message, the note is 200, and
  `len(body) > 100`.
- `test_missing_transcript_json_410[note|text|json]`: after done, delete
  `transcript.json`. Detail `the cache of this job is gone: episodes/<key>/transcript.json is
  missing; share it again`.
- `test_missing_transcript_txt_410_for_text_only`: delete `transcript.txt`. `text` is 410
  naming it; `note` and `json` are 200.
- `test_missing_episode_json_410_for_note_only[apple_podcasts|youtube|url]`: delete
  `episode.json`. `note` is 410 naming it; `text` and `json` are 200.
- `test_upload_note_needs_no_episode_json`: an upload job whose `episode.json` was deleted
  still serves its note, equal to the mailed one.
- `test_missing_shared_json_410_for_note`.
- `test_whole_episode_dir_deleted_410`: `shutil.rmtree(episodes/<key>)`. All formats are
  410.
- `test_corrupt_transcript_json_500`: overwrite with `b'{"SPOKEN-TOKEN-6e1f": 1}'`. `note`
  and `json` are 500 with detail `the cache of this job is unreadable:
  episodes/<key>/transcript.json; delete episodes/<key>/ and share it again`. `text` is 200
  with the bytes of `transcript.txt`, because `text` checks that `transcript.json` exists but
  does not parse it (step 4 in section 4.2).
- `test_corrupt_episode_json_500_for_note`.
- `test_unknown_format_422[xml|NOTE|""]`: detail `format: Input should be 'note', 'text' or
  'json'`, and the body does not contain the bad value (for `xml`).

### 6.3 Brief: "the route reads only the cache, and never starts work"

- `test_route_starts_no_work` (`tests/test_api_transcript.py`): for a queued job and for a
  done job, GET every format. Then:
  - the fakes' call lists are unchanged (`resolver.calls`, `fetcher.calls`, `youtube.calls`,
    `transcriber.calls`, `mailer.calls`, `notifier.calls`);
  - `env.jobs.get(id)` is equal (`==`) before and after;
  - the set of `(path, size, mtime_ns)` under `tmp_path / "data"`, excluding `earmark.db*`, is
    unchanged;
  - for the queued job, `data/episodes` is not created.

### 6.4 Brief: "… and the token check …"

- `tests/test_api.py::test_missing_token_401` gains `("GET", f"/jobs/{'a' * 32}/transcript")`.
- `test_transcript_wrong_token_401` (`tests/test_api_transcript.py`): a done job with
  `Authorization: Bearer nope` gives 401 and no transcript bytes in the body.
- `tests/test_api.py::test_routes_are_exactly_the_kept_ones` lists
  `("/jobs/{job_id}/transcript", frozenset({"GET"}))`, with `len(router.routes) == 5`.

### 6.5 Brief: "A re-share job (cached transcript, new instructions) serves the same transcript, with its own `job_id` in the note."

As section 8.1 explains, the note has no `job_id`. The test proves the rest, and puts the
`job_id` check on the JSON:

- `test_reshare_serves_the_same_transcript` (`tests/test_api_transcript.py`):
  1. Job A is a podcast with instructions "first" and timestamp `…T09:00:00-08:00`. Run it.
  2. Job B is the same URL with instructions "second" and a timestamp an hour later in the
     same offset. Run it.
  3. Then:
     - `fakes.transcriber.computed == 1`;
     - B's note equals B's mailed note, and equals A's note;
     - B's `text` equals A's;
     - B's JSON `job_id == B.id`, A's JSON `job_id == A.id`, and their `segments` are equal;
     - A's note still equals A's mailed note.
- `test_earlier_timestamp_reshare_moves_the_note` (documents D2): as above, but B's timestamp
  is earlier than A's. A's served note now differs from A's mailed note in `shared_at` only
  (compare the parsed frontmatter, `tests.mail_helpers.split_file`). It equals B's mailed
  note. This pins the documented drift, so a change to it is deliberate.

### 6.6 Brief: "No test or log line shows the recipient, the instructions or transcript text."

`tests/test_leaks.py`, with the existing `log_stream` fixture (DEBUG):

- `test_transcript_route_leaks_nothing`:
  - Requests use `to=SECRET_TO`, `instructions=INSTRUCTIONS` and a transcriber whose segment
    is `SPOKEN-TOKEN-6e1f and more words`.
  - Run a podcast job and an upload job to done, plus one failing at `fetch`.
  - Record `log_stream.tell()`, then GET every format for each job, and for an unknown id and
    `?format=xml`.
  - In the log text written after the mark: the route's lines are present (`transcript
    served as note`), and `SPOKEN-TOKEN`, `INSTR-TOKEN`, `SECRET_TO` and `MASKED_TO` are all
    absent.
  - Every response body (200 and error) contains neither `INSTR-TOKEN` nor `SECRET_TO`.
  - The error bodies do not contain `SPOKEN-TOKEN`.
- `test_unreadable_cache_logs_no_content`: write a `transcript.json` holding
  `"SPOKEN-TOKEN-6e1f"` as invalid content, then GET `note`. The response is 500, and neither
  the log text nor the body contains `SPOKEN-TOKEN`, `Traceback` or `input_value`.

Tests compare against the sentinels by name and never print them. That is what "no test
shows" can mean here: no assertion message embeds them. Use `assert "X" not in text`, never
`assert text == …` on a body holding the recipient.

### 6.7 Brief: "`make check` passes."

`make check`.

### 6.8 Criteria this plan adds

- **The CLI** (`tests/test_cli.py`, with `fake_env`):
  - `test_transcript_command_note_equals_mail`: `submit` with `--instructions`, then
    `worker --once`, then `transcript <id>`. Exit 0, and `stdout_bytes` equals the note parsed
    from `data/outbox/<id>.eml`.
  - `test_transcript_command_formats[text|json]`: `text` equals `transcript.txt`; `json`
    parses to the view with `job_id == id`.
  - `test_transcript_command_matches_api[note|text|json]`: the same bytes as a `TestClient` on
    `create_app(Settings(_env_file=None, api_token=SecretStr("t"), data_dir=fake_env / "data",
    allowed_recipients=(TO,)))` serves. This is the property step 34 relies on.
  - `test_transcript_command_unknown_job`: exit 1, stderr `earmark transcript: job not
    found\n`, stdout empty.
  - `test_transcript_command_not_ready`: submitted, not run. Exit 1, and stderr is the
    `NOT_YET` detail for `queued`.
  - `test_transcript_command_gone`: delete `transcript.json`. Exit 1, and stderr names it.
  - `test_transcript_command_bad_format`: `--format pdf` exits 2.
  - `test_terminal_text`: `terminal_text(b"a\x1b[31mb\r\n\tc\xc3\xa9\xff")` is
    `"a?[31mb?\n\tcé�"`.
  - `test_transcript_command_at_a_terminal`: overwrite `transcript.txt` with
    `b"[00:00:00] hi \x1b[2J there\n"`, and monkeypatch `earmark.cli._stdout_is_terminal` to
    return `True`. `--format text` prints `"[00:00:00] hi ?[2J there\n"`. Without the patch it
    prints the raw bytes.
  - The README row is covered by `test_readme_commands_cover_cli`.
- **The module** (`tests/test_transcripts.py`):
  - `test_has_transcript_table`: every `(status, stage)` pair from `JobStatus × (None,
    *Stage)` against the rule `status == "done" or stage == "deliver"`. Build the jobs with
    `make_delivery_job().model_copy(update=…)`.
  - `test_episode_for_job_upload_reads_nothing`: an `ArtifactStore` on an empty directory
    returns `audio_episode(job, sha)`, and the directory stays empty.
  - `test_episode_for_job_url_rebuilds_from_guid`: `episode.json` written by an upload of the
    same sha (`source="upload"`, other title). The result equals `audio_episode(url_job,
    sha)` (`source == "url"`, the URL job's title).
  - `test_episode_for_job_url_rejects_non_sha_guid`: `PermanentError`.
  - `test_read_transcript_file_creates_nothing`: a done-shaped job whose cache is absent. It
    gets `gone`, and `data/episodes` does not exist afterwards.
  - `test_unavailable_is_raised_from_none`: for a corrupt `transcript.json`,
    `exc.__cause__ is None` and `exc.__suppress_context__ is True`.
  - `test_json_bytes_format`: indent 2, a trailing `\n`, and non-ASCII raw.
- **The runner** (`tests/test_runner.py::test_load_episode_per_source`, extended): after an
  upload and a URL job with the same bytes, `load_episode(url_job).source == "url"`. Its
  existing title assertions still hold.
- **The docs**: `test_readme_http_api_lists_every_route` (updated caller) and the existing
  placeholder, anchor and removed-wording tests cover the README, `deploy.md` and this plan.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
make check
uv run pytest tests/test_api_transcript.py tests/test_transcripts.py -q
uv run pytest tests/test_cli.py -k transcript -q
uv run pytest tests/test_leaks.py -k "transcript or unreadable" -q
uv run pytest tests/test_api.py tests/test_docs.py tests/test_runner.py -q
git status --short   # only the files in section 3 (and deploy/caddy/install-cert.sh, untracked, untouched)
```

A local smoke with the fake backends, which needs no network: it uses loopback only, in a
temporary directory.

```bash
demo=$(mktemp -d)
export EARMARK_API_TOKEN=demo-token EARMARK_ALLOWED_RECIPIENTS=you@example.org
export EARMARK_DATA_DIR="$demo/data"
export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_YOUTUBE_BACKEND=fake \
  EARMARK_ASR_BACKEND=fake EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
uv run earmark serve &
curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
id=$(curl -s http://127.0.0.1:8787/transcriptions -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "to": "you@example.org", "instructions": "q"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')
curl -s -i "http://127.0.0.1:8787/jobs/$id/transcript" -H "Authorization: Bearer $EARMARK_API_TOKEN"   # 409 queued
uv run earmark worker --once
curl -s -D - -o "$demo/note.md" "http://127.0.0.1:8787/jobs/$id/transcript" -H "Authorization: Bearer $EARMARK_API_TOKEN"
curl -s "http://127.0.0.1:8787/jobs/$id/transcript?format=json" -H "Authorization: Bearer $EARMARK_API_TOKEN"
curl -s -i "http://127.0.0.1:8787/jobs/$id/transcript?format=xml" -H "Authorization: Bearer $EARMARK_API_TOKEN"   # 422
curl -s -i "http://127.0.0.1:8787/jobs/$id/transcript"   # 401
uv run earmark transcript "$id" > "$demo/cli-note.md"
cmp "$demo/note.md" "$demo/cli-note.md"
uv run python - "$demo/data/outbox/$id.eml" "$demo/note.md" <<'EOF'
import email, email.policy, sys
msg = email.message_from_binary_file(open(sys.argv[1], "rb"), policy=email.policy.default)
note = next(msg.iter_attachments()).get_payload(decode=True)
print("note matches mail:", note == open(sys.argv[2], "rb").read())
EOF
kill %1; wait
```

Expected:

- `healthz` is ok, and the POST answers with a job id.
- The first transcript GET is 409 `this job has no transcript yet; it is queued`.
- After the worker run, the note GET is 200 with `content-type: text/markdown;
  charset=utf-8`, `content-disposition: attachment; filename="…-fake-channel-fake-video-….md"`
  and `cache-control: no-store`.
- The JSON view has the job id. `?format=xml` is 422, and no token is 401.
- `cmp` prints nothing, and the script prints `note matches mail: True`.

Not verifiable in the sandbox, and to be reported as such: the route through Caddy and TLS on
the Spark, and `docker compose exec -T api earmark transcript …` in the container. Both are
operator checks; no new runbook phase is added.

## 8. Deviations and decisions

### 8.1 P1: no `job_id` in the note (deviates from the brief's acceptance)

- **The finding.** Under contract 2 the note's frontmatter has no `job_id`. It is in the
  instructions attachment, which this route must not serve, because it holds the
  instructions. A re-share's note is therefore byte-identical to the first job's when they
  share a time zone and the re-share is later.
- **Option A (built):** keep contract 2. The re-share's own `job_id` is in its JSON view, and
  its note equals the note mailed for it. `Content-Disposition` names the same file as the
  mail.
- **Option B (not built):** add `job_id` to the note. That is a contract revision
  (`contract: 3`):
  - a new `contract/mail-v3.md` and `contract/samples/v3/`;
  - checker and receiver changes, and a plugin version;
  - a new `transcript_sha256` rule question (the key would sit outside the hashed body, so
    re-share detection would survive).

  It belongs in its own step, if the operator wants it.

### 8.2 D2: the note is rendered per request, not stored

- The brief prefers rendering. Storing would freeze the mailed bytes against the four drift
  cases in section 2.1. It would also cost a file per job with no cleanup, and it would not
  freeze `text` or `json` against a re-transcription. The drift cases are rare and
  documented, and the orchestrator asked for the current mail code's rendering.
- If exact copies are wanted later, the smallest fix is to write the note next to the
  outbox, keyed by job id, at compose time. Not now.

### 8.3 D3: "has a transcript" is `done` or stage `deliver`

- The brief lists "queued" among the 409 states. A job queued for another delivery attempt
  already has its transcript, so it is served. Its stage says so (section 2.2).
- A failed job at stage `deliver` is served, as the brief asks ("serves it if so").
- A failed job at any earlier stage is 409, even when the episode is cached by another job.

### 8.4 D4: the JSON format is a public `TranscriptView`

- The cached `Transcript` carries `model` and `asr_profile`, which can name a local model path
  and the operator's ASR options. Its shape is also a cache format that may change.
- The view carries the job's id, the episode key, the language, the duration, the segments
  (`start, end, text, speaker`) and the warnings. It is indented, so a `curl` is readable, and
  the CLI prints the same bytes.

### 8.5 Smaller choices

- **500 for an unreadable cache file.** The brief lists no such state. It is a server-side
  fault, distinct from 410 (gone) and 409 (not ready).
- **`Cache-Control: no-store`** on the route's 200s only. The other routes are unchanged.
- **The JSON content type** is `application/json`, without a charset parameter (RFC 8259).
  The body is UTF-8, and the `text/*` types carry `charset=utf-8`.
- **`build_router` gains a required `artifacts` keyword.** Two test callers are updated.
- **`TranscriptFormat` lives in `models.py`**, so the CLI can annotate `--format` at import
  time without importing the mail code.
- **`Pipeline.load_episode` delegates to `episode_for_job`.** There is one definition of "the
  episode a job was rendered from". The only behaviour change is a URL job's `source`
  (section 5.8).
- **The terminal filter in `earmark transcript`** (section 5.7). Exact bytes when piped.

### 8.6 `docs/ARCHITECTURE.md` changes made by this plan

- "Module layout": `transcripts.py`.
- "HTTP API": the table row, and a `GET /jobs/{id}/transcript` paragraph (what it reads, when
  a job has a transcript, formats and headers, the cache states, the drift cases, logs).
- "Security": "Leak rules for the transcript route".
- "Settled questions": "Settled by step 33".

## 9. Out of scope

- **Client mode** (`EARMARK_API_URL`), and the CLI calling this route: step 34. This step
  makes that easy: the route's 200 body is the CLI's stdout, its `detail` is the CLI's
  message, and the exit codes are fixed.
- **Not creating the database or the data directory** from a read-only command: step 34.
  `earmark transcript` here opens the store like `earmark jobs` does today.
- **Storing the mailed note**, and a `job_id` in the note (contract 3); see 8.1 and 8.2.
- The instructions attachment, the whole `.eml`, or a list of a job's files over the API.
- `HEAD`, `ETag`, range requests, and a `--output` option.
- A runbook phase for the route on the Spark.
- Changing the attachment-cap error message to point at this route.
- The Shortcut (step 31).
