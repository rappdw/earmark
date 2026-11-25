# Step 34: The CLI as an API client

The brief is in `docs/PLAN.md`, "Step 34". It builds on step 04 (the API and its bearer
middleware), step 24 (`submit`, `jobs`, `JobView`, uploads), step 15 (doctor) and step 33
(`GET /jobs/{id}/transcript` and `earmark transcript`, shaped so this client can print the
route's 200 body and `detail` as the local command does).

The step can be built as briefed. One choice the brief leaves to the planner changes how the
product behaves, so it is stated first:

- **Decision P1 (product): the containers ignore `EARMARK_API_URL`; they do not refuse it.**
  `compose.yaml` sets `EARMARK_API_URL: ""` for both services, so every command run in a
  container (`docker compose exec api earmark jobs`) is local whatever `.env` says, and
  `serve` and `worker` never read the setting anywhere. A stray line in `.env` therefore
  cannot stop the service at its next restart, and cannot turn an in-container command into a
  client of a URL that may not resolve inside the container. The docs still say the setting
  belongs in the operator's shell. Section 8.1 has the alternative (refuse) and why it was
  not taken.

The other choices are engineering ones (section 8): one job is `earmark jobs <job_id>` (D2);
plain HTTP is allowed to loopback IP literals only, checked when settings load (D3, D4);
`doctor --online` checks `/healthz` only (D7).

Rules for the implementer that are not about the code itself:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Leave `deploy/caddy/install-cert.sh` alone**, an untracked file of the operator's (step
  35).
- **Use a `TMPDIR` under `~/.cache/`** for anything that writes temp files:
  `export TMPDIR=/home/sandy/.cache/earmark-step34-tmp && mkdir -p "$TMPDIR"`, and
  `rm -rf` it when done. `/tmp` is a small tmpfs. Never install anything into the repo
  `.venv`; a scratch venv goes under `TMPDIR`. Never run `pytest -m network`.
- **Run both gates:** `make check` and `GITHUB_ACTIONS=true make check` (CI forces colour
  in typer's output; see `tests/test_cli.py::test_submit_requires_to`).
- **Placeholders only** in every file this step writes: addresses at `example.org`,
  `example.net` or `example.com`, `earmark.example.com` for the API's name, `127.0.0.1` for
  loopback, `192.168.1.10` for a LAN address, `<…>` for the token in shell examples
  (`test_docs_use_placeholders_only` checks `export EARMARK_API_TOKEN=` lines). No real host
  name, LAN address or token.
- **`ruff format --check` covers Python code blocks in Markdown.** Keep any ```` ```python ````
  fence in `README.md` or `docs/deploy.md` ruff-formatted, or use `bash`.
- **`docs/ARCHITECTURE.md` is already updated** by this plan (section 8.8). Check the code
  against it. Change it only if the code had to differ, and then say so.
- In `README.md` and `docs/deploy.md` the removed-wording scan bans, among others, `vault`,
  `\bdigest`, `embedding`, `\bclaude\b`, `--prompt` and `"prompt"`.

## 1. Outcome

- **Client mode.** With `EARMARK_API_URL` set, `earmark jobs`, `earmark jobs <job_id>`,
  `earmark submit` (a link, or a local file uploaded as multipart) and `earmark transcript`
  call the running API with `EARMARK_API_TOKEN`, through a new `earmark/api_client.py`, and
  never touch `EARMARK_DATA_DIR`. Their stdout is the local mode's for the same jobs, and an
  API error is printed as `earmark <command>: <the API's detail>` with the local mode's exit
  codes.
- **Transport.** The URL must be `https://`, or `http://` to a loopback IP address; anything
  else is refused when settings load. Certificates are always verified, redirects are not
  followed, and the token never reaches output, logs or errors.
- **No empty database by accident.** `jobs` and `transcript` in local mode open the job
  database only if it exists. Otherwise they say so, name `EARMARK_DATA_DIR`, suggest
  `EARMARK_API_URL` or `docker compose exec api earmark …`, create nothing and exit 1.
  `serve`, `worker` and `submit` still create it.
- **Doctor** gains a `client mode` line and, with `--online`, an `api server` line
  (`GET /healthz`).
- **Containers** ignore the setting (P1). `submit --instructions` says the instructions go to
  the PKA with the transcript.
- **Docs.** `docs/deploy.md` section 19, "The command line" (containers, the Spark's host,
  the Mac), the README's commands and settings, `.env.example`.

## 2. Findings

Installed versions (read from the repo venv): httpx2 2.16.2, httpcore2 2.16.2, truststore
0.10.5, fastapi 0.165.0, starlette 1.10.0, pydantic 2.14.4, pydantic-settings (locked),
typer 0.29.2, click 8.5.1. Each finding below was checked with a scratch script under
`TMPDIR`, run with the repo's `.venv/bin/python -I` (nothing installed, nothing written to
the repo).

### 2.1 The CLI today

Read in `cli.py`, `jobs.py`, `db.py` and `tests/test_cli.py` (as of commit `d63d214`).

- `jobs` and `transcript` call `open_job_store(settings)`, which calls `open_database`,
  which runs `path.parent.mkdir(parents=True, exist_ok=True)` and then `sqlite3.connect`,
  which creates the file. That is how the operator's host CLI made an empty `./data/earmark.db`
  and answered "no jobs".
- There is **no single-job command** today; `jobs` only lists. The brief's "a single job" is
  new work (D2).
- `submit` prints `"{job_id} {status}"`, plus `" (existing job)"` when dedupe returned a job.
  That is exactly `TranscriptionResponse(job_id, status, created)` from the API.
- `submit` builds `TranscriptionRequest` or `UploadFields` before opening the store, so a
  validation error exits 2 without creating anything. It never sends a `timestamp`.
- Tests that call `jobs` without a database and expect `"no jobs\n"` break under the new
  rule. Each is listed in section 6.8 with its fix.
- `import earmark.cli` loads neither httpx2 nor fastapi; `earmark.api.schemas` loads neither
  either (checked with `sys.modules`). So the client can parse into `JobView` and friends
  without loading the server.

### 2.2 httpx2's TLS, proxies and errors (checked in the installed package)

- `httpx2.HTTPTransport()` with no `verify` argument builds its context with
  `httpcore2.default_ssl_context()`: on macOS and Windows a `truststore.SSLContext` (the system
  trust store), on Linux an `ssl.SSLContext(PROTOCOL_TLS_CLIENT)` loaded from the compiled-in
  CA paths. The transport's `trust_env` (default `True`) only lets `SSL_CERT_FILE` /
  `SSL_CERT_DIR` replace the CA set. Checked: `HTTPTransport()._pool._ssl_context` has
  `verify_mode == ssl.CERT_REQUIRED` and `check_hostname is True`.
- Proxy variables and `.netrc` are read by `httpx2.Client(trust_env=True)`, so the client is
  built with `trust_env=False`, as `notify/ntfy.py` and `transcribe/remote.py` are.
- A failed certificate check, against a scratch TLS server on loopback with a self-signed
  certificate, raises `httpx2.ConnectError` whose `__cause__` is `httpcore2.ConnectError`,
  whose `__cause__` is `ssl.SSLCertVerificationError`. The client walks that chain to give a
  certificate message (section 5.5).
- httpx2's multipart encoder streams a file object in 64 KiB chunks and sets
  `Content-Length` from the file's size. `files={"file": (name, fileobj)}` guesses the part's
  content type from the name.

### 2.3 Driving the real app without the network (checked)

- **In-process.** `fastapi.testclient.TestClient(app)._transport` is a
  `starlette.testclient._TestClientTransport`, an `httpx2.BaseTransport` (starlette 1.10
  imports httpx2). An `httpx2.Client(base_url="https://earmark.example.com",
  transport=that, trust_env=False, follow_redirects=False)` reached `create_app`'s real
  routes: `/healthz`, `GET /jobs`, a multipart upload (202 with `Location`), a JSON submit
  refused by the allowlist (422 with the allowlist's detail), and a wrong token (401 with
  `WWW-Authenticate`). The scheme is only copied into the ASGI scope, so `https://` URLs work
  in-process with no TLS.
- **Real HTTP/1.1 over a unix socket.** `tests/uds_http.asgi_on_uds(create_app(...,
  max_upload_bytes=1000))` with `httpx2.HTTPTransport(uds=...)` and base URL
  `http://127.0.0.1:8787`. Uploads of 5 KB and of 50 MiB both came back as a clean 413
  `{"detail": "the upload is larger than the 1000 bytes limit (EARMARK_MAX_UPLOAD_MB)"}`:
  the server answering before the body was sent did not surface as a write error.
- **Output parity.** `JobList.model_validate_json(GET /jobs body)` re-dumped with
  `model_dump(mode="json")` equals `job_view(j).model_dump(mode="json")` for the same rows,
  and `json.dumps(..., ensure_ascii=False)` of both is byte-identical. This covered a
  `timestamp` with milliseconds and a `-08:00` offset (pydantic writes `Z` for UTC and keeps
  the offset otherwise, both ways).

### 2.4 Settings and compose (checked)

- `env_ignore_empty=True`: an empty environment variable is unset, **but it does not
  override a `.env` value**. With `EARMARK_ASR_BASE_URL=` in the environment and a value in
  `.env`, the `.env` value won. So `EARMARK_API_URL= earmark jobs` does not turn client mode
  off when `.env` holds the URL; another reason the docs keep the URL out of `.env`.
- In a container there is no `.env` file (the `.dockerignore` allow-list keeps it out of the
  image, and `WORKDIR` is `/home/earmark`), and compose's `environment:` wins over
  `env_file:`. So `EARMARK_API_URL: ""` in compose makes it unset in both containers. This is
  the existing pattern of `EARMARK_ASR_MODEL_DIR: ""`.
- `tests/test_deploy.py::test_compose_forces_container_paths` pins the exact set of
  compose `environment:` keys; `tests/test_config.py::test_settings_fields_are_exactly`
  pins the field order; `test_env_example_lists_every_field` and
  `test_readme_settings_reference_covers_every_field` require the new setting in
  `.env.example` and the README.
- `ipaddress.ip_address("::ffff:127.0.0.1").is_loopback` is `True` on 3.13, as are
  `127.0.0.1`, any `127.*` and `::1`.

### 2.5 Doctor

- `tests/test_doctor.py::test_doctor_recipients_listed_right_after_api_token` pins
  `recipients` right after `api token`, and `test_doctor_check_names_in_order` pins the whole
  offline order. So the new `client mode` check goes **last**, and the second test is updated.
- `probe_models` in `doctor/online.py` is the pattern for a read-only HTTP check with
  `Probes.http_transport`.

### 2.6 Typer (checked)

- An optional positional `job_id: Annotated[str | None, typer.Argument(...)] = None` works
  next to `--limit`, `--status` and `--json`, in either order (`jobs abc --json`,
  `jobs --json abc`). `--limit 0` still exits 2.
- A `limit: int | None` option with `min`/`max` works, which lets the command tell "not
  given" from 20.

### 2.7 Dead ends

- **Do not use `endpoint.error_detail` for the API's errors.** It caps at 200 characters and
  collapses whitespace; the API's validation details reach 301 characters, and the client
  must show them as the local mode does. The client has its own `_detail` (section 4.3).
- **Do not put the token in `httpx2.Client(headers=...)`** as a default header. Send it per
  request, as the ntfy client does.
- **Do not rely on the CLI's log redaction to prove "never logged".** `configure_logging`
  redacts `Settings.secret_values()`, which includes the token, so a CLI-level test would
  pass even if the client logged it. The real leak test drives `ApiClient` with the
  `log_stream` fixture, which redacts nothing (section 6.3).
- **In CLI tests the in-process server logs to the CLI's stderr** (one root handler). Tests
  that compare stderr exactly set `EARMARK_LOG_LEVEL=ERROR` (or `WARNING` where no 401 is
  involved).

No new dependency: httpx2 is already a core dependency. No migration.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/config.py` | `api_url` field after `api_token`; `check_api_url`, `is_loopback_ip`, the message constants (section 4.1). |
| `src/earmark/api_client.py` | **New.** `ApiClient`, `ApiError`, `ClientSetupError`, `default_transport`, `transport_error_message` (section 4.3). Imports httpx2 at module level; imported lazily by the CLI and by doctor only. |
| `src/earmark/db.py` | `MissingDatabaseError(DatabaseSetupError)`. |
| `src/earmark/jobs.py` | `open_existing_job_store`, `NO_DATA_DIR`, `NO_DATABASE` (section 4.2). |
| `src/earmark/cli.py` | Client mode in `jobs`, `submit`, `transcript`; `jobs [JOB_ID]`; read-only local stores; the helpers of section 4.4; the help wording. |
| `src/earmark/doctor/checks.py` | `client_mode` offline check, appended last. |
| `src/earmark/doctor/online.py` | `api_health`, appended last when `EARMARK_API_URL` is set. |
| `compose.yaml` | `EARMARK_API_URL: ""` in the shared `x-earmark` `environment:` anchor, with a comment. |
| `.env.example` | A commented `# EARMARK_API_URL=` line under the API block, with a comment (section 4.6). |
| `README.md` | Commands table (`jobs` row), a client-mode paragraph after the table, the `EARMARK_API_URL` settings row. |
| `docs/deploy.md` | Section 4's "What compose overrides" names `EARMARK_API_URL`; new section 19 (section 4.6). |
| `docs/ARCHITECTURE.md` | Done by this plan (section 8.8). |
| `tests/api_client_helpers.py` | **New.** Shared test helpers (section 6). |
| `tests/test_api_client.py` | **New.** `ApiClient` unit tests. |
| `tests/test_cli_client.py` | **New.** The CLI in client mode, and local/client parity. |
| `tests/test_cli.py` | Local-mode changes: no-database tests, `jobs <id>`, help wording, the fixes of section 6.8, `test_cli_import_is_light` also bans httpx2. |
| `tests/test_config.py` | `api_url` validation; `test_settings_fields_are_exactly` gains `api_url`. |
| `tests/test_jobs.py` | `open_existing_job_store`. |
| `tests/test_doctor.py` | `client mode` and `api server`; the order test. |
| `tests/test_deploy.py` | `test_compose_forces_container_paths` includes `EARMARK_API_URL == ""`. |
| `tests/test_docs.py` | `test_deploy_doc_cli_section`. |
| `tests/test_leaks.py` | `test_api_client_leaks_no_token`. |

`CLAUDE.md`, `docs/PLAN.md` (the orchestrator updates the status table), `docs/shortcut.md`
and `docs/runbook-spark.md` do not change.

## 4. Interfaces

### 4.1 `config.py`

```python
API_URL_SHAPE: Final = (
    "must be the API's root URL, such as https://earmark.example.com or http://127.0.0.1:8787"
)
API_URL_CREDENTIALS: Final = "must not contain credentials; use EARMARK_API_TOKEN"
API_URL_QUERY: Final = "must not contain a query or fragment"
API_URL_PATH: Final = "must be the API's root URL, without a path"
API_URL_PLAIN_HTTP: Final = (
    "plain http is allowed only to a loopback address such as http://127.0.0.1:8787; "
    "use https, or the API token would cross the network unencrypted"
)


def is_loopback_ip(host: str) -> bool:
    """True for an IP literal in 127/8, ::1 or ::ffff:127.*; False for names and others."""


def check_api_url(url: str) -> str:
    """The URL as 'scheme://netloc' (scheme lower-cased); ValueError with one of the
    API_URL_* messages otherwise. Never puts the input in the message."""
```

The new field, after `api_token`:

```python
api_url: str | None = Field(
    None, description="The running API's URL; jobs, submit and transcript then call it."
)
```

and its validator `_check_api_url` (`None` → `None`; strip; empty → `None`; otherwise
`check_api_url(v)`). `check_api_url`, in this order:

1. `urlsplit(url.strip())`, reading `.hostname` and `.port`. A `ValueError` (bad port) →
   `API_URL_SHAPE`.
2. Scheme (lower-cased) not `http`/`https`, or no hostname → `API_URL_SHAPE`.
3. `username` or `password` not `None`, or `@` in the netloc → `API_URL_CREDENTIALS`.
4. A query or fragment, or `?` or `#` anywhere → `API_URL_QUERY`.
5. A path other than `""` or `"/"` → `API_URL_PATH`.
6. Scheme `http` and not `is_loopback_ip(hostname)` → `API_URL_PLAIN_HTTP`.
7. Return `f"{scheme}://{netloc}"`.

A failure is a `ConfigError` line `EARMARK_API_URL: Value error, <message>` (pydantic's
prefix), exit 2 from every command, as for any invalid setting. `earmark config` prints the
URL (it is not a secret; it can hold no credentials). It is not in `secret_values()`.

### 4.2 `db.py` and `jobs.py`

```python
class MissingDatabaseError(DatabaseSetupError):
    """A read-only command found no data directory or no database; str(exc) says which."""
```

```python
NO_DATA_DIR: Final = "EARMARK_DATA_DIR ({path}) does not exist"
NO_DATABASE: Final = "there is no job database in EARMARK_DATA_DIR ({path})"


def open_existing_job_store(settings: Settings, *, clock: Clock = utc_now) -> JobStore:
    """open_job_store for read-only commands: never creates the data directory or the
    database. Raises MissingDatabaseError when either is missing."""
```

- `{path}` is `settings.data_dir.absolute()`.
- `data_dir` does not exist → `NO_DATA_DIR`. `database_path(data_dir)` is not a file
  (including when `data_dir` is a file, or the database path is a directory) →
  `NO_DATABASE`. Otherwise `return open_job_store(settings, clock=clock)`. That may still
  migrate an older schema or switch on WAL, as today; it creates no file the database did not
  already have, apart from SQLite's `-wal` and `-shm`.
- The check-then-open race (someone deletes the file in between) is accepted.

### 4.3 `api_client.py`

```python
"""The CLI's client mode: jobs, submit and transcript over the HTTP API.

The API is an operator endpoint (EARMARK_API_URL): no address guard, no redirects, no proxy
variables, no cookies kept, answers capped, the token only in a per-request Authorization
header, certificates always verified. It logs nothing. Import it only from the CLI's
commands and from doctor: it loads httpx2.
"""

API_TIMEOUT: Final = httpx2.Timeout(60.0, connect=10.0)
MAX_RESPONSE_BYTES: Final = 64 * MIB  # earmark.fetch.MIB
MAX_DETAIL_CHARS: Final = 500
NO_TOKEN: Final = (
    "EARMARK_API_URL is set, so this command calls the API, and that needs "
    "EARMARK_API_TOKEN (the API's token)"
)
BAD_TOKEN: Final = (
    "EARMARK_API_TOKEN must be printable ASCII without spaces, because it is sent in an HTTP header"
)
UNAUTHORIZED_HINT: Final = " (check EARMARK_API_TOKEN)"
REDIRECT: Final = (
    "the API at {host} answered with a redirect (HTTP {status}); redirects are not "
    "followed, check EARMARK_API_URL"
)
HTTP_STATUS: Final = "the API at {host} answered HTTP {status}"
TIMEOUT: Final = "the API at {host} did not answer within {seconds:g} s"
UNREACHABLE: Final = (
    "could not reach the API at {host} ({error}); check EARMARK_API_URL and that the "
    "service is running"
)
CERTIFICATE: Final = (
    "the certificate of {host} did not verify; earmark always checks it, so the server "
    "needs a certificate this machine trusts, with its intermediates"
)
INVALID_URL: Final = "invalid EARMARK_API_URL"
TOO_LARGE: Final = "the API's answer is larger than {limit}"  # limit_text(MAX_RESPONSE_BYTES)
UNREADABLE: Final = (
    "the API at {host} answered with a body earmark cannot read; is it the same version of earmark?"
)
CANNOT_READ_FILE: Final = "cannot read {name}: {reason}"


class ClientSetupError(Exception):
    """Client mode cannot start. str(exc) names settings, never their values."""


class ApiError(Exception):
    """A call failed. ``message`` is safe to show: cleaned, redacted of the token, capped."""

    def __init__(self, message: str, *, status: int | None) -> None: ...

    message: str
    status: int | None  # the HTTP status, or None when there was no usable answer


def default_transport() -> httpx2.BaseTransport:
    """httpx2.HTTPTransport() with its default, verifying TLS context. A seam for tests."""


def transport_error_message(exc: httpx2.HTTPError, host: str, timeout_s: float) -> str:
    """TIMEOUT, CERTIFICATE (an ssl.SSLCertVerificationError anywhere in the
    __cause__/__context__ chain), INVALID_URL, or UNREACHABLE with type(exc).__name__.
    Never str(exc). Shared with doctor."""


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: httpx2.Timeout = API_TIMEOUT,
    ) -> None: ...

    @classmethod
    def from_settings(cls, settings: Settings) -> Self: ...

    @property
    def host(self) -> str: ...  # endpoint.host_label(base_url)

    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...

    def list_jobs(self, *, limit: int, status: JobStatus | None) -> tuple[JobView, ...]: ...
    def get_job(self, job_id: str) -> JobView: ...
    def submit_url(self, request: TranscriptionRequest) -> TranscriptionResponse: ...
    def submit_file(self, path: Path, fields: UploadFields) -> TranscriptionResponse: ...
    def transcript(self, job_id: str, fmt: TranscriptFormat) -> bytes: ...
```

**Construction.**

- `__init__` calls `check_api_url(base_url)`; a `ValueError` becomes
  `ClientSetupError(f"EARMARK_API_URL: {message}")`. This repeats the settings check for
  callers that build the client directly.
- The token: blank after `strip()` → `ClientSetupError(NO_TOKEN)`; any character outside
  `"!"`..`"~"` → `ClientSetupError(BAD_TOKEN)`.
- It builds `httpx2.Client(base_url=<checked url>, transport=transport or
  default_transport(), trust_env=False, follow_redirects=False, timeout=timeout,
  headers={"User-Agent": USER_AGENT})`, with `earmark.fetch.USER_AGENT`. There is no
  `verify` parameter anywhere, and no default `Authorization` header.
- `from_settings(settings)`: `settings.api_url is None` → `ValueError` (a programming
  error). `settings.api_token is None` → `ClientSetupError(NO_TOKEN)`. Otherwise
  `cls(settings.api_url, settings.api_token)`.

**One request** (a private `_call(method, path, *, expected, params=None, json=None,
data=None, files=None) -> bytes`):

1. `client.cookies.clear()`.
2. `with client.stream(method, path, params=..., json=..., data=..., files=...,
   headers={"Authorization": f"Bearer {token}"}) as response:` read chunks. Past
   `MAX_RESPONSE_BYTES`, raise `ApiError(TOO_LARGE…, status=None)`.
3. `httpx2.HTTPError` (timeouts, transport errors, `InvalidURL`, decoding errors) →
   `ApiError(transport_error_message(exc, host, 60.0), status=None)`, raised `from None` (an
   httpx exception's text can quote the URL).
4. Status `expected` → the body. 3xx → `ApiError(REDIRECT…, status=status)`. Anything else →
   the error message below, with `status=status`.

**The error message** of a non-expected answer:

- `detail = _detail(body, content_type)`: when the content type contains `json` or the body
  starts with `{`, parse it. If it is an object whose `detail` is a `str`, take that string,
  replace every non-printable character with `?`, `log.redact` it with the token, and cap it
  at `MAX_DETAIL_CHARS` (499 characters and `…`). Otherwise `None`.
- 401: `(detail or "missing or invalid API token") + UNAUTHORIZED_HINT`.
- otherwise `detail`, or `HTTP_STATUS…` when there is none.

**The calls.**

| Method | Request | Expected | Parsed into |
|---|---|---|---|
| `list_jobs` | `GET /jobs`, `params={"limit": limit}` plus `"status"` when given | 200 | `JobList`, returns `.jobs` |
| `get_job` | `GET /jobs/{job_id}` | 200 | `JobView` |
| `submit_url` | `POST /transcriptions`, `json=request.model_dump(mode="json", exclude_none=True)` | 202 | `TranscriptionResponse` |
| `submit_file` | `POST /transcriptions`, `data=fields.model_dump(mode="json", exclude_none=True)`, `files={"file": (path.name, <path opened "rb">)}` | 202 | `TranscriptionResponse` |
| `transcript` | `GET /jobs/{job_id}/transcript`, `params={"format": fmt}` | 200 | the body bytes as they are |

- `get_job` and `transcript` first check `job_id` against `[0-9a-f]{32}`. Anything else
  raises `ApiError(JOB_NOT_FOUND, status=404)` (`earmark.transcripts.JOB_NOT_FOUND`) without a
  request, as local mode's `store.get` returns `None` for it. So no user text reaches the
  request path.
- A parse failure (`pydantic.ValidationError`, or `ValueError` from JSON) →
  `ApiError(UNREADABLE…, status=<the status>)`, raised `from None`.
- `submit_file`: an `OSError` opening or reading the file → `ApiError(CANNOT_READ_FILE
  .format(name=<cleaned path.name>, reason=exc.strerror or type(exc).__name__),
  status=None)`. The file is closed when the call returns.
- With `exclude_none=True`, the JSON body has `url`, `to`, `instructions` (possibly `""`)
  and `title` only when given. The CLI never sets `timestamp`, as in local mode. The form has
  `to`, `instructions` and `title` when given, and the `file` part.

### 4.4 `cli.py`

**Mode.** `settings.api_url is not None` means client mode for `jobs`, `submit` and
`transcript`. No other command reads `api_url`.

New helpers (private):

```python
NO_DATABASE_HINT: Final = (
    "  To see the running service's jobs, set EARMARK_API_URL (with EARMARK_API_TOKEN) to "
    "call its API, or run the command in its container: "
    "docker compose exec api earmark {command} ..."
)
SUBMIT_REFUSED_STATUSES: Final = frozenset({413, 415, 422})


def _api_client_or_exit(settings: Settings, command: str) -> "ApiClient":
    """ApiClient.from_settings; a ClientSetupError prints 'earmark <command>: <exc>' and
    exits 2."""


def _api_error_exit(
    command: str, exc: "ApiError", *, usage: frozenset[int] = frozenset()
) -> NoReturn:
    """Print 'earmark <command>: <exc.message>' and exit 2 if exc.status is in usage, else 1."""


def _existing_store_or_exit(settings: Settings, command: str) -> "JobStore":
    """open_existing_job_store. MissingDatabaseError prints two lines (below) and exits 1;
    DatabaseSetupError, sqlite3.Error and OSError print 'earmark <command>: cannot open the
    job store: <exc>' and exit 1, as today."""


def _write_transcript(data: bytes) -> None:
    """The local command's output step, shared by both modes (terminal filter or the
    exact bytes)."""
```

The missing-database message, on stderr, two lines:

```text
earmark jobs: EARMARK_DATA_DIR (/home/op/earmark/data) does not exist; nothing was created.
  To see the running service's jobs, set EARMARK_API_URL (with EARMARK_API_TOKEN) to call its API, or run the command in its container: docker compose exec api earmark jobs ...
```

**`jobs`**:

```python
@app.command("jobs")
def jobs_command(
    job_id: Annotated[
        str | None, typer.Argument(help="Show this job only.", show_default=False)
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit", min=1, max=LIST_LIMIT_MAX, help="How many jobs to show (default 20)."
        ),
    ] = None,
    status: Annotated[JobStatus | None, typer.Option("--status", help="Only this status.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """List recent jobs, newest first, or show one job. Calls the API when EARMARK_API_URL
    is set."""
```

1. `_load_or_exit()`, `configure_logging(...)`.
2. A `job_id` with `--limit` or `--status`: stderr `earmark jobs: --limit and --status are
   for the list; leave them out to show one job`, exit 2.
3. Views:
   - Client mode: `with _api_client_or_exit(settings, "jobs") as client:` call
     `client.get_job(job_id)` or `client.list_jobs(limit=limit or 20, status=status)`.
     `ApiError` → `_api_error_exit("jobs", exc)`.
   - Local mode: `store = _existing_store_or_exit(settings, "jobs")`, then
     `store.get(job_id)` (`None` → stderr `earmark jobs: job not found`, exit 1) or
     `store.list_recent(limit or 20, status=status)`, through `job_view`. A `sqlite3.Error`
     or `OSError` while reading → `earmark jobs: cannot open the job store: {exc}`, exit 1, as
     today.
4. Output, the same code for both modes:
   - one job: with `--json`, `json.dumps(view.model_dump(mode="json"), ensure_ascii=False)`
     (the `GET /jobs/{id}` object); otherwise `_job_lines(view)`;
   - a list: unchanged (`{"jobs": [...]}` with `--json`, `no jobs`, or the lines).

**`submit`**:

- Help: SOURCE `"An Apple Podcasts episode link, a YouTube video link, a direct audio URL,
  or a local audio file."`; `--to` `"Destination address (it must be allowed on the
  server)."`; `--instructions` `"Instructions for the PKA; they go to it with the
  transcript."`. Docstring: `"Enqueue a transcription, the same way POST /transcriptions
  does. Calls the API when EARMARK_API_URL is set."`.
- Steps 1 and 2 are unchanged: settings, then the local validation of `TranscriptionRequest`
  or `UploadFields` (`ValidationError` → exit 2, same messages). This runs in both modes.
- Client mode:
  - A file: first `_check_upload(path)`. It raises `_SubmitRefused` with the local mode's
    messages: `f"{_clean(path.name)} does not look like audio (use an audio file extension
    such as .mp3 or .m4a)"` when `upload_extension(path.name, None)` is `None`, and
    `f"{_clean(path.name)} is empty"` when `path.stat().st_size == 0`. That is exit 2. Then
    `client.submit_file(path, fields)`.
  - A link: `client.submit_url(request)`.
  - The local recipient policy and `EARMARK_MAX_UPLOAD_MB` are not consulted; the server
    decides.
  - `ApiError` → `_api_error_exit("submit", exc, usage=SUBMIT_REFUSED_STATUSES)`.
  - Output `f"{r.job_id} {r.status}{'' if r.created else ' (existing job)'}"`.
- Local mode: unchanged. Move the two message strings into small helpers, or constants, so
  both modes use one copy.

**`transcript`**:

- Docstring: `"Print a job's transcript, as GET /jobs/{id}/transcript serves it. Calls the
  API when EARMARK_API_URL is set."`.
- Client mode: `data = client.transcript(job_id, fmt)`; `ApiError` →
  `_api_error_exit("transcript", exc)` (exit 1 for every status).
- Local mode: `job = _existing_store_or_exit(settings, "transcript").get(job_id)`, then as
  today.
- Both modes then `_write_transcript(data)`.

**Exit codes**, both modes:

| Situation | Exit |
|---|---|
| Success | 0 |
| Invalid settings, including `EARMARK_API_URL` | 2 |
| Usage error (typer), `jobs <id>` with `--limit`/`--status` | 2 |
| Client mode without a usable token | 2 |
| `submit` refused locally (validation, not audio, empty) or by the API (413, 415, 422) | 2 |
| Job not found (local, or 404) | 1 |
| No transcript (409, 410, 500, or the local equivalents) | 1 |
| 401, 503, another status, a redirect, an unreadable answer, an unreachable API | 1 |
| No data directory or database (local `jobs`, `transcript`) | 1 |

### 4.5 Doctor

- `OfflineChecks.client_mode`, appended after `notify` as `guarded("client mode", ...)`:
  - `api_url` unset: `ok`, `off: jobs, submit and transcript read EARMARK_DATA_DIR`.
  - set, and the token passes the client's token rules: `ok`, `on: jobs, submit and
    transcript call the API at {host_label(api_url)}`.
  - set, token unset or blank: `fail`, `on, but EARMARK_API_TOKEN is not set; the API
    refuses every call without it`.
  - set, token not printable ASCII: `fail`, `api_client.BAD_TOKEN`.
- `online.api_health(settings, probes) -> CheckResult`, name `api server`, run last in
  `run_online` when `api_url` is set: `skipped("api server")` when `client mode` failed.
  Otherwise `GET {api_url}/healthz` through an `httpx2.Client(transport=probes.http_transport
  or httpx2.HTTPTransport(), trust_env=False, follow_redirects=False,
  timeout=_timeout(probes.timeout_s))`, **with no `Authorization` header**, body capped at
  `MAX_BODY_BYTES`:
  - 200 and JSON `{"status": "ok"}`: `ok`, `{host} answers /healthz`;
  - 200 otherwise: `warn`, `{host} answered /healthz, but not as earmark does`;
  - 3xx: `fail`, `{host} answered with a redirect (HTTP {status}); redirects are not
    followed, check EARMARK_API_URL`;
  - another status: `fail`, `{host} answered HTTP {status} on /healthz`;
  - an `httpx2.HTTPError`: `fail`, `api_client.transport_error_message(exc, host,
    probes.timeout_s)`.

### 4.6 Config files and docs

- **`compose.yaml`**, in the `&earmark-env` anchor after `EARMARK_ASR_MODEL_DIR`:

  ```yaml
      # Empty means unset: commands run in a container read /data, never the API (step 34).
      EARMARK_API_URL: ""
  ```

- **`.env.example`**, after `EARMARK_PORT=8787`:

  ```text
  # The CLI's client mode (jobs, submit, transcript call the running API). Set it in your
  # shell, not here: docs/deploy.md section 19.
  # EARMARK_API_URL=
  ```

- **`README.md`**:
  - the `jobs` row becomes `| \`jobs [<job_id>] [--status] [--limit] [--json]\` | List recent
    jobs, newest first, or show one job. |`;
  - after the table, before "The HTTP API is …", a paragraph: "**Client mode.** With
    `EARMARK_API_URL` set in your shell, `jobs`, `submit` and `transcript` call the running
    API with `EARMARK_API_TOKEN` instead of reading `EARMARK_DATA_DIR`: from the API's host
    (`http://127.0.0.1:8787`) or from another machine (its HTTPS name). See
    [docs/deploy.md](docs/deploy.md) section 19." Keep the "The HTTP API is" paragraph
    separate and unchanged; `_readme_routes` finds it by its first words;
  - the settings table, `### API`, after `EARMARK_API_TOKEN`: `| \`EARMARK_API_URL\` | unset |
    The running API's URL, for the CLI's client mode: \`https://…\`, or \`http://\` to a
    loopback address such as \`http://127.0.0.1:8787\`. Set it in your shell, not in \`.env\`
    ([docs/deploy.md](docs/deploy.md) section 19). |`.
- **`docs/deploy.md`**:
  - section 4, "What compose overrides": add `EARMARK_API_URL` (empty, so commands run in a
    container are never API clients);
  - a new **`## 19. The command line`**, at the end:
    - **19.1 In the containers.** `docker compose exec api earmark jobs`,
      `docker compose exec api earmark jobs <job_id>`,
      `docker compose exec -T api earmark transcript <job_id> > note.md` (and why `-T`, as in
      8.7), `docker compose exec api earmark submit '<link>' --to <address>`. They read
      `/data`. A local file is not visible inside the container; submit it from the host in
      client mode.
    - **19.2 Client mode.** What it is (the three commands, the others stay local); that
      output and exit codes are the local ones; the transport rule (`https://`, or `http://`
      to a loopback address, refused otherwise); certificates always verified, against the
      system trust store (the Mac's keychain), with no way to turn it off (a certificate needs
      its intermediates, as 6.1 says); the token only in the header and never printed. **Where
      to set it: in your shell, not in `.env`.** Compose passes `.env` into the containers
      (compose blanks this one there), a native install's units read `.env` too, and an
      empty `EARMARK_API_URL=` on the command line does not override a `.env` value.
    - **19.3 On the Spark's host.** A venv in the checkout (`uv sync`), then
      `export EARMARK_API_URL=http://127.0.0.1:8787` and `uv run earmark jobs`; the token comes
      from the checkout's `.env`, which compose reads too. A note: an earlier `earmark jobs`
      run on the host may have created `./data/earmark.db`. Under compose that file is not the
      service's data (that is in the `data` volume), and it can be deleted; on a native
      install, `./data` is the real data.
    - **19.4 On the Mac.** From a checkout: `uv sync`, then:

      ```bash
      export EARMARK_API_URL=https://earmark.example.com
      export EARMARK_API_TOKEN='<the token from the Spark>'
      uv run earmark jobs
      uv run earmark submit ~/Downloads/memo.m4a --to pka-personal@example.net \
        --instructions "File this under ideas."
      uv run earmark transcript <job_id> > note.md
      ```

      Add the port to the URL if the proxy does not listen on 443. Keep the token out of shell
      history and files others can read.
    - **19.5 Checking it.** `earmark doctor` shows `client mode`, and `earmark doctor
      --online` adds `api server` (`/healthz`). On the Mac the other doctor lines check the
      Mac's own settings and can be ignored.
    - **19.6 What the answers mean.** A short table: 401 (check `EARMARK_API_TOKEN`), 404, 409
      and 410 (as in 8.7), 422 and 413 on `submit` (exit 2), the certificate message, "could
      not reach", and the missing-database message in local mode.

## 5. Behaviour and edge cases

### 5.1 One mode per run, chosen by the setting

- Only `jobs`, `submit` and `transcript` read `settings.api_url`. `doctor`, `config`,
  `transcribe`, `resolve`, `serve` and `worker` behave as before when it is set.
- In client mode the command never calls `open_job_store`, `Staging`, `ArtifactStore` or
  `settings.recipient_policy()`. It creates nothing under `EARMARK_DATA_DIR`. A test points
  `EARMARK_DATA_DIR` at a path that must not appear.
- Settings still load in full, so an invalid unrelated setting on the Mac still exits 2.
  The Mac's defaults are valid.

### 5.2 Same output

- The views go through the same `_job_lines` and the same `json.dumps(…,
  ensure_ascii=False)` as local mode. The parse and dump round trip is exact (2.3).
- `transcript` prints the response body through `_write_transcript`, so piped output is the
  exact bytes and terminal output is filtered, as in local mode.
- Error lines are `earmark <command>: <detail>`, where the API's detail is the text local
  mode prints (step 33 made the route's details the CLI's messages; `jobs` and `submit`
  details come from the same `Intake` and `JOB_NOT_FOUND`).
- Known differences, accepted:
  - the over-cap upload message: local `the file is larger than the 1 MiB limit
    (EARMARK_MAX_UPLOAD_MB)` (from the local setting), client the server's `the upload is
    larger than …` (from the server's setting);
  - a non-audio file with a not-allowed recipient: local mode reports the recipient first,
    client mode reports the file first (it checks the file before sending it).

### 5.3 Jobs and ids

- `jobs <job_id>` with an id that is not 32 lowercase hex digits is "job not found" in both
  modes, without a request in client mode. This keeps `/`, `?`, `..` and `%` out of the
  request path.
- `--limit` defaults to 20 in both modes and is always sent, so the API's default never
  matters.

### 5.4 Uploads

- `submit FILE` in client mode streams the file from disk (httpx2's multipart encoder,
  64 KiB chunks). It is never read whole into memory.
- The form's file name is `path.name`; the server cleans it (`clean_upload_name`) and takes
  the extension from it, as local mode does. The content type is httpx2's guess and does not
  matter when the name has an audio extension.
- An upload over the server's cap is the server's 413, with exit 2.
- An empty file or a non-audio extension is refused before any request.

### 5.5 Transport, token and errors

- The URL rule is checked when settings load (D4), and again in `ApiClient.__init__`.
  `localhost` is a name, not an IP literal, and is refused for `http://` (D3).
- No `verify` setting, option or parameter exists. `default_transport()` is
  `httpx2.HTTPTransport()`.
- `trust_env=False`: no `HTTP(S)_PROXY`, `ALL_PROXY` or `.netrc`. `follow_redirects=False`:
  a 3xx is an error naming `EARMARK_API_URL`. So the token can never follow a redirect to
  another host or to plain HTTP.
- The token is sent only in the per-request `Authorization` header. Never in the URL, a
  query or a cookie.
- **Never shown.**
  - An httpx exception's text is never used; only its type name.
  - A server's `detail` is redacted of the token before it is shown, so a server that echoes
    the header cannot make the CLI print it.
  - `ApiClient` logs nothing. httpx2's own loggers are gated below WARNING by earmark's
    handler, and print URLs, not headers.
  - `ClientSetupError` messages name settings only.
- A 503 shows the API's `the job store is unavailable; try again shortly`, with exit 1.
- The 64 MiB answer cap is far above any transcript (a note for a long episode is about
  1 MB).

### 5.6 No empty database by accident

- `jobs` (both forms) and `transcript` in local mode go through `open_existing_job_store`.
  It creates neither the data directory nor `earmark.db` (section 4.2).
- `serve`, `worker` and local `submit` keep `open_job_store`, which creates both.
- `doctor` already creates nothing.
- An existing but empty database (one an older `earmark jobs` created) still answers
  `no jobs`. The deploy docs say where such a file comes from (19.3).

### 5.7 Containers and native installs

- Compose blanks `EARMARK_API_URL` (P1), so `docker compose exec api earmark jobs` is always
  local, and doctor in a container says `client mode ok off`.
- A native install whose `.env` holds a valid URL: `serve` and `worker` ignore it, and
  `earmark jobs` run in that directory becomes a client of the same service. That is
  harmless but confusing, so the docs say not to.
- An *invalid* URL in a native install's `.env` is a settings error for every command,
  `serve` included, like any invalid setting. Compose never passes one into the containers.

## 6. Tests

**Fixtures.** None recorded. Everything runs against the real app in-process, on a real
uvicorn over a unix socket, or on `httpx2.MockTransport` stubs. All of them are built in the
test.

`tests/api_client_helpers.py` (new):

- `CLIENT_TOKEN = "client-token-SENTINEL-4f2a9c"` and `API_URL = "https://earmark.example.com"`.
- `api_settings(data_dir: Path, *, token: str = CLIENT_TOKEN, **overrides: Any) -> Settings`:
  `Settings(_env_file=None, api_token=SecretStr(token), data_dir=data_dir,
  allowed_recipients=(TO,), **overrides)`.
- `in_process_transport(app: FastAPI) -> httpx2.BaseTransport`: `TestClient(app)._transport`.
- `RecordingTransport(httpx2.BaseTransport)`: wraps another transport. It reads each request
  (`request.read()`), records `(method, str(url), dict(headers), content)`, and delegates.
- `use_transport(monkeypatch, factory) -> list[int]`: monkeypatches
  `earmark.api_client.default_transport` to call `factory`, and returns a list that counts
  the calls (a test that expects no request asserts it stays empty).

CLI tests (`tests/test_cli_client.py`) use a `client_env` fixture with the same fake
backends and `EARMARK_ALLOWED_RECIPIENTS` as `tests/test_cli.py::fake_env`, plus
`restore_root_logging` and `restore_library_loggers`. They:

- set `EARMARK_API_TOKEN=CLIENT_TOKEN`;
- build the server as `create_app(api_settings(tmp_path / "data"))`;
- run local steps (submit, `worker --once`) with `EARMARK_API_URL` unset;
- run client steps with `EARMARK_API_URL=API_URL` (or `http://127.0.0.1:8787` for the
  unix-socket server), `EARMARK_DATA_DIR=tmp_path / "client-side"`, and
  `use_transport(...)`;
- set `EARMARK_LOG_LEVEL=ERROR` where stderr is compared exactly (2.7).

### 6.1 Brief: "Tests run each client-mode command against the real API app in-process, or on a stub over a unix socket … success; 401, 404, 409 and 422; the plain-HTTP refusal for non-loopback hosts."

`tests/test_cli_client.py`, in-process unless named otherwise:

- **Success.**
  - `test_client_jobs_lists`: two jobs submitted locally, one run. `jobs` exits 0 with two
    lines; `jobs --json` parses to two `JobView`s.
  - `test_client_job_by_id[text|json]`: `jobs <id>` prints that job's line(s) or its
    `JobView` object.
  - `test_client_submit_url`: exit 0, stdout `<id> queued\n`. A second identical submit prints
    `<id> queued (existing job)\n`. Run locally, `jobs --json` shows the job with the given
    `to`, `instructions` and `title`.
  - `test_client_submit_file`: `My Clip.m4a` (`b"ID3" + bytes(5000)`) with `--instructions`.
    Exit 0. The server's `data/uploads/` holds the same bytes, and the job has `source ==
    "upload"`. After a local `worker --once` it is `done`.
  - `test_client_submit_ignores_local_allowlist`: `EARMARK_ALLOWED_RECIPIENTS` unset in the
    CLI's environment, allowed on the server: exit 0.
  - `test_client_transcript[note|text|json]`: after a local run, the bytes equal
    `episodes/<key>/…` as in step 33's tests (the note equals the outbox's note
    attachment).
  - `test_client_mode_over_real_http` (unix socket): `asgi_on_uds(app)`,
    `EARMARK_API_URL=http://127.0.0.1:8787`,
    `use_transport(lambda: httpx2.HTTPTransport(uds=str(path)))`. Submit a link and a
    300 KB file, run a local worker twice, then `jobs`, `jobs <id>` and `transcript <id>`:
    all exit 0 with the expected output.
  - `test_client_upload_over_cap_413` (unix socket, `create_app(..., max_upload_bytes=1000)`):
    exit 2, stderr `earmark submit: the upload is larger than the 1000 bytes limit
    (EARMARK_MAX_UPLOAD_MB)\n`.
- **401.** `test_client_wrong_token_401[jobs|jobs <id>|submit|transcript]`: the server's
  token differs. Exit 1, and stderr's last line is `earmark <command>: missing or invalid API
  token (check EARMARK_API_TOKEN)`.
- **404.**
  - `test_client_unknown_job_404[jobs|transcript]`: id `"a" * 32`. Exit 1, stderr
    `earmark <command>: job not found\n`.
  - `test_client_malformed_id_makes_no_request[../healthz|A*32|"a"*31]`: the same message;
    a `RecordingTransport` saw no request.
- **409.** `test_client_transcript_not_ready_409`: a queued job. Exit 1, stderr
  `earmark transcript: ` + `NOT_YET.format(status="queued")`.
- **410.** `test_client_transcript_gone_410`: `transcript.json` deleted. Exit 1, the
  `GONE` detail.
- **422.**
  - `test_client_submit_refused_recipient_422`: `--to stranger@example.com`. Exit 2, stderr
    `earmark submit: to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)\n`, and
    `stranger` not in the output.
  - `test_client_submit_show_link_422`: `SHOW_LINK`. Exit 2, stderr contains
    `MESSAGES["show_link"]`.
- **Plain-HTTP refusal.**
  - `test_client_refuses_plain_http[http://192.168.1.10:8787|http://earmark.example.com|http://localhost:8787|http://0.0.0.0:8787]`,
    for each of `jobs`, `submit <link> --to …` and `transcript <id>`: exit 2; stderr
    contains `EARMARK_API_URL` and `loopback`, and not the host; `use_transport` counted no
    call; `tmp_path / "client-side"` does not exist.
  - `test_client_allows_loopback_http[http://127.0.0.1:8787|http://[::1]:8787|<another 127.* address>]`:
    `jobs` against the in-process transport exits 0. Build the third address in the test as
    `str(ipaddress.IPv4Address("127.0.0.1") + 0x010203)`.
- **Other client errors.**
  - `test_client_without_token_exits_2`: `EARMARK_API_TOKEN` unset. Exit 2, stderr
    `earmark jobs: ` + `NO_TOKEN`; no transport call.
  - `test_client_unreachable_exits_1`: `use_transport(lambda:
    httpx2.HTTPTransport(uds=str(tmp_path / "missing.sock")))`. Exit 1; the message starts
    `could not reach the API at 127.0.0.1:8787 (ConnectError)`.
  - `test_client_creates_nothing_locally`: after every client command in this file's
    success tests, `tmp_path / "client-side"` does not exist. Fold it into the fixture's
    teardown as an assertion.

`tests/test_api_client.py`, `ApiClient` directly:

- `test_rejects_plain_http_to_non_loopback`: `ApiClient("http://192.168.1.10:8787",
  SecretStr("t"))` raises `ClientSetupError` with the `EARMARK_API_URL:` message.
- `test_token_rules[None|""|"  "|"tok en"|"tök"]`: `NO_TOKEN` or `BAD_TOKEN`, without the
  value in the message.
- `test_sends_bearer_header_only`: through a `RecordingTransport` over the in-process app,
  every request has `Authorization: Bearer <token>`, the token is not in the URL, and there is
  no `Cookie` header.
- `test_submit_url_body`: the JSON body's keys are exactly `{"url", "to", "instructions"}`,
  with `title` added when given, and never `timestamp`.
- `test_submit_file_form`: the multipart part names are a subset of `("file", *FORM_FIELDS)`,
  and the file part's file name is `path.name`.
- `test_redirect_not_followed`: a `MockTransport` answering 302 with `Location:
  http://evil.example.net/`. `ApiError` with the `REDIRECT` message, and one request only.
- `test_error_detail_shown_cleaned_and_capped`: a stub 500 with a 2000-character detail
  holding `\x1b[2J` and a newline. The message is 500 characters ending in `…`, with no
  control character.
- `test_error_without_detail`: a stub 502 with an HTML body → `the API at
  earmark.example.com answered HTTP 502`.
- `test_unreadable_answer`: a 200 that is not a `JobList` → `UNREADABLE`.
- `test_answer_cap`: monkeypatch `MAX_RESPONSE_BYTES` to 1000, and stub a 2000-byte body →
  `TOO_LARGE`.
- `test_transport_error_messages`: `MockTransport` handlers that raise
  `httpx2.ReadTimeout`, `httpx2.ConnectError` and an `httpx2.ConnectError` raised `from
  ssl.SSLCertVerificationError(1, "certificate verify failed")`. They give `TIMEOUT`,
  `UNREACHABLE` (with `ConnectError`) and `CERTIFICATE`.
- `test_unreadable_file`: `submit_file` on a path that is a directory → `CANNOT_READ_FILE`,
  `status is None`.

### 6.2 Brief: "Local-mode and client-mode output are the same for the same jobs."

`tests/test_cli_client.py::test_client_output_matches_local`, parametrised. Setup:

- local: submit `SHARE` with `--instructions`, a direct URL with `--title`, and a file;
  `worker --once` twice;
- a failed job inserted with `open_job_store(...).enqueue(new_podcast_job(...))`, `claim_next`
  and `fail(claim, "bad \x1b[31mred\x1b[0m\nsecond line", transient=False)`, as
  `test_jobs_sanitizes_control_chars` does;
- one more job submitted and not run (queued).

Each case runs the arguments locally and in client mode, both with `EARMARK_LOG_LEVEL=WARNING`,
and asserts `(exit_code, stdout_bytes, stderr)` are equal:

- `jobs`; `jobs --json`; `jobs --status done`; `jobs --limit 1`;
- `jobs <done>`; `jobs <done> --json`; `jobs <failed>` (the cleaned error line);
  `jobs <"a"*32>` (both 1, `job not found`);
- `transcript <done>`; `--format text`; `--format json`; `transcript <queued>` (both 1);
  `transcript <"a"*32>`.

And:

- `test_client_submit_output_matches_local`: in two fresh data dirs (one served, one local),
  the same link submitted twice prints the same shape (`^[0-9a-f]{32} queued\n$`, then
  `… queued (existing job)\n`). The same refused recipient, overlong `--instructions`
  (local validation), non-audio file and empty file give equal `(exit_code, stderr)`.

### 6.3 Brief: "A test fails if the token appears in any output or log line."

- `tests/test_leaks.py::test_api_client_leaks_no_token`, with the `log_stream` fixture
  (DEBUG, **no redaction**) and `restore_library_loggers`. It drives `ApiClient` with
  `CLIENT_TOKEN` through:
  - the in-process app (each method, success and its 404/409/422);
  - a wrong-token client (401);
  - an echo stub (`MockTransport`) that answers 401, 422 and 500 with `{"detail": "seen
    <the Authorization header>"}`, a 302 whose `Location` holds the token, and a 200 that is
    not JSON;
  - a transport that raises `httpx2.ConnectError(f"boom {CLIENT_TOKEN}")`.

  For each call, the token is in neither `ApiError.message`, `str(exc)` nor `repr(exc)`.
  After all calls, it is not in `log_stream.getvalue()`. The echo stub's 422 message still
  contains `seen Bearer **********`, which proves the detail was shown and redacted rather
  than dropped.
- `tests/test_cli_client.py::test_cli_client_mode_never_prints_the_token`, with
  `EARMARK_LOG_LEVEL=DEBUG`. Every client command, in the success and error cases of 6.1 and
  with the echo stub, keeps the token out of `stdout_bytes` and `stderr`.
- `tests/test_cli.py::test_config_never_prints_secrets` already covers `earmark config`, and
  `test_doctor_never_prints_secrets` covers doctor. Extend the latter with
  `EARMARK_API_URL=https://earmark.example.com`, and its online part with the `api server`
  probe against the echo handler. The probe sends no token, so there is nothing to echo.

### 6.4 Brief: "`jobs` with a missing database creates nothing and exits non-zero with the message."

`tests/test_cli.py`:

- `test_read_only_command_without_data_dir[jobs|jobs <id>|transcript <id>]`: no data dir.
  Exit 1. stderr's first line is `earmark <command>: EARMARK_DATA_DIR (<abs>) does not exist;
  nothing was created.`, and the second contains `EARMARK_API_URL` and
  `docker compose exec api earmark <command>`. `tmp_path / "data"` does not exist.
- `test_read_only_command_without_database[...]` (the same three): `data/` exists and is
  empty. The `NO_DATABASE` message, and `list((tmp_path / "data").iterdir()) == []`.
- `test_local_submit_still_creates_the_database`: `submit SHARE` creates `data/earmark.db`.
  `serve` and `worker` are already covered by `test_serve_runs_uvicorn` (it asserts the
  database exists) and `test_submit_then_worker_once`.

`tests/test_jobs.py`:

- `test_open_existing_job_store_missing_dir` and `..._missing_db`: `MissingDatabaseError` with
  the two messages; nothing created.
- `test_open_existing_job_store_opens_existing`: after `open_job_store`, it returns a store
  that lists the same jobs.

### 6.5 Brief: "`make check` passes."

`make check`, and `GITHUB_ACTIONS=true make check`.

### 6.6 Criteria this plan adds

- **Certificates cannot be turned off** (`tests/test_api_client.py`):
  - `test_default_transport_verifies`: `default_transport()._pool._ssl_context` has
    `verify_mode == ssl.CERT_REQUIRED` and `check_hostname`.
  - `test_no_verification_switch`: no `Settings` field name contains `verify`, `insecure`,
    `cert` or `ssl`, and `inspect.signature(ApiClient)` has no `verify` parameter.
- **The URL rule** (`tests/test_config.py`):
  - `test_api_url_accepted[...]`: `https://earmark.example.com` (also with a trailing `/`, and
    with `:8443`), `http://127.0.0.1:8787`, `http://[::1]:8787`, `HTTPS://earmark.example.com`.
    Each normalises to `scheme://netloc`.
  - `test_api_url_refused[...]`, each with its exact `API_URL_*` message in the
    `ConfigError` line, and the input's host not in `str(exc)`:
    `http://192.168.1.10:8787`, `http://localhost:8787`, `http://earmark.example.com`,
    `ftp://earmark.example.com`, `earmark.example.com`, `https://u:p@earmark.example.com`,
    `https://earmark.example.com/api`, `https://earmark.example.com/?x=1`,
    `https://earmark.example.com/#f`, `https://earmark.example.com:99999`.
  - `test_api_url_empty_is_unset`: `EARMARK_API_URL=` → `None`.
  - `test_settings_fields_are_exactly`: `"api_url"` after `"api_token"`.
- **Containers ignore it** (`tests/test_deploy.py::test_compose_forces_container_paths`):
  `EARMARK_API_URL` is in the shared set for both services, with the value `""`.
- **Doctor** (`tests/test_doctor.py`):
  - `test_doctor_client_mode_off`, `test_doctor_client_mode_on` (the detail names the host
    only), `test_doctor_client_mode_without_token_fails`,
    `test_doctor_client_mode_bad_token_fails`;
  - `test_doctor_check_names_in_order` gains `"client mode"` last;
  - `test_doctor_online_api_server_ok`, using `Probes(http_transport=MockTransport(...))`
    that asserts the path is `/healthz` and there is no `Authorization` header;
  - `test_doctor_online_api_server_failures[redirect|503|unreachable|certificate|not earmark]`;
  - `test_doctor_online_api_server_skipped_without_token`;
  - `test_doctor_online_no_api_server_line_when_off`.
- **Wording** (`tests/test_cli.py::test_submit_help_wording`): from
  `typer.main.get_command(app).commands["submit"].params`, the `instructions` option's
  `help` contains `PKA` and `transcript` and not `analysis`. The source argument's help names
  YouTube. Read the param objects, not the rendered help, which rich wraps and colours.
- **`jobs <id>` locally** (`tests/test_cli.py`): `test_jobs_one_job[text|json]`,
  `test_jobs_one_job_not_found`, and `test_jobs_id_with_limit_or_status_exits_2`.
- **Light imports** (`tests/test_cli.py::test_cli_import_is_light`): add `httpx2` to the
  banned set, so `api_client` stays a lazy import.
- **Docs** (`tests/test_docs.py::test_deploy_doc_cli_section`): `docs/deploy.md` has
  `## 19. The command line`, and that section (flattened, lower-cased, backticks removed)
  contains `earmark_api_url`, `http://127.0.0.1:8787`, `https://earmark.example.com`,
  `docker compose exec api earmark jobs`, `in your shell` and `not in .env`. The existing
  placeholder, anchor, removed-wording, settings-coverage and command-coverage tests cover
  the rest of the README, `deploy.md` and `.env.example`.

### 6.7 Existing tests that must change

These change because of the new rule (section 5.6), not because their behaviour regressed:

| Test | Why | Change |
|---|---|---|
| `test_jobs_empty` | no database | Create it first (`open_job_store(Settings(_env_file=None))`), then expect `no jobs`. |
| `test_fresh_database_after_moving_v010_aside` | after moving aside, `jobs` has no database | Run `worker --once` (it creates a fresh database), then `jobs` prints `no jobs`; the identity checks stay. |
| `test_submit_rejects_long_instructions`, `test_submit_prompt_option_is_gone`, `test_submit_requires_to` | they fail before opening the store, so `jobs` finds no database | Replace the `jobs` check with `assert not (fake_env / "data").exists()`. |
| `test_transcript_command_unknown_job` | no database | Create it first; the expected stderr is unchanged. |
| `test_doctor_check_names_in_order` | new last check | Add `"client mode"`. |
| `test_compose_forces_container_paths` | new key | As in 6.6. |
| `test_settings_fields_are_exactly` | new field | As in 6.6. |

`test_v010_database_refused_by_commands[jobs]` is unchanged: the file exists, so the
identity refusal still wins.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
export TMPDIR=/home/sandy/.cache/earmark-step34-verify && mkdir -p "$TMPDIR"
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_api_client.py tests/test_cli_client.py -q
uv run pytest tests/test_cli.py tests/test_config.py tests/test_jobs.py -q
uv run pytest tests/test_doctor.py tests/test_deploy.py tests/test_docs.py tests/test_leaks.py -q
git status --short   # the files in section 3 only (deploy/caddy/install-cert.sh stays untracked, untouched)
```

A local smoke with the fake backends: loopback only, no network, in a temporary directory.
The server and the worker get the real data directory. The client commands get a data
directory that must never appear.

```bash
demo=$(mktemp -d)
srv="env EARMARK_DATA_DIR=$demo/data"
cli="env EARMARK_DATA_DIR=$demo/client-side EARMARK_API_URL=http://127.0.0.1:8787"
export EARMARK_API_TOKEN=demo-token EARMARK_ALLOWED_RECIPIENTS=you@example.org
export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_YOUTUBE_BACKEND=fake \
  EARMARK_ASR_BACKEND=fake EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
$srv uv run earmark serve &
curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
$cli uv run earmark submit "https://www.youtube.com/watch?v=jNQXAC9IVRw" --to you@example.org --instructions q
printf 'not really audio' > "$demo/memo.m4a"
$cli uv run earmark submit "$demo/memo.m4a" --to you@example.org
$cli uv run earmark submit "$demo/memo.m4a" --to stranger@example.com; echo "exit $?"   # 2
$srv uv run earmark worker --once
$srv uv run earmark worker --once
$cli uv run earmark jobs > "$demo/client.txt"
$srv uv run earmark jobs > "$demo/local.txt"
cmp "$demo/client.txt" "$demo/local.txt" && echo "jobs output matches"
id=$($srv uv run earmark jobs --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["jobs"][0]["id"])')
$cli uv run earmark jobs "$id"
$cli uv run earmark transcript "$id" > "$demo/client-note.md"
$srv uv run earmark transcript "$id" > "$demo/local-note.md"
cmp "$demo/client-note.md" "$demo/local-note.md" && echo "note matches"
$cli uv run earmark transcript "$(printf 'a%.0s' $(seq 32))"; echo "exit $?"   # 1, job not found
EARMARK_API_TOKEN=wrong $cli uv run earmark jobs; echo "exit $?"   # 1, check EARMARK_API_TOKEN
EARMARK_API_URL=http://192.168.1.10:8787 uv run earmark jobs; echo "exit $?"   # 2, loopback
$cli uv run earmark doctor --online | grep -E "client mode|api server"
test ! -e "$demo/client-side" && echo "client mode created nothing"
EARMARK_DATA_DIR="$demo/none" uv run earmark jobs; echo "exit $?"   # 1, does not exist
test ! -e "$demo/none" && echo "local jobs created nothing"
kill %1; wait
rm -rf "$demo"
```

Expected:

- The three `submit` lines print `<id> queued`, `<id> queued` and the allowlist refusal
  (exit 2).
- Both outputs match, and each `cmp` prints its "matches" line.
- `jobs <id>` prints one job.
- The unknown id gives `earmark transcript: job not found` (exit 1). The wrong token gives
  `earmark jobs: missing or invalid API token (check EARMARK_API_TOKEN)` (exit 1). The LAN
  URL gives `EARMARK_API_URL: Value error, plain http is allowed only to a loopback address
  …` (exit 2), with no request made, since the settings refuse it first.
- doctor shows `ok client mode on: … 127.0.0.1:8787` and `ok api server 127.0.0.1:8787
  answers /healthz` (the other doctor lines warn about the fakes).
- Both "created nothing" lines print, and the missing data dir gives the two-line message
  (exit 1).

**Not verifiable in the sandbox, to report as such.** These are operator checks; no runbook
phase is added:

- client mode over HTTPS through Caddy from the Mac, with the Mac's trust store
  (`truststore`) and the operator's certificate;
- client mode on the Spark's host against compose's published `127.0.0.1:8787`;
- `docker compose exec api earmark jobs` reading `/data` with `EARMARK_API_URL` blanked by
  compose.

## 8. Deviations and decisions

### 8.1 P1: the containers ignore `EARMARK_API_URL` (product choice; the brief left it open)

- **Built: ignore.** Compose sets `EARMARK_API_URL: ""` for `api` and `worker`, and `serve`
  and `worker` never read the setting. In a container, every command is local.
- **Not built: refuse**, where `serve` and `worker` exit 2 when the setting is set. Against
  it:
  - a stray line in `.env` would stop the service at its next restart;
  - `docker compose exec api earmark jobs` would still become a client of whatever URL
    `.env` holds, which may not resolve inside the container;
  - a native install's operator who exports the variable in the same shell would have to
    unset it to start the service.
- **The same either way.** The docs say the setting belongs in the shell. An invalid URL
  is a settings error for every command, like any invalid setting.
- The operator can still ask for "refuse" instead. It is a small change: a check in
  `serve_command` and `worker_command`.

### 8.2 D2: one job is `earmark jobs <job_id>`

The brief lists "a single job" among the client-mode commands, but the CLI has no such
command. An optional argument to `jobs` keeps the command set the brief names (`jobs`,
`submit`, `transcript`) and `test_cli_commands_are_exactly` unchanged. It mirrors
`GET /jobs/{id}`: with `--json` it prints that `JobView` object. `--limit` and `--status` with
an id are a usage error.

### 8.3 D3: loopback means an IP literal

`localhost` is refused for `http://`: a name can resolve anywhere (`/etc/hosts`, a resolver),
and the brief says "a loopback address". The docs use `http://127.0.0.1:8787`, which is what
compose publishes.

### 8.4 D4: the URL rule is a settings check

- It runs when settings load, so a bad URL fails before any command starts, and doctor
  reports it as a `config` failure.
- `ApiClient` repeats it for direct callers.
- The cost: an invalid URL in a native install's `.env` stops `serve` too, as any invalid
  setting does.

### 8.5 D5: what the client checks before sending

- It checks locally what it can with no server knowledge: the request's validation (same
  messages), a non-audio file name, an empty file.
- It leaves the recipient allowlist and the upload cap to the server, whose settings are the
  ones that matter. So the over-cap message is the server's (section 5.2).

### 8.6 D6 and D7: smaller choices

- **No `timestamp`** from `submit` in client mode, as in local mode, so a job's note is the
  same whichever way it was submitted. A `--timestamp` option is not added.
- **Doctor checks `/healthz` only**, as the brief says, without the token. Whether the token
  is right shows on the first real command (401 with its hint).
- **The submit SOURCE help** now names YouTube links (step 32 added them). This is a one-line
  wording fix next to the briefed `--instructions` change.
- **`jobs --limit` is `int | None`**, defaulting to 20 in code, so `jobs <id> --limit 5` can
  be refused.
- **`MissingDatabaseError` subclasses `DatabaseSetupError`**, so code that already catches
  the base class keeps catching it. The CLI catches the subclass first to print the hint.
- **`api_client.py` is a top-level module**, not `api/client.py`, because `api/` is the
  server.

### 8.7 Dependencies

None added. httpx2 (already a core dependency, and the library of the ntfy and remote ASR
clients) is the client. `urllib.request` was not used: it has no streaming multipart
encoder, and httpx2's error types and test transports are already used across the suite.

### 8.8 `docs/ARCHITECTURE.md` changes made by this plan

- "Module layout": `api_client.py`.
- "Runner and worker", **Commands**: `jobs [JOB_ID]`, client mode, the read-only rule.
- "Doctor": the `client mode` offline check and the `api server` online check.
- "HTTP API": a closing **Client mode** paragraph (routes, same output, errors, transport,
  containers).
- "Configuration": the `API_URL` row.
- "Deployment", **Compose**: the empty `API_URL`.
- "Security": **The CLI as an API client**.
- "Settled questions": **Settled by step 34**.

## 9. Out of scope

- A token check in `doctor --online` (an authenticated probe), and a `--timestamp` option on
  `submit`.
- Detecting running containers (the operator rejected it).
- Client mode for `doctor`, `config`, `transcribe`, `resolve`, `serve` or `worker`.
- A custom CA option, a pinned certificate, or any way to skip verification. `SSL_CERT_FILE`
  is the only lever, and it is httpx2's.
- Aligning the local and server over-cap messages.
- Deleting a stray host database automatically.
- Retries, and a progress display for large uploads.
- Installing the CLI on the Mac as a tool (`uv tool install`); the docs use a checkout.
- Runbook phases for client mode; the certificate route of step 35; the Shortcut (step 31).
