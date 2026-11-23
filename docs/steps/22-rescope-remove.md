# Step 22: Re-scope I: remove analysis, vault, entities, search, re-run, digest

## 1. Outcome

When this step is done, earmark is the transcription half of the old pipeline. It has an API
with its auth (`POST /ingest`, `GET /jobs`, `GET /jobs/{id}`, `GET|HEAD /healthz`), the
job store and worker, the Apple resolver behind the guarded fetch, local and remote
transcription with the transcript cache, ntfy, and doctor. A job runs
resolve → fetch → transcribe → complete → notify. It ends `done` with
`transcript.json` and `transcript.txt` cached and `JobResult(duration_s=…)` recorded. The
push says "Transcribed: <title>" with the duration and carries no URL, prompt or path.

These are deleted, with their tests, settings, `.env.example` lines, workflows and
dependencies:

- the `analyze/`, `vault/`, `index/` and `digest/` packages, and `episodes.py`;
- the `anthropic`, `lancedb`, `fastembed` and `tzdata` dependencies, the `index` extra,
  and PyYAML as a runtime dependency (it moves to the dev group);
- the routes `GET /search`, `GET /episodes` and `POST /episodes/{key}/analyze`;
- the commands `analyze`, `reanalyze`, `episodes`, `digest`, `entities rebuild`, `search`
  and `reindex`;
- the `reanalyze` job kind;
- the `llm` and `index` markers and workflows;
- the digest systemd units and `docs/llm.md`.

The sanitizers and the frontmatter emitter move to `src/earmark/sanitize.py`.

A new database lineage is marked with `PRAGMA application_id`. `serve`, `worker`, `submit`
and `jobs` refuse a database from `v0.1.0-full-pipeline`, and doctor reports it as a
failure. The refusal names the file and says to move it aside. Nothing is migrated.

Prose docs are left alone, except where a doc test forces a change (section 3.7). The
README quickstart, `docs/deploy.md`, `docs/shortcut.md`, the runbook (step 27),
`docs/ARCHITECTURE.md` and `CLAUDE.md` (step 23) may still describe removed behaviour.
**The verifier must not fail this step for that.**

## 2. Findings

### 2.1 How it was checked

- `git diff --stat v0.1.0-full-pipeline HEAD` touches only `docs/PLAN.md` and
  `docs/decisions/…`. The code at HEAD is therefore the tagged code. In particular,
  `src/earmark/db.py` at HEAD *is* the v0.1.0 DDL: migrations `0001_jobs` and
  `0002_entities`, `SCHEMA_VERSION = 2`, and no `application_id`.
- An import grep across `src/` found every coupling from kept code to removed code:
  - `notify/ntfy.py` imports `clean_unicode` and `fit_bytes` from `vault.sanitize`;
  - `fakes.py` imports `digest.model`, `digest.prompts`, `index.chunks`, `index.search`
    and `vault.document.new_note`;
  - `api/app.py`, `api/routes.py` and `api/schemas.py` import `index.search` and
    `episodes`;
  - `doctor/checks.py` lazily imports `vault`, `analyze.llm.*` and `index.*`;
  - `doctor/online.py` has the LLM and Anthropic branches;
  - `pipeline/factory.py` imports `digest` and `index` at the top;
  - `cli.py` has the removed commands;
  - `errors.NeedsChunking` exists only for the analyzer;
  - `log.QUIET_LOGGERS` contains `"anthropic"`;
  - `endpoint.py`'s docstring names the local LLM.
- A trial relock was run in a scratch copy, with `uv lock --offline` (no network). The
  scratch copy was deleted afterwards. It removed the core deps `anthropic`, `pyyaml` and
  `tzdata`, and the `index` extra, and added `pyyaml` to the dev group. Lock output:
  - Removed: `anthropic`, `charset-normalizer`, `colorama`, `deprecation`,
    `docstring-parser`, `fastembed`, `jiter`, `lance-namespace`,
    `lance-namespace-urllib3-client`, `lancedb`, `loguru`, `mmh3`, `pillow`,
    `py-rust-stemmers`, `pyarrow`, `python-dateutil`, `requests`, `sniffio`, `tzdata`,
    `urllib3` and `win32-setctime`.
  - `pyyaml` **stays in `uv.lock`**, because `ctranslate2` (transcribe extra) depends on
    it. The `earmark` package entry lists it only under `[package.dev-dependencies]`, and
    that is what "PyYAML is a dev-only dependency" means.
  - `uv lock --offline` is enough; no network is needed.
- SQLite 3.46.1 (the sandbox's `sqlite3.sqlite_version`): `PRAGMA application_id = N`
  inside `BEGIN IMMEDIATE` is rolled back by `ROLLBACK` and kept by `COMMIT`. It is
  transactional like `user_version`, so it can be set in the migration transaction.
- `ruff format --check .` also formats Python code blocks in Markdown. It reported 214
  files, including `docs/steps/*.md`. Code blocks in this plan are kept ruff-clean.
- `uv run --locked pytest -m "network or slow or gpu" --collect-only -q` collects 16
  opt-in tests today without any extra. That makes it a cheap import check for the opt-in
  files after the change.
- The repo `.venv` has `anthropic`, `tzdata` and `yaml` installed. `uv run` does an
  inexact sync and would leave them installed. Run `uv sync` (exact) after relocking, so
  that no test passes only because a removed package is still importable.

### 2.2 Doc tests that force doc edits

`tests/test_docs.py` checks the following. Its relative-link check covers `README.md`,
`docs/deploy.md` and `docs/shortcut.md`.

- The README settings table equals `Settings` (`test_readme_settings_reference_*`). Rows
  must go.
- The README command table equals the registered commands. Rows must go.
- The README "The HTTP API is …" paragraph equals the routes. The paragraph must change.
- Relative links must resolve. The README links to `docs/llm.md` three times
  (lines 25, 134, 230). `docs/deploy.md` links to `llm.md` (lines 52, 370) and to the two
  digest units (lines 446–447). Those links must go.
- `test_deploy_doc_digest_section` and `test_readme_documents_digest` test removed
  behaviour and are deleted. The quickstart tests still pass against the unchanged
  quickstart: four backends are set to `fake`, and the body still has a `prompt`. So the
  quickstart is **not** edited.

`docs/shortcut.md` needs no change, because `POST /ingest` and its `prompt` field stay
until step 24.

### 2.3 Dead ends

- **Do not detect the old database by `user_version` alone.** v0.1.0 ends at version 2.
  The new lineage restarts at 1, and steps 24 and 25 will add migrations that reach 2 and
  3, so a version number cannot tell the two apart. `application_id` can (section 5.1).
- **Do not write negative assertions that name removed things.** For example,
  `assert "/search" not in routes` or `"vault" not in COMPOSE_TEXT` would trip the
  verifier's grep (section 7.2). Pin exact sets instead: the routes, the command names,
  the `Settings` fields, the compose environment keys, the volumes, the systemd write
  paths.
- **Do not delete `tests/test_log_libraries.py` wholesale.** Its huggingface_hub and
  httpx tests guard the URL scrubbing for faster-whisper's model downloads, which stay.
  They move to the `slow` marker (section 3.6).
- **Do not check the refused database's bytes.** Opening a WAL database touches `-shm`.
  Assert its meaning instead: `user_version`, `application_id`, journal mode and the job
  rows.
- **Do not run `test_ntfy_live.py`.** It publishes to public ntfy.sh. Section 7 uses
  `--collect-only` for the opt-in tests.

## 3. Files

### 3.1 Delete: source

- `src/earmark/analyze/`: the whole package, including `llm/`.
- `src/earmark/vault/`: the whole package. Move `sanitize.py` first (section 3.4).
- `src/earmark/index/`: the whole package.
- `src/earmark/digest/`: the whole package.
- `src/earmark/episodes.py`.

### 3.2 Delete: tests (46 files)

- Analysis:
  - `tests/analysis_samples.py`, `tests/anthropic_stub.py`, `tests/llm_fakes.py`,
    `tests/llm_stub.py`, `tests/long_transcripts.py`;
  - `tests/test_analyze_live.py`, `tests/test_analyze_merge.py`,
    `tests/test_analyze_prompts.py`, `tests/test_analyze_render.py`,
    `tests/test_analyze_schema.py`, `tests/test_analyzer.py`;
  - `tests/test_chunking.py`, `tests/test_llm_anthropic.py`,
    `tests/test_llm_openai_compat.py`, `tests/test_map_reduce.py`,
    `tests/test_openai_compat_live.py`.
- Vault: `tests/test_vault_aliases.py`, `tests/test_vault_document.py`,
  `tests/test_vault_entities.py`, `tests/test_vault_fs.py`, `tests/test_vault_notes.py`,
  `tests/test_vault_sanitize.py`. Move the kept tests out of the last two first
  (section 3.4).
- Index:
  - `tests/index_helpers.py`;
  - `tests/test_index_chunks.py`, `tests/test_index_embed.py`,
    `tests/test_index_rebuild.py`, `tests/test_index_search.py`,
    `tests/test_index_slow.py`;
  - `tests/test_lancedb_index.py`, `tests/test_api_search.py`,
    `tests/test_doctor_index.py`.
- Re-run and episodes: `tests/episodes_helpers.py`, `tests/test_api_episodes.py`,
  `tests/test_episodes.py`, `tests/test_reanalyze.py`.
- Digest:
  - `tests/digest_helpers.py`;
  - `tests/test_digest_collect.py`, `tests/test_digest_live.py`,
    `tests/test_digest_prompts.py`, `tests/test_digest_render.py`;
  - `tests/test_digest_run.py`, `tests/test_digest_themes.py`,
    `tests/test_digest_window.py`.

### 3.3 Delete: other files

- `.github/workflows/llm.yml` and `.github/workflows/index.yml`.
- `deploy/systemd/earmark-digest.service` and `deploy/systemd/earmark-digest.timer`.
- `docs/llm.md`.

### 3.4 Create

- **`src/earmark/sanitize.py`** holds the kept sanitizers and the frontmatter emitter. It
  is standard library only. Move these from `vault/sanitize.py` **verbatim**:
  - `MAX_NAME_BYTES`, `ELLIPSIS`;
  - `_ZWNJ`, `_ZWJ`, `_SURROGATES`, `_DEVICE_NAMES`, `_UNSAFE_NAME_CHARS`, `_YAML_ESCAPED`;
  - `_blen`, `clean_unicode`, `_is_trailing_joiner`, `_is_regional`, `_repair_cut`;
  - `fit_bytes`, `is_safe_component`, `file_component`, `yaml_quote`.

  Move these from `vault/document.py` verbatim: `YamlValue`, `_KEY_NAME` and
  `emit_frontmatter`. It still writes `null` for `None` and refuses aware datetimes;
  step 25 adapts it to the mail contract. **Do not move:** `TITLE_BYTES`, `SHOW_BYTES`,
  `_MD_SPECIAL`, `fold_key`, `escape_markdown`, `wikilink_label` or `wikilink_target_ok`,
  which are vault-only. Module docstring: "Untrusted text to file names and YAML, and the
  frontmatter emitter. Standard library only."
- **`tests/test_sanitize.py`** holds the kept tests, moved unchanged except for the import
  path:
  - from `tests/test_vault_sanitize.py`: `test_file_component_cases`,
    `test_file_component_keeps_unicode`, `test_file_component_byte_cap`,
    `test_fallback_must_be_safe`, `test_cut_never_splits_graphemes`,
    `test_fit_bytes_edges`, `test_is_safe_component`, `test_yaml_quote_round_trip` and
    `test_yaml_quote_lone_surrogate_and_escapes`;
  - from `tests/test_vault_document.py`: `test_emit_frontmatter_shapes`.

  Add `test_sanitize_is_stdlib_only`. In a subprocess, `import earmark.sanitize` and
  assert that no `pydantic`, `httpx2`, `fastapi` or `yaml` module is loaded. Use the
  same pattern as the existing import-light tests.
- **`tests/legacy_db.py`** builds the v0.1.0 test fixture. It is a helper module, not
  collected. It holds:
  - `V010_SCHEMA_VERSION: Final = 2`.
  - `V010_STATEMENTS: Final[tuple[str, ...]]`: the eight SQL statements of v0.1.0's
    `MIGRATIONS`, in order. Copy them from
    `git show v0.1.0-full-pipeline:src/earmark/db.py`, lines 36–125:
    - from `0001_jobs`: the `jobs` table, `jobs_active_dedupe` and `jobs_by_status`;
    - from `0002_entities`: `episodes`, `mentions`, `mentions_by_name`, `aliases` and
      `entities`.

    Whitespace may be re-indented, but the SQL text must not change; section 7.3 checks
    this.
  - `make_v010_database(path: Path, *, wal: bool = True) -> Path` writes a database the
    way v0.1.0 left it:
    - journal mode WAL, unless `wal=False`;
    - every statement in one transaction;
    - `PRAGMA user_version = 2`, with `application_id` left at 0;
    - three `jobs` rows: a `done` `ingest` job, a `done` reanalyze job and a `queued`
      reanalyze job.

    The function closes its connection and returns `path`. Row values must satisfy the
    v0.1.0 CHECKs:
    - `warnings` is `'[]'`;
    - the timestamps are ISO strings;
    - the `done` ingest job's `result` is
      `'{"note_path":"Earmark/Podcasts/x.md","transcript_note_path":null,"analysis_hash":"bbbbbbbbbbbbbbbb"}'`;
    - each row has a distinct `dedupe_key`.
  - `V010_JOB_COUNT: Final = 3`.

  This is the only kept file allowed to spell the removed job kind and the old table
  names. Section 7.2 excludes it from the grep. Tests that use it import only
  `make_v010_database`, `V010_SCHEMA_VERSION` and `V010_JOB_COUNT`, and never spell those
  words themselves.

### 3.5 Change: source

| File | Change |
|---|---|
| `src/earmark/__init__.py` | Docstring: `"""earmark: share a podcast episode, get its transcript."""`. |
| `src/earmark/models.py` | `JobKind = Literal["ingest"]`; `Stage = Literal["resolve", "fetch", "transcribe", "notify"]`. Delete `ANALYSIS_SCHEMA_VERSION`, `PROMPT_HASH_LENGTH`, `PromptHash`, `prompt_hash`, `is_prompt_hash`, `entity_key`, `_check_relative_posix`, `ReanalyzeRequest`, `Claim`, `Entity`, `ResearchLead`, `Analysis` and `StoreResult`, plus the imports only they used (`json`, `unicodedata`, `PurePosixPath`). `JobResult` becomes `duration_s: Seconds` (required). `Job._kind_requirements` keeps only the url check. Keep `normalize_prompt`, `MAX_PROMPT_CHARS`, `IngestRequest` (with `prompt`), `ShareRecord`, `format_hms` and `scrub_surrogates`. |
| `src/earmark/errors.py` | Delete `NeedsChunking`. |
| `src/earmark/db.py` | The new lineage and the identity check (section 4.2). |
| `src/earmark/jobs.py` | Delete `enqueue_reanalyze`. `_enqueue` loses the `kind`, `subject` and `episode_key` parameters: kind is always `"ingest"`, and the subject is the URL. Keep the dedupe formula `_dedupe_key("ingest", url, prompt)` unchanged. |
| `src/earmark/artifacts.py` | Delete the analysis paths and I/O: `analyses_dir`, `analysis_path`, `analysis_parts_dir`, `_part_path`, `read_analysis_part`, `write_analysis_part`, `delete_analysis_part`, `clear_analysis_parts`, `write_analysis`, `read_analysis`, `list_analysis_hashes` and `list_analyses`. Also delete `_check_hash`, `_check_part_id`, `_ANALYSIS_NAME`, `_PART_ID` and the now-unused imports (`shutil`, `Analysis`, `is_prompt_hash`). Keep `has_episode`, `has_transcript`, `read_share` and `list_episode_keys`. They are generic accessors of the kept episode cache. |
| `src/earmark/pipeline/stages.py` | Delete `Analyzer`, `Store` and `Indexer`. `Backends` holds `resolver`, `fetcher`, `transcriber` and `notifier: Notifier \| None`, with the docstring "The four stage implementations the runner drives." Imports: `Episode`, `Job` and `Transcript` only. |
| `src/earmark/pipeline/runner.py` | Section 4.5. |
| `src/earmark/pipeline/factory.py` | Section 4.6. |
| `src/earmark/fakes.py` | Section 4.7. |
| `src/earmark/api/routes.py` | `build_router(store: JobStore) -> APIRouter` with `/ingest`, `/jobs/{job_id}`, `/jobs` and `/healthz` only. |
| `src/earmark/api/app.py` | `create_app(settings, *, store=None)`. Delete the `searcher` and `artifacts` parameters, `REANALYZE_ROUTE`, `REANALYZE_MISSING_BODY` and the reanalyze branch of `_on_validation_error`. Delete `_on_invalid_query`, `_on_index_mismatch` and `_on_search_error`, and their registrations. The `EarmarkError` handler goes too: `/ingest` catches `UnsupportedUrlError` itself. Keep `describe_validation_errors` with its keyword parameters. |
| `src/earmark/api/schemas.py` | Delete `SearchResponse` and the `index.search` import. |
| `src/earmark/cli.py` | Section 4.8. |
| `src/earmark/config.py` | Section 4.9. |
| `src/earmark/log.py` | Remove `"anthropic"` from `QUIET_LOGGERS`. Rewrite the module docstring and the `QUIET_LOGGERS` comment without the anthropic SDK, `ANTHROPIC_LOG` or fastembed. The "Not covered" paragraph keeps only the hf_xet sentence, because the urllib3 sentence referred to the removed paths. |
| `src/earmark/endpoint.py` | Docstring: "Helpers shared by the clients of operator-configured endpoints (remote ASR, ntfy)." |
| `src/earmark/notify/ntfy.py` | Section 4.10. |
| `src/earmark/doctor/__init__.py` | `Probes` drops `anthropic_http_client`. The comment on `http_transport` becomes `# ntfy, ASR`. |
| `src/earmark/doctor/checks.py` | Section 4.11. |
| `src/earmark/doctor/online.py` | Delete the `llm server` and `anthropic api` branches and `anthropic_api`. In `probe_models`, the result name is always `"asr server"`, and the parameter comment becomes `# "EARMARK_ASR", used in messages`. |
| `src/earmark/ingest.py`, `resolve/`, `fetch.py`, `audio.py`, `transcribe/`, `pipeline/worker.py`, `api/auth.py` | No change. |

### 3.6 Change: tests

The rule for every file below: delete each test of removed behaviour, and fix each test
of kept behaviour that used removed fixtures or settings. The work is mostly mechanical:

- `make_fake_backends(tmp_path / "data", vault_dir=…)` becomes
  `make_fake_backends(tmp_path / "data")`;
- drop the `vault_dir=`, `llm_provider=`, `store_backend=`, `index_backend=` and
  `obsidian_vault_name=` keyword arguments to `Settings(...)`, and the matching
  `EARMARK_*` environment variables in fixtures;
- drop `anthropic`, `lancedb` and `fastembed` from the heavy-module lists of the
  import-light tests.

| File | Change |
|---|---|
| `tests/conftest.py` | `_SOCKET_MARKERS = ("network", "slow")`. Delete the `ANTHROPIC_*` isolation block and `FASTEMBED_CACHE_PATH`. Keep `HF_HUB_OFFLINE=1` for non-socket tests, the `EARMARK_*` scrub, the `chdir` and both logging fixtures. Drop the now-unused `tmp_path_factory` parameter. |
| `tests/factories.py` | Delete `make_analysis` and its imports. `make_job() -> Job` (no `kind` parameter) returns the current ingest job with `result=JobResult(duration_s=125.5)`. |
| `tests/pipeline_env.py` | `make_fake_backends(tmp_path / "data")`. `PipelineEnv` drops `vault`. |
| `tests/ntfy_stub.py` | Docstring: "(like `asr_stub.AsrStub`)". |
| `tests/slow_assets.py` | Delete `embed_model_dir`. |
| `tests/test_api.py` | `create_app` calls lose the removed parameters. Add `test_routes_are_exactly_the_kept_ones` (section 6) and `test_create_app_refuses_v010_database`. |
| `tests/test_artifacts.py` | Delete `test_analysis_round_trip`, `test_mismatched_analysis_hash_is_permanent`, `test_list_analyses_sorted_and_filtered`, `test_analysis_parts_paths` and `test_list_analysis_hashes`. Remove the analysis lines from the path, key-check and corrupt-artifact tests (around lines 117, 130–132, 149–150 and 212–224). |
| `tests/test_audio.py`, `tests/test_faster_whisper.py`, `tests/test_remote_asr.py`, `tests/test_resolver.py` | Mechanical only. |
| `tests/test_worker.py` | Line 120: replace the `analyzer.call_count` assertion with `env.fakes.transcriber.computed == 0`. The stage was blocked before it computed. Line 220: mechanical. |
| `tests/test_ci.py` | Delete the llm-workflow and index-workflow sections. Replace `test_only_llm_workflow_references_secrets` with `test_no_workflow_references_secrets`: no workflow file matches `\bsecrets\b`. `test_network_workflow_is_not_the_gate` asserts `not network`, `not slow` and `not gpu`. `SLOW_COMMANDS` becomes `["uv sync --locked --extra transcribe", "uv run --locked --extra transcribe pytest -m slow -v -rs"]`. `test_container_workflow_checks_image` checks the modules `("ctranslate2", "faster_whisper")`. `test_container_workflow_validates_compose` drops the `VAULT_PATH` assertion. Add `test_workflow_files_are_the_known_three`: the set of workflow file names is exactly `{"ci.yml", "container.yml", "network.yml"}`. |
| `tests/test_cli.py` | Section 6.3. |
| `tests/test_config.py` | `test_defaults` lists only the kept fields. Delete `test_valid_tz_accepted`, `test_nested_vault_subdir_accepted`, `test_llm_settings_normalised`, `test_llm_settings_rejected`, `test_llm_api_key_blank_is_none`, `test_llm_base_url_validated`, `test_llm_response_format_normalised`, `test_embed_model_stripped_and_required`, `test_embed_model_dir_path` and `test_obsidian_vault_name_validated`. Drop the removed fields from the parametrized cases of `test_secret_fields_are_secretstr`, `test_secret_values_lists_only_set_secrets`, `test_invalid_values_rejected`, `test_all_backends_accept_fake` and `test_empty_values_are_unset`. Rename `test_backend_none_allowed_for_index_and_notify` to `test_notify_backend_none_allowed`. Add `test_settings_fields_are_exactly` (section 4.9). |
| `tests/test_db.py` | Delete `test_entities_migration_tables`. Add the identity tests (section 6.2). |
| `tests/test_deploy.py` | Section 4.12. |
| `tests/test_docs.py` | Section 3.7. In `test_quickstart_uses_fake_backends`, `fields` lists the `*_backend` fields only, and the assertion becomes `assert len(fields) == 4`. `SECRET_FIELDS` becomes `("api_token", "asr_api_key", "ntfy_url", "ntfy_token")`. `test_readme_http_api_lists_every_route` builds `build_router(store)`. Delete `test_deploy_doc_digest_section` and `test_readme_documents_digest`. |
| `tests/test_doctor.py` | Section 6.4. |
| `tests/test_e2e.py` | Section 6.1. |
| `tests/test_errors.py` | Delete `test_needs_chunking_is_permanent` and the import. |
| `tests/test_factory.py` | Section 6.5. |
| `tests/test_fakes.py` | Delete the analyzer, store, indexer, themer and digest tests: `test_analyzer_cache_hit_not_computed`, `test_analyzer_new_prompt_computes`, `test_store_twice_same_bytes`, `test_store_without_vault_writes_nothing`, `test_store_rejects_bad_subdir`, `test_analyzer_prompt_response`, `test_indexer_rows_replace`, `test_fake_themer_one_theme_per_show` and `test_fake_store_write_digest_statuses`. Adapt `test_fakes_deterministic`, `test_call_counts`, `test_every_fake_supports_fail_next` (the list is resolver, fetcher, transcriber, notifier), `test_fake_pipeline_writes_artifacts` (episode, transcript and audio only), `test_fake_warnings_flow` (transcriber warnings only) and `test_fake_backends_as_backends`. |
| `tests/test_jobs.py` | Delete `test_reanalyze_dedupe` and `test_enqueue_reanalyze`. `test_complete_stores_result` uses `JobResult(duration_s=12.5)`. |
| `tests/test_log.py` | `test_http_client_loggers_do_not_log_urls_at_info` loops over `("httpx2", "httpcore2")`. Delete `test_anthropic_debug_suppressed`. Line 174's comment becomes `# what a library's debug switch does`. In the access-line scrub cases (lines 137–140), replace `/search?q=…` with `/jobs?status=failed&limit=5`; the expectations keep the same shape, with `"GET /jobs HTTP/1.1"`. Move `test_adopt_does_not_import_huggingface_hub` here, unchanged, from `test_log_libraries.py`. |
| `tests/test_log_libraries.py` | Keep it under the `slow` marker. The `slow` CI job installs the transcribe extra, which brings `huggingface_hub` and `httpx`, as computed from `uv.lock`. `pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("restore_root_logging", "restore_library_loggers")]`. Keep `hub_downloads`, `test_hf_download_logs_no_urls`, `test_hf_hint_logged_once` and `test_httpx_httpcore_over_uds_log_no_paths`. In the two hf tests, replace `load_fastembed_runtime()` with `adopt_library_loggers()` from `earmark.log`. The fixture has already imported `huggingface_hub`, so `adopt_library_loggers` imports `huggingface_hub.utils.logging` and strips its handler, which is what the fastembed loader did. Delete `test_urllib3_over_uds_logs_no_path` (urllib3 is no longer installed), `test_fastembed_loguru_stays_disabled`, `test_lancedb_local_ops_log_no_urls` and `test_index_libraries_install_no_handlers` (`test_faster_whisper_slow.py::test_load_runtime_leaves_no_library_handlers` covers the transcribe libraries). Docstring: "Real huggingface_hub and httpx/httpcore logging (`-m slow`, needs the transcribe extra)." The imports of `FakeEmbedder`, `earmark.index.*`, `tests.test_lancedb_index` and `uds_connection_class` go. |
| `tests/test_models.py` | Delete `test_prompt_hash_*`, `test_analysis_hash_must_match`, `test_store_result_*`, `test_entity_key`, `test_entity_id_golden` and `test_reanalyze_request_prompt_rules`. `CASES` loses the removed models, and the `JobResult` cases become `JobResult(duration_s=0.0)` and `JobResult(duration_s=3600.5)`. `test_job_kind_requirements` keeps the ingest-without-url case, and adds `assert typing.get_args(JobKind) == ("ingest",)`. |
| `tests/test_ntfy.py` | Section 6.6. |
| `tests/test_ntfy_live.py` | Build `Settings` without `obsidian_vault_name`, and the job with `result=JobResult(duration_s=125.5)`. Expect: title `"Transcribed: Live: Café X-Evil: 1 = ?UTF-8?B?4pyT?="`; message `"Le Podcast ☕\nDuration: 00:02:05"`; `"click" not in message`; tags and priority unchanged. Drop the `obsidian_url` import. **Not run in this step.** |
| `tests/test_runner.py` | Section 6.7. |
| `tests/test_stages.py` | The conformance list is the four fakes against `Resolver`, `AudioFetcher`, `Transcriber` and `Notifier`. Drop the `VaultStore` import and its row. Keep `test_protocol_mismatch_fails_mypy`, which uses a kept Protocol. |
| `tests/test_tooling.py` | Section 6.8. |

### 3.7 Change: config, deploy, CI and docs

- **`pyproject.toml`**:
  - `description = "Share a podcast episode, get its transcript."`;
  - `dependencies` lose `"anthropic>=2.11.0"`, `"pyyaml>=6.0.3"` and `"tzdata>=2026.6"`;
  - `[project.optional-dependencies]` loses `index`;
  - `[dependency-groups].dev` gains `"pyyaml>=6.0.3"`, kept alphabetical, before
    `types-pyyaml`;
  - pytest `addopts` uses `"-m", "not network and not slow and not gpu"`, and the NOTE
    comment says `` `-m "not slow"` would also include network and gpu tests``;
  - `markers` becomes:
    - `"network: hits real external endpoints (Apple, podcast feeds)"` (unchanged);
    - `"slow: downloads a model, does CPU-heavy work, or needs the transcribe extra"`;
    - `"gpu: needs CUDA"` (unchanged).
- **`uv.lock`**: regenerate with `uv lock --offline`, then run `uv sync`. Never
  hand-edit it.
- **`.env.example`**:
  - delete `EARMARK_VAULT_DIR`, `EARMARK_VAULT_SUBDIR`, `EARMARK_OBSIDIAN_VAULT_NAME`
    and its comment;
  - delete `EARMARK_LLM_PROVIDER`, `EARMARK_STORE_BACKEND` and `EARMARK_INDEX_BACKEND`;
  - delete the whole `--- Analysis ---` and `--- Index ---` blocks, comments included;
  - delete `# EARMARK_TZ=`;
  - backends header: `# --- Backends (fake exists for tests; none disables notify) ---`;
  - compose section: delete the `VAULT_PATH` lines (the `# See docs/deploy.md.` comment
    stays); `# EXTRAS=transcribe`.
- **`compose.yaml`**:
  - the header comment is unchanged; the gpu profile lines stay until step 26;
  - `EXTRAS: ${EXTRAS:-transcribe}`;
  - delete `EARMARK_VAULT_DIR: /vault` and `EARMARK_EMBED_MODEL_DIR: ""`, keeping the
    "Empty means unset" comment for `EARMARK_ASR_MODEL_DIR` and changing it to "models go
    to the volume (`HF_HOME`)";
  - volumes: only `- data:/data`;
  - the `asr` service is unchanged.
- **`Dockerfile`**:
  - the comment becomes `# Optional extras, space-separated: "transcribe" (default) or "".`;
  - `ARG EXTRAS="transcribe"`;
  - the runtime comment becomes `# The user that owns /data.`;
  - `install -d -o earmark -g earmark -m 0750 /data`;
  - delete `EARMARK_VAULT_DIR=/vault \` from `ENV`.
- **`deploy/systemd/earmark-worker.service`**: delete the vault comment, and use
  `ReadWritePaths=/srv/earmark/data`.
- **`.github/workflows/container.yml`**:
  - the header comment is unchanged;
  - in "Validate compose.yaml", delete the `printf 'VAULT_PATH=…'` line;
  - in "Check the image", use
    `docker run --rm --entrypoint python earmark:ci -c "import ctranslate2, faster_whisper"`.
- **`.github/workflows/network.yml`**, slow job:
  - the step name "Cache the Whisper model and the test clip";
  - "Install dependencies with the transcribe extra", running
    `uv sync --locked --extra transcribe`;
  - "Slow tests", running `uv run --locked --extra transcribe pytest -m slow -v -rs`.
- **`README.md`**. Only these edits; everything else waits for step 27:
  1. Delete the `- **analyze**: …` bullet (lines 24–25), which links `docs/llm.md`.
  2. Delete the guide bullet `- [docs/llm.md](docs/llm.md): …` (line 134).
  3. In the command table, delete the rows `analyze`, `episodes`, `reanalyze`, `digest`,
     `entities rebuild`, `search` and `reindex`. Also delete the paragraph right after
     the table (lines 161–162), which only explains two of the deleted rows.
  4. The HTTP API paragraph becomes: "The HTTP API is `POST /ingest`, `GET /jobs/{id}`,
     `GET /jobs`, and `GET|HEAD /healthz`. Everything except `/healthz` needs
     `Authorization: Bearer $EARMARK_API_TOKEN`. See "HTTP API" in
     [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)." Keep the existing line break.
  5. In the settings table:
     - delete the rows `EARMARK_VAULT_DIR`, `EARMARK_VAULT_SUBDIR`,
       `EARMARK_OBSIDIAN_VAULT_NAME`, `EARMARK_LLM_PROVIDER`, `EARMARK_STORE_BACKEND`,
       `EARMARK_INDEX_BACKEND` and `EARMARK_TZ`;
     - delete the whole `### Analysis` subsection, including its `docs/llm.md` link line;
     - delete the whole `### Index` subsection.
- **`docs/deploy.md`**. Only these edits:
  1. Delete the requirement bullet "- An Anthropic API key, or a local LLM
     ([docs/llm.md](llm.md))." (line 52).
  2. Replace the body of `## 8. Analysis` (keep the heading) with one paragraph: "Analysis
     was removed in step 22; earmark now only transcribes. Step 27 rewrites this guide."
  3. Replace the body of `## 14. Weekly digest` (keep the heading) with: "The weekly digest
     was removed in step 22. Step 27 rewrites this guide."

  Keep the section numbers: `test_deploy_doc_hf_telemetry_section` looks for
  `## 16.` and `## 17.`.
- **`docs/llm.md`**: deleted (section 3.3).
- **`docs/PLAN.md`, `docs/ARCHITECTURE.md`, `CLAUDE.md`, `docs/shortcut.md`,
  `docs/runbook-spark.md`, `docs/asr.md`, `.claude/`**: no change. The orchestrator
  updates the PLAN status table.

## 4. Interfaces

### 4.1 Models (`earmark.models`)

```python
Source = Literal["apple_podcasts"]
JobKind = Literal["ingest"]
JobStatus = Literal["queued", "running", "done", "failed", "skipped"]
Stage = Literal["resolve", "fetch", "transcribe", "notify"]  # "notify" is never set


class JobResult(Model):
    duration_s: Seconds  # Transcript.duration_s of the finished job
```

`Job` keeps every field it has: `id`, `kind`, `status`, `stage`, `url`, `prompt`,
`episode_key`, `attempts`, `run_after`, `error`, `warnings`, `result`, `shared_at`,
`created_at`, `updated_at` and `heartbeat_at`. Its validator raises
`"an ingest job requires url"` when `url is None`. `GET /jobs` and `GET /jobs/{id}` still
return the raw `Job`; step 24 adds the view model.

### 4.2 Database (`earmark.db`)

```python
DB_FILENAME: Final = "earmark.db"
APPLICATION_ID: Final = 0x45524D4B  # b"ERMK": marks the post-step-22 lineage
MIGRATIONS: Final[tuple[Migration, ...]]  # one entry, "0001_jobs" (below)
SCHEMA_VERSION: Final = len(MIGRATIONS)  # 1


class DatabaseSetupError(Exception): ...  # unchanged


class LegacyDatabaseError(DatabaseSetupError): ...  # v0.1.0 or older


class ForeignDatabaseError(DatabaseSetupError): ...  # another application's file


def identity_error(
    path: Path, application_id: int, user_version: int
) -> DatabaseSetupError | None: ...
```

`Database` gains two methods:

- `identity(self) -> tuple[int, int]`. Read-only: `(PRAGMA application_id, PRAGMA
  user_version)`.
- `check_identity(self) -> None`. Raises `identity_error(...)` when that returns an error.

`identity_error` rules:

| `application_id` | `user_version` | Result |
|---|---|---|
| `0` | `0` | `None`: a new or empty file. `migrate` adopts it. |
| `0` | `> 0` | `LegacyDatabaseError`: made by earmark v0.1.0, or a pre-release build of it |
| `APPLICATION_ID` | any | `None`; `migrate` then applies its existing newer-than-supported check |
| anything else | any | `ForeignDatabaseError` |

Messages. They are exact, apart from the interpolations, and stay well under doctor's
300-character cap for normal paths:

- Legacy: `f"{path} was created by earmark 0.1.0 (schema version {user_version}), which this
  version cannot use and does not migrate. Move it aside, with its -wal and -shm files if
  present, and start again."`
- Foreign: `f"{path} is not an earmark database (application id {application_id:#x}). Move
  it aside or set EARMARK_DATA_DIR to another directory."`

`migrate(migrations=MIGRATIONS)` works like this, inside its `BEGIN IMMEDIATE`:

1. Read both pragmas.
2. Raise `identity_error` if it returns an error.
3. Apply the existing `current > target` check.
4. Run the pending statements.
5. If `application_id == 0`, execute `PRAGMA application_id = {APPLICATION_ID}`.
6. Set `user_version`.

So even `migrations=()` stamps a fresh file.

`open_database` does `mkdir parents`, then `Database(...)`, then **`check_identity()`**,
then `ensure_wal()`, then `migrate()`. The check runs before `ensure_wal`, so a refused
database in rollback-journal mode is never switched to WAL.

The `0001_jobs` statements are v0.1.0's `0001_jobs` with one change: the kind CHECK is
`CHECK (kind IN ('ingest'))`. Every column and both indexes stay. The comment above
`MIGRATIONS` keeps "append only; never edit or reorder an entry that has shipped" and adds:
"The list restarted in step 22, with `APPLICATION_ID` marking the new lineage; databases
from before are refused (`identity_error`), never migrated."

### 4.3 Job store (`earmark.jobs`)

`JobStore.enqueue(url, *, prompt=None, shared_at=None) -> Enqueued` is unchanged.
`enqueue_reanalyze` is gone. Every other method is unchanged.
`complete(claim, result: JobResult | None = None)` is unchanged in signature, and the runner
always passes a result.

### 4.4 Artifacts (`earmark.artifacts`)

`ArtifactStore` keeps:

- `root`, `episode_dir`, `episode_path`, `share_path`;
- `transcript_json_path`, `transcript_text_path`, `audio_path`, `find_audio`,
  `delete_audio`;
- `write_episode`, `has_episode`, `read_episode`, `read_share`, `record_share`;
- `write_transcript`, `read_transcript`, `has_transcript`, `list_episode_keys`;
- the atomic-write helpers.

An `analyses/` directory left under an episode by v0.1.0 is ignored.

### 4.5 Runner (`earmark.pipeline.runner`)

`describe_error`, `Pipeline.__init__`, `Pipeline.notify`, `Pipeline.load_episode` and
`Pipeline._finish` are unchanged. `Pipeline.run` keeps its signature and outcome mapping.
Its work is:

```python
with interruptible():
    transcript = self._stage_work(claim, state)
done = self._jobs.complete(claim, JobResult(duration_s=transcript.duration_s))
```

`_stage_work(claim, state) -> Transcript`:

1. `PermanentError("ingest job without a url")` if `job.url is None`.
2. `set_stage("resolve")`, resolve, `write_episode`, `set_episode_key`, `record_share`.
   `record_share` is still called so that `shared.json` keeps the first share time;
   step 25 needs it. Its return value is unused.
3. If `transcript.json` exists, read it and log "transcript … is cached", with no
   `set_stage`. Otherwise `set_stage("fetch")`, fetch, `set_stage("transcribe")` and
   transcribe.
4. Raise `PermanentError("transcriber returned a transcript for another episode")` on a
   key mismatch.
5. Copy `transcript.warnings` into `job.warnings` (`_warn`).
6. Return the transcript.

Delete `_ingest_inputs`, `_stored_inputs` and `_analyze_store_index`. Imports lose
`Analysis`, `StoreResult` and `datetime`.

### 4.6 Factory (`earmark.pipeline.factory`)

- **Kept:**
  - `BuildContext(settings, artifacts, clock)`, unchanged;
  - `Builder`, `_lookup`;
  - `RESOLVERS`, `FETCHERS`, `TRANSCRIBERS`, `NOTIFIERS`;
  - `build_resolver`.
- **`build_backends(settings, artifacts, *, clock=utc_now) -> Backends`** looks up four
  registries. Any unbuilt name is a `ConfigError` naming its variable, raised before
  anything is built.
- **`build_worker(settings, *, jobs=None, clock=utc_now) -> Worker`** is unchanged, except
  for its log line: `"backends: resolver=%s fetch=%s asr=%s notify=%s"`.
- **Deleted:**
  - the builders `_anthropic_analyzer`, `_openai_compat_analyzer`, `_vault_store`,
    `_lancedb_indexer`, `_lancedb_searcher`, `_anthropic_themer` and
    `_openai_compat_themer`;
  - the registries `ANALYZERS`, `STORES`, `INDEXERS`, `SEARCHERS` and `THEMERS`;
  - `build_analyzer`, `build_themer`, `build_digest_writer`, `build_digest_notifier`,
    `build_searcher` and `build_note_locator`;
  - the top-level imports of `digest` and `index`, and the `TYPE_CHECKING` `NtfyClient`
    import.

### 4.7 Fakes (`earmark.fakes`)

- **Kept:** `FAKE_NOW`, `fixed_clock`, `FakeStage`, `ResolveCall`, `FetchCall`,
  `TranscribeCall`, `NotifyCall`, `FakeResolver`, `FakeAudioFetcher`, `FakeTranscriber`
  and `FakeNotifier`, all unchanged.
- **Deleted:**
  - `AnalyzeCall`, `StoreCall`, `IndexCall`;
  - `FakeAnalyzer`, `FakeStore`, `FakeThemer`, `FakeIndexer`, `FakeEmbedder`,
    `FakeSearcher`;
  - their imports: `digest.*`, `index.*`, `vault.document`, `math`, `re`,
    `PurePosixPath`, `Literal`, `Mapping`, if they are no longer needed.

```python
@dataclass
class FakeBackends:
    artifacts: ArtifactStore
    resolver: FakeResolver
    fetcher: FakeAudioFetcher
    transcriber: FakeTranscriber
    notifier: FakeNotifier

    def backends(self) -> Backends: ...


def make_fake_backends(data_dir: Path) -> FakeBackends: ...
```

### 4.8 CLI (`earmark.cli`)

The registered commands are exactly `config`, `doctor`, `serve`, `worker`, `submit`,
`jobs`, `resolve` and `transcribe`. There are no Typer groups, and `--version` stays.

- **Deleted:**
  - `entities_app` and its `add_typer`;
  - the commands `reanalyze`, `episodes`, `analyze`, `digest`, `entities rebuild`,
    `search` and `reindex`;
  - `PROMPT_SHOWN_CHARS`, `_episode_lines`, `_digest_now`;
  - the `TYPE_CHECKING` import and the `datetime` import.
- **`serve`:** the searcher block goes. It runs
  `api = create_app(settings)` inside the same `ApiTokenError` (exit 2) and
  `(DatabaseSetupError, sqlite3.Error, OSError)` (exit 1,
  `"earmark serve: cannot open the job store: {exc}"`) handlers, then `uvicorn.run(...)`
  as now.
- **App help and `main` docstring:** "Share a podcast episode, get its transcript."
- **Other commands:** `worker`, `submit` and `jobs` are unchanged. Their existing
  `DatabaseSetupError` handlers print the legacy message (exit 1).

### 4.9 Settings (`earmark.config`)

The `Settings` fields are exactly these, in this order, with defaults and descriptions
unchanged:

- `api_token`, `host`, `port`, `data_dir`;
- `resolver_backend`, `fetch_backend`, `asr_backend`, `notify_backend`;
- `asr_model`, `asr_device`, `asr_compute_type`, `asr_language`, `asr_model_dir`;
- `asr_base_url`, `asr_api_key`, `asr_max_upload_mb`, `asr_timeout_seconds`;
- `keep_audio`;
- `ntfy_url`, `ntfy_token`;
- `max_audio_mb`, `max_feed_mb`, `allow_private_fetch`;
- `job_max_attempts`, `job_lease_seconds`, `worker_poll_seconds`;
- `log_level`.

That is 27 fields.

- **Deleted fields:** `vault_dir`, `vault_subdir`, `obsidian_vault_name`, `llm_provider`,
  `store_backend`, `index_backend`, `llm_model`, `llm_effort`, `llm_fallbacks`,
  `llm_base_url`, `llm_api_key`, `llm_context_tokens`, `llm_timeout_seconds`,
  `llm_response_format`, `embed_model`, `embed_model_dir` and `tz`.
- **Deleted literals and constants:** `LlmProvider`, `StoreBackend`, `IndexBackend`,
  `LlmEffort`, `LlmResponseFormat`, `OBSIDIAN_NAME_MAX`.
- **Deleted validators:** `_check_vault_subdir`, `_check_tz`,
  `_check_obsidian_vault_name`.
- **Narrowed validators:**
  - `_check_model_name`: `"asr_model"` only;
  - `_lower_asr_choice`: `"asr_device"` and `"asr_compute_type"`;
  - `_check_base_url`: `"asr_base_url"` only;
  - `_check_api_key`: `"asr_api_key"` and `"ntfy_token"`.
- **Imports:** `zoneinfo`, `unicodedata` and `PurePosixPath` go if they are unused.

`secret_values` and `redacted_items` are unchanged.

### 4.10 ntfy (`earmark.notify.ntfy`)

```python
TITLE_PREFIX: Final[Mapping[JobStatus, str]] = {
    "done": "Transcribed",
    "failed": "Failed",
    "skipped": "Skipped",
}


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    tags: tuple[str, ...]
    priority: int

    def payload(self, topic: str) -> dict[str, object]: ...  # no "click" key ever


def compose(
    job: Job, episode: Episode | None, *, secrets: Sequence[str] = ()
) -> Notification | None: ...
```

- **Deleted:**
  - `REANALYZE_TITLE_PREFIX`, `DIGEST_TAG`, `DEFAULT_ANALYSIS_LINE`, `PROMPT_MAX_BYTES`,
    `MAX_CLICK_CHARS`;
  - `obsidian_url`, `compose_digest`;
  - `Notification.click`, `NtfyOptions.vault_name`, and `quote` from the imports.
- **Kept, unchanged:** `TAGS`, `UNKNOWN_EPISODE`, `UNKNOWN_SHOW`, the byte caps,
  `clean_field`, `split_topic_url`, `NtfyClient`, `NtfyHealth`, `NtfyAccount`,
  `NtfyAuthError`, `NtfyNotifier` and `build_ntfy_notifier`.
- **Edits:**
  - the import becomes `from earmark.sanitize import clean_unicode, fit_bytes`;
  - the module docstring becomes "title, message, tags, priority" with no link, and
    "like the remote ASR client".

`compose`, line by line. Every untrusted field goes through
`clean_field(redact(text, secrets), cap)` as now.

- **Title:** `f"{TITLE_PREFIX[status]}: {name or UNKNOWN_EPISODE}"`, then `fit_bytes` to
  `TITLE_MAX_BYTES`.
- **`done`:**
  1. `show or UNKNOWN_SHOW`.
  2. `f"Duration: {format_hms(job.result.duration_s)}"` when `job.result` is not `None`.
  3. The warnings line as now.
- **`failed`:**
  1. `show or UNKNOWN_SHOW`. **`job.url` is no longer used.**
  2. `Stage: …` when set.
  3. `Reason: …`.
  4. `Gave up after N attempts.` when `attempts > 1`.
- **`skipped`:** `show or UNKNOWN_SHOW`, then `Reason: …`.
- **Nowhere:** no line uses `job.prompt`, `job.url`, `episode.url`, `episode.audio_url`
  or a path.
- **`queued` or `running`:** `None`.

`NtfyNotifier.notify` calls `compose(job, episode, secrets=options.secrets)`.

### 4.11 Doctor

`OfflineChecks.run()` returns, in order:

1. `api token`
2. `data dir`
3. `database`
4. `resolver`
5. `fetch`
6. `asr`
7. `asr libraries` and `asr model`, only for `faster_whisper` with an ok `asr`
8. `notify`

Deleted from `checks.py`:

- `vault`, `obsidian_link`, `llm`, `index`, `embed_model`, `search_index`;
- `self.anthropic`, `_index`, `_index_info`.

`database()` reads `PRAGMA application_id` with the other pragmas. Right after the
`sqlite3.Error` handler, `identity_error(path, app_id, version)` runs. If it returns an
error, the result is `CheckResult("database", "fail", str(error))`. The rest is
unchanged, with `SCHEMA_VERSION` now 1. The check still opens through
`Database.connect()`: no migration, and no WAL switch.

`run_online` keeps `apple lookup`, `asr server` (remote only) and `ntfy server`/`ntfy token`.

### 4.12 Deploy tests (`tests/test_deploy.py`)

- `test_dockerfile_environment`. The expected values are `EARMARK_HOST=0.0.0.0`,
  `EARMARK_DATA_DIR=/data` and `HF_HOME=/data/huggingface`. It also asserts that the set of
  `EARMARK_*` names set by `ENV` in the runtime stage is exactly
  `{"EARMARK_HOST", "EARMARK_DATA_DIR"}`.
- `test_dockerfile_default_extras_exist` also asserts `default == "transcribe"`.
- New `test_dockerfile_runtime_dirs`. The runtime `RUN` logical line contains
  `install -d -o earmark -g earmark -m 0750 /data` as its last command, so the line ends
  with that text.
- `test_compose_shared_data_and_vault` becomes `test_compose_shared_data`. For `api` and
  `worker`, `volumes == ["data:/data"]`, and `set(COMPOSE["volumes"]) == {"data",
  "asr-models"}`.
- `test_compose_forces_container_paths`:
  - `set(environment) == {"EARMARK_DATA_DIR", "EARMARK_HOST", "EARMARK_PORT",
    "EARMARK_ASR_MODEL_DIR", "HF_HUB_DISABLE_TELEMETRY"}`;
  - the existing value checks, minus the two removed keys.
- New `test_compose_builds_transcribe_extra`:
  `SERVICES["api"]["build"]["args"]["EXTRAS"] == "${EXTRAS:-transcribe}"`.
- `test_compose_only_variables_documented`. The expected set loses `VAULT_PATH`. The
  deploy-doc and `.env.example` checks stay; `docs/deploy.md` still documents the others.
- `test_systemd_units`. For both roles, `write_paths == ["/srv/earmark/data"]`, replacing
  the worker's `len(write_paths) >= 2`.
- Delete `test_digest_service_unit` and `test_digest_timer_unit`.
- `test_systemd_service_files_are_the_known_three` becomes `…_known_two`, with
  `{"earmark-api.service", "earmark-worker.service"}`.
- New `test_systemd_dir_has_only_the_two_services`:
  `{p.name for p in (ROOT / "deploy" / "systemd").iterdir()} == {"earmark-api.service",
  "earmark-worker.service"}`.

## 5. Behaviour and edge cases

### 5.1 Telling the old database apart

v0.1.0 never set `application_id`, so every database it made reads `(0, 2)`. Dev
databases from steps 03–12 read `(0, 1)`. Both are refused as legacy. New databases are
stamped in the same transaction that creates `jobs`, so a crash cannot leave a half-stamped
file. `(0, 0)` is an empty or new file, and is adopted. This stays unambiguous when steps 24
and 25 add migrations 2 and 3, because their databases carry `APPLICATION_ID`.

### 5.2 Refusal touches nothing

- `open_database` checks identity before `ensure_wal` and before `migrate`.
- `migrate` re-checks inside its write transaction. This matters for two processes racing
  on first open, or a file replaced between the check and the migration.
- After a refusal, `user_version` and `application_id` are unchanged, the journal mode is
  unchanged (including a rollback-journal file), and the three job rows are still there.
- The CLI prints `earmark <cmd>: cannot open the job store: <message>` to stderr and
  exits 1. `serve` never reaches `uvicorn.run`.

### 5.3 What still opens the database

- Through `open_job_store`, and so refused: `serve` (`create_app`), `worker`
  (`build_worker`), `submit` and `jobs`.
- Without opening it: `config`, `resolve` and `transcribe`.
- `doctor` reads it without migrating, and reports `FAIL database <message>`.

### 5.4 The interim push

The push says "Transcribed", with the episode title, the show, `Duration: HH:MM:SS`, and a
warnings count. The duration is from the transcript, so it is right on a cache hit too.

- **Failures:** pushes no longer fall back to `job.url` when the episode is unknown; they
  say "Apple Podcasts episode".
- **No prompt:** the prompt line is gone for every status.
- **No link:** no `click` is ever sent.
- **Reasons:** `Reason:` text is `job.error`, which `describe_error` already writes. For
  non-earmark exceptions it is URL-scrubbed (`log.scrub_urls`). Host-only rules for direct
  URLs are step 24's.

### 5.5 Stages

- **Fresh job:** `job.stage` ends at `"transcribe"`.
- **Re-share of a transcribed episode:** it ends at `"resolve"`, because fetch and
  transcribe are skipped without `set_stage`, as today.
- **`notify`:** still never set as a stage.

### 5.6 `prompt` stays, and does nothing

`IngestRequest.prompt`, the `Job.prompt` column and its part of the dedupe key all stay, so
the Shortcut and its doc tests keep working until step 24 replaces the field with
`instructions`. Nothing reads the prompt after enqueue, except `GET /jobs`, which returns
the raw job until step 24. A re-share with a new prompt makes a new job that reuses the
cached transcript.

### 5.7 `GET /episodes` is removed (the planner's decision)

**Removed**, with `earmark episodes` and `episodes.py`. The reasons:

- It was the way to find an episode key for the re-run; the re-run is gone.
- Half of what it showed was the analyses list.
- Step 24 adds uploads and direct URLs keyed by a content hash, where "episode, show,
  published" does not fit. Keeping the route would mean redesigning it there, and step
  24's leak rules and view model only cover `/jobs`.
- `GET /jobs` already lists every request, with its episode key and status. Fewer
  endpoints means less untrusted text served.
- It is recoverable from the tag.

### 5.8 `EARMARK_TZ` and `tzdata` are removed (the planner's decision)

`tz` fed the vault note dates, the `earmark episodes` dates and the digest week windows,
and nothing else. `tzdata` was a core dependency only so that `zoneinfo` works in the slim
image. Both go. The step-25 contract dates use the requester's own UTC offset, or the
publish date, so no zone database is needed. A new zone setting, if one is ever wanted,
belongs to that step.

### 5.9 Left on disk

`data/episodes/<key>/analyses/`, `data/lancedb`, `data/models/embed` and `data/digests`
from an old install are ignored. Nothing reads or deletes them, and step 27's upgrade notes
cover the cleanup. The vault is never touched, because no code can reach it.

### 5.10 `Settings` and unknown variables

`extra="ignore"` stays, so a leftover `EARMARK_VAULT_DIR` or `EARMARK_LLM_*` in an
operator's `.env` is silently ignored. The README quickstart still exports them, and it
must keep working. Settings are read without erroring on unknown keys, as now; the
existing `test_unknown_earmark_key_ignored` covers this.

### 5.11 Test configuration

Removing the `llm` and `index` markers, the `ANTHROPIC_*` isolation and
`FASTEMBED_CACHE_PATH` from `tests/conftest.py` is **authorised by the brief**. It removes
configuration for code that no longer exists and is not a loosening.

- **Unchanged:** `--disable-socket`, `--allow-unix-socket`, `strict`,
  `filterwarnings = error`, the `network`, `slow` and `gpu` markers, socket enabling only
  for `network` and `slow`, and `HF_HUB_OFFLINE=1` for the rest.
- **Changed text:** the `slow` description gains "or needs the transcribe extra", because
  the library-logging tests move under it.

## 6. Tests

### 6.1 Acceptance criteria

| # | Criterion (brief) | Proof |
|---|---|---|
| A1 | `make check` and `GITHUB_ACTIONS=true make check` pass | Section 7.1, both commands |
| A2 | Nothing outside `docs/` and `CLAUDE.md` refers to a removed module, setting, route, command, marker or dependency | The grep in section 7.2 prints nothing. Exact-set tests back it: `test_settings_fields_are_exactly`, `test_routes_are_exactly_the_kept_ones`, `test_cli_commands_are_exactly`, `test_workflow_files_are_the_known_three`, `test_systemd_dir_has_only_the_two_services`, `test_live_markers_registered` |
| A3 | `uv.lock` has no `anthropic`, `lancedb` or `fastembed`; PyYAML is dev-only | Section 7.4 |
| A4 | A job runs resolve, fetch, transcribe and notify on fakes. It ends `done` with the transcript cached, and the push carries no URL | `test_e2e.py::test_ingest_then_run_once` (below); `test_ntfy.py::test_pipeline_ntfy_success_no_warning` (extended, section 6.6); `test_runner.py::test_stage_order_and_inputs`; `test_runner.py::test_reshare_reuses_transcript` |
| A5 | A v0.1.0 fixture database with a done ingest job, a done reanalyze job and a queued reanalyze job is refused by `serve`, `worker` and `earmark jobs`. The message names the path, and a fresh database works | `test_cli.py::test_v010_database_refused_by_commands[serve\|worker\|jobs\|submit]`, `test_cli.py::test_fresh_database_after_moving_v010_aside`, `test_db.py::test_v010_database_refused`, `test_db.py::test_fresh_database_identity` |
| A6 | `GET /episodes` is kept or removed, with the reason recorded | Removed (section 5.7). `test_routes_are_exactly_the_kept_ones`, `test_docs.py::test_readme_http_api_lists_every_route` |

`test_e2e.py::test_ingest_then_run_once` is rewritten:

1. POST `/ingest` with a prompt, then run `worker.run_once()`.
2. Assert the job is `done`, with stage `"transcribe"`, no error, and
   `result == JobResult(duration_s=900.0)`. The fake episode has `duration_s=900`, and
   the fake segments end at 15 s.
3. Assert `episode.json`, `shared.json`, `transcript.json` and `transcript.txt` exist,
   and that the episode directory holds nothing else apart from the fake `audio.mp3`.
4. Assert the resolver was called once with `CANON`.
5. Assert the fetcher and transcriber each computed once.
6. Assert one notify call, with the done job and the episode.
7. Run `compose(notify_call.job, notify_call.episode)`, and assert:
   - the title is `"Transcribed: " + episode.title`;
   - `"Duration: 00:15:00"` is in the message;
   - no `"://"`, `"podcasts.apple.com"` or the prompt text appears in the title or
     message.

`test_run_once_empty_queue` is unchanged.

### 6.2 Database identity (`tests/test_db.py`)

The fixture comes from `tests/legacy_db.make_v010_database(tmp_path / "earmark.db")`.

- **`test_v010_database_refused`:**
  - `open_database(path)` raises `LegacyDatabaseError`;
  - `str(path)` and `"Move it aside"` are in the message;
  - afterwards a raw `sqlite3` connection reads `user_version == 2`,
    `application_id == 0`, `journal_mode == "wal"`, and
    `SELECT count(*) FROM jobs == V010_JOB_COUNT`.
- **`test_v010_rollback_journal_not_switched_to_wal`:**
  `make_v010_database(path, wal=False)`, refused, and afterwards `journal_mode` is still
  `"delete"`.
- **`test_pre_rescope_dev_database_refused`:** a file with one table and
  `user_version = 1`, without `application_id`, raises `LegacyDatabaseError`.
- **`test_foreign_application_id_refused`:** `application_id = 0x12345678`, then
  `ForeignDatabaseError`. `"0x12345678"` and the path are in the message.
- **`test_fresh_database_identity`:**
  - `open_database` on a new path reads `application_id == APPLICATION_ID` and
    `user_version == SCHEMA_VERSION == 1`;
  - the `sqlite_master` table names are `{"jobs"}`;
  - re-opening runs no migration (existing pattern).
- **`test_empty_migrations_still_stamp`:** `open_database(path, migrations=())` reads
  `application_id == APPLICATION_ID` and `user_version == 0`, and a later default open
  migrates it to 1.
- **`test_identity_error_table`:** parametrized over the four rows of section 4.2, against
  `identity_error` directly.

The existing tests stay. `test_newer_schema_refused` still passes, because the file is
stamped first.

### 6.3 CLI (`tests/test_cli.py`)

**Delete:**

- the `analyze` section, `test_analyze_*` (lines 748–917);
- the `entities` section (919–966);
- the `search` section (968–1097);
- the `reindex` section (1098–1170);
- `test_serve_starts_when_search_unavailable`, `test_serve_with_index_none` and
  `test_serve_wires_the_searcher`;
- the `reanalyze` and `episodes` sections (1318–1471);
- the `digest` section (1472–1724).

Drop the imports these used: `FakeSearcher`, `FakeStore`, `IndexExtraMissing`,
`IndexMismatchError`, `SearchQuery`, `put_episode`, `make_analysis`, `index_helpers`,
`make_hit`, `MAX_PROMPT_CHARS` only if it is now unused, and `datetime`/`UTC` only if
they are now unused.

**Change:**

- `BACKEND_VARS` lists `RESOLVER_BACKEND`, `FETCH_BACKEND`, `ASR_BACKEND` and
  `NOTIFY_BACKEND`.
- `fake_env` no longer sets `EARMARK_VAULT_DIR`.
- `doctor_env` no longer makes a vault directory or sets `EARMARK_INDEX_BACKEND`.
  `test_doctor_all_fake_exit_0` drops its vault assertion.
- `test_doctor_never_prints_secrets` drops `EARMARK_LLM_API_KEY` and the
  `EARMARK_LLM_*` and `EARMARK_LLM_PROVIDER` variables.
- `test_import_earmark_is_light` drops `lancedb`, `fastembed` and `anthropic` from its
  heavy set.
- `test_submit_then_worker_once` replaces its vault-glob assertion with: exactly one
  `data/episodes/*/transcript.json` exists.
- `test_jobs_text_output` expects `["done", "transcribe", "1"]`.
- `test_serve_access_log_drops_search_query` becomes `test_serve_access_log_drops_query`.
  It uses the URL `/jobs?limit=3&private-sentinel=1`, and expects 200 with the token and
  401 without. It asserts `'"GET /jobs HTTP/1.1" 200'` and `'"GET /jobs HTTP/1.1" 401'` in
  the output, with neither `"private-sentinel"` nor `"limit="`. It does not set
  `EARMARK_INDEX_BACKEND`.

**Add:**

- `test_cli_commands_are_exactly`. The registered command names are `{"config", "doctor",
  "serve", "worker", "submit", "jobs", "resolve", "transcribe"}`, and
  `app.registered_groups == []`.
- `test_v010_database_refused_by_commands`, parametrized over `["serve"]`,
  `["worker", "--once"]`, `["jobs"]` and `["submit", SHARE]`:
  - `make_v010_database(fake_env / "data" / "earmark.db")`;
  - set `EARMARK_API_TOKEN` (for serve) and use `fake_uvicorn` there;
  - assert exit 1, with `str(db path)` and `"Move it aside"` in stderr;
  - for serve, `fake_uvicorn.calls == []`;
  - afterwards `user_version == V010_SCHEMA_VERSION` and the job count is unchanged.
- `test_fresh_database_after_moving_v010_aside`. Build the fixture, then rename it,
  together with any `-wal` and `-shm` files. `earmark jobs` then prints `"no jobs\n"`
  (exit 0), and the new file carries `APPLICATION_ID`.

### 6.4 Doctor (`tests/test_doctor.py`)

**Delete:**

- `LLM_URL`;
- the vault tests (lines 172–242);
- `test_doctor_index_extra_missing_fails`;
- `MemoryTable`, `MemoryDb` and the index, embed and search-index tests (479–566);
- the Anthropic and openai_compat offline tests (568–612);
- the obsidian tests (663–679);
- the `test_online_anthropic_*` tests (1013–1087);
- the imports of `anthropic_stub` and `index_helpers`.

**Change:**

- `FAKES` drops `llm_provider`, `store_backend` and `index_backend`. `make_settings` no
  longer makes or passes a vault.
- The `kind` fixture (line 913) has only the `"asr"` parameter, or becomes a constant.
  Every `test_online_models_*` test keeps running for the ASR server.
- `test_doctor_unselected_backends_not_imported`: hide `faster_whisper` only, with remote
  ASR. No fails apart from `api token`, and its `index` assertion goes.
- `test_doctor_fakes_and_http_fetch` checks the names `("resolver", "fetch", "asr",
  "notify")`.
- `test_offline_makes_no_requests` drops `anthropic_http_client`, `llm_provider` and
  `llm_api_key`, and expects the names `{"asr", "notify", "resolver"}`.
- `test_offline_creates_nothing` makes only `data/`, and uses default settings.
- `test_doctor_database_older_warns_and_is_not_migrated` uses `migrations=()` and expects
  "schema version 0".
- `test_online_timeouts_are_10s`, `test_online_skipped_after_offline_failure` and
  `test_online_checks_only_for_selected_backends` drop their LLM and Anthropic parts.

**Add:**

- `test_doctor_check_names_in_order`. With all fakes and notify `none`, the names in
  order are `["api token", "data dir", "database", "resolver", "fetch", "asr", "notify"]`.
- `test_doctor_database_v010_fails`. With `make_v010_database(data/earmark.db)`, the
  `database` check is `fail`, its detail contains `"Move it aside"` and `"0.1.0"`, and the
  file still reads `user_version == 2` with `application_id == 0`.

### 6.5 Factory (`tests/test_factory.py`)

**Keep and adapt:**

- `test_unbuilt_backend_name_names_setting` (four registries),
  `test_every_default_backend_is_built`, `test_ntfy_without_url_builds_none`,
  `test_ntfy_with_url_builds_notifier` and `test_all_fake_builds_fakes`;
- `test_registries_match_settings`: `RESOLVERS`, `FETCHERS`, `TRANSCRIBERS` and
  `NOTIFIERS` against their Literals;
- `test_build_worker_runs_a_job`, which asserts `done` and `transcript.json` cached;
- `test_build_worker_config_error_touches_nothing`;
- `test_pipeline_modules_import_light`, with the heavy set
  `{'fastapi', 'starlette', 'uvicorn', 'httpx2', 'faster_whisper', 'onnxruntime', 'numpy'}`;
- `test_fetch_module_not_imported_by_factory`, `test_factory_builds_apple_resolver`,
  `test_build_backends_uses_apple_resolver`, `test_resolver_modules_not_imported_by_factory`,
  `test_build_resolver_only_builds_resolver` and `test_build_resolver_unbuilt_name`.

**Delete everything else:**

- the fake-analyzer clock test, and the vault-store tests (`test_vault_store_*`);
- `test_build_worker_runs_a_job_with_vault`;
- the Anthropic, `build_analyzer`, lancedb, searcher and note-locator tests;
- the openai_compat, themer, digest-writer and digest-notifier tests;
- the `index_helpers` import.

### 6.6 ntfy (`tests/test_ntfy.py`)

**Delete:**

- `test_obsidian_url_golden`, `test_obsidian_url_encodes_reserved` and
  `test_obsidian_url_refuses_hash_and_overlong`;
- `test_compose_done_click_with_vault_name`,
  `test_compose_done_without_vault_name_has_note_line`,
  `test_compose_done_hash_in_path_falls_back_to_note_line` and
  `test_notifier_click_in_request_body`;
- `test_compose_reanalyze_done`, `test_compose_reanalyze_default_prompt`,
  `test_compose_reanalyze_failed_without_episode` and `test_compose_ingest_unchanged_for_kind`;
- `test_compose_digest_*`.

**Change:**

- Every `compose(..., vault_name=…)` call loses the keyword.
- `test_compose_titles_distinct_per_status` expects `"Transcribed: "`.
- `test_compose_done_has_no_reason_and_counts_warnings`: no `"Prompt:"` even with a
  prompt set, and `"Duration: 00:02:05"` present for `make_job()`'s result.
- `test_compose_failed_and_skipped_have_no_click` becomes
  `test_payload_never_has_click`, for all three statuses.
- `test_compose_unknown_episode`: the first message line is `"Apple Podcasts episode"`,
  and `job.url` is not in the message.
- `test_payload_shape` has no `click` key.
- `test_pipeline_ntfy_failure_is_warning` parametrizes over `["transcribe", None]`, and
  fails the fake transcriber with `PermanentError("bad audio")`.
- `test_pipeline_ntfy_success_no_warning` (**A4**): the published JSON title starts with
  `"Transcribed: Fake episode "`, `"Duration: 00:15:00"` is in its message, and the
  request body bytes contain no `b"://"`, no `b"podcasts.apple.com"` and no `b"click"`.
  The job is enqueued with `prompt="secret-prompt-7q"`, and that string is absent from the
  body.

**Add:**

- `test_compose_never_includes_url_prompt_or_path`, parametrized over done, failed and
  skipped, with and without an episode. Use a job with `url=CANON` and
  `prompt="private-question-3k"`, and the episode from `make_episode()`. Neither
  `"://"`, the prompt, `job.url` nor `episode.audio_url` is in the title or message.
- `test_compose_done_without_result_has_no_duration_line`.

### 6.7 Runner (`tests/test_runner.py`)

**Delete:**

- `test_new_prompt_runs_only_analyzer`;
- `test_index_failure_is_warning`, `test_index_transient_is_warning` and
  `test_index_none_skips_index`;
- `test_analysis_not_written_still_stored`;
- every `test_reanalyze_*`.

**Change:**

- `test_same_prompt_reuses_everything` becomes `test_reshare_reuses_transcript`. Two
  ingests of `CANON`, the second after the first is `done`, end with the fetcher and
  transcriber each computed once and the transcriber called once. Both jobs are `done`,
  with the same `JobResult`.
- `test_stage_order_and_inputs` expects stages `["resolve", "fetch", "transcribe"]` and
  `job.stage == "transcribe"`, and the transcribe call received the fetched audio.
- `test_stage_order_cached_transcript` expects `["resolve"]`.
- `test_stage_warnings_copied` uses transcriber warnings only, and expects
  `("no segments",)` on both runs.
- `test_shared_at_is_first_share` asserts
  `env.fakes.artifacts.read_share(key).first_shared_at`. It is `T0` after the second job,
  and `earlier` after the third, the one with an earlier `shared_at`.
- `test_notifier_runs_for_each_terminal_status` and `test_notifier_none`: mechanical.
- Any test that failed the analyzer to reach a failure path fails the transcriber
  instead.

**Add:** `test_done_result_is_transcript_duration`. A
`FakeTranscriber` with segments ending at 42.0, and an episode with `duration_s=None` (a
`FakeResolver` mapping), gives `JobResult(duration_s=42.0)`.

### 6.8 Tooling (`tests/test_tooling.py`)

- `test_live_markers_registered` checks `("network", "slow", "gpu")`, and also asserts that
  no other marker line exists: the registered names are exactly those three.
- `_INNER_MARKERS` keeps plain, network, slow and gpu. Both deselection tests expect
  `passed=1, deselected=3`.
- `test_index_marker_keeps_sockets_blocked` becomes `test_gpu_marker_keeps_sockets_blocked`,
  with a `@pytest.mark.gpu` inner test that opens an `AF_INET` socket, run with `-m gpu`,
  and expected to fail with `SocketBlockedError`. This keeps the "only `network` and
  `slow` get sockets" guard covered.
- Delete `_INNER_ENV` and `test_anthropic_env_isolated`.

### 6.9 Criteria added by the planner

| # | Criterion | Proof |
|---|---|---|
| P1 | The sanitizers moved intact and stdlib-only | `tests/test_sanitize.py` (section 3.4), `test_sanitize_is_stdlib_only` |
| P2 | The fixture's DDL is the tag's DDL | Section 7.3 |
| P3 | A refused database is not modified (journal mode included) | `test_v010_database_refused`, `test_v010_rollback_journal_not_switched_to_wal`, `test_doctor_database_v010_fails` |
| P4 | The push never carries a URL, prompt or path, for any status | `test_compose_never_includes_url_prompt_or_path` |
| P5 | The hf_hub/httpx URL-scrubbing tests survive under `slow` | `uv run --locked pytest -m slow --collect-only -q` lists `tests/test_log_libraries.py::test_hf_download_logs_no_urls`, `::test_hf_hint_logged_once` and `::test_httpx_httpcore_over_uds_log_no_paths`. They run in the weekly `slow` job; see section 7.5 |
| P6 | The image builds with only the transcribe extra | `test_compose_builds_transcribe_extra`, `test_dockerfile_default_extras_exist`, `test_container_workflow_checks_image` (static). The real build runs in `container.yml` on push |

No fixtures need downloading. The v0.1.0 fixture is generated by `tests/legacy_db.py` at
test time.

## 7. Verification

### 7.1 The gate

```bash
uv lock --offline --check   # must succeed: uv.lock matches pyproject.toml
uv sync                     # exact: prunes anthropic, tzdata etc. from .venv
make check
GITHUB_ACTIONS=true make check
uv run --locked pytest -m "network or slow or gpu" --collect-only -q   # imports only
```

**Do not run** `pytest -m network`: it includes `test_ntfy_live.py`, which publishes to
public ntfy.sh. **Do not install extras into the repo `.venv`.**

### 7.2 Nothing refers to a removed thing

Run from the repo root. **Both commands must print nothing.** `git grep` exits 1 when
nothing matches.

```bash
cd /home/sandy/dev/earmark
SCOPE=(src tests .github deploy compose.yaml Dockerfile .dockerignore .env.example
       pyproject.toml Makefile ':!tests/fixtures/' ':!tests/legacy_db.py')
I='anthropic|lancedb|fastembed|obsidian|vault|reanaly[sz]|tzdata|zoneinfo|earmark-digest|digests/|aliases\.yaml|docs/llm\.md|(llm|index)\.yml|--extra index|claude|\banaly[sz]|\bllm|map.?reduce|embedd(ing|er)|embed_'
C='earmark[./](analyze|vault|index|digest|episodes)\b|EARMARK_(VAULT|OBSIDIAN|LLM_|STORE_BACKEND|INDEX_BACKEND|EMBED_|TZ\b)|\b(store_backend|index_backend|note_path|transcript_note_path)\b|settings\.tz\b|"tz"|["'"'"']/episodes|GET /episodes|/episodes/\{|/search\b|/analyze\b|\bearmark (analyze|episodes|digest|entities|search|reindex)\b|\["(analyze|episodes|digest|entities|search|reindex)"|command\("(analyze|episodes|digest|rebuild|search|reindex)"|entities_app|"(analyze|store|index|digest)"|mark\.(llm|index)\b|-m (llm|index)\b|not (llm|index)\b|"(llm|index): |\b(StoreResult|Indexer|Searcher|Search(Hit|Query|Results|Response)|Themer|NeedsChunking|prompt_hash|PromptHash|entity_key|entity_id|Episode(Page|Info)|list_episodes|compose_digest|NotePaths|Digest[A-Z][a-zA-Z]*|DIGEST_[A-Z_]+|ResearchLead|Entity)\b'
git grep --untracked -n -i -E "$I" -- "${SCOPE[@]}"
git grep --untracked -n -E "$C" -- "${SCOPE[@]}"
```

The scope is the brief's "source, test, workflow, deploy file or `.env.example`", plus
`pyproject.toml` (markers and dependencies) and the `Makefile`. These are excluded, and
why:

- `docs/` and `CLAUDE.md`, by the brief;
- `README.md`, which is a prose doc for step 27;
- `uv.lock`, checked in section 7.4;
- `tests/fixtures/`: recorded third-party feeds and pages, which mention "Anthropic" in
  episode descriptions and `search.yahoo.com` in RSS namespaces;
- `tests/legacy_db.py`: the v0.1.0 fixture, which must spell the old job kind and tables.

`--untracked` includes files the implementer created but has not committed.

The planner ran these patterns over the current tree, excluding the files this plan
deletes. Every hit was a real reference this plan removes or rewrites. The only false
positives were in `tests/fixtures/`, which are now excluded.

### 7.3 The fixture DDL matches the tag

```bash
cd /home/sandy/dev/earmark
S=/tmp/claude-501/-home-sandy-dev-earmark/88372f9b-11a6-42b3-950d-76488ef69465/scratchpad
mkdir -p "$S" && git show v0.1.0-full-pipeline:src/earmark/db.py > "$S/v010_db.py"
uv run --locked python - "$S/v010_db.py" <<'EOF'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("v010_db", sys.argv[1])
assert spec is not None and spec.loader is not None
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
from tests.legacy_db import V010_SCHEMA_VERSION, V010_STATEMENTS

tag = [" ".join(s.split()) for m in old.MIGRATIONS for s in m.statements]
ours = [" ".join(s.split()) for s in V010_STATEMENTS]
assert tag == ours, "fixture DDL differs from v0.1.0"
assert len(old.MIGRATIONS) == V010_SCHEMA_VERSION
print("fixture DDL matches v0.1.0:", len(ours), "statements")
EOF
rm "$S/v010_db.py"
```

The verifier's scratchpad path may differ. Any temp directory works, since the file is
deleted afterwards.

### 7.4 The lock

```bash
cd /home/sandy/dev/earmark
grep -nE '^name = "(anthropic|lancedb|fastembed|tzdata)"' uv.lock   # must print nothing
uv run --locked python - <<'EOF'
import tomllib

lock = tomllib.load(open("uv.lock", "rb"))
[me] = [p for p in lock["package"] if p["name"] == "earmark"]
core = {d["name"] for d in me["dependencies"]}
extras = set(me.get("optional-dependencies", {}))
dev = {d["name"] for d in me["dev-dependencies"]["dev"]}
assert "pyyaml" not in core and "pyyaml" in dev, (core, dev)
assert extras == {"transcribe"}, extras
assert not core & {"anthropic", "tzdata"}, core
print("ok: core", sorted(core), "extras", sorted(extras))
EOF
```

`pyyaml` itself stays in `uv.lock` as a dependency of `ctranslate2`. That is expected
(section 2.1).

### 7.5 What cannot be verified here

- The container image build with `EXTRAS=transcribe` (`container.yml`), because there is
  no Docker here. It runs on push.
- The `slow` job: the moved library-logging tests and the Whisper tests with the
  transcribe extra. It is weekly or on demand. The orchestrator may dispatch
  `network.yml` by hand after the push. The network job in that run publishes to ntfy.sh
  as it always has, which is the existing behaviour and the operator's call.
- Nothing on the GPU path changes.

### 7.6 Manual spot checks for the verifier

- `uv run earmark --help` lists only the eight commands.
- With `EARMARK_*_BACKEND=fake` for the four backends and a temp `EARMARK_DATA_DIR`, run
  `uv run earmark submit '<apple url>' && uv run earmark worker --once`. It prints
  `job <id> done`, and `uv run earmark jobs` shows stage `transcribe`.
- `git status` shows the deletions in sections 3.1–3.3, the new files in section 3.4, and
  no change under `docs/` except `docs/llm.md` (deleted), `docs/deploy.md`, and this plan.

## 8. Deviations

1. **`docs/ARCHITECTURE.md` is not edited.** The brief gives it to step 23. Step 23 must
   fold in the contract changes made here:
   - the four-stage `Backends`;
   - `JobResult(duration_s)` and `JobKind = "ingest"`;
   - the `Stage` literal;
   - database identity through `application_id` and the refusal rules (section 4.2);
   - the removed routes and commands, and `GET /episodes` removed;
   - `earmark.sanitize`;
   - the interim push wording;
   - the removed settings, including `EARMARK_TZ`;
   - the `slow` marker now also covering the library-logging tests.
2. **`EARMARK_TZ` and `tzdata` are removed.** The brief does not name them; section 5.8
   gives the reason.
3. **The `slow` marker's description widens, and four library-logging tests move under
   it** instead of being deleted with the `index` marker (sections 3.6 and 5.11). The
   alternative was losing coverage of the URL scrubbing for huggingface_hub, which the
   brief says to keep.
4. **The migration list restarts.** This breaks the "never edit a shipped entry" rule
   on purpose. The new lineage is marked by `application_id`, and old files are refused,
   not migrated, as the brief decides.
5. **The README and `docs/deploy.md` get the edits that their tests force**, plus the
   one-paragraph glosses attached to them: the paragraph after the command table, and
   the §8 and §14 bodies. That is slightly more than the bare link removals, because
   leaving a table explanation or a section that only links deleted files would make
   them contradict the content next to them. Everything else waits for step 27.
6. **Small tagline change.** The tagline in `pyproject.toml`, `earmark/__init__.py` and
   the CLI help becomes "Share a podcast episode, get its transcript."

## 9. Out of scope

- **Step 23:** rewriting `docs/ARCHITECTURE.md` and `CLAUDE.md`. `CLAUDE.md` still
  mentions `--extra index`, the `llm` marker, the vault and the Claude-SDK rules.
- **Step 24:** `POST /transcriptions`, uploads, direct URLs, `to`, `instructions`,
  `title`, the allowlist, the API view model, the leak rules (masking, host-only URLs in
  logs), and the transcript cache key covering backend and model.
- **Step 25:** the mailer, the note and instructions renderers (they will build on
  `earmark.sanitize`), delivery state, and the final push wording.
- **Step 26:** removing the `asr` (vLLM) compose service and its `gpu` profile, and the GPU
  image.
- **Step 27:** the README quickstart, the prose of `docs/deploy.md`, `docs/shortcut.md`
  and the runbook, and the upgrade notes for deleting the old `analyses/`, index and model
  directories.
- **The operator:** the GitHub environment `llm` and its `ANTHROPIC_API_KEY` secret in the
  repository settings are no longer used by any workflow. Deleting them happens outside
  the repo.
