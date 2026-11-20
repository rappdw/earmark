# Step 15: Notifications and doctor

Brief: `docs/PLAN.md`, step 15. Depends on step 05 (runner and worker). Builds on the
operator-endpoint pattern of steps 09 and 11, the fetch guard of step 06, the note paths of
step 12, and the backend builders of steps 08, 10, 11, and 14.

Nothing in this step is blocked. One live check cannot be run here: the Anthropic online
check, because the sandbox has no API key. It is tested against a mock transport.

## 1. Outcome

- `NOTIFY_BACKEND=ntfy` is built. With `EARMARK_NTFY_URL` set, the worker sends one ntfy
  notification per job that ends `done`, `failed`, or `skipped`. Each status has its own
  title prefix, tag, and priority. Failed and skipped notifications carry the reason. A
  done notification carries an `obsidian://open` link to the note when
  `EARMARK_OBSIDIAN_VAULT_NAME` is set. Without `NTFY_URL` the backend sends nothing, and
  the worker starts. Today the default config does not build at all, because `ntfy` has no
  builder.
- The ntfy client is earmark's own small httpx2 client, the same pattern as the remote ASR
  and local LLM clients (no address guard, no redirects, no proxy variables, a capped
  response, host-only messages, and the token only in a per-request header). It publishes
  JSON to the server root. Untrusted text never goes into a header or the URL.
- `earmark doctor` runs offline checks:
  - the API token;
  - data directory and vault writability;
  - the database;
  - for each selected backend: that it builds the way the worker builds it, which imports
    its extra and checks its credentials, plus whether its model is in the cache.

  It loads no model, calls no API, downloads nothing, and creates nothing except two probe
  files that it removes. `earmark doctor --online` adds reachability checks with 10 s
  timeouts: the iTunes lookup, the ASR and LLM servers' `GET /models`, Anthropic's free
  `GET /v1/models/{model}`, and ntfy's `/v1/health` and `/v1/account`. None of them spends
  money or publishes a notification. Output redacts every secret. The exit code is 0
  (warnings allowed), 1 (a check failed), or 2 (settings do not load).

## 2. Findings

### 2.1 ntfy publishing (checked 2026-11-20)

I read `docs.ntfy.sh/publish` and the server source (`binwiederhier/ntfy`, `main`,
`server/server.go`, `util.go`, `types.go`, `errors.go`, `config.go`) and called ntfy.sh
live.

- **Two ways to publish.** One is `POST/PUT` to the topic URL, with the message as the
  body and options in headers (`X-Title`/`Title`/`t`, `X-Priority`, `X-Tags`, `X-Click`,
  …). The other is `POST/PUT` of JSON to the **root URL** (`r.URL.Path == "/"`) with
  `topic`, `message`, `title`, `tags` (array), `priority` (int 1–5), `click`, `markdown`,
  … (`publishMessage` in `types.go`).
- **Header mode reads query parameters too.** `readParam` checks headers, then query
  parameters, and decodes RFC 2047 encoded-words in header values (`maybeDecodeHeader`,
  Go's `mime.WordDecoder.DecodeHeader`, anywhere in the value).
- **JSON mode.** The server copies the fields into internal headers (`transformBodyJSON`),
  so `maybeDecodeHeader` still runs on the title. The topic must match
  `^[-_A-Za-z0-9]{1,64}$` (`topicRegex`), or the server returns 400. The JSON body is
  limited to 2 × `message-size-limit`.
- **Limits** (`config.go`, docs "Limitations"). Messages up to 4096 bytes. Longer
  messages, or messages that are not UTF-8, become attachments. The title is capped at
  1024 bytes (`messageTitleSizeLimit`) and all tags at 512 bytes, else 400. Visitors get
  a burst of 60 requests, refilled one per 5 s. ntfy.sh allows 250 messages a day for an
  anonymous IP (live `/v1/account`: `"messages":250`).
- **Errors** are JSON `{"code": 40101, "http": 401, "error": "unauthorized", "link": …}`.
  The 429 variants (`42901` too many requests, `42908` daily quota, …) carry `Retry-After`.
  Others are 413 (`41303` JSON body too large) and 507 (UnifiedPush without subscriber).
- **Authentication.** `Authorization: Bearer tk_…` for access tokens. Basic auth with an
  empty user and the token as the password also works, as does the `auth=` query
  parameter. earmark uses Bearer only.
- **Live, ntfy.sh.**
  - `GET /v1/health` → 200 `{"healthy":true,"role":"active"}`.
  - `GET /v1/account` anonymously → 200 `{"username":"*","role":"anonymous",
    "limits":{…},"stats":{"messages_remaining":250,…}}`. With a bad token → 401 with the
    JSON above.
  - `GET /v1/version` → 401 (admin only, not usable).
  - `GET /<topic>/auth` → 200 `{"success":true}`, but it checks **read** access only
    (`authorizeTopicRead`).
  - A JSON `POST https://ntfy.sh/` with `{"topic": "<random>", "title": "Note ready: Café
    \r\nX-Evil: 1", "message": "line1\nline2", "tags": ["white_check_mark"], "priority": 3,
    "click": "obsidian://open?vault=My%20Vault&file=Earmark%2FPodcasts%2Fx.md"}` → 200
    `{"id":"cB3aIQWm7ySR","time":…,"expires":…,"event":"message","topic":…,"title":"Note
    ready: Café \r\nX-Evil: 1","message":"line1\nline2","priority":3,
    "tags":["white_check_mark"],"click":"obsidian://open?…"}`.
  - So JSON mode keeps CR/LF inside the title. It cannot inject a header into our request,
    but it is shown to the user, which is why earmark cleans titles itself. A custom-scheme
    `click` is accepted as sent; the server checks only `attach` and `icon` against
    `^https?://`.
  - `GET /<topic>/json?poll=1&since=all` returns the published message, so a live test can
    check the round trip.
- **iOS.** The ntfy iOS app opens `click` with `UIApplication.shared.open(URL(string:
  click))` (`ntfy-ios`, `AppDelegate.swift`), so `obsidian://` opens Obsidian if it is
  installed. `URL(string:)` needs a well-formed URL, so every reserved and non-ASCII
  character must be percent-encoded. A self-hosted server needs `upstream-base-url:
  "https://ntfy.sh"` for instant iOS delivery (docs `config.md`; without it, delivery "can
  take hours"). That is deployment documentation for step 16.
- **Sub-paths.** The docs do not document hosting ntfy under a path prefix. earmark
  derives the root as "the topic URL minus its last segment", so a reverse proxy that
  strips a prefix (`https://host/ntfy/<topic>`) works, but this is not promised.

**Decision: JSON publishing to the root.** Title, message, tags, priority, and click
travel in the JSON body. The only request headers are fixed (`User-Agent`, `Accept`,
`Content-Type: application/json`) plus `Authorization: Bearer <NTFY_TOKEN>` when a token
is set. The token is validated as printable ASCII without spaces when settings load, and
h11 would refuse CR/LF in any header value anyway. There is no RFC 2047 encoding to get
right and no query-string fallback for a hostile value to reach. Header mode was rejected
because a feed title would have to be put into `X-Title`.

### 2.2 Obsidian URI (checked 2026-11-20)

From `obsidianmd/obsidian-help`, `en/Extending Obsidian/Obsidian URI.md`:

- The form is `obsidian://open?vault=<name or id>&file=<path from vault root>`.
- "Ensure that your values are properly URI encoded. For example, forward slash characters
  `/` must be encoded as `%2F` and space characters must be encoded as `%20`."
- "If the file extension is `md`, the extension can be omitted", so including it is
  allowed.
- `Note%23Heading` navigates to a heading. A `#` in the path, even encoded, therefore
  means a heading, so a note whose path contains `#` cannot be opened by URI. earmark's
  sanitizer removes `#` from generated names, but a user may rename a note.
- `StoreResult.note_path` is POSIX and relative to `VAULT_DIR`, the vault root
  (architecture, Domain models), so it is the `file` value as is.

**Encoding.** `urllib.parse.quote(value, safe="")` on both values: UTF-8, everything
except `A-Z a-z 0-9 _ . - ~` percent-encoded, including `/ & ? = + % # ! ' ( ) *`. That is
stricter than JavaScript's `encodeURIComponent`, and it matches what Obsidian asks for.
Neither value is Unicode-normalised: the path must match the file name on disk byte for
byte, and the vault name is used as configured (stripped). Golden values, computed with
CPython 3.13:

- `("My Vault", "Earmark/Podcasts/Hard Fork/2026-11-20 Café & Co?.md")` →
  `obsidian://open?vault=My%20Vault&file=Earmark%2FPodcasts%2FHard%20Fork%2F2026-11-20%20Caf%C3%A9%20%26%20Co%3F.md`
- `quote("a+b=c%d~e_f.g-h!'()*", safe="")` → `a%2Bb%3Dc%25d~e_f.g-h%21%27%28%29%2A`
- `"🎙"` → `%F0%9F%8E%99`; NFD `"é"` → `e%CC%81` (kept, not composed).

### 2.3 Anthropic online check (claude-api skill loaded; docs checked 2026-11-20)

- **Token counting.** `platform.claude.com/docs/en/build-with-claude/token-counting`:
  "Token counting is **free to use** but subject to requests per minute rate limits". It
  needs a request body.
- **Models API.** `GET /v1/models/{model_id}` (`docs/en/api/models/retrieve`) returns
  `ModelInfo` (`id`, `display_name`, `lifecycle` `active|deprecated|retired`,
  `retires_at`, `max_input_tokens`, …). It is a metadata read: no tokens, no inference,
  nothing billed. An unknown id is 404, a bad key 401.
- **Installed SDK** (`anthropic` 2.12.0, `inspect.signature`):
  - `client.models.retrieve(model_id, *, workspace_id=omit, …, timeout=NOT_GIVEN) ->
    ModelInfo`.
  - `ModelInfo.model_fields` include `lifecycle: Literal['active','deprecated','retired']`.
  - `client.with_options(timeout=…, max_retries=…)` exists.

**Decision: `models.retrieve(LLM_MODEL)`.** It checks the credential and the model id in
one free GET, reports the model's lifecycle, sends no transcript-shaped body, and costs
nothing. `count_tokens` is also free and acceptable, but it would only prove the
credential. Doctor never calls `messages.create` or `messages.stream`. The call uses
`max_retries=0` and a 10 s timeout, and its errors go through the existing
`map_api_error`.

### 2.4 Model cache checks without loading or downloading (checked in a scratch venv)

I checked the versions the lock pins (`faster-whisper` 1.2.1, `fastembed` 0.9.1,
`huggingface-hub` 1.33.0) with `uv run --no-project --with …` in the scratchpad, with
`socket.connect` and `getaddrinfo` patched to raise.

- **Whisper.** `faster_whisper.utils.download_model(size_or_id, output_dir=None,
  local_files_only=False, cache_dir=None, revision=None, use_auth_token=None)` is public.
  `WhisperModel(download_root=…)` passes `download_root` to it as `cache_dir` (step 08,
  2.3). With `local_files_only=True` it makes no network call:
  - a missing model raises `huggingface_hub.errors.LocalEntryNotFoundError`, whose MRO is
    `LocalEntryNotFoundError → FileNotFoundError → OSError → EntryNotFoundError`;
  - an unknown size raises `ValueError("Invalid model size 'nope', expected one of: …")`;
  - a cached snapshot returns its directory.

  A directory given as `ASR_MODEL` is loaded by `WhisperModel` as is, so doctor checks it
  for `model.bin`. `faster_whisper.available_models()` lists the sizes.
- **Embeddings.** `TextEmbedding.list_supported_models()` returns dicts such as
  `{"model": "BAAI/bge-small-en-v1.5", "sources": {"hf": "Qdrant/bge-small-en-v1.5-onnx-Q",
  …}, "model_file": "model_optimized.onnx", "dim": 384, "size_in_GB": 0.067, …}`. All 41
  entries have an `hf` source. fastembed stores downloads in the Hugging Face cache layout
  under `cache_dir` (step 14, 2.x).
  - `huggingface_hub.try_to_load_from_cache(repo_id, filename, cache_dir=None,
    revision=None, repo_type=None)` is a pure filesystem lookup (no network in its
    source).
  - It returns `None` for an empty cache and the file path for a cached snapshot (checked
    with a hand-made `models--…/snapshots/<rev>/` and `refs/main`).
- Constructing `TextEmbedding` or `WhisperModel` would load the model (steps 08 and 14),
  so doctor does neither.

### 2.5 Other online targets

- **iTunes.** `GET https://itunes.apple.com/lookup?id=1&entity=podcast` → 200
  `text/javascript`, body `{"resultCount":0,"results":[]}`. That proves reachability
  without depending on any show existing.
- **Sandbox.** DNS here answers 10.200.9.2 for every name (Known risk 5), so through the
  guard this check fails here. That is correct, and with `EARMARK_ALLOW_PRIVATE_FETCH=true`
  it succeeds. An httpx2 client with `trust_env=False` and no guard reaches
  `https://ntfy.sh/v1/health` from the sandbox (checked: 200), so the ntfy online check
  can run live here.
- **ASR and LLM servers.** vLLM, llama.cpp, Ollama, and LM Studio serve `GET /v1/models`
  (`{"data": [{"id": …}, …]}`; step 11, 2.2). A server without it answers 404, which doctor
  reports as a warning.

### 2.6 Code as it is now

- `NOTIFIERS` in `pipeline/factory.py` has only `fake` and `none`, so the **default
  settings do not build** (`EARMARK_NOTIFY_BACKEND: backend 'ntfy' is not built`).
  `tests/test_factory.py::test_default_settings_list_all_unbuilt` asserts exactly that, and
  `test_unbuilt_backend_names_setting` is parametrised over the unbuilt names. Both must
  change once every name is built (section 6.6).
- `Pipeline.notify` (step 05) already catches every `Exception` from the notifier. It logs
  at WARNING with the traceback and records `notify failed: <describe_error>` with
  secrets redacted.
- `Settings.ntfy_url` is a plain `str | None` with no validation. `earmark config` shows it
  through `redact_url`, which prints the topic. On public ntfy.sh without auth, the topic is
  the only secret.
- `tests/test_cli.py::test_config_redacts_url_password` sets
  `EARMARK_NTFY_URL=https://bob:sentinel-pw-1@ntfy.example/t` and expects it echoed with
  the password masked. After this step that value is refused (section 4.1), so the test
  changes.
- Helpers to reuse:
  - `endpoint.host_label` and `error_detail`; `fetch.USER_AGENT`, `MIB`, and
    `parse_retry_after`; `log.redact`.
  - `vault.sanitize.clean_unicode` and `fit_bytes`.
  - `api.auth.require_api_token`.
  - `db.Database`, `DB_FILENAME`, and `SCHEMA_VERSION`.
  - `vault.fs.Vault.open_existing_subdir` (creates nothing, refuses symlinks).
  - `lancedb_index.LanceIndex._existing_table` and `_check` (metadata only).
  - `embed.load_fastembed_runtime` and `default_model_dir`.
  - `transcribe.faster_whisper.load_runtime`, `check_device`, and `WhisperOptions`.
  - `AnthropicClient` (its constructor detects a missing credential) and `map_api_error`.
  - `OpenAICompatOptions.from_settings` and `RemoteOptions.from_settings`.
  - `GuardedFetcher` and `resolve.itunes.LOOKUP_URL`.
- Test helpers to reuse:
  - `tests/llm_stub.py` and `tests/asr_stub.py` (unix-socket servers);
  - `tests/anthropic_stub.py` (`AnthropicStub` raises `AssertionError` on any unexpected
    route, which proves doctor makes no completion call);
  - `tests/index_helpers.stub_extra_missing`;
  - the `sys.modules` stubs in `tests/test_faster_whisper.py`;
  - `tests/http_helpers.StaticResolver` and `ScriptedNetwork`;
  - `tests/pipeline_env.make_env(backends=...)`.

**Dead ends.** Do not route ntfy through `fetch.py`: a tailnet ntfy server is refused by
the guard, and `ALLOW_PRIVATE_FETCH=true` would turn the guard off for every feed. Do not
construct `TextEmbedding` or `WhisperModel` in doctor, since both load or download the
model. Do not use `GET /<topic>/auth` as a "can publish" check, because it tests read
access. Do not use `/v1/version`, which is admin-only.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/config.py` | `ntfy_url` becomes `SecretStr \| None`, with `_check_ntfy_url`. `ntfy_token` joins `_check_api_key`. `_check_obsidian_vault_name`. New `NTFY_TOPIC_PATTERN`. |
| `src/earmark/notify/__init__.py` | New, docstring only. |
| `src/earmark/notify/ntfy.py` | New. `split_topic_url`, `obsidian_url`, `clean_field`, `Notification`, `compose`, `NtfyOptions`, `NtfyClient` (`publish`, `health`, `account`, `close`), `NtfyNotifier`, `build_ntfy_notifier`. |
| `src/earmark/pipeline/factory.py` | `NOTIFIERS["ntfy"] = _ntfy_notifier` (lazy import). Every backend name is now built. |
| `src/earmark/doctor/__init__.py` | New. `Probes`, `run_doctor`. |
| `src/earmark/doctor/report.py` | New. `Status`, `CheckResult`, `render`, `exit_code`, `summary`. |
| `src/earmark/doctor/checks.py` | New. The offline checks. |
| `src/earmark/doctor/online.py` | New. The `--online` checks and `probe_models`. |
| `src/earmark/cli.py` | `earmark doctor [--online]`. |
| `src/earmark/vault/fs.py` | `VaultDir.probe_write()`. |
| `src/earmark/index/lancedb_index.py` | `LanceIndex.check_existing() -> bool`. |
| `src/earmark/index/embed.py` | `ModelInfo` gains `hf_repo` and `model_file` (with defaults). `find_model(settings)` is split out of `build_embedder`. `model_cached(info, cache_dir)`. |
| `src/earmark/transcribe/faster_whisper.py` | `model_cache_status(options)`. |
| `src/earmark/analyze/llm/anthropic.py` | `AnthropicClient.credential_source` and `AnthropicClient.check_model(timeout_s)`, plus `ModelCheck`. |
| `.env.example` | Comments for `NTFY_URL` (topic URL form, secret), `NTFY_TOKEN`, and `OBSIDIAN_VAULT_NAME`. |
| `docs/ARCHITECTURE.md` | Done in this planning change (Deviations 8). |
| `tests/ntfy_stub.py` | New. A unix-socket ntfy server (POST `/`, GET `/v1/health`, GET `/v1/account`) and recorded bodies. |
| `tests/test_ntfy.py` | New. Compose, encoding, client, and notifier tests. |
| `tests/test_ntfy_live.py` | New, `network`. Publish and poll round trip. |
| `tests/test_doctor.py` | New. Offline and online checks, injected. |
| `tests/test_doctor_index.py` | New, `index`. Real LanceDB and fastembed listing. |
| `tests/test_config.py` | NTFY_URL, NTFY_TOKEN, and OBSIDIAN_VAULT_NAME validation. The secret field set gains `ntfy_url`. |
| `tests/test_cli.py` | `doctor` exit codes and redaction. The changed `test_config_redacts_url_password`. |
| `tests/test_factory.py` | The registry tests after every name is built. `ntfy` with and without a URL. |
| `tests/test_runner.py` (or `test_ntfy.py`) | A real `NtfyNotifier` inside the pipeline: failure → warning. |
| `tests/anthropic_stub.py` | `GET /v1/models/{id}` route and a `model_info_body` helper. |
| `tests/test_vault_fs.py`, `tests/test_lancedb_index.py`, `tests/test_index_embed.py`, `tests/test_faster_whisper.py`, `tests/test_llm_anthropic.py` | Unit tests for the small additions. |

No new dependency. The package `src/earmark/notify/` does not exist yet, although the
architecture lists `notify/ntfy.py`.

## 4. Interfaces

Snippets are illustrative and ruff-format clean. Names are binding; bodies are not.

### 4.1 Settings (`config.py`)

```python
NTFY_TOPIC_PATTERN: Final = re.compile(r"[-_A-Za-z0-9]{1,64}")

ntfy_url: SecretStr | None = Field(None, description="ntfy topic URL (secret).")
ntfy_token: SecretStr | None = Field(None, description="ntfy access token.")
obsidian_vault_name: str | None = Field(None, description="Vault name for obsidian:// links.")
```

**`_check_ntfy_url`** (field validator on `ntfy_url`; `v` is a `SecretStr`):

- Strip the value; an empty one is `None`.
- `urlsplit`. The scheme must be `http` or `https` and there must be a host, else "must be
  an http(s) topic URL such as https://ntfy.sh/my-topic".
- No userinfo, else "must not contain credentials; use EARMARK_NTFY_TOKEN".
- No query or fragment.
- After dropping one trailing `/`, the last path segment must fullmatch
  `NTFY_TOPIC_PATTERN`, else "must end in the topic name (letters, digits, '-' and '_', at
  most 64 characters), such as https://ntfy.sh/my-topic". Earlier segments, the path
  prefix, may be anything a URL allows.
- Return `SecretStr(stripped value without the trailing "/")`.
- Messages never contain the value: `load_settings` already uses `include_input=False`,
  and the messages above are literals.

**`ntfy_token`** joins `_check_api_key` (`asr_api_key`, `llm_api_key`, `ntfy_token`):
stripped, blank → `None`, printable ASCII without spaces.

**`_check_obsidian_vault_name`**: stripped, blank → `None`. A character of category `Cc`
or `Cf` → "must not contain control characters". More than 200 characters → "at most 200
characters".

**Effects.**

- `secret_values()` now includes the full NTFY URL. It is redacted from logs, job text,
  and doctor output.
- `redacted_items()` shows `EARMARK_NTFY_URL=**********`, because the `SecretStr` branch
  comes before the `_url` branch. `redact_url` stays for the other `_url` fields.

### 4.2 `notify/ntfy.py`

Imports `httpx2`; import it only from the factory builder and from doctor, absolutely
(like `openai_compat`).

```python
CONNECT_TIMEOUT_S: Final = 5.0
TIMEOUT_S: Final = 10.0  # read, write, pool
MAX_RESPONSE_BYTES: Final = 64 * 1024
TITLE_MAX_BYTES: Final = 250
NAME_MAX_BYTES: Final = 200  # the episode-title part of a title
MESSAGE_MAX_BYTES: Final = 2000
LINE_MAX_BYTES: Final = 600  # any one message line (reason, prompt, warning)
PROMPT_MAX_BYTES: Final = 300
MAX_CLICK_CHARS: Final = 2000
PRIORITY_DEFAULT: Final = 3
PRIORITY_HIGH: Final = 4
TAGS: Final[Mapping[JobStatus, str]] = {
    "done": "white_check_mark",
    "failed": "x",
    "skipped": "fast_forward",
}
TITLE_PREFIX: Final[Mapping[JobStatus, str]] = {
    "done": "Note ready",
    "failed": "Failed",
    "skipped": "Skipped",
}
UNKNOWN_EPISODE: Final = "podcast episode"


def split_topic_url(url: str) -> tuple[str, str]:
    """(root URL ending in '/', topic) of a validated topic URL; ValueError otherwise."""


def obsidian_url(vault_name: str, note_path: str) -> str | None:
    """obsidian://open?vault=…&file=…, both quote(…, safe=""); None if unusable."""


def clean_field(text: str, max_bytes: int) -> str:
    """One line of untrusted text, safe to show: see section 5.2."""


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    tags: tuple[str, ...]
    priority: int
    click: str | None

    def payload(self, topic: str) -> dict[str, object]:
        """The JSON publish body: topic, title, message, tags, priority, and click if set."""


def compose(
    job: Job,
    episode: Episode | None,
    *,
    vault_name: str | None,
    secrets: Sequence[str] = (),
) -> Notification | None:
    """The notification for a terminal job; None for queued or running."""


@dataclass(frozen=True, slots=True)
class NtfyOptions:
    root_url: str  # ends in "/"
    topic: str
    token: SecretStr | None
    vault_name: str | None
    secrets: tuple[str, ...]  # Settings.secret_values(), for redaction

    @property
    def host(self) -> str:
        return host_label(self.root_url)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        """None when EARMARK_NTFY_URL is unset."""


@dataclass(frozen=True, slots=True)
class NtfyHealth:
    status: int  # HTTP status of GET <root>v1/health
    healthy: bool | None  # the "healthy" field when the body is the expected JSON


@dataclass(frozen=True, slots=True)
class NtfyAccount:
    username: str | None
    messages_remaining: int | None


class NtfyClient:
    def __init__(
        self,
        options: NtfyOptions,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None: ...

    def publish(self, notification: Notification) -> None: ...  # raises EarmarkError
    def health(self) -> NtfyHealth: ...  # raises TransientError / PermanentError
    def account(self) -> NtfyAccount: ...  # with the token; raises on 401/403/3xx
    def close(self) -> None: ...


class NtfyNotifier:
    """The `ntfy` Notifier."""

    def __init__(self, client: NtfyClient) -> None: ...
    def notify(self, job: Job, episode: Episode | None, /) -> None: ...
    def close(self) -> None: ...


def build_ntfy_notifier(
    settings: Settings, *, transport: httpx2.BaseTransport | None = None
) -> NtfyNotifier | None:
    """None (and one INFO log) without EARMARK_NTFY_URL. Makes no request."""
```

The client is created lazily, like `OpenAICompatClient._get_client`:

```python
httpx2.Client(
    transport=transport or httpx2.HTTPTransport(),
    trust_env=False,
    follow_redirects=False,
    timeout=httpx2.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S),
    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
)
```

`client.cookies.clear()` runs before every request.

**Requests.**

- **publish.** `POST {root_url}`. Headers: `Content-Type: application/json`, plus
  `Authorization: Bearer <token>` only when the token is set. Body:
  `json.dumps(payload, ensure_ascii=False).encode()`.
- **health.** `GET {root_url}v1/health`, never with the token.
- **account.** `GET {root_url}v1/account`, with the token when set.

### 4.3 Factory

```python
def _ntfy_notifier(ctx: BuildContext) -> Notifier | None:
    from earmark.notify.ntfy import build_ntfy_notifier

    return build_ntfy_notifier(ctx.settings)


NOTIFIERS: Final[Mapping[str, Builder[Notifier | None]]] = {
    "fake": lambda ctx: FakeNotifier(),
    "none": lambda ctx: None,
    "ntfy": _ntfy_notifier,
}
```

### 4.4 Additions to existing modules

```python
# vault/fs.py
class VaultDir:
    def probe_write(self) -> None:
        """Create and remove one .earmark-<16 hex>.tmp (O_EXCL|O_NOFOLLOW); raises OSError."""


# index/lancedb_index.py
class LanceIndex:
    def check_existing(self) -> bool:
        """False when there is no index yet; True when it matches this embedder.

        Raises IndexMismatchError or TransientError. Creates nothing, embeds nothing.
        """


# index/embed.py
@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str
    dim: int
    hf_repo: str | None = None
    model_file: str | None = None


def find_model(settings: Settings, runtime: FastEmbedRuntime) -> ModelInfo:
    """EMBED_MODEL looked up case-insensitively; ConfigError as build_embedder raises today."""


def model_cached(info: ModelInfo, cache_dir: Path) -> bool | None:
    """try_to_load_from_cache(hf_repo, model_file, cache_dir=...); None if it cannot tell."""


# transcribe/faster_whisper.py
type WhisperCacheStatus = Literal["local", "cached", "missing"]


def model_cache_status(options: WhisperOptions) -> WhisperCacheStatus:
    """Offline: a directory with model.bin, a cached snapshot, or missing.

    ConfigError naming EARMARK_ASR_MODEL for an unknown size or a directory without
    model.bin.
    """


# analyze/llm/anthropic.py
@dataclass(frozen=True, slots=True)
class ModelCheck:
    model_id: str
    display_name: str
    lifecycle: str  # "active", "deprecated", "retired"
    retires_at: str | None  # ISO date, if scheduled


class AnthropicClient:
    @property
    def credential_source(self) -> str: ...

    def check_model(self, *, timeout_s: float) -> ModelCheck:
        """GET /v1/models/{model} with max_retries=0; errors through map_api_error."""
```

- **`credential_source`.** `"EARMARK_LLM_API_KEY"` when `options.api_key` is set; else
  `"ANTHROPIC_API_KEY"` when `sdk.api_key` is set; else `"ANTHROPIC_AUTH_TOKEN"` when
  `sdk.auth_token` is set; else `"an ant profile or workload identity"`.
- **`model_cache_status`.**
  - `Path(options.model).is_dir()`: `"local"` if `model.bin` is a file in it, else a
    `ConfigError`.
  - Otherwise `importlib.import_module("faster_whisper.utils").download_model(
    options.model, local_files_only=True, cache_dir=str(options.model_dir) if set else
    None)`. Success → `"cached"`; `FileNotFoundError` → `"missing"`; `ValueError` →
    `ConfigError(["EARMARK_ASR_MODEL: '<clean name>' is not a faster-whisper model size
    (tiny, base, small, medium, large-v3, …), a Hugging Face repo id, or a directory"])`.
  - Any other exception propagates. Doctor catches it.
- **`find_model` and `model_cached`.** `build_embedder` calls `find_model`, so its
  behaviour does not change. `supported()` fills `hf_repo` from `entry["sources"]["hf"]`
  and `model_file` from `entry["model_file"]` when they are strings. `model_cached` imports
  `huggingface_hub` with `importlib`. It returns `None` when the import fails or when
  `hf_repo` or `model_file` is `None`, and `isinstance(found, str)` otherwise.

### 4.5 Doctor

```python
# doctor/report.py
type Status = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def render(results: Sequence[CheckResult], secrets: Sequence[str]) -> list[str]: ...
def summary(results: Sequence[CheckResult]) -> str: ...
def exit_code(results: Sequence[CheckResult]) -> int: ...  # 1 if any fail, else 0


# doctor/__init__.py
ONLINE_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class Probes:
    """Injection points for tests; production uses the defaults."""

    http_transport: httpx2.BaseTransport | None = None  # ntfy, ASR, LLM
    fetcher: GuardedFetcher | None = None  # the iTunes lookup
    anthropic_http_client: httpx2.Client | None = None
    timeout_s: float = ONLINE_TIMEOUT_S


def run_doctor(
    settings: Settings, *, online: bool = False, probes: Probes | None = None
) -> list[CheckResult]: ...
```

**Rendering.** Each line is `f"{label:<5} {name:<14} {detail}"`, with label `ok`, `warn`,
`FAIL`, or `skip`. The detail goes through `log.redact(detail, secrets)`, then the CLI's
non-printable replacement (`?`), then a 300-character cap with `…`. `summary` gives
`"doctor: all checks passed"`, `"doctor: all checks passed, 2 warnings"`, or
`"doctor: 1 check failed, 2 warnings"`.

**CLI.**

```python
@app.command("doctor")
def doctor_command(
    online: Annotated[
        bool, typer.Option("--online", help="Also check that servers are reachable.")
    ] = False,
) -> None:
    """Check the configuration, directories, database, backends, and credentials."""
```

- If settings fail to load, print one `FAIL  config         <line>` per `ConfigError`
  line on stdout and exit 2.
- Otherwise call `configure_logging(max(WARNING, settings.log_level), secrets)`, so
  builder INFO lines do not clutter the output. Then `run_doctor`, then echo each rendered
  line and `summary`, then `typer.Exit(exit_code(results))`.

## 5. Behaviour and edge cases

### 5.1 Who sends, and when

- **`build_ntfy_notifier`.**
  - Without `NTFY_URL` it returns `None` and logs at INFO "notifications are off:
    EARMARK_NTFY_URL is not set". The worker starts. `Backends.notifier` is `None`, so
    `Pipeline.notify` does nothing.
  - It makes no request at build time, not even a health check.
  - `NOTIFY_BACKEND=none` → `None`, as before.
- **`NtfyNotifier.notify`.** `compose(...)` returns `None` for a job that is not terminal:
  log at DEBUG and return. Otherwise `client.publish(notification)` and log at INFO
  `"notified job %s via ntfy at %s"` (id and host only, never the title).
- **Errors.** `publish` raises `TransientError` or `PermanentError` with host-only
  messages, so the topic never appears.
  - 200 (any body, read up to 64 KiB and ignored) → success.
  - 3xx → permanent: "ntfy at {host} answered with a redirect (HTTP {s}); redirects are
    not followed, check EARMARK_NTFY_URL". This is typically `http://` sent to an
    https-only server.
  - 401, 403 → permanent: "ntfy at {host} refused the notification: HTTP {s} (check
    EARMARK_NTFY_TOKEN){detail}".
  - 404 → permanent: "ntfy at {host}: HTTP 404 (check EARMARK_NTFY_URL){detail}".
  - 408, 429, 5xx → transient, with `retry_after_s` from `Retry-After`: "ntfy at {host}
    failed: HTTP {s}{detail}".
  - Other statuses → permanent: "ntfy at {host} rejected the notification: HTTP
    {s}{detail}".
  - Timeouts → "ntfy at {host} did not answer within 10 s". Transport and decoding errors
    → "could not reach ntfy at {host}: {type name}". `InvalidURL` → permanent "invalid
    EARMARK_NTFY_URL".
  - `{detail}` is `": " + endpoint.error_detail(body, content_type, secrets)` when it
    returns something. The secrets are the token, the topic, and the full URL, so an
    echoed topic or token is redacted. `error_detail` reads ntfy's string `error` field
    through `_json_detail` (`error` as a string is handled).
- **Never raised past the pipeline.** `Pipeline.notify` (step 05) catches the exception.
  It logs `notifier failed for job …` at WARNING and adds the job warning `notify failed:
  <message>`. The job's status never changes. That is "notification errors are logged and
  never raised" (Deviations 2).
- **No retry.** One attempt per terminal job, bounded by connect 5 s plus 10 s read, write,
  and pool timeouts. A lost notification is a warning on the job, and `GET /jobs/{id}`
  still answers. The worker finishes notifying before it honours SIGTERM (step 05), so the
  timeouts bound shutdown.
- **Lifecycle.** One `NtfyNotifier` per worker process. The httpx2 client is created on
  first use and reused. Not thread-safe; the worker notifies from one thread.

### 5.2 What a notification may contain

**`clean_field(text, max_bytes)`**, applied to every value that comes from a feed, the
job, or the user:

1. `vault.sanitize.clean_unicode` (lone surrogates → U+FFFD, NFC).
2. Remove characters of category `Cc`, `Cf` (except ZWJ U+200D and ZWNJ U+200C, which
   emoji and some scripts need), `Co`, `Cn`, `Zl`, and `Zp`. That removes CR, LF, TAB,
   ESC, bidi overrides (U+202A–U+202E, U+2066–U+2069), and other invisible format
   characters.
3. Collapse whitespace runs to one space and strip.
4. Replace `=?` with `= ?`. The server runs RFC 2047 decoding on the title even in JSON
   mode (2.1), so a hostile title of encoded-words could otherwise decode into control
   characters on the phone. This is applied to every field, for one rule.
5. `vault.sanitize.fit_bytes(text, max_bytes)`: a safe cut ending in `…`.

**Title.** `f"{TITLE_PREFIX[status]}: {name}"`, where `name` is
`clean_field(episode.title, NAME_MAX_BYTES)` if the episode is known and non-blank after
cleaning, else `UNKNOWN_EPISODE`. Then the whole title goes through
`fit_bytes(TITLE_MAX_BYTES)` (250; ntfy allows 1024). The three statuses therefore always
have different titles.

**Message.** The lines below are joined with `"\n"`, then `fit_bytes(MESSAGE_MAX_BYTES)`
(2000 < 4096, so it never becomes an attachment). Each line is built from cleaned fields
and capped at `LINE_MAX_BYTES`. The first line is never empty, so ntfy never substitutes
"triggered".

- **done**
  1. The show title, or "Apple Podcasts episode" when the episode is unknown.
  2. `Prompt: {clean_field(job.prompt, PROMPT_MAX_BYTES)}`, only with a prompt.
  3. `1 warning: {first}` or `{n} warnings: {first}`, only with warnings.
  4. `Note: {note_path}`, only when there is a note path and no click link.
- **failed**
  1. The show title, or the canonical `job.url` when the episode is unknown. `job.url`
     was built by earmark from parsed ids.
  2. `Stage: {job.stage}`, when set.
  3. `Reason: {job.error}`.
  4. `Gave up after {job.attempts} attempts.`, only when `attempts > 1`.
- **skipped**
  1. Same as failed.
  2. `Reason: {job.error}`.

**Other fields.**

- Tags `(TAGS[status],)`.
- Priority `PRIORITY_HIGH` for failed, `PRIORITY_DEFAULT` otherwise. Sent explicitly.
- `markdown` is never sent, so text is plain on every client.
- `job.error` is already short, whitespace-collapsed, and redacted (step 05). It is cleaned
  again.
- After composing, `log.redact(..., secrets)` runs over the title and message (defence in
  depth; `secrets` = `Settings.secret_values()`).

**Click.** Only for `done`, with `vault_name` set and `job.result.note_path` set:
`obsidian_url(vault_name, note_path)`. That is `None`, and the `Note:` line is used
instead, when:

- the path contains `#`, which Obsidian reads as a heading separator even when encoded
  (2.2);
- the URL is longer than `MAX_CLICK_CHARS`;
- the vault name or path is empty.

Failed and skipped notifications have no click.

**Not in a notification:**

- the topic, the URL path, the token, or any secret;
- the transcript or analysis text;
- the full warnings list (only the first warning and the count);
- the description;
- the Apple URL for done jobs.

**Header injection.** It is impossible by construction. No untrusted value goes into a
header, the request URL, or a query string; the only variable header is the validated
token.

### 5.3 `obsidian_url`

```python
vault = quote(vault_name, safe="")
path = quote(note_path, safe="")
url = f"obsidian://open?vault={vault}&file={path}"
```

Keep `.md`. Do not normalise either value. Return `None` for the cases in 5.2.

### 5.4 `split_topic_url`

Use `urlsplit(url)`.

- The path, with one trailing `/` removed, splits at its last `/` into `prefix` and
  `topic`.
- The topic must fullmatch `NTFY_TOPIC_PATTERN`.
- The root is `f"{scheme}://{netloc}{prefix}/"`. Examples:
  - `https://ntfy.sh/earmark-x1` → `("https://ntfy.sh/", "earmark-x1")`;
  - `http://ntfy.tail1234.ts.net:8080/a/` → `("http://ntfy.tail1234.ts.net:8080/", "a")`;
  - `https://host/ntfy/topic` → `("https://host/ntfy/", "topic")`.
- A URL with no topic segment raises `ValueError`. Settings validation already prevents
  it.

### 5.5 Doctor: offline checks (in this order)

Every check runs inside `try/except Exception`. An unexpected exception becomes `fail`
with `unexpected error: {TypeName}: {str(exc) collapsed}`. One failing check never stops
the others.

Offline checks make no network call, load no model, download nothing, do not migrate or
create the database, and create no directory. The only writes are the two probe files
below, which are removed at once. Each probe file has a random name, so a concurrent
earmark process is unaffected.

| Name | What it does | Statuses |
|---|---|---|
| `api token` | `require_api_token(settings)` | `fail` with `ApiTokenError`'s text. `warn` "shorter than 32 characters; generate one with python -c 'import secrets; print(secrets.token_urlsafe(32))'". `ok` "set". The value and its length are never printed. |
| `data dir` | If `DATA_DIR` exists: not a directory → `fail`. Otherwise `os.open(dir/".earmark-doctor-<16 hex>.tmp", O_WRONLY\|O_CREAT\|O_EXCL\|O_NOFOLLOW\|O_CLOEXEC, 0o600)`, write `b"ok"`, close, unlink → `ok` "{path} is writable"; an `OSError` → `fail` "{path} is not writable ({errno name})". If it is missing: find the nearest existing ancestor; with `os.access(W_OK\|X_OK)` → `ok` "{path} does not exist yet; it is created on first use", else `fail` naming the ancestor. | |
| `database` | `<DATA_DIR>/earmark.db` missing → `ok` "not created yet; `earmark serve` or `earmark worker` creates it". Present → `Database(path, busy_timeout_s=5).connect()` and read `PRAGMA user_version`, `PRAGMA journal_mode`, `PRAGMA quick_check`, and job counts. No migration, no WAL switch. | Version above `SCHEMA_VERSION` → `fail` "schema version N is newer than this earmark (M); upgrade earmark". Below → `warn` "schema version N; the next `earmark serve` or `earmark worker` migrates it to M". Journal mode not `wal` → `warn` "journal mode is X, not WAL (the next start switches it; WAL needs a local filesystem)". quick_check not `ok` → `fail` "integrity check failed: {first row, 100 chars}". `sqlite3.Error` → `fail` "{TypeName}: {message}". Else `ok` "schema N, {total} jobs (queued Q, running R, failed F)". The counts come from `SELECT status, count(*) FROM jobs GROUP BY status`, skipped if it errors. |
| `vault` | `STORE_BACKEND=vault`: `VAULT_DIR` unset, missing, or not a directory → `fail` with the same text as `build_vault_store`. Else `Vault(vault_dir.absolute(), subdir).open_existing_subdir()`. | `SymlinkRefused` (a symlink or non-directory on the subdir path) → `fail` with its message. `None` → `os.access(VAULT_DIR, W_OK\|X_OK)`: `ok` "{subdir} does not exist yet; it is created on the first store", or `fail` "EARMARK_VAULT_DIR is not writable". An open `VaultDir` → `probe_write()`: `ok` "{VAULT_DIR}/{subdir} is writable", or `fail` "{subdir} is not writable ({errno name})". `STORE_BACKEND=fake` → `warn` "fake store (for tests)". The probe never writes outside the subdir, so earmark still writes only inside `<VAULT_DIR>/<VAULT_SUBDIR>/`. |
| `obsidian link` | `OBSIDIAN_VAULT_NAME`. Omitted when `NOTIFY_BACKEND` is not `ntfy` or `NTFY_URL` is unset. | Unset → `warn` "EARMARK_OBSIDIAN_VAULT_NAME is not set; done notifications show the note path instead of a link". Set and different from `VAULT_DIR`'s last path component (compared exactly) → `warn` "Obsidian names vaults after their folder; EARMARK_VAULT_DIR is '{basename}'". Else `ok` "links open vault '{name}'". |
| `resolver` | Build through `RESOLVERS[name]` (`AppleResolver` makes no request); close it. | `apple` → `ok` "apple (iTunes lookup, then the RSS feed)". `fake` → `warn` "fake resolver (for tests)". |
| `fetch` | Build through `FETCHERS[name]`; close if it has `close`. | `http` with `ALLOW_PRIVATE_FETCH` → `warn` "EARMARK_ALLOW_PRIVATE_FETCH=true: the private-address guard is off". `http` → `ok`. `fake` → `warn`. |
| `asr` | Build through `TRANSCRIBERS[name]` (`faster_whisper`: `load_runtime` and `check_device`, no model load). | `ConfigError` → `fail` with its lines joined by `; ` (names `uv sync --extra transcribe`, or the CUDA problem). `faster_whisper` → `ok` "faster_whisper, model M, device D, compute C". `remote` → `ok` "remote server at {host}, model M, API key set\|no API key". `fake` → `warn`. |
| `asr model` | Only for `faster_whisper` after `asr` is ok: `model_cache_status(WhisperOptions.from_settings(settings))`. | `local` → `ok` "local directory". `cached` → `ok` "downloaded". `missing` → `warn` "M is not downloaded yet; the worker downloads it on the first transcription (into EARMARK_ASR_MODEL_DIR or the Hugging Face cache)". `ConfigError` → `fail`. |
| `llm` | `anthropic`: `AnthropicClient(AnthropicOptions.from_settings(settings), http_client=probes.anthropic_http_client, max_retries=0)`. The constructor raises `ConfigError([NO_CREDENTIALS])` without a credential. Keep the client for the online check; close it at the end. `openai_compat`: `OpenAICompatOptions.from_settings`. | `fail` with the `ConfigError` text. `ok` "anthropic, model M, effort E, credential from {credential_source}". `ok` "openai_compat at {host}, model M, context N tokens, API key set\|no API key". `fake` → `warn`. |
| `index` | `lancedb`: `build_lancedb_index(settings, allow_download=False)` (imports lancedb and fastembed, validates `EMBED_MODEL`, creates nothing). | `ConfigError` → `fail` (names `uv sync --extra index`, or the unknown model). `ok` "lancedb, embedding model M (D dimensions)". `none` → `ok` "search is off (EARMARK_INDEX_BACKEND=none)". `fake` → `warn`. |
| `embed model` | Only after `index` is ok: `model_cached(find_model(settings, runtime), default_model_dir(settings))`. | `True` → `ok` "downloaded". `False` → `warn` "M is not downloaded yet; the worker or `earmark reindex` downloads it; until then GET /search answers 503". `None` → `warn` "cannot tell whether M is downloaded". |
| `search index` | Only after `index` is ok: `index.check_existing()`. | `False` → `ok` "empty; filled as episodes are indexed (or by `earmark reindex`)". `True` → `ok` "built with M". `IndexMismatchError` → `fail` with its message, which names `earmark reindex`. `TransientError` → `fail` with its message. |
| `notify` | `ntfy` → `NtfyOptions.from_settings`. | `None` → `warn` "EARMARK_NTFY_URL is not set; no notifications are sent", with "; EARMARK_NTFY_TOKEN is set but unused" appended if the token is set. Else `ok` "ntfy at {host}, token set\|no token". `none` → `ok` "off". `fake` → `warn`. |

The database check opens an existing file with a normal connection and only reads. As
with any reader in WAL mode, SQLite may create `-wal` and `-shm` beside an existing
database; that is the normal state. If `earmark.db` is missing, doctor never calls
`sqlite3.connect`, which would create it.

### 5.6 Doctor: online checks (`--online`, after the offline checks)

Each online check is omitted when its backend is not selected. It is `skip` "offline check
failed" when the offline check it needs failed. Every HTTP client follows the 4.2 rules:
`trust_env=False`, no redirects, cookies cleared, response capped at 64 KiB, host-only
messages, and `httpx2.Timeout(timeout_s, connect=min(5.0, timeout_s))` with `timeout_s` =
`probes.timeout_s` (10 s). Nothing is published, nothing is downloaded, and no request
spends money.

| Name | Request | Statuses |
|---|---|---|
| `apple lookup` | `RESOLVER_BACKEND=apple`. `probes.fetcher` or `GuardedFetcher(allow_private=settings.allow_private_fetch, timeout=httpx2.Timeout(10.0, connect=5.0))`, then `.get(LOOKUP_URL, params={"id": "1", "entity": "podcast"}, max_bytes=64 * 1024, purpose="iTunes lookup", deadline_s=15.0)`. | JSON object with a `resultCount` key → `ok` "itunes.apple.com is reachable". `BlockedAddressError` → `fail` with its message plus "; if a filtering DNS resolver or the sandbox answers private addresses, see EARMARK_ALLOW_PRIVATE_FETCH". Other `EarmarkError` → `fail` with its message. A body that is not the expected JSON → `warn`. |
| `asr server` | `ASR_BACKEND=remote`: `probe_models(base_url, api_key, model)`, below. | See below. |
| `llm server` | `LLM_PROVIDER=openai_compat`: `probe_models(LLM_BASE_URL, LLM_API_KEY, LLM_MODEL)`. | See below. |
| `anthropic api` | `LLM_PROVIDER=anthropic`: `client.check_model(timeout_s=probes.timeout_s)`, one `GET /v1/models/{LLM_MODEL}`. | `active` → `ok` "{model} ({display_name}) is available". `deprecated` → `warn` "{model} is deprecated; retires {retires_at or 'at a date not yet set'}". `retired` → `fail` "{model} is retired; set EARMARK_LLM_MODEL". `EarmarkError` from `map_api_error` → `fail` with its message (401 names `EARMARK_LLM_API_KEY`; 404 names `EARMARK_LLM_MODEL`). |
| `ntfy server` | `NOTIFY_BACKEND=ntfy` with a URL: `client.health()`. | 200 with `healthy` true → `ok` "ntfy at {host} is reachable". `healthy` false or 503 → `warn` "ntfy at {host} reports itself unhealthy". 404 → `warn` "ntfy at {host} answered, but has no /v1/health (ntfy older than 2.x?)". Other status → `warn` "HTTP {s}". 3xx → `fail` (redirect message, 5.1). Transport or timeout error → `fail` "could not reach ntfy at {host}: …". |
| `ntfy token` | Only with `NTFY_TOKEN`, after `ntfy server` did not fail: `client.account()`. | 200 → `ok` "token accepted". With `stats.messages_remaining == 0` → `warn` "token accepted, but today's message quota is used up". 401 or 403 → `fail` "ntfy at {host} rejected EARMARK_NTFY_TOKEN". Other → `warn` "HTTP {s}". |

**`probe_models`** (`doctor/online.py`):

```python
def probe_models(
    base_url: str,
    api_key: SecretStr | None,
    model: str,
    *,
    setting: str,  # "EARMARK_ASR" or "EARMARK_LLM", used in messages
    transport: httpx2.BaseTransport | None,
    timeout_s: float,
    secrets: Sequence[str],
) -> CheckResult: ...
```

It sends `GET {base_url}/models`, with `Authorization: Bearer <key>` only when the key is
set.

- 200 with `data` a list of objects that have a string `id`: `ok` "{host} serves model
  {model}" when it is listed. Otherwise `warn` "{host} does not list model {model} (it
  lists: a, b, c)", with at most 5 ids, each cleaned and capped at 100 characters.
- 200 with another body → `warn` "{host} answered, but not with a model list".
- 401 or 403 → `fail` "{host} refused the request: HTTP {s} (check {setting}_API_KEY)".
- 404 → `warn` "{host} is reachable but has no /models endpoint; cannot check
  {setting}_MODEL".
- 3xx → `fail` "redirect … check {setting}_BASE_URL".
- 5xx and other 4xx → `fail` "HTTP {s}{detail}". `detail` comes from `error_detail` with
  the key redacted.
- Timeout → `fail` "{host} did not answer within 10 s". Transport error → `fail` "could
  not reach {host}: {TypeName}".

The 200 body is read up to 64 KiB. A longer list is truncated for the check only; that
gives `warn` "not a model list" if the JSON is cut off.

### 5.7 Redaction in doctor output

- No check builds a detail from a secret value. Token and key checks say "set", "not set",
  or "accepted", never the value, a prefix, or a length.
- URLs appear only as `endpoint.host_label(...)`. The ntfy topic is never printed (it is
  part of a secret).
- Server error bodies reach a detail only through `endpoint.error_detail(..., secrets)`,
  which redacts and caps at 200 characters.
- `render` runs `log.redact(detail, settings.secret_values())` over every line, after the
  checks, as a second layer. The CLI replaces non-printable characters and caps lines.
- With the logging level at WARNING or above, nothing an SDK or builder logs reaches the
  terminal at INFO. The redacting formatter covers what does.

### 5.8 Smaller rules

- **Doctor and fake backends.** `fake` backends are `warn`, never `fail`, so an all-fake
  test config passes with exit 0.
- **Doctor and unselected backends.** Doctor checks only the selected backends. With
  `ASR_BACKEND=remote` it never imports faster-whisper; with `INDEX_BACKEND=none` it never
  imports lancedb.
- **`probe_write`.** Name pattern `TEMP_NAME`, so step 12's stale-temp cleanup would remove
  a leftover. Create with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0o600`,
  through `dir_fd`. Retry the name 5 times on `FileExistsError`. Unlink through `dir_fd` in
  a `finally`.
- **Doctor and the vault lock.** Doctor takes no lock and needs none.
- **`earmark config`.** It now prints `EARMARK_NTFY_URL=**********`. `earmark doctor` shows
  only the host.
- **Worker log line.** `build_worker`'s "backends: …" log line is unchanged.
- **Light imports.** `notify/ntfy.py` and `doctor/` import httpx2, so the factory imports
  `earmark.notify.ntfy` only inside `_ntfy_notifier`, and the CLI imports `earmark.doctor`
  only inside `doctor_command`. `tests/test_factory.py::test_pipeline_modules_import_light`
  (httpx2 is on its heavy list) keeps guarding this.

## 6. Tests

All of these run in the default suite (sockets blocked) unless marked. HTTP goes through
`httpx2.MockTransport` handlers, or through the new unix-socket `NtfyStub` for real
round trips, like `LlmStub`. Fixtures are the recorded ntfy bodies from 2.1, kept as
constants in `tests/ntfy_stub.py`:

- `HEALTH_OK = b'{"healthy":true,"role":"active"}'`
- `UNAUTHORIZED = b'{"code":40101,"http":401,"error":"unauthorized","link":"https://ntfy.sh/docs/publish/#authentication"}'`
- `PUBLISHED` (the 2.1 publish response)
- `ACCOUNT_ANON` (the 2.1 anonymous account body, trimmed)
- a 429 body `{"code":42901,"http":429,"error":"limit reached: too many requests",…}`
  with `Retry-After: 5`

### 6.1 Brief: done, failed, and skipped each send a distinct title and message; failures include the reason

- `test_compose_titles_distinct_per_status`. The same job and episode in `done`, `failed`,
  and `skipped`: three different titles starting with `Note ready: `, `Failed: `, and
  `Skipped: `, and three different messages. Tags are `white_check_mark`, `x`, and
  `fast_forward`; priorities 3, 4, and 3.
- `test_compose_failed_includes_reason_stage_attempts`. The message contains `Reason:
  <job.error>`, `Stage: transcribe`, and `Gave up after 4 attempts.`.
- `test_compose_skipped_includes_reason`.
- `test_compose_done_has_no_reason_and_counts_warnings`. `2 warnings: <first>`, and a
  `Prompt:` line only when `job.prompt` is set.
- `test_compose_unknown_episode`. Failed at resolve with `episode=None`: the title is
  `Failed: podcast episode` and the message carries `job.url`.
- `test_compose_non_terminal_is_none`. Queued and running → `None`.
- `test_notifier_sends_one_request_per_job` (MockTransport). One POST to the root URL. The
  JSON body has `topic`, `title`, `message`, `tags`, and `priority`, and no `markdown`.

### 6.2 Brief: with `OBSIDIAN_VAULT_NAME` set, the notification carries an `obsidian://` link to the note

- `test_obsidian_url_golden`. The 2.2 golden values, exactly.
- `test_obsidian_url_encodes_reserved`. `/ & ? = + % ! ' ( ) *`, space, and non-ASCII are
  encoded. NFD input stays NFD (`e%CC%81`).
- `test_obsidian_url_refuses_hash_and_overlong`. A path with `#` → `None`; a URL over 2000
  characters → `None`.
- `test_compose_done_click_with_vault_name`. `click` equals
  `obsidian_url(name, job.result.note_path)`, and there is no `Note:` line.
- `test_compose_done_without_vault_name_has_note_line`. No `click`, and `Note: <path>` in
  the message.
- `test_compose_failed_and_skipped_have_no_click`.
- `test_notifier_click_in_request_body` (MockTransport). The JSON `click` is the URL; no
  header carries it.

### 6.3 Brief: the ntfy token is sent only when configured

- `test_publish_without_token_sends_no_authorization`. The request has no `Authorization`
  header (also checked case-insensitively).
- `test_publish_with_token_sends_bearer`. `Authorization: Bearer tk_sentinel…` exactly.
- `test_health_never_sends_token`. `account` sends it; `health` does not.
- `test_ntfy_stub_round_trip` (unix socket). A real HTTP/1.1 POST reaches `NtfyStub` with
  `Content-Type: application/json` and the expected JSON. Proves framing and headers end
  to end.

### 6.4 Brief: notification errors are logged and never raised

- `test_publish_status_mapping` (parametrised). 301 → `PermanentError` naming
  `EARMARK_NTFY_URL`. 401 and 403 → `PermanentError` naming `EARMARK_NTFY_TOKEN`. 404 →
  `PermanentError`. 413 → `PermanentError`. 429 with `Retry-After: 5` → `TransientError`
  with `retry_after_s == 5`. 500 and 507 → `TransientError`. `httpx2.ConnectError` and
  `ReadTimeout` from the transport → `TransientError`. Every message names the host, and
  none contains the topic or the token (both sentinels).
- `test_publish_error_detail_redacted`. A 403 body echoing the token and the topic in
  `error` → message has `**********` and neither sentinel.
- `test_publish_no_redirect_followed`. A 302 to another host → one request only.
- `test_pipeline_ntfy_failure_is_warning`. `make_env(tmp_path, backends=replace(fakes.backends(),
  notifier=NtfyNotifier(client over a 500 MockTransport)))`.
  - Enqueue and run: no exception escapes `worker.run_once()`.
  - The job is `done` with the warning `notify failed: ntfy at ntfy.test failed: HTTP
    500…`.
  - `caplog` has a WARNING `notifier failed for job`.
  - No sentinel in warning or log text.
  - Repeat with a transport that raises `RuntimeError` (unexpected) and for a job that
    ends `failed`.
- `test_pipeline_ntfy_success_no_warning`. Same wiring with a 200 transport: no warning,
  one request.

### 6.5 Brief: doctor checks the token, data dir and vault writability, the database, the selected backends' extras, and credentials; `--online` adds reachability; exits non-zero on failure; redacts secrets

In `tests/test_doctor.py`, with `run_doctor(settings, probes=...)` and settings built with
`Settings(_env_file=None, ...)`:

- **Token.**
  - `test_doctor_token_missing_fails`;
  - `test_doctor_token_short_warns`;
  - `test_doctor_token_non_ascii_fails`;
  - `test_doctor_token_ok_prints_no_value`.
- **Data dir.**
  - `test_doctor_data_dir_missing_ok_creates_nothing`. The directory does not exist after
    the run.
  - `test_doctor_data_dir_is_file_fails`.
  - `test_doctor_data_dir_readonly_fails`. `chmod 0o500`; `pytest.skip` when
    `os.geteuid() == 0`.
  - `test_doctor_data_dir_probe_leaves_nothing`. The directory listing is identical
    before and after.
- **Vault.**
  - `test_doctor_vault_unset_fails`;
  - `test_doctor_vault_missing_fails`;
  - `test_doctor_vault_subdir_missing_ok`. Nothing is created in `VAULT_DIR`.
  - `test_doctor_vault_subdir_symlink_fails`;
  - `test_doctor_vault_readonly_fails` (skip as root);
  - `test_doctor_vault_probe_leaves_nothing`;
  - `test_doctor_fake_store_warns`.
- **Database.**
  - `test_doctor_database_missing_not_created`. No `earmark.db` afterwards.
  - `test_doctor_database_ok_counts_jobs`. Uses `open_database` and two enqueued jobs.
  - `test_doctor_database_newer_fails`. `PRAGMA user_version = SCHEMA_VERSION + 1`.
  - `test_doctor_database_older_warns`. A database at version 1, migrated only that far.
  - `test_doctor_database_corrupt_fails`. 4 KiB of random bytes as `earmark.db`.
  - `test_doctor_database_not_migrated`. An older database keeps its `user_version`
    after the run.
- **Extras of the selected backends.**
  - `test_doctor_whisper_extra_missing_fails`. `sys.modules["faster_whisper"] = None`; the
    detail contains `uv sync --extra transcribe`.
  - `test_doctor_whisper_ok_with_stub_runtime`. Reuses the `test_faster_whisper` stubs,
    plus a stub `faster_whisper.utils` module whose `download_model` returns, raises
    `FileNotFoundError`, or raises `ValueError`. Gives `asr model` `ok`, `warn`, and
    `fail`. Asserts `download_model` was called with `local_files_only=True` and that no
    `WhisperModel` was constructed.
  - `test_doctor_whisper_local_dir`. A directory with and without `model.bin`.
  - `test_doctor_cuda_unavailable_fails`. A stub with `get_cuda_device_count() == 0` and
    `ASR_DEVICE=cuda`.
  - `test_doctor_index_extra_missing_fails`. `stub_extra_missing(monkeypatch, "lancedb")`;
    the detail contains `uv sync --extra index`. No `embed model` or `search index` rows.
  - `test_doctor_unselected_backends_not_imported`. `sys.modules["faster_whisper"] = None`
    and lancedb stubbed missing, with `ASR_BACKEND=remote` and `INDEX_BACKEND=none`: no
    fail.
- **Credentials.**
  - `test_doctor_anthropic_no_credential_fails`. The detail is `NO_CREDENTIALS`.
  - `test_doctor_anthropic_key_from_setting`. Says `credential from EARMARK_LLM_API_KEY`.
  - `test_doctor_anthropic_key_from_env`. `ANTHROPIC_API_KEY` set by monkeypatch.
  - `test_doctor_openai_compat_missing_base_url_fails`;
  - `test_doctor_remote_asr_missing_base_url_fails`;
  - `test_doctor_ntfy_unset_warns`;
  - `test_doctor_ntfy_token_without_url_warns`;
  - `test_doctor_ntfy_ok_shows_host_only`. No topic in the detail.
  - `test_doctor_obsidian_name_mismatch_warns`.
- **Online.** Each test uses a MockTransport handler routed by host and path.
  - `test_online_ntfy_health_ok`;
  - `test_online_ntfy_unreachable_fails`. The transport raises `ConnectError`.
  - `test_online_ntfy_redirect_fails`;
  - `test_online_ntfy_no_health_endpoint_warns`. 404.
  - `test_online_ntfy_token_rejected_fails`. `UNAUTHORIZED`.
  - `test_online_ntfy_token_quota_warns`;
  - `test_online_ntfy_never_publishes`. Every request is a GET, and none goes to the root
    path `/`.
  - `test_online_models_listed_ok`, `test_online_models_missing_model_warns`,
    `test_online_models_404_warns`, `test_online_models_401_fails` (for both
    `ASR_BACKEND=remote` and `openai_compat`). The key is sent only when set.
  - `test_online_anthropic_active_ok`. `AnthropicStub` with a new `GET /v1/models/{id}`
    route. Asserts exactly one request, `GET /v1/models/claude-opus-5-5`, and no POST.
  - `test_online_anthropic_deprecated_warns`, `test_online_anthropic_retired_fails`,
    `test_online_anthropic_404_fails`, `test_online_anthropic_401_fails`.
  - `test_online_anthropic_no_retry_on_500`. Exactly one request (`max_retries=0`).
  - `test_online_apple_ok`. `GuardedFetcher` with a MockTransport answering the 2.5 body.
  - `test_online_apple_blocked_fails`. `GuardedFetcher(resolve=StaticResolver({"itunes.apple.com":
    ["10.0.0.1"]}), network=ScriptedNetwork())`; the detail names
    `EARMARK_ALLOW_PRIVATE_FETCH`.
  - `test_online_timeouts_are_10s`. Read `request.extensions["timeout"]` in the ntfy, ASR,
    and LLM handlers: connect 5.0, read 10.0. For Anthropic, assert `timeout=10.0` and
    `max_retries=0` reached the SDK: read the stub request's `timeout` extension if the SDK
    sets one, otherwise spy on `with_options`.
  - `test_online_skipped_after_offline_failure`. A missing LLM base URL gives `llm server`
    the status `skip`.
- **Offline makes no requests.**
  - `test_offline_makes_no_requests`. `Probes` whose transport and `http_client` raise
    `AssertionError` on any request, with every backend real-but-unreachable (remote ASR,
    openai_compat, ntfy, anthropic with a key). `run_doctor(online=False)` → no
    `AssertionError` was hit (recorded in a list).
  - `test_offline_creates_nothing`. `tmp_path` tree identical before and after, for a
    config whose data dir and vault exist.
- **Exit codes and redaction (CLI, `tests/test_cli.py`).**
  - `test_doctor_all_fake_exit_0`. Every backend `fake`, data and vault in `tmp_path`,
    token set → exit 0, and the last line starts with `doctor: all checks passed`.
  - `test_doctor_failure_exit_1`. Token missing.
  - `test_doctor_bad_settings_exit_2_no_echo`. `EARMARK_PORT=sentinel-port-x` → exit 2,
    a `FAIL  config` line, and no sentinel.
  - `test_doctor_never_prints_secrets`. The secrets are sentinels:
    - `API_TOKEN`, `ASR_API_KEY`, `LLM_API_KEY`, and `NTFY_TOKEN`;
    - `NTFY_URL=https://ntfy.test/sentinel-topic-91x`.

    Run offline through the CLI, and online through `run_doctor` plus `render` with
    handlers whose error bodies echo the received `Authorization` header and the topic. No
    sentinel appears in stdout or stderr.
  - `test_doctor_online_cli_unreachable`. `doctor --online` with ntfy configured: the
    blocked socket gives `FAIL ntfy server` and exit 1, with no traceback in the output.

### 6.6 Criteria added by this plan

- **Default config builds.**
  - `tests/test_factory.py::test_default_settings_list_all_unbuilt` becomes
    `test_every_default_backend_is_built`: `expected == set()`.
  - `test_unbuilt_backend_names_setting` becomes
    `test_unbuilt_backend_name_names_setting`. It monkeypatches `factory.NOTIFIERS` to a
    dict without `ntfy` and checks the `ConfigError` wording, so the guarantee is still
    tested.
  - `test_ntfy_without_url_builds_none` and `test_ntfy_with_url_builds_notifier` make no
    request.
- **Settings.**
  - `test_ntfy_url_accepted` (parametrised):
    - `https://ntfy.sh/topic`;
    - `http://ntfy.tail1234.ts.net:8080/earmark`;
    - `https://host/prefix/topic`;
    - a trailing `/`.
  - `test_ntfy_url_refused` (parametrised; none echoes the value):
    - no path;
    - a topic containing `.`;
    - a 65-character topic;
    - credentials (message names `EARMARK_NTFY_TOKEN`);
    - a query;
    - a fragment;
    - `ftp:`.
  - `test_ntfy_token_validated`;
  - `test_obsidian_vault_name_validated`;
  - `test_ntfy_url_is_secretstr`. `Settings.model_fields["ntfy_url"].annotation ==
    SecretStr | None`, and the URL is in `secret_values()`. The existing
    `test_secret_fields_are_secretstr` selects by name suffix, so it is unchanged.
  - `test_config_redacts_ntfy_url` (replaces `test_config_redacts_url_password`).
    `EARMARK_NTFY_URL=https://ntfy.example/sentinel-topic-77` prints
    `EARMARK_NTFY_URL=**********` and no sentinel. A URL with credentials exits 2 without
    echoing the password.
- **Untrusted text.**
  - `test_clean_field_strips_controls_and_bidi`. CR, LF, TAB, ESC, U+202E, U+2066, U+2028,
    and U+0085 removed; ZWJ kept.
  - `test_clean_field_defuses_encoded_words`. `=?UTF-8?B?...?=` contains no `=?`
    afterwards.
  - `test_compose_title_and_message_caps`. A 10 000-character title and error give a
    title of at most 250 UTF-8 bytes and a message of at most 2000 bytes, ending in `…`;
    multi-byte characters are not split.
  - `test_no_untrusted_text_in_headers`. Title `"x\r\nX-Evil: 1"`: request headers are
    exactly the fixed set, and the JSON title is `"x X-Evil: 1"`.
  - `test_compose_redacts_secrets`. A secret inside `job.error` is redacted.
- **URL split.** `test_split_topic_url` with the 5.4 examples.
- **Small additions.**
  - `test_probe_write_removes_file`;
  - `test_probe_write_refuses_symlinked_dir` (`tests/test_vault_fs.py`);
  - `test_model_cache_status_*` (`tests/test_faster_whisper.py`, with stubs);
  - `test_find_model_and_model_cached` (`tests/test_index_embed.py`, a stub runtime and a
    stub `huggingface_hub` module);
  - `test_check_model_*`, `test_credential_source_*` (`tests/test_llm_anthropic.py`);
  - `test_check_existing_*` (`tests/test_lancedb_index.py`, `index` marker, beside the
    existing table tests).
- **`index` marker** (`tests/test_doctor_index.py`, real lancedb and fastembed listing, no
  model download; sockets stay blocked):
  - `test_doctor_index_ok_empty`. `index` ok, `embed model` warn (cache empty),
    `search index` ok "empty".
  - `test_doctor_index_mismatch_fails`. A table created by `LanceIndex` with
    `FakeEmbedder` of another name or dimension (use the `tests/index_helpers` builders);
    the detail contains `earmark reindex`.
- **`network` marker** (`tests/test_ntfy_live.py`): `test_ntfy_publish_round_trip`.
  - It publishes one `done` notification, with a title containing CR/LF and `=?UTF-8?B?`
    and an `obsidian://` click.
  - The target is `NTFY_TEST_URL` (with optional `NTFY_TEST_TOKEN`), else
    `https://ntfy.sh/earmark-test-<uuid4 hex>`, a fresh unsubscribed topic.
  - It then polls `GET <topic>/json?poll=1&since=all` with the same client settings, and
    asserts the title, message, tags, priority, and click as composed.
  - It runs in the weekly `network` job (`pytest -m network`); no workflow change is
    needed.

## 7. Verification

```bash
make check
uv run pytest tests/test_ntfy.py tests/test_doctor.py tests/test_factory.py tests/test_config.py -v
uv run --extra index pytest -m index tests/test_doctor_index.py tests/test_lancedb_index.py -v
uv run pytest -m network tests/test_ntfy_live.py -v -rs    # publishes to a random ntfy.sh topic
```

**Live smoke**, from a scratch directory:

```bash
export EARMARK_API_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export EARMARK_DATA_DIR=$PWD/data EARMARK_VAULT_DIR=$PWD/vault
mkdir -p vault
for b in RESOLVER FETCH ASR STORE INDEX NOTIFY; do export EARMARK_${b}_BACKEND=fake; done
export EARMARK_LLM_PROVIDER=fake
uv run earmark doctor; echo "exit $?"                    # 0, fakes are warnings
ls -A data vault                                         # nothing created
uv run earmark config | grep NTFY                        # EARMARK_NTFY_URL= (unset)

export EARMARK_NOTIFY_BACKEND=ntfy EARMARK_NTFY_URL=https://ntfy.sh/earmark-smoke-$RANDOM$RANDOM
export EARMARK_OBSIDIAN_VAULT_NAME=vault
uv run earmark config | grep NTFY_URL                    # EARMARK_NTFY_URL=**********
uv run earmark doctor --online; echo "exit $?"
# ntfy server ok "ntfy at ntfy.sh is reachable"; no topic printed. No apple lookup row:
# the resolver is fake.

export EARMARK_RESOLVER_BACKEND=apple
uv run earmark doctor --online; echo "exit $?"           # apple lookup FAIL (sandbox DNS), exit 1
EARMARK_ALLOW_PRIVATE_FETCH=true uv run earmark doctor --online; echo "exit $?"   # apple lookup ok
unset EARMARK_RESOLVER_BACKEND; export EARMARK_RESOLVER_BACKEND=fake

# A notification end to end with fakes: serve, ingest, worker, then poll the topic.
uv run earmark serve &   # then curl POST /ingest as in step 05's smoke
uv run earmark worker --once
curl -s "$EARMARK_NTFY_URL/json?poll=1&since=all"        # one message, "Note ready: …",
                                                         # click obsidian://open?vault=vault&file=…
```

**Also check.**

- `EARMARK_LLM_PROVIDER=anthropic` with no key gives `FAIL llm` and exit 1. With
  `--online`, `anthropic api` is `skip`. This cannot be checked live here, because there is
  no key; the mock-transport tests cover it.
- `EARMARK_NTFY_URL=https://u:p@ntfy.sh/t uv run earmark config` exits 2 and does not print
  `p`.
- Read `tests/test_doctor.py::test_offline_makes_no_requests` and
  `test_online_anthropic_active_ok`, and confirm that they would fail if doctor sent a
  request offline or a POST to Anthropic.

**Cannot be verified here.**

- The ntfy iOS app opening the `obsidian://` link on a phone.
- A self-hosted ntfy on the tailnet.
- The GPU device check against real CUDA.
- `doctor --online` against a real Anthropic key, ASR server, or LLM server.

## 8. Deviations

1. **`EARMARK_NTFY_URL` is now a secret and validated.** It was a plain string. Now it is
   a `SecretStr`, redacted in `earmark config`, logs, job text, and doctor. It must be an
   http(s) topic URL whose last segment is a valid ntfy topic; credentials, a query, and a
   fragment are refused. Reason: on public ntfy.sh the topic is the only access control,
   and JSON publishing needs the root and the topic apart. This changes step 01's
   `earmark config` output for this one variable, and two existing tests (6.6).
2. **"Errors are logged and never raised".** The ntfy client raises `TransientError` or
   `PermanentError` with host-only text. `Pipeline.notify` (step 05) is the single place
   that logs and swallows, and it records `notify failed: …` on the job. Swallowing inside
   the notifier would lose that warning, which step 05's acceptance requires. Nothing
   raises out of the worker, and the tests in 6.4 prove it through the pipeline.
3. **The link is only on done notifications.** Failed and skipped jobs have no note to open.
   The link is also omitted when the note path contains `#` (Obsidian cannot open it, 2.2)
   or the URL exceeds 2000 characters; the message then shows the note path.
4. **Exit code 2 for unloadable settings.** The brief says "non-zero on any failure"; 2
   matches the other commands' configuration-error code, and check failures exit 1.
5. **Doctor never publishes a test notification.** The brief asks for reachability, and a
   test push would ping the user's phone. A token is verified with `/v1/account` instead.
   An `earmark notify --test` command is left out (section 9).
6. **Small public additions to earlier modules.** These let doctor check without loading
   or downloading:
   - `VaultDir.probe_write`;
   - `LanceIndex.check_existing`;
   - embed `ModelInfo.hf_repo` and `model_file`, `find_model`, `model_cached`;
   - `faster_whisper.model_cache_status`;
   - `AnthropicClient.credential_source` and `check_model`.

   Existing behaviour is unchanged.
7. **The live ntfy test publishes to public ntfy.sh** by default: one message per run, to
   a fresh random topic with no subscriber, inside the opt-in `network` job.
   `NTFY_TEST_URL` points it elsewhere.
8. **`docs/ARCHITECTURE.md` updated in this planning change:**
   - the Notification decision;
   - the module layout (`notify/`, `doctor/`);
   - new "Notifying" and "Doctor" subsections;
   - the configuration rows for `OBSIDIAN_VAULT_NAME`, `NOTIFY_BACKEND`, `NTFY_URL`, and
     `NTFY_TOKEN`;
   - Security (operator endpoints, notification text, secrets);
   - Testing (`NtfyStub`, the live ntfy test, doctor).

## 9. Out of scope

- Deployment notes for ntfy (step 16):
  - `upstream-base-url: "https://ntfy.sh"` on a self-hosted server for iOS push;
  - reserving the topic or using an access token on ntfy.sh;
  - Tailscale reachability for the phone;
  - the README and Shortcut docs mentioning `earmark doctor`.
- A test-notification command, notification retries or queueing, and notification
  actions (buttons).
- Notifications for reanalyze jobs (step 17 reuses the same notifier unchanged).
- Doctor discovery of a local LLM's context window (`/v1/models` metadata, `/props`,
  `/api/ps`; step 11 section 9) and exact token counting.
- Doctor checks of Hugging Face reachability, of `aliases.yaml`, of stale running jobs, and
  of the filesystem type under `DATA_DIR`. Also a `--json` output mode.
- A CI workflow change: the new tests run in the existing `ci.yml`, `index.yml`, and
  weekly `network` jobs.
