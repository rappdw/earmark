# Step 19: Fix: library errors are not bad audio

## 1. Outcome

A broken or incompatible library can no longer pass for bad audio. Only two things
raise `UnusableAudioError`, which fails the job permanently and deletes its audio unless
`EARMARK_KEEP_AUDIO` is set:

- a file that is not decodable audio: PyAV's `InvalidDataError` or `EOFError`, or a
  container with no audio stream;
- a file that decodes to no samples.

Every other exception from the decoder or the model propagates unchanged. That
includes PyAV 19's `TypeError: open() got an unexpected keyword argument
'metadata_errors'`, OS errors, and stray `ValueError`s from the model. The runner then
treats it as unexpected: transient, logged with a traceback, and the audio is kept for
the retry.

The audit (2.5) found two more ways to lose audio when nothing was wrong with it, and
this step fixes both. "No speech found" becomes `NoSpeechError`: still a permanent
failure, but the audio is kept. A remote ASR reply that is HTTP 200 but has no `text`
field is now "not a transcription" instead of "no speech".

The plain `TypeError` in `earmark transcribe` becomes a one-line "unexpected error"
message instead of `could not decode jfk.flac: TypeError`.

`earmark doctor` gains an `asr libraries` check. It reads the installed `faster-whisper`
and `av` versions from package metadata and warns on a known-incompatible pair. It
loads no model and does not import either package.

## 2. Findings

All checks ran on 2026-11-20. The probes used a scratch venv at `/tmp/earmark-probe`
(`UV_PROJECT_ENVIRONMENT` and `UV_CACHE_DIR` both under it):
`uv sync --locked --no-dev --extra transcribe`, which needed about 350 MB. The repo
`.venv` was not touched, and the scratch venv was deleted afterwards. Probe scripts
lived in the scratchpad and ran with `python -I`. The test files were generated with
PyAV itself, not downloaded.

### 2.1 PyAV 18.1.0 `av.error` hierarchy (inspected with `vars(av.error)` and `__mro__`)

- `FFmpegError(Exception)` is the base of everything PyAV raises for an FFmpeg error
  code. `av.error.classes` maps 157 codes to classes.
- Errno codes map to classes that inherit from both `FFmpegError` and the builtin.
  Examples: `FileNotFoundError(FFmpegError, FileNotFoundError)`, `PermissionError`,
  `IsADirectoryError`, `BlockingIOError`, `TimeoutError`, `MemoryError(FFmpegError,
  MemoryError)`, `OSError(FFmpegError, OSError)` for most other errnos,
  `ArgumentError(FFmpegError, ValueError)` for EINVAL, `OverflowError`, and
  `NotImplementedError`.
- FFmpeg's own codes: `InvalidDataError(FFmpegError, ValueError)` (AVERROR_INVALIDDATA),
  `EOFError(FFmpegError, EOFError)` (AVERROR_EOF), `DecoderNotFoundError`,
  `DemuxerNotFoundError`, `EncoderNotFoundError`, `MuxerNotFoundError`,
  `ProtocolNotFoundError`, `FilterNotFoundError`, `BSFNotFoundError`, and
  `OptionNotFoundError`. All of the `*NotFoundError` classes have MRO `(LookupError,
  FFmpegError, LookupError)`. The rest are `PatchWelcomeError`, `BugError(FFmpegError,
  RuntimeError)`, `BufferTooSmallError(…, ValueError)`, `ExitError`, `ExternalError`,
  `UnknownError`, `UndefinedError`, `ExperimentalError`, `InputChangedError`,
  `OutputChangedError`, `PyAVCallbackError(…, RuntimeError)`, and the `HTTP*Error`
  classes.
- PyAV 19.0.2 has the same names and MROs for `FFmpegError`, `InvalidDataError`,
  `EOFError`, `DecoderNotFoundError`, `PatchWelcomeError`, `OSError`, and
  `MemoryError` (checked after `uv pip install av==19.0.2` into the scratch venv).
  `FFmpegError.strerror` is FFmpeg's message. `str(exc)` includes the full path when
  the error comes from opening a file.

### 2.2 What `faster_whisper.decode_audio` raises (faster-whisper 1.2.1, PyAV 18.1.0)

`decode_audio(path, sampling_rate=16000)` runs `av.open(path, mode="r",
metadata_errors="ignore")`, then `container.decode(audio=0)`. It skips
`av.error.InvalidDataError` frame by frame (`_ignore_invalid_frames`), resamples, and
returns a float32 array. Observed results:

| Input | Result |
|---|---|
| FLAC, WAV, MP3, AAC/M4A made by PyAV (2 s sine) | samples (32 000; M4A 33 792) |
| text file named `.mp3` | `InvalidDataError`, strerror `Invalid data found when processing input` |
| empty file | `InvalidDataError` |
| first 100 bytes of an MP3 | `InvalidDataError` |
| first 100 bytes of a FLAC | `av.error.EOFError`, strerror `End of file` |
| first half of a FLAC | samples (5 760) |
| FLAC with 2 000 random bytes in the middle | **0 samples** |
| 200 kB of random bytes named `.mp3` | **0 samples** |
| 200 kB of random bytes named `.bin` (detected as format `bin`, one video stream) | **`IndexError: tuple index out of range`** |
| MP4 with only a video stream (mpeg4) | **`IndexError: tuple index out of range`** |
| WAV with format tag 0x7777 (unknown codec) | `DecoderNotFoundError`, strerror `Decoder not found` |
| missing path | `av.error.FileNotFoundError` (an `OSError`) |
| a directory | `av.error.IsADirectoryError` (an `OSError`) |
| mode 000 file (uid 501) | `av.error.PermissionError` (an `OSError`) |

The `IndexError` comes from `container.decode(audio=0)` when the container has no audio
stream. It is a genuine "no audio" condition, but it is the same exception type a code
bug raises, so it cannot be classified by type alone (5.2).

### 2.3 PyAV 19: the regression and two dead ends

- With `av 19.0.2` and faster-whisper 1.2.1, **every input in the table above**
  (including the missing path and the directory) raises `TypeError: open() got an
  unexpected keyword argument 'metadata_errors'` before any I/O. The current
  `except Exception` in `FasterWhisperEngine.transcribe_file` turns that into
  `UnusableAudioError("could not decode <name>: TypeError")` and deletes the audio.
- faster-whisper `master` fixes it with `_AV_OPEN_KWARGS = {"metadata_errors": "ignore"}
  if int(av.__version__.split(".")[0]) < 19 else {}`, but `master`'s `version.py` still
  says `1.2.1`. PyPI's latest faster-whisper is still 1.2.1 (2025-10-31). PyPI's latest
  av is 19.0.2 (2026-10-21). Tags v0.10.0, v1.0.0, v1.1.0, and v1.2.0 all pass
  `metadata_errors` too, so every faster-whisper release so far is affected.
- **Dead end 1: probe every file with `av.open` first.** PyAV 18 decodes container and
  stream metadata eagerly at open (`container/input.py`: `self._metadata =
  avdict_to_dict(..., self.metadata_errors)`; `stream.py` likewise). The default is
  `metadata_errors="strict"`, so a plain `av.open(path)` raises `UnicodeDecodeError`
  on files with non-UTF-8 tags. faster-whisper passes `"ignore"` for exactly that
  reason, and earmark cannot pass it either, because PyAV 19 rejects the argument. So
  a probe that runs before decoding would break files that decode fine today. The plan
  only probes after an `IndexError` and treats any probe failure as inconclusive (5.2).
- **Dead end 2: decode with PyAV directly instead of `faster_whisper.decode_audio`.**
  This would sidestep the PyAV 19 bug, but it means reimplementing resampling and
  framing, and it is a workaround for the incompatibility rather than the fix the brief
  asks for. The `av>=18.1,<19` pin and the Dependabot rule stay.

### 2.4 Package metadata

- `importlib.metadata.version("faster-whisper")`, `version("faster_whisper")`, and
  `version("Faster_Whisper")` all return `1.2.1`, and `version("AV")` returns `18.1.0`
  (names are normalised). An absent distribution raises `PackageNotFoundError`. Reading
  metadata imports none of `av`, `faster_whisper`, `ctranslate2`, or `numpy`
  (checked with `sys.modules`).
- `distribution("faster-whisper").read_text("direct_url.json")` is `None` for a
  registry install. PEP 610 writes `direct_url.json` with `vcs_info` (git and similar)
  or `dir_info` (local path, possibly editable) for source installs. For those, the
  version number does not identify the code: a git install of faster-whisper `master`
  says 1.2.1 and works with PyAV 19. The check therefore skips a rule when either
  package came from a source tree (4.5).
- Doctor's existing `asr` check already imports the extra (`TRANSCRIBERS[...]` →
  `load_runtime`; step 15, no model load). The new check itself imports nothing heavy.

### 2.5 Audit: broad catches that turn code or library errors into permanent, data-destroying outcomes

I listed every `except` handler in `src/earmark` with an AST scan, and every deletion
call site (`unlink`, `rmtree`, `delete_audio`, `delete_*`, `clear_*`). Audio is the only
artifact earmark deletes on a failure path. Everything else it deletes is a temp file,
a partial download, or a recomputable checkpoint.

**Fixed in this step:**

| # | Where | Problem | Fix |
|---|---|---|---|
| F1 | `transcribe/faster_whisper.py` `transcribe_file`, `except Exception` around decode | Any exception, including PyAV 19's `TypeError`, OS errors, and code bugs, becomes `UnusableAudioError`, which is permanent and deletes the audio. | 5.1–5.3: only `AudioDecodeError` from the runtime counts. |
| F2 | same file, `except ValueError` around `model.transcribe(...)` **and the segment loop** | Any `ValueError` from faster-whisper, CTranslate2, or numpy becomes a permanent `transcription failed` that blames `EARMARK_ASR_LANGUAGE`. Examples are `feature_extractor.py`'s stft checks and the prompt-length check in `transcribe.py`. Not data-destroying (audio kept), but a library error made permanent. | 5.4: only the language-code `ValueError` from the `transcribe(...)` call stays permanent. |
| F3 | `transcribe_file`: no segments → `UnusableAudioError`; `remote.transcript_from_response`: empty text → `UnusableAudioError` | A broken VAD (onnxruntime), model, or ASR server yields "no speech" for every episode, and each one deletes its audio. A podcast with no speech at all is far less likely than a fault. The brief's list of unusable audio does not include "no speech". | 5.5: `NoSpeechError(PermanentError)`, audio kept. |
| F4 | `remote._WireTranscription`: every field has a default, `extra="ignore"` | An HTTP 200 body such as `{}`, `{"error": {...}}`, or `{"detail": "..."}` (a misconfigured or non-OpenAI server) validates as an empty transcription and becomes "no speech". | 5.6: `text` is required, so such a body is "not a transcription" (permanent, audio kept). |
| F5 | `cli.py` `transcribe_command` | Not a catch, but the visible symptom: the bug reads as an audio problem. Once F1 is fixed, an unexpected error would otherwise reach Typer's traceback printer. | 5.7: one clear line, exit 1. |

**Reviewed and left as they are:**

| Where | Catch | Why it stays |
|---|---|---|
| `faster_whisper._map_load_error` | `ValueError` at model load → `PermanentError` | CTranslate2 and `download_model` raise `ValueError` for a bad `ASR_MODEL`, `ASR_DEVICE`, or `ASR_COMPUTE_TYPE` (step 08 2.3). That is configuration and recurs on every retry. The audio is kept, and a re-share after a fix reuses it. Unknown exception types already propagate. |
| `transcribe/base.py` `_cleanup` | `OSError` from `delete_audio` → warning | It only fails to delete, so nothing is lost. |
| `transcribe/remote.py` | `ValidationError` or JSON `ValueError` on a 200 → `PermanentError`; 3xx/4xx → `PermanentError` | These come from untrusted server output or the operator's configuration. The audio is kept. |
| `fetch.py` `_map_httpcore_errors`, `_transport_errors` | narrow; unknown exceptions re-raised | Transport errors are transient, and unknown errors propagate unchanged. |
| `fetch.py` `download` → `atomic_writer` (`except BaseException`) | removes the `.part` temp file | The file is a partial download, never the stored audio. |
| `audio.py` `_remove_stale_parts` | deletes `.audio.*.part` older than 2× the download deadline | These are abandoned partial downloads. |
| `fetch.py:336`, `resolve/rss.py:118` | `(TypeError, ValueError, IndexError)` → `None` | This is lenient parsing of one date or header field from untrusted input (`email.utils` raises those types). It is never a job outcome. |
| `resolve/resolver.py`, `rss.py`, `itunes.py`, `apple_page.py` | status- and format-based `PermanentError`/`SkipEpisode`/`FeedFormatError` | These are narrow catches on HTTP status or parse errors of untrusted input. Nothing is deleted, and a re-share resolves again. |
| `analyze/calls.py` `parse_as` | `ValidationError` → one repair, then `PermanentError` | This is model output. The transcript is kept, and `earmark reanalyze` re-runs it. |
| `analyze/mapreduce.py:164`, `digest/themes.py:172` | invalid cached part or call → deleted and recomputed | These are checkpoints, not source data, and recomputing is the right response to an unreadable checkpoint. |
| `analyze/analyzer.py` `clear_analysis_parts`, `digest/run.py` `clear_calls` | delete checkpoints | These run only after the result is written (or on `--refresh`), never on a failure path. |
| `analyze/llm/anthropic.py` `_map_error` | unknown SDK errors → `TransientError` | Already the safe direction. |
| `analyze/llm/openai_compat.py:350` | non-JSON body → `PermanentError` | This is untrusted server output. The transcript is kept. |
| `artifacts.py` `_read_json` | `ValidationError` → `PermanentError("corrupt artifact")` | Nothing is deleted. A code change that rejected valid artifacts would fail jobs but destroy nothing. |
| `vault/notes.py` `_collect_docs`, `_load_doc` | unreadable artifacts → skipped with a warning | `earmark entities rebuild` refuses to drop notes (`RebuildRefused`) unless `--prune` is given. Vault writes stay inside markers, and hand edits are backed up. |
| `vault/notes.py` `_guard` | `OSError`/sqlite → mapped Permanent or Transient | Narrow catches. The vault is written only under its lock and only inside markers. |
| `vault/fs.py` `except BaseException` blocks, `remove_stale_temps` | remove our own temp files, re-raise | Only `TEMP_NAME`-matching files older than 1 h are removed. |
| `index/lancedb_index.py` `_lance`, `index/embed.py` `_model` | `Exception` → `TransientError` | Index failures are job warnings. The index is derived and rebuildable. `reset` (drop table) runs only from `earmark reindex`. |
| `pipeline/runner.py` | `Exception` → transient (unexpected path), notifier errors → warning | This is the intended catch-all. |
| `pipeline/worker.py`, `doctor/checks.py` `guarded` | log, or report `fail` | Diagnostic only. |

### 2.6 Lint and format probes

- `ruff check --config pyproject.toml` (ruff 0.17.6) accepts `except Exception as exc:`
  when the handler calls `logger.<level>(..., exc_info=exc)`, whether it then returns
  a value or raises `typer.Exit(...) from None`. This is the same pattern as
  `doctor/checks.py` `guarded`, so no `noqa` is needed for BLE001.
- This plan's Python snippets were checked with `uv run --locked ruff format --check
  docs/steps/19-library-errors.md`.

## 3. Files

| File | Change |
|---|---|
| `src/earmark/transcribe/base.py` | New `NoSpeechError(PermanentError)`. `UnusableAudioError` docstring narrowed. `CachedTranscriber` logic unchanged: it deletes on `UnusableAudioError` only. |
| `src/earmark/transcribe/faster_whisper.py` | New `AudioDecodeError`, `DECODE_ERROR_NAMES`, `LANGUAGE_ERROR_MARK`. `load_runtime` also imports `av` and `av.error`, and its `decode_audio` closure classifies (5.2). `FasterWhisperEngine.transcribe_file` catches only `AudioDecodeError` (5.3), narrows the `ValueError` mapping (5.4), and raises `NoSpeechError` (5.5). New private `_audio_streams`. |
| `src/earmark/transcribe/remote.py` | `_WireTranscription.text` required (5.6). Empty text → `NoSpeechError`. |
| `src/earmark/transcribe/compat.py` | New, light. Known-incompatible pairs, metadata lookup, version parsing (4.5). |
| `src/earmark/doctor/__init__.py` | `Probes.installed` injection point (4.6). |
| `src/earmark/doctor/checks.py` | New `asr libraries` check (4.6). |
| `src/earmark/cli.py` | `transcribe_command` handles unexpected exceptions (5.7). |
| `tests/whisper_fakes.py` | `FakeRuntime` docs: `decode_error` is an `AudioDecodeError` for bad input. New `StubExtra` (stub `faster_whisper`, `ctranslate2`, `av`, `av.error` modules) (4.7). |
| `tests/test_faster_whisper.py` | Updated and new tests (6). `test_real_runtime_wiring` registers the av stubs. |
| `tests/test_transcribe_base.py` | `NoSpeechError` keeps the audio. |
| `tests/test_remote_asr.py` | No-speech and error-shaped-200 tests (6). |
| `tests/test_transcribe_compat.py` | New (6). |
| `tests/test_doctor.py` | `WhisperStubs` also registers `av` and `av.error`. New `asr libraries` tests. |
| `tests/test_cli.py` | `test_transcribe_unusable_audio_exits_1` uses `AudioDecodeError`. New unexpected-error test. |
| `tests/test_faster_whisper_slow.py` | New slow test with real PyAV 18.1 (6, AC1-slow). |
| `docs/ARCHITECTURE.md` | Updated: Transcribing (shell, engine, errors, remote), Doctor, module layout, data layout (section 8). |

No dependency is added or changed. `pyproject.toml`, `uv.lock`, and
`.github/dependabot.yml` stay as they are: the `av>=18.1,<19` pin and the ignore rule
remain until a faster-whisper release supports PyAV 19.

## 4. Interfaces

### 4.1 `earmark.transcribe.base`

```python
class UnusableAudioError(PermanentError):
    """The file is not decodable audio, or decodes to no samples. The audio is deleted."""


class NoSpeechError(PermanentError):
    """The audio decoded but the engine found no speech. Permanent; the audio is kept."""
```

`NoSpeechError` is **not** a subclass of `UnusableAudioError`. `CachedTranscriber` is
unchanged. It deletes the episode's audio on success, on a cache hit, and on
`UnusableAudioError`, and keeps it on everything else. `NoSpeechError` falls in the
"everything else" group because of that subclassing.

### 4.2 `earmark.transcribe.faster_whisper`: new names

```python
DECODE_ERROR_NAMES: Final = ("InvalidDataError", "EOFError")  # attributes of av.error
LANGUAGE_ERROR_MARK: Final = "not a valid language code"
NO_AUDIO_STREAM: Final = "no audio stream"


class AudioDecodeError(Exception):
    """Raised by ``WhisperRuntime.decode_audio`` when the input itself is not decodable audio."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
```

`AudioDecodeError` is internal to the runtime-to-engine boundary. It is deliberately
not an `EarmarkError`: if it ever escaped the engine, the runner would treat it as
unexpected (transient) and not as a job outcome.

`WhisperRuntime` keeps its three fields. Its `decode_audio` contract becomes:

> Return the decoded samples, or raise `AudioDecodeError` when the input is not
> decodable audio. Any other exception is a library or environment problem and
> propagates.

### 4.3 `load_runtime()`

```python
def load_runtime() -> WhisperRuntime: ...
```

- Imports, in this order: `faster_whisper`, `ctranslate2`, `av`, `av.error`, all through
  `importlib.import_module`. The `MissingExtraError` mapping is unchanged: a
  `ModuleNotFoundError` naming `faster_whisper` or `ctranslate2` → "not installed", and
  any other `ImportError` (including `av`) → "failed to import".
- `bad_input: tuple[type[Exception], ...]` is built once per call. It holds each
  `getattr(av_error, name, None)` for `name in DECODE_ERROR_NAMES` that is a class and
  a subclass of `Exception`. A name missing from a future PyAV is skipped, so less is
  classified as bad audio, which is the safe direction. An empty tuple is valid
  (`except ():` catches nothing).
- `decode_audio(path)`:

  ```python
  def decode_audio(path: Path) -> DecodedAudio:
      try:
          samples = fw.decode_audio(str(path), sampling_rate=SAMPLE_RATE)
      except bad_input as exc:
          raise AudioDecodeError(_decode_detail(exc)) from exc
      except IndexError as exc:
          if _audio_streams(av, path) == 0:
              raise AudioDecodeError(NO_AUDIO_STREAM) from exc
          raise
      return DecodedAudio(samples, duration_s=len(samples) / SAMPLE_RATE)
  ```

- `load_model` and `cuda_device_count` are unchanged.

```python
def _audio_streams(av: types.ModuleType, path: Path) -> int | None:
    """Count the audio streams with a plain ``av.open``; None when the probe itself fails."""
    try:
        with av.open(str(path)) as container:
            return len(container.streams.audio)
    except Exception as exc:
        logger.debug("could not count the audio streams of %s", path.name, exc_info=exc)
        return None
```

`av.open` is called with no keyword arguments (2.3). The `except Exception` is
deliberate: any failure of the probe means "cannot confirm", and the caller then
re-raises the original `IndexError`.

### 4.4 `FasterWhisperEngine.transcribe_file`: changed parts only

```python
try:
    decoded = self._runtime.decode_audio(audio)
except AudioDecodeError as exc:
    raise UnusableAudioError(f"could not decode {audio.name}: {_collapse(exc.detail)}") from exc
if decoded.duration_s <= 0:
    raise UnusableAudioError(f"{audio.name} contains no audio")
```

The `except MemoryError: raise` and `except Exception` clauses are removed. The mapping
of the `transcribe(...)` call is in 5.4, and the no-speech case is in 5.5.

### 4.5 `earmark.transcribe.compat` (new, light)

Imports only `json`, `re`, `importlib.metadata`, `collections.abc`, `dataclasses`, and
`typing`. It never imports the packages it checks.

```python
@dataclass(frozen=True, slots=True)
class Installed:
    version: str
    from_source: bool  # direct_url.json has vcs_info or dir_info (PEP 610)


@dataclass(frozen=True, slots=True)
class Incompatibility:
    package: str  # distribution name, e.g. "faster-whisper"
    up_to: tuple[int, ...]  # affected releases of `package`: release <= up_to
    other: str  # e.g. "av"
    from_other: tuple[int, ...]  # affected releases of `other`: release >= from_other
    effect: str  # one clause: what breaks


@dataclass(frozen=True, slots=True)
class CompatResult:
    status: Literal["ok", "incompatible", "unknown"]
    detail: str


type Lookup = Callable[[str], Installed | None]

TRANSCRIBE_PACKAGES: Final = ("faster-whisper", "av")
KNOWN_INCOMPATIBLE: Final[tuple[Incompatibility, ...]] = (
    Incompatibility(
        package="faster-whisper",
        up_to=(1, 2, 1),
        other="av",
        from_other=(19,),
        effect=(
            "faster-whisper passes av.open(metadata_errors=...), which PyAV 19 removed, "
            "so every file fails to decode"
        ),
    ),
)
MAX_VERSION_CHARS: Final = 40


def installed(name: str) -> Installed | None: ...


def release(version: str) -> tuple[int, ...] | None: ...


def check_transcribe_packages(lookup: Lookup = installed) -> CompatResult: ...
```

- `installed(name)`: `dist = importlib.metadata.distribution(name)`, where
  `PackageNotFoundError` gives `None`. `version = dist.version`, and if it is not a
  non-empty `str`, return `None`. `from_source`: `raw = dist.read_text("direct_url.json")`.
  If it is a string, `json.loads` it; when the result is a dict containing `"vcs_info"`
  or `"dir_info"`, `from_source=True`. An `OSError` or `ValueError` while reading or
  parsing gives `False`.
- `release(version)`: `re.match(r"\s*(?:\d+!)?(\d+(?:\.\d+)*)", version)`. No match →
  `None`. Otherwise the dotted integers, with trailing zeros stripped but at least one
  element kept. So `"19.0.2"` → `(19, 0, 2)`, `"19.0.0rc1"` → `(19,)`, `"1.2.1.post1"`
  → `(1, 2, 1)`, `"1.2.1.0"` → `(1, 2, 1)`, `"1!2.0"` → `(2,)`, `"0"` → `(0,)`,
  `"dev"` → `None`. Comparison is plain tuple comparison after stripping. This
  ignores the pre-, post-, and dev-release ordering on purpose: "1.2.2rc1" counts as
  fixed, and "19.0.0rc1" counts as PyAV 19.
- `check_transcribe_packages(lookup)`:
  1. `found = {name: lookup(name) for name in TRANSCRIBE_PACKAGES}`. Each version is
     shown as `" ".join(v.split())[:MAX_VERSION_CHARS]`.
  2. If any value is `None`, or `release(version)` is `None` → `unknown`, detail
     `cannot read the installed version of {names joined with " and "}; not checked
     against known-incompatible pairs`.
  3. For each `Incompatibility`: skip it if either package is `from_source`. Otherwise,
     if `release(pkg) <= up_to` and `release(other) >= from_other` → `incompatible`,
     detail `faster-whisper {v} with av {v} is a known-incompatible pair: {effect};
     reinstall the locked versions with `uv sync --locked --extra transcribe``.
  4. Otherwise `ok`, detail `faster-whisper {v}, av {v}`. Append `; faster-whisper
     installed from a source tree, so its version is not checked` (or the same for
     `av`, or both names joined with " and ") for each `from_source` package.

### 4.6 Doctor

`earmark.doctor.Probes` gains one field, defaulting to the real lookup:

```python
from earmark.transcribe.compat import Lookup, installed


@dataclass(frozen=True, slots=True)
class Probes:
    http_transport: httpx2.BaseTransport | None = None
    fetcher: GuardedFetcher | None = None
    anthropic_http_client: httpx2.Client | None = None
    timeout_s: float = ONLINE_TIMEOUT_S
    installed: Lookup = installed  # package metadata, for the asr libraries check
```

`OfflineChecks.run`: when `asr_backend == "faster_whisper"` and the `asr` result is
`ok`, append `guarded("asr libraries", self.asr_libraries)` **before** `asr model`.

```python
def asr_libraries(self) -> CheckResult: ...
```

It calls `check_transcribe_packages(self.probes.installed)`. `ok` → `CheckResult("asr
libraries", "ok", detail)`. `incompatible` and `unknown` → `"warn"` with the detail.
The check loads no model and imports nothing new. Its status is `warn`, not `fail`, as
the brief says, so `earmark doctor` still exits 0.

Example lines:

```
ok    asr libraries  faster-whisper 1.2.1, av 18.1.0
warn  asr libraries  faster-whisper 1.2.1 with av 19.0.2 is a known-incompatible pair: faster-whisper passes av.open(metadata_errors=...), which PyAV 19 removed, so every file fails to decode; reinstall the locked versions with `uv sync --locked --extra transcribe`
```

### 4.7 Test helpers (`tests/whisper_fakes.py`)

`FakeRuntime` is unchanged in code. Its docstring and the tests say that
`decode_error = AudioDecodeError("...")` simulates bad input, and any other exception
simulates a library or environment failure.

New `StubExtra` installs stub modules so the **real** `load_runtime()` closure runs
without the extra:

```python
class StubExtra:
    """Stub faster_whisper, ctranslate2, av, and av.error modules for the real load_runtime."""

    def __init__(
        self,
        model: FakeWhisperModel,
        *,
        decode: Callable[[str], Sequence[float]],
        audio_streams: int | BaseException = 1,
        cuda_devices: int = 0,
    ) -> None: ...

    decode_calls: list[str]  # paths passed to faster_whisper.decode_audio
    open_calls: list[str]  # paths passed to av.open (the probe)
    errors: types.ModuleType  # the stub av.error module

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None: ...
```

- The stub `av.error` module mirrors PyAV 18.1's MROs (2.1):
  `FFmpegError(Exception)`, `InvalidDataError(FFmpegError, ValueError)`,
  `EOFError(FFmpegError, builtins.EOFError)`,
  `FileNotFoundError(FFmpegError, builtins.FileNotFoundError)`, and
  `DecoderNotFoundError(LookupError, FFmpegError)`. Each has `strerror` and a `str()`
  of the form `[Errno N] <strerror>: '<full path>'`. Build the module with
  `types.ModuleType("av.error")` and `module.__dict__.update({...})`, so the names
  `EOFError` and `FileNotFoundError` never shadow builtins in Python source (ruff `A`).
  The test classes themselves have names like `StubEOFError`.
- The stub `av` module has `error` (the module above) and `open(path)`, which records
  the call. It raises `audio_streams` if that is an exception; otherwise it returns a
  context manager whose `.streams.audio` is a list of that length.
- The stub `faster_whisper` module has `WhisperModel(*args, **kwargs)`, which returns
  `model`, and `decode_audio(path, sampling_rate)`, which records the call and returns
  `decode(path)` (that callable may raise). The stub `ctranslate2` module has
  `get_cuda_device_count()`.
- `install` registers `faster_whisper`, `ctranslate2`, `av`, and `av.error` in
  `sys.modules` with `monkeypatch.setitem`.

`tests/test_doctor.py`'s `WhisperStubs` must also register `av` and `av.error`, because
the `asr` check calls the real `load_runtime`, which now imports them. It can reuse the
stub `av` modules from `StubExtra`, or register two plain `types.ModuleType`s with an
`error` attribute. `test_real_runtime_wiring` needs the same.

## 5. Behaviour and edge cases

### 5.1 What counts as unusable audio

| Condition | Outcome |
|---|---|
| `av.error.InvalidDataError` from `faster_whisper.decode_audio` | `UnusableAudioError("could not decode audio.mp3: Invalid data found when processing input")`; audio deleted unless `KEEP_AUDIO` |
| `av.error.EOFError` | `UnusableAudioError("could not decode audio.flac: End of file")`; deleted |
| `IndexError`, and the probe counts 0 audio streams | `UnusableAudioError("could not decode audio.mp4: no audio stream")`; deleted |
| decoded to 0 samples | `UnusableAudioError("audio.mp3 contains no audio")`; deleted |
| remote: the file is 0 bytes | `UnusableAudioError("audio.mp3 is empty")`; deleted (unchanged) |
| everything else from the decoder | propagates unchanged; audio kept |

"Everything else" includes:

- `TypeError` (PyAV 19);
- `IndexError` when the probe finds an audio stream or cannot run;
- every `OSError` subclass, PyAV's included (missing file, directory, permission,
  ENOSPC, EAGAIN, timeouts);
- `MemoryError` and `av.error.MemoryError`;
- `DecoderNotFoundError`, `DemuxerNotFoundError`, and the other `*NotFoundError`s;
- `PatchWelcomeError`, `BugError`, `UnknownError`, `ExternalError`, `UndefinedError`,
  `ArgumentError`;
- `RuntimeError`, `ValueError`, `AttributeError`, `ImportError`, and anything else.

Why `DecoderNotFoundError` and `PatchWelcomeError` are excluded: they describe what this
PyAV build can do, not the bytes. A PyAV wheel that lost a decoder would otherwise
delete every episode in that codec. Leaving them out costs a few retries, after which
the job fails with the file kept. Treat every unclear case this way: misreading bad
audio as a library error costs retries and disk, while misreading a library error as
bad audio costs the user's data.

### 5.2 The no-audio-stream probe

- It runs only on an `IndexError` from `faster_whisper.decode_audio`, so files that
  decode normally are opened once, as before.
- `_audio_streams` returns 0 → `AudioDecodeError(NO_AUDIO_STREAM)`, chained from the
  `IndexError`. It returns a positive count or `None` → the **original** `IndexError` is
  re-raised (bare `raise`, same object).
- With PyAV 18, the probe's plain `av.open` raises `UnicodeDecodeError` on non-UTF-8
  metadata (2.3). It returns `None`, so such a file without an audio stream takes the
  unexpected path. That is rare and safe.
- The probe never runs for the PyAV 19 `TypeError`.

### 5.3 Messages and paths

- `_decode_detail(exc)` is unchanged: `exc.strerror` when it is a non-empty `str`, else
  the class name, whitespace-collapsed and capped at `MAX_DETAIL_CHARS`. It is applied
  in the runtime closure. The engine collapses and caps `exc.detail` again, because
  fakes can pass anything.
- `UnusableAudioError` messages carry the base name only, never the full path, which
  PyAV puts in `str()`.
- Unexpected exceptions reach `job.error` through the runner's existing `describe_error`
  (`unexpected error: TypeError: open() got an unexpected keyword argument
  'metadata_errors' (at audio.py:52 in decode_audio)`). For PyAV's `OSError`s that text
  includes the full audio path, as for any other unexpected error. Paths are not
  secrets, and the brief asks for the normal unexpected path. Do not map these to
  `TransientError`: that would drop the traceback.

### 5.4 `ValueError` from the model (F2)

- Around `raw_segments, info = model.transcribe(...)` only: catch `ValueError`. If
  `LANGUAGE_ERROR_MARK in str(exc)`, raise `self._value_error(exc)` (unchanged message:
  `transcription failed: <detail> (check EARMARK_ASR_LANGUAGE)`, where the hint is added
  when `options.language` is set) from `exc`. Otherwise use a bare `raise`.
- The rest of the block, from `float(info.duration)` through the segment loop, is no
  longer inside a `ValueError` handler. A `ValueError` raised while iterating propagates.
- faster-whisper 1.2.1 raises the language error in `Tokenizer.__init__`, which is
  called inside `WhisperModel.transcribe` before the generator is returned (checked in
  the installed source: `tokenizer.py` line 29, `transcribe.py` around line 963). So the
  narrowed catch still covers the configuration case. The other `ValueError`s in the
  package (`feature_extractor.py`'s stft checks, `transcribe.py`'s prompt-length check)
  are about data or the library and now take the unexpected path.

### 5.5 No speech (F3)

- faster-whisper: no segments after mapping → `NoSpeechError(f"no speech found in
  {audio.name} ({format_hms(duration)} of audio)")`, the same text as before.
- remote: empty text and no segments → `NoSpeechError(f"no speech found in
  {audio_name} (remote ASR)")`, the same text.
- `CachedTranscriber` keeps the audio. The runner fails the job permanently
  (`PermanentError`), so there is no retry loop: re-transcribing is deterministic.
  Re-sharing the episode creates a new job, and the fetcher reuses the kept audio.
- `KEEP_AUDIO` makes no difference here: the audio is kept either way.

### 5.6 Remote ASR replies without `text` (F4)

`_WireTranscription.text: str`, with no default. A JSON object without a string `text`
field fails validation → `PermanentError("remote ASR returned a response that is not a
transcription (audio.mp3)")` (the existing message). The audio is kept. Every shape in
step 09 2.3 (OpenAI, vLLM, whisper.cpp, LocalAI, speaches, text-only) includes `text`,
so none of them is affected. `{"text": ""}` with no segments is still "no speech", now
`NoSpeechError`.

### 5.7 `earmark transcribe` (F5)

After the existing `TransientError` and `PermanentError` clauses, add:

```python
except Exception as exc:
    logger.error("earmark transcribe failed", exc_info=exc)
    text = describe_error(exc, settings.secret_values())
    typer.echo(
        f"earmark transcribe: {_clean(text)} (not a problem with the audio file; "
        "`earmark doctor` checks the installed libraries)",
        err=True,
    )
    raise typer.Exit(code=1) from None
```

- `describe_error` is imported from `earmark.pipeline.runner` inside the function, next
  to the other local imports.
- The traceback goes to the log (stderr) at ERROR, and the summary is the last stderr
  line.
- `BaseException`s (`KeyboardInterrupt`) are not caught.
- The input file is never touched, as before.
- `NoSpeechError` and `UnusableAudioError` still go through the `PermanentError`
  clause.

### 5.8 Worker and job lifecycle

There is no runner change. A propagated decoder exception reaches `Pipeline.run`'s
`except Exception`: ERROR log with `exc_info`, then `jobs.fail(transient=True)`. The job
is requeued with backoff (60 s × 4^(n-1)) until `MAX_ATTEMPTS`, then `failed`. The audio
stays at every point. On the retry, `HttpAudioFetcher.fetch` returns the existing
`audio.*` without downloading.

### 5.9 Doctor edge cases

- The check runs only for `ASR_BACKEND=faster_whisper` after `asr` is `ok`. With the
  extra missing, `asr` already fails and names the extra.
- Stub modules without metadata (the default test environment) → `unknown` → `warn`.
  Tests inject `Probes(installed=...)` for every assertion about this check. Existing
  doctor tests look results up by name, so the extra line does not affect them.
- The check reads metadata only, so a known-bad pair is reported even though `asr`
  (which imports the modules) succeeded. That is exactly the PyAV 19 case, where import
  works and decoding fails.

## 6. Tests

Fixtures: none downloaded. Default tests use `FakeRuntime`, `StubExtra`, and injected
lookups. The slow test generates its inputs with the real PyAV (the extra) at run time.
It does not need the `tiny` model, because it wraps the real decode with a fake model
loader (`WhisperRuntime(load_model=lambda _o: fake_model, decode_audio=real.decode_audio,
cuda_device_count=real.cuda_device_count)`).

### AC1: only genuine decode failures are unusable audio, permanent, and delete the audio unless `KEEP_AUDIO`

`tests/test_faster_whisper.py`:

- `test_decode_error_is_unusable_audio[keep_audio=False|True]` (updated):
  `fake.decode_error = AudioDecodeError("Invalid data found when processing input")` →
  `UnusableAudioError`, message `could not decode audio.mp3: Invalid data found when
  processing input`, `__cause__` is the `AudioDecodeError`, and `audio.exists() is
  keep_audio`.
- `test_zero_duration_is_unusable` (extended): also `not audio.exists()`, and with
  `keep_audio=True` the audio exists.
- `test_real_runtime_classifies_bad_input` (parametrized, `StubExtra`): stub decode
  raising stub `InvalidDataError(path)` → `runtime.decode_audio` raises
  `AudioDecodeError` with detail `Invalid data found when processing input`, and no
  `tmp_path` in the detail. Stub `EOFError` → detail `End of file`. `__cause__` is the
  stub error in both. `open_calls == []` in both.
- `test_real_runtime_no_audio_stream`: stub decode raises `IndexError`,
  `audio_streams=0` → `AudioDecodeError("no audio stream")`, `__cause__` is the
  `IndexError`, and `open_calls == [str(path)]`.
- `test_real_runtime_unusable_through_transcriber`: `StubExtra` with an `InvalidData`
  decode, through `build_faster_whisper_transcriber` (real `load_runtime`) and
  `CachedTranscriber.transcribe` → `UnusableAudioError`, audio deleted.

`tests/test_faster_whisper_slow.py` (`@pytest.mark.slow`, real PyAV 18.1):

- `test_real_decode_classifies_inputs`: under `tmp_path`, generate a 2 s FLAC (PyAV
  `flac` encoder, mono 16 kHz) and a video-only MP4 (PyAV `mpeg4` encoder, 64×64, five
  frames). Write a text file named `.mp3`, an empty `.mp3`, and the FLAC's first 100
  bytes. Then:
  - `load_runtime().decode_audio` returns samples for the FLAC;
  - it raises `AudioDecodeError` for the text file, the empty file (detail `Invalid
    data found when processing input`), the 100-byte FLAC (`End of file`), and the MP4
    (`no audio stream`);
  - for a missing path and for a directory it raises an `OSError` that is not
    `AudioDecodeError`.

  Through a `CachedTranscriber` with the real decode and a fake model loader, the text
  file → `UnusableAudioError` and the audio is deleted.

`tests/test_transcribe_base.py`: `test_unusable_audio_deleted` is unchanged and still
passes.

### AC2: other exceptions take the unexpected path and keep the audio; the PyAV-19 `TypeError` requeues the job

`tests/test_faster_whisper.py`:

- **`test_pyav19_type_error_requeues_and_keeps_audio`** (the regression test). It uses
  `StubExtra` whose `faster_whisper.decode_audio` raises `TypeError("open() got an
  unexpected keyword argument 'metadata_errors'")`, with
  `build_faster_whisper_transcriber(settings, fakes.artifacts)` (so the real
  `load_runtime` closure runs) inside `make_env`. Then `env.jobs.enqueue(CANON)` and
  `env.worker.run_once()`. Assert:
  - `job.status == "queued"`, `job.attempts == 1`;
  - `job.error.startswith("unexpected error: TypeError: open() got an unexpected keyword
    argument 'metadata_errors'")` and `"could not decode" not in job.error`;
  - `fakes.artifacts.find_audio(key)` is not `None`, and there is no transcript;
  - `open_calls == []`;
  - an ERROR record from `earmark.pipeline.runner` whose `exc_info[0] is TypeError`
    (`caplog`).

  Then make the stub decode return `[0.0] * 32_000`, `env.clock.advance(61)`, and run
  again → `done`, the transcript exists, the audio is deleted, and `len(decode_calls)
  == 2` (the retry used the kept file).
- `test_pyav19_type_error_exhausted_keeps_audio`: the same with `make_env(...,
  max_attempts=1)` → `job.status == "failed"`, and the audio still exists.
- `test_decoder_library_errors_propagate` (parametrized over `TypeError(...)`,
  `RuntimeError("x")`, `IndexError("tuple index out of range")`,
  `PermissionError(13, "Permission denied")`, stub
  `DecoderNotFoundError("Decoder not found", path)`, `MemoryError()`), via
  `FakeRuntime.decode_error` and `CachedTranscriber` with `keep_audio=False`:
  `pytest.raises(type(error))` and `info.value is error`, the error is not an
  `UnusableAudioError`, `audio.exists()`, and there is no transcript. This replaces
  `test_decode_error_without_strerror_names_class` and
  `test_decode_memory_error_propagates`.
- `test_real_runtime_type_error_not_classified`: `StubExtra` decode raising
  `TypeError` → `runtime.decode_audio` raises that same object, and `open_calls == []`.
- `test_real_runtime_index_error_with_audio_stream_propagates`
  (`audio_streams=1`) and `test_real_runtime_index_error_probe_fails_propagates`
  (`audio_streams=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")`):
  the original `IndexError` object propagates, and `open_calls == [str(path)]`.
- `test_real_runtime_other_av_errors_propagate`: stub `FileNotFoundError` and
  `DecoderNotFoundError` from decode propagate as the same objects.
- `test_transcribe_value_error_is_permanent` (unchanged): the language-code message is
  still a `PermanentError`, and the audio is kept.
- `test_transcribe_other_value_error_propagates`: `transcribe_error=ValueError("stft:
  expected 0 < n_fft <= 10, but got n_fft=400")` with `language="en"` → `ValueError`
  propagates (the same object, not a `PermanentError`), and the audio is kept.
- `test_value_error_while_iterating_propagates`: an `on_segment` that raises
  `ValueError("not a valid language code")` at index 1 → `ValueError` propagates, even
  with the mark in its text, because only the call is mapped. The audio is kept.

`tests/test_cli.py`:

- `test_transcribe_unusable_audio_exits_1` (updated: `decode_error =
  AudioDecodeError("bad")`), with the same assertions.
- `test_transcribe_unexpected_error_exits_1`: `whisper.decode_error =
  TypeError("open() got an unexpected keyword argument 'metadata_errors'")` → exit
  code 1. The last stderr line starts with `earmark transcribe: unexpected error:
  TypeError: open() got an unexpected keyword argument 'metadata_errors'` and contains
  `earmark doctor`. `"could not decode"` is not in stderr, stdout is empty, and the
  input file's bytes are unchanged.

### AC3: the audit; each fix has a test (the list is in 2.5)

- F1 and F5: covered above.
- F2: `test_transcribe_other_value_error_propagates` and
  `test_value_error_while_iterating_propagates`.
- F3, faster-whisper: `test_no_speech_keeps_audio[segments...][keep_audio=False|True]`
  (replaces `test_no_speech_is_unusable`). `type(info.value) is NoSpeechError`, it is a
  `PermanentError` and not an `UnusableAudioError`, the message is as before,
  `audio.exists()` for both `keep_audio` values, and there is no transcript.
- F3, worker: `test_no_speech_fails_job_and_keeps_audio` (`make_env` with a
  `FasterWhisperEngine` over a `FakeRuntime` with no segments) → `failed`, `attempts ==
  1`, error `no speech found in audio.mp3 (...)`, and the audio exists.
- F3, remote: `tests/test_remote_asr.py::test_no_speech_keeps_audio[body][keep_audio]`
  (replaces `test_no_speech_is_unusable`): `NoSpeechError`, and the file exists for
  both `keep_audio` values.
- F4: `test_unreadable_200_is_permanent` gains `{}`, `{"error": {"message": "model not
  loaded"}}`, `{"detail": "Not Found"}`, and `{"segments": []}` →
  `PermanentError("... not a transcription ...")`, not a `NoSpeechError`. New
  `test_error_shaped_200_keeps_audio`: `{"error": "busy"}` through `CachedTranscriber`
  with `keep_audio=False` → `PermanentError`, and the file exists.
- `tests/test_transcribe_base.py::test_no_speech_error_keeps_audio[keep]`:
  `RecordingEngine(error=NoSpeechError("x"))` → raised, and the audio exists for both
  values. This proves the shell does not treat it as unusable.

### AC4: doctor warns on a known-incompatible `av` / `faster-whisper` pair without loading a model

`tests/test_transcribe_compat.py` (new):

- `test_release` table: `"19.0.2"` → `(19, 0, 2)`; `"19.0.0rc1"` → `(19,)`;
  `"1.2.1.post1"` → `(1, 2, 1)`; `"1.2.1.0"` → `(1, 2, 1)`; `"1!2.0"` → `(2,)`; `"0"` →
  `(0,)`; `" 18.1.0 "` → `(18, 1)`; `"dev"` and `""` → `None`.
- `test_known_incompatible_pairs` table over `check_transcribe_packages(lookup)` with a
  dict-backed lookup:
  - (1.2.1, 19.0.2) → `incompatible`, and the detail contains both versions,
    `metadata_errors`, and `uv sync --locked --extra transcribe`;
  - (1.2.1, 18.1.0) → `ok` `faster-whisper 1.2.1, av 18.1.0`;
  - (1.2.2, 19.0.2) → `ok`; (1.3.0, 20.0) → `ok`;
  - (1.2.1.post1, 19.0.0rc1) → `incompatible`; (1.1.1, 19.0.0) → `incompatible`.
- `test_source_install_skips_rule`: faster-whisper `Installed("1.2.1",
  from_source=True)` with av 19.0.2 → `ok`, and the detail mentions the source tree.
- `test_unknown_versions`: a missing av, or a version `"weird"` → `unknown`, and the
  detail names the package.
- `test_installed_reads_metadata`: monkeypatch `importlib.metadata.distribution` with a
  fake distribution (`version`, `read_text`). Cases: no `direct_url.json` → `False`;
  `{"url": "...", "vcs_info": {...}}` → `True`; `dir_info` → `True`; invalid JSON →
  `False`; `PackageNotFoundError` → `None`.
- `test_compat_imports_light`: a subprocess runs `import earmark.transcribe.compat`,
  and none of `av`, `faster_whisper`, `ctranslate2`, `numpy`, `httpx2` is in
  `sys.modules`.

`tests/test_doctor.py` (with `WhisperStubs`, so `asr` is ok and nothing is loaded):

- `test_doctor_asr_libraries_incompatible_warns`: `Probes(installed=lookup(fw="1.2.1",
  av="19.0.2"))` → `asr libraries` is `warn` and contains `known-incompatible`.
  `stubs.models_built == 0`. The results are in the order `asr`, `asr libraries`, `asr
  model`. The doctor exit code is unaffected by this warning.
- `test_doctor_asr_libraries_ok`: (1.2.1, 18.1.0) → `ok` `faster-whisper 1.2.1, av
  18.1.0`.
- `test_doctor_asr_libraries_unknown_warns`: the lookup returns `None` → `warn`.
- `test_doctor_asr_libraries_skipped`: `asr` fails (extra missing) → no `asr libraries`
  result. The same for `ASR_BACKEND=remote` and `fake`.

### Criteria added by the planner

- **Doctor loads no model.** `test_doctor_asr_libraries_incompatible_warns` asserts
  `stubs.models_built == 0`, and the stub lookup proves the check reads only metadata.
- **Light imports stay light.** The existing `test_transcribe_modules_import_light`
  passes unchanged: `load_runtime` still imports only when called.
- **`MissingExtraError` mapping unchanged.** `test_load_runtime_missing_extra` and
  `test_load_runtime_broken_extra` pass unchanged. Add an `av`-is-`None` case
  (`monkeypatch.setitem(sys.modules, "av", None)` with stub `faster_whisper` and
  `ctranslate2`) → "failed to import".

## 7. Verification

```bash
make check
uv run --locked pytest tests/test_faster_whisper.py tests/test_transcribe_base.py \
  tests/test_remote_asr.py tests/test_transcribe_compat.py tests/test_doctor.py \
  tests/test_cli.py -q
uv run --locked ruff format --check docs/steps/19-library-errors.md
git diff --stat -- pyproject.toml uv.lock .github/   # expect no changes
```

Live smoke with the real extra. It runs in a scratch venv, never the repo `.venv`, and
`/tmp` needs about 400 MB free. It downloads no model, but `uv sync` needs network for
the wheels.

```bash
export UV_PROJECT_ENVIRONMENT=/tmp/earmark-verify/venv UV_CACHE_DIR=/tmp/earmark-verify/cache
cd /home/sandy/dev/earmark
uv sync --locked --extra transcribe
# 1. Real PyAV 18.1: the new slow decode test (no model download).
uv run --locked --extra transcribe pytest -m slow -k classifies -v -rs
# 2. Swap in PyAV 19.0.2 WITHOUT uv run (uv run would re-sync it back to 18.1).
uv pip install --python /tmp/earmark-verify/venv/bin/python av==19.0.2
V=/tmp/earmark-verify/venv/bin
mkdir -p /tmp/earmark-verify/files
$V/python -I -c "import pathlib; pathlib.Path('/tmp/earmark-verify/files/x.mp3').write_text('not audio')"
$V/python -I -c "
import sys
from pathlib import Path
from earmark.transcribe.faster_whisper import AudioDecodeError, load_runtime
try:
    load_runtime().decode_audio(Path(sys.argv[1]))
except AudioDecodeError as exc:
    sys.exit(f'WRONG: classified as bad audio: {exc}')
except TypeError as exc:
    print('ok: TypeError propagates:', exc)
" /tmp/earmark-verify/files/x.mp3
EARMARK_ASR_BACKEND=faster_whisper EARMARK_ASR_DEVICE=cpu \
  EARMARK_DATA_DIR=/tmp/earmark-verify/data $V/earmark doctor | grep 'asr libraries'
# expect: warn ... faster-whisper 1.2.1 with av 19.0.2 is a known-incompatible pair ...
rm -rf /tmp/earmark-verify
```

`earmark doctor` may report other checks as `warn` or `FAIL` in that environment (no
vault, no token). Only the `asr libraries` line matters.

An optional, fuller reproduction of the original report needs network and about 80 MB
for the `tiny` model. Under PyAV 19, `earmark transcribe` on the slow clip (with
`EARMARK_ASR_MODEL=tiny`) should exit 1 with `earmark transcribe: unexpected error:
TypeError: open() got an unexpected keyword argument 'metadata_errors' ...`, and the
clip should be unchanged.

Not verifiable here: GPU transcription and the container build (unchanged by this step).

## 8. Deviations

1. **"No speech found" no longer counts as unusable audio** (F3). Steps 08 and 09 made
   it an `UnusableAudioError`, which deleted the audio. It is now `NoSpeechError`: still
   a permanent failure, but the audio is kept. The brief's definition of unusable audio
   lists decode failures and "a file that decodes to no samples", not "no speech". A
   broken VAD, model, or server would otherwise delete every episode's audio. The cost
   is disk space for the rare episode that is genuinely silent. **Product decision for
   the user:** if you prefer the old behaviour, the change is one line (subclass
   `UnusableAudioError`).
2. **The model's `ValueError` mapping is narrowed** (F2). Step 08 mapped every
   `ValueError` from `transcribe(...)` and from iteration to a permanent error. Now only
   the language-code error from the call is mapped, following the brief's "any other
   exception from ... the model" rule.
3. **Remote replies must carry `text`** (F4). Step 09's wire model accepted a body with
   no fields at all. All the response shapes step 09 found include `text`.
4. **No-audio-stream detection probes with `av.open` after an `IndexError`.** This is
   the only place earmark calls PyAV directly. It keeps the step 08 behaviour for
   video-only and misdetected files without a broad `IndexError` catch.
5. **`earmark transcribe` prints unexpected errors as one line** (exit 1) instead of
   letting them reach Typer's traceback printer. The traceback is still logged.
6. **`docs/ARCHITECTURE.md`** changes:
   - Transcribing: the shell, engine, and errors bullets (the unusable set, `NoSpeechError`,
     what propagates, the narrowed `ValueError`) and the remote errors.
   - Doctor: a new offline check.
   - Module layout: `transcribe/compat.py`.
   - Data layout: when audio is deleted.

   Steps 08 and 09's plans are historical and are not edited. This plan supersedes them
   on these points.
7. The doctor check reports `warn`, not `fail`, as the brief says, although a
   known-incompatible pair breaks every transcription.

## 9. Out of scope

- Bumping or unpinning `av`, and changing `.github/dependabot.yml`: that waits for a
  faster-whisper release after 1.2.1.
- Refusing to start the worker, or logging a startup warning, on a known-incompatible
  pair. Only doctor checks it, per the brief. With this step, a bad pair produces
  transient failures that keep the audio, and their `job.error` names the `TypeError`.
- A doctor self-test that decodes a generated in-memory WAV, which would catch unknown
  incompatibilities as well as listed ones. It is a candidate for the backlog.
- Decoding before loading the model, so that bad audio is rejected without a model
  load. That is a behaviour change to step 08's order and not needed for this fix.
- Pretty-traceback settings for other CLI commands.
