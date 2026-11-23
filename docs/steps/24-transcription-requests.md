# Step 24: Transcription requests: audio upload or URL, destination, instructions

The step can be built as briefed. Section 8 lists the choices this plan makes where the brief
is silent or open. Three of them change user-visible behaviour, and the user should know about
them: the dedupe window covers `done` jobs only (8.2), `EARMARK_KEEP_AUDIO` does not keep
uploaded or downloaded audio (8.3), and doctor fails when the allowlist is unset (8.9).

## 1. Outcome

`POST /transcriptions` replaces `POST /ingest`. It takes either JSON `{url, to,
instructions?, title?, timestamp?}` or a `multipart/form-data` upload with a `file` part and
the same text fields. Every request names one destination address. That address is checked
against `EARMARK_ALLOWED_RECIPIENTS`, and an unset allowlist refuses every request.

- **Podcasts.** An Apple Podcasts link goes through the resolver as before.
- **Direct URLs.** Any other http(s) URL is downloaded by the worker through the guarded
  fetch. It is hashed, and it is transcribed only if no transcript exists for its bytes.
- **Uploads.** The file streams to `data/uploads/` under an injectable byte cap. It is hashed
  while it streams, and it is never spooled into memory or the system temp dir. Uploads and
  direct URLs share one content key, the same one `earmark transcribe` uses (`file_key`).
- **Retention.** Their audio is deleted when the job ends.

The transcript cache records the ASR backend, model and options. A transcript made with
another profile is a miss.

- **Dedupe.** A repeat of the same source, `to` and `instructions` returns the active job, or
  a job that finished `done` within `EARMARK_DEDUPE_WINDOW_S`.
- **Job views.** `GET /jobs` and `GET /jobs/{id}` return a view model, never the raw `Job`.
- **The CLI.** `earmark submit` takes a URL or a file, with `--to`, `--instructions` and
  `--title`.
- **Leak tests.** Instructions, addresses and direct URLs are kept out of logs and pushes,
  and a test proves each rule.

## 2. Findings

What was checked, how, and what it means for the build. Versions are the ones in `uv.lock`
unless stated otherwise.

### 2.1 Starlette's form parser cannot be used for uploads

Read in `.venv/lib/python3.13/site-packages/starlette/formparsers.py` (Starlette 1.10.0):

- `MultiPartParser.on_headers_finished` gives every file part a
  `SpooledTemporaryFile(max_size=self.spool_max_size)`. `spool_max_size` is a class
  attribute of 1 MiB, and no `dir=` is passed.
- So every upload is held in memory up to 1 MiB. Past that it rolls over into the system
  temp dir (`tempfile.gettempdir()`).
- `request.form()` and FastAPI's `File`/`UploadFile` parameters both go through this parser.

Both break the brief ("never the system temp dir", "nothing is held in memory"). **Do not use
`request.form()`, `UploadFile`, `File(...)` or `Form(...)`.** The route reads
`request.stream()` itself and feeds python-multipart's streaming `MultipartParser` (2.3),
the same parser Starlette uses underneath.

### 2.2 FastAPI's body limit is per route and static

Read in `fastapi/_body_limit.py` and `starlette/middleware/body_limit.py` (FastAPI 0.165.0):

- `max_body_size=` on a route wraps it in `RequestBodyLimitMiddleware`.
- That middleware refuses a request whose `Content-Length` is over the limit before it is
  read, counts the bytes it receives, and answers 413 with the plain text `Content Too Large`.
- The limit is one number per route. It cannot depend on the content type, so it cannot
  give JSON 64 KiB and multipart `EARMARK_MAX_UPLOAD_MB`.

The new route sets **no** `max_body_size` and enforces both caps itself (5.3, 5.4). Its 413s
are JSON `{"detail": …}` like every other error.

### 2.3 python-multipart: the new dependency

- **Version.** The latest release on PyPI (JSON API, 2026-11-23) is `0.0.34`, released
  2026-11-16, `requires_python >=3.11`. It ships only a pure-Python wheel
  (`python_multipart-0.0.34-py3-none-any.whl`), so it installs on linux/aarch64. It has no
  dependencies and ships `py.typed`.
- **How it was checked.** It was installed into a scratch venv (deleted afterwards), and a
  prototype parsed real bodies in 7000-byte writes. The changelog for 0.0.33–0.0.34 lists
  header limits, padding handling and speedups, and no API change to `MultipartParser`.
- **Add it** with `uv add 'python-multipart>=0.0.34'`. That updates `pyproject.toml` and
  `uv.lock`.

**Why this package.**

- It is the parser Starlette and FastAPI already integrate, and is maintained by Starlette's
  maintainer.
- Its `MultipartParser` is a push parser with callbacks, and it buffers no part data beyond
  the boundary look-behind.
- The alternatives were rejected:
  - `streaming-form-data` is a Cython extension, so it needs wheels per platform.
  - `multipart` (defnull) is a second dependency for the same job.
  - A hand-written parser is a security surface for no benefit.

**Behaviour found with the prototype**, which the implementer must account for:

- **Constructor.** `MultipartParser(boundary: bytes, callbacks: MultipartCallbacks,
  max_size=inf, *, max_header_count=8, max_header_size=4224)`.
  - A boundary over 256 bytes raises `FormParserError` in the constructor.
  - The `MultipartCallbacks` TypedDict is defined under `if TYPE_CHECKING:` in
    `python_multipart/multipart.py`. Import it only for typing.
- **Callbacks used.** `on_part_begin`, `on_header_field(data, start, end)`,
  `on_header_value(data, start, end)`, `on_header_end`, `on_headers_finished`,
  `on_part_data(data, start, end)`, `on_part_end`, `on_end`.
  - Header field and value bytes can arrive in pieces, so concatenate them until
    `on_header_end`.
  - `python_multipart.multipart.parse_options_header(value)` turns a `Content-Disposition`
    or `Content-Type` value into `(type_bytes, {b"name": …, b"filename": …})`. The
    `filename` bytes are the raw header bytes (UTF-8 from real clients) carried as latin-1.
  - It ignores `filename*=`.
- **A single `write()` can span several parts.** In the prototype, attributing pending data
  to "the current part" after `write()` returned gave a 4 KiB text part the audio's bytes.
  The `on_part_data` callback must record `(part, bytes(data[start:end]))` pairs, as
  Starlette does, and the route writes them after `write()` returns.
- **Callback exceptions propagate.** An exception raised inside a callback comes out of
  `parser.write()`. Use this to stop early on an unknown field or a non-audio file part.
- **Truncated bodies.** A body that stops before the closing boundary makes `finalize()`
  return normally, and `on_end` is never called. The route must check that `on_end` ran.
- **Malformed input** raises `python_multipart.exceptions.MultipartParseError`, a subclass
  of `FormParserError` and `ValueError`.
  - The library also logs a WARNING on the `python_multipart.multipart` logger, for example
    `Expected boundary character 45, got 103 at index 2`. It carries offsets and at most one
    byte's `repr`, never field content.
  - That record goes through earmark's handler (third-party, so URL-scrubbed). No change to
    `log.py` is needed.
- **Size limits.** `max_size` truncates silently with a warning, so do not use it. Count
  the bytes in the route.

### 2.4 Request streaming and auth order

A probe called a FastAPI app wrapped in `BearerAuthMiddleware` directly through ASGI, with
a hand-written `receive`. Results:

- An unauthenticated POST got 401 with **zero** `receive()` calls, so the body is never read.
- `request.stream()` yields one chunk per `http.request` message, then a final `b""`.
- An `http.disconnect` before the body ends raises `starlette.requests.ClientDisconnect`
  from the stream.

`TestClient` cannot test streaming. `starlette/testclient.py` calls `request.read()` and
sends the whole body as one message, even when the content is a generator. So the tests
that need several chunks, a lying `Content-Length`, a mid-body disconnect, or "receive was
never called" drive the ASGI app directly, through the new `tests/asgi_helpers.py` (6.1).

### 2.5 SQLite migration

With the uv-managed CPython (SQLite 3.46.1, the same build CI uses), all of these succeed
inside one `BEGIN IMMEDIATE` on a `STRICT` table:

- `ALTER TABLE … ADD COLUMN source TEXT NOT NULL DEFAULT 'apple_podcasts' CHECK (source IN
  (…))`, with the CHECK enforced on later inserts;
- `UPDATE … SET instructions = COALESCE(prompt, '')`;
- `ALTER TABLE … DROP COLUMN prompt`, which works because `prompt` is in no index or
  constraint.

Changing `kind`'s CHECK would need a table rebuild, so `kind` stays `'ingest'` (8.7).

### 2.6 pydantic-settings comma lists

`Annotated[tuple[str, ...], NoDecode]` with a `mode="before"` validator that splits on
commas was checked against pydantic-settings 2.16.3. It turns
`X_ALLOWED='a@x.org, b@y.org,'` into `('a@x.org', 'b@y.org')`, and an empty value into
`()` (with `env_ignore_empty=True`). Without `NoDecode`, pydantic-settings would try to
JSON-decode the value.

### 2.7 Code facts the plan relies on

- `transcribe.faster_whisper.file_key(path)` is
  `episode_key("local", "file", sha256(bytes).hexdigest())`.
- `fetch.AUDIO_EXTENSIONS` and `fetch.CONTENT_TYPE_EXTENSIONS` already define what counts as
  audio for downloads. The upload check reuses them.
- `GuardedFetcher.download(url, destination, *, max_bytes, purpose, limit_name)` refuses
  textual content types, writes through `atomic_writer(…, temp_suffix=".part")`, and names
  hosts only in its errors.
- `CachedTranscriber` deletes audio only under `episodes/<key>/`. Audio passed from
  elsewhere is left alone (`test_audio_outside_episode_dir_untouched`), which is what lets
  uploads be transcribed straight from their staging file.
- ruff's `ASYNC` rules are on. `ASYNC230` flags `open()` and `Path.open()` inside
  `async def`, so every file operation in the async route goes through
  `starlette.concurrency.run_in_threadpool`.

### 2.8 Dead ends

- `request.form()` and `UploadFile`, because of the temp-file spooling (2.1).
- A route-level `max_body_size`. It is one limit for both content types, and its 413 is
  plain text (2.2).
- `MultipartParser(max_size=…)`. It truncates silently (2.3).
- Moving uploaded or downloaded audio into `episodes/<key>/` so `AudioFetcher.fetch` can find
  it. Two jobs with the same bytes would then share and delete each other's audio. Per-job
  staging files avoid that (5.7).
- `TestClient` for streaming tests (2.4).

## 3. Files

### 3.1 Create

| File | Purpose |
|---|---|
| `src/earmark/recipients.py` | Address validation, normalisation, masking, and `RecipientPolicy`. Standard library only. |
| `src/earmark/sources.py` | Classifying request URLs, the display form of a direct URL, the upload audio check, the audio title, `sha256_file`, and `audio_episode`. Light imports only. |
| `src/earmark/staging.py` | `Staging`: `data/uploads/` and `data/downloads/`, the streaming `UploadWriter`, per-job download paths, release at a terminal state, and the startup sweeps. |
| `src/earmark/submission.py` | `Intake`: recipient check, URL classification and enqueue, for the API and the CLI. Replaces `ingest.py`. |
| `src/earmark/api/upload.py` | `receive_upload`: the streaming multipart reader on python-multipart. |
| `tests/asgi_helpers.py` | `call_asgi`: drives an ASGI app with scripted `http.request` chunks, an optional disconnect, and a per-`receive` hook. Records the receive count. |
| `tests/multipart_helpers.py` | `Part`, and `multipart_body(parts, boundary)`, which builds exact multipart bytes in any part order. |
| `tests/test_recipients.py`, `tests/test_sources.py`, `tests/test_staging.py`, `tests/test_upload.py`, `tests/test_submission.py`, `tests/test_leaks.py` | See section 6. |

### 3.2 Change

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | Add `python-multipart>=0.0.34` to `[project].dependencies` with `uv add`. |
| `src/earmark/models.py` | `Source` gains `upload` and `url`; `AUDIO_SOURCES`, `AUDIO_SHOW_ID`; `content_key`, `source_key`; the `Episode` key rule; `Transcript.asr_profile`; `TranscriptionRequest` and `UploadFields` replace `IngestRequest`; new `Job` fields; caps; `REQUEST_ERROR_TYPES` replaces `INGEST_ERROR_TYPES`. `normalize_prompt` and `MAX_PROMPT_CHARS` are deleted. |
| `src/earmark/config.py` | `allowed_recipients`, `max_upload_mb`, `dedupe_window_s`; `Settings.recipient_policy()`; the masked display in `redacted_items`. |
| `src/earmark/db.py` | Migration `0002_requests`. |
| `src/earmark/jobs.py` | `NewJob`; `enqueue(new)`; the new dedupe key and window; `active_upload_files()`, `running_job_ids()`; new columns in `_row_to_job`; `open_job_store` passes the window. |
| `src/earmark/artifacts.py` | `ArtifactStore.data_dir` property. |
| `src/earmark/audio.py` | `HttpAudioFetcher.download(url, destination)`. |
| `src/earmark/fakes.py` | `FakeAudioFetcher.download` and `downloads`; `FakeTranscriber.cached` and `profile`; `DownloadCall`. |
| `src/earmark/pipeline/stages.py` | `AudioFetcher.download`, `Transcriber.cached`. |
| `src/earmark/pipeline/runner.py` | Source dispatch in `_stage_work`; profile-aware cache lookup; `finish()` (release staged audio, then notify); `load_episode` for audio sources; the `staging` keyword. |
| `src/earmark/pipeline/worker.py` | Jobs that `requeue_stale` fails go through `pipeline.finish`. |
| `src/earmark/pipeline/factory.py` | `build_worker` builds `Staging`, sweeps downloads, and passes it to `Pipeline`. |
| `src/earmark/transcribe/base.py` | `TranscriptionEngine.profile`; `asr_profile()`; `CachedTranscriber.cached` and `.profile`; the profile stamped on write. |
| `src/earmark/transcribe/faster_whisper.py` | `FasterWhisperEngine.profile`; `file_key` built on `sources.sha256_file` and `models.content_key`. |
| `src/earmark/transcribe/remote.py` | `RemoteAsrEngine.profile`. |
| `src/earmark/notify/ntfy.py` | Labels for upload and URL jobs; defensive `scrub_urls` on reasons and warnings. |
| `src/earmark/doctor/checks.py` | The `recipients` check after `api token`. |
| `src/earmark/api/app.py` | `create_app(…, max_upload_bytes=None)`; builds the `Intake`; sweeps uploads; warns on `all`; validation messages for the new models. |
| `src/earmark/api/routes.py` | `POST /transcriptions` (async, content-type dispatch); `GET /jobs` returns views; `build_router(intake)`. |
| `src/earmark/api/schemas.py` | `TranscriptionResponse`, `JobView`, `job_view()`, `JobList` of views. |
| `src/earmark/cli.py` | `submit SOURCE --to [--instructions] [--title]`; `jobs` prints views. |
| `src/earmark/ingest.py`, `tests/test_ingest.py` | Deleted. `submission.py` and `tests/test_submission.py` replace them. |
| `.env.example` | A `# --- Requests ---` block: `# EARMARK_ALLOWED_RECIPIENTS=you@example.org`, `EARMARK_MAX_UPLOAD_MB=1024`, `EARMARK_DEDUPE_WINDOW_S=600`, each with a one-line comment. |
| `README.md` | Only what `tests/test_docs.py` reads (7.4): the quickstart's request line and allowlist export, three settings rows, the `submit` row, and the HTTP API sentence. |
| `docs/shortcut.md` | Only what the doc tests read, plus two error-table rows that would otherwise be wrong (5.15). |
| `CLAUDE.md` | "Shape of the code": the diagram's first line becomes `POST /transcriptions ─► jobs table (SQLite) ─► worker ─► Pipeline`, and the "Planned:" sentence keeps only step 25's email delivery. Nothing else. |
| `docs/ARCHITECTURE.md` | Already updated by this plan (section 8.14). The implementer corrects it only where the code has to differ, and says so. |
| Tests | As listed in section 6. Every test that used `/ingest`, `IngestRequest`, `enqueue_ingest`, `prompt`, `store.enqueue(url, …)` or the raw `Job` from the API is updated. |

Test helpers to update: `tests/factories.py` (`make_job` with the new fields, plus
`make_url_job`, `make_upload_job`, `new_podcast_job`) and `tests/pipeline_env.py`
(`BlockingTranscriber.cached` delegates; `make_env` builds the default `Staging`). The test
stubs of `TranscriptionEngine` in `tests/test_transcribe_base.py` and
`tests/whisper_fakes.py` gain `profile`.

## 4. Interfaces

Signatures are binding. Parameters a Protocol declares are positional-only, as everywhere in
`stages.py`.

### 4.1 `earmark/models.py`

```python
Source = Literal["apple_podcasts", "upload", "url"]
AUDIO_SOURCES: Final[frozenset[Source]] = frozenset({"upload", "url"})
AUDIO_SHOW_ID: Final = "audio"  # the synthetic show id of upload and url episodes
GUID_SHA256_PREFIX: Final = "sha256:"

MAX_URL_CHARS: Final = 2048  # unchanged
MAX_INSTRUCTIONS_CHARS: Final = 4000
MAX_TITLE_CHARS: Final = 200
MAX_ADDRESS_CHARS: Final = 254
REQUEST_ERROR_TYPES: Final = frozenset(
    {
        "url_empty",
        "url_too_long",
        "to_invalid",
        "instructions_too_long",
        "title_too_long",
        "timestamp_out_of_range",
    }
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
UploadFileName = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}\.[a-z0-9]{1,5}$")]


def content_key(sha256_hex: str) -> str:
    """Key of content-identified audio: episode_key("local", "file", sha256_hex)."""


def source_key(source: Source, show_id: str, guid: str) -> str:
    """content_key(hex) for upload and url (guid must be 'sha256:' + 64 hex),
    episode_key(source, show_id, guid) otherwise."""
```

`Episode._key_matches` uses `source_key`. For `upload` and `url` it also requires
`show_id == AUDIO_SHOW_ID` and a guid of `sha256:` plus 64 lower-case hex; a violation is a
`ValueError`.

`Transcript` gains `asr_profile: str | None = None`, the profile it was made with (4.7).
Transcripts written before this step load with `None`.

Request models, which share one base class. Each validator raises `PydanticCustomError` with
the type named, and the message never contains input.

```python
class RequestFields(Model):
    to: str
    instructions: str = ""
    title: str | None = None
    timestamp: AwareDatetime | None = None


class TranscriptionRequest(RequestFields):  # the JSON body
    url: str


class UploadFields(RequestFields):  # the multipart text fields
    pass
```

| Field | Rule | Error type and message |
|---|---|---|
| `url` | As before: stripped, non-empty, at most 2048 characters | `url_empty`, `url_too_long`, unchanged |
| `to` | `normalize_address` | `to_invalid`: `to must be one email address` |
| `instructions` | `None` becomes `""`; stripped; at most 4000 characters | `instructions_too_long`: `instructions is too long (at most 4000 characters)` |
| `title` | Stripped; blank becomes `None`; at most 200 characters | `title_too_long`: `title is too long (at most 200 characters)` |
| `timestamp` | As before: must convert to UTC | `timestamp_out_of_range`, unchanged |

Field order, which the "allowed:" lists in error messages follow:

- `TranscriptionRequest`: `url`, `to`, `instructions`, `title`, `timestamp`;
- `UploadFields`: `to`, `instructions`, `title`, `timestamp`.

So `url` must come first in `TranscriptionRequest`'s schema. Declare the fields explicitly in
that order, or build the "allowed" list from a constant rather than from `model_fields`.

`instructions` accepts JSON `null` and turns it into `""`, so declare it `str | None` with a
before-validator, and make the stored type `str`. Only leading and trailing whitespace is
stripped. Everything inside is kept verbatim, including newlines.

`Job` loses `prompt` and gains these fields, all with defaults so old rows load:

```python
source: Source = "apple_podcasts"
url: str | None  # apple_podcasts: canonical URL; url: the direct URL; upload: None
to: str | None = None  # normalised; None only on jobs enqueued before step 24
instructions: str = ""
title: str | None = None
upload_file: UploadFileName | None = None  # upload: the staged file's name under data/uploads
upload_name: str | None = None  # upload: the client's file name, cleaned
content_sha256: Sha256Hex | None = None  # upload: sha256 of the staged bytes
```

`Job._kind_requirements` becomes a source check:

- `apple_podcasts` and `url` need `url` set and `upload_file is None`;
- `upload` needs `url is None`, with `upload_file` and `content_sha256` both set.

`JobKind` stays `Literal["ingest"]`.

### 4.2 `earmark/recipients.py`

```python
ALL: Final = "all"
ELLIPSIS: Final = "…"


def normalize_address(value: str) -> str:
    """Strip; validate; return local part unchanged + '@' + domain lower-cased.
    Raises ValueError("not an email address") (no input in the message)."""


def mask_address(address: str) -> str:
    """'alice@example.org' -> 'a…@example.org'. For a value without '@', '…'."""


@dataclass(frozen=True, slots=True)
class RecipientPolicy:
    allow_all: bool
    addresses: frozenset[str]  # normalised

    @classmethod
    def from_setting(cls, entries: Sequence[str]) -> Self: ...  # ("all",) -> allow_all
    @property
    def refuses_all(self) -> bool: ...  # not allow_all and no addresses
    def allows(self, address: str) -> bool: ...  # address already normalised
    def describe(self) -> str: ...  # "all", "" when empty, else masked, comma-separated, sorted


class RecipientRefused(Exception):
    message: str  # safe to return to the client; never contains the address
```

**What counts as an address.** ASCII only, with exactly one `@`, at most 254 characters in
total. Anything else is rejected, so no CR, LF, space, comma, quote, `<` or `>` can get
through:

- **Local part.** 1–64 characters, a dot-atom of RFC 5322 `atext`:
  `[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]`, with single dots between runs and none at either end.
- **Domain.** At least two labels, at most 253 characters. Each label is
  `[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?`, and the last label is not all digits.
- **Not accepted.** Quoted local parts, IP literals and non-ASCII (IDN) addresses.

**`RecipientRefused` messages:**

- when `refuses_all`: `no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the
  server`;
- when not allowed: `to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)`.

### 4.3 `earmark/config.py`

```python
allowed_recipients: Annotated[tuple[str, ...], NoDecode] = Field(
    (), description="Who may receive transcripts: comma-separated addresses, or all."
)
max_upload_mb: int = Field(1024, gt=0, description="Upload size cap (MiB).")
dedupe_window_s: int = Field(600, ge=0, description="Seconds a finished job answers repeats.")


def recipient_policy(self) -> RecipientPolicy: ...
```

The `allowed_recipients` before-validator handles three cases:

- **Splitting.** A `str` is split on `,`, each entry is stripped, and empty entries are
  dropped. A list or tuple passes through.
- **`all`.** Matched case-insensitively. It must be the only entry, and then the value is
  `("all",)`. Otherwise the error is `all cannot be combined with addresses`.
- **Addresses.** Every other entry goes through `normalize_address`, and duplicates are
  dropped in order. A bad entry gives `entry <n> is not an email address` (1-based, no
  value). `ConfigError` text never contains input values.

`redacted_items` shows `allowed_recipients` as `recipient_policy().describe()`: `all`, the
masked list, or empty.

### 4.4 `earmark/sources.py`

```python
@dataclass(frozen=True, slots=True)
class SourceUrl:
    source: Literal["apple_podcasts", "url"]
    url: str              # apple: canonical episode URL; url: the URL without its fragment

def parse_source_url(text: str, /) -> SourceUrl:      # raises resolve.apple.UnsupportedUrlError
def display_url(url: str, /) -> str:                   # "scheme://host[:port]/path" (no userinfo/query/fragment)
def url_host(url: str, /) -> str:                      # endpoint.host_label(url)
def upload_extension(filename: str | None, content_type: str | None) -> str | None:
def clean_upload_name(raw: str | None) -> str | None:
def audio_title(job: Job) -> str:
def sha256_file(path: Path, /) -> str:                 # 1 MiB chunks, hex digest
def audio_episode(job: Job, sha256_hex: str) -> Episode:
```

**`parse_source_url` rules**, in this order:

1. **Shape.** After stripping, empty text, text over 2048 characters, or any character
   `<= " "` or `\x7f` is `malformed`.
2. **No `://`.** The text is an Apple link or nothing: prepend `https://` and parse. If the
   host is in `APPLE_HOSTS`, return `parse_apple_url(text)`. Otherwise it is `malformed`. So
   a direct URL must spell out `http://` or `https://`.
3. **Parsing.** `urlsplit` raising, or `.port` raising, is `malformed`. A scheme other than
   http or https (case-insensitive) is `malformed`. An empty host (trailing `.` stripped) is
   `malformed`.
4. **Hosts.** A host in `APPLE_HOSTS` gives `parse_apple_url(text)`, so the existing
   messages apply: `malformed`, `not_episode`, `show_link`. A host equal to `apple.com`, or
   ending in `.apple.com`, is `not_episode`: `music.apple.com` and
   `embed.podcasts.apple.com` are refused as before, not treated as audio.
5. **Credentials.** A URL with userinfo (`@` in the netloc) gets a new `UrlProblem`,
   `"credentials"`, with the message `Links with a user name or password are not supported.`
   Add it to `resolve/apple.py`'s `MESSAGES` and `UrlProblem`.
6. **Otherwise** the result is `SourceUrl("url", parts._replace(fragment="").geturl())`.

**`upload_extension`.** It returns the extension to stage under, or `None` when the upload
does not look like audio. The checks run in order, and the first hit wins:

1. the extension of `filename` (lower-cased, after the last `.`) when it is in
   `fetch.AUDIO_EXTENSIONS`;
2. `fetch.CONTENT_TYPE_EXTENSIONS[media_type(content_type)]`;
3. `"bin"` when the media type starts with `audio/`;
4. otherwise `None`.

This is the brief's "declared content type or extension only".

**`clean_upload_name`.**

- Decoding is the caller's job (5.4).
- Replace `\` with `/` and keep the part after the last `/`.
- Clean with `sanitize.clean_unicode`, drop characters in Unicode categories `Cc`, `Cf`,
  `Co`, `Cn`, `Zl` and `Zp` (keeping ZWJ and ZWNJ), and collapse whitespace.
- Cut to 255 characters. The result is `None` when empty.

**`audio_title(job)`.** The title of an upload or URL episode. Clean it the same way and cap
it at 200 characters. Take the first that is non-empty:

1. `job.title`;
2. for an upload, the stem (`PurePosixPath(upload_name).stem`) of `job.upload_name`;
3. for a URL, the last non-empty path segment of `job.url`, `urllib.parse.unquote`d;
4. for an upload, `Uploaded audio`; for a URL, `url_host(job.url)`.

**`audio_episode(job, hex)`** builds the `Episode`:

- `key=content_key(hex)`, `source=job.source`, `show_id=AUDIO_SHOW_ID`, `episode_id=""`,
  `guid="sha256:"+hex`;
- `title=audio_title(job)`, `show_title=""`;
- `published_at=None`, `duration_s=None`, `audio_url=""`, `feed_url=None`, `url=""`,
  `description=""`.

### 4.5 `earmark/staging.py`

```python
UPLOADS_DIR: Final = "uploads"
DOWNLOADS_DIR: Final = "downloads"


@dataclass(frozen=True, slots=True)
class StagedUpload:
    name: str  # "<upload_id>.<ext>", matches UploadFileName
    path: Path
    size: int
    sha256: str  # 64 hex


class UploadWriter:  # not thread-safe; one per upload
    @property
    def size(self) -> int: ...
    def write(self, data: bytes, /) -> None: ...  # appends and hashes
    def commit(self) -> StagedUpload: ...  # flush, fsync, close, rename, fsync dir
    def abort(self) -> None: ...  # close and unlink; idempotent
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...  # abort unless committed


class Staging:
    def __init__(self, data_dir: Path) -> None: ...
    @property
    def uploads_dir(self) -> Path: ...  # <data_dir>/uploads
    @property
    def downloads_dir(self) -> Path: ...  # <data_dir>/downloads
    def begin_upload(self, ext: str) -> UploadWriter: ...  # see below
    def upload_path(self, name: str) -> Path: ...  # ValueError unless UploadFileName
    def remove_upload(self, name: str) -> bool: ...
    def download_destination(self, job_id: str) -> Callable[[str], Path]: ...
    def clear_download(self, job_id: str) -> int: ...  # unlink downloads/<job_id>.* and .<job_id>.*
    def release(self, job: Job) -> None: ...  # terminal cleanup; logs, never raises
    def sweep_uploads(self, keep: Collection[str]) -> int: ...
    def sweep_downloads(self, running: Collection[str]) -> int: ...
```

- **Ids and names.** `upload_id` is `uuid.uuid4().hex`. `ext` must match `[a-z0-9]{1,5}`,
  else `ValueError`. Job ids must match `[0-9a-f]{32}` wherever they become a path.
- **`begin_upload(ext)`** creates `uploads/` (mode 0o700) and opens
  `.<upload_id>.<ext>.part` with `O_CREAT | O_EXCL`, mode 0o600.
- **`download_destination(job_id)`** maps `ext` to `downloads/<job_id>.<ext>`.
- **`commit()`** renames `.part` to `<upload_id>.<ext>` with `os.replace` and keeps mode
  0o600.
- **`release(job)`** handles each source:
  - `upload`: `remove_upload(job.upload_file)`;
  - `url`: `clear_download(job.id)`;
  - `apple_podcasts`: nothing.

  An `OSError` is logged at WARNING (with no path beyond the base name) and swallowed.
- **`sweep_uploads(keep)`** unlinks every regular file in `uploads/` whose name is not in
  `keep`. That covers `.part` files and orphans. It leaves directories alone, logs one INFO
  line with the count when it is above 0, and returns the count.
- **`sweep_downloads(running)`** unlinks every regular file in `downloads/` unless its name
  matches `^\.?(?P<id>[0-9a-f]{32})\.` with `id` in `running`.

No function in this module uses `tempfile`.

### 4.6 Stage Protocols, `earmark/pipeline/stages.py`

```python
class AudioFetcher(Protocol):
    def fetch(self, episode: Episode, /) -> Path: ...  # unchanged
    def download(self, url: str, destination: Callable[[str], Path], /) -> Path:
        """Download a client-supplied direct audio URL to destination(ext); return the path.
        Always downloads (no cache). Errors follow fetch.py."""


class Transcriber(Protocol):
    def cached(self, episode: Episode, /) -> Transcript | None:
        """The cached transcript if one exists for this transcriber's profile; never loads a model."""

    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript: ...
```

The `transcribe` contract is unchanged, except that "cached" now means "cached with this
profile". `Resolver` and `Notifier` are unchanged.

- **`HttpAudioFetcher.download`** calls
  `self._fetcher.download(url, destination, max_bytes=self._max_bytes, purpose="audio",
  limit_name="EARMARK_MAX_AUDIO_MB")`. It logs
  `"downloaded direct audio: %d bytes from %s after %d redirects"` with `urlsplit(…).hostname`,
  and returns the path.
- **`FakeAudioFetcher`** gains `download(url, destination)`:
  - it records a `DownloadCall(url)` and honours `fail_next`;
  - it writes `payloads.get(url, payload)` with `atomic_write_bytes(destination("mp3"), …)`,
    increments `downloads`, and returns the path;
  - its constructor gains `payloads: Mapping[str, bytes] | None = None`.

  The `calls` element type becomes `FetchCall | DownloadCall`.

### 4.7 Transcript profile, `earmark/transcribe/base.py`

```python
def asr_profile(backend: str, options: Mapping[str, str | int | bool | None]) -> str:
    """backend + " " + json.dumps(options, sort_keys=True, separators=(",", ":"))."""


class TranscriptionEngine(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def profile(self) -> str: ...  # new
    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript: ...


class CachedTranscriber:
    @property
    def profile(self) -> str: ...  # self.engine.profile
    def cached(self, episode: Episode, /) -> Transcript | None: ...
```

**Profiles:**

- `FasterWhisperEngine`: `asr_profile("faster_whisper", {"model", "device", "compute_type",
  "language", "beam_size": BEAM_SIZE, "vad_filter": VAD_FILTER})`, taking the first four
  from `WhisperOptions`, with `language=None` when auto;
- `RemoteAsrEngine`: `asr_profile("remote", {"model", "language"})`;
- `FakeTranscriber`: `asr_profile("fake", {"model": self.model, "language": self.language})`.

**Caching.**

- `cached(episode)` returns `read_transcript(key)` when that transcript's `asr_profile`
  equals `self.profile`, and `None` otherwise.
- `transcribe` uses `cached()` where it used `read_transcript`. After the engine returns, it
  writes `transcript.model_copy(update={"asr_profile": self.profile})` and returns that.
- A miss overwrites `transcript.json` and `transcript.txt` with the new profile's transcript.
  On disk there is one transcript per episode (8.6).
- `FakeTranscriber` follows the same rules.

### 4.8 Jobs, `earmark/jobs.py` and `earmark/db.py`

```python
@dataclass(frozen=True, slots=True)
class NewJob:
    source: Source
    to: str  # normalised, non-empty
    instructions: str = ""
    title: str | None = None
    url: str | None = None  # apple_podcasts: canonical; url: direct
    upload_file: str | None = None
    upload_name: str | None = None
    content_sha256: str | None = None
    shared_at: datetime | None = None


class JobStore:
    def __init__(
        self,
        db: Database,
        *,
        clock: Clock = utc_now,
        max_attempts: int = 4,
        lease: timedelta = timedelta(seconds=900),
        dedupe_window: timedelta = timedelta(seconds=600),  # new; must be >= 0
    ) -> None: ...
    def enqueue(self, new: NewJob, /) -> Enqueued: ...
    def active_upload_files(self) -> set[str]: ...  # upload_file of queued and running jobs
    def running_job_ids(self) -> set[str]: ...
```

The old `enqueue(url, *, prompt, shared_at)` is removed.

**`enqueue`:**

1. **Validate.** `new` must satisfy the `Job` source rules; any problem is a `ValueError`.
2. **Dedupe key.** `_dedupe_key(kind, source, subject, to, instructions)` is
   `sha256(json.dumps([kind, source, subject, to, instructions], ensure_ascii=False,
   separators=(",", ":")))`. The subject is the `url`, or `"sha256:" + content_sha256` for
   an upload. `title` and `shared_at` are not part of it.
3. **Lookup.** Inside the same `BEGIN IMMEDIATE`:
   ```sql
   SELECT * FROM jobs
   WHERE dedupe_key = :key
     AND (status IN ('queued', 'running') OR (status = 'done' AND updated_at > :cutoff))
   ORDER BY CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
            updated_at DESC, seq DESC
   LIMIT 1
   ```
   `:cutoff` is `_to_db(now - dedupe_window)`. With a window of 0, a done job never matches.
4. **Outcome.** A hit returns `Enqueued(existing, False)`. Otherwise insert, with
   `recipient` = `to`, and return `Enqueued(job, True)`.

**Migration `0002_requests`**, appended to `MIGRATIONS`, so `SCHEMA_VERSION` becomes 2.
These statements run in order:

```sql
ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'apple_podcasts'
    CHECK (source IN ('apple_podcasts', 'upload', 'url'))
ALTER TABLE jobs ADD COLUMN recipient TEXT
ALTER TABLE jobs ADD COLUMN instructions TEXT NOT NULL DEFAULT ''
ALTER TABLE jobs ADD COLUMN title TEXT
ALTER TABLE jobs ADD COLUMN upload_file TEXT
ALTER TABLE jobs ADD COLUMN upload_name TEXT
ALTER TABLE jobs ADD COLUMN content_sha256 TEXT
UPDATE jobs SET instructions = COALESCE(prompt, '')
ALTER TABLE jobs DROP COLUMN prompt
CREATE INDEX jobs_by_dedupe ON jobs (dedupe_key, status, updated_at)
```

- **Naming.** The column is `recipient`, because `TO` is an SQL keyword. The `Job` field is
  `to`.
- **Old rows.** Pre-step-24 rows keep `recipient` NULL. Their statuses are not touched, so
  step 25 must fail a job without `to` permanently.
- **Wiring.** `open_job_store` passes `dedupe_window=timedelta(seconds=settings.dedupe_window_s)`.

### 4.9 `earmark/submission.py`

```python
@dataclass(frozen=True, slots=True)
class Intake:
    store: JobStore
    policy: RecipientPolicy
    staging: Staging

    def check_recipient(self, to: str, /) -> None: ...  # raises RecipientRefused
    def submit_url(self, request: TranscriptionRequest, /) -> Enqueued: ...
    def submit_upload(
        self, fields: UploadFields, staged: StagedUpload, *, upload_name: str | None
    ) -> Enqueued: ...
```

**`submit_url`**

1. Check the recipient.
2. Classify the URL with `parse_source_url` (`UnsupportedUrlError` propagates).
3. Enqueue a `NewJob` with `source` and `url` from the result, `to`, `instructions`,
   `title`, and `shared_at=request.timestamp`.
4. Log once at INFO (5.12).

**`submit_upload` owns `staged` from the moment it is called.**

- It deletes the staged file when it raises (recipient refused, `sqlite3.Error`, anything
  else) and when the enqueue returns an existing job.
- It enqueues `NewJob(source="upload", upload_file=staged.name, content_sha256=staged.sha256,
  upload_name=upload_name, …)`, then logs as `submit_url` does.

### 4.10 `earmark/api/upload.py`

```python
MULTIPART_OVERHEAD_BYTES: Final = 128 * 1024  # text fields, part headers, boundaries
MAX_FIELD_BYTES: Final = 4 * MAX_INSTRUCTIONS_CHARS + 64
MAX_PARTS: Final = 8
FILE_FIELD: Final = "file"
TEXT_FIELDS: Final = ("to", "instructions", "title", "timestamp")


class UploadRejected(Exception):
    def __init__(self, status: int, detail: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ReceivedUpload:
    fields: dict[str, str]  # decoded text parts, at most one per name
    writer: UploadWriter  # complete but not committed; caller commits or aborts
    filename: str | None  # cleaned (clean_upload_name)


async def receive_upload(
    headers: Headers, stream: AsyncIterator[bytes], staging: Staging, *, max_file_bytes: int
) -> ReceivedUpload: ...
```

`receive_upload` cleans up everything it staged before it raises, whether the exception is
`UploadRejected`, `ClientDisconnect` or anything else.

### 4.11 HTTP surface

| Route | Behaviour |
|---|---|
| `POST /transcriptions` | **JSON** (`Content-Type: application/json`): a `TranscriptionRequest`, at most 64 KiB. **Multipart** (`multipart/form-data`): one `file` part and the text fields `to`, `instructions`, `title`, `timestamp`; the file is at most `EARMARK_MAX_UPLOAD_MB` (or `max_upload_bytes`). The answer is 202 `{job_id, status, created}` with `Location: /jobs/{id}`. 401 bad token, 413 too large, 415 other content type or non-audio file, 422 invalid, 503 store unavailable. |
| `GET /jobs/{id}` | `JobView`, 404 if unknown. |
| `GET /jobs` | `{"jobs": [JobView, …]}`, `?limit=1..500` (default 20), `?status=`. |
| `GET`, `HEAD /healthz` | Unchanged. |

`POST /ingest` no longer exists: it is 404 after auth.

```python
class TranscriptionResponse(ApiModel):  # renamed from IngestResponse; same fields
    job_id: JobId
    status: JobStatus
    created: bool


class JobView(ApiModel):
    id: JobId
    status: JobStatus
    stage: Stage | None
    source: Source
    url: str | None  # apple: canonical; url: display_url(job.url); upload: None
    to: str | None  # in full
    instructions: str  # <= 200 chars: text if len <= 200 else text[:199] + "…"
    title: str | None
    episode_key: EpisodeKey | None
    attempts: int
    run_after: AwareDatetime
    error: str | None  # scrub_urls(job.error)
    warnings: tuple[str, ...]  # scrub_urls(each)
    result: JobResult | None
    shared_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


def job_view(job: Job) -> JobView: ...


VIEW_INSTRUCTIONS_CHARS: Final = 200
```

`JobView` has no `heartbeat_at`, `upload_file`, `upload_name` or `content_sha256`.

```python
def create_app(
    settings: Settings, *, store: JobStore | None = None, max_upload_bytes: int | None = None
) -> FastAPI: ...
def build_router(intake: Intake, *, max_upload_bytes: int) -> APIRouter: ...
```

`create_app` builds `Intake(store, settings.recipient_policy(), Staging(settings.data_dir))`.
`max_upload_bytes` defaults to `settings.max_upload_mb * MIB`. Before returning, it calls
`staging.sweep_uploads(store.active_upload_files())`, and it logs the `all` warning (5.13).

### 4.12 CLI

```
earmark submit SOURCE --to ADDRESS [--instructions TEXT] [--title TEXT]
earmark jobs [--status] [--limit] [--json]            # unchanged options; prints views
```

- **SOURCE.** If it names an existing regular file, the file is an upload. Otherwise it is a
  URL.
- **Uploads.**
  - `upload_extension(path.name, None)` must not be `None`, else exit 2 with `earmark
    submit: <name> does not look like audio (use an audio file extension such as .mp3 or
    .m4a)`.
  - The file is copied in 1 MiB reads into `Staging.begin_upload(ext)`. Over
    `EARMARK_MAX_UPLOAD_MB` is exit 2: `earmark submit: the file is larger than the <n> MiB
    limit (EARMARK_MAX_UPLOAD_MB)`, and nothing is left in staging.
  - The fields are validated as `UploadFields`, and the job goes through
    `Intake.submit_upload` with `upload_name=clean_upload_name(path.name)`.
- **URLs** are validated as `TranscriptionRequest` and go through `Intake.submit_url`.
- **Output.** `<job_id> <status>`, with ` (existing job)` when deduped.
- **Exit codes.**
  - 2: validation errors (the pydantic messages, as today), `UnsupportedUrlError`, or
    `RecipientRefused` (its message);
  - 1: store errors, unchanged.
- **The old option.** `--prompt` is gone. typer's usage error (exit 2) covers it.
- **`jobs`.** Each line shows `id status stage attempts created subject`. The subject is
  `view.url`, else `upload` for an upload, else `-`. `--json` prints
  `{"jobs": [view.model_dump(mode="json"), …]}`.

### 4.13 Doctor

The `recipients` check runs right after `api token`:

| `recipient_policy()` | Result |
|---|---|
| `refuses_all` | `FAIL recipients EARMARK_ALLOWED_RECIPIENTS is not set: every request is refused. Set it to the destination addresses, comma-separated, or to all.` |
| `allow_all` | `warn recipients all: any address is accepted. Anyone with the API token can have earmark send to any address.` |
| a list | `ok recipients <n> allowed: <describe()>`, masked |

## 5. Behaviour and edge cases

### 5.1 Route dispatch

The handler is `async def transcriptions(request: Request) -> JSONResponse`, with no body
parameters, so FastAPI reads nothing. It branches on `media_type(request.headers.get(
"content-type"))`:

- `application/json` goes to 5.3;
- `multipart/form-data` goes to 5.4;
- anything else, including a missing header, is **415**: `send JSON (Content-Type:
  application/json) or a multipart form (Content-Type: multipart/form-data)`.

Every store call and every file operation runs through `run_in_threadpool` (2.7). Errors are
raised as `HTTPException(status, detail)`, so they render as `{"detail": …}` like the rest of
the API. A `ClientDisconnect` while reading either kind of body is logged at INFO (`client
disconnected during the request`). The handler then returns a bare `Response(status_code=400)`
that nobody reads, so no error traceback is logged.

### 5.2 Auth first

`BearerAuthMiddleware` already answers 401 before the app runs, and `receive()` is never
called (2.4). The route adds nothing. The tests prove it for both body types with
`call_asgi`: receive calls == 0, and `uploads/` was never created.

### 5.3 JSON

1. **Declared size.** A `Content-Length` (digits only) over `MAX_JSON_BODY_BYTES` (64 KiB)
   is **413** `request body is larger than 64 KiB`, before reading.
2. **Read.** Read `request.stream()`, accumulating at most 64 KiB. One byte more is the same
   413.
3. **Empty body.** It is 422 `request body is missing; send a JSON object with url and to`.
4. **Parse.** `TranscriptionRequest.model_validate_json(body)`. A `ValidationError` is 422
   with `describe_validation_errors(errors)`, where each error's `loc` is prefixed with
   `"body"` so the existing `_describe` rules apply:
   - `json_invalid` becomes `request body is not valid JSON`;
   - a non-object becomes the "must be a JSON object" message;
   - an unknown key becomes `unknown field 'x' (allowed: url, to, instructions, title,
     timestamp)`.

   Use `exc.errors(include_input=False, include_url=False)`. Messages never echo input.
5. **Submit.** `Intake.submit_url`. `RecipientRefused` and `UnsupportedUrlError` are 422 with
   their message.

The 64 KiB cap still fits the largest valid body. 4000 instruction characters at up to 12
JSON bytes each, a 2048-character URL at 6, a 200-character title at 12, an address and a
timestamp come to about 53 KB. Update the comment next to the constant.

### 5.4 Multipart

The order is binding. Every failure path ends with the writer aborted, so nothing is left in
`uploads/`.

1. **Declared size.** A `Content-Length` over `max_upload_bytes + MULTIPART_OVERHEAD_BYTES`
   is **413** `the upload is larger than the <limit> limit (EARMARK_MAX_UPLOAD_MB)`, before
   reading. `<limit>` is `fetch._limit_text(max_upload_bytes)`: `N MiB` when it divides
   evenly, else `N bytes`. Expose it as a public `fetch.limit_text` (rename with its one
   caller).
2. **Boundary.** It comes from `parse_options_header(content_type)[1][b"boundary"]`. Missing
   is 422 `the multipart form has no boundary`. Constructing the parser can raise
   `FormParserError` (a boundary over 256 bytes), which is 422 `the multipart form is
   malformed`.
3. **Stream.** For each chunk of `request.stream()`:
   1. Add it to the body total. Over `max_upload_bytes + MULTIPART_OVERHEAD_BYTES` is the
      413 above.
   2. Call `parser.write(chunk)`. A `FormParserError` is 422 `the multipart form is
      malformed`.
   3. Write the file slices collected during that `write()`, in order, to the writer in one
      `run_in_threadpool` call. If the file bytes then exceed `max_upload_bytes`, it is the
      413 above. The cap is exact: a file of `max_upload_bytes` bytes passes, one more byte
      fails.
4. **Callbacks.** At `on_headers_finished`, a part is classified by the `name` and
   `filename` of its `Content-Disposition`:
   - **More than `MAX_PARTS` parts:** 422 `the form has too many parts`.
   - **No `name`, or a disposition that is not `form-data`:** 422 `the multipart form is
     malformed`.
   - **`name == "file"`:**
     - A second file part is 422 `the form must have exactly one file field named 'file'`.
     - The extension is `upload_extension(filename, part Content-Type)`. `None` is **415**
       `the file does not look like audio: send it with an audio content type such as
       audio/mpeg, or a file name ending in an audio extension such as .mp3 or .m4a`.
     - Otherwise `staging.begin_upload(ext)` opens the writer. This is the one file
       operation that runs inside a callback: it is a sync `os.open` in the event loop, done
       once, which `ASYNC230` does not flag. Alternatively, set a flag and open the writer
       in the threadpool before the slices are written; either is acceptable.
     - The filename is decoded with `.decode("utf-8", "replace")` and then
       `clean_upload_name`.
   - **`name` in `TEXT_FIELDS`:**
     - A `filename` parameter is 422 `field '<name>' must be a text field`.
     - A repeat of the name is 422 `field '<name>' is given more than once`.
     - Its data is buffered, and more than `MAX_FIELD_BYTES` is 422 `field '<name>' is too
       long`.
   - **Any other name:** 422 `unknown field '<name[:40]>' (allowed: file, to, instructions,
     title, timestamp)`.
5. **End of the body.** Call `parser.finalize()`. If `on_end` never ran, it is 422 `the
   multipart form is incomplete`.
6. **Checks.**
   - No file part is 422 `the form must have exactly one file field named 'file'`.
   - A file of 0 bytes is 422 `the uploaded file is empty`.
   - Text fields are decoded as strict UTF-8. Invalid bytes are 422 `field '<name>' is not
     valid UTF-8`.
7. **Validate.** `UploadFields.model_validate(fields)`. Errors are 422 through
   `describe_validation_errors(prefixed errors, body_fields=("file", "to", "instructions",
   "title", "timestamp"))`. A missing `to` reads `to is required`.
8. **Commit.** `writer.commit()` in the threadpool.
9. **Submit.** `Intake.submit_upload(fields, staged, upload_name=filename)` in the
   threadpool. `RecipientRefused` is 422, and `sqlite3.Error` propagates to the 503 handler.
   Either way `submit_upload` has deleted the file.

The text fields may come before or after the file. Nothing is checked against them until the
body has ended.

**Memory.**

- At most one received chunk of file data is held, between `parser.write()` and the
  threadpool write.
- At most `MAX_FIELD_BYTES` per text field is held, and there are at most four fields.
- Nothing goes through `tempfile`.

### 5.5 Recipients

- **Matching.** The address is compared after `normalize_address`, so the domain is
  case-insensitive and the local part is exact: `Alice@Example.ORG` matches an allowlist
  entry `Alice@example.org` but not `alice@example.org`.
- **Storage.** The stored `to` is the normalised form.
- **Refusal.** An unset or empty setting refuses every request with the `refuses_all`
  message. It is checked after the body validates and before anything is enqueued.
- **`all`.** It accepts any valid address, but the address must still be valid.

### 5.6 The runner (`Pipeline._stage_work`)

```
set_stage(resolve)
apple_podcasts: episode = resolver.resolve(job.url); audio = None
upload:         audio = staging.upload_path(job.upload_file)
                missing file            -> PermanentError("the uploaded audio is missing; upload it again")
                sha256_file(audio) != job.content_sha256
                                        -> PermanentError("the uploaded audio changed after it was received")
                episode = audio_episode(job, job.content_sha256)
url:            set_stage(fetch); staging.clear_download(job.id)
                audio = fetcher.download(job.url, staging.download_destination(job.id))
                episode = audio_episode(job, sha256_file(audio))
state.episode = episode; write_episode; set_episode_key; record_share
transcript = transcriber.cached(episode)
if transcript is None:
    if audio is None: set_stage(fetch); audio = fetcher.fetch(episode)
    set_stage(transcribe); transcript = transcriber.transcribe(episode, audio)
else: log "transcript for <key> is cached"
episode-key check and warnings, as today
```

- **The resolver** is called only for `apple_podcasts`. Nothing else reaches it.
- **A URL** is downloaded on every attempt of every job (brief: "downloaded again on each
  request"). The download is never moved into `episodes/<key>/`.
- **An upload** is transcribed in place from `uploads/`.
- **`episode.json`** of a content key is rewritten by each job, so the last writer's title
  wins. It is descriptive only. Pushes and step 25 use the in-memory episode.

### 5.7 Retention

`Pipeline.finish(job, episode)` is `staging.release(job)` followed by `notify`. It runs:

- after `complete` (done);
- in `_finish` when the stored status is not `queued` (failed or skipped);
- in `Worker.run_once` for each job that `requeue_stale` failed, instead of the bare
  `notify`.

A transient failure that requeues keeps the staged audio for the retry. A lost lease touches
nothing.

**Uploaded and downloaded audio is deleted at every terminal state, whatever
`EARMARK_KEEP_AUDIO` says** (8.3). Podcast audio is unchanged: `CachedTranscriber` deletes it
after transcription unless `KEEP_AUDIO`, or when it proves undecodable.

**Undecodable audio.** The real engine raises `UnusableAudioError` ("could not decode
`<upload_id>.<ext>`: …"), a `PermanentError`. The job fails on its first attempt, and
`finish` deletes the staged file.

`load_episode(job)`, which the worker uses for stale-failed jobs:

- **Podcasts:** unchanged.
- **Uploads:** `audio_episode(job, job.content_sha256)`.
- **URLs:** read `episode.json` by key and replace its title with `audio_title(job)`; `None`
  when the key is unset.

### 5.8 Startup sweeps

- **The API.** `create_app` runs `sweep_uploads(store.active_upload_files())`. It removes
  partial uploads left by a crash, and complete files no active job references (a crash
  between commit and insert, or a failed release). It keeps files of queued and running
  jobs.
- **The worker.** `build_worker` runs `sweep_downloads(jobs.running_job_ids())` after
  opening the store. At that moment a crashed job is still `running` with a stale heartbeat,
  so its partial download is kept. `requeue_stale` then requeues it, and the retry's
  `clear_download` removes the partial. If it fails instead, `finish` releases it.
- **Limitation.** A `.part` written by `earmark submit FILE` while the API is starting can be
  swept. The CLI then fails at `commit()` with an `OSError`, which exits 1. This is rare and
  documented in code.

### 5.9 De-duplication

- **Same request.** The same `(source, subject, to, instructions)` returns the active job,
  or the newest `done` job whose `updated_at` is within the window. `created` is false, and
  the status may be `done`.
- **Not deduped.** A `failed` or `skipped` job is never returned, so sharing again after a
  failure retries (8.2).
- **Titles.** A change of `title` alone returns the same job.
- **Normalisation.** `instructions` are compared after stripping. `to` is compared
  normalised.
- **The upload's copy.** A deduped upload deletes its own staged file before the 202.
- **Across sources.** The same bytes uploaded and also given as a URL are different requests,
  because the URL's bytes are unknown at enqueue time. They then share one transcript
  through the content key.

### 5.10 Transcript cache and profiles

- **The sequence.** A `tiny` transcript, then a `large-v3` worker: `cached()` misses, the
  podcast audio is fetched again (it was deleted), and the `large-v3` transcript replaces the
  file. Switching back re-transcribes.
- **Old transcripts.** A transcript from before this step (`asr_profile` null) is a miss.
- **`earmark transcribe`** writes nothing and is unaffected.

### 5.11 Pushes (ntfy `compose`)

The label stands for the episode name in titles: `Transcribed: <label>`,
`Failed: <label>`, `Skipped: <label>`. The first message line names the source.

| Source | Label | First message line |
|---|---|---|
| `apple_podcasts` | `episode.title`, or `podcast episode` | the show, or `Apple Podcasts episode` (unchanged) |
| `upload` | `job.title`, else `episode.title`, else the stem of `upload_name`, else `uploaded audio` | `Uploaded audio` |
| `url` | `job.title`, else `audio from <url_host(job.url)>`; never `episode.title`, which can come from the URL path | `Audio from <url_host(job.url)>` |

Then the existing duration, warning, stage, reason and attempts lines follow. The `Reason:`
and warning lines additionally pass through `log.scrub_urls`. Nothing else changes:

- every value is still cleaned and redacted;
- there is no `click`;
- the push carries no `to`, instructions or URL path. The step-25 push adds the masked
  address.

### 5.12 Logs

`Intake` logs one INFO line per request:

```
enqueued job <id>: <subject>, to <mask_address(to)>, instructions <given|none> (created=<bool>)
```

The subject is one of:

- the canonical Apple URL, built from ids, as today;
- `audio URL on <url_host>`;
- `upload of <size> bytes`.

Never log `to` unmasked, `instructions`, `title`, the upload name, or a direct URL beyond its
host. `HttpAudioFetcher.download` logs the host only (4.6). The upload reader logs rejections
at INFO with its own fixed reason text only.

### 5.13 The `all` warning

When `recipient_policy().allow_all`, `create_app` logs one WARNING:

```
EARMARK_ALLOWED_RECIPIENTS=all: any address is accepted; anyone with the API token can have
earmark send transcripts to any address
```

It is on one line. Only `serve` logs it; the worker does not need the policy until step 25.

### 5.14 The view model

- **`to`** is shown in full.
- **`instructions`** is cut to 199 characters plus `…` when longer than 200, so the result is
  exactly 200 characters. Code points are counted, not bytes.
- **`url`.** A direct URL is shown as scheme, host (lower-case; IPv6 in brackets), optional
  port, and path. The query, fragment and userinfo are dropped, and the path is kept as sent.
  The canonical Apple URL is shown whole, because its `?i=` is earmark-built and identifies
  the episode.
- **`error` and `warnings`** pass through `scrub_urls`.

The CLI `jobs` output uses the same view.

### 5.15 Docs

**README** (only what the doc tests read):

- **Quickstart.** Add `export EARMARK_ALLOWED_RECIPIENTS=you@example.org`. Change the ingest
  `curl` to `POST http://127.0.0.1:8787/transcriptions` with
  `-d '{"url": "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981", "to":
  "you@example.org", "instructions": "Which papers are mentioned?"}'`.
- **Settings.** Three rows for the new settings in the settings table, under a new
  `### Requests` heading.
- **Commands.** The `submit` row becomes ``| `submit <url-or-file> --to <address>
  [--instructions] [--title]` | Enqueue a transcription, the same way `POST /transcriptions`
  does. |``.
- **HTTP API.** The sentence names `POST /transcriptions`.
- **Everything else** in the README waits for step 27.

**`docs/shortcut.md`** (keep the marker names `ingest-request-example` and
`ingest-response-example`; step 27 rewrites the guide):

- **Request example:**
  `{"url": "https://podcasts.apple.com/us/podcast/the-daily/id1200361736?i=1000802907981",
  "to": "you@example.org", "instructions": "Which studies are cited, and who ran them?"}`.
- **Action 4 (Ask for Input).** The prompt becomes "Instructions for this transcript
  (optional)". The text still says "optional".
- **Action 5 (Get Contents of URL).** The URL ends in `/transcriptions`. The JSON fields
  become `url` = Item from List, `to` = your address (Text), and `instructions` = Provided
  Input. Keep the sentence `` `Authorization` is the only header to add ``, the
  `Content-Type` remark, and exactly two `Authorization` mentions.
- **The laptop `curl`** points at `/transcriptions`.
- **Error table.**
  - The unknown-field row becomes `unknown field 'x' (allowed: url, to, instructions, title,
    timestamp)`.
  - Replace the `Only Apple Podcasts episode links are supported.` row, which the API can no
    longer return, with `to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)`.
- **Everything else** in the guide waits for step 27.

**`tests/test_docs.py` is updated to match:**

- `_post` posts to `/transcriptions`, with `allowed_recipients=("you@example.org",)` in its
  settings.
- The quickstart and Shortcut examples validate as `TranscriptionRequest`.
- The Shortcut example's keys are `{"url", "to", "instructions"}`.
- The POST action must mention `` `url` ``, `` `to` `` and `` `instructions` ``.
- The quickstart must export `EARMARK_ALLOWED_RECIPIENTS`.

### 5.16 Removed names

The grep in 7.2 lists every name this step removes: `IngestRequest`, `enqueue_ingest`,
`normalize_prompt`, `MAX_PROMPT_CHARS`, `INGEST_ERROR_TYPES`, `IngestResponse` and
`earmark.ingest`. It also covers `/ingest` in `src/` and `tests/`, and `prompt` as a job
field or CLI option.

## 6. Tests

### 6.1 Fixtures and helpers

All fixtures are synthetic and built in the test. Nothing is downloaded.

- **`tests/asgi_helpers.py`.**

  ```python
  call_asgi(app, *, method, path, headers, chunks, disconnect_after=None, on_receive=None) -> AsgiResult
  ```

  - It runs one request with `anyio.run`.
  - `chunks` are delivered as `http.request` messages, with `more_body` true except on the
    last.
  - After `disconnect_after` messages it returns `{"type": "http.disconnect"}`.
  - `on_receive(n)` runs before message `n` is returned, so a test can look at
    `uploads/` mid-stream.
  - It returns `(status, headers, body, receive_calls)`.
- **`tests/multipart_helpers.py`.** `multipart_body(parts, boundary=b"earmark-test-boundary")`
  builds exact bytes. A `Part` is `(name, data, filename=None, content_type=None)`.
- **Audio bytes.** `b"ID3" + bytes(n)`. The fakes never decode.
- **Distinctive tokens for the leak tests.**
  - instructions: `INSTR-TOKEN-5c1e summarise the budget`;
  - to: `zelda.secret@example.org`, masked `z…@example.org`;
  - direct URL: `https://audio.example.net/private/PATH-TOKEN-91d2.mp3?sig=QUERY-TOKEN-a77b`;
  - upload filename: `FILE-TOKEN-3b8f.m4a`.
- **Settings in tests:**
  `Settings(_env_file=None, api_token=…, data_dir=…, allowed_recipients=("you@example.org", "zelda.secret@example.org"))`.
- **Shared builders.** `tests/factories.py` gets `new_podcast_job(url=CANON,
  to="you@example.org", **kw) -> NewJob`, so `store.enqueue(URL)` call sites become
  `store.enqueue(new_podcast_job(URL))`.

### 6.2 Brief acceptance criteria → tests

| # | Criterion | Tests (file::name) |
|---|---|---|
| A1 | Each input form creates a job, and each transcribes on fakes | `test_e2e.py::test_podcast_request_transcribes_on_fakes`, `::test_direct_url_request_transcribes_on_fakes`, `::test_upload_request_transcribes_on_fakes`. Each one POSTs through `TestClient`, runs `worker.run_once()`, and checks `done`, `result.duration_s`, `transcript.json` under `episodes/<key>/` and the push call. The URL test also asserts `resolver.calls == []` and `fetcher.downloads == 1`. The upload test asserts `resolver.calls == []` and `fetcher.call_count == 0`. |
| A2 | Missing, invalid or not-allowlisted `to` → 422 with a showable message | `test_api.py::test_to_missing_422`, `::test_to_invalid_422_does_not_echo` (parametrized: `"not-an-address"`, `"a@b"`, `"a@b.org, c@d.org"`, `"a b@c.org"`, `"a@b.org\r\nBcc: x@y.org"`; the detail contains none of the input), `::test_to_not_allowlisted_422`, `::test_allowlist_unset_refuses_everything`, `::test_upload_to_not_allowlisted_422_and_staging_empty` |
| A3 | A bad URL or a show link → 422 with the existing messages | `test_api.py::test_bad_urls_422` (parametrized: `"not a url"` → `MESSAGES["malformed"]`, `ftp://…` → malformed, an Apple show link → `show_link`, an Apple non-episode → `not_episode`, `https://music.apple.com/…` → `not_episode`, `https://user:pw@cdn.example/a.mp3` → `credentials`), `test_sources.py::test_parse_source_url_cases` |
| A4 | Oversize upload → 413, by declared length and by bytes read | `test_upload.py::test_declared_length_over_cap_413_without_reading` (`call_asgi`, receive calls == 0, `uploads/` absent), `::test_file_one_byte_over_cap_413` (exact boundary: cap passes, cap+1 is 413), `::test_lying_content_length_caught_by_bytes_read` (declares 100, sends more), `::test_chunked_upload_without_length_over_cap_413` |
| A5 | A partial or rejected upload leaves nothing in staging | `test_upload.py::test_client_disconnect_mid_file_leaves_nothing`, `::test_truncated_body_422_leaves_nothing`, `::test_malformed_multipart_422_leaves_nothing`, `::test_not_audio_415_leaves_nothing`, `::test_validation_failure_after_file_deletes_staged` (file first, then a bad `to`), `::test_store_error_503_deletes_staged` (a store whose `enqueue` raises `sqlite3.OperationalError`), `::test_dedupe_hit_deletes_second_copy`. Each asserts `list(uploads_dir.iterdir()) == []`, or exactly the one kept file. |
| A6 | Auth is checked before the body is read | `test_upload.py::test_auth_before_body_multipart` and `test_api.py::test_auth_before_body_json` (`call_asgi` without a token: 401, receive calls == 0, no `uploads/`), plus the kept `test_auth_runs_before_body_parsing` moved to `/transcriptions` |
| A7 | A repeat within the window returns the same job | `test_jobs.py::test_dedupe_active_returns_same`, `::test_dedupe_done_within_window_returns_same`, `::test_dedupe_done_after_window_creates_new` (`MutableClock.advance(601)`), `::test_dedupe_window_zero_active_only`, `test_api.py::test_double_tap_returns_same_job` |
| A8 | A new `to` or new `instructions` makes a new job | `test_jobs.py::test_new_to_new_job`, `::test_new_instructions_new_job`, `::test_title_change_same_job` |
| A9 | That new job downloads a podcast once and transcribes once; a direct URL is downloaded again and not transcribed again | `test_runner.py::test_reshare_podcast_new_to_no_refetch_no_retranscribe` (`fetcher.computed == 1`, `transcriber.computed == 1`, resolver calls 2), `::test_reshare_url_new_to_redownloads_not_retranscribed` (`fetcher.downloads == 2`, `transcriber.computed == 1`) |
| A10 | Two direct URLs serving the same bytes share one cached transcript | `test_runner.py::test_two_urls_same_bytes_share_transcript` (same payload via `FakeAudioFetcher(payloads=…)`; both jobs have the same `episode_key`; `transcriber.computed == 1`), `::test_two_urls_different_bytes_differ` |
| A11 | Audio from a failed upload is deleted | `test_runner.py::test_failed_upload_audio_deleted` (`transcriber.fail_next(UnusableAudioError(...))` → `failed` after one attempt, staged file gone), `::test_transient_exhausted_upload_audio_deleted`, `::test_transient_requeue_keeps_upload_audio`, `::test_done_upload_audio_deleted`, `::test_url_download_deleted_on_done_and_failed`, `test_worker.py::test_stale_failed_upload_audio_deleted` |
| A12 | `instructions` never in logs or pushes | `test_leaks.py::test_instructions_never_logged`, `::test_push_carries_no_instructions` |
| A13 | `to` in logs only masked | `test_leaks.py::test_recipient_masked_in_logs` |
| A14 | A direct URL in logs and pushes host-only | `test_leaks.py::test_direct_url_host_only_in_logs` (fakes and, separately, the real `HttpAudioFetcher` on `tests/http_helpers.mock_fetcher`), `::test_direct_url_host_only_in_push` |
| A15 | Pushes: no instructions, no URL path or query, no unmasked address | `test_leaks.py::test_push_has_no_instructions_url_path_or_address`, parametrized over source (podcast, upload, url) × status (done, failed). It captures the body bytes sent through `httpx2.MockTransport` by the real `NtfyNotifier`, and asserts that none of the tokens and no `@` appear. |
| A16 | `all` accepts any address and logs the warning at startup | `test_api.py::test_allow_all_accepts_any_address`, `::test_allow_all_logs_warning_at_startup` (caplog: one WARNING naming `EARMARK_ALLOWED_RECIPIENTS=all`), `test_doctor.py::test_doctor_recipients_all_warns` |

### 6.3 Criteria this plan adds → tests

| # | Criterion | Tests |
|---|---|---|
| P1 | Uploads stream to `data/uploads/`, never the system temp dir, and are not held in memory | `test_upload.py::test_upload_streams_to_staging_while_receiving` (`on_receive` sees the `.part` grow by the previous chunks before the next is delivered); `::test_upload_never_uses_system_tempdir` (`tempfile.tempdir` and `TMPDIR` pointed at an empty dir that stays empty; `tempfile.SpooledTemporaryFile`, `TemporaryFile`, `NamedTemporaryFile` and `mkstemp` monkeypatched to raise); `::test_upload_memory_bounded` (`tracemalloc`: a 16 MiB upload in 64 KiB chunks peaks below 2 MiB) |
| P2 | The fields may arrive after the file part | `test_upload.py::test_fields_after_file_accepted`, `::test_fields_before_file_accepted` |
| P3 | Multipart shape rules | `test_upload.py::test_unknown_field_422`, `::test_duplicate_field_422`, `::test_text_field_with_filename_422`, `::test_two_file_parts_422`, `::test_no_file_part_422`, `::test_empty_file_422`, `::test_field_too_long_422`, `::test_too_many_parts_422`, `::test_field_not_utf8_422`, `::test_missing_boundary_422`, `::test_url_field_in_form_is_unknown` |
| P4 | The audio check is content type or extension only | `test_sources.py::test_upload_extension` (parametrized: `.m4a` with `application/octet-stream` → `m4a`; no name with `audio/mpeg` → `mp3`; `audio/x-unknown` → `bin`; `.txt` with `text/plain` → `None`; `.mp3` with `text/plain` → `mp3`), `test_upload.py::test_not_audio_415_leaves_nothing` |
| P5 | The upload key is `file_key` of the bytes | `test_sources.py::test_content_key_matches_file_key`, `test_upload.py::test_upload_job_hash_matches_file`, `test_runner.py::test_upload_episode_key_is_file_key` |
| P6 | Episode generalisation | `test_models.py::test_audio_episode_key_rule` (a bad guid or show id fails), `::test_source_values`, `::test_job_source_requirements` (parametrized over the three sources) |
| P7 | The transcript cache key includes backend, model and options | `test_transcribe_base.py::test_profile_mismatch_is_a_miss` (stub engine; a transcript with another profile is re-transcribed), `::test_profile_match_is_a_hit`, `::test_legacy_transcript_without_profile_is_a_miss`, `::test_written_transcript_carries_profile`, `test_faster_whisper.py::test_profile_includes_model_and_options` (`tiny` ≠ `large-v3`; language and compute type change it), `test_remote_asr.py::test_profile_includes_model_and_language`, `test_runner.py::test_tiny_transcript_not_reused_for_large` (two fake transcribers with different models) |
| P8 | Nothing but Apple links reaches the resolver | `test_runner.py::test_resolver_not_called_for_upload_or_url` |
| P9 | Startup sweeps | `test_staging.py::test_sweep_uploads_keeps_active_removes_rest`, `::test_sweep_downloads_keeps_running_only`, `test_api.py::test_create_app_sweeps_stale_uploads` (a `.part` and an orphan removed; the file of a queued job kept), `test_factory.py::test_build_worker_sweeps_downloads` |
| P10 | Staging writer | `test_staging.py::test_writer_commit_names_and_mode` (0o600, name pattern, sha256 and size), `::test_writer_abort_and_context_exit_remove_part`, `::test_upload_path_rejects_bad_names`, `::test_release_per_source`, `::test_release_never_raises` |
| P11 | Recipients | `test_recipients.py::test_normalize_address_valid` (parametrized), `::test_normalize_address_invalid` (CR/LF, comma, space, quotes, `<>`, IDN, IP literal, 65-char local part, 255 total), `::test_domain_case_insensitive_local_exact`, `::test_mask_address`, `::test_policy_from_setting`, `test_config.py::test_allowed_recipients_parsing` (comma, spaces, trailing comma, duplicates, `ALL`, `all,x@y.org` refused, a bad entry named by number with no value in the message), `::test_allowed_recipients_redacted_display_masked` |
| P12 | API view model | `test_api.py::test_job_view_fields` (exactly the `JobView` fields; no `heartbeat_at`, `upload_file` or `content_sha256`), `::test_view_instructions_truncated_to_200`, `::test_view_direct_url_scheme_host_path_only`, `::test_view_podcast_url_whole`, `::test_view_errors_scrubbed`, `::test_list_jobs_returns_views` |
| P13 | JSON body cap and content types | `test_api.py::test_json_over_64k_413_declared_and_streamed`, `::test_json_max_valid_body_accepted`, `::test_unsupported_content_type_415`, `::test_empty_json_body_422`, `::test_validation_messages` (updated table), `::test_ingest_route_gone_404`, `::test_routes_are_exactly_the_kept_ones` (updated) |
| P14 | Migration 0002 | `test_db.py::test_migration_0002_from_v1` (a DB created with `MIGRATIONS[:1]` and a row with `prompt="p"` becomes version 2 with `instructions == "p"`, no `prompt` column, `source == 'apple_podcasts'`, `recipient` NULL, and the row loads as `Job` with `to is None`), `::test_source_check_constraint`, updated `SCHEMA_VERSION == 2` assertions |
| P15 | CLI | `test_cli.py::test_submit_url`, `::test_submit_file_copies_to_staging`, `::test_submit_file_not_audio_exit_2`, `::test_submit_file_over_cap_exit_2_nothing_staged`, `::test_submit_recipient_refused_exit_2`, `::test_submit_requires_to`, `::test_jobs_json_prints_views` |
| P16 | Doctor and config | `test_doctor.py::test_doctor_recipients_unset_fails`, `::test_doctor_recipients_list_ok_masked`, `::test_doctor_recipients_all_warns`; existing "healthy" doctor tests set `allowed_recipients` |
| P17 | Push labels | `test_ntfy.py::test_compose_upload_labels`, `::test_compose_url_label_uses_host_not_path`, `::test_compose_url_with_title`, `::test_compose_failed_before_episode_for_audio_sources`, `::test_compose_reason_scrubbed` |
| P18 | HTTP fetcher download | `test_audio.py::test_download_writes_destination_and_logs_host_only`, `::test_download_refuses_textual_type` (reuses `fetch.py` behaviour), `::test_http_fetcher_satisfies_protocol` (updated) |
| P19 | Doc tests | `test_docs.py` as in 5.15 |

**How the leak tests capture output.**

- **Logs.** `configure_logging("DEBUG", (), stream=io.StringIO())`, under the existing
  `restore_root_logging` and `restore_library_loggers` fixtures, so the production formatter
  runs. A test then drives the API with `TestClient` (a JSON URL request and a multipart
  upload) and the worker to `done`. It runs one more job to `failed` through
  `fetcher.fail_next(PermanentFetchError("audio URL on audio.example.net returned 404",
  host=…))`. Then it searches the captured text.
- **The masking test** asserts `zelda.secret` is absent and `z…@example.org` is present.
- **The URL test** asserts `PATH-TOKEN`, `QUERY-TOKEN` and `/private/` are absent and
  `audio.example.net` is present.

## 7. Verification

### 7.1 The gate

```bash
cd /home/sandy/dev/earmark
uv sync --locked
make check
GITHUB_ACTIONS=true make check
uv tree --locked | grep -i multipart          # python-multipart v0.0.34 (or newer), a direct dependency
```

### 7.2 Greps (each must print nothing)

```bash
grep -rnE 'IngestRequest|enqueue_ingest|normalize_prompt|MAX_PROMPT_CHARS|INGEST_ERROR_TYPES|IngestResponse|earmark\.ingest|from earmark import ingest' src tests
grep -rn '"/ingest"\|/ingest\b' src tests
grep -rnE '\bprompt\b' src | grep -v -i 'ask for input'
grep -rnE '\.prompt\b|prompt=' tests
grep -rnE 'request\.form\(|UploadFile|SpooledTemporaryFile|File\(|Form\(' src
grep -rn 'tempfile' src/earmark/staging.py src/earmark/api/upload.py src/earmark/submission.py
grep -rn 'max_body_size' src/earmark/api/routes.py
```

### 7.3 Live smoke on fakes (no network)

Run from a scratch directory under the scratchpad. Use `--form-string` for the text values.

```bash
cd "$(mktemp -d -p "$SCRATCH")"   # SCRATCH = the session scratchpad; never /tmp directly
export EARMARK_API_TOKEN=smoke-token EARMARK_DATA_DIR="$PWD/data" \
  EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_ASR_BACKEND=fake \
  EARMARK_NOTIFY_BACKEND=fake EARMARK_ALLOWED_RECIPIENTS=you@example.org EARMARK_MAX_UPLOAD_MB=1
uv run --project /home/sandy/dev/earmark earmark doctor        # recipients ok, fakes warn
uv run --project /home/sandy/dev/earmark earmark serve > serve.log 2>&1 &
curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
H='Authorization: Bearer smoke-token'
curl -s -H "$H" -H 'Content-Type: application/json' http://127.0.0.1:8787/transcriptions \
  -d '{"url": "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981", "to": "you@example.org", "instructions": "SMOKE-INSTR"}'
curl -s -H "$H" -H 'Content-Type: application/json' http://127.0.0.1:8787/transcriptions \
  -d '{"url": "https://audio.example.net/p/SMOKE-PATH.mp3?sig=SMOKE-Q", "to": "you@example.org"}'
head -c 2048 /dev/zero > clip.mp3
curl -s -H "$H" http://127.0.0.1:8787/transcriptions -F 'file=@clip.mp3;type=audio/mpeg' \
  --form-string to=you@example.org --form-string title='Smoke upload'
uv run --project /home/sandy/dev/earmark earmark worker --once   # x3: each "job <id> done"
uv run --project /home/sandy/dev/earmark earmark worker --once
uv run --project /home/sandy/dev/earmark earmark worker --once
curl -s -H "$H" http://127.0.0.1:8787/jobs                       # three done views; URL without ?sig
ls -A data/uploads data/downloads                                 # both empty
head -c 2097152 /dev/zero > big.mp3
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://127.0.0.1:8787/transcriptions \
  -F 'file=@big.mp3;type=audio/mpeg' --form-string to=you@example.org   # 413
curl -s -H "$H" -H 'Content-Type: application/json' http://127.0.0.1:8787/transcriptions \
  -d '{"url": "https://audio.example.net/a.mp3", "to": "stranger@example.com"}'  # 422 not allowed
ls -A data/uploads                                                # empty
grep -c 'SMOKE-INSTR\|SMOKE-PATH\|SMOKE-Q\|you@example.org' serve.log   # 0
kill %1; wait
```

Expected results:

- the three POSTs answer 202;
- `worker --once` prints `done` three times;
- the views show `to` in full, and the direct URL as `https://audio.example.net/p/SMOKE-PATH.mp3`
  (path kept, query dropped);
- the server log names `y…@example.org` and `audio.example.net`.

### 7.4 Not verifiable here

- A real iPhone Shortcut upload.
- Caddy forwarding a large multipart body.
- An upload of hundreds of megabytes over the tunnel.

The streaming, the caps and the cleanup are proven in-process only. Step 27 documents the
Shortcut's file field and the proxy.

## 8. Deviations and decisions

Each of these is recorded in `docs/ARCHITECTURE.md` (section 8.14 lists the edits).

1. **`timestamp` is kept** in the JSON body and as a form field. The brief's field list omits
   it. It was `IngestRequest`'s, it still sets `shared_at`, and step 25 may take the
   requester's offset from it. Removing it would take away an option no step asked to remove.
2. **The dedupe window applies to `done` jobs only.** "For `EARMARK_DEDUPE_WINDOW_S` after it
   finishes" could also cover `failed` and `skipped`. The brief's purpose ("so a double tap
   sends one email") concerns jobs that delivered. Returning a failed job would make
   re-sharing useless as a retry for ten minutes. A double tap during a failing job is still
   deduped, because the job is active then. Changing this is one SQL clause.
3. **`EARMARK_KEEP_AUDIO` does not keep uploaded or downloaded audio.** The brief says that
   audio is deleted at a terminal state, and that podcast audio keeps today's behaviour.
   Uploads may be private recordings, so the setting stays podcast-only.
4. **Uploaded and downloaded audio is transcribed from its per-job staging file**
   (`uploads/<id>.<ext>`, `downloads/<job_id>.<ext>`) and never moved into
   `episodes/<key>/`. Jobs that share a content key then cannot delete each other's audio.
   `Transcriber` and `AudioFetcher` still work on an `Episode`.
5. **Contract changes.**
   - `AudioFetcher` gains `download(url, destination)`, so fakes can stand in for the direct
     download.
   - `Transcriber` gains `cached(episode)`, so the runner can ask for a profile-aware hit
     before fetching.
   - `TranscriptionEngine` gains `profile`.
   - `Pipeline` gains a `staging` keyword.
   - `build_router` takes an `Intake`.
6. **The cache key is a profile stamp.** The transcript cache key is implemented as
   `Transcript.asr_profile` and compared on read. A miss overwrites the one transcript an
   episode has on disk, rather than keeping one transcript per profile. This satisfies "never
   reused for `large-v3`", keeps the data layout, and keeps one `transcript.txt`. Switching
   profiles back and forth re-transcribes.
7. **`JobKind` stays `ingest`.** The new meaning travels in `source`. Changing the CHECK
   would need a table rebuild in SQLite.
8. **URL rules the brief does not spell out.**
   - A direct URL needs an explicit `http://` or `https://`. Without a scheme, only an Apple
     link is accepted.
   - Other `*.apple.com` hosts keep the `not_episode` message.
   - A URL with userinfo gets a new message (`credentials`).
   - A direct URL gets no audio check at the API, because there is no declared type without
     a request, and the handler makes none. The download's textual-type refusal and decoding
     are the checks.
9. **Statuses and doctor.**
   - 415 answers a request content type that is neither JSON nor multipart, and a non-audio
     upload. 422 stays for invalid content.
   - Doctor's `recipients` check FAILs when the allowlist is unset, because every request
     would be refused.
10. **Migration `0002` is appended**, rather than editing `0001`, under the append-only rule.
    Rows from before step 24 keep `to` NULL.
11. **Only `serve` logs the `all` warning.** The worker reads no recipient setting until step 25.
12. **The allowlist is masked** in `earmark config` and doctor output. The brief does not
    require it, and it costs nothing.
13. **The open questions the architecture gave to this step:**
    - `MAX_INSTRUCTIONS_CHARS` is 4000, the old prompt cap, about 700 words, and keeps the
      64 KiB JSON cap valid.
    - `MAX_TITLE_CHARS` is 200, the contract's title cap.
    - `EARMARK_MAX_UPLOAD_MB` defaults to 1024, the same as `EARMARK_MAX_AUDIO_MB`.
    - The synthetic show id is `audio`.
    - The job stores its request through migration `0002`, with the column `recipient`.
    - `prompt` is replaced by `instructions`, and existing prompts are carried over.
    - For uploads and URLs, the resolve stage builds the episode from the job and the bytes
      (5.6).
14. **Edits to `docs/ARCHITECTURE.md` made with this plan.**
    - **Moved into the current part:** step 24's subsections of "Planned", rewritten as
      current text under Flow, Module layout, Domain models, Stage contracts, Runner,
      Fetching, Transcribing, Notifying, Doctor, Jobs, Data layout, HTTP API, Configuration,
      Security and Testing.
    - **Renamed:** "Planned (steps 24–26)" is now "Planned (steps 25–26)".
    - **Answered:** the step-24 open questions are recorded.
    - **Moved to "Delivery (step 25)":** the step-25 leak rules.
    - **Added:** the note that step 25 must fail a job without `to`.
15. **Outside `docs/`.** `CLAUDE.md`'s "Shape of the code" lines (3.2) are the implementer's
    change, because the planner edits only `docs/`.

## 9. Out of scope

- **Step 25:** mail sending, the note and instructions files, SMTP settings, the delivery
  state, the `Mailer` stage, and the time zone question. It also covers the push that says
  "sent" with the masked recipient, the step-25 leak rules, and failing pre-step-24 jobs that
  have no `to`.
- **Step 26:** `EARMARK_ASR_BATCH_SIZE`, which joins the profile then, and GPU anything.
- **Step 27:**
  - the Shortcut rewrite (the "Choose from Menu" for `to`, the file upload in the Shortcut);
  - the README beyond the doc-tested parts;
  - `docs/deploy.md`, the runbook, and the Caddy notes on large request bodies.
- **Backlog:** a send-rate cap, re-shares as a reference only, other sources and video
  files.
