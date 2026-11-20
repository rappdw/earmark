# Step 08: Transcription: faster-whisper

## 1. Outcome

`EARMARK_ASR_BACKEND=faster_whisper` (the default) works when the `transcribe` extra is
installed. The worker's transcribe stage loads a faster-whisper model on first use (once
per worker process), decodes the episode audio, transcribes it with voice-activity
filtering, maps the segments to a `Transcript` with monotonic timestamps, writes
`transcript.txt` and `transcript.json` atomically, and deletes the audio unless
`EARMARK_KEEP_AUDIO` is set. An existing `transcript.json` is returned without loading a
model. Progress is logged about once a minute during long transcriptions; the heartbeat
thread keeps beating because CTranslate2 releases the GIL. `earmark transcribe
<audio-file>` transcribes a local file and prints `[HH:MM:SS] text` lines (or JSON).

Without the extra, `import earmark`, the CLI, and the default suite work as before, and
selecting the backend is a configuration error at worker startup that names the extra.
The cache-and-cleanup shell (`transcribe/base.py`) is engine-agnostic, so step 09's
remote backend reuses it. The scheduled live workflow gains a `slow` job that installs the
extra, caches the model and test clip, and runs `pytest -m slow`.

**Architecture risk 1, in one line:** the PyPI CTranslate2 wheel for linux/aarch64 is
CPU-only, so on the DGX Spark this backend transcribes on the Grace CPU. GPU
transcription there needs step 09 (a remote GPU ASR server) or a self-built CUDA
CTranslate2; see 2.1. This step works as briefed; the decision about the default model
on the Spark is listed in section 8.

## 2. Findings

All checks ran on 2026-11-20. Package probes used a throwaway venv on the same
filesystem as the uv cache (hardlinked, so almost no disk), deleted afterwards. Probe
scripts and downloads lived in the scratchpad and were run with `python -I`.

### 2.1 Architecture risk 1: CTranslate2 on aarch64 and the DGX Spark

- **PyPI.** `ctranslate2 4.9.0` (2026-11-10) ships
  `ctranslate2-4.9.0-cp313-cp313-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl`.
  Installed here: `ctranslate2.get_cuda_device_count() == 0`,
  `get_supported_compute_types("cpu") == {"float32", "int8", "int8_float32"}`, and
  `WhisperModel("tiny", device="cuda")` raises `ValueError: This CTranslate2 package was
  not compiled with CUDA support`. Step 01 already found that the bundled
  `libctranslate2` links no CUDA library. **The aarch64 wheel is CPU-only.**
- **Upstream.** The CTranslate2 releases page (v4.7.0 to v4.9.0, 2026) says nothing
  about CUDA wheels for aarch64; v4.8.5 added *source-build* instructions for Jetson.
  Issue [OpenNMT/CTranslate2#1933](https://github.com/OpenNMT/CTranslate2/issues/1933)
  ("CUDA 13 support") is open; on 2026-11-15 the maintainer wrote "My goal is to keep
  shipping CUDA 12 and let users build CUDA 13 from source", and asked for a PR adding
  sm_100/sm_120 to the default architectures. The DGX Spark's GB10 is compute
  capability 12.1 (sm_121) and CUDA 13-era. A community fork (CTranslate2-Reborn)
  publishes CUDA 13 wheels for Windows and x86_64; sm_121 is explicitly untested there.
- **Field reports.** [speaches-ai/speaches#620](https://github.com/speaches-ai/speaches/issues/620)
  (open): the arm64 `latest-cuda` image falls back to a CPU-only CTranslate2 on DGX
  Spark and GB10; workarounds are CPU inference or a PyTorch Whisper server on an NGC
  25.03+ image. [attevon-llc/OpenTranscribe#154](https://github.com/attevon-llc/OpenTranscribe/pull/154)
  reports a source build with CUDA working on GB10 (`get_cuda_device_count() == 1`),
  pointing `libctranslate2` at pip-installed NVIDIA cuBLAS/cuDNN and installing the
  wheel with `--no-deps`. One report says CTranslate2 engines were unavailable on GB10
  without the right cuDNN. None of this was verified here (no GPU).
- **Conclusion for earmark.** On the Spark, this backend runs on CPU with the PyPI
  wheel. Options for GPU, in order of effort: (1) step 09's `remote` backend against a
  GPU ASR server that already runs on the Spark (that step researches servers, including
  Parakeet); (2) build CTranslate2 from source with CUDA for sm_121 inside the step-16
  image, then `EARMARK_ASR_DEVICE=auto` picks the GPU with no code change. The code in
  this step does not assume either: `auto` uses CUDA when CTranslate2 sees a device and
  logs that it is on CPU otherwise, and `cuda` with no visible device is a startup error
  that names step 09's alternative.
- **CPU speed here** (not representative of Grace; this sandbox is shared): `tiny`,
  int8, 10 vCPUs: the 11 s clip in 1.3 s; a 156 s synthetic file (the clip repeated
  with silence) in 48 s. `cpu_threads=10` was slower than the default here. `large-v3`
  is about 40 times the parameters of `tiny`; on any CPU it will be much slower than real
  time for long episodes. No `large-v3` number was measured.

### 2.2 faster-whisper 1.2.1 and its dependencies

- Current releases: `faster-whisper 1.2.1` (2025-10-31, pure `py3-none-any`; requires
  `ctranslate2>=4.0,<5`, `huggingface-hub>=0.21`, `tokenizers>=0.13,<1`,
  `onnxruntime>=1.14,<2`, `av>=11`, `tqdm`). All dependencies have cp313 or abi3
  manylinux aarch64 wheels: ctranslate2 4.9.0, av (see below), onnxruntime 1.32.0,
  tokenizers 0.23.3 (abi3), numpy 2.5.5, huggingface-hub 2.4.0 (pure; it now uses
  `httpx2<3`, compatible with the lock's 2.16.2), hf-xet 1.8.0.
- **Dead end: `av` 19 breaks faster-whisper 1.2.1.** With `av 19.0.2` (the newest),
  every transcription fails in `faster_whisper/audio.py` with `TypeError: open() got an
  unexpected keyword argument 'metadata_errors'`. PyAV's changelog for v19.0.0: "Remove
  the `metadata_encoding` and `metadata_errors` arguments to `av.open`". faster-whisper's
  `master` already works around it (`**_AV_OPEN_KWARGS`) but no release has it. With
  `av 18.1.0` (`av-18.1.0-cp311-abi3-manylinux_2_28_aarch64.whl`) everything works.
  **The extra must pin `av>=18.1,<19`** until a faster-whisper release after 1.2.1.
- A scratch copy of the project with
  `transcribe = ["faster-whisper>=1.2.1", "av>=18.1,<19"]` locked cleanly
  (`required-environments` satisfied) and added exactly: av 18.1.0, ctranslate2 4.9.0,
  faster-whisper 1.2.1, filelock, flatbuffers, fsspec, hf-xet, huggingface-hub 2.4.0,
  numpy 2.5.5, onnxruntime 1.32.0, protobuf, tokenizers 0.23.3, tqdm. Nothing else in
  the lock changed.
- No `py.typed` or stubs in `faster_whisper` or `ctranslate2`. A static `import
  faster_whisper` would make mypy's result depend on whether the extra is installed
  (`import-not-found` without it, `import-untyped` with it). The plan therefore imports
  through `importlib.import_module` (returns `types.ModuleType`, whose attributes are
  `Any`) and types what it uses with local Protocols. No mypy override is needed.
- `import faster_whisper` takes about 0.1 s and loads `ctranslate2`, `av`, `numpy`,
  `tokenizers`, `huggingface_hub`; it is cheap enough to do when the worker builds its
  backends, which catches a broken install at startup.

### 2.3 API shapes used (inspected with `inspect.signature` on 1.2.1)

- `WhisperModel(model_size_or_path, device="auto", device_index=0,
  compute_type="default", cpu_threads=0, num_workers=1, download_root=None,
  local_files_only=False, files=None, revision=None, use_auth_token=None)`.
  `model_size_or_path` is a size from `faster_whisper.available_models()` (`tiny`,
  `base`, `small`, `medium`, `large-v1/v2/v3`, `large`, `distil-*`, `large-v3-turbo`,
  `turbo`, and `.en` variants), a Hugging Face repo id containing `/`, or a local
  directory. `download_root` becomes `huggingface_hub`'s `cache_dir`.
- `model.transcribe(audio, language=None, ..., beam_size=5, ..., vad_filter=False, ...)`
  accepts a path or a float32 numpy array and returns `(segments, info)` where
  `segments` is a **lazy generator**: the work happens while it is iterated. `Segment`
  has `id, seek, start, end, text, tokens, avg_logprob, compression_ratio,
  no_speech_prob, words, temperature`; `text` has a leading space. `TranscriptionInfo`
  has `language, language_probability, duration, duration_after_vad, ...`.
- `faster_whisper.decode_audio(input_file, sampling_rate=16000, split_stereo=False)`
  returns a float32 mono array via PyAV, which bundles its own FFmpeg libraries, so no
  system ffmpeg is needed. Re-encoding the clip to m4a (AAC), mp3, and ogg (Opus) with
  PyAV and transcribing each worked. Passing the decoded array to `transcribe` gives
  `info.duration == len(samples) / 16000`.
- `model.model.device` and `model.model.compute_type` report what was chosen:
  `device="auto", compute_type="auto"` gave `cpu`, `int8_float32` here.
- Compute types on CPU: `auto`, `default`, `int8`, `int8_float32`, `float32` load;
  `int8_float16`, `int8_bfloat16`, `int16`, `float16`, `bfloat16` raise `ValueError:
  Requested <t> compute type, but the target device or backend do not support efficient
  ...`; `float8`, `int4` raise `ValueError: Invalid compute type`. Devices `auto`, `cpu`
  (case-insensitive) work; `gpu` raises `ValueError: unsupported device gpu`.
- `language="xx"` or `"EN"` raises `ValueError: 'xx' is not a valid language code
  (accepted language codes: af, am, ...)` when `transcribe` is called. Codes are lower
  case, two or three letters (`haw`, `yue`).

### 2.4 Behaviour observed with the `tiny` model on CPU

- The 11 s clip (2.5): one segment `(0.0, 11.0, ' And so, my fellow Americans, ask not
  what your country can do for you, ask what you can do for your country.')`, language
  `en` (p = 0.98), with and without VAD.
- 20 s of silence: with `vad_filter=True` no segments; with `vad_filter=False` one
  hallucinated segment `' You'`. VAD (Silero, via onnxruntime, bundled in the wheel)
  works on aarch64 and is worth turning on for podcasts (music beds, silence).
- **GIL.** A thread waking every 50 ms while the main thread transcribed 156 s of audio
  saw a largest gap of 0.11 s. CTranslate2 releases the GIL, so the worker's heartbeat
  thread is not starved.
- **Signals.** A `SIGALRM` handler raising a `BaseException` while iterating the
  segment generator interrupted it about 1.1 s later (after the current window's
  decode). So the worker's `Shutdown` reaches a transcription within seconds.
- Segments in that run were monotonic; `end >= start` held. The mapping still enforces
  it (5.3), since long-form Whisper output is known to regress occasionally.
- Decode failures: a non-audio file and an empty file both raise
  `av.error.InvalidDataError` (MRO: `InvalidDataError, FFmpegError, ValueError,
  Exception`) whose `str()` contains the full path; `exc.strerror` is `Invalid data
  found when processing input`.

### 2.5 Model download and the test clip

- `Systran/faster-whisper-tiny` (MIT, revision `d90ca5fe…`): `model.bin` 75.5 MB,
  `tokenizer.json` 2.2 MB, `vocabulary.txt` 0.46 MB, `config.json`. About 78 MB on
  disk. Downloading it here worked: the sandbox's intercepting DNS (risk 5) points at a
  proxy that forwards to the real host. huggingface_hub 2.x placed `model.bin` in a
  shared blob store under the cache dir (`blobs/95/…`) with a symlink from the repo's
  `blobs/`; that is its own business and works.
- **Offline behaviour.** With `HF_ENDPOINT=http://127.0.0.1:9`: a cached model loads
  in 0.2 s (huggingface_hub falls back to the local snapshot). An empty cache raises
  `huggingface_hub.errors.LocalEntryNotFoundError` (MRO: `LocalEntryNotFoundError,
  FileNotFoundError, OSError, EntryNotFoundError, Exception`), cause
  `httpx2.ConnectError`. Against the real endpoint, a missing repo raises
  `RepositoryNotFoundError` (MRO: `RepositoryNotFoundError, HfHubHTTPError,
  httpx2.HTTPError, OSError, Exception`) with a message containing the API URL and a
  request id. `RevisionNotFoundError`, `GatedRepoError` (a `RepositoryNotFoundError`),
  and `DisabledRepoError` are `HfHubHTTPError`s too. So: not-found classes are
  permanent, every other `OSError` from loading is a transient download problem, and
  messages must not copy the exception text (URLs, request ids).
- huggingface_hub reads `HF_HOME` when it is imported, so an environment variable set
  later in the process has no effect. An explicit `download_root` is the reliable way
  to choose the cache, hence the new `EARMARK_ASR_MODEL_DIR` (4.1).
- **Test clip.** `tests/data/jfk.flac` from the faster-whisper repository at tag
  `v1.2.1` (commit `65882eee9f5cdbeeb2d877f1131d48cf241b327d`):
  `https://raw.githubusercontent.com/SYSTRAN/faster-whisper/65882eee9f5cdbeeb2d877f1131d48cf241b327d/tests/data/jfk.flac`,
  1 152 693 bytes, sha256
  `63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715`, served as
  `audio/flac`. It is 11 s of John F. Kennedy's inaugural address (1961), a work of the
  US federal government made in the course of official duties and therefore in the
  public domain (17 U.S.C. § 105). The slow test downloads it (through
  `GuardedFetcher`) into the test cache instead of committing a 1.1 MB binary. The
  other files in that directory have no stated licence and are not used.
- Model weights are CTranslate2 `model.bin` plus JSON and text; nothing is unpickled,
  so loading a model runs no code from the download.

### 2.6 actions/cache and setup-uv

- `actions/cache` latest release `v6.1.0` (2026-06-26), tag commit
  `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` (lightweight tag; `v6` points at the same
  commit). Inputs used: `path`, `key`. GitHub evicts caches not accessed for 7 days, so
  a weekly schedule may often miss; the download is 79 MB, so a miss costs little.
- `astral-sh/setup-uv` at the pinned `68559e99…` (v10.3.3) has a `cache-suffix` input
  ("Suffix for the cache key"). The slow job sets `cache-suffix: transcribe` so its uv
  cache (with the extra's wheels) does not collide with the network job's, which uses
  the same `uv.lock` hash.
- `uv run --help`: `--exact` performs an exact sync; without it `uv run` does not remove
  extras, but the slow job passes `--extra transcribe` to `uv run` as well so the
  environment it checks matches the one it installed.

### 2.7 Lint and formatting checks

- `ruff check --select BLE` accepts `except Exception as exc: raise X(...) from exc`
  (probe file with the project config).
- `uv run ruff format --check docs/` formats the Python blocks in Markdown; this plan's
  snippets were checked with it.

### 2.8 Disk in this sandbox

`/home/sandy` (tmpfs, 2 GB) has about 1.2 GB free; the uv cache is about 900 MB and
already holds every wheel the extra needs (ctranslate2 4.9.0, av 18.1.0, onnxruntime,
numpy, tokenizers, huggingface-hub 2.4.0, hf-xet). `uv sync --extra transcribe`
hardlinks from the cache into `.venv` (same filesystem), so it costs a few MB. `/tmp`
has about 1 GB free. Section 7 puts the model and clip in the scratchpad (on `/tmp`),
about 80 MB.

## 3. Files

| File | Change |
|---|---|
| `pyproject.toml` | Fill the `transcribe` extra: `"faster-whisper>=1.2.1"`, `"av>=18.1,<19"` with a comment pointing at 2.2. Then `uv lock`. |
| `uv.lock` | Regenerated; only the packages listed in 2.2 are added. |
| `src/earmark/config.py` | `AsrDevice` and `AsrComputeType` Literals; `asr_device`, `asr_compute_type` retyped; validators for `asr_model`, `asr_device`, `asr_compute_type`, `asr_language`; new `asr_model_dir`. |
| `.env.example` | Add `# EARMARK_ASR_MODEL_DIR=` in the Transcription block. |
| `src/earmark/transcribe/__init__.py` | New, docstring only. |
| `src/earmark/transcribe/base.py` | New. `TranscriptionEngine` Protocol, `UnusableAudioError`, `CachedTranscriber` (the `Transcriber` shell: cache, write, delete audio). Light imports only. |
| `src/earmark/transcribe/faster_whisper.py` | New. Protocols for the model, `WhisperOptions`, `WhisperRuntime`, `DecodedAudio`, `MissingExtraError`, `load_runtime`, `check_device`, `segment_from`, `FasterWhisperEngine`, `build_faster_whisper_transcriber`, `file_key`. No heavy import at module top. |
| `src/earmark/pipeline/factory.py` | Register `"faster_whisper"` in `TRANSCRIBERS` with a lazy builder. |
| `src/earmark/cli.py` | New `transcribe AUDIO_FILE [--json]` command. |
| `tests/whisper_fakes.py` | New helper module (not collected): `FakeSegment`, `FakeInfo`, `FakeWhisperModel`, `FakeRuntime`. |
| `tests/slow_assets.py` | New helper module: clip URL, size, sha256, licence note, cache directory, `fetch_clip()`. |
| `tests/test_transcribe_base.py` | New. `CachedTranscriber`. |
| `tests/test_faster_whisper.py` | New. Engine, options, runtime, builder, pipeline integration. |
| `tests/test_faster_whisper_slow.py` | New. `@pytest.mark.slow` tests with the real `tiny` model. |
| `tests/test_factory.py` | Adjust tests that treat `faster_whisper` as unbuilt (6.3). |
| `tests/test_cli.py` | Adjust `test_worker_unbuilt_backend_exits_2`; add `transcribe` and missing-extra tests. |
| `tests/test_config.py` | Add invalid-value cases and normalisation tests for the ASR settings. |
| `tests/test_ci.py` | Network-workflow tests select jobs by id; new tests for the `slow` job. |
| `.github/workflows/network.yml` | Rename to "Live tests"; add the `slow` job (4.8). |
| `docs/ARCHITECTURE.md` | See section 8, item 9. |

Dependencies added (extra only): `faster-whisper` (the brief's engine; CTranslate2-based,
int8 on CPU, CUDA when available; the alternatives, `openai-whisper` and `whisper.cpp`
bindings, need PyTorch or a native build and are not what the architecture chose) and a
direct `av` bound, which exists only to keep PyAV below 19 (2.2). No new runtime
dependency for the default install.

## 4. Interfaces

### 4.1 Settings (`earmark.config`)

```python
AsrDevice = Literal["auto", "cpu", "cuda"]
AsrComputeType = Literal[
    "auto",
    "default",
    "int8",
    "int8_float32",
    "int8_float16",
    "int8_bfloat16",
    "int16",
    "float16",
    "bfloat16",
    "float32",
]
```

| Field | Type | Default | Validation |
|---|---|---|---|
| `asr_model` | `str` | `"large-v3"` | stripped; empty after stripping → error `must not be empty` |
| `asr_device` | `AsrDevice` | `"auto"` | `mode="before"`: strings stripped and lower-cased (like `log_level`) |
| `asr_compute_type` | `AsrComputeType` | `"auto"` | same |
| `asr_language` | `str \| None` | `None` | stripped, lower-cased; `"auto"` → `None`; otherwise must `re.fullmatch("[a-z]{2,3}")`, else `must be a language code such as 'en', or 'auto'` |
| `asr_model_dir` | `Path \| None` | `None` | new. Description: "Where Whisper models are downloaded (default: the Hugging Face cache)." |

Errors go through the existing `load_settings` path, so messages name the variable and
never echo the value. `.env.example` gains `# EARMARK_ASR_MODEL_DIR=` after
`EARMARK_ASR_COMPUTE_TYPE=auto`; the existing values stay valid.

### 4.2 `earmark.transcribe.base`

```python
class TranscriptionEngine(Protocol):
    @property
    def name(self) -> str: ...  # e.g. "faster-whisper:large-v3", for logs

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript: ...


class UnusableAudioError(PermanentError):
    """The audio cannot be transcribed: undecodable, empty, or without speech."""


class CachedTranscriber:
    def __init__(
        self, artifacts: ArtifactStore, engine: TranscriptionEngine, *, keep_audio: bool
    ) -> None: ...

    @property
    def engine(self) -> TranscriptionEngine: ...

    @property
    def keep_audio(self) -> bool: ...

    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript: ...
```

`CachedTranscriber` satisfies the `Transcriber` Protocol. It imports only `earmark`
light modules (`artifacts`, `errors`, `models`). Behaviour in 5.1.

### 4.3 `earmark.transcribe.faster_whisper`: constants and errors

```python
EXTRA: Final = "transcribe"
INSTALL_HINT: Final = "install it with `uv sync --extra transcribe`"
SAMPLE_RATE: Final = 16_000
BEAM_SIZE: Final = 5
VAD_FILTER: Final = True
PROGRESS_INTERVAL_S: Final = 60.0
MAX_DETAIL_CHARS: Final = 200
NOT_FOUND_ERRORS: Final = frozenset(
    {"RepositoryNotFoundError", "RevisionNotFoundError", "GatedRepoError", "DisabledRepoError"}
)

type Monotonic = Callable[[], float]


class MissingExtraError(ConfigError):
    """faster-whisper (the `transcribe` extra) is not installed or does not import."""
```

`MissingExtraError` is built with one line:
`EARMARK_ASR_BACKEND: backend 'faster_whisper' needs the optional 'transcribe' extra,
which is not installed; install it with `uv sync --extra transcribe``. When the import
fails for another reason than absence (the module exists but raises `ImportError` while
importing, e.g. a missing shared library), the line is `... needs the optional
'transcribe' extra, which failed to import ({type(exc).__name__}); reinstall it with
`uv sync --extra transcribe``. Both name `EARMARK_ASR_BACKEND`, the extra, and the
command.

### 4.4 Model and runtime types

```python
class SegmentLike(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def text(self) -> str: ...


class InfoLike(Protocol):
    @property
    def language(self) -> str: ...

    @property
    def duration(self) -> float: ...


class WhisperModelLike(Protocol):
    def transcribe(
        self, audio: object, /, *, language: str | None, beam_size: int, vad_filter: bool
    ) -> tuple[Iterable[SegmentLike], InfoLike]: ...


@dataclass(frozen=True, slots=True)
class WhisperOptions:
    model: str
    device: str
    compute_type: str
    language: str | None
    model_dir: Path | None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self: ...


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    samples: object  # a float32 numpy array at SAMPLE_RATE, opaque to earmark
    duration_s: float


@dataclass(frozen=True, slots=True)
class WhisperRuntime:
    load_model: Callable[[WhisperOptions], WhisperModelLike]
    decode_audio: Callable[[Path], DecodedAudio]
    cuda_device_count: Callable[[], int]


def load_runtime() -> WhisperRuntime: ...


def check_device(options: WhisperOptions, runtime: WhisperRuntime) -> None: ...
```

- `WhisperOptions.from_settings` copies `asr_model`, `asr_device`, `asr_compute_type`,
  `asr_language`, `asr_model_dir` (already normalised by `Settings`).
- `load_runtime()` does `fw = importlib.import_module("faster_whisper")` and
  `ct2 = importlib.import_module("ctranslate2")`. A `ModuleNotFoundError` whose `name` is
  `"faster_whisper"` or `"ctranslate2"` gives the "not installed" `MissingExtraError`
  (checked on Python 3.13: both an absent module and `sys.modules[name] = None` raise
  `ModuleNotFoundError` with `name` set to that module). Any other `ImportError`
  (including a `ModuleNotFoundError` for a dependency such as `av`) gives the "failed to
  import" form. Raise with `from None`. It returns a runtime whose
  - `load_model(o)` is `fw.WhisperModel(o.model, device=o.device,
    compute_type=o.compute_type, download_root=None if o.model_dir is None else
    str(o.model_dir))`, assigned to a variable annotated `WhisperModelLike` before it is
    returned (the module attribute is `Any`; returning it directly trips
    `warn_return_any`);
  - `decode_audio(path)` is `samples = fw.decode_audio(str(path),
    sampling_rate=SAMPLE_RATE)`, returned as `DecodedAudio(samples, duration_s=
    len(samples) / SAMPLE_RATE)`;
  - `cuda_device_count()` is `int(ct2.get_cuda_device_count())`.
  The functions are closures or small private functions in the module; no
  `faster_whisper` name appears at module top.
- `check_device`: if `options.device == "cuda"` and `runtime.cuda_device_count() == 0`,
  raise `ConfigError(["EARMARK_ASR_DEVICE: 'cuda' was requested but CTranslate2 sees no
  CUDA device (the PyPI wheels for linux/aarch64 are CPU-only); use 'auto' or 'cpu',
  or EARMARK_ASR_BACKEND=remote"])`. Otherwise nothing.

### 4.5 `segment_from` and `FasterWhisperEngine`

```python
def segment_from(
    raw: SegmentLike, *, previous_start: float, duration_s: float
) -> Segment | None: ...


class FasterWhisperEngine:
    def __init__(
        self,
        options: WhisperOptions,
        runtime: WhisperRuntime,
        *,
        monotonic: Monotonic = time.monotonic,
        progress_interval_s: float = PROGRESS_INTERVAL_S,
    ) -> None: ...

    @property
    def name(self) -> str: ...  # f"faster-whisper:{options.model}"

    @property
    def options(self) -> WhisperOptions: ...

    @property
    def model_loaded(self) -> bool: ...

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript: ...


def build_faster_whisper_transcriber(
    settings: Settings, artifacts: ArtifactStore, *, runtime: WhisperRuntime | None = None
) -> CachedTranscriber: ...


def file_key(path: Path) -> str: ...
```

- `build_faster_whisper_transcriber`: `options = WhisperOptions.from_settings(settings)`;
  `runtime = runtime or load_runtime()` (looked up as a module global at call time, so
  tests can monkeypatch `earmark.transcribe.faster_whisper.load_runtime`);
  `check_device(options, runtime)`; return
  `CachedTranscriber(artifacts, FasterWhisperEngine(options, runtime),
  keep_audio=settings.keep_audio)`. It loads no model.
- `file_key(path)`: `episode_key("local", "file", sha256(file bytes).hexdigest())`,
  reading in 1 MiB chunks. Used only by `earmark transcribe`.

### 4.6 Factory

```python
def _faster_whisper_transcriber(ctx: BuildContext) -> Transcriber:
    from earmark.transcribe.faster_whisper import build_faster_whisper_transcriber

    return build_faster_whisper_transcriber(ctx.settings, ctx.artifacts)


TRANSCRIBERS: Final[Mapping[str, Builder[Transcriber]]] = {
    "fake": lambda ctx: FakeTranscriber(ctx.artifacts),
    "faster_whisper": _faster_whisper_transcriber,
}
```

`MissingExtraError` and the `check_device` `ConfigError` raised from the builder
propagate out of `build_backends` and `build_worker`. Builders run after the registry
lookups and before the database is opened, so `earmark worker` exits 2 with the message
and creates nothing (the existing CLI handler already catches `ConfigError`).

### 4.7 CLI: `earmark transcribe AUDIO_FILE [--json]`

```python
@app.command("transcribe")
def transcribe_command(
    audio: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Local audio file."),
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Print the Transcript as JSON.")] = False,
) -> None: ...
```

1. `_load_or_exit()`, `configure_logging(...)`. Import `earmark.transcribe.faster_whisper
   as fw` inside the function and call `fw.load_runtime()` through the module (so tests
   can patch it).
2. `options = fw.WhisperOptions.from_settings(settings)`; `runtime = fw.load_runtime()`;
   `fw.check_device(options, runtime)`. `ConfigError` → `str(exc)` on stderr, exit 2.
3. `transcript = fw.FasterWhisperEngine(options, runtime).transcribe_file(audio,
   fw.file_key(audio))`. `TransientError` → stderr `earmark transcribe: {msg} (temporary;
   try again later)`, exit 1; `PermanentError` (including `UnusableAudioError`) →
   `earmark transcribe: {msg}`, exit 1. Messages pass through the CLI's `_clean`.
4. stdout: with `--json`, `transcript.model_dump_json(indent=2)`; otherwise each line of
   `transcript.to_text()` through `_clean`, one `typer.echo` per line.

It always uses the local faster-whisper engine with the `EARMARK_ASR_*` settings,
whatever `EARMARK_ASR_BACKEND` says. It writes nothing under `DATA_DIR`, never deletes
or modifies the input file, and writes only the model cache. Progress logs go to stderr.
A missing or unreadable path is rejected by click (exit 2) before settings load matter.

### 4.8 Workflow `.github/workflows/network.yml`

Header comment updated (two opt-in jobs, not required checks). `name: Live tests`.
Triggers, `permissions: contents: read`, and `concurrency` unchanged. Job `network`
unchanged. New job, after it:

```yaml
  slow:
    name: pytest -m slow (linux/arm64)
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 20
    steps:
      - name: Check out
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Install uv
        uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
        with:
          # Same minor as the uv_build bound in pyproject.toml; bump both together.
          version: ">=0.13.9,<0.14"
          enable-cache: true
          cache-dependency-glob: uv.lock
          prune-cache: true
          cache-python: true
          cache-suffix: transcribe
      - name: Cache the Whisper model and test clip
        uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
        with:
          path: ~/.cache/earmark-tests
          key: slow-assets-${{ hashFiles('tests/slow_assets.py') }}
      - name: Install dependencies with the transcribe extra
        run: uv sync --locked --extra transcribe
      - name: Slow tests
        run: uv run --locked --extra transcribe pytest -m slow -v -rs
```

No `env`, no secrets. The test cache directory default (`~/.cache/earmark-tests`)
matches `path`, so no environment variable is needed.

### 4.9 Test helpers

`tests/whisper_fakes.py`:

```python
@dataclass(frozen=True)
class FakeSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class FakeInfo:
    language: str
    duration: float


class FakeWhisperModel:
    def __init__(
        self,
        segments: Sequence[FakeSegment],
        info: FakeInfo,
        *,
        on_segment: Callable[[int], None] | None = None,
        transcribe_error: Exception | None = None,
    ) -> None: ...

    calls: list[dict[str, object]]  # audio, language, beam_size, vad_filter per call

    def transcribe(
        self, audio: object, /, *, language: str | None, beam_size: int, vad_filter: bool
    ) -> tuple[Iterable[FakeSegment], FakeInfo]: ...  # returns a generator


class FakeRuntime:
    """Builds a WhisperRuntime; counts loads and decodes; can fail either."""

    def __init__(
        self,
        model: FakeWhisperModel,
        *,
        duration_s: float = 60.0,
        cuda_devices: int = 0,
    ) -> None: ...

    loads: list[WhisperOptions]
    decodes: list[Path]
    load_errors: list[BaseException]  # raised in order by load_model, then normal
    decode_error: BaseException | None

    def runtime(self) -> WhisperRuntime: ...
```

`FakeWhisperModel.transcribe` returns `(generator, info)`; the generator calls
`on_segment(i)` before yielding segment `i`, which lets tests advance clocks, wait for
heartbeats, or send signals mid-transcription. `FakeRuntime.decode_audio` returns
`DecodedAudio(samples=("fake", path.name), duration_s=duration_s)`.

`tests/slow_assets.py`:

```python
CLIP_URL: Final = (
    "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/"
    "65882eee9f5cdbeeb2d877f1131d48cf241b327d/tests/data/jfk.flac"
)
CLIP_SHA256: Final = "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715"
CLIP_SIZE: Final = 1_152_693
# JFK inaugural address, 20 January 1961: a US federal government work, public domain
# (17 U.S.C. 105). Copied in faster-whisper's tests at tag v1.2.1.
SLOW_MODEL: Final = "tiny"
CACHE_ENV: Final = "SLOW_TEST_CACHE"
DEFAULT_CACHE_DIR: Final = Path.home() / ".cache" / "earmark-tests"


def cache_dir() -> Path: ...  # $SLOW_TEST_CACHE if set, else DEFAULT_CACHE_DIR


def model_dir() -> Path: ...  # cache_dir() / "whisper"


def fetch_clip() -> Path: ...  # cache_dir() / "clips" / "jfk.flac", verified
```

`fetch_clip()`: if the file exists with the right sha256, return it. Otherwise
`GuardedFetcher(allow_private=dns_intercepted())` (from `tests.http_helpers`; the
sandbox's DNS needs it, as in the `network` tests) `.download(CLIP_URL, lambda _ext:
target, max_bytes=2 * MIB, purpose="test clip")`, then check size and sha256; on a
mismatch delete the file and `pytest.fail` with both digests. The variable is not
`EARMARK_`-prefixed because `tests/conftest.py` strips those per test.

## 5. Behaviour and edge cases

### 5.1 `CachedTranscriber.transcribe(episode, audio)`

1. `cached = artifacts.read_transcript(episode.key)`. If not `None`: unless
   `keep_audio`, delete the episode's audio (step 6 rules); return `cached`. The engine
   is not called, so no model is loaded and no audio is decoded. (The runner already
   skips the stage when `transcript.json` exists; this path covers direct calls and a
   crash between writing the transcript and deleting the audio.)
2. `if not audio.is_file(): raise PermanentError(f"audio file missing: {audio.name}")`.
3. `transcript = engine.transcribe_file(audio, episode.key)`. On `UnusableAudioError`:
   unless `keep_audio`, delete the episode's audio, then re-raise. Any other exception
   (including `TransientError` and `BaseException`s such as the worker's `Shutdown`)
   propagates and the audio is kept for the retry.
4. If `transcript.episode_key != episode.key`: `PermanentError(f"{engine.name} returned
   a transcript for another episode")`.
5. `artifacts.write_transcript(transcript)`: `transcript.txt`, then `transcript.json`,
   each through `atomic_write_text` (temp file in the same directory, `fsync`, rename).
   `transcript.json` is the cache marker and is written last, so a crash between the two
   leaves no cache hit. A failure here propagates; the audio is kept.
6. Unless `keep_audio`: delete the audio.
7. Log INFO `transcript for %s written (%d segments)`; return `transcript`.

Deleting audio: `artifacts.delete_audio(episode.key)` (every `audio.<ext>` in the
episode directory; `.part` files are the fetcher's). An `OSError` is logged as a WARNING
with the key and swallowed: the transcript is the product, and a leftover file is only
disk. The `audio` argument itself is never deleted directly, so a path outside the
episode directory is never touched.

### 5.2 `FasterWhisperEngine.transcribe_file(audio, episode_key)`

1. **Model.** If not loaded: under a `threading.Lock`, log INFO `loading Whisper model
   %r (device %s, compute type %s)`, call `runtime.load_model(options)`, keep it, log
   INFO `loaded Whisper model %r in %.1f s on %s (%s)` with the actual device and
   compute type read with `getattr(getattr(model, "model", None), "device", "?")` (and
   the same for `compute_type`). If `options.device == "auto"` and
   `runtime.cuda_device_count() == 0`, also log INFO `CTranslate2 sees no CUDA device;
   transcribing on CPU`. A failed load leaves nothing cached; the next call tries again.
   Load errors map as in 5.4.
2. **Decode.** `decoded = runtime.decode_audio(audio)`. `MemoryError` propagates.
   Any other `Exception` → `UnusableAudioError(f"could not decode {audio.name}:
   {detail}")` from the exception, where `detail` is `exc.strerror` when it is a
   non-empty `str`, else `type(exc).__name__`, whitespace-collapsed and capped at
   `MAX_DETAIL_CHARS`. The full path is never in the message (PyAV puts it in `str()`).
   `decoded.duration_s <= 0` → `UnusableAudioError(f"{audio.name} contains no audio")`.
3. **Transcribe.** `segments, info = model.transcribe(decoded.samples,
   language=options.language, beam_size=BEAM_SIZE, vad_filter=VAD_FILTER)`. A
   `ValueError` from this call → `PermanentError(f"transcription failed: {detail}")`
   where `detail` is `str(exc)` collapsed and capped, plus ` (check
   EARMARK_ASR_LANGUAGE)` when `options.language` is set. `duration =
   float(info.duration)` if finite and positive, else `decoded.duration_s`.
4. Log INFO `transcribing %s: %s of audio with %s` (key, `format_hms(duration)`,
   `name`).
5. **Iterate** the generator. For each raw segment: `seg = segment_from(raw,
   previous_start=prev, duration_s=duration)`; if not `None`, append and set `prev =
   seg.start`. After each raw segment, if `monotonic() - last_log >=
   progress_interval_s`: log INFO `transcribing %s: %s of %s (%d%%)` (key,
   `format_hms(seg_end)`, `format_hms(duration)`, percent floored and capped at 100),
   and reset `last_log`. `last_log` starts at the time the iteration starts. A
   `ValueError` raised while iterating maps as in step 3; other exceptions propagate
   (an unexpected CTranslate2 `RuntimeError`, such as CUDA running out of memory, is
   then a transient failure with a traceback, by the runner's rule).
6. No segments → `UnusableAudioError(f"no speech found in {audio.name}
   ({format_hms(duration)} of audio)")`.
7. Return `Transcript(episode_key=episode_key, language=options.language or
   info.language or None, duration_s=duration, model=name, segments=tuple(segments))`
   with no warnings. Log INFO `transcribed %s: %d segments, language %s, %.1f s (%.2fx
   real time)`.

The engine never writes files and never deletes anything; that is `CachedTranscriber`'s
job. It is used from one thread at a time (the worker runs one job at a time); the lock
only guards the load.

### 5.3 `segment_from`: monotonic timestamps

With `D = duration_s if duration_s > 0 else math.inf`:

1. `text = " ".join(raw.text.split())`. Empty → return `None` (the previous start is
   unchanged).
2. `start = raw.start if math.isfinite(raw.start) else previous_start`;
   `start = min(max(start, previous_start, 0.0), D)`.
3. `end = raw.end if math.isfinite(raw.end) else start`; `end = min(max(end, start), D)`.
4. Return `Segment(start=start, end=end, text=text)`.

So starts never decrease, `end >= start`, and every time is within `[0, D]`, which also
keeps timestamps inside `duration_s` for step 10's range check. Overlap with the previous
segment's end is allowed (Whisper windows can overlap slightly); it is not an error.
Adjustments are silent (no warning): they are small model noise, not data loss.

### 5.4 Load error mapping

Applied to exceptions from `runtime.load_model(options)`, in this order:

| Exception | Result |
|---|---|
| any class in its MRO named in `NOT_FOUND_ERRORS` | `PermanentError(f"Whisper model {model!r} was not found on Hugging Face (EARMARK_ASR_MODEL)")` |
| `ValueError` | `PermanentError(f"cannot load Whisper model {model!r}: {detail} (check EARMARK_ASR_MODEL, EARMARK_ASR_DEVICE, EARMARK_ASR_COMPUTE_TYPE)")`, `detail` = first line of `str(exc)`, capped |
| `OSError` (includes `LocalEntryNotFoundError`, other `HfHubHTTPError`s, connection errors, a full disk) | `TransientError(f"could not download or open Whisper model {model!r}: {type(exc).__name__}")` |
| anything else | propagates unchanged |

Matching by class name keeps `huggingface_hub` out of the import graph of tests. `model`
is `options.model` shortened to 100 characters. The exception text of `OSError`s is not
copied (it carries URLs and request ids). All mapped errors chain with `from exc`.

### 5.5 Interruption

`Shutdown` (a `BaseException`) raised by the worker's signal handler surfaces from the
segment iterator within about a second (2.4). Nothing has been written, the audio is
kept, and the loaded model stays loaded. The worker releases the job (step 05). A
`KeyboardInterrupt` in `earmark transcribe` behaves the same and exits.

### 5.6 Heartbeat and progress

The heartbeat thread (step 05) beats every `min(60 s, lease / 4)` on its own; it needs
only that the transcription does not hold the GIL, which 2.4 confirmed for CTranslate2.
The progress log lines are for the operator watching a long job, and they show the stage
is moving. Nothing in this step calls `jobs.heartbeat` itself.

### 5.7 Memory and long episodes

`decode_audio` holds the whole episode as float32 at 16 kHz: about 230 MB per hour of
audio (690 MB for three hours). `MAX_AUDIO_MB` (1 GiB by default) bounds the download,
not the decoded size; a 1 GiB low-bitrate file could decode to several GB. This is a
documented limitation, not handled here.

### 5.8 Messages and logs

Job errors from this stage name the file by its base name (`audio.mp3`), the model by
its configured name, and settings by variable name. They never contain a full path, a
URL, or exception text from huggingface_hub. Transcript text is never logged.
CTranslate2 prints some warnings straight to stderr (spdlog, bypassing Python logging);
they contain no secrets.

## 6. Tests

All default-suite tests use `FakeRuntime`/`FakeWhisperModel`; none needs the extra,
sockets, or a GPU. Tests that set `sys.modules["faster_whisper"] = None` use
`monkeypatch.setitem` so it is undone. Episodes come from `FakeResolver().resolve(CANON)`
(and a second URL for a second episode); audio files are a few bytes written to
`artifacts.audio_path(key, "mp3")`, since decoding is faked.

### 6.1 Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | `import earmark` and the default suite work without the extra; selecting the backend without it gives an error naming the extra | Existing `test_cli.py::test_import_earmark_is_light`, `test_cli_import_is_light`, `test_models.py::test_contract_modules_import_light`, `test_factory.py::test_pipeline_modules_import_light` keep passing. New `test_faster_whisper.py::test_transcribe_modules_import_light` (subprocess: `import earmark.transcribe.base, earmark.transcribe.faster_whisper, earmark.pipeline.factory, earmark.cli`; none of `faster_whisper`, `ctranslate2`, `av`, `huggingface_hub`, `numpy`, `onnxruntime`, `tokenizers` in `sys.modules`). `test_load_runtime_missing_extra`, parametrized over `faster_whisper` and `ctranslate2` set to `None` in `sys.modules`: `MissingExtraError`, an instance of `ConfigError`, one line starting `EARMARK_ASR_BACKEND: ` and containing `'transcribe' extra`, `not installed`, and `uv sync --extra transcribe`. `test_load_runtime_broken_extra`, parametrized: `importlib.import_module` monkeypatched with a wrapper that, for `faster_whisper`, raises `ImportError("libfoo.so: cannot open shared object file")` or `ModuleNotFoundError("No module named 'av'", name="av")` and otherwise delegates: the "failed to import" form naming the exception class, and neither `libfoo` nor the delegated text in the message. `test_factory_missing_extra_is_config_error` (`build_backends` with `asr_backend="faster_whisper"`, others fake, extra hidden: `ConfigError` with that line; data dir not created). `test_cli.py::test_worker_missing_extra_exits_2` (`EARMARK_ASR_BACKEND=faster_whisper`, others fake, extra hidden: exit 2, stderr contains `transcribe` and `uv sync --extra transcribe`, no `data/`). `test_cli.py::test_transcribe_missing_extra_exits_2`. CI's gate (`uv sync --locked`, no extras) is the end-to-end proof that the suite runs without the extra |
| A2 | The model loads lazily, once per worker process | `test_faster_whisper.py::test_builder_loads_no_model` (after `build_faster_whisper_transcriber(..., runtime=fake.runtime())`: `fake.loads == []`, `engine.model_loaded is False`). `test_model_loaded_once_for_two_episodes` (two episodes transcribed: `len(fake.loads) == 1`, two model calls). `test_failed_load_is_retried` (`load_errors=[OSError()]`: first call `TransientError`, second succeeds, `len(fake.loads) == 2` attempts with one success). `test_worker_loads_model_once`: `build_worker` with every backend fake except `asr_backend="faster_whisper"` and `load_runtime` monkeypatched to the fake; two jobs for two different share URLs; both `done`; `len(fake.loads) == 1` |
| A3 | Model, device, compute type, and language come from settings | `test_options_from_settings` (env `EARMARK_ASR_MODEL=small`, `EARMARK_ASR_DEVICE=CPU`, `EARMARK_ASR_COMPUTE_TYPE=INT8`, `EARMARK_ASR_LANGUAGE=" EN "`, `EARMARK_ASR_MODEL_DIR=<tmp_path>`, then `load_settings(env_file=None)`; going through the environment matters because the pydantic mypy plugin, with `init_typed`, rejects `Settings(asr_device="CPU")` → `WhisperOptions("small", "cpu", "int8", "en", tmp_path)`, and the first `fake.loads` entry equals it). `test_language_and_options_reach_model` (model call kwargs: `language == "en"`, `beam_size == 5`, `vad_filter is True`, `audio` is the decoded samples; with no language, `language is None` and the transcript's language comes from `info`). `test_real_runtime_wiring`: stub modules in `sys.modules` (`types.ModuleType` with a recording `WhisperModel` class and `decode_audio` returning a list of 32 000 floats; a `ctranslate2` stub with `get_cuda_device_count` returning 2): `load_runtime().load_model(opts)` constructed `WhisperModel("small", device="cpu", compute_type="int8", download_root=str(tmp_path))` (and `download_root=None` when unset); `decode_audio(p)` called the stub with `(str(p), sampling_rate=16000)` and returned `duration_s == 2.0`; `cuda_device_count() == 2`. `test_config.py::test_asr_settings_normalised` (`EARMARK_ASR_DEVICE=CUDA` → `cuda`; `EARMARK_ASR_LANGUAGE=auto` → `None`; `EARMARK_ASR_LANGUAGE=Haw` → `haw`) and new rows in `test_invalid_values_rejected`: `EARMARK_ASR_DEVICE=gpu-sentinel`, `EARMARK_ASR_COMPUTE_TYPE=float8-sentinel`, `EARMARK_ASR_LANGUAGE=english-sentinel`, `EARMARK_ASR_MODEL=" "` (echo check off for the last). `test_cuda_without_device_is_config_error` (`asr_device="cuda"`, `cuda_devices=0`: `ConfigError` naming `EARMARK_ASR_DEVICE` and `EARMARK_ASR_BACKEND=remote`; with `cuda_devices=1` it builds) |
| A4 | Segments map to `Transcript` with monotonic timestamps; `transcript.json` and `transcript.txt` written atomically | `test_segments_map_to_transcript` (three segments with leading spaces and inner newlines: `Transcript.model == "faster-whisper:tiny"`, `language`, `duration_s` from `info`, texts collapsed and stripped, `episode_key`). `test_segment_from`, parametrized table: regressing start → previous start; `end < start` → `end == start`; NaN start → previous start; NaN end → start; negative start → 0; start and end beyond duration → clamped to duration; duration 0 → no upper clamp; blank text → `None`. `test_timestamps_monotonic_property` (a scrambled list of 50 segments: starts non-decreasing, every `end >= start`, all within `[0, duration]`). `test_transcript_files_written` (after `CachedTranscriber.transcribe`: `transcript.txt == transcript.to_text()`, `transcript.json` validates back to the returned transcript, both mode `0o644`, no `.tmp` file left in the episode directory, and `ArtifactStore.write_transcript` was the writer: wrap it with a spy that counts calls). `test_write_failure_leaves_no_cache` (monkeypatch `ArtifactStore._write_json` to raise `OSError`: the error propagates, `transcript.json` absent, no temp files, audio kept). Atomicity of `atomic_write_text` itself is covered by step 02's `test_artifacts.py` tests |
| A5 | An existing transcript is returned without loading the model | `test_transcribe_base.py::test_cached_transcript_returned_without_engine` (a recording engine double: `transcribe_file` never called; returned transcript equals the stored one). `test_faster_whisper.py::test_cached_transcript_loads_no_model` (real engine, fake runtime: `fake.loads == []`, `fake.decodes == []`) |
| A6 | Progress is logged during long transcriptions so the worker's heartbeat stays fresh | `test_progress_logged` (a 1 h `FakeInfo`, 12 segments 5 min apart, a fake monotonic advanced 25 s by `on_segment`, interval 60 s: `caplog` INFO records from `earmark.transcribe.faster_whisper` matching `^transcribing <key>: \d{2}:\d{2}:\d{2} of 01:00:00 \(\d+%\)$`; between 4 and 6 of them, fewer than the 12 segments; percentages increase; plus the start and `transcribed …` lines). `test_heartbeat_fresh_during_transcription`: `tests/pipeline_env.make_env(tmp_path, store_type=CountingJobStore, lease_s=30, heartbeat_s=0.01, fakes=fakes, backends=dataclasses.replace(fakes.backends(), transcriber=CachedTranscriber(fakes.artifacts, FasterWhisperEngine(opts, fake.runtime()), keep_audio=False)))` with `fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")`; `on_segment(0)` advances `env.clock` by 31 s and calls `env.jobs.wait_for_beats(beats_now + 2)` (asserting it returned true), then calls `requeue_stale()` and records the result; the job ends `done`, the recorded result is `[]`. Slow `test_real_model_does_not_block_other_threads` (6.4) shows the real engine releases the GIL |
| A7 | Audio is deleted after success unless `KEEP_AUDIO` is set | `test_transcribe_base.py::test_audio_deleted_after_success` (no `audio.*` left), `test_keep_audio_keeps_file`, `test_cached_hit_cleans_up_audio` (transcript exists and audio exists: audio deleted, or kept with `keep_audio=True`), `test_audio_kept_on_failure` (engine raises `TransientError`, then `PermanentError`, then `Shutdown`-like `BaseException`: audio still there each time), `test_unusable_audio_deleted` (engine raises `UnusableAudioError`: re-raised, audio deleted; kept with `keep_audio=True`), `test_delete_failure_is_warning` (`ArtifactStore.delete_audio` raises `OSError`: transcript returned, one WARNING naming the key), `test_audio_outside_episode_dir_untouched` (an `audio` path in another directory: not deleted). `test_faster_whisper.py::test_builder_passes_keep_audio` (`keep_audio=True` in settings → `transcriber.keep_audio`). `test_pipeline_with_faster_whisper` (`make_env` with the real `CachedTranscriber` and fake runtime: job `done`, `transcript.json` exists, no `audio.*`; a second job with a new prompt makes no load and no decode) |
| A8 | Unit tests inject a fake model; an opt-in `slow` test transcribes a short clip with `tiny` on CPU and gets non-empty text | All tests above use `FakeRuntime`. `test_faster_whisper_slow.py::test_tiny_model_transcribes_clip` (6.4) |
| A9 | The scheduled workflow also runs `pytest -m slow` with the `transcribe` extra installed and the model download cached | `test_ci.py` (6.5) |

### 6.2 Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | The backend is registered and built only by the factory; factory rules hold | `test_faster_whisper.py::test_factory_builds_faster_whisper` (`load_runtime` patched: `isinstance(b.transcriber, CachedTranscriber)`, its engine is a `FasterWhisperEngine` with options from settings, model not loaded). `test_transcriber_satisfies_protocol` (annotated `t: Transcriber = CachedTranscriber(...)`, `e: TranscriptionEngine = FasterWhisperEngine(...)`, `m: WhisperModelLike = FakeWhisperModel(...)`; mypy is the proof). `test_factory.py` changes in 6.3 |
| B2 | Load failures are classified without leaking URLs | `test_load_error_mapping`, parametrized over a `ValueError("Invalid model size 'x'")` → `PermanentError` naming the three settings; locally defined classes `RepositoryNotFoundError(OSError)` and `GatedRepoError(RepositoryNotFoundError)` with messages containing `https://huggingface.co/api/...` → `PermanentError` "was not found", URL absent; `LocalEntryNotFoundError(FileNotFoundError)` and `ConnectionError` → `TransientError` ending in the class name, URL absent; `RuntimeError("boom")` → propagates as `RuntimeError`. Each mapped error has `__cause__` set |
| B3 | Undecodable, empty, and speechless audio fail permanently with short messages | `test_decode_error_is_unusable_audio` (decode raises an exception with `strerror = "Invalid data found when processing input"` and a `str()` containing `tmp_path`: `UnusableAudioError`, message `could not decode audio.mp3: Invalid data found when processing input`, `str(tmp_path)` not in it). `test_decode_memory_error_propagates`. `test_zero_duration_is_unusable`. `test_no_speech_is_unusable` (model yields nothing, or only blank texts). `test_transcribe_value_error_is_permanent` (`transcribe_error=ValueError("'xx' is not a valid language code ...")` with `language="xx"`: `PermanentError`, message ends with `(check EARMARK_ASR_LANGUAGE)`, at most `MAX_DETAIL_CHARS` of detail) |
| B4 | An interruption writes nothing and keeps the model | `test_shutdown_mid_transcription` (`on_segment(1)` raises `earmark.pipeline.worker.Shutdown`: it propagates out of `CachedTranscriber.transcribe`; no `transcript.*`, no temp files, audio kept; a second call with a normal model does not reload: `len(fake.loads) == 1`) |
| B5 | `earmark transcribe` works and touches nothing | `test_cli.py` with `load_runtime` patched: `test_transcribe_prints_lines` (stdout is `[00:00:00] …` lines matching the fake segments; exit 0; input file unchanged and present; `./data` not created). `test_transcribe_json` (stdout validates as `Transcript`; `episode_key == file_key(path)`). `test_transcribe_missing_file_exits_2`. `test_transcribe_unusable_audio_exits_1` (stderr `earmark transcribe: could not decode …`). `test_transcribe_transient_exits_1` (stderr ends `(temporary; try again later)`). `test_transcribe_cleans_control_chars` (a segment text containing `\x1b[31m`: no ESC byte in stdout). `test_transcribe_ignores_asr_backend` (`EARMARK_ASR_BACKEND=fake`: still uses the faster-whisper engine, i.e. the patched runtime was used). `test_file_key_deterministic` (same bytes in two files → same key; different bytes → different; `is_episode_key`) |
| B6 | The new setting is wired everywhere settings are listed | Existing `test_config.py::test_env_example_lists_every_field` and `test_env_example_values_are_defaults` cover `ASR_MODEL_DIR`. `test_config.py::test_asr_model_dir_path` (`EARMARK_ASR_MODEL_DIR=/models` → `Path("/models")`) |
| B7 | The engine output is checked | `test_transcribe_base.py::test_engine_returning_other_episode_fails` (`PermanentError` naming the engine; nothing written) |

### 6.3 Changes to existing tests

- `test_factory.py::test_default_settings_list_all_unbuilt`: with defaults,
  `faster_whisper` is now built, so the expected lines are the fields whose **default
  value** has no builder (llm, store, index, notify: 4). Compute that set from
  `Settings(_env_file=None)` and the registries instead of `UNBUILT_FIELDS`; keep the
  per-field count check and add `not any(line.startswith("EARMARK_ASR_BACKEND") ...)`.
  `UNBUILT_FIELDS` still contains `asr_backend` (because of `remote`), so
  `test_unbuilt_backend_names_setting` keeps working and now picks `remote`.
- `test_factory.py::test_build_worker_config_error_touches_nothing`: use
  `asr_backend="remote"`.
- `test_cli.py::test_worker_unbuilt_backend_exits_2`: use `EARMARK_ASR_BACKEND=remote`.
- `test_factory.py::test_build_resolver_only_builds_resolver` stays as is (it proves no
  other backend is built, which now also means no faster-whisper import).

### 6.4 Slow tests (`tests/test_faster_whisper_slow.py`, `@pytest.mark.slow`)

A module-level fixture `whisper_runtime` calls `load_runtime()` and turns
`MissingExtraError` into `pytest.fail(f"{exc} (the slow tests need the transcribe
extra)")`, so a CI misconfiguration fails rather than skips. Settings for every test:
`Settings(_env_file=None, data_dir=tmp_path / "data", asr_model=SLOW_MODEL,
asr_device="cpu", asr_compute_type="int8", asr_model_dir=slow_assets.model_dir())`.

- `test_tiny_model_transcribes_clip`: `clip = fetch_clip()`; copy it to
  `artifacts.audio_path(key, "flac")`; `t = build_faster_whisper_transcriber(settings,
  artifacts)`; while a daemon thread records the largest gap between 50 ms ticks, call
  `t.transcribe(episode, audio)`. Assert: `t.engine.model_loaded`; the joined text is
  non-empty and contains `country` (case-insensitive); `language == "en"`; `10.5 <=
  duration_s <= 11.5`; starts non-decreasing and `end >= start`; `transcript.json` and
  `transcript.txt` exist; no `audio.*` left; largest tick gap `< 2.0` s. Then a second
  `CachedTranscriber` over a `FakeRuntime` whose `load_model` raises returns the cached
  transcript (no load).
- `test_cli_transcribe_tiny`: `CliRunner` with `EARMARK_ASR_MODEL=tiny`,
  `EARMARK_ASR_DEVICE=cpu`, `EARMARK_ASR_COMPUTE_TYPE=int8`,
  `EARMARK_ASR_MODEL_DIR=<model_dir>`, a copy of the clip: exit 0; stdout's first line
  starts with `[00:00:00]` and contains `country`; the copy still exists with the same
  sha256.

Fixtures: the model comes from Hugging Face through faster-whisper's own download into
`slow_assets.model_dir()` (79 MB, once); the clip from `fetch_clip()` (2.5, 4.9). Both
need sockets, which `tests/conftest.py` already re-enables for `slow` items.

### 6.5 Workflow tests (`tests/test_ci.py`)

The parametrized rules (`permissions` present, no `pull_request_target`, every `uses:`
pinned to a 40-character SHA with a `# vX.Y.Z` comment, actionlint clean) cover the
edited file automatically. Changes:

- A helper `_network_job(job_id: str) -> dict[str, Any]` returns `_jobs(_network())[job_id]`.
  `test_network_workflow_runs_network_marker`, `test_network_workflow_read_only_no_secrets`,
  and `test_network_workflow_pins_match_ci` use `_network_job("network")` instead of
  `_only_job(...)`; their assertions on that job are unchanged.
- `test_live_workflow_jobs`: `set(_jobs(_network())) == {"network", "slow"}`.
- `test_slow_job_runs_slow_marker`: `runs-on == ARM64_RUNNER`; `timeout-minutes` an
  `int` in 1..30; run steps exactly `["uv sync --locked --extra transcribe", "uv run
  --locked --extra transcribe pytest -m slow -v -rs"]`; actions in order `[CHECKOUT,
  SETUP_UV, "actions/cache", "", ""]`.
- `test_slow_job_read_only_no_secrets`: no `env` on the workflow, the job, or any step;
  checkout `persist-credentials: false`; (the workflow-level `permissions` and no-secrets
  checks already cover the file).
- `test_slow_job_pins`: `_remote_uses` of the slow job is the CI list plus
  `"actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"`; its setup-uv `with:` equals
  CI's plus `{"cache-suffix": "transcribe"}`.
- `test_slow_job_caches_test_assets`: the cache step's `with.path == "~/.cache/earmark-tests"`,
  equal to `"~/" + DEFAULT_CACHE_DIR.relative_to(Path.home()).as_posix()` from
  `tests.slow_assets`; `with.key` contains `hashFiles('tests/slow_assets.py')`.
- `test_network_workflow_is_not_the_gate`: also assert `"not slow"` is in the default
  `-m` expression.

## 7. Verification

`SCRATCH` is the verifier's scratchpad directory (on `/tmp`).

```bash
cd /home/sandy/dev/earmark
df -h /home/sandy /tmp            # need ~100 MB free on /tmp for the model and clip
uv cache prune                    # safe; removes only unreferenced cache entries

# 1. The gate without the extra (exact sync removes extras if present)
uv sync --locked
make check
uv run --locked python -c 'import importlib.util as u; print(u.find_spec("faster_whisper"))'   # None
uv run ruff format --check docs/
uv lock --check
git diff uv.lock | grep '^+name = ' # only the packages in section 2.2

# Light imports
uv run --locked python -c 'import sys, earmark.cli, earmark.pipeline.factory, earmark.transcribe.faster_whisper; print(sorted(m for m in ("faster_whisper","ctranslate2","av","huggingface_hub","numpy") if m in sys.modules))'
# expect: []

# 2. Without the extra, the backend is a clear startup error
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
for v in RESOLVER_BACKEND FETCH_BACKEND LLM_PROVIDER STORE_BACKEND INDEX_BACKEND \
  NOTIFY_BACKEND; do export "EARMARK_$v=fake"; done
export EARMARK_DATA_DIR="$SCRATCH/data" EARMARK_VAULT_DIR="$SCRATCH/vault"
$E worker --once; echo "exit=$?"          # names EARMARK_ASR_BACKEND and the transcribe extra, exit=2
ls "$SCRATCH/data" 2>&1                   # No such file or directory
$E transcribe /etc/hostname; echo "exit=$?"   # same message, exit=2

# 3. With the extra: slow tests (model and clip into the scratchpad)
cd /home/sandy/dev/earmark
uv sync --locked --extra transcribe
SLOW_TEST_CACHE="$SCRATCH/slow-cache" uv run --locked --extra transcribe pytest -m slow -v -rs
# expect: 2 passed
du -sh "$SCRATCH/slow-cache"              # about 80 MB
make check                                # still green with the extra installed
uv run --locked --extra transcribe pytest tests/test_ci.py -q
```

Live smoke with the real backend (fake resolver, a local file as the "download"):

```bash
cd "$SCRATCH"
export EARMARK_ASR_BACKEND=faster_whisper EARMARK_ASR_MODEL=tiny EARMARK_ASR_DEVICE=cpu \
  EARMARK_ASR_COMPUTE_TYPE=int8 EARMARK_ASR_MODEL_DIR="$SCRATCH/slow-cache/whisper"
$E transcribe "$SCRATCH/slow-cache/clips/jfk.flac"
# [00:00:00] And so, my fellow Americans, ask not what your country can do for you, ...
$E transcribe --json "$SCRATCH/slow-cache/clips/jfk.flac" | head -5
sha256sum "$SCRATCH/slow-cache/clips/jfk.flac"   # unchanged: 63a4b1e4…

# Worker: the fake fetcher writes a fake audio.mp3, which is not decodable, so the job
# fails permanently in the transcribe stage with a short message and the audio is deleted.
$E submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981'
$E worker --once                     # job <id> failed
$E jobs                              # stage transcribe, "could not decode audio.mp3: ..."
ls data/episodes/*/                  # episode.json shared.json, no audio.*, no transcript.*

# Worker with real audio: put the clip where the fake fetcher's cache check finds it
KEY=$(ls data/episodes)
cp "$SCRATCH/slow-cache/clips/jfk.flac" "data/episodes/$KEY/audio.flac"
$E submit 'https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981' --prompt again
$E worker --once 2>worker.log        # job <id> done
grep -E "loading Whisper|loaded Whisper|transcribing|transcribed|no CUDA" worker.log
cat data/episodes/$KEY/transcript.txt   # the JFK line
ls data/episodes/$KEY/               # no audio.*; transcript.json, transcript.txt, analyses/
for v in RESOLVER_BACKEND FETCH_BACKEND ASR_BACKEND LLM_PROVIDER STORE_BACKEND \
  INDEX_BACKEND NOTIFY_BACKEND DATA_DIR VAULT_DIR ASR_MODEL ASR_DEVICE \
  ASR_COMPUTE_TYPE ASR_MODEL_DIR; do unset "EARMARK_$v"; done

# 4. Leave the venv as CI has it, and free the scratch space
cd /home/sandy/dev/earmark && uv sync --locked
rm -rf "$SCRATCH/slow-cache" "$SCRATCH/data" "$SCRATCH/vault" "$SCRATCH/worker.log"
```

(`FakeAudioFetcher.fetch` returns `artifacts.find_audio(key)` when it exists and only
otherwise writes its fake `audio.mp3`, which is why copying the clip in as `audio.flac`
works. `KEY` is the only episode directory because both submits use the same URL.)

Also check:

- `git status` lists only the files in section 3 and this plan.
- No `# type: ignore`, `noqa`, or mypy override for `faster_whisper`; no loosened lint,
  type, or test settings.
- `grep -rn "faster_whisper\|ctranslate2" src/earmark` shows imports only inside
  functions of `transcribe/faster_whisper.py` (via `importlib.import_module`) and the
  lazy import in `factory.py`.
- `docs/ARCHITECTURE.md` matches section 8, item 9.

Not verifiable in the sandbox: CUDA transcription (no GPU; and the PyPI aarch64 wheel
is CPU-only anyway, 2.1), `large-v3` speed and memory, the `slow` job on GitHub Actions
(it runs once `network.yml` is on `main`, or by `workflow_dispatch` there), and the
actions/cache hit.

## 8. Deviations

1. **`av>=18.1,<19` in the extra** alongside `faster-whisper>=1.2.1`: faster-whisper
   1.2.1 is broken with PyAV 19 (2.2). Remove the bound when a faster-whisper release
   after 1.2.1 ships the fix; Dependabot will propose `av` bumps that the slow job would
   catch.
2. **New setting `EARMARK_ASR_MODEL_DIR`** (default unset = the Hugging Face cache).
   huggingface_hub reads `HF_HOME` only at import, so the slow test and the container
   (step 16) need an explicit download directory. Added to `Settings`, `.env.example`,
   and the architecture's configuration table.
3. **ASR settings are validated when settings load**: `ASR_DEVICE` and
   `ASR_COMPUTE_TYPE` become `Literal`s (case-insensitive), `ASR_LANGUAGE` is normalised
   (`auto` means detect) and checked for shape, `ASR_MODEL` must be non-empty. Capability
   errors (float16 on CPU, CUDA missing) still surface at model load, except `cuda` with
   no visible device, which the builder rejects at startup.
4. **VAD filtering is on** (`vad_filter=True`, not faster-whisper's default): without it
   the probe hallucinated text in silence (2.4). Not a setting.
5. **Engine and cache shell are split** (`transcribe/base.py` with `CachedTranscriber`
   and the `TranscriptionEngine` Protocol), so step 09 reuses the caching, atomic writes,
   and audio deletion instead of copying them ("Caching and file outputs match step 08").
6. **Unusable audio** (undecodable, empty, no speech) fails the job permanently and
   deletes the audio unless `KEEP_AUDIO`, so a re-share downloads it again rather than
   reusing a broken file. A transcript with no segments is never written.
7. **`earmark transcribe` prints and writes nothing else**: it always uses the local
   faster-whisper engine with the `EARMARK_ASR_*` settings, prints text (or `--json`),
   and does not touch `DATA_DIR`.
8. **The live workflow is renamed "Live tests"** and has two jobs; the `slow` job adds
   `actions/cache` (pinned) and a `cache-suffix` for setup-uv. `tests/test_ci.py` selects
   network-workflow jobs by id instead of assuming one job.
9. **Architecture edits** made with this plan: Decisions (transcription row: CPU-only
   aarch64 wheel, GPU path); module layout (`transcribe/base.py`,
   `transcribe/faster_whisper.py`); a "Transcribing" subsection under Stage contracts;
   Configuration (ASR settings' values, `ASR_MODEL_DIR`); Data layout (when audio is
   deleted); Security (model downloads); Testing (`slow` marker and cache, the `slow`
   CI job); Known risk 1 (findings), new risk 7 (PyAV pin).

**Decision for the user (not changed by this plan):** the default `EARMARK_ASR_MODEL` is
`large-v3`. On the DGX Spark with the PyPI wheel it runs on CPU, which will be slow for
hour-long episodes. Until step 09 or a CUDA build of CTranslate2 is in place, consider a
smaller CPU model on the Spark (`EARMARK_ASR_MODEL=small`, `distil-large-v3`, or
`large-v3-turbo`); none was benchmarked here.

## 9. Out of scope

- The remote ASR backend (`EARMARK_ASR_BACKEND=remote`): step 09. It stays unbuilt and
  is the factory's remaining unbuilt ASR name.
- Building CTranslate2 with CUDA for sm_121, and the GPU container profile: step 16
  (documented as untested there).
- `earmark doctor` checking the extra and the model cache: step 15.
- Word timestamps, diarization (backlog), batched inference, `cpu_threads` tuning,
  unloading an idle model, and streaming decode for very long files (5.7).
- Re-transcribing when the model changes: the transcript cache has no model key (as in
  step 02); delete `transcript.json` to redo an episode.
