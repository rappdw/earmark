# Step 09: Transcription: remote ASR backend

## 1. Outcome

`EARMARK_ASR_BACKEND=remote` works with no optional extra installed. The worker's
transcribe stage uploads the episode audio, as a streamed `multipart/form-data` POST, to
an operator-run OpenAI-compatible `POST {EARMARK_ASR_BASE_URL}/audio/transcriptions`
endpoint with `response_format=verbose_json`. It maps the returned segments to a
`Transcript` with monotonic timestamps. If the server returns text but no segment
timestamps, the transcript is one segment and the job gets a warning. The stage reuses
step 08's `CachedTranscriber`, so caching, the atomic `transcript.txt` and
`transcript.json`, and audio deletion behave exactly as for `faster_whisper`.

The remote client is a separate, small httpx2 client. It is not `fetch.GuardedFetcher`,
and it has no private-address guard, because its only URL comes from operator
configuration and never from third-party data (5.1). It never follows redirects, ignores
proxy environment variables, keeps no cookies, caps the response size, and sends
`EARMARK_ASR_API_KEY` as a bearer header only when set. The key never reaches logs, job
errors, or `repr`.

`earmark transcribe <file>` uses the remote engine when `EARMARK_ASR_BACKEND=remote`.
`docs/asr.md` tells the operator which server to run, on the DGX Spark and elsewhere.

**On the DGX Spark, the recommended server is vLLM serving Whisper**
(`openai/whisper-large-v3` or `-turbo`) from NVIDIA's arm64 CUDA 13 vLLM container. It
gives `verbose_json` segments and 30 s chunking with absolute offsets. whisper.cpp's
`whisper-server` built for sm_121 is the lighter alternative. LocalAI's CUDA 13 arm64
image, with its `whisper` or `parakeet-cpp` backend, is the packaged alternative and the
practical way to run Parakeet with timestamps. NVIDIA's Parakeet NIM HTTP API is not
suitable: it returns no timestamps and accepts no MP3 (2.2). None of these servers was
run here, because there is no GPU in this sandbox.

## 2. Findings

Checked on 2026-11-20. Package probes ran as scratch pytest files outside the repository
with `uv run pytest -c pyproject.toml --rootdir .`, so `--disable-socket
--allow-unix-socket` and `filterwarnings = error` applied. Upstream source was read from
the default branches on GitHub, downloaded into the scratchpad. Those files and the probe
files have been deleted.

### 2.1 httpx2 2.16.2: multipart and streaming upload (installed package)

- `Client.post(url, data=..., files=...)` goes through `httpx2/_content.py:encode_request`:
  with `files`, it builds a `MultipartStream(data, files, boundary)` (in
  `httpx2/_multipart.py`).
  - `data` is a `Mapping[str, Any]`; a list or tuple value becomes repeated fields.
  - A `files` value is `(filename, fileobj, content_type)`, with
    `FileContent = IO[bytes] | bytes | str`.
  - `FileField.render_data` seeks the file to 0, then reads it in `CHUNK_SIZE = 64 *
    1024` chunks. So a request that is re-sent re-reads the file from the start.
  - `get_content_length()` uses `peek_filelike_length` (`os.fstat(fileno).st_size`), so
    a real file gives a `Content-Length` header, not chunked encoding.
  - With `content_type=None`, httpx2 guesses the type with `mimetypes`, which reads the
    host's `/etc/mime.types` and so varies between machines. Pass the content type
    explicitly.
  - The filename is HTML5-escaped in `Content-Disposition`, and header values with
    control characters raise `ValueError`.
- **Probe 1: the request body streams.** A recording `httpx2.BaseTransport` iterated
  `request.stream` for a 300 KiB random file plus three form fields. It saw a
  `MultipartStream` and `content-length: 307679` with no `transfer-encoding`. There were
  21 chunks, none over 65 536 bytes, and they summed to the `Content-Length`. The file
  was never read in one piece.
- **Probe 2: a real server works with sockets blocked.** A stdlib
  `socketserver.UnixStreamServer` with `http.server.BaseHTTPRequestHandler`, in a thread
  and on a unix socket under the blocked suite, received the upload through
  `httpx2.Client(transport=httpx2.HTTPTransport(uds=path))`.
  - It saw path `/v1/audio/transcriptions`, the `Host` from the URL
    (`asr.internal:8000`), and `Authorization: Bearer k`.
  - It parsed the parts `model`, `response_format`, and `file` (`audio.mp3`,
    `audio/mpeg`, 204 800 bytes) with
    `email.parser.BytesParser(policy=email.policy.HTTP)`.
  - It answered with JSON that `response.json()` read.
  - Two handler overrides are needed: `address_string()` (an AF_UNIX `client_address` is
    `''`) and `log_message`.
  - `socket.socket(AF_INET)` in the same run raised `SocketBlockedError`.
- **Probe 3: a signal interrupts the wait.** A `SIGALRM` handler raising a
  `BaseException` while the client waited for a slow unix-socket server (the handler
  slept 3 s) interrupted `client.post` after 0.30 s. So the worker's `Shutdown` reaches
  a remote transcription that is waiting on the server. In tests, the stub handler must
  wait on a `threading.Event` (set at teardown) rather than `time.sleep`, and must
  swallow `BrokenPipeError`/`ConnectionResetError`, so no thread error turns into a
  warning, which `filterwarnings = error` would fail.
- `MockTransport.handle_request` calls `request.read()` (buffers the body) before the
  handler. That is fine for content assertions. Streaming is proved with the recording
  transport (probe 1).
- `httpx2.HTTPTransport(verify=True, trust_env=True, uds=None, retries=0, ...)`:
  - its own `trust_env` makes the SSL context honour `SSL_CERT_FILE`/`SSL_CERT_DIR`;
  - `Client(trust_env=False)` with an explicit transport ignores proxy variables (step
    06, 2.3);
  - `follow_redirects` defaults to `False`.
- The exception tree is as recorded in step 06, 2.3: `ReadTimeout`, `WriteTimeout`,
  `ConnectTimeout`, and `PoolTimeout` are under `TimeoutException`, which is under
  `TransportError`. `DecodingError` is a `RequestError` but not a `TransportError`.

### 2.2 Servers with `/v1/audio/transcriptions` and segment timestamps

| Server | `verbose_json` segments | aarch64 + CUDA (DGX Spark) | Notes |
|---|---|---|---|
| **vLLM** (v0.34.0, 2026-11-16) with Whisper | **Yes**, Whisper only | **Yes**, NVIDIA's arm64 CUDA 13 container `nvcr.io/nvidia/vllm:<YY.MM>-py3` (playbook at build.nvidia.com/spark/vllm; pick the current tag from NGC) | See the vLLM notes below. |
| **whisper.cpp `whisper-server`** | **Yes** | **Yes**, built from source (no prebuilt arm64+CUDA binaries) | See the whisper.cpp notes below. |
| **LocalAI** (v5.0.0, 2026-11-18) | **Yes**, seconds | **Yes**, image `quay.io/go-skynet/local-ai:latest-nvidia-l4t-arm64-cuda-13`; backends `cuda13-nvidia-l4t-arm64-whisper` and `cuda13-nvidia-l4t-arm64-parakeet-cpp` in `backend/index.yaml` | See the LocalAI notes below. |
| **speaches** (v0.9.0-rc.3, 2025-12-27) | Yes (faster-whisper) | **No**: issue speaches-ai/speaches#620 is open; the arm64 `latest-cuda` image falls back to a CPU-only CTranslate2 on GB10 (as step 08 found for the wheel) | Fine on x86_64 GPUs. It also has a Parakeet executor (`onnx_asr`, `with_timestamps()`), but there is no GPU path on the Spark to rely on. |
| **NVIDIA ASR NIM, Parakeet** (1.1B CTC en-US, 1.1B RNNT multilingual: the Parakeet models the support matrix lists for DGX Spark) | **No** | Yes | See the NIM notes below. |
| **NVIDIA Nemotron Transcribe NIM** (`nvcr.io/nim/nvidia/nemotron-transcribe:1.0.0`, multi-arch) | Yes (`verbose_json`, `timestamp_granularities[]`) | Yes (`concurrency=low` only) | See the NIM notes below. |
| **martinb78/parakeet-tdt-v3-spark** (community, NeMo on `nvcr.io/nvidia/pytorch:25.11-py3`) | Accepts `verbose_json`, but **returns no segments** | Yes | Earmark would produce a one-segment transcript with a warning. |

Notes per server:

- **vLLM.**
  - In `serving.py`, `verbose_json` is refused unless the model class sets
    `supports_segment_timestamp`, and only `whisper.py` does. So Canary, Voxtral, and
    Parakeet-family models in vLLM return 400 for `verbose_json`.
  - Audio over `max_audio_clip_s = 30` is split with a 1 s overlap
    (`config/speech_to_text.py`). Segment times get `start_time = chunk offset`, so they
    are absolute, and segments come back in chunk order. The overlap can make
    neighbouring segments overlap slightly; 5.4 clamps that.
  - Defaults that must be raised for podcasts (`envs.py`):
    - `VLLM_MAX_AUDIO_CLIP_FILESIZE_MB=25`;
    - `VLLM_MAX_AUDIO_DECODE_DURATION_S=600`;
    - `VLLM_MAX_AUDIO_DECODE_BYTES=268435456` (float32 PCM, about 70 min at 16 kHz).
  - An oversized upload is rejected with a validation error (400) by
    `read_upload_with_limit`.
  - Decoding uses soundfile, with an ffmpeg fallback for MP4/M4A.
  - It accepts `timestamp_granularities[]` (`transcription/protocol.py`) and has bearer
    auth middleware (`serve/middleware/authenticate.py`, `--api-key`).
  - Known container caveats from the NVIDIA forums: 26.04 needs a newer driver, and
    26.07 has a tool-calling bug (irrelevant here).
- **whisper.cpp `whisper-server`.**
  - `--inference-path` (default `/inference`) sets the route, so pass
    `--inference-path /v1/audio/transcriptions`.
  - `verbose_json` returns `task`, `language` (a code), `duration`, `text`, and
    `segments` with `start` and `end` in seconds (`t0 * 0.01`).
  - Built-in decoding (miniaudio) handles WAV, MP3, and FLAC. `--convert` needs ffmpeg
    in the image for M4A/AAC.
  - It has no API key option.
  - The default read and write timeouts are 600 s.
  - Its README says to run it sandboxed and without admin rights.
  - NVIDIA forum recipe (2026-05-30) for the Spark: build in
    `docker.io/nvidia/cuda:13.0.3-devel-ubuntu24.04` with `-DGGML_CUDA=ON
    -DCMAKE_CUDA_ARCHITECTURES="120;121"` (`120` alone fails on GB10), link
    `/usr/local/cuda/lib64/stubs` with `-lcuda`, and build only the `whisper-server`
    target. One later reply says the recipe broke for them.
- **LocalAI.**
  - The `verbose_json` path converts segments to `TranscriptionSegmentSeconds`
    (`core/http/endpoints/openai/transcription.go`), so times are float seconds. The
    `time.Duration` nanosecond structs are internal.
  - `parakeet-cpp` builds segments from word timestamps the way NeMo does. Without
    words it returns one segment with no times (`start = end = 0`), which 5.4 treats as
    "no timestamps".
  - The `model` field names a model configured in LocalAI.
- **NVIDIA NIMs.**
  - The Parakeet NIM's HTTP API (`docs.nvidia.com/nim/speech/latest/reference/api-references/asr/http-asr.html`)
    allows `response_format` `json` or `text` only (anything else is 400), returns
    `{"text", "usage"}`, accepts WAV, Opus, and FLAC only, and needs `language` or
    `model`. Timestamps are gRPC-only. So it is **not suitable**: no MP3, and no
    segments.
  - Nemotron Transcribe accepts WAV, FLAC, and Ogg Opus only (**no MP3/M4A**), caps a
    request at 3600 s of audio and a 256 MiB body, rejects unknown fields with 400, needs
    an NGC key, and needs a 26 to 70 GB model cache. It is not recommended for podcasts
    without a conversion step; earmark does not transcode.

What was **not** verified: running any of these servers (no GPU or Docker here), their
real-time factors on GB10, and whether the NVIDIA vLLM container includes ffmpeg (which
matters for M4A).

### 2.3 Response shapes earmark must accept

- OpenAI and vLLM: `{"text", "language", "duration", "segments": [{"id", "seek",
  "start", "end", "text", "tokens", "temperature", "avg_logprob", "compression_ratio",
  "no_speech_prob"}], "usage"?}`. OpenAI's `language` is a full name ("english"); vLLM
  and whisper.cpp return a code ("en").
- whisper.cpp also adds `detected_language`, `language_probabilities`, and per-segment
  `words`. LocalAI adds top-level `words`. Extra keys must be ignored.
- A text-only answer is `{"text": "..."}`, sometimes with `duration` or `usage`.
- Error bodies: `{"error": {"message": ...}}` (OpenAI, vLLM), `{"error": "..."}`
  (whisper.cpp), `{"detail": ...}` (FastAPI servers such as speaches),
  `{"message": ...}`, or plain text.

### 2.4 Design decision: the remote client and `fetch.py`

CLAUDE.md says "all third-party fetches go through `earmark/fetch.py`". The ASR endpoint
is not third-party. Its URL is `EARMARK_ASR_BASE_URL`, set by the operator, and nothing
from a feed, an Apple response, or a transcript can change where the client connects.
The architecture already treats operator-chosen endpoints this way: Hugging Face model
downloads (step 08) and the Anthropic SDK (step 10) do not use `fetch.py` either.

The private-address guard exists to stop *untrusted* URLs from reaching the LAN. The ASR
server is expected to *be* on the LAN or tailnet (100.64.0.0/10, which the guard
refuses). Routing it through `GuardedFetcher` would force operators to set
`EARMARK_ALLOW_PRIVATE_FETCH=true`, which turns the SSRF guard off for every feed and
enclosure. That is the wrong trade.

So:

- `transcribe/remote.py` owns its own httpx2 client.
- `ALLOW_PRIVATE_FETCH` does not affect it, and it does not need that setting.
- It reuses only stateless helpers from `fetch.py`: `USER_AGENT`, `MIB`,
  `AUDIO_EXTENSIONS`, `parse_retry_after`.

The rules from `fetch.py` that still apply to an operator endpoint are kept:

- http(s) only, checked when settings load;
- no credentials in the URL;
- no redirects;
- no environment proxies;
- no cookie jar reuse;
- a response size cap;
- host-only messages;
- quiet `httpx2`/`httpcore2` loggers (already set by `log.configure_logging`).

The response is still untrusted *text*: it is validated against a schema, capped, and
never used to build a path. The architecture's Security section is updated (section 8).

### 2.5 Lint and formatting

`make lint` runs `ruff format --check .`, which formats Python blocks in `docs/*.md`. This
plan was checked with `uv run ruff format --check docs/`.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/config.py` | Validators for `asr_base_url` and `asr_api_key`. New field `asr_timeout_seconds`. Descriptions updated (4.1). |
| `.env.example` | Add `# EARMARK_ASR_TIMEOUT_SECONDS=10800` after `# EARMARK_ASR_MAX_UPLOAD_MB=`, and a comment line noting that `EARMARK_ASR_MODEL`/`EARMARK_ASR_LANGUAGE` also apply to `remote`. The value must equal the default (`test_env_example_values_are_defaults`). |
| `src/earmark/transcribe/base.py` | Move `SegmentLike` and `segment_from` here from `faster_whisper.py`, unchanged. |
| `src/earmark/transcribe/faster_whisper.py` | Import `SegmentLike` and `segment_from` from `base` (both are used in this module, so the names stay importable from here, as existing tests require). No behaviour change. |
| `src/earmark/transcribe/remote.py` | New. `RemoteOptions`, `RemoteAsrEngine`, `transcript_from_response`, `error_detail`, `build_remote_transcriber`, constants (4.2 to 4.5). Imports `httpx2` at module top; nothing light imports it. |
| `src/earmark/pipeline/factory.py` | Register `"remote"` in `TRANSCRIBERS` with a lazy builder (4.6). |
| `src/earmark/cli.py` | `earmark transcribe` picks the remote engine when `EARMARK_ASR_BACKEND=remote` (4.7). |
| `docs/asr.md` | New operator guide (4.8). |
| `tests/asr_stub.py` | New helper module (not collected): unix-socket OpenAI-style stub server and recording transport (4.9). |
| `tests/test_remote_asr.py` | New. Engine, mapping, errors, secrets, settings, factory, pipeline, CLI, and the doc check. |
| `tests/test_remote_asr_live.py` | New. One `@pytest.mark.network` test against a real server, skipped unless `ASR_TEST_BASE_URL` is set (6.4). |
| `tests/test_config.py` | New invalid-value rows and normalisation tests (6.1, B5). |
| `tests/test_factory.py` | `test_build_worker_config_error_touches_nothing` keeps `asr_backend="remote"` (now the missing-base-URL path) and asserts the line names `EARMARK_ASR_BASE_URL`. |
| `tests/test_cli.py` | `test_worker_unbuilt_backend_exits_2` picks an unbuilt backend from the registries (6.3). New remote `transcribe` tests. |
| `docs/ARCHITECTURE.md` | Edited with this plan (section 8, item 7). |

**No new dependency.** httpx2 is already a runtime dependency. The alternatives were the
`openai` SDK (an extra dependency, and its `verbose_json` typing does not fit servers that
omit fields) and stdlib `urllib` (no streaming multipart, and a different error model).
httpx2 does both already (2.1).

## 4. Interfaces

### 4.1 Settings (`earmark.config`)

| Field | Type | Default | Validation |
|---|---|---|---|
| `asr_base_url` | `str \| None` | `None` | Described below. |
| `asr_api_key` | `SecretStr \| None` | `None` | Described below. |
| `asr_max_upload_mb` | `int \| None` | `None` | unchanged (`gt=0`). Description: "Refuse remote uploads larger than this (MiB)." |
| `asr_timeout_seconds` | `float` | `10800.0` | **new**, `gt=0`. Description: "How long to wait for the remote ASR server's answer." |

- **`asr_base_url`** (`mode="after"`, `None` passes through):
  - strip the value, then `urllib.parse.urlsplit`;
  - the scheme (lower-cased) must be `http` or `https` and there must be a `hostname`,
    else `must be an http(s) URL with a host, such as http://gpu-box:8000/v1`;
  - a `ValueError` from `urlsplit` or from `.port` gets the same message;
  - `username` or `password` present → `must not contain credentials; use
    EARMARK_ASR_API_KEY`;
  - a non-empty `query` or `fragment` → `must not contain a query or fragment`;
  - return it with trailing `/` removed (`http://h:8000/v1/` → `http://h:8000/v1`).
  - Description: "OpenAI-compatible ASR base URL, ending in /v1."
- **`asr_api_key`** (`mode="after"`): strip `get_secret_value()`. Empty → `None`.
  Otherwise every character must be printable ASCII without spaces (`"!" <= c <= "~"`),
  else `must be printable ASCII without spaces`. Return `SecretStr(stripped)`. This stops
  a pasted trailing newline from breaking the header, and httpx2 from raising on control
  characters.

Messages never contain the value (`load_settings` drops inputs; these messages are
fixed text). `ASR_MODEL` and `ASR_LANGUAGE` are reused by `remote`: the model becomes the
`model` form field, and the language the `language` field. `ASR_DEVICE`,
`ASR_COMPUTE_TYPE`, and `ASR_MODEL_DIR` do not apply to `remote`.

### 4.2 `earmark.transcribe.remote`: constants

```python
TRANSCRIPTIONS_PATH: Final = "/audio/transcriptions"
RESPONSE_FORMAT: Final = "verbose_json"
CONNECT_TIMEOUT_S: Final = 10.0
MAX_RESPONSE_BYTES: Final = 64 * MIB
MAX_ERROR_BYTES: Final = 64 * 1024
MAX_DETAIL_CHARS: Final = 200
MAX_LANGUAGE_CHARS: Final = 32
NO_TIMESTAMPS_WARNING: Final = (
    "the ASR server returned no segment timestamps; the transcript is one segment"
)
UPLOAD_CONTENT_TYPES: Final[Mapping[str, str]] = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "m4b": "audio/mp4",
    "mp4": "audio/mp4",
    "m4v": "video/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "webm": "audio/webm",
    "mka": "audio/x-matroska",
    "mov": "video/quicktime",
    "aiff": "audio/aiff",
}
```

The keys equal `fetch.AUDIO_EXTENSIONS`; a test asserts it.

### 4.3 `RemoteOptions`

```python
@dataclass(frozen=True, slots=True)
class RemoteOptions:
    base_url: str  # validated, no trailing slash
    model: str
    language: str | None
    api_key: SecretStr | None
    max_upload_bytes: int | None
    timeout_s: float

    @property
    def endpoint(self) -> str: ...  # base_url + TRANSCRIPTIONS_PATH

    @property
    def host(self) -> str: ...  # "host" or "host:port" from base_url, for messages

    @classmethod
    def from_settings(cls, settings: Settings) -> Self: ...
```

- `from_settings` copies `asr_base_url`, `asr_model`, `asr_language`, `asr_api_key`,
  `asr_max_upload_mb * MIB` (or `None`), and `asr_timeout_seconds`.
- If `asr_base_url is None`, it raises `ConfigError(["EARMARK_ASR_BASE_URL: required when
  EARMARK_ASR_BACKEND=remote (the server's OpenAI-compatible base URL, such as
  http://gpu-box:8000/v1)"])`.
- `host` uses `urlsplit(base_url).hostname` (IPv6 in brackets) plus `:port` when the URL
  names one, shortened to 100 characters.
- The dataclass `repr` shows `SecretStr('**********')`, never the key.

### 4.4 `RemoteAsrEngine`

```python
class RemoteAsrEngine:
    def __init__(
        self,
        options: RemoteOptions,
        *,
        transport: httpx2.BaseTransport | None = None,
        monotonic: Monotonic = time.monotonic,
    ) -> None: ...

    @property
    def name(self) -> str: ...  # f"remote:{options.model}"

    @property
    def options(self) -> RemoteOptions: ...

    @property
    def timeout(self) -> httpx2.Timeout: ...  # what the client is built with

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript: ...

    def close(self) -> None: ...
```

- It satisfies `TranscriptionEngine`.
- The `httpx2.Client` is created on first use:
  - `httpx2.Client(transport=transport or httpx2.HTTPTransport(), trust_env=False,
    follow_redirects=False, timeout=httpx2.Timeout(options.timeout_s,
    connect=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S), headers={"User-Agent":
    USER_AGENT, "Accept": "application/json"})`;
  - so read and write timeouts are `ASR_TIMEOUT_SECONDS`.
- `HTTPTransport()` keeps its default `trust_env=True` only for TLS CA variables
  (`SSL_CERT_FILE`); proxies are off through the client.
- `close()` closes and forgets the client; a later call makes a new one.
- The engine is used from one thread at a time.

Module type alias: `type Monotonic = Callable[[], float]`.

### 4.5 Pure helpers

```python
def transcript_from_response(
    payload: object,
    *,
    episode_key: str,
    model_name: str,
    language: str | None,
    audio_name: str,
) -> Transcript: ...


def error_detail(body: bytes, content_type: str | None, secrets: Sequence[str]) -> str | None: ...


def build_remote_transcriber(
    settings: Settings,
    artifacts: ArtifactStore,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> CachedTranscriber: ...
```

`build_remote_transcriber` works like this:

1. `options = RemoteOptions.from_settings(settings)`, which can raise `ConfigError`.
2. Log INFO `remote ASR at %s (model %r, upload limit %s, timeout %g s)`, with the host
   and with the limit given as `N MiB` or `none`.
3. Return `CachedTranscriber(artifacts, RemoteAsrEngine(options, transport=transport),
   keep_audio=settings.keep_audio)`.

It makes no request.

Private wire models (pydantic, `extra="ignore"`, lax types; floats accept ints). For
`segment_from`, each wire segment is wrapped in a small private frozen dataclass
`_Raw(start: float, end: float, text: str)`, with `None` given as `math.nan`, which
satisfies `SegmentLike`:

```python
class _WireSegment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: float | None = None
    end: float | None = None
    text: str = ""


class _WireTranscription(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    language: str | None = None
    duration: float | None = None
    segments: list[_WireSegment] | None = None
```

### 4.6 Factory

```python
def _remote_transcriber(ctx: BuildContext) -> Transcriber:
    from earmark.transcribe.remote import build_remote_transcriber

    return build_remote_transcriber(ctx.settings, ctx.artifacts)


TRANSCRIBERS: Final[Mapping[str, Builder[Transcriber]]] = {
    "fake": lambda ctx: FakeTranscriber(ctx.artifacts),
    "faster_whisper": _faster_whisper_transcriber,
    "remote": _remote_transcriber,
}
```

The `ConfigError` for a missing base URL propagates out of `build_backends` and
`build_worker` before the database opens, so `earmark worker` exits 2 and creates
nothing. `factory.py` must not import `httpx2` at module top
(`test_pipeline_modules_import_light` lists it as heavy).

### 4.7 CLI: `earmark transcribe AUDIO_FILE [--json]`

The signature is unchanged. The new behaviour:

- With `settings.asr_backend == "remote"`:
  - import `earmark.transcribe.remote as remote` inside the function;
  - `engine = remote.RemoteAsrEngine(remote.RemoteOptions.from_settings(settings))`,
    where `ConfigError` → stderr, exit 2;
  - then `transcript = engine.transcribe_file(audio, fw.file_key(audio))` inside
    `try/finally: engine.close()`;
  - error handling and output are exactly as step 08 (`TransientError` → exit 1 with
    `(temporary; try again later)`; `PermanentError` → exit 1);
  - transcript warnings are printed to stderr as `earmark transcribe: warning: <text>`
    through `_clean`.
- With any other value: the local faster-whisper engine, as before.

Docstring: "Transcribe a local audio file and print the result. Uses the remote ASR
server when EARMARK_ASR_BACKEND=remote, otherwise the local faster-whisper engine;
writes nothing under EARMARK_DATA_DIR and leaves the input file alone."

`fw.file_key` stays in `faster_whisper.py`, which is importable without the extra.

### 4.8 `docs/asr.md` (operator guide)

Required content, in this order. Facts come from 2.2; state "not verified by the earmark
build (no GPU)" where it applies.

1. **What earmark sends.** A POST of `multipart/form-data` to
   `$EARMARK_ASR_BASE_URL/audio/transcriptions` with these fields:
   - `file` (`audio.<ext>` with its content type);
   - `model` (`EARMARK_ASR_MODEL`);
   - `response_format=verbose_json`;
   - `timestamp_granularities[]=segment`;
   - `language` (only when `EARMARK_ASR_LANGUAGE` is set);
   - `Authorization: Bearer $EARMARK_ASR_API_KEY` (only when set).

   What it expects back: JSON with `text` and `segments[].start/end/text` in seconds.
   Text without segments works but gives one segment and a job warning. Include a
   `curl` with `--form-string` for the scalar fields, so an operator can test the server
   by hand.
2. **Settings.** `EARMARK_ASR_BACKEND=remote`, `EARMARK_ASR_BASE_URL` (ending in `/v1`),
   `EARMARK_ASR_MODEL` (the server's model id, for example `openai/whisper-large-v3`;
   the faster-whisper default `large-v3` usually 404s on a server), `EARMARK_ASR_API_KEY`,
   `EARMARK_ASR_MAX_UPLOAD_MB` (set it to the server's limit to fail before uploading),
   `EARMARK_ASR_TIMEOUT_SECONDS`, `EARMARK_ASR_LANGUAGE`, `EARMARK_KEEP_AUDIO`. Also say
   that `EARMARK_ALLOW_PRIVATE_FETCH` is **not** needed and should stay `false`. Test
   with `earmark transcribe <file>`.
3. **On the DGX Spark: vLLM with Whisper (recommended).** Cover:
   - NVIDIA's arm64 vLLM container (find the tag on NGC or at build.nvidia.com/spark/vllm);
   - `vllm serve openai/whisper-large-v3-turbo --api-key ...` (or `whisper-large-v3`);
   - raising `VLLM_MAX_AUDIO_CLIP_FILESIZE_MB` (25 MB by default),
     `VLLM_MAX_AUDIO_DECODE_DURATION_S` (600 s by default), and
     `VLLM_MAX_AUDIO_DECODE_BYTES` (256 MiB by default, about 70 min of 16 kHz audio),
     with values for 3 h episodes, for example 1024 MB, 14400 s, and 4 GiB;
   - that `verbose_json` works only for Whisper models in vLLM;
   - the 30 s chunking with absolute timestamps;
   - that M4A needs ffmpeg in the container;
   - that the GPU memory budget is shared with any LLM on the Spark (`--gpu-memory-utilization`).
4. **Alternative: whisper.cpp `whisper-server`.** Cover:
   - the build recipe from 2.2 (CUDA 13 devel Ubuntu 24.04 base, `-DGGML_CUDA=ON
     -DCMAKE_CUDA_ARCHITECTURES="120;121"`, target `whisper-server`);
   - running it with `--host 0.0.0.0 --port 8000 --inference-path
     /v1/audio/transcriptions --convert` (ffmpeg in the image) and a ggml model such as
     `ggml-large-v3-turbo.bin`;
   - that it has no API key, so bind it to the tailnet or localhost and leave
     `EARMARK_ASR_API_KEY` unset;
   - that its base URL is `http://<host>:8000/v1`.
5. **Alternative: LocalAI.** Cover the CUDA 13 arm64 image, installing the `whisper` or
   `parakeet-cpp` backend, and that `EARMARK_ASR_MODEL` is the LocalAI model name.
   Present it as the way to run Parakeet with segment timestamps on the Spark.
6. **Parakeet notes.** Cover:
   - LocalAI `parakeet-cpp` (segments from word timestamps);
   - NVIDIA Parakeet NIM HTTP (json/text only, WAV/Opus/FLAC: not suitable);
   - Nemotron Transcribe NIM (verbose_json but no MP3/M4A, 3600 s cap: not suitable
     without conversion, which earmark does not do);
   - community Parakeet servers that accept `verbose_json` but return no segments (one
     segment and a warning).
7. **x86_64 GPU hosts.** speaches (faster-whisper, `verbose_json`) works there; on
   arm64 its CUDA image falls back to CPU (speaches#620).
8. **Security.** Cover:
   - the server is operator-trusted and reached directly (no address guard), so point
     the URL only at a server you run;
   - prefer the tailnet or https;
   - over plain http the key is visible on the path unless the path is a tailnet;
   - earmark never follows redirects from it.
9. **Troubleshooting.** Map these job errors to fixes: HTTP 404 (base URL without `/v1`,
   or the model id), 401/403 (key), 400/413 (server size limits, unsupported
   `verbose_json`, format not decodable), timeouts (`EARMARK_ASR_TIMEOUT_SECONDS`), and
   the "no segment timestamps" warning.

The doc carries no secrets and no real host names; examples use `gpu-box` and `spark`.

### 4.9 Test helpers (`tests/asr_stub.py`)

```python
@dataclass(frozen=True)
class StubReply:
    status: int = 200
    body: bytes = b'{"text": ""}'
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "application/json"),)
    hold: threading.Event | None = None  # wait for it (with a 10 s cap) before replying


@dataclass(frozen=True)
class ReceivedRequest:
    method: str
    path: str
    headers: Mapping[str, str]  # names lower-cased
    fields: Mapping[str, str]  # non-file parts
    file_name: str | None
    file_type: str | None
    file_bytes: bytes


class AsrStub:
    """Threaded HTTP/1.1 server on a unix socket in a short temp dir; scripted replies."""

    def __init__(self, replies: Sequence[StubReply]) -> None: ...

    requests: list[ReceivedRequest]

    def transport(self) -> httpx2.HTTPTransport: ...  # HTTPTransport(uds=socket path)

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...  # set holds, shutdown, remove dir


class RecordingTransport(httpx2.BaseTransport):
    """Iterates the request body without buffering it; records chunk sizes and headers."""

    def __init__(self, reply: Callable[[httpx2.Request], httpx2.Response]) -> None: ...

    chunk_sizes: list[int]
    headers: Mapping[str, str] | None
    body: bytes  # the concatenated chunks, for parsing after the fact


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], bytes, str | None]: ...


def verbose_json(segments: Sequence[tuple[float, float, str]], **extra: object) -> bytes: ...
```

- The socket directory comes from `tempfile.mkdtemp(prefix="asr-")`, because pytest's
  `tmp_path` can exceed the 108-byte `AF_UNIX` path limit.
- The handler overrides `address_string`, `log_message`, and `handle_error` (recording,
  not printing), reads `Content-Length` bytes, and parses multipart with
  `email.parser.BytesParser(policy=email.policy.HTTP)` (2.1).
- A reply beyond the script is a 500 with body `{"error": "unscripted"}`.
- It catches `BrokenPipeError` and `ConnectionResetError` when writing.
- `verbose_json` builds an OpenAI-shaped body (`text` joined, `language` `"en"`,
  `duration` = last end unless overridden) for fixtures.

## 5. Behaviour and edge cases

### 5.1 Trust boundary

- The only URL the client contacts is `options.endpoint`, built from settings. Nothing
  in a response can change the target: redirects are not followed, and no `Location` is
  read.
- There is no address check, and `ALLOW_PRIVATE_FETCH` is not consulted (2.4).
- The response body is untrusted text: capped, parsed as JSON, validated, whitespace
  collapsed, and never used in a path or logged.

### 5.2 `RemoteAsrEngine.transcribe_file(audio, episode_key)`

1. `size = audio.stat().st_size`.
   - `size == 0` → `UnusableAudioError(f"{audio.name} is empty")`.
   - If `max_upload_bytes` is set and `size > max_upload_bytes` → `PermanentError(f"{audio.name}
     is {size / MIB:.1f} MiB, larger than the {limit} MiB remote ASR upload limit
     (EARMARK_ASR_MAX_UPLOAD_MB)")`, where `limit = max_upload_bytes // MIB`.
   - No request is made, and the audio is kept (`CachedTranscriber` keeps it on a plain
     `PermanentError`), so raising the limit and re-sharing reuses the download.
2. `ext = audio.suffix[1:].lower()`, or `"bin"` if that is not in `AUDIO_EXTENSIONS`.
   The upload filename is `f"audio.{ext}"`, never the local name or path. The content
   type is `UPLOAD_CONTENT_TYPES.get(ext, "application/octet-stream")`.
3. Fields, in this order: `model`, `response_format=verbose_json`,
   `timestamp_granularities[]=segment`, and `language` when set.
4. Headers: `Authorization: Bearer <key>` only when `api_key` is not `None`. It is
   passed per request, never stored on the client and never logged.
5. Log INFO `uploading %s (%.1f MiB) to remote ASR at %s for %s` (key, size, host,
   `name`).
6. Call `client.cookies.clear()` first (as `fetch.py` does), so no cookie a server set
   is ever sent back. Send the request with `with audio.open("rb") as fh, client.stream("POST",
   options.endpoint, data=fields, files={"file": (filename, fh, ctype)}, headers=headers)
   as response:`.
   - Check the status (5.3).
   - Read the body with `iter_bytes()`. More than `MAX_RESPONSE_BYTES` decoded bytes →
     `PermanentError(f"remote ASR at {host} returned more than 64 MiB")`.
7. `json.loads(body)`. A `ValueError` (including `UnicodeDecodeError`) →
   `PermanentError(f"remote ASR at {host} returned a response that is not JSON")`.
8. `transcript_from_response(payload, episode_key=..., model_name=self.name,
   language=options.language, audio_name=audio.name)` (5.4).
9. Log INFO `remote ASR transcribed %s: %d segments, language %s, %s of audio in %.1f s`.
   If the transcript has warnings, log each at WARNING. Return the transcript.

The engine writes and deletes nothing. `BaseException`s (`Shutdown`, `KeyboardInterrupt`)
pass through untouched; no `except` clause in the engine catches anything broader than
`Exception`. The file handle is closed in every case.

### 5.3 Status and transport errors

`host` is `options.host`. Server error text (`detail`) comes from `error_detail` (5.5);
`: {detail}` is appended when it is not `None`.

| Condition | Result |
|---|---|
| 200 | parse (5.2) |
| 408, 429, 500 to 599 | `TransientError(f"remote ASR at {host} failed: HTTP {status}{detail}", retry_after_s=parse_retry_after(Retry-After, now))` |
| 401, 403 | `PermanentError(f"remote ASR at {host} refused the request: HTTP {status} (check EARMARK_ASR_API_KEY){detail}")` |
| 404 | `PermanentError(f"remote ASR at {host}: HTTP 404 (check EARMARK_ASR_BASE_URL, which should end in /v1, and EARMARK_ASR_MODEL){detail}")` |
| 413 | `PermanentError(f"remote ASR at {host}: HTTP 413, the upload is too large for the server (raise its limit, or set EARMARK_ASR_MAX_UPLOAD_MB to fail early){detail}")` |
| 300 to 399 | `PermanentError(f"remote ASR at {host} answered with a redirect (HTTP {status}); redirects are not followed, check EARMARK_ASR_BASE_URL")` |
| other 400 to 499 | `PermanentError(f"remote ASR at {host} rejected the request: HTTP {status}{detail}")` |
| anything else | `PermanentError(f"remote ASR at {host} returned unexpected HTTP {status}")` |
| `httpx2.ReadTimeout` | `TransientError(f"remote ASR at {host} did not answer within {timeout_s:g} s (EARMARK_ASR_TIMEOUT_SECONDS)")` |
| other `httpx2.TimeoutException` | `TransientError(f"remote ASR request to {host} timed out")` |
| other `httpx2.TransportError`, `httpx2.DecodingError` | `TransientError(f"remote ASR request to {host} failed: {type(exc).__name__}")` |
| `httpx2.InvalidURL` | `PermanentError("invalid EARMARK_ASR_BASE_URL")` (should not happen after validation) |

- Every mapped error chains with `from exc`.
- The exception text of httpx2 errors is never copied, because it can carry the URL.
- For error statuses, at most `MAX_ERROR_BYTES` of the body are read for the detail,
  then the response is closed.
- The clock for `Retry-After` is `utc_now` (the engine does not need an injected clock;
  tests use `Retry-After: 120`).

### 5.4 `transcript_from_response`: mapping and the no-segments fallback

1. Validate with `_WireTranscription.model_validate(payload)`. A non-dict or a
   `ValidationError` → `PermanentError(f"remote ASR returned a response that is not a
   transcription ({audio_name})")`. The pydantic text is not copied.
2. `segments = wire.segments or []`.
3. **Duration**, first that applies:
   - `wire.duration` if finite and `> 0`;
   - else the largest finite `end` over segments, if `> 0`;
   - else `0.0`.
4. **Timestamps are present** if any segment has a finite `end > 0`. LocalAI's
   no-timestamp segment (`start = end = 0`), whisper.cpp with timestamps off (no
   `start`/`end`), and an empty list all count as absent.
5. **With timestamps:** walk segments in the order received. Convert `None` to `nan` and
   call `segment_from(raw, previous_start=prev, duration_s=duration)`, the same rules as
   step 08: blank text dropped, start never decreasing, `end >= start`, clamped to
   `[0, duration]`. Keep the non-`None` results. If any remain, there are no warnings.
6. **Without timestamps**, or when step 5 left nothing:
   - `text` = `wire.text` whitespace-collapsed, or else the segment texts joined with
     spaces and collapsed;
   - if `text` is non-empty, use one `Segment(start=0.0, end=duration, text=text)` and
     `warnings=(NO_TIMESTAMPS_WARNING,)`;
   - the text is never truncated.
7. Empty text and no segments → `UnusableAudioError(f"no speech found in {audio_name}
   (remote ASR)")`. `CachedTranscriber` then deletes the audio unless `KEEP_AUDIO`, like
   step 08.
8. **Language.** If `language` (the setting) is set, use it. Otherwise use
   `wire.language` whitespace-collapsed and lower-cased, kept only if non-empty, at most
   `MAX_LANGUAGE_CHARS`, and made of letters, spaces, and hyphens; otherwise `None`.
   `"english"` and `"en"` are both kept as given.
9. Return `Transcript(episode_key=..., language=..., duration_s=duration,
   model=model_name, segments=tuple(...), warnings=...)`.

### 5.5 `error_detail(body, content_type, secrets)`

- Decode as UTF-8 with `errors="replace"`.
- If the media type is JSON, or the body starts with `{`, try `json.loads`. Take the
  first string among `error.message`, `error` (when it is a string), `detail` (a string,
  or the `msg` of its first item when it is a list), and `message`.
- Otherwise, for `text/plain` only, use the text. HTML and other types give `None`.
- Replace non-printable characters with spaces, collapse whitespace, redact every string
  in `secrets` with `log.redact`, and cap at `MAX_DETAIL_CHARS`. Empty → `None`.
- The engine passes `[options.api_key.get_secret_value()]` when a key is set. The runner
  redacts every `Settings` secret from `job.error` as well, so this is defence in depth.

### 5.6 Caching, files, and audio (unchanged shell)

All of this is `CachedTranscriber` from step 08, tested here through the remote engine:

- an existing `transcript.json` → returned with no request;
- otherwise transcribe, then `write_transcript` (`transcript.txt`, then `transcript.json`,
  atomic);
- then delete `audio.*` unless `KEEP_AUDIO`;
- the audio is kept on every failure except `UnusableAudioError`.

### 5.7 Long jobs, heartbeat, and shutdown

- The main thread blocks in socket I/O, which releases the GIL, so the heartbeat thread
  keeps the lease fresh for the whole wait.
- `ASR_TIMEOUT_SECONDS` (default 3 h) bounds a silent wait. It covers an hour of audio
  even on a server running at a third of real time. On GPU, vLLM and whisper.cpp are
  expected to be far faster; that is not measured here.
- A timeout is transient, so the job retries with backoff and re-uploads.
- SIGTERM raises `Shutdown` out of the blocking read (2.1, probe 3). Nothing is written,
  the audio is kept, and the worker releases the job.

### 5.8 Logs and secrets

- Logs name the host, the episode key, the file base name, sizes, and counts. They never
  contain the URL path, the key, response bodies, or transcript text.
- The key exists only in `RemoteOptions.api_key` (a `SecretStr`) and in the per-request
  header.
- `earmark config` already redacts it, and `log.configure_logging` already redacts every
  secret and quiets `httpx2`/`httpcore2`.
- Over plain `http` the key travels in clear text unless the path is a tailnet;
  `docs/asr.md` says so. Earmark does not refuse it, because the tailnet encrypts.

## 6. Tests

All default-suite tests run with sockets blocked. They use `httpx2.MockTransport`,
`RecordingTransport`, or `AsrStub` (a unix socket, allowed). Engines are built with
`RemoteOptions(...)` directly, or through `Settings(_env_file=None, asr_backend="remote",
asr_base_url="http://asr.test:8000/v1", ...)`. Episodes come from
`FakeResolver().resolve(CANON)`. Audio is a few KiB of random bytes written to
`artifacts.audio_path(key, "mp3")`; the server never decodes it. Fixtures are built in
the tests; nothing is recorded from a live server.

### 6.1 Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | Against a mock server, audio is uploaded as streamed multipart, and a response with segments maps to a `Transcript` with timestamps | See A1 below. |
| A2 | Text but no segments yields a single-segment transcript and a job warning | See A2 below. |
| A3 | Audio larger than `ASR_MAX_UPLOAD_MB` raises `PermanentError` naming the setting | See A3 below. |
| A4 | The API key header is sent only when configured; the timeout suits hour-long audio | See A4 below. |
| A5 | Connection errors and 5xx are transient; 4xx is permanent | See A5 below. |
| A6 | Caching and file outputs match step 08 | See A6 below. |

**A1. Streamed upload and segment mapping.**

- `test_upload_is_streamed_multipart`: a 1 MiB random file through `RecordingTransport`.
  Assert:
  - `content-type` starts with `multipart/form-data; boundary=`;
  - `content-length` equals `sum(chunk_sizes)`, and there is no `transfer-encoding`;
  - `max(chunk_sizes) <= 65536` and `len(chunk_sizes) > 16`;
  - the parsed parts are `model == "whisper-x"`, `response_format == "verbose_json"`,
    `timestamp_granularities[] == "segment"`, no `language`, and file name `audio.mp3`
    of type `audio/mpeg` whose bytes equal the file.
- `test_upload_over_unix_socket_server`: the same against `AsrStub` (a real h11 path).
  `stub.requests[0].path == "/v1/audio/transcriptions"`, the method is `POST`, the file
  bytes match, and `user-agent` is `earmark/<version>`.
- `test_segments_map_to_transcript`: a three-segment `verbose_json` with leading spaces,
  inner newlines, extra keys (`tokens`, `avg_logprob`, `words`), and `duration` 30.0.
  Assert the texts are collapsed, the start and end values are exact,
  `duration_s == 30.0`, `model == "remote:whisper-x"`, `language == "en"`, and
  `warnings == ()`.
- `test_monotonic_mapping`, parametrized:
  - regressing start → previous start;
  - `end < start` → `end == start`;
  - `start` missing → previous start;
  - `end` missing → start;
  - beyond `duration` → clamped;
  - a blank segment dropped;
  - overlapping chunk boundaries kept in order.
- `test_duration_fallbacks`: no `duration` → the largest end; `duration` 0 or negative →
  the largest end.
- `test_language_from_settings_wins` (sent as the `language` field, and used in the
  transcript).
- `test_language_normalised` (`"English"` → `"english"`; `"EN"` → `"en"`; `"??"` → `None`).

**A2. Text without segments.**

- `test_text_without_segments_is_one_segment_with_warning`, parametrized over these
  bodies:
  - `{"text": " hello  world "}`;
  - `{"text": "hello", "segments": []}`;
  - `{"text": "hello", "segments": null, "duration": 12.5}`;
  - LocalAI's `{"text": "hello", "segments": [{"id": 0, "text": "hello", "start": 0,
    "end": 0}]}`;
  - whisper.cpp without timestamps `{"text": "hello", "segments": [{"id": 0, "text":
    "hello"}]}`.

  Each gives one segment starting at 0 with the full text, `end == duration_s` (12.5
  where given, else 0), and `warnings == (NO_TIMESTAMPS_WARNING,)`.
- `test_long_text_not_truncated` (200 000 characters in, all out).
- `test_text_only_warning_reaches_job`: `make_env` with
  `CachedTranscriber(fakes.artifacts, RemoteAsrEngine(opts, transport=mock), keep_audio=False)`.
  The job is `done`, `NO_TIMESTAMPS_WARNING` is in `job.warnings`, and
  `transcript.json` holds the warning. A second job with a new prompt for the same
  episode reports the warning again from the cache, with no new request.
- `test_no_speech_is_unusable`: `{"text": ""}`, `{"text": "  ", "segments": [{"start":
  0, "end": 1, "text": " "}]}` → `UnusableAudioError`. Through `CachedTranscriber`, the
  audio is deleted (and kept with `keep_audio=True`).

**A3. Upload cap.**

- `test_upload_cap_refuses_before_request`: `max_upload_bytes = 1 MiB` and a file of
  1 MiB + 1 byte. `PermanentError`, the message contains `EARMARK_ASR_MAX_UPLOAD_MB` and
  `1 MiB`, the handler is called 0 times, and through `CachedTranscriber` the audio is
  kept.
- `test_upload_cap_allows_equal_size` (exactly 1 MiB passes).
- `test_no_cap_by_default` (`asr_max_upload_mb` unset → `max_upload_bytes is None`, a
  2 MiB file uploads).
- `test_options_from_settings_cap` (`EARMARK_ASR_MAX_UPLOAD_MB=100` →
  `100 * 1024 * 1024`).

**A4. API key and timeout.**

- `test_no_auth_header_without_key` (MockTransport: `"authorization" not in
  request.headers`; also over `AsrStub`).
- `test_bearer_header_with_key` (`Authorization == "Bearer sk-test-77"`).
- `test_key_from_env_is_stripped` (`EARMARK_ASR_API_KEY=" sk-x\n"` → `Bearer sk-x`).
- `test_key_never_logged_or_in_errors`, with a key `sentinel-key-9f3`:
  - `caplog.set_level(DEBUG)` on the root logger;
  - one successful request, and one 401 whose body is
    `{"error": {"message": "bad key sentinel-key-9f3"}}`, over both MockTransport and
    `AsrStub` (so httpcore2 runs);
  - the sentinel is in neither `caplog.text` nor the `PermanentError` message (which
    contains `**********`), nor `repr(options)`, nor `repr(engine.options)`.
- `test_timeout_from_settings`: `engine.timeout` has `read == write == 10800.0` by
  default and `connect == pool == 10.0`, and `read == 60.0` with
  `EARMARK_ASR_TIMEOUT_SECONDS=60`. Through a MockTransport handler, the request's
  `extensions["timeout"]` equals `{"connect": 10.0, "read": 10800.0, "write": 10800.0,
  "pool": 10.0}`, which proves the client really uses it.
- `test_default_timeout_covers_an_hour`: `Settings().asr_timeout_seconds >= 3600`.
- `test_read_timeout_is_transient_naming_setting` (the handler raises
  `httpx2.ReadTimeout` → `TransientError` containing `EARMARK_ASR_TIMEOUT_SECONDS`).

**A5. Error classes.**

- `test_status_mapping`, parametrized:
  - 408, 429, 500, 502, 503, 504 → `TransientError`;
  - 400, 401, 403, 404, 413, 415, 422 → `PermanentError`, each message containing
    `HTTP <status>`;
  - 401 names `EARMARK_ASR_API_KEY`, 404 names `EARMARK_ASR_BASE_URL` and
    `EARMARK_ASR_MODEL`, 413 names `EARMARK_ASR_MAX_UPLOAD_MB`;
  - 301 and 307 → `PermanentError` "redirects are not followed", with exactly one
    request made (the handler counts calls);
  - 204 and 206 → "unexpected".
- `test_retry_after_honoured` (`503` with `Retry-After: 120` → `retry_after_s == 120`).
- `test_transport_errors_transient`, parametrized over `httpx2.ConnectError`,
  `ConnectTimeout`, `WriteTimeout`, `PoolTimeout`, `ReadError`, `RemoteProtocolError`,
  `WriteError`, and `DecodingError`, each raised by the handler with a message
  containing `/v1/secret-path`: `TransientError`, and the path is not in the message.
- `test_connection_refused_over_unix_socket` (`HTTPTransport(uds=<missing path>)` →
  `TransientError`).
- `test_error_detail_extraction`, a table:
  - each of the five body shapes in 2.3 → its message;
  - HTML → `None`;
  - 300 characters → capped at 200;
  - control characters → spaces;
  - a secret → `**********`;
  - invalid UTF-8 → no exception.
- `test_unreadable_200_is_permanent`: HTML body, a JSON list, `{"text": 5}`, and more
  than `MAX_RESPONSE_BYTES` (monkeypatch the constant to 1 KiB) → `PermanentError`.
- `test_empty_file_is_unusable`.

**A6. Caching and file outputs.**

- `test_cached_transcript_makes_no_request`: `transcript.json` exists → the handler is
  called 0 times, the stored transcript is returned, and leftover audio is deleted (kept
  with `keep_audio=True`).
- `test_transcript_files_written`:
  - `transcript.txt == transcript.to_text()`;
  - `transcript.json` validates back to the returned transcript;
  - both are mode `0o644`, and no `.tmp` is left;
  - no `audio.*` remains, and the audio is kept with `keep_audio=True`.
- `test_audio_kept_on_failure` (503, then 400, then `Shutdown` raised from the handler:
  the audio exists after each, and no `transcript.*`).
- `test_pipeline_with_remote`:
  - `make_env(tmp_path, fakes=fakes, backends=dataclasses.replace(fakes.backends(),
    transcriber=build_remote_transcriber(settings, fakes.artifacts, transport=mock)))`,
    with `settings` having `asr_backend="remote"` and
    `asr_base_url="http://asr.test:8000/v1"`;
  - two jobs for one episode with different prompts: both `done`, and the handler is
    called exactly once.
  - The factory wiring itself is covered by `test_factory_builds_remote` (B1).
- `test_heartbeat_fresh_during_remote_wait`: the same pattern as step 08's
  `test_heartbeat_fresh_during_transcription`. The MockTransport handler advances
  `env.clock` by 31 s, waits for two beats, records `requeue_stale()`, and then replies.
  The job is `done`, and the recorded result is `[]`.
- `test_shutdown_during_remote_wait`: `AsrStub` with a held reply. A `SIGALRM` after
  0.2 s raises `earmark.pipeline.worker.Shutdown` from the handler, `setitimer` is
  restored in `finally`, and the handler is run only in the main thread. Assert:
  - `Shutdown` propagates out of `CachedTranscriber.transcribe` within 5 s;
  - there is no `transcript.*`, and the audio is kept;
  - the stub is torn down cleanly (`hold.set()` in `__exit__`).

### 6.2 Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | The backend is registered, built only by the factory, light to import | See B1 below. |
| B2 | A missing base URL is a startup error that touches nothing | See B2 below. |
| B3 | The remote client ignores the address guard and environment proxies, and never follows redirects | See B3 below. |
| B4 | `earmark transcribe` uses the remote engine when configured | See B4 below. |
| B5 | Settings validation | See B5 below. |
| B6 | Shared segment mapping is unchanged for faster-whisper | See B6 below. |
| B7 | `docs/asr.md` stays in step with the settings | See B7 below. |
| B8 | Upload metadata | See B8 below. |

**B1. Registration and light imports.**

- `test_factory_builds_remote`: `isinstance(b.transcriber, CachedTranscriber)`, its
  engine is a `RemoteAsrEngine` whose options equal `RemoteOptions.from_settings`, and no
  request is made.
- `test_types_satisfy_protocols` (`t: Transcriber = CachedTranscriber(...)`, `e:
  TranscriptionEngine = RemoteAsrEngine(...)`; mypy is the proof).
- `test_remote_module_import_light` (subprocess: `import earmark.transcribe.remote`; none
  of `faster_whisper`, `ctranslate2`, `av`, `numpy`, `huggingface_hub` in
  `sys.modules`).
- The existing `test_pipeline_modules_import_light` and
  `test_transcribe_modules_import_light` keep passing (no `httpx2` via the factory or the
  CLI at import).

**B2. Missing base URL.**

- `test_missing_base_url_is_config_error`: `build_backends` with `asr_backend="remote"`
  and no URL → `ConfigError` whose single line starts `EARMARK_ASR_BASE_URL: required
  when EARMARK_ASR_BACKEND=remote`.
- `test_factory.py::test_build_worker_config_error_touches_nothing` asserts that line, and
  that `data/` does not exist.
- `test_cli.py::test_worker_remote_without_url_exits_2` (exit 2, stderr names
  `EARMARK_ASR_BASE_URL`, no `data/`).

**B3. Client trust boundary.**

- `test_private_address_allowed`: an `AsrStub` reached with base URL
  `http://127.0.0.1:8000/v1` and `http://100.100.1.1/v1`, with `allow_private_fetch`
  false (default) → success. The uds transport ignores the host, so this shows that no
  guard runs.
- `test_env_proxy_ignored`: `monkeypatch.setenv` `HTTP_PROXY`, `HTTPS_PROXY`, and
  `ALL_PROXY` to `http://proxy.invalid:1`. Monkeypatch `httpx2.HTTPTransport` with a
  callable that returns the `AsrStub` transport, and build the engine with
  `transport=None`. The request reaches the stub, which shows that no proxy transport
  was mounted.
- `test_redirect_not_followed` (covered in A5: one request).
- `test_cookies_not_sent`: the first reply sets a `Set-Cookie`, and the second request
  has no `cookie` header. Either clear `client.cookies` before each request, or build
  the client with a no-op cookie jar. The plan requires the clearing, as in `fetch.py`.

**B4. `earmark transcribe` with the remote engine.** Monkeypatch
`earmark.transcribe.remote.RemoteAsrEngine` with a subclass that injects a MockTransport.

- `test_cli_transcribe_remote_prints_lines`: `EARMARK_ASR_BACKEND=remote`, base URL
  set. Exit 0, the `[00:00:00]` lines, the input file unchanged, and `./data` not
  created.
- `test_cli_transcribe_remote_warning_on_stderr` (text-only response → stderr contains
  `warning: the ASR server returned no segment timestamps`).
- `test_cli_transcribe_remote_errors` (503 → exit 1 with `(temporary; try again later)`;
  404 → exit 1).
- `test_cli_transcribe_remote_without_url_exits_2`.
- The existing `test_transcribe_ignores_asr_backend` (with `fake`) still uses the local
  engine.

**B5. Settings validation.** New rows in `test_config.py::test_invalid_values_rejected`
(`echo_check` true):

- `EARMARK_ASR_BASE_URL=ftp://sentinel-host/v1`;
- `EARMARK_ASR_BASE_URL=sentinel-nohost`;
- `EARMARK_ASR_BASE_URL=http://user:sentinel-pw@h/v1`;
- `EARMARK_ASR_BASE_URL=http://h/v1?k=sentinel-q`;
- `EARMARK_ASR_BASE_URL=http://h:99999/v1` (echo off);
- `EARMARK_ASR_API_KEY=sentinel key`;
- `EARMARK_ASR_TIMEOUT_SECONDS=0` (echo off).

Also add `test_asr_base_url_normalised` (`" http://h:8000/v1/ "` → `http://h:8000/v1`;
`HTTPS://H/v1` accepted) and `test_asr_api_key_blank_is_none` (`"   "` → `None`, and not
in `secret_values()`). The existing `.env.example` tests cover the new field.

**B6. faster-whisper unchanged.** The existing `test_faster_whisper.py::test_segment_from`
and `test_segment_from_blank_text_is_none` still import from `faster_whisper` and pass.
Add `test_transcribe_base.py::test_segment_from_lives_in_base` (`faster_whisper.segment_from
is base.segment_from`).

**B7. `docs/asr.md`.** `test_asr_doc_names_settings`: the file exists and contains each of
`EARMARK_ASR_BACKEND=remote`, `EARMARK_ASR_BASE_URL`, `EARMARK_ASR_API_KEY`,
`EARMARK_ASR_MAX_UPLOAD_MB`, `EARMARK_ASR_TIMEOUT_SECONDS`, `EARMARK_ASR_MODEL`,
`verbose_json`, and `/audio/transcriptions`.

**B8. Upload metadata.**

- `test_upload_filename_and_type`, parametrized over `mp3`, `m4a`, `opus`, `flac`, and
  an odd suffix `.xyz`. The filename is `audio.<ext>` (or `audio.bin`) with the mapped
  type (or `application/octet-stream`), and the local file name never appears in the
  body headers.
- `test_content_types_cover_audio_extensions` (`set(UPLOAD_CONTENT_TYPES) ==
  fetch.AUDIO_EXTENSIONS`).

### 6.3 Changes to existing tests

- `test_cli.py::test_worker_unbuilt_backend_exits_2`: `remote` is now built.
  - Compute the first `(field, name)` whose name is valid for the field's `Literal` but
    absent from its `factory` registry, using the same `FIELDS` mapping idea as
    `test_factory.py` (import it from there or rebuild it locally).
  - Set that variable, and keep the assertions (exit 2, stderr names the variable,
    nothing created).
  - `pytest.skip("every backend is built")` if there is none.
- `test_factory.py`: `UNBUILT_FIELDS` no longer contains `asr_backend`, and the
  parametrization shrinks by itself. `test_default_settings_list_all_unbuilt` is
  unaffected.

### 6.4 Opt-in live test (`tests/test_remote_asr_live.py`)

`@pytest.mark.network` `test_live_server_transcribes_clip`:

- Skip with the reason `set ASR_TEST_BASE_URL (and ASR_TEST_MODEL, ASR_TEST_API_KEY) to
  run against a real server` unless `ASR_TEST_BASE_URL` is set. The variables are not
  `EARMARK_`-prefixed, because `conftest.py` strips those.
- `clip = slow_assets.fetch_clip()` (the public-domain JFK clip from step 08); copy it
  into an episode directory.
- Transcribe through `CachedTranscriber` with `RemoteOptions(base_url=..., model=$ASR_TEST_MODEL,
  language=None, api_key=..., max_upload_bytes=None, timeout_s=600)`.
- Assert:
  - the joined text contains `country` (case-insensitive);
  - `transcript.warnings == ()` (a server without segment timestamps fails this test,
    with a message saying so);
  - at least one segment has `end > 0`;
  - `10.0 <= duration_s <= 12.0`.

CI's weekly `network` job reports it as skipped. The operator runs it on the homelab
(section 7).

## 7. Verification

```bash
cd /home/sandy/dev/earmark
uv sync --locked
make check
uv run ruff format --check docs/
uv lock --check                 # no dependency change
git diff --stat uv.lock pyproject.toml   # expect nothing

# Light imports
uv run --locked python -c 'import sys, earmark.pipeline.factory, earmark.cli; print(sorted(m for m in ("httpx2", "earmark.transcribe.remote") if m in sys.modules))'
# expect: []
uv run --locked python -c 'import sys, earmark.transcribe.remote; print(sorted(m for m in ("faster_whisper", "ctranslate2", "av", "numpy") if m in sys.modules))'
# expect: []
grep -n "httpx2" src/earmark/pipeline/factory.py src/earmark/cli.py   # expect nothing
```

**Live smoke over real loopback TCP** (outside pytest, so sockets work). `SCRATCH` is the
verifier's scratchpad. Save this stub as `$SCRATCH/asr_stub_server.py`. It requires the
key, prints the fields it receives, and answers by mode (`MODE` env: `segments`, `text`,
`error503`).

```python
import email.parser
import email.policy
import http.server
import json
import os
import sys

KEY = os.environ["STUB_KEY"]
MODE = os.environ.get("MODE", "segments")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        head = b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n"
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
        fields = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True)
            fields[name] = part.get_filename() or data.decode()
        auth_ok = self.headers.get("Authorization") == f"Bearer {KEY}"
        print(self.path, fields, "auth ok" if auth_ok else "auth missing", file=sys.stderr)
        if not auth_ok:
            status, reply = 401, {"error": {"message": "bad key"}}
        elif MODE == "error503":
            status, reply = 503, {"error": {"message": "warming up"}}
        elif MODE == "text":
            status, reply = 200, {"text": "hello from the stub"}
        else:
            segments = [
                {"start": 0.0, "end": 2.0, "text": " hello"},
                {"start": 2.0, "end": 4.5, "text": " from the stub"},
            ]
            reply = {"text": "hello from the stub", "language": "en", "duration": 4.5}
            status, reply = 200, {**reply, "segments": segments}
        out = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


http.server.HTTPServer(("127.0.0.1", 18089), Handler).serve_forever()
```

```bash
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
head -c 300000 /dev/urandom > clip.mp3
STUB_KEY=k-smoke-5d1e MODE=segments python3 -I asr_stub_server.py 2>stub.log &
STUB=$!
for v in RESOLVER_BACKEND FETCH_BACKEND LLM_PROVIDER STORE_BACKEND INDEX_BACKEND \
  NOTIFY_BACKEND; do export "EARMARK_$v=fake"; done
export EARMARK_ASR_BACKEND=remote EARMARK_ASR_BASE_URL=http://127.0.0.1:18089/v1 \
  EARMARK_ASR_MODEL=stub-model EARMARK_ASR_API_KEY=k-smoke-5d1e \
  EARMARK_DATA_DIR="$SCRATCH/data" EARMARK_VAULT_DIR="$SCRATCH/vault" EARMARK_LOG_LEVEL=DEBUG

$E transcribe clip.mp3                 # [00:00:00] hello / [00:00:02] from the stub
cat stub.log                           # /v1/audio/transcriptions {'model': 'stub-model', 'response_format': 'verbose_json', 'timestamp_granularities[]': 'segment', 'file': 'audio.mp3'} auth ok
$E config | grep ASR_API_KEY           # EARMARK_ASR_API_KEY=**********

# Worker: put a real file where the fake fetcher finds it, then run one job
$E submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981'
$E worker --once 2>worker.log          # first run: the fake fetcher writes audio.mp3 and uploads it
$E jobs                                # done
KEY=$(ls data/episodes)
python3 -c "import json,sys; t=json.load(open(sys.argv[1])); print(t['model'], len(t['segments']), t['warnings'])" "data/episodes/$KEY/transcript.json"
# remote:stub-model 2 []
ls data/episodes/$KEY/                 # no audio.*
grep -c k-smoke-5d1e worker.log        # 0

# Text-only server: one segment, a warning on the job
kill $STUB; rm -rf data
STUB_KEY=k-smoke-5d1e MODE=text python3 -I asr_stub_server.py 2>stub.log &
STUB=$!
$E submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981'
$E worker --once; $E jobs              # done, "warning: the ASR server returned no segment timestamps; ..."

# Transient: 503 requeues the job with the error
kill $STUB; rm -rf data
STUB_KEY=k-smoke-5d1e MODE=error503 python3 -I asr_stub_server.py 2>stub.log &
STUB=$!
$E submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981'
$E worker --once; $E jobs              # queued, attempt 1, error "remote ASR at 127.0.0.1:18089 failed: HTTP 503: warming up"
ls data/episodes/*/                    # audio.mp3 kept for the retry

# Upload cap, wrong key, missing URL
head -c 2000000 /dev/urandom > big.mp3
EARMARK_ASR_MAX_UPLOAD_MB=1 $E transcribe big.mp3; echo "exit=$?"
# expect exit=1, "big.mp3 is 1.9 MiB, larger than the 1 MiB remote ASR upload limit (EARMARK_ASR_MAX_UPLOAD_MB)"
grep -c big stub.log                   # 0: nothing was uploaded
EARMARK_ASR_API_KEY=wrong-key-1 $E transcribe clip.mp3; echo "exit=$?"   # exit=1, names EARMARK_ASR_API_KEY
EARMARK_ASR_BASE_URL= $E worker --once; echo "exit=$?"                     # exit=2, names EARMARK_ASR_BASE_URL

kill $STUB
for v in RESOLVER_BACKEND FETCH_BACKEND ASR_BACKEND LLM_PROVIDER STORE_BACKEND \
  INDEX_BACKEND NOTIFY_BACKEND ASR_BASE_URL ASR_MODEL ASR_API_KEY DATA_DIR VAULT_DIR \
  LOG_LEVEL; do unset "EARMARK_$v"; done
rm -rf "$SCRATCH/data" "$SCRATCH/vault" "$SCRATCH"/*.log "$SCRATCH"/clip.mp3 "$SCRATCH"/big.mp3
```

The first worker run uploads the fake fetcher's `audio.mp3`, which the stub does not
decode, so the job completes.

Also check:

- `git status` lists only the files in section 3 and this plan.
- No `# type: ignore`, `noqa`, or loosened lint, type, or test settings.
- `grep -rn "allow_private" src/earmark/transcribe/` finds nothing.
- `docs/ARCHITECTURE.md` matches section 8, item 7.
- `docs/asr.md` follows 4.8 and contains no secrets or real host names.

**Live check on the homelab** (not possible here): with a server from `docs/asr.md`
running, run `ASR_TEST_BASE_URL=http://spark:8000/v1 ASR_TEST_MODEL=openai/whisper-large-v3-turbo
uv run pytest -m network tests/test_remote_asr_live.py -v -rs`. The verifier lists this
as unverified.

## 8. Deviations

1. **New setting `EARMARK_ASR_TIMEOUT_SECONDS`** (default 10 800 s). The brief asks for
   "a timeout that suits hour-long audio". How long a server takes is the operator's
   hardware, from a minute on a GPU to hours on a CPU fallback, so a constant would be
   wrong for someone. It is added to `Settings`, `.env.example`, and the architecture.
2. **`ASR_MODEL` and `ASR_LANGUAGE` are shared with `remote`** rather than adding
   `ASR_REMOTE_MODEL`. One "which model transcribes" setting is simpler. The
   faster-whisper default (`large-v3`) is usually not a valid server model id; the 404
   message and `docs/asr.md` say so.
3. **Settings validation for `ASR_BASE_URL` and `ASR_API_KEY`** at load (4.1). The URL
   must be http(s) with a host and without credentials, query, or fragment, and ends in
   `/v1` by convention, the OpenAI SDK's `base_url`. The key must be printable ASCII.
4. **No private-address guard and no `fetch.py` client for the ASR endpoint** (2.4). It
   is an operator endpoint, like the Hugging Face and Anthropic endpoints, and it keeps
   the other `fetch.py` rules (no redirects, no env proxies, size cap, host-only
   messages). The architecture's Security section records this.
5. **`segment_from`/`SegmentLike` move to `transcribe/base.py`**, so both engines share
   the monotonic mapping. They are re-imported in `faster_whisper.py`; behaviour is
   unchanged.
6. **`earmark transcribe` uses the remote engine when `EARMARK_ASR_BACKEND=remote`.**
   Step 08 made it always local. Operators on the Spark need a quick way to test the
   server. Other backend values keep the local engine.
7. **Architecture edits** made with this plan:
   - Decisions: the transcription row names the Spark recommendation;
   - Module layout: `remote.py`;
   - Transcribing: a remote-engine bullet;
   - Configuration: the `ASR_*` rows, including `ASR_TIMEOUT_SECONDS`;
   - Security: operator-configured endpoints;
   - Testing: the unix-socket stub and the opt-in live test;
   - Known risk 1: the GPU path findings.
8. **No automatic fallback from `verbose_json` to `json`.** A server that rejects
   `verbose_json` (vLLM with non-Whisper models, the Parakeet NIM) fails with HTTP 400
   and its message. Retrying as `json` would hide the problem, and the servers that need
   it also reject MP3 or return no timestamps anyway.

**Decisions for the user:**

- **Which server to run on the Spark.** The plan recommends vLLM with Whisper (closest
  to the OpenAI contract, segment timestamps, an API key, maintained by NVIDIA for the
  Spark). The lighter whisper.cpp route has no auth and needs a source build. Parakeet is
  practical only through LocalAI's `parakeet-cpp`. None was run here.
- **The transcript cache has no model key** (as in steps 02 and 08). Switching from
  `faster_whisper` to `remote` does not re-transcribe episodes already cached. Delete
  `transcript.json` to redo one.

## 9. Out of scope

- Transcoding or splitting audio for servers with format or length limits (Nemotron and
  Parakeet NIMs, vLLM limits). Earmark never truncates; too large fails with a clear
  message.
- Word timestamps, diarization (`diarized_json`), streaming responses, and the
  translation endpoint.
- Building or running the ASR server, the GPU compose profile, and an ASR service in
  `compose.yaml`: step 16 (documented as untested there).
- `earmark doctor` reachability checks for `ASR_BASE_URL`: step 15.
- Re-transcribing when the model or backend changes.
- An automatic `verbose_json` → `json` fallback (deviation 8).
