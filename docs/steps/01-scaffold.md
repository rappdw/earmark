# Step 01: Scaffold, tooling, config

## 1. Outcome

A clean clone runs `uv sync && make check` and gets green on Python 3.13. The repo has a
`src/earmark` package built with `uv_build`, an `earmark` console script with
`--version` and `config`, a frozen `Settings` covering every variable in the
architecture's configuration table (secrets as `SecretStr`), a logging setup whose
formatter redacts secret values, `.env.example`, an extended `.gitignore`, a `Makefile`,
and a committed `uv.lock`. The default test suite blocks sockets, deselects the
`network`, `slow`, `llm`, and `gpu` markers, and runs mypy in strict mode over `src` and
`tests`. The `transcribe` and `index` extras exist and are empty.

## 2. Findings

All checks were run on 2026-11-19 in this sandbox (aarch64, Debian glibc 2.41, system
CPython 3.13.5, uv 0.13.9), against PyPI's JSON API and with throwaway uv projects in the
scratchpad. Nothing was installed into the repo.

### 2.1 Python version: pin 3.13

PyPI JSON API (`https://pypi.org/pypi/<pkg>/json`), latest release files:

| Package | Latest | linux/aarch64 wheels |
|---|---|---|
| ctranslate2 | 4.9.0 (2026-11-10) | cp39 to cp315, manylinux_2_27/2_28 aarch64 |
| faster-whisper | 1.2.1 | pure `py3-none-any` |
| lancedb | 0.41.0 (2026-10-29) | `cp310-abi3` manylinux_2_28 aarch64 |
| onnxruntime | 1.32.0 (2026-11-13) | cp311 to cp314 (+ cp313t, cp314t), manylinux_2_28 aarch64. **No cp315** |
| fastembed | 0.9.1 | pure `py3-none-any` (needs onnxruntime, tokenizers, py-rust-stemmers, mmh3, pillow: all have aarch64 wheels) |
| pylance, pyarrow, tokenizers, av, numpy | 14.0.3, 26.0.1, 0.23.3, 19.0.2, 2.5.5 | all have aarch64 wheels for cp313 (abi3 or per-version) |

Then:

- `uv pip compile` of the runtime, dev, and heavy packages together with
  `--python-version 3.13 --python-platform aarch64-unknown-linux-gnu --only-binary :all:`
  resolved cleanly (71 packages).
- Installing `faster-whisper lancedb fastembed onnxruntime` with `--only-binary :all:`
  into a 3.13 venv and importing them worked: ctranslate2 4.9.0, faster_whisper 1.2.1,
  lancedb 0.41.0 (created a table and ran a vector search), fastembed 0.9.1, onnxruntime
  1.32.0 (providers `AzureExecutionProvider`, `CPUExecutionProvider`), pyarrow 26.0.1,
  av 19.0.2.
- With `requires-python = ">=3.13"` (open-ended), a universal `uv lock` that includes
  fastembed **fails**: uv must also solve for Python 3.15, where onnxruntime has no
  aarch64 wheels. `">=3.13,<3.15"` and `">=3.13,<3.14"` both lock.
- Without `tool.uv.required-environments`, the open-ended lock *succeeds silently* and
  would break later at `uv sync` on 3.15. With
  `required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]`
  the same problem is a lock-time error. An sdist-only package (tested with `docopt`)
  still locks under this setting, so it only bites when wheels exist but none fit
  linux/aarch64.

**Decision:** `requires-python = ">=3.13,<3.14"`, `.python-version` = `3.13`. 3.13 is
the brief's preference and the sandbox's system interpreter, so `uv sync` needs no
interpreter download here. 3.14 would also work for the four heavy packages today, but
nothing needs it. The DGX Spark gets 3.13 from uv-managed Python or a `python:3.13`
image (step 16). The DGX Spark is also aarch64, so the same wheels apply there.

**Side finding for step 08 (architecture risk 1):** the ctranslate2 4.9.0 cp313 aarch64
wheel is CPU-only. `readelf -d` on the bundled `libctranslate2-*.so.4.9.0` lists only
libpthread, libgomp, libdl, libstdc++, libm, libgcc_s, and libc; no strings mention
cudart, cublas, or cudnn. `get_supported_compute_types('cpu')` returns
`{float32, int8, int8_float32}`. Recorded in `docs/ARCHITECTURE.md` risk 1.

### 2.2 httpx is superseded by httpx2: use httpx2

- `httpx` 0.28.1 is the latest release and dates from 2024-12-06.
- `httpx2` 2.16.2 (2026-11-16) is published from `github.com/pydantic/httpx2`. Its README
  says Pydantic took over stewardship of HTTPX under the new name for maintenance and
  security updates. The API matches httpx under the import name `httpx2`: `Client`,
  `AsyncClient`, `MockTransport`, `HTTPTransport`, `Timeout`, `Limits`, `stream`,
  `HTTPStatusError`, `TimeoutException`, `ConnectError`, and `TransportError` all exist,
  and `Client.__init__` has the familiar keyword arguments (`follow_redirects`,
  `max_redirects`, `timeout`, `transport`, `trust_env`, ...). It uses `truststore`
  (system CA store) instead of `certifi`.
- `anthropic` 2.11.0 (needed in step 10) **requires `httpx2>=2,<3`**, so httpx2 is in
  the tree regardless.
- Starlette 1.10.0 (pulled in by FastAPI 0.165.0): `starlette.testclient` imports
  `httpx2`. If only `httpx` is installed it emits `StarletteDeprecationWarning: Using
  httpx with starlette.testclient is deprecated; install httpx2 instead`. With
  `filterwarnings = ["error"]` that warning fails every TestClient test. Observed
  directly.

**Decision:** the runtime dependency is `httpx2`, not `httpx`. Do not add `httpx` at
all. Later steps import `httpx2`. This is a deviation from the brief (see section 8).

### 2.3 Build backend

`uv init --package` under uv 0.13.9 generates
`requires = ["uv_build>=0.13.9,<0.14.0"]`, `build-backend = "uv_build"`, and a src layout.
`uv_build` needs no configuration when the module is `src/earmark/`. Hatchling would
also work but adds config for nothing. Do not copy the `authors` table that `uv init`
fills from git config; omit `authors`.

### 2.4 pytest 9.2.0 and pytest-socket 0.8.1 (both latest)

Verified with a toy project:

- pytest 9 reads native `[tool.pytest]` (not only `[tool.pytest.ini_options]`) with real
  TOML lists. `strict = true` turns on `strict_config`, `strict_markers`, `strict_xfail`,
  and `strict_parametrization_ids` (source: `_pytest/main.py`, `_pytest/python.py`). An
  unregistered marker such as `@pytest.mark.netwrok` then fails the run.
- `addopts` containing `-m "not network and not slow and not llm and not gpu"`
  deselects marked tests. A command-line `-m network` **replaces** it, because the last
  `-m` wins and addopts come first. Verified: `pytest -m network` selected only the
  network test.
- pytest-socket 0.8.1 (`pytest_socket/__init__.py`): `--disable-socket` replaces
  `socket.socket` with a guard that raises `SocketBlockedError`, and **also** replaces
  `socket.getaddrinfo` and `socket.gethostbyname`. `--allow-unix-socket` lets AF_UNIX
  through. Markers `enable_socket`, `disable_socket`, and `allow_hosts` and fixtures
  `socket_enabled` and `socket_disabled` exist. `pytest_runtest_setup` checks
  `item.get_closest_marker("enable_socket")`, so markers added in
  `pytest_collection_modifyitems` take effect.
- **Gotcha 1:** `SocketBlockedError.__init__` calls `warnings.warn(msg)`. Under
  `filterwarnings = ["error"]` that warning is raised *instead of* the
  `SocketBlockedError`, so `pytest.raises(SocketBlockedError)` fails with `UserWarning`.
  Observed. Fix: add the narrow filter
  `"ignore:A test tried to use socket:UserWarning"`. This prefix covers both the
  `socket.socket` and the `getaddrinfo`/`gethostbyname` messages, and the error is still
  raised.
- **Gotcha 2:** without `--allow-unix-socket`, `asyncio.run(...)` and FastAPI's
  `TestClient` fail with `SocketBlockedError`, because the event loop's self-pipe is a
  `socket.socketpair()` (AF_UNIX) and `socket.socketpair` wraps the fds with the patched
  `socket.socket`. Observed. With `--allow-unix-socket` both work, while AF_INET
  sockets, `create_connection`, `getaddrinfo`, and `httpx2.get("http://127.0.0.1:9/")`
  all still raise `SocketBlockedError`. httpx2 does not wrap it, since it is a
  `RuntimeError` and not an `OSError`.
- `pytest_plugins = ["pytester"]` in `tests/conftest.py` works with
  `testpaths = ["tests"]`. `pytester.runpytest_subprocess()` with the repo's pyproject
  and conftest copied in gave `passed=1, deselected=4` by default, and with
  `-m network` the network test could open an AF_INET socket, because the conftest hook
  re-enabled sockets.

### 2.5 ruff 0.17.6 (latest)

- The rule selection in section 4.1 runs on a toy project with no formatter-conflict
  warnings. All listed prefixes exist in `ruff linter`.
- **Do not select `PL`** wholesale. `PLC0415` (import-outside-top-level) contradicts the
  project rule that heavy dependencies are imported lazily inside functions, and
  `PLR2004` (magic values) is noise in tests.
- **Do not select `TC`** (flake8-type-checking). It moves imports under
  `if TYPE_CHECKING:`, which breaks pydantic models, FastAPI signatures, and typer
  `Annotated` parameters, because all three resolve annotations at runtime.
- Tests need `S101` (assert), `S105` and `S106` (hard-coded "passwords" used as
  sentinels), `S108` (tmp paths), and `S603` (subprocess without shell) ignored.

### 2.6 mypy 2.4.1 (latest) and the pydantic plugin

- `[tool.mypy] strict = true` with `files = ["src", "tests"]` checks both trees, and
  `uv run mypy` takes no arguments. The editable install is found through its `.pth`
  file.
- `plugins = ["pydantic.mypy"]` (pydantic 2.14.4) with
  `[tool.pydantic-mypy] init_forbid_extra = true, init_typed = true,
  warn_required_dynamic_aliases = true` flags `Settings(port="x")` (`arg-type`) and
  `Settings(nope=1)` (`call-arg`), but **accepts** `Settings(_env_file=None)`. Tests rely
  on that.
- `warn_unreachable = true` and
  `enable_error_code = ["ignore-without-code", "redundant-expr", "truthy-bool",
  "possibly-undefined", "deprecated"]` are accepted by mypy 2.4.1. Strict mode also
  reports unused `# type: ignore`.
- pytest-socket and pytest's `Pytester` are typed, so no ignores are needed.

### 2.7 pydantic-settings 2.16.3 (latest) and SecretStr

- `SettingsConfigDict(env_prefix="EARMARK_", env_file=".env", env_file_encoding="utf-8",
  extra="ignore", env_ignore_empty=True, frozen=True)` works. Field `tz` reads
  `EARMARK_TZ` and never the system `TZ`.
- `SecretStr` repr is `SecretStr('**********')`, and `model_dump(mode="json")` renders a
  set secret as `"**********"`.
- Without `env_ignore_empty=True`, `EARMARK_API_TOKEN=` yields `SecretStr('')` and
  `EARMARK_ASR_LANGUAGE=` yields `''`, not `None`. With it, both yield `None`. Verified.
- **Dead end:** `extra="forbid"` rejects *any* unknown key in `.env`, including
  `ANTHROPIC_API_KEY=...`, and its `ValidationError` text echoes `input_value='sk-...'`.
  Use `extra="ignore"`.
- **Gotcha:** `str(ValidationError)` includes `input_value=...` for every failing field.
  Any user-facing config error must be built from
  `exc.errors(include_input=False, include_url=False)`, never from `str(exc)`.
- pydantic-settings does not export `.env` entries to `os.environ`, so the `anthropic`
  SDK will not see an `ANTHROPIC_API_KEY` placed in `.env`. Noted in the architecture for
  step 10. Nothing to do here.
- `fastembed` 0.9.1's `TextEmbedding.__init__` defaults to
  `model_name="BAAI/bge-small-en-v1.5"` (read from the installed wheel), which becomes
  the `EMBED_MODEL` default.
- `zoneinfo.ZoneInfo("Europe/Oslo")` works here from `/usr/share/zoneinfo`, but the
  `tzdata` PyPI package is not installed, and slim container images may lack system zone
  data. Adding `tzdata` makes `TZ` validation deterministic everywhere.

### 2.8 typer 0.29.2 and click 8.5.1 (latest)

- An eager `--version` option on `@app.callback()` prints and raises `typer.Exit`, and
  works before any subcommand.
- `typer.Typer(no_args_is_help=True, add_completion=False)` with no arguments prints
  help and **exits 2** under click 8.5. Tests should expect 2.
- `typer.testing.CliRunner().invoke(...)`: `result.output` holds stdout and stderr
  interleaved (click ≥ 8.2), and `result.stdout` and `result.stderr` are separate.

### 2.9 Sandbox constraints the implementer should know

- `/tmp` is a 1 GB tmpfs and `/home/sandy` is a 2 GB tmpfs, with about 780 MB already
  used by the uv cache. Base and dev dependencies fit easily. Do not install the heavy
  extras in this step. If uv warns "Failed to hardlink files", that is harmless; it
  happens only when the venv and the cache are on different filesystems.
- `uv run --locked ...` fails with a hint when `uv.lock` is stale relative to
  `pyproject.toml`. Verified. The Makefile uses it so the gate catches a forgotten
  `uv lock`.

## 3. Files

Create (all paths relative to the repo root):

| File | Purpose |
|---|---|
| `pyproject.toml` | Project metadata, dependencies, extras, dev group, console script, and tool config (section 4.1). |
| `uv.lock` | Generated by `uv lock`; committed. |
| `.python-version` | `3.13` |
| `Makefile` | `check`, `lint`, `fmt`, `typecheck`, `test` (section 4.2). Recipe lines use tabs. |
| `.env.example` | Every `Settings` field, once each, with its default (section 4.5). |
| `src/earmark/__init__.py` | `__version__` from `importlib.metadata`. Nothing else is imported. |
| `src/earmark/__main__.py` | `from earmark.cli import app; app(prog_name="earmark")` for `python -m earmark`. |
| `src/earmark/py.typed` | Empty PEP 561 marker. |
| `src/earmark/config.py` | `Settings`, backend `Literal` aliases, `ConfigError`, `load_settings`, redaction helpers (section 4.3). |
| `src/earmark/log.py` | `configure_logging` and `RedactingFormatter` (section 4.4). Not named `logging.py`, to avoid confusion with the stdlib module. |
| `src/earmark/cli.py` | Typer app: root callback with `--version`, and the `config` command (section 4.6). |
| `tests/__init__.py` | Empty. Makes `tests` a package so mypy module names cannot collide as the suite grows; later test subdirectories also get `__init__.py`. |
| `tests/conftest.py` | `pytest_plugins = ["pytester"]`, the socket re-enabling hook for live markers, and an autouse environment-isolation fixture (section 5.5). |
| `tests/test_tooling.py` | Socket blocking, marker registration and deselection, strictness guards. |
| `tests/test_config.py` | `Settings` defaults, sources, validation, secrets, `.env.example` parity. |
| `tests/test_cli.py` | `--version`, console script, `config` output and redaction, config errors, import lightness. |
| `tests/test_log.py` | Logging setup and redaction. |

Change:

| File | Change |
|---|---|
| `.gitignore` | Append `*.egg-info/`, `build/`, `.coverage`, `htmlcov/`, `.env.*`, and `!.env.example`. Keep the existing entries. |
| `docs/ARCHITECTURE.md` | Already edited by this plan (section 8). The implementer does not touch it. |

Do not create a `README.md` beyond the existing one, `docs/` content, or any module from
later steps (`models.py`, `errors.py`, `api/`, and so on).

## 4. Interfaces

### 4.1 `pyproject.toml`

Use these settings exactly. Versions are the latest on 2026-11-19 and serve as lower
bounds; `uv.lock` pins the rest.

```toml
[project]
name = "earmark"
version = "0.1.0"
description = "Share a podcast episode, get a research note."
readme = "README.md"
requires-python = ">=3.13,<3.14"
dependencies = [
  "fastapi>=0.165.0",
  "uvicorn>=0.54.0",
  "pydantic>=2.14.4",
  "pydantic-settings>=2.16.3",
  "httpx2>=2.16.2",
  "typer>=0.29.2",
  "tzdata>=2026.6",
]

[project.optional-dependencies]
transcribe = []   # step 08: faster-whisper
index = []        # step 14: lancedb, fastembed

[project.scripts]
earmark = "earmark.cli:app"

[dependency-groups]
dev = [
  "pytest>=9.2.0",
  "pytest-socket>=0.8.1",
  "ruff>=0.17.6",
  "mypy>=2.4.1",
]

[build-system]
requires = ["uv_build>=0.13.9,<0.14.0"]
build-backend = "uv_build"

[tool.uv]
# Lock fails if any dependency ships wheels but none for the deployment platform
# (DGX Spark, linux/aarch64). See docs/steps/01-scaffold.md section 2.1.
required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]

[tool.pytest]
minversion = "9.0"
testpaths = ["tests"]
strict = true
addopts = [
  "-ra",
  "--disable-socket",
  "--allow-unix-socket",          # asyncio/TestClient need an AF_UNIX socketpair
  "-m", "not network and not slow and not llm and not gpu",
]
markers = [
  "network: hits real external endpoints (Apple, podcast feeds)",
  "slow: downloads a model or does CPU-heavy work",
  "llm: calls the real analysis model",
  "gpu: needs CUDA",
]
filterwarnings = [
  "error",
  # pytest-socket warns before raising SocketBlockedError; under "error" the warning
  # would replace the exception. The exception itself is still raised.
  "ignore:A test tried to use socket:UserWarning",
]

[tool.mypy]
python_version = "3.13"
strict = true
files = ["src", "tests"]
plugins = ["pydantic.mypy"]
warn_unreachable = true
enable_error_code = [
  "ignore-without-code",
  "redundant-expr",
  "truthy-bool",
  "possibly-undefined",
  "deprecated",
]

[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true

[tool.ruff]
target-version = "py313"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = [
  "E", "W", "F", "I", "N", "UP", "B", "A", "C4", "DTZ", "T20", "PT", "PIE", "RET",
  "SIM", "PTH", "S", "BLE", "LOG", "G", "ASYNC", "FAST", "PGH", "PERF", "RUF",
]
# Not selected on purpose: PL (PLC0415 conflicts with lazy imports of heavy deps),
# TC (moves runtime-needed annotations under TYPE_CHECKING; breaks pydantic/typer/FastAPI).

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "S105", "S106", "S108", "S603"]

[tool.ruff.lint.flake8-pytest-style]
fixture-parentheses = false
mark-parentheses = false
```

Later steps fill the extras and may add *narrowly scoped* `filterwarnings` ignores or
mypy `[[tool.mypy.overrides]]` for specific untyped third-party modules, each with a
comment giving the reason. That is the sanctioned mechanism and does not count as
loosening. Removing `strict`, `error`, `--disable-socket`, or selected rule groups does.

### 4.2 `Makefile`

```make
UV_RUN := uv run --locked

.PHONY: check lint fmt typecheck test
check: lint typecheck test

lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

fmt:
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

typecheck:
	$(UV_RUN) mypy

test:
	$(UV_RUN) pytest
```

`check` is the gate named in `CLAUDE.md`: ruff check, then ruff format check, then mypy,
then pytest. Make stops at the first failure.

### 4.3 `earmark.config`

```python
ResolverBackend = Literal["apple", "fake"]
FetchBackend    = Literal["http", "fake"]
AsrBackend      = Literal["faster_whisper", "remote", "fake"]
LlmProvider     = Literal["anthropic", "openai_compat", "fake"]
StoreBackend    = Literal["vault", "fake"]
IndexBackend    = Literal["lancedb", "fake", "none"]
NotifyBackend   = Literal["ntfy", "fake", "none"]
LogLevel        = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

ENV_PREFIX: Final = "EARMARK_"
REDACTED: Final = "**********"

class ConfigError(Exception):
    """Settings could not be loaded. str(exc) is safe to show: it never contains input values."""
    errors: list[str]          # one "EARMARK_<NAME>: <message>" line per problem

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EARMARK_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", env_ignore_empty=True, frozen=True,
    )
    ...fields below...

    def secret_values(self) -> list[str]: ...
    def redacted_items(self) -> list[tuple[str, str]]: ...

def env_var(field_name: str) -> str: ...        # "api_token" -> "EARMARK_API_TOKEN"
def load_settings(*, env_file: Path | str | None = ".env") -> Settings: ...
def redact_url(url: str) -> str: ...
```

Fields, in this order. The order is the order of `earmark config` output and of
`.env.example`.

| Field | Type | Default | Constraint |
|---|---|---|---|
| `api_token` | `SecretStr \| None` | `None` | |
| `host` | `str` | `"127.0.0.1"` | |
| `port` | `int` | `8787` | `Field(ge=1, le=65535)` |
| `data_dir` | `Path` | `Path("./data")` | |
| `vault_dir` | `Path \| None` | `None` | |
| `vault_subdir` | `str` | `"Earmark"` | validator: non-empty after strip; `PurePosixPath(v)` not absolute; no `..` part; no backslash. Error message: `"must be a relative path inside the vault without '..'"` |
| `obsidian_vault_name` | `str \| None` | `None` | |
| `resolver_backend` | `ResolverBackend` | `"apple"` | |
| `fetch_backend` | `FetchBackend` | `"http"` | |
| `asr_backend` | `AsrBackend` | `"faster_whisper"` | |
| `llm_provider` | `LlmProvider` | `"anthropic"` | |
| `store_backend` | `StoreBackend` | `"vault"` | |
| `index_backend` | `IndexBackend` | `"lancedb"` | |
| `notify_backend` | `NotifyBackend` | `"ntfy"` | |
| `asr_model` | `str` | `"large-v3"` | |
| `asr_device` | `str` | `"auto"` | |
| `asr_compute_type` | `str` | `"auto"` | |
| `asr_language` | `str \| None` | `None` | |
| `asr_base_url` | `str \| None` | `None` | |
| `asr_api_key` | `SecretStr \| None` | `None` | |
| `asr_max_upload_mb` | `int \| None` | `None` | `Field(gt=0)` when set |
| `keep_audio` | `bool` | `False` | |
| `llm_model` | `str` | `"claude-opus-5-5"` | |
| `llm_base_url` | `str \| None` | `None` | |
| `llm_api_key` | `SecretStr \| None` | `None` | |
| `llm_context_tokens` | `int \| None` | `None` | `Field(gt=0)` when set |
| `embed_model` | `str` | `"BAAI/bge-small-en-v1.5"` | |
| `ntfy_url` | `str \| None` | `None` | |
| `ntfy_token` | `SecretStr \| None` | `None` | |
| `max_audio_mb` | `int` | `1024` | `gt=0` |
| `max_feed_mb` | `int` | `20` | `gt=0` |
| `allow_private_fetch` | `bool` | `False` | |
| `job_max_attempts` | `int` | `4` | `ge=1` |
| `job_lease_seconds` | `int` | `900` | `gt=0` |
| `worker_poll_seconds` | `float` | `2.0` | `gt=0` |
| `tz` | `str \| None` | `None` | validator: if set, `zoneinfo.ZoneInfo(v)` must succeed (catch `ZoneInfoNotFoundError` and `ValueError`), else `ValueError("unknown time zone")` |
| `log_level` | `LogLevel` | `"INFO"` | `mode="before"` validator upper-cases strings |

Each field gets a short `description=` (used for nothing at runtime in this step, but it
documents the field next to its definition). URLs stay `str` in this step; the steps that
use them validate them, which avoids pydantic's `HttpUrl` normalisation (an added
trailing slash) surprising later code.

The four secret fields are exactly `api_token`, `asr_api_key`, `llm_api_key`, and
`ntfy_token`. Convention for later steps: any field whose name ends in `_token`, `_key`,
`_secret`, or `_password` must be `SecretStr | None`. A test enforces this.

Methods:

- `secret_values()` returns the non-empty `get_secret_value()` of every `SecretStr`
  field, found by inspecting field values (`isinstance(v, SecretStr)`), not a hard-coded
  list.
- `redacted_items()` returns `[(env_var(name), display), ...]` in field order, where
  `display` is:
  - `None` → `""`;
  - a set `SecretStr` → `REDACTED`;
  - a field whose name ends in `_url` → `redact_url(value)`;
  - `bool` → `"true"` or `"false"`;
  - `Path` → `str(value)`;
  - anything else → `str(value)`.
- `redact_url(url)`: if `urllib.parse.urlsplit(url)` has a password, return the URL with
  the password replaced by `****` (keep scheme, username, host, port, path, query). If it
  has a username but no password, return it unchanged. If it does not parse, return it
  unchanged. Example: `https://bob:hunter2@ntfy.example/topic` →
  `https://bob:****@ntfy.example/topic`.
- `load_settings(env_file=".env")` constructs `Settings(_env_file=env_file)`. It catches
  `pydantic.ValidationError` and raises `ConfigError` from it. The error has one line per
  error, built from `exc.errors(include_input=False, include_url=False)` as
  `f"{env_var(str(err['loc'][0]))}: {err['msg']}"`; for an error with an empty `loc`,
  use `"EARMARK: <msg>"`. `str(ConfigError)` is
  `"invalid configuration:\n  " + "\n  ".join(errors)`. Use `raise ... from None` so the
  original exception, which contains the inputs, is not chained into tracebacks.

`Settings` is not cached globally. Callers call `load_settings()` once per process; tests
construct `Settings(_env_file=None, ...)` directly.

### 4.4 `earmark.log`

```python
class RedactingFormatter(logging.Formatter):
    def __init__(
        self, secrets: Iterable[str], fmt: str | None = None, datefmt: str | None = None
    ) -> None: ...
    def format(self, record: logging.LogRecord) -> str: ...

    # super().format(record), then replace every secret (longest first) with REDACTED.


HANDLER_NAME: Final = "earmark"
LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(
    level: str = "INFO", secrets: Iterable[str] = (), stream: TextIO | None = None
) -> logging.Handler: ...
```

`configure_logging` behaviour:

- It creates a `StreamHandler(stream or sys.stderr)` named `HANDLER_NAME`, with a
  `RedactingFormatter(secrets, LOG_FORMAT)`.
- It removes any existing root handler with that name before adding the new one, so it
  is idempotent: calling it twice leaves exactly one earmark handler.
- It sets the root logger level to `level`. Other root handlers, such as pytest's
  capture handlers, are left alone.
- It returns the handler so tests can remove it.
- Empty strings in `secrets` are ignored, so an empty secret never blanks every message.
- Redaction happens on the fully formatted string, so it covers `msg % args`, exception
  tracebacks (`exc_text`), and `stack_info`.

### 4.5 `.env.example`

One line per `Settings` field, in field order, grouped with `#` comment headers. Each
variable appears exactly once, as either `EARMARK_<NAME>=<value>` (fields with a
non-`None` default, value equal to the default as an env string) or `# EARMARK_<NAME>=`
(fields that default to `None`, including all four secrets, commented out with nothing
after `=`). A comment line may follow the header to explain a group. No real secret,
hostname, or personal value appears. The file must load (`Settings(_env_file=
".env.example")`) to exactly the defaults. Use `true`/`false` for booleans, `./data` for
`DATA_DIR`, and `2` or `2.0` for `WORKER_POLL_SECONDS`.

The parity test finds variables with the regex `^#?\s*(EARMARK_[A-Z0-9_]+)=` per line.
Prose comments must therefore not contain `EARMARK_X=` patterns at the start of a line.

### 4.6 CLI (`earmark.cli`)

```python
app = typer.Typer(
    name="earmark",
    no_args_is_help=True,
    add_completion=False,
    help="Share a podcast episode, get a research note.",
)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            is_eager=True,
            callback=_version_callback,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None: ...


@app.command("config")
def config_command() -> None: ...
```

| Invocation | stdout | stderr | Exit |
|---|---|---|---|
| `earmark --version` | `earmark <version>\n` where version is `importlib.metadata.version("earmark")` (`0.1.0`) | | 0 |
| `earmark` | help | | 2 (click's `no_args_is_help`) |
| `earmark config` | One line per field in field order: `EARMARK_<NAME>=<display>` from `redacted_items()` | | 0 |
| `earmark config` with invalid settings | nothing | `str(ConfigError)`: `invalid configuration:` then one indented line per error, naming the variable, never echoing input | 2 |

`config_command` calls `load_settings()`, then
`configure_logging(settings.log_level, settings.secret_values())`, then prints with
`typer.echo`, never `print` (T20). `--version` does not load settings, so it works even
when the configuration is invalid. The callback does not load settings either; later
commands load them for themselves.

Factor the error path as a private helper (for example `_load_or_exit() -> Settings`,
which echoes `ConfigError` to stderr and raises `typer.Exit(code=2)`) so later commands
reuse it.

## 5. Behaviour and edge cases

1. **`import earmark` is cheap.** `earmark/__init__.py` imports only
   `importlib.metadata`. It must not import `config`, `cli`, pydantic, typer, fastapi, or
   httpx2. `cli.py` may import `config` and `log` at module top; that is fine because
   they are light.
2. **Source precedence.** Init kwargs, then environment variables, then `.env`, then
   defaults (pydantic-settings' default order). Do not customise
   `settings_customise_sources`.
3. **Empty means unset.** `env_ignore_empty=True` applies to environment variables and to
   `.env`, so a copied `.env.example` with `EARMARK_API_TOKEN=` gives `None`, not
   `SecretStr('')`. Step 04's "refuses to start with an empty token" then only has to
   check `None`, but it should treat a whitespace-only token as empty too (its concern).
4. **Unknown keys.** `extra="ignore"`. A typo such as `EARMARK_PROT` is silently ignored
   in this step; step 15's `doctor` can warn about unknown `EARMARK_*` names.
5. **No secret leaks through errors.** `ConfigError` never includes `input` from
   pydantic errors, and is raised `from None`. Test it with a sentinel in an invalid
   field such as `EARMARK_PORT`. A `SecretStr` field can only fail validation if its
   value is not a string, which env sources never produce, but the rule is general.
6. **No secret leaks through output.** `earmark config` uses `redacted_items()` only. It
   never prints `repr(settings)` or `model_dump()` without `mode="json"`. The
   `**********` marker is the same whatever the secret's length.
7. **URL credentials.** `ntfy_url`, `asr_base_url`, and `llm_base_url` may carry
   `user:password@`. `earmark config` masks the password. This is defence in depth, not
   an excuse to put credentials in URLs.
8. **`TZ` and `LOG_LEVEL`.** `EARMARK_TZ=Mars/Base` fails at load and names
   `EARMARK_TZ`. `EARMARK_LOG_LEVEL=debug` is accepted as `DEBUG`;
   `EARMARK_LOG_LEVEL=verbose` fails.
9. **Backend names.** A value outside the `Literal` fails at load naming the variable,
   for example `EARMARK_ASR_BACKEND: Input should be 'faster_whisper', 'remote' or
   'fake'`. `none` is valid only for index and notify.
10. **Frozen settings.** Assigning `settings.port = 1` raises `ValidationError`. Tests
    build new instances instead of mutating.
11. **Live markers and sockets.** The conftest hook adds `pytest.mark.enable_socket` to
    any item carrying `network`, `slow`, or `llm` (`slow` downloads a Whisper model in
    step 08). `gpu` alone does not re-enable sockets.
12. **`-m` semantics.** `uv run pytest -m network` runs only network tests (verified).
    Note that `-m "not slow"` would *include* network, llm, and gpu tests, because it
    replaces the default expression. Put a comment saying so next to the `addopts`
    `-m`.
13. **Formatter redaction ordering.** Replace longer secrets first, so that a secret
    that is a substring of another does not leave part of the longer one visible.

### 5.5 `tests/conftest.py`

- `pytest_plugins = ["pytester"]`.
- `pytest_collection_modifyitems(config, items)`: for each item with any of the markers
  `network`, `slow`, or `llm`, call `item.add_marker(pytest.mark.enable_socket)`.
- An autouse fixture `_isolate_environment(monkeypatch, tmp_path)` deletes every
  environment variable starting with `EARMARK_` and runs `monkeypatch.chdir(tmp_path)`,
  so a developer's shell or a real `.env` in the repo root cannot leak into tests. Tests
  that need repo files locate them via `Path(__file__).resolve().parents[1]` and never
  via the cwd.
- A fixture `restore_root_logging` (not autouse) snapshots `logging.getLogger().handlers`
  and the root level and restores them afterwards, for `test_log.py` and CLI tests that
  call `configure_logging`. The `config` command configures logging, so CLI tests that
  invoke it use this fixture too; otherwise a handler bound to CliRunner's closed stream
  lingers on the root logger.

## 6. Tests

`ROOT = Path(__file__).resolve().parents[1]` wherever repo files are read. No fixtures
need downloading; the only fixture files are generated inside `pytester` temp dirs.

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | From a clean clone, `uv sync && make check` passes | Verified by the verifier (section 7, clean-clone run). No unit test. |
| A2 | `uv run earmark --version` prints the version | `test_cli.py::test_version_flag` (CliRunner: exit 0, output exactly `f"earmark {importlib.metadata.version('earmark')}\n"`); `test_cli.py::test_console_script_prints_version` (runs `Path(sys.executable).parent / "earmark"` with `--version` via `subprocess.run`, asserts stdout `earmark 0.1.0`; proves the entry point wiring); `test_cli.py::test_version_works_with_invalid_config` (`EARMARK_PORT=nope`, `--version` still exits 0) |
| A3 | `earmark config` never prints a secret value | `test_cli.py::test_config_never_prints_secrets`: set all four secrets via env to distinct sentinels (for example `sentinel-api-7f3a9c`), and also `EARMARK_NTFY_TOKEN` via a `.env` in the tmp cwd while it is absent from env. Invoke `config`. Assert exit 0, no sentinel in `result.output` (stdout and stderr), and the lines `EARMARK_API_TOKEN=**********` and `EARMARK_NTFY_TOKEN=**********` present. `test_cli.py::test_config_redacts_url_password` (`EARMARK_NTFY_URL=https://bob:sentinel-pw-1@ntfy.example/t` → output has `https://bob:****@ntfy.example/t` and no sentinel). `test_cli.py::test_config_error_does_not_echo_input` (`EARMARK_PORT=sentinel-port-x` → exit 2, stderr names `EARMARK_PORT`, no sentinel anywhere in output) |
| A4 | Opening a socket inside the default suite raises | `test_tooling.py::test_inet_socket_blocked` (`socket.socket(AF_INET, SOCK_STREAM)` raises `SocketBlockedError`); `test_tooling.py::test_dns_lookup_blocked` (`socket.getaddrinfo("example.com", 443)`); `test_tooling.py::test_http_client_blocked` (`httpx2.get("http://127.0.0.1:9/")` raises `SocketBlockedError`); `test_tooling.py::test_unix_socketpair_allowed` (`asyncio.run` of a trivial coroutine succeeds, documenting `--allow-unix-socket`) |
| A5 | Markers `network`, `slow`, `llm`, `gpu` registered and deselected by default | `test_tooling.py::test_live_markers_registered` (`pytestconfig.getini("markers")` has an entry starting with each name followed by `:`); `test_tooling.py::test_live_markers_deselected_by_default` (pytester: write the repo's `pyproject.toml` via `pytester.makepyprojecttoml(...)`, copy `tests/conftest.py` into `tests/`, add `tests/test_inner.py` with one unmarked test and one test per marker; `runpytest_subprocess()` → `assert_outcomes(passed=1, deselected=4)`); `test_tooling.py::test_network_opt_in_reenables_sockets` (same setup, the inner network test opens and closes an AF_INET socket; `runpytest_subprocess("-m", "network")` → `passed=1, deselected=4`); `test_tooling.py::test_unregistered_marker_fails` (inner `@pytest.mark.netwrok` → non-zero `ret`) |
| A6 | mypy runs in strict mode over `src` and `tests` | `test_tooling.py::test_mypy_strict_over_src_and_tests` (parse `ROOT/pyproject.toml` with `tomllib`: `tool.mypy.strict is True`, `files == ["src", "tests"]`, and `"pydantic.mypy" in plugins`); plus the gate itself, which the verifier runs (section 7, mypy output must say it checked files under `tests/`) |
| A7 | Every `Settings` field appears in `.env.example`, enforced by a test | `test_config.py::test_env_example_lists_every_field` (regex from section 4.5: the set of names equals `{env_var(f) for f in Settings.model_fields}`, and each name occurs exactly once); `test_config.py::test_env_example_values_are_defaults` (`Settings(_env_file=ROOT / ".env.example") == Settings(_env_file=None)`) |

The pytester tests need the inner run to see pytest-socket. It is installed in the venv
and autoloads through its entry point, so `runpytest_subprocess` picks it up. They also
need the inner `pyproject.toml` to be the repo's real one, read from `ROOT`, not a copy
pasted into the test, so the test fails if the real config is loosened.

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | Defaults match the architecture table | `test_config.py::test_defaults` (every field's default asserted, including backend names, `llm_model == "claude-opus-5-5"`, `embed_model`, `tz is None`, `log_level == "INFO"`, secrets `None`) |
| B2 | `EARMARK_` env vars and `.env` are read, env beats `.env` | `test_config.py::test_reads_env_prefix`, `test_config.py::test_reads_dotenv_in_cwd`, `test_config.py::test_env_overrides_dotenv`, `test_config.py::test_unprefixed_vars_ignored` (`PORT=1` and `TZ=...` have no effect), `test_config.py::test_unknown_earmark_key_ignored` (`EARMARK_PROT=1` in `.env` loads fine) |
| B3 | Empty values mean unset | `test_config.py::test_empty_values_are_unset` (`EARMARK_API_TOKEN=` → `None`; `EARMARK_ASR_LANGUAGE=` → `None`; `EARMARK_VAULT_SUBDIR=` → `"Earmark"`) |
| B4 | Secret fields are `SecretStr` and hidden in repr | `test_config.py::test_secret_fields_are_secretstr` (fields whose names end in `_token`, `_key`, `_secret`, or `_password` have annotation `SecretStr \| None`, and that set equals `{"api_token", "asr_api_key", "llm_api_key", "ntfy_token"}`); `test_config.py::test_secrets_hidden_in_repr_and_str` (set each, assert value absent from `repr()` and `str()`); `test_config.py::test_secret_values_lists_only_set_secrets` |
| B5 | Invalid values are rejected naming the variable, without echoing input | `test_config.py::test_invalid_values_rejected`, parametrized over `EARMARK_PORT=0`, `EARMARK_PORT=70000`, `EARMARK_JOB_MAX_ATTEMPTS=0`, `EARMARK_WORKER_POLL_SECONDS=0`, `EARMARK_MAX_AUDIO_MB=-1`, `EARMARK_ASR_BACKEND=whisperx`, `EARMARK_RESOLVER_BACKEND=none`, `EARMARK_INDEX_BACKEND=chroma`, `EARMARK_TZ=Mars/Base`, `EARMARK_LOG_LEVEL=verbose`, `EARMARK_VAULT_SUBDIR=../x`, `EARMARK_VAULT_SUBDIR=/abs`. Each: `load_settings(env_file=None)` raises `ConfigError` whose `errors` has a line starting with that variable name, `str(exc)` does not contain `input_value`, and, for the non-numeric bad values (`whisperx`, `chroma`, `Mars/Base`, `verbose`, `../x`, `/abs`), `str(exc)` does not contain the bad value. Use distinctive strings where possible (for example `EARMARK_ASR_BACKEND=whisperx-sentinel`) |
| B6 | Accepted variants | `test_config.py::test_backend_none_allowed_for_index_and_notify`; `test_config.py::test_all_backends_accept_fake`; `test_config.py::test_log_level_case_insensitive`; `test_config.py::test_valid_tz_accepted` (`Europe/Oslo`, `UTC`); `test_config.py::test_nested_vault_subdir_accepted` (`Notes/Earmark`) |
| B7 | Settings are frozen | `test_config.py::test_settings_frozen` |
| B8 | `config` lists every field once, in env form | `test_cli.py::test_config_lists_every_field` (each `env_var(f)` appears as a line prefix exactly once, and `EARMARK_PORT=8787` and `EARMARK_KEEP_AUDIO=false` appear verbatim) |
| B9 | `redact_url` behaviour | `test_config.py::test_redact_url` (parametrized: password masked; username only unchanged; no userinfo unchanged; port preserved; garbage string unchanged) |
| B10 | Logging redacts secrets and is idempotent | `test_log.py::test_redacts_secret_in_message_and_args`; `test_log.py::test_redacts_secret_in_traceback` (`logger.exception` inside `except` of an error whose message contains the secret); `test_log.py::test_longer_secret_redacted_first` (secrets `"abc"` and `"abcdef"`, message `"abcdef"` → `REDACTED` with no `"def"` left over); `test_log.py::test_empty_secret_ignored`; `test_log.py::test_configure_twice_single_handler`; `test_log.py::test_level_applied`. All use `stream=io.StringIO()` and the `restore_root_logging` fixture. |
| B11 | `import earmark` stays light | `test_cli.py::test_import_earmark_is_light` (subprocess `[sys.executable, "-c", ...]` imports `earmark`, prints `sorted(m for m in sys.modules if m.split(".")[0] in {"pydantic", "pydantic_settings", "typer", "fastapi", "uvicorn", "httpx2", "faster_whisper", "ctranslate2", "lancedb", "fastembed", "anthropic"})`, expects `[]`); also asserts `earmark.__version__ == importlib.metadata.version("earmark")` |
| B12 | `python -m earmark` works | `test_cli.py::test_module_entry_point_version` (subprocess `[sys.executable, "-m", "earmark", "--version"]`) |
| B13 | No-args shows help | `test_cli.py::test_no_args_shows_help` (exit code 2, output contains `config`) |
| B14 | Gate guards cannot be quietly loosened | `test_tooling.py::test_pytest_guards_configured` (parse pyproject: `tool.pytest.strict is True`; `"--disable-socket"` in addopts; `"error"` is the first `filterwarnings` entry); `test_tooling.py::test_ruff_lint_groups` (`tool.ruff.lint.select` contains at least `E, F, I, B, UP, S, RUF, DTZ, T20`) |
| B15 | Lock is enforced for aarch64 | `test_tooling.py::test_required_environments_include_aarch64` (pyproject `tool.uv.required-environments` contains the linux/aarch64 marker) |

Subprocess tests use `sys.executable` and absolute paths, so they need no shell, no
network, and no `uv`. They run with the current environment plus `cwd=tmp_path` (from
the isolation fixture).

## 7. Verification

The verifier runs these, in order, and reports each result.

```bash
# 1. Clean-clone gate (the brief's first criterion). The step is uncommitted, so emulate a
#    clone by copying exactly the files git would track (tracked + untracked-not-ignored).
#    rsync is not installed in this sandbox; use git ls-files + cp --parents.
D=$(mktemp -d -p "$SCRATCH")          # SCRATCH = the verifier's scratchpad directory
cd /home/sandy/dev/earmark
git ls-files -z --cached --others --exclude-standard | xargs -0 cp --parents -t "$D"
cd "$D" && ls -a                      # no .venv, .env, caches
uv sync && make check
cd / && rm -rf "$D"                   # free tmpfs space afterwards

# 2. In the repo itself
cd /home/sandy/dev/earmark
uv sync
make check
uv run mypy                            # expect "Success: no issues found in N source files", N counting tests/*.py
uv run earmark --version               # expect: earmark 0.1.0
uv run python -m earmark --version     # same
uv run earmark                         # help, exit 2

# 3. Secrets never printed (manual, in addition to the test)
EARMARK_API_TOKEN=live-sentinel-123 EARMARK_LLM_API_KEY=live-sentinel-456 \
  uv run earmark config | tee /dev/stderr | grep -c 'live-sentinel' # expect 0
EARMARK_PORT=bogus-sentinel uv run earmark config; echo "exit=$?"   # expect exit=2, names EARMARK_PORT, no 'bogus-sentinel'

# 4. Markers and sockets
uv run pytest -q                       # all pass; summary shows no network/slow/llm/gpu tests run
uv run pytest -q -m network            # "no tests ran" / all deselected is fine at this step (exit 5 acceptable)
uv run pytest --markers | grep -E '^@pytest.mark.(network|slow|llm|gpu):'

# 5. Lock and platform pin
uv lock --check                        # lock is current
cat .python-version                    # 3.13
grep -n 'requires-python\|required-environments' pyproject.toml
```

Also check:

- `git diff --stat` touches only the files in section 3, plus `docs/ARCHITECTURE.md`
  and this plan.
- No `# type: ignore` and no `noqa` anywhere in `src/` or `tests/`. None should be
  needed in this step.
- `.env.example` contains no real-looking secret.
- `httpx` (as opposed to `httpx2`) does not appear in `pyproject.toml`.
  `uv tree --depth 1` shows no `httpx` as a direct dependency.

Nothing in this step needs a GPU, Docker, or credentials, so there is no live smoke
beyond the CLI commands above. Heavy-extra installation was verified by the planner in
scratch (section 2.1) and is not repeated here, to save tmpfs space.

## 8. Deviations

1. **httpx2 instead of httpx.** The brief lists `httpx` as a runtime dependency. httpx
   has had no release since December 2024, its maintained continuation is httpx2 (same
   API, import name `httpx2`), the `anthropic` SDK that step 10 adds requires httpx2,
   and Starlette's TestClient emits a deprecation warning without httpx2. That warning
   would fail the suite under `filterwarnings = error`. Using both would carry two HTTP
   stacks. Recorded in `docs/ARCHITECTURE.md` (Decisions table, tooling row). Later
   plans should write `import httpx2`.
2. **`tzdata` added as a runtime dependency.** It is not in the brief. It is tiny, pure
   Python, and maintained by CPython core developers, and it makes `TZ` validation
   (and steps 12 and 18's dates) independent of the host's zone files, which slim
   container images may lack. The alternative, relying on `/usr/share/zoneinfo`, works
   in this sandbox but is not guaranteed in step 16's container.
3. **`requires-python = ">=3.13,<3.14"`** instead of the architecture's "Python ≥ 3.12".
   The brief asked for a pin. The upper bound is needed because uv's universal lock must
   otherwise solve for 3.15, where onnxruntime has no wheels. Architecture updated.
4. **`tool.uv.required-environments` for linux/aarch64.** Not in the brief. It turns
   architecture risk 3 into a lock-time check for every later dependency.
5. **New setting `LOG_LEVEL`** (default `INFO`). Logging setup is in the brief but the
   configuration table had no level. Added to the architecture table.
6. **Concrete backend names and the `EMBED_MODEL` default** are now fixed in the
   architecture's configuration table: `apple`, `http`, `faster_whisper` or `remote`,
   `anthropic` or `openai_compat`, `vault`, `lancedb`, `ntfy`, plus `fake` and `none`
   where stated, and `BAAI/bge-small-en-v1.5`. The table previously said "real backends"
   and "a small local model".
7. **New modules `log.py` and `__main__.py`**, added to the architecture's module
   layout. `log.py` rather than `logging.py`, to avoid confusion with the stdlib module.
8. **Configuration semantics documented in the architecture:** empty means unset,
   unknown keys are ignored, `.env` is not exported to the process environment (relevant
   to `ANTHROPIC_API_KEY` in step 10), and `Settings` is frozen.
9. **Testing section in the architecture** now states `--allow-unix-socket`, the
   automatic socket re-enable for `network`, `slow`, and `llm`, and the `-m` override
   semantics.
10. **Architecture risks 1 and 3** were updated with this step's findings: ctranslate2's
    aarch64 wheel is CPU-only, and the wheel check passed for 3.13.

`docs/PLAN.md` still says `httpx` in the step 01 brief. Planners do not edit PLAN.md
under the build-loop protocol; this section and the architecture are the record.

## 9. Out of scope

- `models.py`, `errors.py`, `artifacts.py`, `pipeline/`, `fakes.py` (step 02). Note that
  `ConfigError` lives in `config.py` and is not one of the pipeline's retry-intent
  errors.
- Refusing to serve without a token, and any API code (step 04). `fastapi` and `uvicorn`
  are installed now but unused.
- `earmark serve`, `worker`, `submit`, `jobs`, `doctor`, and any other subcommands.
- Filling the `transcribe` and `index` extras (steps 08 and 14), and any `anthropic`
  dependency (step 10).
- Warning about unknown `EARMARK_*` keys (step 15 `doctor`).
- Validating URL settings beyond redaction (steps 09, 11, 15).
- README content, Docker, and deployment docs (step 16).
