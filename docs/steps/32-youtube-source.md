# Step 32: YouTube as a source

The brief is in `docs/PLAN.md`, "Step 32". It builds on step 24 (requests), step 25 (the mail
code), step 29 (the contract, samples and checker) and step 30 (the receiving skill).

The step can be built as briefed. One choice was the operator's, and it changed what gets
built:

- **Decision P1: does the service send `contract: 2` for all mail, or only for YouTube?**
  **Decided on 2026-11-25: option A, all mail**, as the planner recommended, and this plan
  is written for A. Section 8.1 gives the reasons, and lists what would have changed under
  **YouTube only** (option B); option B does not apply.

Three smaller choices, each **decided on 2026-11-25** as the planner recommended (section
8): a YouTube note is a new `type: video` with a new `channel` key rather than a podcast with
`show` (D3); age-gated, members-only and private videos are refused, a bot check fails the
job and is not retried, and there is no duration cap (D4); and YouTube is on by default in
both images, so a worker without the new extra refuses to start (D6).

Rules for the implementer that are not about the code itself:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Never run `uv python install` without `--no-bin`.**
- **Placeholders only** in every file this step writes: addresses at `example.org`,
  `example.net` or `example.com`, no real host name, LAN address, mail address or token.
  Public YouTube URLs are fine. Never write the googlevideo host a real extraction printed:
  use `rr1---sn-placeholder.googlevideo.com`.
- **Never copy a line of contract 1** into any Markdown file, this plan's v2 text included
  (`test_contract_text_exists_once` scans every `*.md`). Contract 2 is written as a list of
  changes to contract 1, so it repeats nothing.
- **`contract/samples/v1/*.eml` and the text between the `contract-v1` markers in
  `contract/mail-v1.md` do not change by one byte.**
- **Every change under `plugins/` needs the new plugin version** (1.1.0) and its
  `PLUGIN_RELEASES` row.
- In `README.md`, `docs/deploy.md`, `docs/shortcut.md`, `docs/runbook-spark.md` and
  `docs/asr.md`, the removed-wording scan bans, among others, `\bdigest`, `embedding`,
  `\bentit(y|ies)\b`, `\bclaude\b` and `vault`. Write "sha256", "hash", "embed link" instead.
- If you install the `youtube` extra into the repo's `.venv` to run the `slow` or live tests,
  run `uv sync --locked` afterwards, so the default suite is proven without it.
- Use a `TMPDIR` under `~/.cache/` for anything large; the sandbox's `/tmp` is a small tmpfs.

## 1. Outcome

- **A YouTube link is a request.** `POST /transcriptions` (and `earmark submit`) recognise a
  YouTube video link in any of its common forms, keep only the 11-character video id, and
  store the canonical `https://www.youtube.com/watch?v=<id>` with the new source `youtube`.
  A YouTube link that is not one video (a channel, a playlist, a search) is refused with a
  422 that says so. Every other URL behaves as in step 24.
- **The episode key and the cache follow the video id.** `youtu.be/<id>`,
  `youtube.com/watch?v=<id>&t=…`, `/shorts/<id>` and the rest give one key and one cached
  transcript. A re-share is a re-share.
- **yt-dlp downloads the audio itself.** A new stage, `YoutubeSource`, has a real backend
  `ytdlp` (`earmark/youtube/ytdlp.py`) and a fake. It reads the metadata (title, channel,
  upload date, duration), refuses live streams, premieres that have not aired, private,
  removed, members-only and age-gated videos with a permanent error that says which, picks
  an audio-only stream, and downloads it into the job's staging file. No ffmpeg, no
  cookies, no config files, no plugins, no playlists, no extractor but `youtube`.
- **Contract 2.** `contract/mail-v2.md` adds `type: video`, `origin: youtube`, the
  `channel` key, `guid: youtube:<video id>` and `source_url: https://youtu.be/<video id>`.
  Under option A the service sends contract 2 for every message. The v1 text and the v1
  samples stay, frozen; `contract/samples/v2/` holds thirteen samples built by the mail
  code. The checker and the receiver accept both versions; the plugin becomes 1.1.0.
- **The dependency.** A new optional extra, `youtube`, locks `yt-dlp[default,deno]`: yt-dlp,
  its challenge scripts `yt-dlp-ejs`, and the Deno JavaScript runtime as a PyPI wheel. Both
  images install it. `earmark doctor` reports the three versions and whether the runtime is
  there.
- **The database** moves to schema 4: migration `0004_youtube` rebuilds `jobs` so its
  `source` check allows `youtube`.
- **Docs**: deploy section 18 (YouTube, updating yt-dlp), a YouTube check in runbook phases
  3 and 4, the Shortcut doc's YouTube note and error rows, ARCHITECTURE and CLAUDE.md.

What stays unverified, and is reported as such: a real download and transcription on the
Spark (runbook phase 4), and both image builds with the new extra (runbook phase 3; CI only
builds the CPU image).

## 2. Findings

Everything below was checked on 2026-11-25 in this sandbox (aarch64, glibc 2.41) in a
scratch venv, with yt-dlp 2026.11.23, yt-dlp-ejs 0.8.0 and deno 2.9.8 from PyPI. The
sandbox reached YouTube directly, so the yt-dlp behaviour below is observed, not read.

### 2.1 What yt-dlp needs for YouTube today

Sources: yt-dlp's README "Dependencies" and `--js-runtimes` / `--remote-components`
options (master, read 2026-11-25), the wiki page "EJS" (External JS Scripts Setup Guide),
the PyPI metadata of yt-dlp 2026.11.23, and the release notes of the last twelve releases.

- **A JavaScript runtime is required for full YouTube support.** YouTube's player
  challenges (the `n` and signature transforms) are solved by external scripts,
  `yt-dlp-ejs`, run in a JS runtime. Supported: Deno (recommended, the only one enabled by
  default), Node 22+ (`--js-runtimes node`), QuickJS, and Bun (deprecated, at most 1.3.14).
  Release 2026.06.09 raised the Deno minimum to 2.3.0. Without a runtime yt-dlp drops the
  `web` and `tv_samsung` clients, and some formats go missing.
- **`yt-dlp-ejs` must match yt-dlp.** The wiki: the version "MUST match the version
  specified in yt-dlp's pyproject.toml". yt-dlp's `default` extra pins it exactly
  (`yt-dlp-ejs==0.8.0` in 2026.11.23), so locking `yt-dlp[default]` keeps the pair in step.
  The alternatives (`--remote-components ejs:npm` or `ejs:github`) fetch the scripts at run
  time from npm or GitHub; earmark allows neither.
- **Deno on Linux aarch64 comes from PyPI.** yt-dlp has a `deno` extra (`deno>=2.9.8`).
  The `deno` package ships `deno-2.9.8-py3-none-manylinux_2_27_aarch64.whl` (40 MB), which
  puts the `deno` binary in the venv's `bin/`. glibc 2.27 is below Ubuntu 24.04's 2.39 (the
  GPU image) and Debian trixie's 2.41 (the CPU image). `deno 2.9.8 (stable, release,
  aarch64-unknown-linux-gnu)` ran here. So no `apt`, no `curl | sh`, no extra layer: the
  lock installs it, and `uv lock --upgrade-package` updates it.
- **How yt-dlp finds Deno.** `yt_dlp/utils/_jsruntime.py` `_find_exe` looks first in
  `sysconfig.get_path("scripts")`, the running interpreter's `bin/`, then falls back to the
  bare name on `PATH`. earmark passes the path explicitly
  (`js_runtimes={"deno": {"path": …}}`) so a system Deno on `PATH` is never picked.
- **How yt-dlp runs Deno.** Observed command:
  `deno run --ext=js --no-code-cache --no-prompt --no-remote --no-lock
  --node-modules-dir=none --no-config --no-npm --cached-only -`, with the script on stdin and
  the EJS scripts read from the Python package ("source: python package"). Deno grants no
  file, network, environment or subprocess permission without `--allow-*` flags, so the
  player JavaScript from YouTube runs with none. Deno writes an analysis cache into
  `DENO_DIR` (`node_analysis_cache_v2`, `dep_analysis_cache_v2`, `npm/`), and works when
  that directory is not writable. It did not write an update-check file with piped stdio.
  yt-dlp passes `os.environ` to Deno, so earmark sets `DENO_DIR` around its calls (5.5).
- **ffmpeg is not needed** for an audio-only https format. yt-dlp warned "ffmpeg not found"
  only for its default `best/bestvideo+bestaudio` spec. For DASH m4a it warns "writing DASH
  m4a … Install ffmpeg to fix this automatically" (its `FixupM4a` step); `fixup: "never"`
  turns that step off. PyAV 18.1.0, the locked `av`, decoded both downloads here: the opus
  `webm` (format 251) and the AAC `m4a` (format 140) of a 19 s video, each to about
  19.0 s of 16 kHz mono.
- **Python.** yt-dlp 2026.10.26 raised its minimum to 3.11; earmark is on 3.13.
- **The lock.** In a scratch copy of the project, `uv lock` with
  `youtube = ["yt-dlp[default,deno]>=2026.11.23"]` resolved under
  `required-environments` (linux/aarch64) and added: brotli 1.2.1, brotlicffi (PyPy only),
  cffi 2.1.1, charset-normalizer 3.5.3, deno 2.9.8, mutagen 1.48.1, pycparser 3.12,
  pycryptodomex 3.24.2, requests 2.34.2, urllib3 2.8.0, websockets 17.4, yt-dlp 2026.11.23,
  yt-dlp-ejs 0.8.0. (The existing warning that `av==18.1.0` is yanked is unrelated and out of
  scope.)

### 2.2 The download design: yt-dlp downloads the audio itself

The brief asks whether yt-dlp downloads, or only extracts the stream URL for
`earmark/fetch.py`. **yt-dlp downloads.** Evidence, from a 10-minute public video (Big Buck
Bunny, `aqz-KE-bpKQ`, format 251, 10 202 210 bytes), with the format's own `http_headers`:

| Request | Result |
|---|---|
| One plain GET, no `Range` | HTTP 200, 786 432 bytes in 25 s: **31 KB/s**, about twice real time for a 128 kbps stream. A one-hour video would take about 30 minutes. |
| The same without the client headers | the same 31 KB/s |
| `Range: bytes=0-10485759` | HTTP 206, the whole 10.2 MB in **0.53 s** (19 MB/s) |
| yt-dlp's own download (`process_ie_result`) | the same 10.2 MB in 0.7 s |

- Every YouTube https audio format carries `downloader_options: {"http_chunk_size":
  10485760}`: yt-dlp requests 10 MiB ranges precisely because YouTube throttles an
  unranged GET. Each format also carries client-specific `http_headers` (the User-Agent of
  the `tv`/`visionos` client that obtained it), and the first request can answer `302` to
  another `googlevideo.com` host.
- So `fetch.py` would need a ranged, chunked downloader, the per-format headers, and the
  redirect, all new code that tracks YouTube's behaviour, and it would still need yt-dlp to
  extract and solve the URL. yt-dlp's `HttpFD` already does all of it, with its retries.
- What is lost is the address guard on the media requests, and `fetch.py`'s own size cap.
  The cap is kept by earmark (5.4). The guard is replaced by narrower rules (5.6) and stated
  as the exception the brief asks for.

### 2.3 The yt-dlp Python API, as earmark uses it

All observed with the parameters in 4.6.

- **`allowed_extractors=["youtube"]`** is matched with `re.fullmatch`, case-insensitive,
  against extractor names (`orderedSet_from_options`): exactly one extractor loads
  (`Youtube`). Generic, `youtube:tab` (channels, playlists) and the rest are not loaded.
  `extract_info(…, ie_key="Youtube")` also forces it. A channel URL with this setup raised
  `DownloadError("ERROR: No suitable extractor (Youtube) found for URL …")`.
- **No config files.** Only the CLI's `parseOpts` reads them; `YoutubeDL` never does.
- **Plugins.** `YoutubeDL.__init__` calls `load_all_plugins()` once per process unless
  already done. `load_plugins` returns early when `yt_dlp.globals.plugin_dirs.value` is
  empty (the CLI's `--no-plugin-dirs` sets exactly that) or `YTDLP_NO_PLUGINS` is set.
  earmark sets `plugin_dirs.value = []` before the first `YoutubeDL`.
- **No cookies on disk.** `cookiefile` and `cookiesfrombrowser` unset give an in-memory jar.
  earmark builds a new `YoutubeDL` per call, so no cookie outlives a job.
- **`proxy=""`** becomes `__noproxy__`: environment proxies are ignored, as in `fetch.py`.
- **`cachedir=False`**: yt-dlp writes nothing under `~/.cache/yt-dlp`.
- **One extraction per job.** `extract_info(url, download=False, process=True)` returns the
  processed info with the selected format's fields merged at the top level (`format_id`,
  `ext`, `protocol`, `url`, `filesize`, `filesize_approx`, `acodec`, `vcodec`). Passing that
  dict to `process_ie_result(info, download=True)` on a **second** `YoutubeDL` (built with
  the destination in `outtmpl`) downloads it without extracting again: the second
  instance's only message was `Downloading 1 format(s): 251`. One webpage request and two
  player API requests in all; 1.8 s to extract, 0.7 s to download 10 MB.
- **Format choice.** Plain `bestaudio` picked format 258 for Big Buck Bunny, a 384 kbps 5.1
  m4a (30 MB). `bestaudio[protocol=https][vcodec=none][audio_channels<=2][abr<=192]` picked
  251 (opus, stereo, 128 kbps, 10 MB), and 251 for the 19 s video too. Whisper hears 16 kHz
  mono, so stereo at about 128 kbps loses nothing.
- **`max_filesize` is silent.** With `max_filesize` below the size, yt-dlp skipped the
  download: no exception, no file, `requested_downloads` without a path. earmark does not
  pass it (5.4).
- **A progress hook can stop a download.** A hook raising a subclass of
  `yt_dlp.utils.DownloadCancelled` propagated out of `process_ie_result` unchanged (not
  wrapped), and left `<name>.webm.part` (1 MiB) on disk.
- **`ignore_no_formats_error=True` keeps the metadata.** Without it, a video YouTube will
  not play raises `ExtractorError(reason, expected=True)` from `raise_no_formats`, and the
  info is lost. With it, the reason becomes a logger warning and `extract_info` returns the
  info. Observed:

  | Video | `live_status` | `availability` | `age_limit` | formats | warning text (start) |
  |---|---|---|---|---|---|
  | `jNQXAC9IVRw` ("Me at the zoo") | `not_live` | `public` | 0 | 25 | none |
  | `aaaaaaaaaaa` (no such video) | none | none | 0 | 0 | `[youtube] This video is unavailable` |
  | `yZIXLfi8CZQ` (private, from yt-dlp's tests) | none | none | 0 | 0 | `[youtube] Private video. If the owner of this video has granted you access, please sign in. …` |
  | `Cr381pDsSsA` (removed) | none | none | 0 | 0 | `[youtube] This video has been removed for violating YouTube's Terms of Service` |
  | `HtVdAasjOgU` (age-gated) | `not_live` | `needs_auth` | 18 | 10, none audio-only https | `… Some web_embedded client https formats have been skipped …` |
  | `jfKfPfyJRdk` (24/7 stream) | `post_live` | `public` | 0 | 0 | `[youtube] This live stream recording is not available.` |

  For the unplayable ones, `title` is the placeholder `youtube video #<id>` and `channel`,
  `upload_date` and `duration` are `None`. Each is followed by `No video formats found!` and
  `Requested format is not available`.
- **Errors.** `YoutubeDL.trouble` raises `DownloadError(message, exc_info)` with
  `exc_info = sys.exc_info()` at report time, so `exc_info[1]` is the `ExtractorError`.
  `ExtractorError` has `orig_msg`, `expected`, `cause` and `video_id`, and sets
  `expected=True` when raised while handling a network exception. `GeoRestrictedError` is
  an `ExtractorError`. `yt_dlp.networking.exceptions`: `HTTPError(RequestError)` with
  `.status`; `TransportError(RequestError)` with the subclasses `IncompleteRead`,
  `SSLError`, `CertificateVerifyError`, `ProxyError`. `ContentTooShortError` is in
  `yt_dlp.utils`. None of these is an `OSError`.
- **Logging.** With `logger` set, `to_screen` and debug lines go to `logger.debug`,
  warnings to `logger.warning`, errors to `logger.error`. One line escapes it:
  `utils.parse_codecs` writes `WARNING: Unknown codec iamf.001.001.Opus` straight to stderr
  (a codec name, no URL). Debug lines carry googlevideo URLs with the server's IP in the
  query, so earmark gates them (5.7).
- **Dead ends.** `YoutubeDL.params` can be mutated after construction to set `outtmpl` (it
  worked), but a second instance is cleaner and is what this plan uses. A `before_dl`
  post-processor could check the format, but the brief forbids post-processing, and the
  check is simpler on the processed info.

### 2.4 YouTube link forms

From yt-dlp's `YoutubeIE._VALID_URL` and its tests: `youtu.be/<id>`;
`(www.|m.|music.)youtube.com/watch?v=<id>`; `/shorts/<id>`, `/live/<id>`, `/embed/<id>`,
`/v/<id>`; `youtube-nocookie.com/embed/<id>`. Ids are 11 characters of `A-Za-z0-9_-`. Two
11-character path words are not ids: `/embed/videoseries` (a playlist embed) and
`/embed/live_stream` (a channel's live page); yt-dlp excludes both. The YouTube app's share
sheet gives `https://youtu.be/<id>?si=<tracking>`, which is what the operator shared.

### 2.5 The repo, where this step touches it

- `jobs.source` has `CHECK (source IN ('apple_podcasts', 'upload', 'url'))` (migration
  `0002`). SQLite cannot alter a check, so the table is rebuilt. The rebuild in 4.4 was run
  here on a schema-3 database in WAL mode inside `BEGIN IMMEDIATE`: rows identical,
  `application_id` kept, `integrity_check` ok, the partial unique index still enforced, a
  `youtube` row accepted and `vimeo` refused, columns in the same order.
- Branches that treat "not audio" as "podcast" break for a third kind:
  `mail/note.py` (`podcast = episode.source not in AUDIO_SOURCES`, `note_guid`), and
  `runner._stage_work`'s final `else` (it means `url`). Each becomes explicit.
- `test_contract_text_exists_once` forbids any 40-character line of the v1 block in any
  Markdown file. `test_unknown_contract` (receiver) uses `contract: 2` as its unknown
  version and must move to 3. `test_architecture_mail_contract_provenance` pins the heading
  `#### The mail contract (\`contract: 1\`)`. `test_architecture_system_seams` wants
  `contract: 1` and `contract/samples/v1/` in the mail row. `test_dockerfile_default_extras_exist`
  pins the CPU default extras, `test_gpu_venv_from_lock` the GPU sync flags,
  `NETWORK_COMMANDS` / the slow-job constants in `tests/test_ci.py` the workflow commands,
  and `test_shortcut_error_table_matches_api` one trigger per error row, in order.
- `test_quickstart_uses_fake_backends` enumerates every `*_backend` setting, so the README
  quickstart must set `EARMARK_YOUTUBE_BACKEND=fake`.

## 3. Files

**Service (`src/earmark/`)**

| File | Change |
|---|---|
| `models.py` | `Source` gains `"youtube"`; `YOUTUBE_SHOW_ID`, `GUID_YOUTUBE_PREFIX`, `YOUTUBE_ID`, `youtube_key`; `source_key` validates YouTube episodes |
| `youtube/__init__.py` | new, docstring only |
| `youtube/links.py` | new, light (stdlib, models, `resolve.apple` errors): link parsing, canonical URLs, `youtube_episode` |
| `youtube/ytdlp.py` | new: the `ytdlp` backend; imports yt-dlp only inside `load_runtime` |
| `resolve/apple.py` | `UrlProblem` and `MESSAGES` gain `youtube_not_video` and `youtube_bad_id` |
| `sources.py` | `parse_source_url` recognises YouTube hosts (with or without a scheme) |
| `submission.py` | the enqueue log names `YouTube video <id>` |
| `db.py` | migration `0004_youtube`; `SCHEMA_VERSION` 4 |
| `jobs.py` | comments only (`NewJob.url` for youtube is canonical) |
| `staging.py` | `release` clears the downloads of `youtube` jobs too |
| `config.py` | `YoutubeBackend = Literal["ytdlp", "fake"]`; `youtube_backend` |
| `pipeline/stages.py` | `YoutubeSource` Protocol; `Backends.youtube` |
| `pipeline/factory.py` | `YOUTUBE_SOURCES` registry; built in `build_backends`; logged |
| `pipeline/runner.py` | the `youtube` branch; explicit `url` branch |
| `fakes.py` | `FakeYoutubeSource`, `YoutubeResolveCall`, `YoutubeDownloadCall`; in `make_fake_backends` |
| `mail/note.py` | `CONTRACT = 2`; the video note |
| `notify/ntfy.py` | the YouTube label |
| `api/schemas.py` | comment only (`url` of a youtube job is the canonical URL) |
| `log.py` | `QUIET_LOGGERS` gains `"yt_dlp"` |
| `doctor/checks.py` | the `youtube` line |

**Plugin (`plugins/earmark-pka/`)**

| File | Change |
|---|---|
| `.claude-plugin/plugin.json` | version `1.1.0` |
| `skills/earmark-receive/scripts/earmark_mail_contract.py` | contracts 1 and 2 (4.11) |
| `skills/earmark-receive/scripts/earmark_receive.py` | accepts contracts 1 and 2 |
| `skills/earmark-receive/SKILL.md` | one paragraph on the note types (4.12) |

`check_mail.py` and `receive.py` (the launchers) do not change.

**Contract (`contract/`)**

| File | Change |
|---|---|
| `mail-v2.md` | new: the contract 2 text (4.10) |
| `samples/v2/*.eml` | new: thirteen samples, `make samples` |
| `samples/v2/README.md` | new: what each sample shows |
| `README.md` | the versions table, both sample sets, what is frozen |
| `samples/v1/README.md` | its first paragraph says the v1 samples are frozen since step 32 (D9) |

**Build, deploy, CI**

| File | Change |
|---|---|
| `pyproject.toml` | extra `youtube = ["yt-dlp[default,deno]>=2026.11.23"]` |
| `uv.lock` | `uv lock` |
| `Dockerfile` | `ARG EXTRAS="transcribe youtube"` |
| `Dockerfile.gpu` | `--extra youtube` on both `uv sync` lines; a runtime-stage `RUN` that runs `deno --version` and imports `yt_dlp` and `yt_dlp_ejs` |
| `compose.yaml` | `EXTRAS: ${EXTRAS:-transcribe youtube}` |
| `.github/workflows/container.yml` | two checks on the CPU image: `deno --version`, `import yt_dlp, yt_dlp_ejs` |
| `.github/workflows/network.yml` | the `slow` job syncs and runs with `--extra transcribe --extra youtube` |
| `.env.example` | `EARMARK_YOUTUBE_BACKEND=ytdlp` in the backends block |
| `Makefile` | unchanged (`make samples` still runs `python -m tests.contract_samples`) |

**Tests** (section 6 has the names)

| File | Change |
|---|---|
| `tests/fixtures/youtube/*.json` | new: recorded metadata (6.10) |
| `tests/ytdlp_fakes.py` | new: a fake yt-dlp runtime, session and error classes |
| `tests/test_youtube_links.py` | new |
| `tests/test_ytdlp.py` | new: the backend on the fake runtime |
| `tests/test_ytdlp_real.py` | new, `slow`: the real library, offline |
| `tests/test_youtube_live.py` | new, `network`, opt-in |
| `tests/contract_samples.py` | v2 generator; v1 frozen (4.13) |
| `tests/receiver_helpers.py`, `tests/checker_helpers.py` | a sample version argument |
| `tests/factories.py` | `new_youtube_job` |
| `tests/test_contract_samples.py`, `test_mail_checker.py`, `test_receiver.py`, `test_plugin.py`, `test_checker_floor.py`, `test_note.py`, `test_message.py`, `test_runner.py`, `test_e2e.py`, `test_api.py`, `test_sources.py`, `test_submission.py`, `test_jobs.py`, `test_db.py`, `test_staging.py`, `test_ntfy.py`, `test_leaks.py`, `test_doctor.py`, `test_factory.py`, `test_config.py`, `test_stages.py`, `test_fakes.py`, `test_log.py`, `test_deploy.py`, `test_deploy_gpu.py`, `test_ci.py`, `test_docs.py` | extended (section 6) |

**Docs**

| File | Change |
|---|---|
| `docs/ARCHITECTURE.md` | 4.15 |
| `CLAUDE.md` | 4.15 |
| `docs/PLAN.md` | the seam row names contract 2; the backlog's "Other sources" drops YouTube |
| `docs/deploy.md` | new section 18; sections 1, 4, 5 and 15 touched (4.16) |
| `docs/runbook-spark.md` | phases 3 and 4 (4.16) |
| `docs/shortcut.md` | YouTube through the URL branch; two error rows (4.16) |
| `README.md` | the source list, the settings row, the quickstart's fake backend, the extras |

## 4. Interfaces

### 4.1 Models (`models.py`)

```python
Source = Literal["apple_podcasts", "upload", "url", "youtube"]
YOUTUBE_SHOW_ID: Final = "youtube"  # synthetic show id of a YouTube episode
GUID_YOUTUBE_PREFIX: Final = "youtube:"
YOUTUBE_ID: Final = re.compile(r"[A-Za-z0-9_-]{11}")


def youtube_key(video_id: str) -> str:
    """episode_key("youtube", "youtube", "youtube:" + video_id); ValueError if not an id."""
```

- `source_key("youtube", show_id, guid)` requires `show_id == YOUTUBE_SHOW_ID` and
  `guid == "youtube:" + <id>` with `YOUTUBE_ID.fullmatch(<id>)`, else `ValueError`, and
  returns `youtube_key(<id>)`. `AUDIO_SOURCES` stays `{"upload", "url"}`.
- A YouTube `Episode`: `key = youtube_key(id)`, `source = "youtube"`,
  `show_id = "youtube"`, `episode_id = <id>`, `guid = "youtube:<id>"`, `title` (cleaned,
  at most 500, may be `""`), `show_title` = the channel (cleaned, at most 500, `""` when
  unknown), `published_at` and `published_date_only` (5.3), `duration_s` (or `None`),
  `audio_url = ""` (stream URLs are never stored: they expire and carry the server's IP),
  `feed_url = None`, `url = "https://www.youtube.com/watch?v=<id>"`, `description = ""`,
  `feed_guid = None`.
- `Job`: no new field. A `youtube` job has `url` (the canonical URL), so `_kind_requirements`
  already holds.

### 4.2 Links (`youtube/links.py`)

Standard library, `earmark.models` and `earmark.resolve.apple` only.

```text
SHORT_HOSTS: Final = frozenset({"youtu.be"})
WATCH_HOSTS: Final = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
EMBED_HOSTS: Final = frozenset({"youtube-nocookie.com", "www.youtube-nocookie.com"})
VIDEO_PATHS: Final = frozenset({"shorts", "live", "embed", "v"})   # /<word>/<id> on WATCH_HOSTS
NOT_IDS: Final = frozenset({"videoseries", "live_stream"})

def is_youtube_host(host: str) -> bool:
    """youtu.be, youtube.com and any *.youtube.com, youtube-nocookie.com and any subdomain."""

def video_id_of(parts: urllib.parse.SplitResult) -> str:
    """The video id of a YouTube link. Raises UnsupportedUrlError (youtube_not_video, youtube_bad_id)."""

def is_video_id(text: str) -> bool
def canonical_youtube_url(video_id: str) -> str      # https://www.youtube.com/watch?v=<id>
def short_youtube_url(video_id: str) -> str          # https://youtu.be/<id>
def video_id_from_canonical(url: str) -> str         # exact canonical form only; PermanentError otherwise
def youtube_episode(
    video_id: str, *, title: str, channel: str, published_at: datetime | None,
    published_date_only: bool, duration_s: float | None,
) -> Episode
```

`video_id_of(parts)`, with `host = parts.hostname.rstrip(".")` (already lower-case) and
`segments` the non-empty path segments:

1. `host in SHORT_HOSTS`: exactly one segment, which is the candidate; otherwise
   `youtube_bad_id`.
2. `host in WATCH_HOSTS` and `segments == ["watch"]` (the word compared lower-cased): the
   values of `v` in `parse_qsl(query, keep_blank_values=True)`; exactly one distinct
   non-empty value is the candidate, else `youtube_bad_id`.
3. `host in WATCH_HOSTS`, first segment lower-cased in `VIDEO_PATHS`, or `host in
   EMBED_HOSTS` with first segment `embed`: two segments, the second is the candidate; one
   segment is `youtube_bad_id`; more is `youtube_not_video`.
4. Anything else on a YouTube host (`/@name`, `/channel/…`, `/c/…`, `/user/…`,
   `/playlist`, `/results`, `/feed/…`, `/`, `studio.youtube.com`, …): `youtube_not_video`.
5. A candidate in `NOT_IDS` is `youtube_not_video`. A candidate that is not
   `YOUTUBE_ID.fullmatch` is `youtube_bad_id`. The id keeps its case.

Query parameters (`t`, `si`, `list`, `index`, `feature`, `pp`, …) and the fragment are
dropped.

`resolve/apple.py` additions:

| Problem | Message |
|---|---|
| `youtube_not_video` | `This YouTube link is not a single video. Share a video, not a channel, playlist or search.` |
| `youtube_bad_id` | `This YouTube link has no valid video id.` |

### 4.3 Classifying a request URL (`sources.py`)

`SourceUrl.source: Literal["apple_podcasts", "url", "youtube"]`. In `parse_source_url`:

- **Without a scheme**: when the host of `"https://" + text` is an Apple host (as today) or
  `is_youtube_host`, parse it as https. Any other schemeless text stays `malformed`.
- **With a scheme**, after the existing scheme and host checks and before the Apple check
  falls through to `url`: when `is_youtube_host(host)`: `"@" in netloc` is `credentials`,
  a port is `malformed` (as for Apple), otherwise
  `SourceUrl("youtube", canonical_youtube_url(video_id_of(parts)))`. `http` is accepted and
  canonicalised to https.
- Every other host keeps step 24's behaviour exactly.

`Intake.submit_url` logs `YouTube video <id>` as the subject for a youtube job (the id
comes from `video_id_from_canonical(parsed.url)`), the canonical Apple URL for a podcast,
and `audio URL on <host>` otherwise. The dedupe subject is the canonical URL, so two forms
of one link with the same `to` and instructions are one request.

### 4.4 Migration `0004_youtube` (`db.py`)

Appended to `MIGRATIONS`; `SCHEMA_VERSION` becomes 4. The statements, in order:

```sql
CREATE TABLE jobs_0004 (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('ingest')),
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'skipped')),
    stage TEXT,
    url TEXT,
    episode_key TEXT,
    dedupe_key TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    run_after TEXT NOT NULL,
    error TEXT,
    warnings TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings)),
    result TEXT,
    shared_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    heartbeat_at TEXT,
    lease_token TEXT,
    source TEXT NOT NULL DEFAULT 'apple_podcasts'
        CHECK (source IN ('apple_podcasts', 'upload', 'url', 'youtube')),
    recipient TEXT,
    instructions TEXT NOT NULL DEFAULT '',
    title TEXT,
    upload_file TEXT,
    upload_name TEXT,
    content_sha256 TEXT,
    utc_offset_min INTEGER CHECK (utc_offset_min BETWEEN -1439 AND 1439),
    delivery_state TEXT CHECK (delivery_state IN ('sending', 'sent')),
    message_id TEXT,
    sent_at TEXT,
    resent INTEGER NOT NULL DEFAULT 0 CHECK (resent IN (0, 1))
) STRICT
```

then `INSERT INTO jobs_0004 (<the 30 columns>) SELECT <the 30 columns> FROM jobs`,
`DROP TABLE jobs`, `ALTER TABLE jobs_0004 RENAME TO jobs`, and the three indexes exactly as
`0001` and `0002` created them (`jobs_active_dedupe` partial unique on `dedupe_key WHERE
status IN ('queued', 'running')`, `jobs_by_status (status, created_at, seq)`,
`jobs_by_dedupe (dedupe_key, status, updated_at)`). The column list is written out (no
`SELECT *`), in the order above, which is the current order. The existing `migrate`
transaction (`BEGIN IMMEDIATE`, identity checked inside) wraps it. No other migration
changes.

### 4.5 The stage (`pipeline/stages.py`)

```python
class YoutubeSource(Protocol):
    def resolve(self, video_id: str, /) -> Episode:
        """The video's metadata as an Episode. Raises PermanentError for a video earmark
        refuses (live, not yet aired, private, removed, members-only, age-gated), and
        Transient/PermanentError as fetching does."""
        ...

    def download(self, video_id: str, destination: Callable[[str], Path], /) -> Path:
        """Download an audio-only stream to destination(ext) and return the path. Reuses
        the extraction of the resolve just before it. Never caches."""
        ...
```

`Backends` gains `youtube: YoutubeSource`, after `fetcher` (both constructions use
keywords).

### 4.6 The `ytdlp` backend (`youtube/ytdlp.py`)

Imports at module level: standard library, `earmark.config`, `earmark.errors`,
`earmark.models`, `earmark.resolve.text.clean_text`, `earmark.youtube.links`. yt-dlp is
imported only in `load_runtime`, with `importlib.import_module` (the modules are untyped and
absent in CI, so a plain `import` would fail mypy).

```text
EXTRA: Final = "youtube"
INSTALL_HINT: Final = "install it with `uv sync --extra youtube`"
AUDIO_FORMAT: Final = (
    "bestaudio[protocol=https][vcodec=none][audio_channels<=2][abr<=192]"
    "/bestaudio[protocol=https][vcodec=none]"
)
MEDIA_HOST_SUFFIX: Final = ".googlevideo.com"
SOCKET_TIMEOUT_S: Final = 30
RETRIES: Final = 3
DOWNLOAD_DEADLINE_S: Final = 3600.0
PENDING_MAX_AGE_S: Final = 1800.0
MAX_WARNINGS: Final = 20            # kept per call, each cut to 500 characters
YTDLP_LOGGER: Final = "yt_dlp"

class MissingYoutubeExtraError(ConfigError): ...

@dataclass(frozen=True, slots=True)
class YtDlpErrors:                 # the yt-dlp classes classify_error needs
    download_error: type[Exception]        # yt_dlp.utils.DownloadError
    extractor_error: type[Exception]       # yt_dlp.utils.ExtractorError
    geo_restricted: type[Exception]        # yt_dlp.utils.GeoRestrictedError
    download_cancelled: type[Exception]    # yt_dlp.utils.DownloadCancelled
    content_too_short: type[Exception]     # yt_dlp.utils.ContentTooShortError
    http_error: type[Exception]            # yt_dlp.networking.exceptions.HTTPError
    transport_error: type[Exception]       # yt_dlp.networking.exceptions.TransportError

class YtDlpSession(Protocol):
    def extract_info(self, url: str, *, download: bool, process: bool, ie_key: str) -> Any: ...
    def process_ie_result(self, info: Any, *, download: bool) -> Any: ...
    def close(self) -> None: ...

@dataclass(frozen=True, slots=True)
class YtDlpRuntime:
    version: str                    # yt_dlp.version.__version__
    ejs_version: str | None         # importlib.metadata.version("yt-dlp-ejs")
    deno_version: str | None        # importlib.metadata.version("deno")
    deno_path: Path                 # Path(sysconfig.get_path("scripts")) / ("deno" + EXE)
    errors: YtDlpErrors
    open_session: Callable[[dict[str, Any]], YtDlpSession]   # lambda p: yt_dlp.YoutubeDL(p)

def load_runtime() -> YtDlpRuntime
def ytdlp_params(
    *, deno_path: Path, logger: "YtDlpLog", progress_hook: Callable[[dict[str, Any]], None] | None,
    outtmpl: str | None,
) -> dict[str, Any]
class YtDlpLog:                    # debug, info, warning, error; .warnings: list[str]
def deno_environment(deno_dir: Path) -> AbstractContextManager[None]
def episode_from_info(video_id: str, info: Mapping[str, Any]) -> Episode
def check_playable(video_id: str, info: Mapping[str, Any], warnings: Sequence[str]) -> None
def reason_from_text(text: str) -> str          # a key of REFUSALS
def classify_error(exc: Exception, errors: YtDlpErrors, warnings: Sequence[str]) -> EarmarkError | None

class YtDlpYoutubeSource:
    def __init__(self, runtime: YtDlpRuntime, *, max_bytes: int, deno_dir: Path,
                 clock: Callable[[], float] = time.monotonic) -> None
    def resolve(self, video_id: str, /) -> Episode
    def download(self, video_id: str, destination: Callable[[str], Path], /) -> Path
    def close(self) -> None

def build_ytdlp_source(settings: Settings) -> YtDlpYoutubeSource
```

**`load_runtime()`**:

1. `importlib.import_module` `yt_dlp`, `yt_dlp.utils`, `yt_dlp.networking.exceptions`,
   `yt_dlp.globals`, `yt_dlp.version`, and `yt_dlp_ejs`. A `ModuleNotFoundError` or
   `ImportError` is `MissingYoutubeExtraError`:
   `EARMARK_YOUTUBE_BACKEND=ytdlp needs the 'youtube' extra, which is not installed;
   install it with \`uv sync --extra youtube\`` (or `… which failed to import (<TypeName>);
   reinstall it with …`), the same shape as faster-whisper's message.
2. `yt_dlp.globals.plugin_dirs.value = []` (2.3).
3. `deno_path` as above. When it is not a file with `os.access(…, os.X_OK)`:
   `MissingYoutubeExtraError` "EARMARK_YOUTUBE_BACKEND=ytdlp needs the Deno runtime that the
   'youtube' extra installs into the environment's bin directory; reinstall it with
   \`uv sync --extra youtube\`". The message names no full path.
4. Versions from `yt_dlp.version.__version__` and package metadata (`None` when unreadable).

**`ytdlp_params(...)`** returns exactly these keys (a test compares the key set):

| Key | Value | Why |
|---|---|---|
| `quiet` | `True` | nothing on stdout |
| `noprogress` | `True` | |
| `color` | `"no_color"` | |
| `logger` | the `YtDlpLog` | 5.7 |
| `allowed_extractors` | `["youtube"]` | YouTube only; no generic |
| `noplaylist` | `True` | no playlists |
| `ignore_no_formats_error` | `True` | keep the metadata of an unplayable video (2.3) |
| `format` | `AUDIO_FORMAT` | audio only, https, stereo |
| `postprocessors` | `[]` | no post-processing |
| `fixup` | `"never"` | no `FixupM4a` |
| `cachedir` | `False` | nothing under `~/.cache/yt-dlp` |
| `proxy` | `""` | no environment proxies |
| `socket_timeout` | `SOCKET_TIMEOUT_S` | |
| `retries` | `RETRIES` | |
| `js_runtimes` | `{"deno": {"path": str(deno_path)}}` | only this Deno |
| `remote_components` | `[]` | no EJS from npm or GitHub |
| `overwrites` | `True` | |
| `continuedl` | `False` | no resume of a stale `.part` |
| `updatetime` | `False` | |
| `progress_hooks` | `[progress_hook]` when given, else `[]` | 5.4 |
| `outtmpl` | `{"default": outtmpl}` when given; absent otherwise | |

Never set: `cookiefile`, `cookiesfrombrowser`, `usenetrc`, `netrc_location`, `username`,
`password`, `max_filesize`, `paths`, `external_downloader`, `ffmpeg_location`,
`match_filter`, `extract_flat`, `playlist_items`, `writeinfojson`, `extractor_args`. The
`outtmpl` value is the destination path with every `%` doubled.

**`resolve(video_id)`**:

1. `is_video_id(video_id)`, else `PermanentError("not a YouTube video id")`.
2. A new `YtDlpLog`; a session from `ytdlp_params(deno_path=…, logger=log,
   progress_hook=None, outtmpl=None)`.
3. Inside `deno_environment(deno_dir)`:
   `info = session.extract_info(canonical_youtube_url(video_id), download=False,
   process=True, ie_key="Youtube")`. `session.close()` in a `finally`.
4. An `Exception` from step 3: `mapped = classify_error(exc, errors, log.warnings)`;
   `raise mapped from exc` when not `None`, else re-raise. A `BaseException` that is not an
   `Exception` (the worker's `Shutdown`) is never caught.
5. `info` not a `Mapping`: `PermanentError(REFUSALS["not_video"])`.
6. `check_playable(video_id, info, log.warnings)`.
7. `episode = episode_from_info(video_id, info)`; keep
   `self._pending = (video_id, info, clock())`; return `episode`.

**`download(video_id, destination)`**:

1. Take `self._pending` when its id matches and it is younger than `PENDING_MAX_AGE_S`;
   otherwise call `self.resolve(video_id)` and take the new one. Set `self._pending = None`.
2. The selected format, from the info's top level: `format_id` and `url` must be
   non-empty strings, else `PermanentError(REFUSALS["no_audio"])`. `vcodec` must be `"none"`
   and `protocol` `"https"`, else `PermanentError(REFUSALS["no_audio"])`. The URL's scheme
   must be `https` and its host end with `MEDIA_HOST_SUFFIX`, else
   `PermanentError(REFUSALS["off_host"])`. `ext` must match `[a-z0-9]{1,5}`, else
   `PermanentError(REFUSALS["no_audio"])`.
3. Declared size: the first of `filesize`, `filesize_approx` that is an `int` (not `bool`)
   or finite `float`. Above `max_bytes`: `PermanentError` (too large, 5.4), before any
   download request.
4. `path = destination(ext)`. A `_Progress` state with `started = clock()` and
   `reason: str | None`. Its hook reads `int(d.get("downloaded_bytes") or 0)`: above
   `max_bytes` it sets `reason = "too_large"`, after `DOWNLOAD_DEADLINE_S` it sets
   `reason = "deadline"`, and either way raises `errors.download_cancelled("earmark stopped
   the download")`.
5. A new `YtDlpLog` and a session from `ytdlp_params(…, progress_hook=hook,
   outtmpl=<path with % doubled>)`. Inside `deno_environment`:
   `session.process_ie_result(info, download=True)`; `close()` in a `finally`.
6. An `Exception` from step 5: `reason == "too_large"` is the too-large `PermanentError`;
   `"deadline"` is `TransientError("the YouTube download took longer than 1 hour")`;
   otherwise `classify_error` as in resolve.
7. After: `path.is_file()`, else `PermanentError(REFUSALS["nothing"])`. Size 0:
   `TransientError("the YouTube download was empty")`. Size above `max_bytes`: unlink it,
   too-large `PermanentError`.
8. Log `downloaded YouTube audio for <episode key>: <n> bytes, format <format_id>`
   (`earmark.youtube.ytdlp`, INFO) and return `path`.

`close()` drops `_pending`. The backend is not thread-safe; each worker builds its own.

**`build_ytdlp_source(settings)`**: `load_runtime()`, then
`YtDlpYoutubeSource(runtime, max_bytes=settings.max_audio_mb * MIB,
deno_dir=settings.data_dir.resolve() / "cache" / "deno")`. It makes no network request and
starts no process.

### 4.7 Refusals and error mapping

`REFUSALS: Final[Mapping[str, str]]`, fixed ASCII sentences that never quote yt-dlp or
YouTube text:

| Key | Class | Message |
|---|---|---|
| `private` | permanent | `This YouTube video is private.` |
| `members` | permanent | `This YouTube video is for channel members only.` |
| `premium` | permanent | `This YouTube video is for YouTube Premium members only.` |
| `age` | permanent | `This YouTube video is age-restricted, and earmark does not sign in to YouTube.` |
| `sign_in` | permanent | `This YouTube video needs a signed-in account, and earmark does not sign in to YouTube.` |
| `removed` | permanent | `This YouTube video is unavailable: it was removed, or never existed.` |
| `geo` | permanent | `This YouTube video is not available in this server's country.` |
| `live` | permanent | `This is a live stream. Share it again once it has ended and YouTube has processed the recording.` |
| `upcoming` | permanent | `This premiere or live stream has not started yet. Share it again once it has aired.` |
| `post_live` | permanent | `This live stream has just ended and YouTube is still processing it. Share it again later.` |
| `bot` | permanent | `YouTube asked this server to sign in to prove it is not a bot, and earmark does not sign in. Share it again later.` |
| `rate_limited` | transient, `retry_after_s=3600` | `YouTube is limiting requests from this server; earmark tries again in an hour.` |
| `not_video` | permanent | `This YouTube link did not lead to a single video.` |
| `refused` | permanent | `YouTube would not play this video.` |
| `no_audio` | permanent | `YouTube offered no audio-only stream for this video. yt-dlp may be out of date, or its JavaScript runtime missing: run earmark doctor (docs/deploy.md section 18).` |
| `out_of_date` | permanent | `yt-dlp could not read this video, which usually means it is out of date: upgrade it and rebuild the images (docs/deploy.md section 18).` |
| `http_403` | permanent | `YouTube refused the download (HTTP 403), which usually means yt-dlp is out of date: upgrade it and rebuild the images (docs/deploy.md section 18).` |
| `off_host` | permanent | `yt-dlp chose a stream outside YouTube's media hosts; nothing was downloaded.` |
| `nothing` | permanent | `yt-dlp finished without writing the audio file; it may be out of date (docs/deploy.md section 18).` |

Built per call: too large, `PermanentError(f"the YouTube audio is larger than the
EARMARK_MAX_AUDIO_MB limit ({mib} MiB)")`; HTTP 408, 429 and 5xx,
`TransientError(f"YouTube answered HTTP {status}; earmark tries again later")`; any other
HTTP status, `PermanentError(f"YouTube answered HTTP {status}")`; a transport error,
`TransientError(f"could not reach YouTube ({TypeName})")`; `ContentTooShortError`,
`TransientError("the YouTube download was cut short")`.

**`check_playable(video_id, info, warnings)`**, first match wins:

1. `info.get("_type")` not in `(None, "video")`, or `info.get("id") != video_id`:
   `not_video`.
2. `live_status`: `is_live` → `live`; `is_upcoming` → `upcoming`; `post_live` →
   `post_live`. (`was_live`, a past stream's recording, is fine.)
3. `availability`: `private` → `private`; `subscriber_only` → `members`; `premium_only` →
   `premium`; `needs_auth` → `age` when `age_limit` is an int of 18 or more, else `sign_in`.
4. No formats (`formats` missing, not a list, or empty): `reason_from_text(" ".join(warnings))`.
5. Otherwise it passes. (Formats but no audio-only https stream is `download`'s `no_audio`,
   so a video with a cached transcript is not refused for it.)

**`reason_from_text(text)`**: lower-case it and turn `’` into `'`, then the first match of:

| Contains | Key |
|---|---|
| `private` | `private` |
| `members-only`, `members only`, `join this channel` | `members` |
| `premium` | `premium` |
| `confirm your age`, `age-restricted`, `inappropriate for some users` | `age` |
| `not a bot`, `captcha` | `bot` |
| `try again later`, `rate-limited`, `rate limited` | `rate_limited` |
| `premieres in`, `will begin in`, `scheduled` | `upcoming` |
| `not available in your country`, `not made this video available in your country` | `geo` |
| `recording is not available`, `removed`, `unavailable`, `no longer available`, `terminated`, `deleted`, `does not exist` | `removed` |
| `no suitable extractor` | `not_video` |
| `sign in` | `sign_in` |
| none | `refused` |

Then the class from the table (`rate_limited` transient, the rest permanent).

**`classify_error(exc, errors, warnings)`**:

1. `inner = exc.exc_info[1]` when `exc` is an `errors.download_error` whose `exc_info` has
   an exception, else `exc`.
2. Walk `inner` and its `cause`, `__cause__` and `__context__` (breadth first, at most 8
   objects, no repeats). The first `errors.http_error` decides by `.status` (403 →
   `http_403`; 408, 429, 500–599 → transient; other → permanent). Else the first
   `errors.transport_error` → transient. Else the first `errors.content_too_short` → cut
   short.
3. `inner` is an `errors.geo_restricted` → `geo`.
4. `inner` is an `errors.extractor_error`: `expected` true →
   `reason_from_text(orig_msg + " " + " ".join(warnings))`; `expected` false →
   `out_of_date`.
5. `inner` is the `errors.download_error` itself (no inner exception):
   `reason_from_text(str(exc) + " " + " ".join(warnings))`.
6. `inner` is an `OSError`: return `None` (re-raised unchanged; the runner treats it as
   transient and logs the traceback: a full disk, a permission problem).
7. Anything else raised inside a yt-dlp call (`KeyError`, `TypeError`, …): log
   `yt-dlp raised <TypeName>` at WARNING with `exc_info` on `earmark.youtube.ytdlp`, and
   return `out_of_date`.

### 4.8 The fake (`fakes.py`)

```text
@dataclass(frozen=True)
class YoutubeResolveCall:
    video_id: str

@dataclass(frozen=True)
class YoutubeDownloadCall:
    video_id: str

class FakeYoutubeSource(FakeStage[YoutubeResolveCall | YoutubeDownloadCall]):
    def __init__(self, payload: bytes = b"fake youtube audio\n",
                 payloads: Mapping[str, bytes] | None = None,
                 episodes: Mapping[str, Episode] | None = None) -> None
    downloads: int
    def resolve(self, video_id: str) -> Episode    # episodes.get(id) or youtube_episode(id,
        # title=f"Fake video {id}", channel="Fake Channel", published_at=FAKE_NOW,
        # published_date_only=False, duration_s=600.0)
    def download(self, video_id: str, destination: Callable[[str], Path]) -> Path
        # writes payloads.get(id, payload) to destination("webm"), downloads += 1
```

`make_fake_backends` and `FakeBackends` gain `youtube: FakeYoutubeSource`.

### 4.9 Settings, factory, doctor, notify, log

- `config.py`: `YoutubeBackend = Literal["ytdlp", "fake"]`;
  `youtube_backend: YoutubeBackend = Field("ytdlp", description="YouTube source.")`.
  README row: `EARMARK_YOUTUBE_BACKEND` | `ytdlp` | `ytdlp` (needs the `youtube` extra) or
  `fake`. `.env.example`: `EARMARK_YOUTUBE_BACKEND=ytdlp` after `EARMARK_FETCH_BACKEND`.
- `factory.py`: `YOUTUBE_SOURCES = {"fake": lambda ctx: FakeYoutubeSource(), "ytdlp":
  _ytdlp_source}`; `_ytdlp_source` imports `earmark.youtube.ytdlp` inside the builder.
  `build_backends` looks it up with the others (a missing name is the usual `ConfigError`),
  and the worker's log line becomes `backends: resolver=… fetch=… youtube=… asr=… mail=…
  notify=…`.
- `doctor/checks.py`: `guarded("youtube", self.youtube)` after `fetch`:
  - `fake`: `warn` "fake YouTube source (for tests)";
  - `ytdlp`: build through `YOUTUBE_SOURCES`; a `ConfigError` is `fail` with its text;
    otherwise `ok` "yt-dlp <v>, yt-dlp-ejs <v>, deno <v>" from the runtime (`unknown` for a
    version that cannot be read). It makes no network call and starts no process.
- `notify/ntfy.py` `_label`, for `job.source == "youtube"`: the first line is
  `YouTube: <channel>` when the episode has a channel, else `YouTube video`; the name is
  the episode's title, else `YouTube video`. No video id, no URL.
- `log.py`: `QUIET_LOGGERS` gains `"yt_dlp"`.

### 4.10 Contract 2 (`contract/mail-v2.md`)

The file's header, then the block between `<!-- contract-v2:begin -->` and
`<!-- contract-v2:end -->`, which a test pins by hash like v1's. Write it as below.

```markdown
# The mail contract, version 2

`contract: 2`. Status: **DRAFT**, like version 1, until the operator approves the receiving
skill after the one-episode check. Written in step 32 (2026-11-25) for YouTube as a source.
Version 1 (`mail-v1.md`) stays, because mail sent under it can still be waiting in an
inbox; the receiving skill accepts both.

<!-- contract-v2:begin -->

Contract 2 is contract 1 (`contract/mail-v1.md`) with the changes below. Every rule of
contract 1 that this list does not change holds unchanged.

- **The version.** Both attachments carry `contract: 2`. A message whose two files carry
  different versions breaks the contract.
- **A third note type, `video`, for a YouTube video.** `type: video` goes with
  `origin: youtube`, and `origin: youtube` with `type: video` only. Its `tags` are
  `["video"]`.
- **The keys of a video note.**

  | key | type | video | notes |
  |---|---|---|---|
  | `type`, `contract`, `source`, `date`, `duration`, `shared_at`, `transcript_sha256`, `tags` | as in contract 1 | required | |
  | `origin` | string | required | `youtube` |
  | `title` | string | required | the video's title, sanitised |
  | `channel` | string | optional | the YouTube channel's name, sanitised, only when known |
  | `published` | date | optional | the video's upload date, only when known |
  | `language` | string | optional | BCP 47 |
  | `source_url` | string | required | `https://youtu.be/<video id>` |
  | `guid` | string | required | `youtube:<video id>`: the 11-character id YouTube gives the video, from `A-Z`, `a-z`, `0-9`, `-` and `_`, case kept |
  | `show`, `apple_url` | | omit | |

- **`channel` is a new key.** Only a video note has it; a podcast or audio note never does.
- **`source_url`** is allowed for `origin: url`, as before, and for `origin: youtube`,
  where it is always the short link built from the guid's video id.
- **The file name and heading of a video note.** With a channel, the slug joins the channel
  and title slugs, truncated as a whole, and the body opens with `# <channel> — <title>`.
  Without one they follow the audio rules. An empty slug becomes `youtube-<video id slug>`.
- **De-duplication is unchanged.** Two links to one video carry one guid.

<!-- contract-v2:end -->
```

The inner fence above is for this plan only: in `mail-v2.md` the text is plain Markdown
(the header, then the marked block).

### 4.11 The checker (`earmark_mail_contract.py`)

Standard library, Python 3.9, as now. Changes:

- `CONTRACTS: Final = (1, 2)`; `CONTRACT: Final = 2` (the newest, used in the parser's
  description "Check an earmark mail, contract 1 or 2.").
- The note's version: `note.scalar("contract", "int")` as an int when in `CONTRACTS`, else
  `None`. With `None` the v2 tables apply (so only `note.contract` reports the problem).
- Per version:

  | | contract 1 | contract 2 |
  |---|---|---|
  | key table | today's `_NOTE_TYPES` | plus `channel: string` |
  | types | `podcast`, `audio` | plus `video` |
  | origins | `apple_podcasts`, `upload`, `url` | plus `youtube` |
  | type ↔ origin | podcast ↔ apple_podcasts; audio ↔ upload or url | plus video ↔ youtube |

- `note.required`: the base list, plus `show` for a podcast, plus `source_url` for a video.
- `note.omitted`: audio omits `show`, `apple_url` and `channel`; a podcast omits
  `channel` and `source_url`; a video omits `show` and `apple_url`; `source_url` belongs
  to origin `url` or `youtube` only.
- `note.guid`: a video's guid matches `youtube:[A-Za-z0-9_-]{11}`.
- **New rule `note.source-url-youtube`** ("a video's source_url is https://youtu.be/ and
  its guid's video id"), placed after `note.source-url`, needs `attachments`. It runs when
  `origin` is `youtube`, `source_url` is present and the guid has the video form.
- `note.heading`: a video with `channel` must open `# <channel> — <title>\n\n## Transcript\n\n`;
  without it, `# <title>\n\n## Transcript\n\n`.
- `note.contract`: "contract is 1 or 2". `instructions.contract`: "contract is the note's
  (1 or 2)": the instructions' int must be in `CONTRACTS` and, when the note's version is
  known, equal to it.
- The descriptions of `note.type` and `note.origin` name `video` and `youtube` "from
  contract 2".
- Output: the JSON's `contract` is the note's version (or `null`), and the text line is
  `OK: earmark mail, contract <n>` (`?` when unknown). Nothing else in the output changes,
  and it still never quotes mail text.

### 4.12 The receiver and `SKILL.md`

- `earmark_receive.py` `_classify`: `if found.contract not in contract.CONTRACTS` is
  `unknown-contract`; `base["contract"] = found.contract`. Nothing else changes: recognition
  already requires the same `contract: N` line in both files, de-duplication works on the
  guid and `transcript_sha256` lines, which are the same across versions, and the scan
  finds `guid: "youtube:…"` like any other guid line.
- `SKILL.md`, section 5, after the Repo Map sentence, one paragraph: the note's `type` is
  `podcast`, `audio`, or `video` (a YouTube video, with its `channel`), and a video is filed
  like the others, by the Repo Map. No other section changes; the outcome table already
  covers a contract the plugin does not know.
- `plugin.json` version `1.1.0`; `PLUGIN_RELEASES` gains `"1.1.0": "<digest>"` (the test's
  failure message prints it).

### 4.13 Samples (`tests/contract_samples.py`)

- `SAMPLES_DIR` becomes `contract/samples/v2`; `SAMPLES` holds the v2 inputs; `build`,
  `drifted`, `write` and `main` work on v2. `make samples` writes v2 only.
- `V1_DIR = contract/samples/v1`, `V1_NAMES` (today's ten names) and
  `V1_SHA256: Final[dict[str, str]]`, each file's sha256 as committed (compute it with
  `sha256sum` before touching anything). `main(["--check"])` also fails when a v1 file is
  missing or its hash differs.
- `ALL_SAMPLES: Final[list[tuple[str, str]]]`: `("v1", name)` for each v1 name, then
  `("v2", name)` for each v2 name; `sample_path(version, name) -> Path`.
- The v2 set is today's ten inputs, unchanged (now rendered as contract 2), plus three:

  | Name | Job | Inputs |
  |---|---|---|
  | `youtube` | 11, `source="youtube"`, `url="https://www.youtube.com/watch?v=exampleVid1"`, instructions `"Summarise the talk in three bullets.\nFile it under learning."`, `utc_offset_min=120` | `youtube_episode("exampleVid1", title="An Example Talk", channel="The Example Channel", published_at=datetime(2026, 11, 21, 15, 0, tzinfo=UTC), published_date_only=False, duration_s=125.0)`, `_FEED_SEGMENTS`, language `en` |
  | `youtube.reshare` | 12, the same URL, instructions `"Compare it with the earlier talk."`, `shared_at=_LATER_SHARED`, `utc_offset_min=120` | the same episode and transcript, `first_shared_at=_FIRST_SHARED` |
  | `youtube-no-channel` | 13, `url="https://www.youtube.com/watch?v=ex_ample-02"`, instructions `""` | `youtube_episode("ex_ample-02", title="Untitled Upload Example", channel="", published_at=datetime(2026, 11, 19, tzinfo=UTC), published_date_only=True, duration_s=95.0)`, `_AUDIO_SEGMENTS`, language `None` |

  As `SampleInputs`: `youtube` and `youtube-no-channel` use `first_shared_at=_FIRST_SHARED`
  and `date=_FIRST_DATE`; `youtube.reshare` uses `first_shared_at=_FIRST_SHARED` and
  `date=_LATER_SHARED + timedelta(minutes=5)`. The ids are placeholders, not real videos.
  `contract/samples/v2/README.md` describes all thirteen, as v1's README does.

### 4.14 Runner (`pipeline/runner.py`)

In `_stage_work`, after `set_stage("resolve")`:

```text
elif job.source == "youtube":
    if job.url is None:
        raise PermanentError("ingest job without a url")
    video_id = video_id_from_canonical(job.url)
    episode = b.youtube.resolve(video_id)
    if episode.source != "youtube" or episode.key != youtube_key(video_id):
        raise PermanentError("the YouTube source returned another video")
elif job.source == "url":
    ...  # today's else branch, unchanged
else:
    raise PermanentError("unknown source")
```

Recording, the cache check and the transcript check are unchanged. On a cache miss,
`audio is None` for both podcasts and YouTube; YouTube then does `set_stage("fetch")`,
`staging.clear_download(job.id)`,
`audio = b.youtube.download(video_id, staging.download_destination(job.id))`, and
podcasts keep `fetcher.fetch`. The staged file is transcribed in place, like a direct URL's,
and `staging.release` deletes it when the job ends. `load_episode` needs no change (it
reads `episode.json` by key).

### 4.15 ARCHITECTURE and CLAUDE.md (written by the implementer)

`docs/ARCHITECTURE.md`:

- **System**: the parts table says the service takes YouTube links (step 32); the mail row
  of the seams table names `contract: 2` (from step 32) and `contract: 1` (still accepted),
  `contract/samples/v1/` (frozen) and `contract/samples/v2/`; "The mail seam" describes
  both versions, the frozen v1 samples and `make samples`; "The receiving skill" says
  version 1.1.0 and both contracts; the repo layout adds `mail-v2.md` and `samples/v2/`.
- **Decisions**: the Scope and State rows (staged YouTube audio under `downloads/`, the Deno
  cache under `cache/deno/`), the Delivery row (`contract: 2`).
- **Flow**: a `youtube` line under resolve and fetch.
- **Module layout**: `youtube/` (`links.py`, `ytdlp.py`).
- **Domain models**: `Source`, the YouTube `Episode`, the key.
- **Stage contracts**: `YoutubeSource`, `Backends.youtube`, the fake.
- **Runner and worker**: the `youtube` branch of "Order"; retention.
- **A new subsection "### YouTube"** after "Resolving": sections 2.2, 2.3, 4.6, 4.7 and 5
  of this plan in prose: the design, the options, the refusals, the size cap, the
  exception.
- **Delivering**: the video note; the heading becomes
  `#### The mail contract (\`contract: 2\`)`, pointing to `contract/mail-v2.md` and still
  to `contract/mail-v1.md`.
- **Notifying**, **Doctor**, **Jobs** (the dedupe subject), **Database identity**
  (`0004_youtube`, schema 4, the table rebuild), **Data layout** (`cache/deno/`, YouTube
  downloads), **HTTP API** (YouTube URLs, the two new 422 messages), **Configuration**
  (`YOUTUBE_BACKEND`), **Deployment** (the extra in both images, the GPU build-time
  check), **Testing** (the new files, the `slow` job's extras, the opt-in live test).
- **Security**: a new bullet "Requests: YouTube links" (only an id reaches yt-dlp), and in
  "Outbound fetches" the exception: yt-dlp makes its own requests, for YouTube only, under
  the rules of 5.6. Leak rules: a YouTube job is logged by its video id, and pushes carry the
  title and channel only.
- **Known risks**: a new item 11 (5.10).
- **Settled questions**: "Settled by step 32", the decisions of section 8 in one line each.
- **Not planned**: "other sources" no longer includes YouTube.

`CLAUDE.md`: "The service today" names YouTube links; the fetch rule gains the YouTube
exception; the mail rule names `contract: 2` (with 1 still accepted) and both sample
folders; the heavy-dependency rule names the `youtube` extra; the Commands block's
`uv sync` comment adds `--extra youtube`.

### 4.16 The other docs

- **`docs/deploy.md`**
  - Section 1 ("What is tested"): YouTube downloads are checked only by runbook phase 4.
  - Sections 4 and 5: both images and the native `uv sync` install the `youtube` extra
    (`--extra transcribe --extra youtube`).
  - Section 15 ("Backups and upgrades"): this version migrates the database to schema 4 on
    first start; an older image then refuses it, so back up `earmark.db` first.
  - **New section 18, "YouTube"**:
    - 18.1 what it needs: the extra (yt-dlp, yt-dlp-ejs, Deno, all from `uv.lock`), no
      ffmpeg, no cookies; what is refused;
    - 18.2 updating yt-dlp: when (a job fails with "out of date", "no audio-only stream" or
      HTTP 403), how (`uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
      --upgrade-package deno`, `make check`, commit, on the Spark `docker compose build` and
      `docker compose up -d`, then `docker compose run --rm worker doctor`), and that
      Dependabot proposes the update weekly;
    - 18.3 what earmark fetches from YouTube, and the exception to the address guard.
- **`docs/runbook-spark.md`**
  - Phase 3's check: doctor shows `youtube ok` with the three versions.
  - Phase 4: a third request, `https://youtu.be/jNQXAC9IVRw?si=example` ("Me at the zoo",
    19 s of speech), then a fourth with `https://www.youtube.com/watch?v=jNQXAC9IVRw&t=5`
    and other instructions. Check: both `done`; the worker log shows one transcription and
    then `transcript for <key> is cached`; the notes have `origin: "youtube"`,
    `guid: "youtube:jNQXAC9IVRw"`, `channel: "jawed"`, `published: 2005-04-24` and the zoo
    text; `data/downloads/` is empty.
- **`docs/shortcut.md`**: a YouTube share (from the YouTube app or Safari) goes through the
  URL branch with no change to the Shortcut; section 4 gains the two 422 rows after
  `This Apple link is not a podcast episode.`; section 5's "new question" paragraph says a
  YouTube video is downloaded again only when it must be transcribed again.
- **`README.md`**: what a request can carry; the settings row; the quickstart's
  `EARMARK_YOUTUBE_BACKEND=fake`; `--extra youtube` where the transcribe extra is named.
- **`docs/PLAN.md`**: the seam row; the backlog line becomes "**Other sources:** other
  podcast platforms, and video files."

## 5. Behaviour and edge cases

### 5.1 One video, one identity

The key, the guid and the dedupe subject come from the video id alone, never from
metadata, so they do not move when a title or channel changes. Every link form maps to the
canonical URL at the API, so the job, the episode key, `shared.json`, the transcript and
the note's guid are the same for all of them. A re-share with new instructions is a new job
that reuses the transcript and skips the download (the runner checks the cache before
`download`). The note's `shared_at` is the earliest share of the video, whichever form.

### 5.2 Every job resolves, even with a cached transcript

`resolve` runs on every job (it is the metadata, like Apple's resolver), so a video that
has become private since its first share fails the new job even though a transcript is
cached. That matches podcasts and keeps the note's metadata current. The download is the
expensive part, and it is skipped.

### 5.3 Metadata is untrusted

- `title`: a `str`, through `resolve.text.clean_text(limit=500)`, else `""`. The note
  falls back to `YouTube video <id>`.
- channel: the first `str` of `channel`, `uploader`, cleaned the same way, else `""`.
- `published_at`: `timestamp` when it is an `int` or finite `float` (not `bool`) between 0
  and 4102444800, as `datetime.fromtimestamp(ts, UTC)`; otherwise `upload_date` when it is
  a `str` of eight digits forming a real date, as midnight UTC with
  `published_date_only=True`; otherwise `None`. So the note's `published` is the upload day
  in the requester's offset, or YouTube's calendar date.
- `duration_s`: `duration` as a finite, non-negative `int`/`float` (not `bool`), else
  `None`. The note's `duration` comes from the transcript, as for every note.
- Every string reaching the note goes through `contract_text` as today; the subject is the
  cleaned title. Nothing else from the info dict is kept: no description, tags, chapters,
  thumbnails or comments.

### 5.4 The size cap

`EARMARK_MAX_AUDIO_MB` applies, three ways: the declared size before any download request
(YouTube's https formats state `filesize`), the bytes counted by the progress hook while
downloading, and the file on disk afterwards. yt-dlp's own `max_filesize` is not used: it
skips silently (2.3). A partial `.part` file is left to staging: `clear_download` at the
next attempt and `release` at the end both match `<job_id>.*`.

**No duration cap.** A podcast has none either, and the size cap bounds a YouTube download
(1024 MiB is about 17 hours at 128 kbps). Live streams, whose length is unbounded, are
refused.

### 5.5 Deno's cache

`deno_environment(deno_dir)` sets `DENO_DIR=<data dir>/cache/deno` unless the operator has
set `DENO_DIR`, and `DENO_NO_UPDATE_CHECK=1`, for the duration of each yt-dlp call, and
restores both afterwards. The worker runs one job at a time and the heartbeat thread starts
no process, so the scoped change is safe. Without it Deno would write `~/.cache/deno`, which
is read-only under the systemd unit (`ProtectSystem=strict`) and outside the data
directory. Deno creates the folder itself.

### 5.6 The exception to "all third-party fetches go through `fetch.py`"

State it in ARCHITECTURE (Security, "Outbound fetches") and CLAUDE.md:

- yt-dlp is given one URL, `https://www.youtube.com/watch?v=<id>`, built by earmark from an
  id it validated; never the client's string.
- Only the `youtube` extractor is loaded (`allowed_extractors`, `ie_key`); no generic
  extractor, no playlists, no plugins, no config files, no cookies kept, no environment
  proxies, no remote components.
- Before downloading, the chosen stream must be `https` on a `*.googlevideo.com` host.
  yt-dlp follows that host's redirects itself (observed: `302` to another googlevideo
  host), and its extraction requests (the watch page, the player API, the player script)
  go to YouTube's hosts without earmark's address guard, `EARMARK_ALLOW_PRIVATE_FETCH`
  or `MAX_FEED_MB`. A resolver that points those names at a private address is not caught;
  the guard's other rules (size cap, no proxies) are kept as above.
- YouTube's player JavaScript runs in Deno with no permissions (2.1).

### 5.7 Logging and leaks

- `YtDlpLog` forwards `debug` and `info` to `logging.getLogger("yt_dlp")` at DEBUG, and
  `warning` and `error` at WARNING, and keeps the warning texts for classification.
  `"yt_dlp"` is in `QUIET_LOGGERS`, so the DEBUG records (googlevideo URLs with the
  server's IP) never print, whatever `EARMARK_LOG_LEVEL`. Warnings reach the log through
  earmark's handler, which scrubs URLs from third-party records.
- Job errors and pushes carry only the fixed messages of 4.7: no URL, no yt-dlp text, no
  YouTube reason string.
- The enqueue line names `YouTube video <id>`, and the download line names the episode key
  and the format id. Instructions and the recipient follow step 24's rules unchanged.
- yt-dlp writes `WARNING: Unknown codec …` straight to stderr for some formats (2.3); it
  carries a codec name only. Say so in ARCHITECTURE's "Third-party logging".

### 5.8 Errors and retries

- Live, not yet aired, just-ended, private, members-only, Premium, age-gated, needs
  sign-in, removed, geo-blocked, not a video: permanent, with the message saying which.
- A bot check is permanent too: retrying within the job's four attempts (about 21 minutes)
  rarely clears it and adds requests from the same address. YouTube's "try again later"
  rate limit is transient with an hour's delay.
- Network failures, HTTP 408, 429 and 5xx, a cut-short download, an empty file, the
  one-hour deadline: transient.
- An extractor failure that means yt-dlp is out of date (an unexpected `ExtractorError`,
  an exception from inside yt-dlp, HTTP 403, no audio-only stream, no file written):
  permanent, naming the remedy (deploy section 18).
- An `OSError` (disk full) is not yt-dlp's: it reaches the runner unchanged (transient,
  traceback logged).
- `Shutdown` (a `BaseException`) passes through every `except Exception`, so SIGTERM
  releases a YouTube job mid-download like any other.

### 5.9 Contract 2 for every message (option A)

- `mail/note.py`: `CONTRACT = 2` in both files of every message. The bytes of a podcast or
  audio note change only in that line, so its `transcript_sha256` (over the body) is
  unchanged. A note filed from a v1 message and a re-share that arrives as v2 therefore
  classify as `reshare`, and a different transcript as `retranscription`.
- The video note (`episode.source == "youtube"`): `type: "video"`, `origin: "youtube"`,
  `title` (fallback `YouTube video <id>`), `channel` when known, `date`, `published`,
  `duration`, `language`, `source_url: "https://youtu.be/<id>"`, `guid: "youtube:<id>"`,
  `shared_at`, `transcript_sha256`, `tags: ["video"]`. Key order: the existing list with
  `channel` after `show`. Slug: channel and title slugs joined when there is a channel,
  else the title slug; empty becomes `youtube-<slugify(id)>`. Heading `# <channel> — <title>`
  or `# <title>`.
- `podcast` becomes `episode.source == "apple_podcasts"` (not "not audio"), and
  `note_guid` returns `episode.guid` for youtube.
- The message body, subject and attachment rules are unchanged.

### 5.10 Known risk 11 (for ARCHITECTURE)

YouTube changes break yt-dlp every few weeks; the remedy is an upgrade (deploy section 18).
Dependabot proposes yt-dlp updates weekly, and the weekly `slow` job runs the real library
offline. YouTube is moving formats to SABR-only streaming for some clients (yt-dlp warned
of skipped `web_embedded` https formats on an age-gated video), which may one day leave no
https audio stream: `no_audio`. Datacenter addresses get bot checks, so CI never downloads
from YouTube; the Spark's own line is what matters. The address guard does not cover
yt-dlp (5.6).

### 5.11 What the API refuses, and what it does not

The API makes no request. A well-formed link to a private or removed video is accepted
with 202 and fails in the worker with a push that says why. Only link shapes are refused
at the API.

## 6. Tests

### 6.1 Brief: "Tests cover the link forms, the refusals and canonicalisation. Two forms of one video give one episode key."

- `tests/test_youtube_links.py`:
  - `test_video_link_forms` (parametrised; each to `jNQXAC9IVRw` or a placeholder id):
    `https://youtu.be/<id>`, `…?si=abc&t=30`, `youtu.be/<id>` without a scheme,
    `http://youtu.be/<id>/`, `https://www.youtube.com/watch?v=<id>`,
    `https://youtube.com/watch?v=<id>&list=PLx&index=2`, `https://m.youtube.com/watch?v=<id>&feature=share`,
    `https://music.youtube.com/watch?v=<id>`, `https://www.youtube.com/shorts/<id>?feature=share`,
    `https://www.youtube.com/live/<id>?si=x`, `https://www.youtube.com/embed/<id>`,
    `https://www.youtube.com/v/<id>`, `https://www.youtube-nocookie.com/embed/<id>`,
    `https://WWW.YouTube.com/Watch?v=<id>#t=1`, an id with `-` and `_`;
  - `test_canonical_drops_parameters`: every form gives exactly
    `https://www.youtube.com/watch?v=<id>`;
  - `test_refused_not_video` (parametrised): `/@name`, `/channel/UC…`, `/c/name`,
    `/user/name`, `/playlist?list=PL…`, `/results?search_query=x`, `/`, `/feed/subscriptions`,
    `https://studio.youtube.com/video/<id>`, `/embed/videoseries?list=PL…`,
    `/embed/live_stream?channel=UC…`, `/shorts/<id>/extra` → `youtube_not_video`;
  - `test_refused_bad_id`: `https://youtu.be/`, `youtu.be/short`, `watch`, `watch?v=`,
    `watch?v=<id>&v=<other id>`, a 12-character id, `/shorts/` with no id, an id with `.` →
    `youtube_bad_id`;
  - `test_credentials_and_port_refused`: a youtu.be link with a user name and password
    before the host → `credentials`; `https://youtu.be:8443/<id>` → `malformed`;

  - `test_two_forms_one_key`: `youtu.be/<id>` and `/shorts/<id>` give the same id and
    `youtube_key`; a different id gives a different key;
  - `test_video_id_from_canonical`: round trip; any other string is a `PermanentError`.
- `tests/test_models.py`: `test_youtube_key_and_source_key` (valid; wrong show id, bad guid
  and short id are `ValueError`); `test_youtube_episode_validates`.
- `tests/test_sources.py`: `test_parse_source_url_youtube` (source and canonical URL);
  `test_non_youtube_urls_unchanged` (an Apple link, a direct `.mp3`, `https://vimeo.com/1`,
  `https://notyoutube.com/watch?v=<id>` keep step 24's results).
- `tests/test_api.py`: `test_youtube_link_enqueues_canonical` (202, the job's `source` and
  `url`); `test_youtube_refusals_are_422` (both messages, exactly); `test_youtube_forms_dedupe`
  (two forms, same `to` and instructions: the second answer is `created: false`).
- `tests/test_submission.py`: `test_youtube_enqueue_log_names_the_id` (no URL in the line).
- `tests/test_docs.py`: the error table gains the two rows, and `_error_triggers` two
  triggers (`https://www.youtube.com/@example` and `https://youtu.be/short`) at the same
  place.

### 6.2 Brief: "The YouTube fetcher is tested with yt-dlp stubbed (recorded metadata, no network)"

All in `tests/test_ytdlp.py` on `tests/ytdlp_fakes.py` (6.10), sockets blocked.

- **Success.** `test_resolve_builds_episode` (from `public.json`: title `Me at the zoo`,
  channel `jawed`, `published_at` 2005-04-24 03:31:52 UTC, `duration_s` 19.0, key
  `youtube_key("jNQXAC9IVRw")`, `audio_url == ""`, `description == ""`);
  `test_download_writes_destination` (the fake session writes the payload to `outtmpl`;
  `download` returns `destination("webm")`); `test_one_extraction_per_job`
  (`extract_info` once, `process_ie_result` once, on two sessions, the second with
  `outtmpl`); `test_download_without_resolve_extracts`; `test_stale_pending_reextracts`
  (an injected clock past `PENDING_MAX_AGE_S`); `test_outtmpl_doubles_percent` (a
  destination under a directory named `a%b`).
- **Each error class and its retry intent.** `test_refusals` (parametrised, each with its
  exact message and `PermanentError`): `private.json` (warning text), `removed.json`,
  `tos_removed.json`, `age_gated.json` (structured `needs_auth`, 18), `members.json`
  (`subscriber_only`), `premium.json`, `sign_in.json` (`needs_auth`, age 0), `geo.json`
  (warning), `bot.json` (warning), `not_video` (a `_type` of `playlist`; and an id
  mismatch); `test_refusal_from_extractor_error` (`ExtractorError(expected=True)` with each
  reason text); `test_rate_limited_is_transient` (`retry_after_s == 3600`);
  `test_network_errors_are_transient` (a `TransportError` cause, HTTP 503 and 429, an
  `IncompleteRead`, `ContentTooShortError`, each wrapped in `DownloadError` as yt-dlp
  does); `test_http_403_names_upgrade`; `test_http_404_is_permanent`;
  `test_extractor_bug_is_out_of_date` (`ExtractorError(expected=False)`, a `KeyError`
  raised by the session); `test_os_error_propagates`; `test_base_exception_propagates` (a
  `BaseException` subclass from the session leaves unchanged); `test_no_audio_format`
  (no `format_id`; `vcodec` not `none`; `protocol` `m3u8_native`); `test_stream_off_host`
  (`https://example.com/…`, `http://…googlevideo.com/…`); `test_nothing_written`;
  `test_empty_file_is_transient`.
- **The size cap.** `test_size_cap_declared` (`filesize` above the cap: permanent, the
  message names `EARMARK_MAX_AUDIO_MB`, `process_ie_result` never called, falling back to
  `filesize_approx`); `test_size_cap_while_downloading` (the fake session calls the hook with
  growing `downloaded_bytes` past the cap: permanent, the `.part` left);
  `test_size_cap_on_disk`; `test_deadline_is_transient` (injected clock).
- **A live stream refused.** `test_live_refused` (`live.json`), `test_upcoming_refused`,
  `test_post_live_refused`: permanent, `process_ie_result` never called, nothing written.
- **The options.** `test_params_are_exact` (the key set of 4.6, and each value);
  `test_params_youtube_only` (`allowed_extractors == ["youtube"]`, `ie_key="Youtube"` on
  every `extract_info`, `noplaylist`); `test_params_no_cookies_config_plugins` (none of the
  never-set keys present; `cachedir` `False`; `remote_components == []`; `proxy == ""`;
  the runtime factory was given `plugin_dirs` cleared, recorded by the fake);
  `test_params_no_postprocessing` (`postprocessors == []`, `fixup == "never"`);
  `test_js_runtime_is_the_venv_deno`.
- **Untrusted metadata.** `test_metadata_sanitised` (a title with CR, LF, tabs and 600
  characters; a channel that is a list; `timestamp` `True`, negative or huge, falling back
  to `upload_date`; a bad `upload_date`; `duration` `NaN` and `"19"`);
  `test_messages_carry_no_url_or_ytdlp_text` (every mapped message is free of `http`, the
  id, and the fixture's warning text).
- **Environment and logging.** `test_deno_environment` (inside a session call `DENO_DIR` is
  `<data>/cache/deno` and `DENO_NO_UPDATE_CHECK` is `1`; both restored after; an operator's
  `DENO_DIR` kept); `test_ytdlp_debug_never_printed` (earmark's handler at DEBUG on a
  `StringIO`: a debug line with a googlevideo URL does not appear, a warning appears with
  its URL scrubbed).
- **Laziness and the extra.** `test_import_is_light` (a subprocess imports
  `earmark.youtube.ytdlp` and `earmark.pipeline.factory`: no `yt_dlp` in `sys.modules`);
  `test_missing_extra_is_config_error` (`sys.modules["yt_dlp"] = None`: the message names
  the `youtube` extra and `uv sync --extra youtube`); `test_missing_deno_is_config_error`.

### 6.3 Brief: "Tests assert the options passed to yt-dlp: YouTube only, and no cookies, config, plugins or playlists."

`test_params_are_exact`, `test_params_youtube_only`, `test_params_no_cookies_config_plugins`
(6.2), and in `tests/test_ytdlp_real.py` (`slow`): `test_real_plugins_disabled`
(`yt_dlp.globals.plugin_dirs.value == []` after `load_runtime()`) and
`test_real_params_accepted` (a real `YoutubeDL(ytdlp_params(…))` constructs offline, loads
exactly one extractor, `Youtube`, and its `proxies` is `{"all": "__noproxy__"}`).

### 6.4 Brief: "A cached transcript is reused across link forms. A re-share of the same video classifies as reshare in the receiver."

- `tests/test_runner.py`: `test_youtube_job_runs` (stages resolve, fetch, transcribe,
  deliver; the note is a video note; staging empty after); `test_youtube_cache_reused`
  (two jobs, different instructions: one `download`, one transcription, the same
  `episode_key`); `test_youtube_cached_skips_download`; `test_youtube_episode_mismatch`;
  `test_youtube_refusal_fails_job` (a fake `PermanentError` at resolve: failed, notified,
  staging clean).
- `tests/test_e2e.py`: `test_youtube_two_link_forms_one_transcript`: `youtu.be/<id>?si=x`
  and `/shorts/<id>` posted with different instructions through the app, two
  `worker --once` runs on fakes; both `done`, one transcription, two messages with the same
  guid and `transcript_sha256`.
- `tests/test_receiver.py`: `test_youtube_reshare` (file `v2/youtube`, then classify
  `v2/youtube.reshare`: `reshare`, the same path); `test_reshare_across_contract_versions`
  (file `v1/podcast-feed`, then `v2/podcast-feed.reshare`: `reshare`).

### 6.5 Brief: "Every v2 sample passes the checker, and the mail code reproduces it. Every v1 sample still passes. For each new v2 rule, a mutated sample breaking it fails the checker."

- `tests/test_contract_samples.py`: `test_samples_reproduce` (v2, byte for byte);
  `test_v1_samples_frozen` (each v1 file's sha256 equals `V1_SHA256`, and the set of
  files is exactly `V1_NAMES`); `test_sample_set_is_complete` (both READMEs name every
  file); `test_generator_check_and_write`, `test_generator_check_of_the_committed_samples`
  (v2, and a changed v1 file fails `--check`); `test_youtube_sample_properties` (guid
  `youtube:exampleVid1`, `source_url` `https://youtu.be/exampleVid1`, `channel`, the
  heading, `tags`, `published 2026-11-21`, `shared_at` ending `+02:00`; the no-channel
  sample has no `channel`, a date-only `published` and no `language`);
  `test_every_v2_message_is_contract_2`; the existing property, hostile, resend, reshare,
  placeholder and wire-format tests on v2, and the placeholder and wire tests on v1 too.
- `tests/test_mail_checker.py`: `test_samples_pass_message_mode` and
  `test_samples_pass_files_mode` over `ALL_SAMPLES` on every floor interpreter (the JSON's
  `contract` is 1 or 2 as the folder says); `test_mutation_fails` gains the cases below,
  each with `version="v2"` and base `youtube` unless named; `test_every_rule_has_a_mutation`
  covers `note.source-url-youtube`.

  | Rule | Mutation |
  |---|---|
  | `note.contract` | `contract: 3` in both files |
  | `instructions.contract` | note 2, instructions 1 |
  | `note.type` | `type: "video"` in v1 `podcast-feed` (with `contract: 1`) |
  | `note.origin` | `origin: "youtube"` with `type: "podcast"` (v2 `podcast-feed`); `origin: "url"` on a video |
  | `note.keys` | a `channel` line in v1 `podcast-feed` |
  | `note.omitted` | `channel` on v2 `podcast-feed`; `show` on a video; `apple_url` on a video |
  | `note.required` | a video without `source_url` |
  | `note.guid` | `youtube:short`; a `sha256:` guid on a video; `youtube:<id>` on v2 `podcast-feed` |
  | `note.source-url-youtube` | `https://youtu.be/exampleVid2`; `https://www.youtube.com/watch` (no query, other path) |
  | `note.heading` | a video with `channel` whose heading lacks it |
  | `note.tags` | a video with `tags` `podcast` |
  | `string.length` | a 301-character `channel` |

  (Each mutation that changes a key the mutation does not target keeps the rest valid, as
  the step 29 cases do; the hash is re-computed where the body changes.)
- `tests/test_receiver.py`: `test_every_sample_classifies_new` and `…_eml` over
  `ALL_SAMPLES`; `test_unknown_contract` uses 3; `test_v2_retranscription`
  (`podcast-feed` then `podcast-feed.retranscription`, both v2).
- `tests/test_plugin.py`: the `1.1.0` row passes `test_plugin_release_is_pinned`;
  `SKILL.md`'s required statements and `test_skill_length` (at most 300 lines) still hold.
- `tests/test_checker_floor.py`: unchanged, and passes (one copy, stdlib only, no bytecode,
  the gate rejects 3.10+ probes).

### 6.6 Brief: "`make check` passes, including the 3.9 floor for the plugin's scripts."

`make check`. The checker and receiver changes use no 3.10+ syntax or API: `X | None` only
in annotations (the modules already have `from __future__ import annotations`), no
`match`, no `zip(strict=…)`, no `int.bit_count`. The ruff `py39` target and the pinned mypy
in `tools/py39/` enforce it.

### 6.7 Brief: "The step plan reports as unverified …"

Nothing to test; section 7 and the step summary say it. Also `tests/test_docs.py`
`test_deploy_doc_youtube` checks deploy section 18 names `uv lock --upgrade-package yt-dlp`,
`yt-dlp-ejs`, `deno`, `EARMARK_YOUTUBE_BACKEND`, and that the runbook's phase 4 has a
YouTube request and check.

### 6.8 Criteria this plan adds

- **The migration.** `tests/test_db.py`: `test_migration_0004_keeps_rows` (a schema-3
  database with a row of each source, a `sending` delivery and warnings: every column
  equal after, `application_id` kept, `quick_check` ok, the three indexes present);
  `test_migration_0004_accepts_youtube_only` (`youtube` inserts, `vimeo` fails the check);
  `test_migration_0004_keeps_active_dedupe`; `test_schema_version_is_4`; the existing
  refusal tests still pass.
- **Jobs.** `tests/test_jobs.py`: `test_youtube_dedupe_subject` (two `NewJob`s with the
  same canonical URL, `to` and instructions are one job).
- **Staging.** `tests/test_staging.py`: `test_release_clears_youtube_downloads`.
- **Notifications.** `tests/test_ntfy.py`: `test_youtube_labels` (with and without a
  channel, and before resolve); `tests/test_leaks.py`: `test_youtube_job_leaks_nothing`
  (logs and push: no instructions, masked address, no URL path or query, no guid).
- **Doctor.** `tests/test_doctor.py`: `test_youtube_fake_warns`, `test_youtube_ok_versions`
  (a stub runtime through `monkeypatch` of `load_runtime`), `test_youtube_missing_extra_fails`.
- **Factory, config, stages, fakes.** `FIELDS` in `tests/test_factory.py` gains
  `youtube_backend`; `test_pipeline_modules_import_light` adds `yt_dlp` to the heavy set;
  `test_all_fake_builds_fakes` checks `FakeYoutubeSource`; `tests/test_config.py`
  `test_youtube_backend_default`; `tests/test_stages.py` conformance of `FakeYoutubeSource`
  and `YtDlpYoutubeSource` to `YoutubeSource`; `tests/test_fakes.py`
  `test_fake_youtube_source`.
- **Log.** `tests/test_log.py`: `yt_dlp` is quiet.
- **The note.** `tests/test_note.py`: `test_video_note_fields`, `test_video_note_no_channel`,
  `test_video_slug_fallback` (empty title and channel → `youtube-ex-ample-02`),
  `test_every_note_is_contract_2`, `test_podcast_is_apple_only`.
- **Deploy and CI.** `tests/test_deploy.py`: the CPU default extras are
  `transcribe youtube` and exist; compose's `EXTRAS` default. `tests/test_deploy_gpu.py`:
  both syncs have `--extra youtube`; the runtime stage runs `deno --version` and imports
  `yt_dlp`; no `uv` after the swap still holds. `tests/test_ci.py`: the slow job's two
  commands; the container checks.
- **Docs.** `tests/test_docs.py`: `CONTRACT_V2_SHA256` pins the v2 block;
  `test_contract_v2_text_exists_once` (no 40-character line of it in any other `*.md`,
  except `docs/steps/32-youtube-source.md`, which specified it); `CONTRACT_DOCS` gains
  `contract/mail-v2.md` and `contract/samples/v2/README.md`; `test_contract_readme` and
  `test_contract_still_draft` cover v2; `test_architecture_system_seams` and
  `test_architecture_mail_contract_provenance` follow 4.15; `test_plan_and_architecture_point_to_contract`
  also wants `contract/mail-v2.md`; `PLANS` gains this plan (placeholder scan).

### 6.9 The opt-in tests

- `tests/test_ytdlp_real.py`, marker `slow` (needs the extra; fails, does not skip, without
  it, like the transcribe `slow` tests), sockets blocked: `test_real_runtime_loads`
  (versions, Deno path executable), `test_real_deno_runs` (`deno --version` as a
  subprocess), `test_real_plugins_disabled`, `test_real_params_accepted`,
  `test_real_errors_classify` (real `DownloadError` wrapping real `ExtractorError`s with a
  real `HTTPError` built from `yt_dlp.networking.Response(io.BytesIO(b""), url, {},
  status=503)` and `403`, a `TransportError`, `GeoRestrictedError`, and expected and
  unexpected `ExtractorError`s: each maps as 4.7 says).
- `tests/test_youtube_live.py`, marker `network`, skipped unless `YOUTUBE_TEST=1` (CI never
  sets it: GitHub's runners get bot checks): `test_live_resolve_and_download` (the real
  backend on `jNQXAC9IVRw` into `tmp_path`: the metadata of 6.2, a `webm` or `m4a` over
  100 KB, nothing else written, `DENO_DIR` under `tmp_path`); `test_live_unavailable`
  (`aaaaaaaaaaa` is `removed`).

### 6.10 Fixtures and the fake runtime

Write these by hand from the recorded values below; no network is needed.
`tests/fixtures/youtube/public.json` is the processed info of "Me at the zoo" as recorded on
2026-11-25, trimmed to what earmark reads, with the selected format merged at the top level
and a placeholder stream URL:

```json
{
  "_type": "video", "id": "jNQXAC9IVRw", "title": "Me at the zoo", "channel": "jawed",
  "uploader": "jawed", "upload_date": "20050424", "timestamp": 1114313512, "duration": 19,
  "live_status": "not_live", "was_live": false, "is_live": false, "availability": "public",
  "age_limit": 0, "extractor_key": "Youtube",
  "webpage_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
  "format_id": "251", "ext": "webm", "protocol": "https", "acodec": "opus", "vcodec": "none",
  "filesize": 252182, "filesize_approx": 252180,
  "url": "https://rr1---sn-placeholder.googlevideo.com/videoplayback?expire=0&id=placeholder",
  "formats": [
    {"format_id": "139", "ext": "m4a", "acodec": "mp4a.40.5", "vcodec": "none", "protocol": "https", "filesize": 117526},
    {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "vcodec": "none", "protocol": "https", "filesize": 309288},
    {"format_id": "251", "ext": "webm", "acodec": "opus", "vcodec": "none", "protocol": "https", "filesize": 252182}
  ]
}
```

The refusal fixtures are small JSON files, each `{"info": {…}, "warnings": [...]}`:

| File | `info` | `warnings` |
|---|---|---|
| `private.json` | `id` `yZIXLfi8CZQ`, `title` `youtube video #yZIXLfi8CZQ`, `age_limit` 0, `formats` `[]` | `[youtube] Private video. If the owner of this video has granted you access, please sign in. Use --cookies-from-browser or --cookies for the authentication.`, `No video formats found!`, `Requested format is not available` |
| `removed.json` | `id` `aaaaaaaaaaa`, the same shape | `[youtube] This video is unavailable`, then the two above |
| `tos_removed.json` | `id` `Cr381pDsSsA` | `[youtube] This video has been removed for violating YouTube's Terms of Service`, then the two |
| `age_gated.json` | `id` `HtVdAasjOgU`, `title` `The Witcher 3: Wild Hunt - The Sword Of Destiny Trailer`, `channel` `The Witcher`, `live_status` `not_live`, `availability` `needs_auth`, `age_limit` 18, two video-only formats | `Requested format is not available` |
| `post_live.json` | `id` `jfKfPfyJRdk`, `title` `lofi hip hop radio`, `channel` `Lofi Girl`, `live_status` `post_live`, `availability` `public`, `formats` `[]` | `[youtube] This live stream recording is not available.`, then the two |

The rest are built in the test module from `public.json` with one field changed
(`live_status` `is_live` or `is_upcoming`; `availability` `subscriber_only`, `premium_only`
or `needs_auth` with age 0; `_type` `playlist`) or with a reason warning and no formats
(`Sign in to confirm you’re not a bot. …` with the curly apostrophe YouTube uses;
`Join this channel to get access to members-only content like this video, and other
exclusive perks.`; `The uploader has not made this video available in your country`;
`This content isn't available, try again later.`).

`tests/ytdlp_fakes.py`:

- stand-in classes with yt-dlp's attributes: `FakeYoutubeDLError(Exception)` with `msg`;
  `FakeExtractorError` (`orig_msg`, `expected`, `cause`, `video_id`); `FakeGeoRestrictedError`
  (an extractor error); `FakeDownloadError` (`exc_info`); `FakeDownloadCancelled`;
  `FakeContentTooShortError`; `FakeRequestError`, `FakeTransportError`, `FakeIncompleteRead`
  (a transport error), `FakeHTTPError` (`status`); and `FAKE_ERRORS = YtDlpErrors(...)`;
- `FakeSession(script)`: records every `extract_info` and `process_ie_result` call with its
  arguments and the params it was built with; on `extract_info` sends the script's warnings
  to `params["logger"].warning` and returns its info, or raises its error; on
  `process_ie_result(download=True)` calls each progress hook with the script's
  `downloaded_bytes` steps, then writes the script's payload to `params["outtmpl"]["default"]`
  with `%%` turned back into `%` (or writes nothing, or raises);
- `fake_runtime(tmp_path, script, *, deno=True) -> YtDlpRuntime` with
  `deno_path = tmp_path / "bin" / "deno"` (an executable stub), versions `2026.11.23`,
  `0.8.0`, `2.9.8`, and `open_session` building `FakeSession`s and recording them;
- `wrap(exc)`: `FakeDownloadError("ERROR: …", exc_info=(type(exc), exc, None))`, as
  `YoutubeDL.trouble` does.

## 7. Verification

```bash
uv sync --locked
make check
uv run --locked python -m tests.contract_samples --check
git diff --exit-code -- 'contract/samples/v1/*.eml' contract/mail-v1.md   # nothing changed
git status --short contract/ plugins/   # v2 files, the two READMEs, the plugin changes only
claude plugin validate --strict .                                # by hand; not in CI
```

**Live smoke on fakes** (the README quickstart's setup, with the new backend):

```bash
export EARMARK_DATA_DIR=$PWD/.smoke EARMARK_API_TOKEN=demo-token \
  EARMARK_ALLOWED_RECIPIENTS=you@example.org \
  EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_YOUTUBE_BACKEND=fake \
  EARMARK_ASR_BACKEND=fake EARMARK_MAIL_BACKEND=fake EARMARK_NOTIFY_BACKEND=fake
uv run earmark serve &   # then, in another shell with the same variables:
for url in 'https://youtu.be/jNQXAC9IVRw?si=x' 'https://www.youtube.com/shorts/jNQXAC9IVRw'; do
  curl -s http://127.0.0.1:8787/transcriptions -H "Authorization: Bearer demo-token" \
    -H "Content-Type: application/json" \
    -d "{\"url\": \"$url\", \"to\": \"you@example.org\", \"instructions\": \"$url\"}"
done
curl -s http://127.0.0.1:8787/transcriptions -H "Authorization: Bearer demo-token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/@example", "to": "you@example.org"}'   # 422
uv run earmark worker --once && uv run earmark worker --once
uv run earmark jobs
python3 -I plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py message .smoke/outbox/<job id>.eml
```

Check: both jobs `done` with the same `episode_key`; the second log line says the transcript
is cached; both outbox messages have `type: "video"`, `contract: 2`,
`guid: "youtube:jNQXAC9IVRw"`, and pass the checker; `.smoke/downloads/` is empty; the
`@example` request answers 422 with the `youtube_not_video` message. Stop the server and
delete `.smoke`.

**Optional, with network** (this sandbox reached YouTube during planning; GitHub's runners
may not):

```bash
uv sync --locked --extra youtube
uv run --locked --extra youtube pytest -m slow tests/test_ytdlp_real.py -v
YOUTUBE_TEST=1 uv run --locked --extra youtube pytest -m network tests/test_youtube_live.py -v -rs
uv sync --locked   # back to the default environment
```

**Unverified, and reported so:** the image builds with the new extra (both
`docker compose build` targets; CI builds only the CPU image), Deno inside the GPU image,
and a real YouTube download and transcription on the Spark. Those are runbook phases 3 and
4. Whether a PKA follows `SKILL.md` for a video note is phase 12, as before.

## 8. Deviations and decisions

### 8.1 P1 (the operator's): contract 2 for all mail — decided 2026-11-25: option A

**Option A, all mail (the operator's decision of 2026-11-25).** Every message carries
`contract: 2`.

- One current contract. The sender has one `CONTRACT`; a later revision replaces it the
  same way.
- No receiver is installed anywhere (runbook phase 11 not done), so there is no installed
  base to protect. The receiver ships in the same commit and accepts both versions, so v1
  mail already in an inbox (the operator's real shares) is still handled.
- A podcast or audio note differs from its v1 form only in the `contract` line, and the
  hash covers the body, so a podcast first filed from v1 mail and re-shared after the
  upgrade is a `reshare` (tested).
- Cost: the v1 samples can no longer be rebuilt by the mail code. They are frozen and pinned
  by hash. The brief's acceptance already reads that way ("the mail code reproduces" v2;
  "every v1 sample still passes").

**Option B, only YouTube (not chosen).** Podcast, upload and URL mail stays `contract: 1`.

- Nothing about existing mail changes, and the v1 sample tests stay as they are.
- But two versions are current at once, and the version number then means "the format for
  this origin" rather than "the format earmark sends now". The next revision has to settle
  the split anyway.

**Had the operator chosen B**, the implementer would have changed only this (it does not
apply):

- `mail/note.py`: `contract = 2 if episode.source == "youtube" else 1`, used in both files.
- `tests/contract_samples.py` keeps v1 exactly as today (generated and reproduced; no
  `V1_SHA256`) and adds a second table, `SAMPLES_V2`, with only the three YouTube samples;
  `make samples` writes both folders; `--check` checks both.
- `contract/samples/v1/README.md` is not touched (D9 drops), and `contract/samples/v2/`
  holds three samples.
- `mail-v2.md` adds one sentence outside the marked block: earmark sends contract 2 only
  for `origin: youtube`. The contract text inside the markers is unchanged.
- Tests: `test_v1_samples_frozen`, `test_every_v2_message_is_contract_2`,
  `test_every_note_is_contract_2` and `test_reshare_across_contract_versions` go;
  `test_podcast_note_stays_contract_1` and `test_youtube_note_is_contract_2` come; the v2
  mutation cases that use v2 `podcast-feed` use the v2 YouTube sample with the type and
  origin changed instead.
- Everything else (links, backend, migration, checker, receiver, plugin 1.1.0, docs) is the
  same.

### 8.2 Other decisions

- **D1: yt-dlp downloads the audio itself** (2.2), so this is the one exception to "every
  third-party fetch goes through `fetch.py`", narrowed as in 5.6.
- **D2: a new stage, `YoutubeSource`,** with its own setting and registry, rather than a
  second method on `AudioFetcher` or a second `Resolver`. Step 07 fixed "the resolver
  takes only Apple Podcasts episode links", and the backend needs yt-dlp, which neither
  existing backend should import. `Backends` grows a sixth field.
- **D3: `type: video` with a new `channel` key** (decided by the operator on 2026-11-25),
  not `type: podcast` with `show`. A
  channel is an uploader, not a series, and a PKA's Repo Map may route `show:` notes to a
  podcasts folder. `published` is reused for the upload date, `source_url` for the short
  link (no query, so v1's rule holds), and the guid needs only a new prefix. The operator may
  prefer reusing `show`; it would make contract 2 one rule smaller and file YouTube notes
  like podcasts.
- **D4: refusals** (decided by the operator on 2026-11-25). Age-gated, members-only, Premium
  and private videos are refused, since
  earmark never signs in (no cookies: the brief). A bot check is permanent (5.8). No
  duration cap (5.4).
- **D5: the canonical form is `https://www.youtube.com/watch?v=<id>`** in the job, and
  `https://youtu.be/<id>` in the note (query-free, so it fits `source_url`).
- **D6: YouTube is on by default** (decided by the operator on 2026-11-25).
  `EARMARK_YOUTUBE_BACKEND` defaults to `ytdlp`, both
  images install the extra (the CPU image grows by about 45 MB, mostly Deno, so a CPU worker
  works too), and a worker without the extra refuses to start with a `ConfigError` naming
  it, as faster-whisper does. A native install that skips `--extra youtube` on upgrade
  must add it or set `EARMARK_YOUTUBE_BACKEND=fake`; deploy section 15 says so.
- **D7: the dependency** is `yt-dlp[default,deno]`: the `default` extra pins the matching
  `yt-dlp-ejs` exactly, and the `deno` extra brings a locked, aarch64 Deno with no system
  package. Node (needs `--js-runtimes node`, an apt or tarball install, and has weaker
  sandboxing), QuickJS (slower, temp files, not on PyPI for aarch64) and Bun (deprecated)
  were not chosen. The `default` extra also brings requests, urllib3, websockets, brotli,
  mutagen, pycryptodomex and certifi; yt-dlp prefers requests for HTTP. `curl-cffi`
  (impersonation) is not needed for YouTube and is left out.
- **D8: migration `0004` rebuilds `jobs`** (4.4). After it, an image from before step 32
  refuses the database ("newer than this earmark supports"); deploy section 15 says to back
  it up before upgrading.
- **D9: under A, `contract/samples/v1/README.md` changes** (its first paragraph says the
  samples are frozen since step 32). The brief keeps "v1's text, its pin and its samples"
  unchanged, and they are: the `.eml` files and the marked text. Leaving the README as is
  would leave it saying a test rebuilds them.
- **D10: contract 2 is written as changes to contract 1**, which is the smallest text and
  copies no v1 line. Its "exists once" test exempts this plan, which had to give the text.
- **D11: the weekly `slow` job installs the `youtube` extra**, so the real library is
  exercised offline every week; the live download stays opt-in (`YOUTUBE_TEST=1`), because
  CI's addresses get bot checks.
- **D12: the planner did not edit `docs/ARCHITECTURE.md`.** Its changes (4.15) depend on
  the operator's answer to P1 and are part of the step's build; the implementer writes them
  with the code. The planner changed nothing outside `docs/steps/`.

## 9. Out of scope

- Other sites yt-dlp supports, YouTube playlists and channels, and video files (backlog).
- Cookies, sign-in, PO-token providers and `curl-cffi`.
- An `earmark doctor --online` check against YouTube (the runbook's phase 4 is the check).
- Calling contract 1 or 2 final: both stay DRAFT until the operator approves the receiving
  skill after phase 12.
- An HMAC over the attachments (a later contract).
- The yanked `av==18.1.0` in the lock (`uv lock` warns; upgrading PyAV is a separate
  change).
- The Shortcut file (step 31).
