# Step 04: Ingest API

## 1. Outcome

`earmark serve` runs a FastAPI app under uvicorn on `EARMARK_HOST:EARMARK_PORT`. It
refuses to start without a usable `EARMARK_API_TOKEN`. Every path except `/healthz`
requires `Authorization: Bearer <token>`, checked in constant time by a pure ASGI
middleware before any route code or body parsing runs. `POST /ingest` validates the
body (`IngestRequest`, now normalising), parses the URL with
`resolve.apple.parse_apple_url`, enqueues the **canonical** episode URL in the
`JobStore`, and returns 202 `{job_id, status, created}` with a `Location` header.
`GET /jobs/{id}` and `GET /jobs` read the store. Interactive docs and the OpenAPI
schema are off. Errors are `{"detail": "<short message>"}`, written so the iOS
Shortcut can show them as they are. The handler does no outbound I/O. No worker exists
yet (step 05), so jobs stay `queued`.

## 2. Findings

Checked on 2026-11-19 against the installed packages (`uv pip list`): fastapi 0.165.0,
starlette 1.10.0, uvicorn 0.54.0, httpx2 2.16.2, httpcore2 2.16.2, anyio 4.16.0,
pydantic 2.14.4, pydantic-core 2.50.5, typer 0.29.2. The probes were scratch files run
with `uv run pytest -c pyproject.toml --rootdir .`, so `--disable-socket
--allow-unix-socket` and `filterwarnings = error` applied. They have been deleted.

### 2.1 TestClient uses httpx2

`starlette/testclient.py` imports `httpx2 as httpx`. It falls back to `httpx` only when
httpx2 is missing, and that fallback raises `StarletteDeprecationWarning`. httpx is not
installed, so `fastapi.testclient.TestClient` runs on httpx2 with no warning. A probe
with a sync route, a pure ASGI middleware, exception handlers, `HEAD`, a websocket
connect, and use both with and without `with TestClient(app)` passed under the socket
block and `filterwarnings = error`. Sync handlers ran on an `AnyIO worker thread`. Two
rules for the tests:

- Open clients with `with TestClient(app) as client:` in a fixture so they are closed
  deterministically. An unclosed client could raise a `ResourceWarning` during GC,
  which `filterwarnings = error` turns into a failure.
- httpx2 encodes `str` header values as ASCII and raises `UnicodeEncodeError` on the
  client side for `"Bearer té"`. To send a non-ASCII header, pass raw bytes:
  `headers=[(b"authorization", "Bearer t\xe9".encode("latin-1"))]`.

### 2.2 Body size limits are built in

FastAPI 0.165 has native request-body limits (`fastapi/_body_limit.py`, wrapping
Starlette 1.10's `starlette.middleware.body_limit.RequestBodyLimitMiddleware`). The
`max_body_size` argument works on `FastAPI(...)`, `APIRouter(...)`, and each route
decorator. Probe results with `@router.post("/ingest", max_body_size=100)`:

- A body over the limit with a `Content-Length` gets **413** with a `text/plain` body
  `Content Too Large`. The limiter replaces the response at `http.response.start`.
- An over-limit chunked body (httpx2 `content=<generator>`, no `Content-Length`) gets
  **413** with JSON `{"detail":"Content Too Large"}`. Here the limiter raises an
  `HTTPException` while the body is read.
- The route handler never runs in either case.

So tests assert the status (413) and that nothing was enqueued, never the body format.
Use the **route-level** limit. The app-level `max_body_size` middleware sits outside
user middleware. With it, an unauthenticated request with a large `Content-Length`
would get 413 instead of 401, because the limiter rewrites the auth middleware's
response. With the route-level limit, auth always answers first.

### 2.3 Body parsing happens before dependencies

In `fastapi/routing.py::get_request_handler`, the JSON body is read and decoded
**before** `solve_dependencies` runs. A JSON decode error raises
`RequestValidationError` at once. If auth were a FastAPI dependency (`Depends` or
`HTTPBearer`), an unauthenticated request with malformed JSON would get 422 instead of
401. Auth is therefore a pure ASGI middleware; the probe confirmed that bad JSON without
a token gets 401. `fastapi.security.HTTPBearer` (0.165) does now return 401 with
`WWW-Authenticate: Bearer` for a missing or non-Bearer header. It is not used, both
because of this ordering and because it would compare tokens in route code.

### 2.4 Validation error shapes (what the 422 formatter sees)

`RequestValidationError.errors()` from the probe:

| Request | `type` | `loc` | `msg` |
|---|---|---|---|
| `{not json` | `json_invalid` | `("body", 1)` | `JSON decode error` (+ `ctx.error`) |
| empty body or `null` | `missing` | `("body",)` | `Field required` |
| JSON array, or a JSON body without `Content-Type` | `model_attributes_type` | `("body",)` | `Input should be a valid dictionary or object to extract fields from` |
| unknown key `promt` | `extra_forbidden` | `("body", "promt")` | `Extra inputs are not permitted` |
| `PydanticCustomError("bad_url", "That link is bad.")` in a field validator | `bad_url` | `("body", "url")` | `That link is bad.` |

Every entry also has `input`, which echoes what the client sent (the bytes of the whole
body in the no-`Content-Type` case). FastAPI's default handler returns all of it as a
list, which is neither short nor safe to show. A custom handler builds one string
without `input` or `ctx` (section 5.6). With `strict_content_type=True` (the 0.165
default), a body sent without `Content-Type: application/json` is not parsed as JSON.
The Shortcut's "Get Contents of URL" with a JSON body sends that header, and the docs
of step 16 say so.

### 2.5 Docs off

`FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
swagger_ui_oauth2_redirect_url=None)`: with a valid token, `/docs`, `/redoc`,
`/openapi.json`, and `/docs/oauth2-redirect` all return 404. Without a token they
return 401, because the middleware runs first.

### 2.6 FastAPI 0.165 native OpenTelemetry

`FastAPI.__init__` now takes `telemetry: TelemetryConfig`. It is active only when an
OpenTelemetry provider is configured globally, or when `FASTAPI_OTEL_AUTO_CONFIGURE=true`
and the OTLP exporters are installed. Its `logs` option records validation failures,
which can include the prompt. With the env var set and no exporters installed,
`app._native_telemetry.enabled()` was `False`. Earmark disables it explicitly anyway
(section 4.6), so an ambient environment variable plus a later dependency cannot start
exporting request data.

### 2.7 mypy and ruff on the wiring

A probe module type-checked clean under `mypy --strict` with the project config. It
contained `FastAPI(..., telemetry={...all False...})`,
`app.add_middleware(Middleware, token=...)` (ParamSpec-typed, keyword checked),
`app.add_exception_handler(RequestValidationError, handler)` and
`(sqlite3.Error, handler)` with handlers typed `(Request, Exception) -> JSONResponse`,
`Annotated[int, Query(ge=1, le=500)] = 20`, a `Response` parameter,
`@router.post(..., max_body_size=...)`, `@router.api_route(..., methods=["GET",
"HEAD"])`, `WebSocketClose(code=1008)`, and `uvicorn.run(app, host=..., port=...,
log_config=None, server_header=False)`. Ruff's only finding was S101 for an `assert
isinstance(exc, ...)` in a handler. In `src`, narrow with
`if not isinstance(...): raise TypeError(...)` instead. Starlette's `Headers(scope=scope)`
raises `KeyError` on a `lifespan` scope, so read headers only for `http` and `websocket`
scopes.

### 2.8 uvicorn

`uvicorn.run` (0.54) defaults to `log_config=LOGGING_CONFIG`, which installs uvicorn's
own handlers and formatters and so bypasses earmark's redacting formatter. With
`log_config=None` it configures nothing. A live probe in this sandbox showed
`uvicorn.error` and `uvicorn.access` records propagating to the root handler: startup
lines and `127.0.0.1:… - "GET /healthz HTTP/1.1" 200`. `server_header=False` dropped
the `server: uvicorn` header. Binding 127.0.0.1 and `curl` work in the sandbox, so the
verifier can run a live smoke. uvicorn's access log line contains the path and query,
but never headers, so the token does not reach the log.

### 2.9 Apple Podcasts URL shapes (live, 2026-11-19)

Checked without personal details: a generic User-Agent, a public show (The Daily,
`id1200361736`).

- iTunes Lookup (`/lookup?id=1200361736&entity=podcastEpisode&limit=3&country=us`)
  `trackViewUrl` for episodes:
  `https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet-and-other-things/id1200361736?i=1000802907981&uo=4`.
  The slug is the **episode** title, and extra parameters (`uo=4`) appear.
  `collectionViewUrl` on episode records uses the legacy host:
  `https://itunes.apple.com/us/podcast/the-daily/id1200361736?mt=2&uo=4`.
- The episode page's `<link rel="canonical">` and `og:url` are
  `https://podcasts.apple.com/gb/podcast/<episode-slug>/id1200361736?i=1000802907981`,
  with the storefront country, the slug, and `?i=` only.
- Redirects (`curl -w %{redirect_url}`):
  - `https://podcasts.apple.com/us/podcast/id1200361736?i=…` (no slug) → 301 to the
    slugged URL.
  - `https://podcasts.apple.com/podcast/id1200361736?i=…` (no country) → 301 to `/us/…`
    from this sandbox. Apple picks the storefront from the client.
  - `/US/` → 301 to `/us/`.
  - `…/id1200361736/?i=…` (trailing slash) → 301 to the canonical form.
  - `https://itunes.apple.com/us/podcast/the-daily/id1200361736?i=…&mt=2` → 301 to
    `podcasts.apple.com`, keeping the query.
  - `/gb/` with the slug → 200.

  So `https://podcasts.apple.com/{country}/podcast/id{show}?i={episode}` is a working,
  stable link to the episode. It becomes the canonical form.
- "Share from time" links: the iOS 18+ share sheet offers "From Start" and "From
  [time]". Third-party guides say the timed link adds `&r=<seconds>`; Apple does not
  document it. The parser ignores every query parameter except `i`, so a timed share
  and a from-start share canonicalise to the same URL and dedupe. Not verifiable here:
  the exact string today's iOS share sheet hands to a Shortcut. That needs a phone. The
  shapes above come from Apple's own API and page metadata. Step 16 should paste a
  real share into a test.

Dead ends to avoid:
- Do not match `\d` for ids. Python's `\d` matches Unicode digits (`١٢٣`). Use `[0-9]`.
- Do not use `HTTPBearer` or a dependency for auth (section 2.3).
- Do not use the app-level `max_body_size` (section 2.2).
- Do not assert the 413 body (section 2.2).

## 3. Files

| File | Change |
|---|---|
| `src/earmark/models.py` | `IngestRequest` gains validators: strip the URL, cap URL and prompt length, turn a blank prompt into `None`, range-check the timestamp. New constants `MAX_URL_CHARS`, `MAX_PROMPT_CHARS`. Still frozen with `extra="forbid"`. |
| `src/earmark/resolve/__init__.py` | New. Docstring only. |
| `src/earmark/resolve/apple.py` | New. `parse_apple_url`, `canonical_episode_url`, `UnsupportedUrlError`, message constants. Imports the stdlib, `earmark.models`, and `earmark.errors` only. |
| `src/earmark/ingest.py` | New. `enqueue_ingest(store, request) -> Enqueued`, shared by the API and step 05's `earmark submit`. |
| `src/earmark/api/__init__.py` | New. Docstring only; nothing re-exported, so importing a submodule stays explicit. |
| `src/earmark/api/app.py` | New. `create_app`, the 422 formatter `describe_validation_errors`, exception handlers. |
| `src/earmark/api/auth.py` | New. `ApiTokenError`, `require_api_token`, `bearer_credentials`, `token_digest`, `token_matches`, `BearerAuthMiddleware`, `PUBLIC_PATHS`, `WWW_AUTHENTICATE`. |
| `src/earmark/api/routes.py` | New. `build_router(store) -> APIRouter` with the four routes, and `MAX_BODY_BYTES`. |
| `src/earmark/api/schemas.py` | New. Response models `IngestResponse`, `JobList`, `Health`. |
| `src/earmark/cli.py` | Add `earmark serve`, which imports `earmark.api` and `uvicorn` lazily inside the command. |
| `tests/test_resolve_apple.py` | New. Parser tests. |
| `tests/test_ingest.py` | New. `enqueue_ingest` canonical dedupe without HTTP. |
| `tests/test_api.py` | New. Auth, routes, validation, limits, docs off, the store-error mapping. |
| `tests/test_models.py` | Add `IngestRequest` normalisation tests. Existing tests stay unchanged and must still pass. |
| `tests/test_cli.py` | Add the `serve` tests and a test that `import earmark.cli` stays light. |
| `docs/ARCHITECTURE.md` | Updated contracts (section 8). |
| `docs/PLAN.md` | Status row for 04 set to `done` by the orchestrator after verification, not by the implementer. |

There are no new dependencies and no new settings, so `.env.example` is unchanged.

## 4. Interfaces

### 4.1 `earmark.models` (changed)

```python
MAX_URL_CHARS: Final = 2048
MAX_PROMPT_CHARS: Final = 4000


class IngestRequest(Model):
    url: str
    prompt: str | None = None
    timestamp: AwareDatetime | None = None

    # field_validator("url"), mode="after": strip; "" -> url_empty;
    #   len > MAX_URL_CHARS -> url_too_long
    # field_validator("prompt"), mode="after": None stays None; strip; "" -> None;
    #   len > MAX_PROMPT_CHARS -> prompt_too_long
    # field_validator("timestamp"), mode="after": value.astimezone(UTC) must not raise
    #   OverflowError -> timestamp_out_of_range; return the value unchanged (offset kept)
```

Errors are raised as `pydantic_core.PydanticCustomError(type, message)` with these
exact types and messages (`INGEST_ERROR_TYPES` is a module-level `frozenset` of the
four types, used by the API formatter):

| type | message |
|---|---|
| `url_empty` | `url must not be empty` |
| `url_too_long` | `url is too long (at most 2048 characters)` |
| `prompt_too_long` | `prompt is too long (at most 4000 characters)` |
| `timestamp_out_of_range` | `timestamp is out of range` |

Build the two "too long" messages from the constants with an f-string. They contain no
`{}` placeholders, so pydantic's templating leaves them alone. Lengths are `len(str)`
(code points), measured **after** stripping. Do not put `max_length` on the fields,
because pydantic checks it before the strip. Stripping uses `str.strip()`, which covers
Unicode whitespace such as NBSP and U+3000. The validators are idempotent, so the JSON
round-trip tests still hold. `pydantic_core` is a dependency of pydantic, so
`models.py` stays light.

### 4.2 `earmark.resolve.apple`

```python
APPLE_HOSTS: Final = frozenset({"podcasts.apple.com", "itunes.apple.com"})
DEFAULT_COUNTRY: Final = "us"

UrlProblem = Literal["malformed", "unsupported_host", "show_link", "not_episode"]

MESSAGES: Final[Mapping[UrlProblem, str]] = MappingProxyType(
    {
        "malformed": "That is not a valid link. Share an episode from Apple Podcasts.",
        "unsupported_host": "Only Apple Podcasts episode links are supported.",
        "show_link": "This links to a whole show. Share a single episode instead.",
        "not_episode": "This Apple link is not a podcast episode.",
    }
)


class UnsupportedUrlError(PermanentError):
    def __init__(self, problem: UrlProblem) -> None:
        super().__init__(MESSAGES[problem])
        self.problem = problem


def canonical_episode_url(country: str, show_id: str, episode_id: str) -> str:
    return f"https://podcasts.apple.com/{country}/podcast/id{show_id}?i={episode_id}"


def parse_apple_url(url: str, /) -> EpisodeRef: ...
```

`UnsupportedUrlError` subclasses `PermanentError`, so step 07's resolver can let it
propagate and the job fails permanently. The messages never include the input.

`parse_apple_url` runs these steps in order. The first failure raises:

1. `text = url.strip()`. Empty, longer than `MAX_URL_CHARS`, or containing any
   character `<= " "` or `"\x7f"` (a space or control character anywhere) →
   `malformed`. Non-ASCII is allowed, because slugs may be raw Unicode.
2. If `"://"` is not in `text`, prefix `"https://"`. This accepts
   `podcasts.apple.com/us/podcast/…` pasted without a scheme.
3. `parts = urlsplit(text)`; a `ValueError` (for example a bad IPv6 bracket) →
   `malformed`. `parts.scheme.lower()` not in `{"http", "https"}` → `malformed`.
4. Userinfo (`"@" in parts.netloc`) → `malformed`. `parts.port` raising
   `ValueError`, or any explicit port → `malformed`.
5. `host = (parts.hostname or "").rstrip(".")` (`hostname` is already lower-case).
   Empty → `malformed`. Not in `APPLE_HOSTS` → `unsupported_host`. Exact match only:
   `embed.podcasts.apple.com`, `music.apple.com`, `apple.com`, and
   `podcasts.apple.com.evil.example` are all `unsupported_host`.
6. Path: `segments = [s for s in parts.path.split("/") if s]`. This tolerates trailing
   and doubled slashes.
   - If `segments[0]` is two ASCII letters, `country = segments[0].lower()` and
     `rest = segments[1:]`. Otherwise `country = DEFAULT_COUNTRY` and `rest = segments`.
   - The episode shape is `rest[0].lower() == "podcast"`, `len(rest) in (2, 3)`, and
     `re.fullmatch(r"id([0-9]{1,20})", rest[-1], re.IGNORECASE)`. A middle element, if
     present, is the slug and is ignored. Anything else → `not_episode`. That covers
     `/us/channel/…`, `/us/genre/…`, `/us/podcast/slug` with no id, and `/`.
   - `show_id = str(int(digits))`, which drops leading zeros; `"0"` → `malformed`.
7. Query: `values = {v for k, v in parse_qsl(parts.query, keep_blank_values=True) if
   k == "i"}`. No `i` key, or only blank values → `show_link`. More than one distinct
   non-blank value → `malformed`. The value must match `[0-9]{1,20}`, otherwise
   `malformed`. `episode_id = str(int(value))`; `"0"` → `malformed`. Every other
   parameter (`uo`, `mt`, `at`, `ct`, `l`, `r`, …) and the fragment are ignored.
8. Return `EpisodeRef(source="apple_podcasts", show_id=show_id, episode_id=episode_id,
   country=country, url=canonical_episode_url(country, show_id, episode_id))`.

So host problems come before path problems, and path problems before query problems.
For example, a Spotify link is `unsupported_host`, an Apple show link is `show_link`,
and an Apple channel link is `not_episode` whether or not it has `?i=`.
`parse_apple_url(parse_apple_url(x).url) == parse_apple_url(x)` for every accepted `x`.

### 4.3 `earmark.ingest`

```python
def enqueue_ingest(store: JobStore, request: IngestRequest) -> Enqueued:
    """Parse the URL, enqueue its canonical form. Raises UnsupportedUrlError."""
    ref = parse_apple_url(request.url)
    return store.enqueue(ref.url, prompt=request.prompt, shared_at=request.timestamp)
```

This is the only place that maps a request to `JobStore.enqueue`. It logs one INFO
line on `earmark.ingest`: `enqueued job <id> for <canonical url> (created=<bool>,
prompt=<yes|no>)`. It never logs the prompt text.

### 4.4 `earmark.api.schemas`

```python
class ApiModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IngestResponse(ApiModel):
    job_id: JobId
    status: JobStatus
    created: bool


class JobList(ApiModel):
    jobs: tuple[Job, ...]


class Health(ApiModel):
    status: Literal["ok"]
```

`GET /jobs/{id}` returns `Job` itself, the domain model from step 02. It contains no
secret: the lease token is not a `Job` field. These models deliberately do not subclass
`earmark.models.Model`, so step 02's test that every `Model` round-trips is unaffected.

### 4.5 `earmark.api.auth`

```python
PUBLIC_PATHS: Final = frozenset({"/healthz"})
WWW_AUTHENTICATE: Final = 'Bearer realm="earmark"'
UNAUTHORIZED_DETAIL: Final = "missing or invalid API token"


class ApiTokenError(ValueError):
    """The configured token is unusable. str(exc) names the variable, never the value."""


def require_api_token(settings: Settings) -> SecretStr: ...
def token_digest(token: str) -> bytes: ...  # sha256 of token.encode("utf-8", "surrogatepass")
def bearer_credentials(header: str | None) -> str | None: ...
def token_matches(presented: str, expected_digest: bytes) -> bool: ...


class BearerAuthMiddleware:
    def __init__(
        self, app: ASGIApp, *, token: SecretStr, public_paths: frozenset[str] = PUBLIC_PATHS
    ) -> None: ...

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None: ...
```

- `require_api_token`: if `settings.api_token` is `None` or its value is blank after
  `strip()`, raise `ApiTokenError("EARMARK_API_TOKEN is not set; refusing to serve the
  API without a token. Set it in the environment or in .env (for example to the output
  of: python -c 'import secrets; print(secrets.token_urlsafe(32))').")`. If any
  character is outside `"!"`..`"~"` (0x21–0x7E: printable ASCII, no spaces), raise
  `ApiTokenError("EARMARK_API_TOKEN must be printable ASCII without spaces, because it
  is sent in an HTTP header.")`. Otherwise return the `SecretStr`.
- `bearer_credentials`: `None` or `""` → `None`. Split with
  `scheme, _, rest = header.partition(" ")`. If `scheme.lower() != "bearer"`, return
  `None`; the scheme is case-insensitive per RFC 7235. Return `rest.strip()`, or `None`
  if that is empty.
- `token_matches`: `hmac.compare_digest(token_digest(presented), expected_digest)`.
  Call it as `hmac.compare_digest` through `import hmac`, so a test can spy on it.
  Hashing both sides first makes the comparison independent of length.
  `compare_digest` on raw strings can leak the length and raises `TypeError` on
  non-ASCII `str`.
- `BearerAuthMiddleware.__init__` computes `token_digest(token.get_secret_value())`
  once and keeps only the digest.
- `__call__`:
  - `scope["type"] == "http"`: if `scope["path"] in public_paths`, pass through.
    Otherwise take `credentials = bearer_credentials(Headers(scope=scope).get(
    "authorization"))`. If it is `None` or `token_matches` is false, send
    `JSONResponse({"detail": UNAUTHORIZED_DETAIL}, status_code=401,
    headers={"WWW-Authenticate": WWW_AUTHENTICATE})` and return without calling the
    app or reading the body. Log a WARNING on `earmark.api.auth`:
    `rejected <METHOD> <path> from <client host>: <no bearer token|wrong token>`. Never
    log the header.
  - `scope["type"] == "websocket"`: the same check (no public paths). On failure, send
    `WebSocketClose(code=1008)`. No websocket routes exist, but a future one is covered.
  - Any other scope (`lifespan`): pass through without touching headers.

The same `WWW-Authenticate` value is sent for a missing and for a wrong token, so the
response does not say which it was.

### 4.6 `earmark.api.app`

```python
def create_app(settings: Settings, *, store: JobStore | None = None) -> FastAPI: ...
def describe_validation_errors(errors: Sequence[Mapping[str, Any]]) -> str: ...
```

`create_app`, in this order:
1. `token = require_api_token(settings)`. Raises before anything touches the disk.
2. `store = store if store is not None else open_job_store(settings)`.
3. `app = FastAPI(title="earmark", version=__version__, docs_url=None, redoc_url=None,
   openapi_url=None, swagger_ui_oauth2_redirect_url=None, telemetry={"tracing": False,
   "metrics": False, "logs": False, "operation_spans": False, "auto_configure":
   False})`.
4. `app.include_router(build_router(store))`.
5. `app.add_exception_handler(RequestValidationError, _on_validation_error)`. The
   handler returns 422 `{"detail": describe_validation_errors(exc.errors())}`.
6. `app.add_exception_handler(sqlite3.Error, _on_store_error)`. The handler logs with
   `logger.exception("job store error")` and returns 503
   `{"detail": "the job store is unavailable; try again shortly"}` with
   `Retry-After: 5`.
7. `app.add_middleware(BearerAuthMiddleware, token=token)`.

In `src`, handlers narrow `exc` with `isinstance` plus `raise TypeError`, not `assert`
(ruff S101).

### 4.7 `earmark.api.routes`

```python
MAX_BODY_BYTES: Final = 64 * 1024


def build_router(store: JobStore) -> APIRouter: ...
```

`MAX_BODY_BYTES` covers the largest valid body with room to spare: 4000 prompt
characters at up to 12 bytes each (a JSON `\uXXXX\uXXXX` surrogate-pair escape), 2048
URL characters at up to 6 bytes each, and a timestamp, about 61 KiB. It is defined here
because the route decorator uses it. `app.py` imports `routes.py`, never the reverse.

The routes are closures over `store`, all plain `def` (FastAPI runs them in its thread
pool; `JobStore` is thread-safe):

| Route | Signature | Result |
|---|---|---|
| `POST /ingest` | `def ingest(body: IngestRequest, response: Response) -> IngestResponse`, decorated with `status_code=202, max_body_size=MAX_BODY_BYTES` | Calls `enqueue_ingest`. `UnsupportedUrlError` → `HTTPException(422, detail=exc.message)`. Sets `response.headers["Location"] = f"/jobs/{job.id}"`. Returns `IngestResponse(job_id=job.id, status=job.status, created=enqueued.created)`. A dedupe hit is also 202. |
| `GET /jobs/{job_id}` | `def get_job(job_id: str) -> Job` | `store.get(job_id)`; `None` → `HTTPException(404, detail="job not found")`. A malformed id is a 404, not a 422, because `store.get` returns `None` without querying. |
| `GET /jobs` | `def list_jobs(limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = 20, status: JobStatus \| None = None) -> JobList` | `JobList(jobs=tuple(store.list_recent(limit, status=status)))`, newest first. |
| `GET`/`HEAD /healthz` | `@router.api_route("/healthz", methods=["GET", "HEAD"])`, `def healthz() -> Health` | `Health(status="ok")`. Touches neither the store nor the settings, and reports no version (it is unauthenticated). |

### 4.8 `earmark serve`

```python
@app.command("serve")
def serve_command() -> None:
    """Run the ingest API on EARMARK_HOST:EARMARK_PORT."""
```

1. `settings = _load_or_exit()` (an invalid config exits 2, as `config` does).
2. `configure_logging(settings.log_level, settings.secret_values())`.
3. Import lazily: `from earmark.api.app import create_app` and
   `from earmark.api.auth import ApiTokenError`.
4. `api = create_app(settings)`:
   - On `ApiTokenError`, echo `f"earmark serve: {exc}"` to stderr and exit **2**.
   - On `DatabaseSetupError`, `sqlite3.Error`, or `OSError`, echo
     `f"earmark serve: cannot open the job store: {exc}"` to stderr and exit **1**.
5. `import uvicorn` (lazy), then `uvicorn.run(api, host=settings.host,
   port=settings.port, log_config=None, server_header=False)`.

There are no `--host`/`--port` options; the settings are the interface. Importing
`earmark.cli` must not import `fastapi`, `starlette`, or `uvicorn`.

### 4.9 HTTP surface summary

| | Request | Responses |
|---|---|---|
| `POST /ingest` | `Authorization: Bearer <token>`, `Content-Type: application/json`, body `{"url": str, "prompt"?: str \| null, "timestamp"?: ISO 8601 with offset \| null}`. Unknown keys are rejected. | 202 `{"job_id", "status", "created"}` and `Location: /jobs/<id>`. 401, 413, 422 `{"detail": str}`, 503. |
| `GET /jobs/{id}` | auth | 200 `Job` JSON; 401; 404 |
| `GET /jobs?limit=1..500&status=queued\|running\|done\|failed\|skipped` | auth | 200 `{"jobs": [Job, …]}`; 401; 422 |
| `GET`/`HEAD /healthz` | none | 200 `{"status": "ok"}` |
| anything else | auth | 404/405 with a token, 401 without |

## 5. Behaviour and edge cases

1. **Extra keys in the body stay forbidden.** Step 02 left this to this step. The
   client is a Shortcut the user builds by hand from `docs/shortcut.md`. Its JSON body
   is exactly the dictionary the user typed, and Shortcuts adds no fields of its own.
   The likely mistake is a typo: `promt` would be silently dropped under
   `extra="ignore"`, and the episode would get the default analysis instead of the
   user's question, with nothing to show why. Rejecting the key gives
   `unknown field 'promt' (allowed: url, prompt, timestamp)` the first time the
   Shortcut is tried. The Shortcut and the server have one owner and change together,
   so the forward compatibility that ignoring would buy is worth little. A typo in
   `timestamp` is also caught, rather than silently replaced by the server time.
2. **What goes into the store is the canonical URL.** Use
   `https://podcasts.apple.com/{country}/podcast/id{show}?i={episode}`, never the
   string the client sent. Step 03 dedupes on the exact string, so the same episode
   shared from the share sheet, from a copied link with `uo=4`, with a different slug,
   from the legacy host, over `http`, with `&r=` (share from time), or with an
   upper-case country is **one** active job. Different countries stay different jobs.
   The country selects the iTunes storefront for step 07, and the transcript cache is
   keyed by `(show_id, guid)`, so the work is still shared. `Job.url`, and therefore
   `Episode.url` and the note's `url` frontmatter, are the canonical link, which is
   cleaner than a tracking-laden share link. The original string is not kept.
3. **Prompt normalisation lives in `IngestRequest`.** Whitespace-only and empty
   prompts become `None`. Other prompts are stripped of surrounding whitespace, since
   Shortcuts' "Ask for Input" can add a trailing newline, and internal whitespace is
   kept. `None` and the stripped text are what reach `enqueue`. That makes a blank
   prompt dedupe with a missing prompt, which is what step 03 relied on. A non-string
   prompt (`5`, `true`) is a 422, because pydantic's lax mode does not coerce numbers
   to `str`.
4. **Timestamp.** It must be timezone-aware (pydantic: `Input should have timezone
   info`). Numbers are accepted as Unix seconds, which pydantic turns into UTC. A value
   that cannot be converted to UTC, such as `0001-01-01T00:00:00+05:00`, is rejected.
   Otherwise `JobStore._to_db` would raise `OverflowError` and the request would get a
   500. There is no plausibility window; step 18 may add one if digests need it.
   `shared_at` is the request timestamp if given, else the store clock at enqueue. A
   dedupe hit keeps the first job's `shared_at`.
5. **Auth comes first.** The middleware runs before routing, body reading, and
   validation. A request without a valid token gets 401 whatever its body is (bad
   JSON, too large, wrong fields) and whatever its path is (unknown paths and `/docs`
   included). The body is never read. `/healthz` is matched on the exact
   `scope["path"]`. If the app is ever mounted under a `root_path`, `/healthz` would
   require auth: this fails closed, which is acceptable. `HEAD /healthz` is public too.
6. **The 422 message** (`describe_validation_errors`). For each error, in order,
   compute one message. Deduplicate, join with `"; "`, and cap at 300 characters
   (append `"…"` when cut). `field` is `".".join(str(p) for p in loc[1:])`, the
   location without its `body`/`query`/`path` prefix.
   - `json_invalid` → `request body is not valid JSON`
   - `missing` with `loc == ("body",)` → `request body is missing; send a JSON object
     with a url`
   - `model_attributes_type`, `model_type`, or `dict_type` with `loc == ("body",)` →
     `request body must be a JSON object sent with Content-Type: application/json`
   - `extra_forbidden` under `body` → `unknown field '<field[:40]>' (allowed: url,
     prompt, timestamp)`. The allowed list is built from `IngestRequest.model_fields`.
   - `missing` → `<field> is required`
   - a type in `INGEST_ERROR_TYPES` → `msg` unchanged (it already names the field)
   - any other error whose `field` is `timestamp` → `timestamp: <msg> (send ISO 8601
     with a UTC offset, e.g. 2026-11-19T19:03:03-08:00)`
   - anything else → `<field>: <msg>`, or just `<msg>` if `field` is empty

   `input` and `ctx` are never used, so nothing the client sent is echoed back except
   an unknown key's name, truncated.
7. **URL errors are 422** with the `MESSAGES` text as `detail`, and nothing is
   enqueued. The checks run in the order host, then path, then query (section 4.2).
8. **Body limit.** 64 KiB on `POST /ingest` only. GET routes read no body. A
   `Content-Length` over the limit and a chunked body over the limit both get 413 and
   enqueue nothing.
9. **Store errors.** Any `sqlite3.Error` escaping a route (for example
   `database is locked` after the 10 s busy timeout) becomes 503 with `Retry-After: 5`,
   so the Shortcut shows a retryable message instead of a bare 500. Other unexpected
   exceptions stay 500, from Starlette's `ServerErrorMiddleware`, with `debug=False`
   and so no traceback in the response.
10. **No outbound I/O in the handler.** `enqueue_ingest` parses a string and writes
    SQLite. There is no DNS and no fetch: resolving the episode is step 07's job, in
    the worker.
11. **Token hygiene.** The token lives in `Settings` as a `SecretStr`, and the
    middleware keeps only its SHA-256 digest. `configure_logging` is given
    `settings.secret_values()` before uvicorn starts, and uvicorn runs with
    `log_config=None`, so its records go through the redacting formatter. No error
    message includes the token.
12. **The default country** is `us`, used only for links with no country segment.
    The share sheet always includes one (section 2.9). There is no setting for it.
13. **Not normalised:** the slug, `l=` (language), and the fragment are dropped.
    `embed.podcasts.apple.com` and `music.apple.com` are not accepted. Extracting a URL
    from surrounding text (`"Title\nhttps://…"`) is not done: whitespace inside the
    string is `malformed`. Step 16's Shortcut passes the URL itself ("Get URLs from
    Input").

## 6. Tests

Shared fixtures in `tests/test_api.py`:

- `TOKEN = "test-token-6b1f0e"`.
- `settings(tmp_path)` returns
  `Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")`.
- `store` is a `JobStore(open_database(tmp_path / "earmark.db"), clock=clock)` with
  the `MutableClock` from `tests/factories.py`.
- `client` yields from
  `with TestClient(create_app(settings, store=store), headers={"Authorization": f"Bearer {TOKEN}"}) as c:`.
- `anon` is the same client without the header.

URL fixtures are literals built from the live shapes in section 2.9 (show
`1200361736`, episode `1000802907981`). There are no recorded files and nothing to
download.

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | A missing or wrong token returns 401 with `WWW-Authenticate`, and the comparison is constant-time | `test_api.py::test_missing_token_401`, parametrized over `POST /ingest`, `GET /jobs`, `GET /jobs/<hex>`, `GET /nope`, `GET /docs`: status 401, `WWW-Authenticate == 'Bearer realm="earmark"'`, detail `UNAUTHORIZED_DETAIL`. `test_wrong_token_401` covers a wrong token, a token with an extra character, `Basic <token>`, `Bearer` with nothing after it, and `Bearer  ` with spaces only. `test_bearer_scheme_case_insensitive` (`bearer <token>` → 202). `test_non_ascii_authorization_header_401` sends raw latin-1 bytes per section 2.1 and gets 401, not 500. `test_token_compared_with_compare_digest`: monkeypatch `hmac.compare_digest` with a spy that wraps the real one; one authenticated request calls it exactly once with two 32-byte `bytes`, and the wrong-token request calls it too. `test_auth_runs_before_body_parsing`: `anon` posting `{not json`, a 100 KiB body, and `{"promt": 1}` gets 401 each time, and the store stays empty. `test_websocket_without_token_refused` (`websocket_connect("/x")` raises `WebSocketDisconnect` with code 1008). Unit tests in the same file: `test_bearer_credentials` (table of header → result) and `test_token_matches` (true for the token, false for a prefix, a superset, the empty string, and a non-ASCII string) |
| A2 | A valid request returns 202 with a `job_id`, and that job is `queued` in the store | `test_api.py::test_ingest_returns_202_and_queues`: posts a share-sheet URL with prompt and timestamp. Checks 202; `body == {"job_id": …, "status": "queued", "created": True}`; `Location == f"/jobs/{job_id}"`; `store.get(job_id)` has status `queued`, kind `ingest`, `url` equal to the canonical URL, the prompt, and `shared_at` equal to the timestamp as an instant. `test_ingest_shared_at_defaults_to_now` (no timestamp → `shared_at == clock.now`). `test_get_job_after_ingest` (`GET /jobs/{id}` → 200, and `Job.model_validate(r.json())` equals `store.get(id)`) |
| A3 | The handler performs no outbound I/O, shown by the suite's socket block | `test_api.py::test_ingest_makes_no_outbound_io`: in the same test, first `pytest.raises(SocketBlockedError)` around `socket.socket(AF_INET, SOCK_STREAM)` and around `socket.getaddrinfo("podcasts.apple.com", 443)`, proving the block is active, then a successful ingest (202). The test is not marked, so it never gets sockets back. The whole of `test_api.py` runs under the same block |
| A4 | A non-Apple host, a show link without `?i=`, and a malformed URL each return 422 with a short human-readable message | `test_api.py::test_ingest_rejects_url`, parametrized: `https://open.spotify.com/episode/abc` → `MESSAGES["unsupported_host"]`; `https://podcasts.apple.com/us/podcast/the-daily/id1200361736` → `MESSAGES["show_link"]`; `not a url` → `MESSAGES["malformed"]`; `https://podcasts.apple.com/us/channel/x/id1?i=2` → `MESSAGES["not_episode"]`. Each gets 422 with `detail` equal to the constant, under 100 characters, and the store stays empty. `test_resolve_apple.py::test_messages_short_and_never_echo_input` checks every `MESSAGES` value is at most 100 characters, and that for a URL containing `sentinel-9c2e` the raised message does not contain it |
| A5 | `parse_apple_url` handles URLs with and without a slug, with a country code, with extra query parameters, and the legacy `itunes.apple.com` host | `test_resolve_apple.py::test_parses_episode_urls`, parametrized, each case asserting the full `EpisodeRef` (source, show id, episode id, country, canonical url): the lookup's `trackViewUrl` with slug and `uo=4`; the canonical `og:url` form (gb); no slug; no country (`us`); upper-case `US`; `uo=4&mt=2&at=x&ct=y&l=es&r=754` in any order with `i` first, middle, or last; `itunes.apple.com/us/podcast/the-daily/id…?i=…&mt=2`; `http://`; host `Podcasts.Apple.COM`; host with a trailing dot; a trailing slash after the id; a doubled slash; a fragment; scheme-less `podcasts.apple.com/…`; a percent-encoded Unicode slug; a raw Unicode slug; `ID` in upper case; leading zeros in the ids (stripped); `?i=5&i=5` (the same value twice). `test_canonical_is_fixed_point` (parse, then parse the canonical URL, gives an equal ref, for every case above). `test_canonical_episode_url` (golden string) |
| A6 | A blank or whitespace-only prompt is stored as null; over-long prompts and oversized bodies are rejected | `test_models.py::test_ingest_blank_prompt_is_none`, parametrized over `""`, `" "`, `"\n\t"`, `" "`, `"　"`. `test_ingest_prompt_stripped` (`"  why?\n"` → `"why?"`, internal newlines kept). `test_ingest_prompt_length_cap` (exactly `MAX_PROMPT_CHARS` accepted, including when padded with whitespace; one more is rejected with type `prompt_too_long`). `test_api.py::test_ingest_blank_prompt_stored_as_null` (POST with `"prompt": "   "` → stored `prompt is None`; a following POST with no prompt is a dedupe hit with the same `job_id` and `created` false). `test_ingest_overlong_prompt_422` (detail is the `prompt_too_long` message; nothing enqueued). `test_ingest_body_too_large_413`, parametrized over a `json=` body just over `MAX_BODY_BYTES` (with Content-Length) and a generator `content=` (chunked) with `Content-Type: application/json`: 413 and the store is empty. `test_ingest_max_valid_body_accepted` (a 4000-character prompt of `"😀"` sent as `content=json.dumps(body).encode()` with `Content-Type: application/json`; `json.dumps` defaults to `ensure_ascii=True`, so this is about 48 KiB of `😀` escapes; returns 202) |
| A7 | `/healthz` returns 200 without auth; interactive docs are off by default | `test_api.py::test_healthz_public` (`anon` GET → 200 `{"status": "ok"}`; HEAD → 200; no store access, checked by passing a store whose methods raise). `test_docs_disabled`, parametrized over `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`: with a token → 404 |
| A8 | `earmark serve` refuses to start with an empty token and says why | `test_cli.py::test_serve_refuses_without_token`, parametrized over unset and `EARMARK_API_TOKEN="   "`: exit code 2, stderr contains `EARMARK_API_TOKEN is not set`, the monkeypatched `uvicorn.run` was not called, and `./data` was not created (cwd is `tmp_path` via the autouse fixture). `test_serve_rejects_unsafe_token` (`"tok en"`, `"tök"`) → exit 2, message about printable ASCII, the token value absent from the output. `test_api.py::test_create_app_requires_token` raises `ApiTokenError` for the same values, the message never containing the value |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | Re-shares of one episode dedupe through canonicalisation; a different prompt or country does not | `test_ingest.py::test_variants_dedupe`: five spellings of the same episode (share sheet, no slug, legacy host, `&r=754`, `http` + `US`) give one job (`created` true once). `test_ingest.py::test_prompt_and_country_distinguish` (`prompt="a"` vs `None` vs `"b"`; `gb` vs `us`: separate jobs). `test_ingest.py::test_unsupported_url_enqueues_nothing` (raises `UnsupportedUrlError` with the right `problem`; the store is empty). `test_api.py::test_ingest_dedupe_hit_returns_existing_job` (the second POST: 202, same id, `created` false, `status` the existing job's, including `running` after `store.claim_next()`) |
| B2 | The parser rejects everything else with the right problem | `test_resolve_apple.py::test_rejects_hosts` (Spotify, `apple.com`, `music.apple.com`, `embed.podcasts.apple.com`, `podcasts.apple.com.evil.example`, `evilpodcasts.apple.com`, `localhost` → `unsupported_host`). `test_rejects_show_links` (no query, `?i=`, `?uo=4`, `?i=&uo=4` → `show_link`). `test_rejects_non_episode_paths` (`/us/channel/x/id1`, `/us/genre/x/id26`, `/us/podcast/slug`, `/`, `/us/podcast/a/b/id1?i=2` → `not_episode`). `test_rejects_malformed` (`""`, `"   "`, `"not a url"`, `ftp://podcasts.apple.com/…`, `https://u:p@podcasts.apple.com/…`, `:8443` port, `?i=abc`, `?i=1&i=2`, `?i=0`, `id0`, Arabic-Indic digits in the id and in `i`, a 21-digit id, internal whitespace, a tab, NUL, `MAX_URL_CHARS + 1` characters, `https://[::1`) |
| B3 | Validation errors are one short string and never echo input | `test_api.py::test_validation_messages`, parametrized over: bad JSON → `request body is not valid JSON`; empty body; JSON `null`; a JSON array; a JSON object without `Content-Type` → the Content-Type message; `{}` → `url is required`; `{"url": ["sentinel-4a7d"]}` → starts with `url:`; `{"url": "…", "promt": "sentinel-4a7d"}` → `unknown field 'promt' (allowed: url, prompt, timestamp)`; a naive timestamp → contains `timestamp:` and `UTC offset`; `"prompt": 5`. Each response is 422, `detail` is a `str` of at most 300 characters, and `sentinel` never appears. `test_describe_validation_errors_dedupes_and_caps` (a unit test on synthetic error dicts) |
| B4 | `IngestRequest` normalisation is idempotent and the URL is capped | `test_models.py::test_ingest_url_stripped_and_capped` (`"  u \n"` → `"u"`; `""` and `"  "` → `url_empty`; `MAX_URL_CHARS + 1` → `url_too_long`). `test_ingest_timestamp_out_of_range` (`0001-01-01T00:00:00+05:00` → `timestamp_out_of_range`; `-08:00` accepted with its offset kept). `test_ingest_normalised_round_trip` (a padded prompt round-trips through JSON to an equal model). The existing `test_json_round_trip` and `test_extra_field_rejected` still pass unchanged |
| B5 | `GET /jobs/{id}` and `GET /jobs` | `test_api.py::test_get_job_unknown_404` (32 random hex characters → 404 `job not found`; `"nothex"` → 404). `test_list_jobs` (three ingests at advancing clock times come back newest first; `limit=2` gives two; `status=queued` filters; after `claim_next`, `status=running` shows one). `test_list_jobs_bad_params_422` (`limit=0`, `limit=501`, `status=bogus` → 422 with a `str` detail) |
| B6 | A store failure is a 503, not a 500 | `test_api.py::test_store_error_503`: a `JobStore` subclass whose `enqueue` raises `sqlite3.OperationalError("database is locked")` gives 503, `Retry-After: 5`, and the detail message |
| B7 | `earmark serve` runs uvicorn with earmark's logging and the configured address | `test_cli.py::test_serve_runs_uvicorn` (with `restore_root_logging`; env token, `EARMARK_PORT=8799`; monkeypatch `uvicorn.run` to record its arguments). Exit 0; one call; the first argument is a `FastAPI`; `host == "127.0.0.1"`, `port == 8799`, `log_config is None`, `server_header is False`; `tmp_path / "data" / "earmark.db"` exists. `test_serve_store_error_exits_1` (`EARMARK_DATA_DIR` points at a regular file → exit 1, stderr starts with `earmark serve: cannot open the job store`). `test_serve_never_prints_token` (the token appears in neither stdout nor stderr, on success or refusal). `test_cli_import_is_light` (a subprocess `import earmark.cli` loads none of `fastapi`, `starlette`, `uvicorn`) |
| B8 | The API package keeps the light-import rules | `test_resolve_apple.py::test_apple_module_imports_light` (a subprocess `import earmark.resolve.apple, earmark.ingest` loads none of `fastapi`, `starlette`, `uvicorn`, `httpx2`) |

Test-writing notes:
- Assert exact `detail` strings through the exported constants (`MESSAGES`,
  `UNAUTHORIZED_DETAIL`) rather than copied literals, except the formatter outputs in
  B3, which are the contract.
- For A1's spy: `real = hmac.compare_digest`, then
  `monkeypatch.setattr(hmac, "compare_digest", spy)`, where `spy` records its arguments
  and returns `real(a, b)`.
- For A7's "no store access", subclass `JobStore` with every public method raising
  `AssertionError`, and build the app with that store.
- `uvicorn.run` monkeypatch: `monkeypatch.setattr(uvicorn, "run", fake)`. The CLI
  imports `uvicorn` inside the command, so it sees the patched attribute.

## 7. Verification

`SCRATCH` is the verifier's scratchpad directory.

```bash
cd /home/sandy/dev/earmark
uv sync
make check
uv run ruff format --check docs/
uv run pytest -q tests/test_api.py tests/test_resolve_apple.py tests/test_ingest.py \
  tests/test_models.py tests/test_cli.py

# Light imports
uv run python -c 'import sys, earmark.cli; print(sorted(m for m in ("fastapi","starlette","uvicorn") if m in sys.modules))'
# expect: []

# No HTTPBearer/Depends-based auth, no app-level max_body_size, no plain == on the token
grep -n "HTTPBearer\|max_body_size\|compare_digest" src/earmark/api/*.py
```

Live smoke. It needs no network beyond loopback. Run it from `$SCRATCH`, so no `.env`
in the working directory can supply a token. Note that `EARMARK_API_TOKEN=` (empty)
counts as unset and falls through to `.env`.

```bash
cd "$SCRATCH"
E="uv run --project /home/sandy/dev/earmark earmark"
export EARMARK_API_TOKEN=smoke-token-1 EARMARK_DATA_DIR="$SCRATCH/data" EARMARK_PORT=18787
$E serve > "$SCRATCH/serve.log" 2>&1 &
SERVE_PID=$!
for i in $(seq 50); do curl -sf http://127.0.0.1:18787/healthz && break; sleep 0.1; done
B=http://127.0.0.1:18787
URL='https://podcasts.apple.com/us/podcast/did-a-i-break-the-internet-and-other-things/id1200361736?i=1000802907981&uo=4'

curl -si $B/healthz                                    # 200 {"status":"ok"}, no "server:" header
curl -si -X POST $B/ingest -H 'Content-Type: application/json' -d "{\"url\":\"$URL\"}"
                                                       # 401, www-authenticate: Bearer realm="earmark"
curl -si -X POST $B/ingest -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"url\":\"$URL\",\"prompt\":\"  \"}"
                                                       # 202 {"job_id":…,"status":"queued","created":true}, location: /jobs/…
curl -s -X POST $B/ingest -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://itunes.apple.com/US/podcast/x/id1200361736?i=1000802907981&mt=2"}'
                                                       # same job_id, "created":false
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" $B/jobs   # one job, prompt null,
                                                       # url https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981
curl -s -X POST $B/ingest -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://podcasts.apple.com/us/podcast/the-daily/id1200361736"}'
                                                       # 422 {"detail":"This links to a whole show. …"}
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $EARMARK_API_TOKEN" $B/docs
                                                       # 404
kill $SERVE_PID; wait $SERVE_PID 2>/dev/null
grep -c smoke-token-1 "$SCRATCH/serve.log"             # 0: the token is never logged
grep 'rejected POST /ingest' "$SCRATCH/serve.log"      # the 401 was logged without the header

EARMARK_API_TOKEN= $E serve; echo "exit=$?"
                                                       # message naming EARMARK_API_TOKEN, exit=2
unset EARMARK_API_TOKEN EARMARK_DATA_DIR EARMARK_PORT
rm -rf "$SCRATCH/data" "$SCRATCH/serve.log"
```

Also check:
- `git status` shows only the files in section 3 and this plan.
- No `# type: ignore` or `noqa` in the new files, and no loosened lint, type, or test
  settings.
- `docs/ARCHITECTURE.md` matches section 8.

Not verifiable in the sandbox: the exact URL the iOS share sheet gives a Shortcut today
(section 2.9), and the Shortcut's real request. Step 16 covers them.

## 8. Deviations

1. **`POST /ingest` response.** The architecture said `{job_id, status}`. It is now
   `{job_id, status, created}` plus a `Location` header, so the Shortcut can tell
   "queued" from "already in progress". `docs/ARCHITECTURE.md` is updated.
2. **Canonical URL in jobs.** `Job.url` for ingest jobs, and so `Episode.url`, is the
   canonical `https://podcasts.apple.com/{country}/podcast/id{show}?i={episode}`, not
   the string the user shared. Step 02 had described `Episode.url` as "the URL the user
   shared". This is what makes re-shares dedupe (section 5.2). The architecture's
   domain-model and security sections are updated.
3. **`IngestRequest` normalises** its URL, prompt, and timestamp, and stays
   `extra="forbid"` (section 5.1). Step 02 allowed this step to relax it; it does not.
   The architecture is updated.
4. **Auth is a middleware covering every path except `/healthz`**, not a per-route
   dependency, so 401 wins over 404, 413, and 422 (section 2.3). A websocket without a
   token is closed with 1008.
5. **Status codes not in the architecture's table:** 413 (body over 64 KiB), 503 (store
   unavailable), 404 for an unknown job. `GET /jobs` takes `limit` and `status`.
   Recorded in the architecture.
6. **New module `earmark/ingest.py`**, so the API and step 05's `earmark submit` share
   one request-to-enqueue path. Added to the module layout.
7. **FastAPI's built-in OpenTelemetry is switched off explicitly** (section 2.6). This
   adds nothing, but it is a choice the brief did not mention.
8. **Token rules beyond "empty".** A whitespace-only token is treated as empty. A token
   containing characters that cannot travel in an HTTP header (anything outside
   printable ASCII) is refused at startup, because it could never match.

`docs/ARCHITECTURE.md` was edited with this plan. The changes:
- Module layout: `ingest.py` added; the `api/` files named.
- Domain models: the `IngestRequest` validators and forbidden extras; `EpisodeRef.url`
  is canonical; `Job.url` (and `Episode.url`) is canonical.
- HTTP API: the auth middleware, the token rules, docs off, the error shape, 413, 503,
  the `POST /ingest` response with `created` and `Location`, and the `GET /jobs` params.
- Security, ingest bullet: exact host match, the canonical URL stored, no outbound I/O.

## 9. Out of scope

- The worker, `earmark submit`, and `earmark jobs` (step 05). Step 05's `submit`
  should call `earmark.ingest.enqueue_ingest` so CLI submissions are canonicalised the
  same way.
- Resolving the episode, the iTunes lookup, and using `EpisodeRef.country` (step 07).
  The resolver receives the canonical URL and may call `parse_apple_url` again.
- Using the share-from-time `&r=` offset, for example to highlight a moment in the note.
- Other sources (YouTube, articles) and a source registry. `POST /ingest` calls
  `parse_apple_url` directly; the backlog seam is `enqueue_ingest`.
- `GET /search` (step 14), `/episodes` (step 17), `doctor`'s token check (step 15, which
  can reuse `require_api_token`).
- The Shortcut itself, Tailscale binding, and TLS (step 16).
- Rate limiting. The API is only reachable on the tailnet.
