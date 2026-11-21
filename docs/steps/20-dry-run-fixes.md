# Step 20: Fix: dry-run findings (third-party URL logging, noisy output)

## 1. Outcome

When this step is done:

- Every log record goes through earmark's one stderr handler, which now does three things
  on top of secret redaction. It drops records below WARNING from the wire loggers
  (`httpx`, `httpcore`, `httpx2`, `httpcore2`, `urllib3`, `anthropic`, `huggingface_hub`),
  whatever level a library or an environment variable gave those loggers. It rewrites URLs
  in third-party records and in every traceback to `scheme://host[:port]/…`, and drops the
  query from HTTP request lines. And it is the only handler: `huggingface_hub`'s own stderr
  handler is removed, so its warnings print once, formatted and redacted.
- A model download shows the Hugging Face "unauthenticated requests" hint once, as one
  log record. earmark still never asks for, reads, or passes an HF token.
- `earmark serve` no longer logs `/search` queries. uvicorn's access line keeps method,
  route, and status.
- Job errors from unexpected library exceptions have URL paths and queries scrubbed.
- `earmark digest --since …` no longer ends with "; the window is not over yet". A
  `--week` that has not ended still does.
- New tests drive real requests through httpx2/httpcore2, the anthropic SDK, and uvicorn
  in the default suite, and through huggingface_hub, httpx/httpcore, urllib3, fastembed,
  and LanceDB under `-m index`, all with TCP sockets blocked.

## 2. Findings

Checked on 2026-11-21 in scratch venvs under `/tmp/em20` (`UV_PROJECT_ENVIRONMENT` and
`UV_CACHE_DIR` set there, `uv sync --frozen --extra transcribe`, then deleted and redone
with `--extra index`; the venv is deleted). Versions are the locked ones: huggingface-hub
1.33.0, hf-xet 1.8.0, httpx 0.28.1, httpcore 1.0.9, urllib3 2.8.0, requests 2.34.2,
fastembed 0.9.1, loguru 0.7.3, lancedb 0.41.0, faster-whisper 1.2.1, anthropic 2.12.0,
httpx2/httpcore2 2.16.2, uvicorn 0.54.0.

### 2.1 Finding 1 reproduced, live and offline

A live `faster_whisper.utils.download_model("tiny", cache_dir=…)` after
`configure_logging("DEBUG")` printed, among others:

```
INFO httpx: HTTP Request: HEAD https://huggingface.co/api/resolve-cache/models/Systran/faster-whisper-tiny/<sha>/config.json?%2FSystran%2F…&etag=%223baa18e… "HTTP/1.1 200 OK"
DEBUG httpcore.http11: receive_response_headers.complete return_value=(b'HTTP/1.1', 307, b'Temporary Redirect', [… (b'Location', b'/api/resolve-cache/…?…&etag=…') …])
```

- `httpx` (`httpx/_client.py:117`, `logging.getLogger("httpx")`) logs every request at
  INFO with `request.url` (path and query). It never logs at WARNING or above.
- `httpcore` (`httpcore.connection`, `httpcore.http11`, …) logs DEBUG traces only.
  `receive_response_headers.complete` includes every response header, so redirect
  `Location` values appear as relative paths with queries. A URL pattern
  (`scheme://…`) cannot recognise these. This is why scrubbing alone is not enough and the
  wire loggers must be gated.
- The same HF requests offline: `huggingface_hub.set_client_factory(lambda:
  httpx.Client(transport=httpx.MockTransport(handler), event_hooks={"request":
  [hf_request_event_hook]}, follow_redirects=True))` then `hf_hub_download(...)` gives
  the identical `httpx` lines with no socket. With `HF_HUB_OFFLINE=1` in the environment,
  patching `huggingface_hub.constants.HF_HUB_OFFLINE = False` is enough. HF reads that
  attribute on each call. The flow that worked offline: the mock answers `HEAD
  /org/repo/resolve/<sha>/config.json` with 307 and `Location:
  /api/resolve-cache/models/org/repo/<sha>/config.json?%2Forg%2Frepo%2Fresolve%2F<sha>%2Fconfig.json=&etag=%22abc%22`,
  and answers that URL (HEAD and GET) with 200, `X-Repo-Commit: <sha>`, `ETag: "abc"`,
  `Content-Length: 2`, body `{}`. `hf_hub_download("org/repo", "config.json",
  revision=<sha>, cache_dir=tmp)` then returns the snapshot path.

### 2.2 Finding 2: where each copy of the HF hint comes from

The text is not in the library. It is a server header: `X-HF-Warning: <topic>; Warning:
You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable
higher rate limits and faster downloads.` `huggingface_hub.utils._http._warn_on_warning_headers`
logs it with `logger.warning(message)` on `huggingface_hub.utils._http`, once per topic
per process (`_WARNED_TOPICS`).

It prints twice because of **a logger plus its own handler**. It is not `warnings.warn`,
and earmark never calls `logging.captureWarnings`. `huggingface_hub/utils/logging.py`
runs `_configure_library_root_logger()` at import. That adds a bare
`logging.StreamHandler()` (stderr, format `%(message)s`) to the `huggingface_hub` logger
and leaves `propagate=True`; the docstring says propagation is off by default, but the
code does not do that. So the record is written once by HF's handler (unformatted,
**unredacted**) and once by earmark's root handler. Live output:

```
Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN …
2026-11-21 05:48:49,664 WARNING huggingface_hub.utils._http: Warning: You are sending unauthenticated requests …
```

When the handler appears matters. `import huggingface_hub` is lazy and installs nothing.
The handler appears when `huggingface_hub.utils.logging` is first imported:
- `import fastembed` does it at once (probe: handler present right after the import).
- `faster_whisper/utils.py` only does `import huggingface_hub` and calls
  `huggingface_hub.snapshot_download` later. By the probe above, nothing is installed until
  that first call, which comes after any code that ran "right after the import". This was
  inferred from the source and the `import huggingface_hub` probe. The transcribe venv was
  gone by then, so it was not re-run with faster-whisper itself. The live run's duplicate
  appeared during the download, which is consistent with it. The forced import below makes
  the order irrelevant either way.

A prototype that removed handlers right after the import still printed the bare copy.
Forcing `importlib.import_module("huggingface_hub.utils.logging")` first and then removing
the handler gave exactly one copy (prototype output: one `WARNING
huggingface_hub.utils._http: …` line, `logging.getLogger("huggingface_hub").handlers ==
[]`). That import takes 9 ms, adds 53 modules, and does not import `httpx`.

Dead ends:
- `huggingface_hub.utils.logging.disable_propagation()` keeps only the unredacted copy.
- `HF_HUB_VERBOSITY=error` hides the hint, but also HF's retry and download warnings.
  HF's handler stays, and the variable is applied only at import.
- A `logging.Filter` on the `huggingface_hub` logger does not run for records created on
  child loggers such as `huggingface_hub.utils._http`.
- A handler removal that does not force the `utils.logging` import (see above).

Since `_WARNED_TOPICS` dedupes per process, "once" means once per worker process, which
the brief allows ("at most once").

### 2.3 Level quieting alone is defeated at import

`anthropic/_utils/_logs.py` `setup_logging()` runs when the SDK is imported. With
`ANTHROPIC_LOG=debug` it sets `anthropic` and `httpx2` to DEBUG. earmark imports the SDK
lazily, after `configure_logging`. Probe: `ANTHROPIC_LOG=debug`, then
`configure_logging("DEBUG")`, then `import anthropic`, then a DEBUG record on
`anthropic._base_client`. The record **was printed** (`Request options: TRANSCRIPT`), and
the levels afterwards were `anthropic 10`, `httpx2 10`. `HF_HUB_VERBOSITY` does the same
to `huggingface_hub` when `huggingface_hub.utils.logging` is imported (per its source,
`_configure_library_root_logger` calls `setLevel(_get_default_logging_level())`). A
handler-level filter is immune to this. The prototype filter dropped the same record with
`ANTHROPIC_LOG=debug` set.

### 2.4 Mechanism: gate by logger at the handler, plus content scrubbing

Both mechanisms are used, each where the other fails:

- **Gate (by logger name, at the handler).** The wire loggers' records below WARNING carry
  little an operator needs: request lines, connection traces, and the SDK's request
  bodies. Some of their URLs cannot be recognised by content (relative `Location`
  headers, `urllib3`'s `"GET /path?q HTTP/1.1"` after a space, transcript text in
  `anthropic` bodies). Dropping them by name at the handler is simple, certain, and
  survives library level resets (2.3). Logger levels are still set to WARNING, as today,
  so the records are not even built in the normal case.
- **Scrub (by content, third-party records and tracebacks).** WARNING records from the
  same libraries are useful and must stay: HF retries (`'{err}' thrown while requesting
  {method} {url}`), `Error while downloading from %s` (a signed CDN URL), and "Could not
  reach the Hub". Gating them at ERROR would hide them. Scrubbing keeps the text and the
  host and removes path and query. It also covers loggers nobody audited, such as
  `uvicorn.access` (2.5), fastembed's `logging.info` on the root logger, and any future
  library, at every level. Earmark's own messages are not scrubbed. They are host-only by
  contract, except ingest's canonical Apple URL
  (`https://podcasts.apple.com/<cc>/podcast/id<show>?i=<ep>`, built from parsed ids).
  Scrubbing would delete the episode id from that line.
- **Handler adoption.** Neither rule helps if a library prints through its own handler.
  So `huggingface_hub`'s handler is removed (2.2).

The prototype (scratch only) confirmed all three: `httpx` INFO gone, `anthropic` DEBUG gone
under `ANTHROPIC_LOG=debug`, HF hint once, a HF retry warning printed as `… requesting GET
https://cas-bridge.xethub.hf.co/…`, an `httpx.HTTPStatusError` traceback logged on
`earmark.pipeline.runner` printed `for url 'https://huggingface.co/…'`, the ingest line
unchanged, and uvicorn's line printed `"GET /search HTTP/1.1" 200`.

### 2.5 Additional finding: uvicorn's access log records the search query

`uvicorn/protocols/http/h11_impl.py:477` logs `'%s - "%s %s HTTP/%s" %d'` on
`uvicorn.access` at INFO, with `get_path_with_query_string(scope)`. `earmark serve` passes
`log_config=None`, so the record reaches earmark's handler (step 04 noted the path and
query and accepted it before search existed). Probe: a real `uvicorn.Server` on a unix
socket (`uvicorn.Config(app, uds=…, log_config=None, lifespan="off")` in a thread), and
`httpx2.Client(transport=httpx2.HTTPTransport(uds=…))` sending `GET
/search?q=private+thoughts`. The output was `INFO uvicorn.access:  - "GET
/search?q=private+thoughts HTTP/1.1" 200`. That breaks the architecture's "the query is
never logged" (step 14). This ran under `pytest --disable-socket --allow-unix-socket`, so
the test below can run in the default suite. uvicorn only installs signal handlers in the
main thread, so a thread is fine. Set `server.should_exit = True` and join to stop it.
httptools is not installed, so the h11 protocol is the one used.

### 2.6 Audit of every library earmark or its extras load

| Library | How it logs | URL path or query? | Treatment |
|---|---|---|---|
| `httpx` 0.28.1 (HF's client) | `httpx` INFO `HTTP Request: M URL "…"` | yes, full URL at INFO | gate |
| `httpcore` 1.0.9 | `httpcore.*` DEBUG traces with all response headers | yes, `Location` paths | gate |
| `httpx2`/`httpcore2` 2.16.2 (earmark, SDK) | same shapes | yes | gate (was level-only) |
| `anthropic` 2.12.0 | DEBUG request options (body) and `HTTP Response: POST https://…/v1/messages`; INFO `Retrying request to /v1/messages`; `ANTHROPIC_LOG` resets levels | yes, plus transcript | gate |
| `urllib3` 2.8.0 (requests, used by fastembed's GCS fallback) | DEBUG `http://h:80 "GET /p?q HTTP/1.1"`; INFO `Redirecting u -> u` (PoolManager); WARNING `Retrying (…) after connection broken by '…': <relative url>` | yes | gate. The WARNING can carry a relative path the scrubber cannot see. requests uses `max_retries=0`, so `increment` raises before that warning; earmark's only urllib3 caller cannot emit it (residual, see 5.3) |
| `requests` 2.34.2 | `NullHandler` only, no URL log calls | no | none |
| `huggingface_hub` 1.33.0 | own StreamHandler + propagation; DEBUG `Request <id>: M URL`; WARNING retries and download errors with full URLs; the `X-HF-Warning` hint | yes | gate below WARNING, scrub WARNING+, remove its handler |
| `hf_xet` 1.8.0 (Rust) | JSON lines at INFO to its own files `$HF_HOME/xet/logs/xet_<ts>_<pid>.log`, created on `import hf_xet` | in a live `tiny` download: hosts (`https://cas-server.xethub.hf.co`), request ids, hashes, local paths; **no** URL path or query | out of earmark's handler; documented, left as shipped (5.3) |
| fastembed 0.9.1 | loguru (already disabled for `fastembed`); `parallel_processor` uses root `logging.info` (unused path); one `print(f"Warning: Content-length header is missing … from {url}")` in the GCS fallback | the print: fastembed's static model-archive URL | loguru stays disabled; the root-logger record is scrubbed; the print cannot be filtered (5.3) |
| loguru 0.7.3 | separate system, default stderr sink | n/a | `disable("fastembed")` (step 14), proved offline below |
| lancedb 0.41.0, pyarrow 26.0.1 | Python loggers only in `streaming`, `remote`, `embeddings` (unused); Rust core logs only with `LANCE_LOG` | no (local paths) | scrub covers anything |
| faster-whisper 1.2.1 | `faster_whisper` INFO duration, VAD, language; downloads via `huggingface_hub.snapshot_download` with `tqdm_class=disabled_tqdm` | no | covered through `huggingface_hub` |
| ctranslate2 4.9.0, onnxruntime 1.32.0, tokenizers 0.23.3, av 18.1.0 | native stderr logging or none; open local files only | no request is made | none |
| uvicorn 0.54.0 | `uvicorn.access` INFO with path and query; `uvicorn.error` `running on http://h:p` | yes (`/search?q=`) | scrub drops the request-line query |
| filelock 4.2.12 | DEBUG lock paths under the model cache | no | none (DEBUG noise only) |
| fastapi, starlette, opentelemetry-api, charset_normalizer, asyncio, dotenv, pydantic-settings | none or no URLs | no | none |

Handler audit after `configure_logging` and importing fastembed, lancedb, pyarrow,
huggingface_hub, requests, urllib3, loguru, onnxruntime, tokenizers, anthropic, uvicorn,
fastapi: only `huggingface_hub` had a non-`NullHandler` (`charset_normalizer`,
`requests`, `urllib3` carry `NullHandler`). The transcribe extra was grepped for
`addHandler`, `basicConfig`, and `captureWarnings`. Nothing outside onnxruntime's `tools/`
and `transformers/` scripts matched, plus `av/logging.py:26`, a `logging.basicConfig()`
line right after documentation text that links to `logging.basicConfig`. It looks like a
docstring example, but that was not confirmed before the venv was removed. Either way,
`basicConfig` is a no-op once earmark's handler is on root. A slow test (6, P6) checks
for library handlers at run time.

### 2.7 Other observations

- **fastembed offline drive.** `load_fastembed_runtime().load("BAAI/bge-small-en-v1.5",
  <empty dir>, False)` raises `ValueError("Could not load model … from any source.")`
  with `HF_HUB_OFFLINE=1`. A loguru sink added with `level="TRACE"` received 0 records.
  After `logger.enable("fastembed")` it received 2 (`retrieve_model_gcs` and
  `download_model` errors, naming local paths). So this is a real, offline proof that the
  disable holds.
- **urllib3 offline drive.** `urllib3.HTTPConnectionPool("models.example", 80)` with
  `ConnectionCls` set to a subclass whose `connect()` opens an `AF_UNIX` socket to a
  stdlib `socketserver.UnixStreamServer` worked under `--disable-socket
  --allow-unix-socket`. It logged `DEBUG urllib3.connectionpool: http://models.example:80
  "GET /secret/path?sid=1 HTTP/1.1" 200 0`. Under mypy strict, subclassing a class from an
  `importlib` module is "subclassing Any", so the test builds the class with `type(...)`.
- **hf_xet.** `HF_XET_LOG_DEST=""` did **not** switch hf_xet to console: a log file was
  still created. Upstream `xet_runtime/src/logging/config.rs` maps an empty `log.dest` to
  console, a trailing `/` or an existing directory to a directory, and anything else to a
  file. Its defaults are `info` for files and `warn` for the console, with `RUST_LOG`
  overriding both (`init.rs`, `constants.rs`, read on GitHub `main`). The empty env value
  evidently never reaches that code. A live download also logged `telemetry accepted …
  xet_download_summary`: hf_xet reports a download summary to the Hub. The binary
  contains the strings `HF_HUB_DISABLE_TELEMETRY`, `DISABLE_TELEMETRY`, and
  `DO_NOT_TRACK`, so it probably honours them. That was not tested. These are outside
  this step (section 9). They are documented in the architecture.
- **Sandbox artefacts.** In the live run huggingface_hub also called `GET
  /api/agent-harnesses` and added `agent/claude-code` to its User-Agent. It does this
  when it detects an agent environment, so it happens in this sandbox and not in
  production. Ignore it.
- **`warnings.warn` in huggingface_hub** (symlinks unsupported, low disk space) prints
  through Python's warnings display. These are not request URLs and are not part of the
  duplicate. They are left alone.
- **Digest.** `cli.py:643` adds the tail whenever `window.end > now`. A `--since` window
  always ends at tomorrow's local midnight (`since_window`), so it always matches.
  `DigestWindow.weekly` (already present) tells the kinds apart.
- **Existing test that would go vacuous.** `tests/test_llm_anthropic.py::test_key_not_logged[True]`
  raises the `anthropic` logger to DEBUG to prove the key is absent "even unquieted".
  With the gate, those records never reach earmark's handler, so the test would pass
  without showing anything. It must attach its own plain handler (P7 in section 6).
- **Formatter detail.** `logging.Formatter.format` builds `formatMessage(record)`, then
  appends `record.exc_text` (cached on the record by whichever formatter ran first) and
  the stack. Scrubbing everything after the `formatMessage` prefix therefore also covers
  a traceback cached unscrubbed by another handler. Overriding `formatException` would
  not.
- **Snippets** in this plan were checked with `ruff format --check` (line length 100).

## 3. Files

| File | Change |
|---|---|
| `src/earmark/log.py` | `QUIET_LOGGERS` extended; new `LIBRARY_HANDLER_LOGGERS`, `OWN_LOGGER`, `URL_ELISION`, `scrub_urls`, `is_own_logger`, `is_quiet_logger`, `QuietLoggerFilter`, `adopt_library_loggers`; `RedactingFormatter.format` scrubs; `configure_logging` adds the filter and adopts. Module docstring and the comment above `QUIET_LOGGERS` updated. |
| `src/earmark/pipeline/runner.py` | `describe_error` scrubs the text of non-earmark exceptions. |
| `src/earmark/transcribe/faster_whisper.py` | Call `adopt_library_loggers()` in `load_runtime` (after the imports succeed) and in `model_cache_status` (after importing `faster_whisper.utils`). |
| `src/earmark/index/embed.py` | Call `adopt_library_loggers()` in `load_fastembed_runtime` (after `loguru.logger.disable`) and in `model_cached` (after importing `huggingface_hub`). |
| `src/earmark/cli.py` | Digest tail only for weekly windows (one line). |
| `tests/conftest.py` | New fixture `restore_library_loggers`. |
| `tests/uds_http.py` | New: `UdsReply`, `UdsHttpServer`, `asgi_on_uds`, `uds_connection_class`. |
| `tests/test_log.py` | New unit and default-suite library tests (6). |
| `tests/test_log_libraries.py` | New, `pytestmark = pytest.mark.index`: huggingface_hub, httpx/httpcore, urllib3, fastembed, LanceDB, handler audit. |
| `tests/test_llm_anthropic.py` | `test_key_not_logged[True]` uses its own handler; new `test_sdk_log_env_cannot_unquiet`. |
| `tests/test_cli.py` | Digest `--since` assertions; new `test_serve_access_log_drops_search_query`. |
| `tests/test_runner.py` | New `describe_error` tests. |
| `tests/test_faster_whisper.py`, `tests/test_index_embed.py` | Call-site adoption tests with the existing stubs. |
| `tests/test_faster_whisper_slow.py` | New slow test: no library handlers after the real `load_runtime`. |
| `pyproject.toml` | Only the `index` marker's description text: `"index: needs the index extra; real LanceDB, fastembed, or huggingface_hub offline in a temp directory, no network"`. Nothing else. `tests/test_tooling.py` only checks the `index:` prefix. |
| `docs/deploy.md` | Section 16 table: one new row (5.6). Section 7a: one sentence on HF tokens and xet logs (5.6). |
| `docs/ARCHITECTURE.md` | Already edited by this plan (section 8). |

## 4. Interfaces

### 4.1 `earmark.log`

```python
QUIET_LOGGERS: Final = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "urllib3",
    "anthropic",
    "huggingface_hub",
)
LIBRARY_HANDLER_LOGGERS: Final = ("huggingface_hub",)
OWN_LOGGER: Final = "earmark"
URL_ELISION: Final = "/…"


def scrub_urls(text: str) -> str: ...


def is_own_logger(name: str) -> bool: ...


def is_quiet_logger(name: str) -> bool: ...


class QuietLoggerFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool: ...


def adopt_library_loggers() -> None: ...


def configure_logging(
    level: str = "INFO", secrets: Iterable[str] = (), stream: TextIO | None = None
) -> logging.Handler: ...
```

- `is_own_logger(name)`: `name == OWN_LOGGER or name.startswith(OWN_LOGGER + ".")`.
  `"earmarker"` and `"root"` are not own.
- `is_quiet_logger(name)`: `name` equals an entry of `QUIET_LOGGERS` or starts with
  `entry + "."`. `"httpx2"` is not a child of `"httpx"`, so the prefix check needs the dot.
- `QuietLoggerFilter.filter`: `record.levelno >= logging.WARNING or not
  is_quiet_logger(record.name)`.
- `scrub_urls`, exactly these two patterns, applied in this order:

```python
_URL: Final = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?:[^\s/?#@'\"<>`]*@)?"
    r"(?P<host>[^\s/?#'\"<>`]*)"
    r"(?P<rest>[/?#][^\s'\"<>`]*)?"
)
_REQUEST_QUERY: Final = re.compile(r'("[A-Z]+ [^\s?"]*)\?\S*?( HTTP/\d(?:\.\d)?")')
```

  First, each `_URL` match becomes `scheme + host + (URL_ELISION if rest else "")`:
  userinfo is dropped, and port and IPv6 brackets stay in `host`. Then each
  `_REQUEST_QUERY` match becomes `\1\2`. Expected results (from the probe):

  | Input | Output |
  |---|---|
  | `GET https://huggingface.co/api/resolve-cache/m/x?%2Fa=&etag=%22c "HTTP/1.1 200 OK"` | `GET https://huggingface.co/… "HTTP/1.1 200 OK"` |
  | `Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)` | unchanged |
  | `127.0.0.1:5 - "GET /search?q=private+thoughts HTTP/1.1" 200` | `127.0.0.1:5 - "GET /search HTTP/1.1" 200` |
  | `"GET /search?q=a"b&kind=x HTTP/1.1" 200` | `"GET /search HTTP/1.1" 200` |
  | `"GET /healthz HTTP/1.1" 200` | unchanged |
  | `for url 'https://user:pw@h.example:8443/p?q=1#f'` | `for url 'https://h.example:8443/…'` |
  | `https://[::1]:8000/x`, `file:///tmp/a`, `https://h.example` | `https://[::1]:8000/…`, `file:///…`, unchanged |
  | `URL('https://h.example/a/b')` | `URL('https://h.example/…')` |

- `RedactingFormatter.format` (constructor unchanged):

```python
def format(self, record: logging.LogRecord) -> str:
    text = super().format(record)
    if is_own_logger(record.name):
        head = self.formatMessage(record)  # record.message and asctime set by super()
        if text.startswith(head):
            text = head + scrub_urls(text[len(head) :])
        else:
            text = scrub_urls(text)
    else:
        text = scrub_urls(text)
    return redact(text, self._secrets)
```

  For own records, the message is kept and the traceback and stack after it are
  scrubbed. Scrub first, then redact.
- `adopt_library_loggers()`:

```python
def adopt_library_loggers() -> None:
    if sys.modules.get("huggingface_hub") is not None:
        try:
            importlib.import_module("huggingface_hub.utils.logging")
        except ImportError:
            pass
    for name in LIBRARY_HANDLER_LOGGERS:
        library = logging.getLogger(name)
        for handler in list(library.handlers):
            if not isinstance(handler, logging.NullHandler):
                library.removeHandler(handler)
```

  It never imports `huggingface_hub` itself, never changes `propagate`, and is idempotent.
  `try/except ImportError: pass` may need `contextlib.suppress(ImportError)` to satisfy
  ruff `SIM105`; either form is fine.
- `configure_logging` is unchanged except: `handler.addFilter(QuietLoggerFilter())`; the
  existing loop `for name in QUIET_LOGGERS: setLevel(max(WARNING, root.level))` now
  covers the longer tuple; and `adopt_library_loggers()` is the last call before
  `return handler`.

### 4.2 `earmark.pipeline.runner.describe_error`

Only the non-`EarmarkError` branch changes: `message = scrub_urls(str(exc))`, applied
before it is appended. The `(at file:line in func)` suffix and the `EarmarkError` branch
(earmark's own text) are untouched. Collapse, redact, and cap follow as today.

### 4.3 Call sites of `adopt_library_loggers`

Import it as `from earmark.log import adopt_library_loggers` (earmark.log is light, and
these modules already import earmark.config).
- `transcribe/faster_whisper.py::load_runtime`: after the `try/except` import block,
  before building the closures.
- `transcribe/faster_whisper.py::model_cache_status`: right after `utils =
  importlib.import_module("faster_whisper.utils")`.
- `index/embed.py::load_fastembed_runtime`: right after `loguru.logger.disable("fastembed")`.
- `index/embed.py::model_cached`: right after `hub = importlib.import_module("huggingface_hub")`,
  inside the success path. The `ImportError → None` path is unchanged.

### 4.4 `cli.py` digest

```python
tail = "; the window is not over yet" if window.weekly and window.end > now else ""
```

### 4.5 Test helpers (`tests/uds_http.py`, new)

```python
@dataclass(frozen=True)
class UdsReply:
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


class UdsHttpServer:
    """Threaded HTTP/1.1 server on a unix socket in a short temp directory."""

    def __init__(self, reply: Callable[[str, str], UdsReply]) -> None: ...

    socket_path: Path
    requests: list[tuple[str, str]]  # (method, request target), in order

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...


@contextmanager
def asgi_on_uds(app: object, **config: Any) -> Iterator[Path]: ...


def uds_connection_class(base: type[Any], socket_path: Path) -> type[Any]: ...
```

- `UdsHttpServer` follows `tests/asr_stub.py`: a `socketserver.ThreadingMixIn` +
  `UnixStreamServer` with `daemon_threads = True`, `get_request` returning a fake client
  address, a `BaseHTTPRequestHandler` with `protocol_version = "HTTP/1.1"`,
  `log_message` silenced, and `do_GET`/`do_HEAD`/`do_POST` that read any body, call
  `reply(method, self.path)`, and send status, headers, `Content-Length`, and the body
  (no body for HEAD). The directory comes from `tempfile.mkdtemp(prefix="em", dir="/tmp")`,
  short enough for `AF_UNIX`'s 108-byte limit. It is removed on exit.
- `asgi_on_uds(app, **config)` runs `uvicorn.Server(uvicorn.Config(app, uds=str(path),
  **config))` in a daemon thread. It waits up to 5 s for `server.started` and yields the
  socket path. On exit it sets `server.should_exit = True`, joins (5 s), and removes the
  directory. uvicorn is a core dependency, so this works in the default suite.
- `uds_connection_class(base, path)` returns `type("UdsHTTPConnection", (base,),
  {"connect": connect})`, where:

```python
def connect(self: Any) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    self.sock = sock


conn_cls = type("UdsHTTPConnection", (base,), {"connect": connect})
```

### 4.6 Fixture (`tests/conftest.py`)

`restore_library_loggers`: before the test, record `level` for each name in
`log.QUIET_LOGGERS` and the handler list for each name in `log.LIBRARY_HANDLER_LOGGERS`.
After it, restore both. Every new test that calls `configure_logging`, changes those
levels, or adds handlers uses it together with `restore_root_logging`.

## 5. Behaviour and edge cases

### 5.1 The gate

- It applies only to earmark's handler. pytest's `caplog` and any other handler still see
  what the logger levels let through. Logger levels are still set, so `caplog` tests
  behave as before.
- `LOG_LEVEL=DEBUG` does not show the wire loggers' DEBUG output, and neither do
  `ANTHROPIC_LOG` or `HF_HUB_VERBOSITY`. This was already true for httpx2 and anthropic
  unless the env var was set. It is the price of the guarantee. Say it in the module
  docstring.
- Children are matched by `name.startswith(entry + ".")`. `huggingface_hub.utils._http` is
  gated; `httpx2` is not a child of `httpx`, but is listed itself.
- WARNING and above from wire loggers pass, and are scrubbed (they are third-party).

### 5.2 Scrubbing

- Third-party records are scrubbed in full, including `uvicorn.access`, `uvicorn.error`,
  the root logger (fastembed's `logging.info`), and `py.warnings` if anyone captures
  warnings. Own records keep their message and have their traceback and stack scrubbed.
- Earmark's own messages are not changed. The ingest line keeps its canonical Apple URL.
  `fetch.py` keeps `GET <host> (hop n)`. `api/auth.py` keeps `rejected GET /search from …`,
  which uses `scope["path"]` without the query.
- Cosmetic loss: trailing punctuation after a URL path, such as `)` or `.`, goes into the
  elision. Scheme-less URLs (`huggingface.co/x`) and relative paths are not recognised.
  The gate is what covers those in the wire loggers (2.1).
- The access line keeps the route (`/episodes/<key>`, `/jobs/<id>`). Those are
  earmark's own routes, the same thing `auth.py` logs. Only the query is dropped, so
  `limit`, `offset`, `kind`, and `q` are gone from access lines.
- Redaction still runs last, over the whole line.

### 5.3 What this step does not cover (residuals, stated in the module docstring or the architecture)

- `urllib3`'s WARNING `Retrying … : <relative url>` can carry a path and query the
  scrubber cannot see. earmark's only urllib3 path, fastembed's GCS fallback through
  `requests` with `max_retries=0`, raises before logging it.
- fastembed's GCS-fallback `print(...)` of its static model-archive URL goes to stdout
  and cannot be filtered without patching fastembed.
- hf_xet's own log files and its telemetry (2.7). The architecture documents them, and
  `docs/deploy.md` names the knobs. earmark does not set `HF_XET_*` or
  `HF_HUB_DISABLE_TELEMETRY` (section 9 has the reason).

### 5.4 Handler adoption

- If `configure_logging` was never called (library use), removing HF's handler sends HF
  warnings to `logging.lastResort` (WARNING+ to stderr, unformatted). They are still
  printed once.
- Default-suite tests replace `faster_whisper`, `fastembed`, and `loguru` with stub modules
  and never have `huggingface_hub` in `sys.modules`. Adoption then imports nothing and
  only removes handlers. The existing "heavy modules not imported" subprocess tests
  (`test_faster_whisper.py:116`, `test_cli.py:132`, `test_factory.py:268`,
  `test_index_search.py:189`) must keep passing unchanged. They do if adoption never
  imports `huggingface_hub` itself.
- A test that sets `sys.modules["huggingface_hub"] = None` (as in `test_cli.py:497` for
  faster_whisper) is handled by `sys.modules.get(...) is not None`.

### 5.5 HF token

No code change: earmark already never reads or passes a token. `download_model(...)` gets
no `use_auth_token`, and fastembed gets no token. A static test (A2b) keeps it that way.
The hint text itself tells the user to set `HF_TOKEN`. That is the server's text, shown
once, and it is informational.

### 5.6 Docs (`docs/deploy.md`)

- Section 16 table, new row: symptom `"You are sending unauthenticated requests to the
  HF Hub"` in the worker log. Cause: informational, shown once per process on a model
  download. Public models need no token, and earmark never reads one; `huggingface_hub`
  uses `HF_TOKEN` if you set it in the service environment.
- Section 7a, one sentence: Hugging Face's `hf_xet` writes its own JSON logs under
  `$HF_HOME/xet/logs/` (`/data/huggingface/xet/logs` in the container) and reports a
  download summary to the Hub. `HF_HUB_DISABLE_TELEMETRY=1` (or `DO_NOT_TRACK=1`) and
  `HF_XET_LOG_DEST` (a directory ending in `/`, or a file) are Hugging Face's settings
  for that; earmark has not tested them.
- Keep `tests/test_docs.py` passing. It has no HF-specific checks today.

### 5.7 Digest

- `--since Nd`: no tail, in the `wrote`, `is up to date`, and empty (`nothing written`) lines.
- `--week` current or future: tail kept. Past week and the default (last complete week):
  no tail, as today.

## 6. Tests

All new tests that touch logging use `restore_root_logging` and `restore_library_loggers`,
and pass a `StringIO` to `configure_logging`. "URL path absent" means the test's sentinel
path segments and query values (such as `secret/path`, `sid=`, `etag=`, `resolve-cache`,
`private`) do not occur in the captured output.

### A1: no third-party logger records a URL path or query at INFO or below; earmark's own host-only messages remain

Default suite (`tests/test_log.py` unless noted):
- `test_scrub_urls_examples`: parametrized over the table in 4.1.
- `test_quiet_loggers_gated_even_when_unquieted`: parametrized over every
  `QUIET_LOGGERS` name and `name + ".child"`. Call `configure_logging("DEBUG", stream=s)`,
  then `logging.getLogger(name).setLevel(logging.DEBUG)` (what `ANTHROPIC_LOG` does).
  Emit DEBUG and INFO records containing `https://x.example/secret/path?sid=1` and a
  relative `b'/secret/path?sid=1'`: neither appears. A WARNING with the same URL appears
  as `https://x.example/…` with its other text intact.
- `test_third_party_info_is_scrubbed`: on loggers `uvicorn.error`, `root`
  (`logging.getLogger().info`), and `somelib.sub`, an INFO with a URL is printed with
  `scheme://host/…`.
- `test_own_messages_unchanged`: `earmark.ingest` INFO with
  `https://podcasts.apple.com/us/podcast/id1?i=2` and `earmark.fetch` DEBUG `GET
  feeds.example (hop 0)` print verbatim. `test_own_traceback_scrubbed`: an `earmark.x`
  `logger.exception` whose exception text is `for url 'https://h.example/p?sid=1'` keeps
  the message line and shows `https://h.example/…` in the traceback.
- `test_cached_exc_text_scrubbed`: format the same record first with a plain
  `logging.Formatter` (which caches `exc_text`), then through earmark's handler. The
  output is still scrubbed.
- `test_httpx2_httpcore2_over_uds_log_no_paths`: `UdsHttpServer` answers `GET
  /start?sid=s1` with 307 `Location: /next?sid=s2` and `/next?sid=s2` with 200.
  `configure_logging("DEBUG")`, then set `httpx2` and `httpcore2` to DEBUG. Use
  `httpx2.Client(transport=httpx2.HTTPTransport(uds=...), base_url="http://models.example",
  follow_redirects=True)`. `server.requests` shows both targets (the request happened).
  Output contains no `sid=`, `/start`, or `/next`.
- `tests/test_llm_anthropic.py::test_sdk_log_env_cannot_unquiet`: monkeypatch env
  `ANTHROPIC_LOG=debug`, `configure_logging("DEBUG", stream=s)`, then call
  `anthropic._utils._logs.setup_logging()`. This is what SDK import does, and the SDK is
  already imported in the test process. Assert the levels are now DEBUG. Run one analysis
  through the existing `stub`/`make_client` (MockTransport) with a transcript holding
  `TRANSCRIPT_SENTINEL`. The output contains neither the sentinel nor `/v1/messages`.
  Reset levels in `finally` (the fixture does it).
- `tests/test_cli.py::test_serve_access_log_drops_search_query`: with the existing
  `fake_uvicorn` fixture, invoke `serve` (env token, `EARMARK_INDEX_BACKEND=none` so the
  answer is a deterministic 503 `search is turned off`) and take the captured app and
  kwargs. Then `configure_logging("INFO", stream=s)` and run the app with
  `asgi_on_uds(app, **{k: v for k, v in kwargs.items() if k not in ("host", "port")})`.
  Send `GET /search?q=private-sentinel&limit=3` with the bearer token, and one
  without it, through `httpx2` over the socket. `s` contains `"GET /search HTTP/1.1" 503`
  and `"GET /search HTTP/1.1" 401`, and contains neither `private-sentinel` nor `limit=`.

Index (`tests/test_log_libraries.py`, `pytestmark = pytest.mark.index`; libraries via
`importlib.import_module`):
- `test_hf_download_logs_no_urls`: the 2.1 mock flow, with `X-HF-Warning: unauth; Warning:
  You are sending unauthenticated requests to the HF Hub.` on every response. Call
  `load_fastembed_runtime()` first (it imports `huggingface_hub.utils.logging` and
  adopts). Patch `huggingface_hub.constants.HF_HUB_OFFLINE` to False and
  `huggingface_hub.utils._http._WARNED_TOPICS` to a fresh `set()` with
  `monkeypatch.setattr`. Call `set_client_factory`. In `finally`,
  `set_client_factory(default_client_factory)` and `close_session()`. Unexpected paths get
  404. Assert: the download returns a file containing `{}`; the mock saw the
  `resolve`, `resolve-cache` HEAD, and GET requests; the output has no `resolve`,
  `etag`, `%2F`, or `HTTP Request:` text.
- `test_httpx_httpcore_over_uds_log_no_paths`: same as the httpx2 test with
  `httpx.HTTPTransport(uds=...)` and `httpx`/`httpcore` set to DEBUG first.
- `test_urllib3_over_uds_logs_no_path`: `urllib3.HTTPConnectionPool("models.example",
  80)` with `ConnectionCls = uds_connection_class(urllib3.connection.HTTPConnection,
  path)`. Set `urllib3` to DEBUG, then `GET /secret/path?sid=1`. Status 200; no
  `secret/path`, `sid=`.
- `test_fastembed_loguru_stays_disabled`: `rt = load_fastembed_runtime()`. Add a loguru
  sink (list append, `level="TRACE"`) and remove it in `finally`. `rt.load(<model from
  rt.supported()[0]>, empty tmp dir, False)` raises (`ValueError` or `OSError`). The sink
  got no record whose `record["name"]` starts with `fastembed`, and the earmark stream
  has no `fastembed` text.
- `test_lancedb_local_ops_log_no_urls`: under `configure_logging("DEBUG")`, run one
  index write and one search with the existing `tests/index_helpers` and
  `FakeEmbedder`, the same way `test_lancedb_index.py` does. The output matches no
  `://\S*/` (no URL with a path).

### A2: the Hugging Face hint at most once; earmark never asks for or reads an HF token

- `tests/test_log.py::test_adopt_removes_library_handlers` (default): add a
  `StreamHandler(io_b)` and a `NullHandler` to `logging.getLogger("huggingface_hub")`.
  `configure_logging("INFO", stream=s)` removes the StreamHandler and keeps the
  NullHandler. Then emit a WARNING on `huggingface_hub.utils._http`: `s` has it once,
  `io_b` is empty. A second `adopt_library_loggers()` is a no-op.
- `tests/test_log_libraries.py::test_adopt_does_not_import_huggingface_hub` (index, so
  that `huggingface_hub` is installed and the check means something; a subprocess like the
  existing heavy-import tests): `import earmark.log; earmark.log.configure_logging();
  earmark.log.adopt_library_loggers()`, then print whether `huggingface_hub` is in
  `sys.modules`. Expect `False`.
- `tests/test_log_libraries.py::test_hf_hint_logged_once` (index): extends the download
  test. The earmark stream contains the hint exactly once, across two downloads of two
  different files in the same test (the per-process dedupe).
  `logging.getLogger("huggingface_hub").handlers` has no non-`NullHandler`. The handler
  assertion is the decisive one, because HF's own handler writes to whatever
  `sys.stderr` was when it was created.
- `tests/test_faster_whisper.py::test_load_runtime_adopts_library_loggers` and
  `::test_model_cache_status_adopts_library_loggers` (default, existing stubs): a
  `StreamHandler` placed on the `huggingface_hub` logger before the call is gone after it.
- `tests/test_index_embed.py::test_load_fastembed_runtime_adopts_library_loggers` and
  `::test_model_cached_adopts_library_loggers` (default, existing stubs; for
  `model_cached`, a stub `huggingface_hub` module in `sys.modules` with a
  `try_to_load_from_cache`): same assertion.
- `tests/test_log.py::test_earmark_never_reads_hf_token` (A2b, default, static): no file
  under `src/earmark` contains `HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`, `use_auth_token`,
  `get_token`, or `huggingface_hub.login`, and no `Settings` field name contains `hf`.

### A3: `--since` output no longer says the window is not over; `--week` still does for an in-progress week

In `tests/test_cli.py`:
- `test_digest_since_names_range_note`: add `assert "not over yet" not in result.stdout`.
- New `test_digest_since_rerun_no_tail`: a second `--since 7d` gives `… is up to date: …`
  without the tail.
- New `test_digest_since_empty_no_tail`: with `_digest_now` set to 2027-01-15, `--since
  1d` prints `no episodes were first shared in 2027-01-15 to 2027-01-15 (…); nothing
  written` with no tail.
- `test_digest_week_writes_and_prints`, `test_digest_rerun_up_to_date`, and
  `test_digest_window_in_progress_note` stay unchanged. They prove `--week 2026-W47` still
  has the tail and W46 does not.

### A4: the opt-in `slow` and `index` tests still pass where they can run

- `pytest -m index` with the index extra (section 7): all existing index tests plus
  `tests/test_log_libraries.py`.
- `pytest -m slow tests/test_faster_whisper_slow.py` with the transcribe extra, and
  `pytest -m slow tests/test_index_slow.py` with the index extra (section 7).

### Criteria added by the planner

- **P1. The gate survives library level resets.** Covered by
  `test_quiet_loggers_gated_even_when_unquieted` and `test_sdk_log_env_cannot_unquiet`.
- **P2. Useful third-party warnings are kept, with hosts only.** The WARNING half of
  `test_quiet_loggers_gated_even_when_unquieted`. Also
  `test_log.py::test_hf_retry_warning_kept_scrubbed`: on `huggingface_hub.utils._http`,
  `"'ConnectError' thrown while requesting GET
  https://cas-bridge.xethub.hf.co/xet-bridge-us/abc?X-Amz-Signature=dead"` prints as
  `… requesting GET https://cas-bridge.xethub.hf.co/…`.
- **P3. Tracebacks are scrubbed; earmark messages are not.** `test_own_traceback_scrubbed`,
  `test_cached_exc_text_scrubbed`, `test_own_messages_unchanged`.
- **P4. The search query is never logged.** `test_serve_access_log_drops_search_query`.
- **P5. Job errors from library exceptions carry hosts only.** `tests/test_runner.py::test_describe_error_scrubs_library_urls`:
  `ValueError("Client error '404 Not Found' for url 'https://huggingface.co/api/models/x?sid=1'")`
  gives text with `https://huggingface.co/…` and without `sid=1` or `api/models`.
  `::test_describe_error_keeps_earmark_text`: `PermanentError("see https://example.org/docs/x")`
  is unchanged.
- **P6. No library installs its own handler.** `tests/test_log_libraries.py::test_index_libraries_install_no_handlers`
  (index): import `fastembed`, `lancedb`, `pyarrow`, `huggingface_hub.utils.logging`,
  `requests`, `urllib3`, `onnxruntime`, `tokenizers`, `loguru`; call
  `configure_logging("INFO", stream=s)`. Then every `logging.Logger` instance in
  `logging.Logger.manager.loggerDict` (`PlaceHolder` entries skipped; the root logger is
  not in that dict) has only `NullHandler`s. A failure names the logger
  and handler, so a new library that does this is caught. Also
  `tests/test_faster_whisper_slow.py::test_load_runtime_leaves_no_library_handlers` (slow,
  transcribe extra, no download): `configure_logging`, then the real `load_runtime()` and
  `importlib.import_module("faster_whisper.utils")`; same assertion.
- **P7. The key test stays meaningful.** `test_key_not_logged[True]` adds
  `logging.StreamHandler(buf)` with a plain `Formatter` directly to the `anthropic`
  logger and sets it to DEBUG. Remove the handler in `finally`. Keep the existing
  assertions. It still proves that the key is absent from the SDK's own DEBUG output,
  without earmark's formatter and without the gate. The `[False]` case is unchanged.

Fixtures: none recorded. Every library test builds its responses in code (4.5, 2.1). No
network, model, or API key is needed outside `slow`.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
make check

# Index extra in a scratch venv (about 640 MB under /tmp; /tmp has about 1 GB).
export UV_PROJECT_ENVIRONMENT=/tmp/em20v/venv UV_CACHE_DIR=/tmp/em20v/cache
uv sync --locked --extra index
uv run --locked --extra index pytest -m index -v -rs
# The real embedding model (about 65 MB, network):
SLOW_TEST_CACHE=/tmp/em20v/slow uv run --locked --extra index pytest -m slow tests/test_index_slow.py -v -rs
rm -rf /tmp/em20v

# Transcribe extra (about 470 MB), separately because of disk.
export UV_PROJECT_ENVIRONMENT=/tmp/em20t/venv UV_CACHE_DIR=/tmp/em20t/cache
uv sync --locked --extra transcribe
SLOW_TEST_CACHE=/tmp/em20t/slow uv run --locked --extra transcribe pytest -m slow tests/test_faster_whisper_slow.py -v -rs
```

Live smoke 1, the original dry-run symptom. Run it in the transcribe venv before deleting
it. It downloads `tiny`, about 75 MB:

```bash
HF_HOME=/tmp/em20t/hf uv run --locked --extra transcribe python -c "
from earmark.log import configure_logging
configure_logging('DEBUG')
from earmark.transcribe import faster_whisper as fw
fw.load_runtime()
import faster_whisper.utils as u
u.download_model('tiny', cache_dir='/tmp/em20t/hf/models')
" 2> /tmp/em20t/err.txt
grep -c 'unauthenticated requests' /tmp/em20t/err.txt   # expect 1 (0 if the Hub stops sending it)
grep -E 'resolve-cache|etag=|HTTP Request:|httpcore' /tmp/em20t/err.txt   # expect no output
rm -rf /tmp/em20t
unset UV_PROJECT_ENVIRONMENT UV_CACHE_DIR
```

Live smoke 2, the access log. The repo venv is enough:

```bash
EARMARK_API_TOKEN=smoke-token-0123456789abcdef EARMARK_DATA_DIR=/tmp/em20s \
  uv run earmark serve 2> /tmp/em20s.log &
sleep 2
curl -s -H 'Authorization: Bearer smoke-token-0123456789abcdef' \
  'http://127.0.0.1:8787/search?q=private-sentinel' > /dev/null
kill %1; wait
grep 'uvicorn.access' /tmp/em20s.log      # expect "GET /search HTTP/1.1" with a status
grep -c private-sentinel /tmp/em20s.log   # expect 0
rm -rf /tmp/em20s /tmp/em20s.log
```

8787 is the default `EARMARK_PORT`. Unset any `EARMARK_PORT` in the shell first.

Not verifiable here: the HF hint depends on the Hub still sending `X-HF-Warning`, and the
GPU/container paths are unaffected and untested.

## 8. Deviations

- **ARCHITECTURE.md changed** (contract): a new Security bullet, "Third-party logging"
  (gate, scrub, adoption, `describe_error`). The fetch bullet no longer carries the
  logger rule, which moved into that bullet. The LLM bullet now points at the gate. The
  Search bullet notes the access-line query drop. The Model downloads bullet gains
  *Tokens* and *hf_xet*. The runner Outcomes bullet notes scrubbed library text. The
  digest CLI bullet states the weekly-only tail. Testing (`index`) names the new tests.
  The module layout line for `log.py` is updated.
- **Beyond the brief, same rule:** uvicorn's access log query (2.5) and `describe_error`
  (P5). Both break the architecture's existing "never logged / not put in job errors"
  rules in the way finding 1 does, and both fixes reuse `scrub_urls`.
- **Mechanism:** the brief offers level quieting or content filtering. This plan uses
  both, each limited to where it works (2.4). Level quieting alone fails under
  `ANTHROPIC_LOG` (2.3), and content filtering alone misses relative paths (2.1).
- **WARNING records from wire loggers still pass** (scrubbed). The brief's criterion is
  about INFO and below. The urllib3 residual (5.3) is a WARNING that cannot happen in
  earmark's use.
- **`index` marker** gets a broader description and hosts the library tests that need
  the extra but not LanceDB. `test_faster_whisper_slow.py` gains a test that needs the
  transcribe extra but no download, because the `slow` job is the only one with that
  extra.

## 9. Out of scope

- hf_xet's file logs and Hub telemetry (and `huggingface_hub`'s own telemetry). Setting
  `HF_HUB_DISABLE_TELEMETRY=1` or `HF_XET_LOG_DEST` by default would be a product
  decision about the operator's environment, not a logging fix. This step documents
  them only.
- tqdm progress bars (faster-whisper disables them; fastembed manages its own).
- Routing `warnings.warn` into logging (`logging.captureWarnings`).
- Redacting an operator-set `HF_TOKEN` (earmark never sees it; huggingface_hub does not
  log it).
- `typer.echo` error paths in the CLI that print library exception text.
- Native stderr output of ctranslate2, onnxruntime, and LanceDB's Rust core.
