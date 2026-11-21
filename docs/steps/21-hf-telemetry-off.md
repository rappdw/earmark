# Step 21: Hugging Face telemetry off by default

## 1. Outcome

Every shipped way of running earmark sets `HF_HUB_DISABLE_TELEMETRY=1`. That covers the
container image (`ENV`), the `api`, `worker`, and `asr` compose services (interpolated
`${HF_HUB_DISABLE_TELEMETRY:-1}`, so `.env` can override it), and all three systemd service
units, including the digest. A live download in this sandbox showed that the one variable
stops hf_xet's download summary and `huggingface_hub`'s own telemetry. No other variable is
set. `HF_XET_LOG_DEST` stays unset (section 5.3 gives the reasons). `docs/deploy.md`
gains a section 17 that says telemetry is off and how to turn it back on for each way of
running. `docs/ARCHITECTURE.md` is already updated by this plan. Static tests pin the
setting in every deploy file, and the container CI workflow checks the built image and
the resolved compose config.

No Python source under `src/` changes.

## 2. Findings

### 2.1 How it was checked

- Scratch venv `$SCRATCH/venv` with `UV_PROJECT_ENVIRONMENT` and `UV_CACHE_DIR` in the
  scratchpad, created with `uv sync --locked --extra transcribe`. The repo `.venv` was not
  touched. Locked versions: huggingface_hub 1.33.0, hf-xet 1.8.0, faster-whisper 1.2.1.
  `/tmp` had 1009 MB free before and after, and the venv, uv cache, and all model copies
  were deleted afterwards.
- Each run used a fresh `HF_HOME` and stripped `HF_HUB_DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
  and `DISABLE_TELEMETRY` from the environment. It ran
  `faster_whisper.utils.download_model('tiny', cache_dir=$HF_HOME/models)` through
  `python -I`, then grepped `$HF_HOME/xet/logs/*.log`. The download went through the
  sandbox proxy and worked every time.
- To see the send itself rather than only its reply, the runs were repeated with
  `RUST_LOG=debug`. That level shows `emitting telemetry` and the `cas::telemetry` request
  span. Every run also logged `File reconstruction completed`, which proves hf_xet
  downloaded `model.bin`, so a missing telemetry line is not a skipped Xet path.

### 2.2 Results: hf_xet download summary

| Environment | `emitting telemetry` | `cas::telemetry` request | `telemetry accepted` |
|---|---|---|---|
| nothing set (INFO) | n/a at INFO | n/a | **1** (`xet_download_summary`, `200 OK`) |
| nothing set (DEBUG) | 1 | 2 lines (to `cas-server.xethub.hf.co`) | 1 |
| `HF_HUB_DISABLE_TELEMETRY=1` (INFO and DEBUG) | 0 | 0 | 0 |
| `HF_HUB_DISABLE_TELEMETRY=true` | 0 | 0 | 0 |
| `DO_NOT_TRACK=1` | 0 | 0 | 0 |
| `HF_XET_TELEMETRY_ENABLED=false` | 0 | 0 | 0 |
| `HF_HUB_DISABLE_TELEMETRY=0` | 1 | 2 | 1 |
| `HF_HUB_DISABLE_TELEMETRY=` (empty) | 1 | 2 | 1 |

- The summary is a POST to the CAS server's `/v1/telemetry`. The binary also has a
  `xet_transfer_heartbeat` event, with `heartbeat_after = 300s`, for transfers that last
  longer than 5 minutes. The same switch disables the whole telemetry sink, so the
  heartbeat is covered too. That was not observed directly, because `tiny` downloads in
  about 2 s.
- hf_xet's config dump shows `Config: enabled = true (default)` for telemetry even with
  `HF_HUB_DISABLE_TELEMETRY=1`. Only `HF_XET_TELEMETRY_ENABLED=false` flips that line, to
  `false (user set)`. The generic variables are checked when the event is sent, not in the
  config. **Do not use the config dump as evidence.** Use the absence of `emitting
  telemetry` or `telemetry accepted`.
- `huggingface_hub` creates `XetSession()` with no arguments (`utils/_xet.py`), so hf_xet
  reads the **process environment** itself. Patching `huggingface_hub.constants` would not
  reach it.

### 2.3 Results: `huggingface_hub`'s own telemetry

`constants.HF_HUB_DISABLE_TELEMETRY` is `_is_true(HF_HUB_DISABLE_TELEMETRY) or
_is_true(DISABLE_TELEMETRY) or _is_true(DO_NOT_TRACK)`. The true values are `1`, `ON`,
`YES`, and `TRUE`, in any case. The constant is read at import. It gates three things:

1. `send_telemetry()`. No library in the transcribe extra calls it (grepped).
2. **The `/api/agent-harnesses` fetch.** A live `hf_hub_download("Systran/faster-whisper-tiny",
   "config.json")` with the variable unset logged `GET https://huggingface.co/api/agent-harnesses`
   and wrote `$HF_HOME/.agent_harnesses.json`, which is refreshed once every 24 h. **It
   also happened under `env -i`, with no agent variables set.** Step 20 (2.7) called this
   a sandbox-only artefact. It is not: production makes the same fetch. Only the
   `agent/claude-code` suffix in the User-Agent is specific to the sandbox. With
   `HF_HUB_DISABLE_TELEMETRY=1`, no fetch was made and no file was written.
3. **The User-Agent extras.** Without the variable, the agent was `unknown/None;
   hf_hub/1.33.0; python/3.13.5; agent/claude-code`, plus `torch/…` when torch is
   installed. With it, the agent was `unknown/None; hf_hub/1.33.0; python/3.13.5`.

fastembed (the `index` extra) downloads through the same `huggingface_hub` and hf_xet, so
the same switch covers it. The `index` extra was not installed this time, to save disk.

### 2.4 Results: hf_xet log files (for the `HF_XET_LOG_DEST` decision)

These were checked with `import hf_xet; hf_xet.XetSession()` alone, which creates the log
file. No download was needed.

| Environment | Where the log went |
|---|---|
| default | `$HF_HOME/xet/logs/xet_<ts>_<pid>.log` (22 KB) |
| `HF_XET_LOG_DEST=<dir>/` | `<dir>/xet_<ts>_<pid>.log` |
| `HF_XET_LOG_DEST=<file>` | that file |
| `RUST_LOG=off` | `$HF_HOME/xet/logs/…log` still created, 0 bytes |

Step 20 found that an empty `HF_XET_LOG_DEST` is ignored and gives a file, not the
console. The config dump also shows hf_xet's own retention: `dir_max_size = 250MB`,
`dir_max_retention_age = 1209600s` (14 days), `dir_min_deletion_age = 86400s`, and
`dir_disable_cleanup = false`.

### 2.5 Override semantics (documented behaviour, not run here)

- **Compose precedence:** `environment:` beats `env_file:`, which beats the image's `ENV`.
  A literal `HF_HUB_DISABLE_TELEMETRY: "1"` under `environment:` could therefore not be
  overridden from `.env`. `${HF_HUB_DISABLE_TELEMETRY:-1}` is interpolated from the shell
  environment and then the project `.env`, the same file `env_file` reads. So
  `HF_HUB_DISABLE_TELEMETRY=0` in `.env` gives `0`, and a missing or empty value gives `1`.
  `:-` rather than `-` is deliberate: `.env.example` says "empty values mean unset", and
  an empty value must not turn telemetry on. Under compose, an empty value would otherwise
  mean "on" to HF (2.2).
- **systemd:** if the same variable is assigned twice by `Environment=`, the later
  assignment wins. Drop-ins are parsed after the unit, so `systemctl edit earmark-worker`
  with `[Service]` / `Environment=HF_HUB_DISABLE_TELEMETRY=0` overrides it. A line in
  `/srv/earmark/.env` does **not** override it natively, because earmark reads that file
  through pydantic-settings and never exports it to `os.environ`.
- **Plain `docker run`:** `-e HF_HUB_DISABLE_TELEMETRY=0` overrides the image `ENV`.
- **Not possible to override:** a `DO_NOT_TRACK=1` or `DISABLE_TELEMETRY=1` already in the
  environment keeps `huggingface_hub` off whatever this variable says (an OR). The docs
  say so.

### 2.6 Dead ends

- `HF_XET_LOG_DEST=""` does not route hf_xet's logs to the console (step 20).
- `RUST_LOG=off` still creates an empty log file per process. It is also a generic
  variable that other Rust code in the image may read. Do not use it.
- Patching `huggingface_hub.constants.HF_HUB_DISABLE_TELEMETRY` does not reach hf_xet
  (2.2).

## 3. Files

| File | Change |
|---|---|
| `Dockerfile` | Runtime-stage `ENV` block: add `HF_HUB_DISABLE_TELEMETRY=1` (after `HF_HOME=/data/huggingface`). |
| `compose.yaml` | `x-earmark` `environment:` (so both `api` and `worker`): add `HF_HUB_DISABLE_TELEMETRY: ${HF_HUB_DISABLE_TELEMETRY:-1}` with a one-line comment. `asr` `environment:`: the same line. |
| `deploy/systemd/earmark-api.service`, `earmark-worker.service`, `earmark-digest.service` | `[Service]`: a separate line `Environment=HF_HUB_DISABLE_TELEMETRY=1`, in api and worker right after the existing `Environment=HF_HOME=…`, and in digest after `WorkingDirectory=`. Do not add `HF_HOME` to the digest unit (out of scope, 9). |
| `.env.example` | Compose-only section: a commented `# HF_HUB_DISABLE_TELEMETRY=1` with a two-line comment (5.4). |
| `.github/workflows/container.yml` | "Validate compose.yaml" and "Check the image" steps: the assertions in 4.3. |
| `docs/deploy.md` | Section 4 table row, a section 5 sentence, a 7a paragraph replaced by a pointer, and a new `## 17. Hugging Face telemetry and logs` after Troubleshooting (5.5). |
| `docs/ARCHITECTURE.md` | Done by the planner (8). The implementer does not edit it. |
| `tests/test_deploy.py` | New tests and one updated test (6). |
| `tests/test_ci.py` | One new test for the workflow assertions (6). |
| `tests/test_docs.py` | One new test for deploy.md section 17 (6). |

## 4. Interfaces

### 4.1 The variable

`HF_HUB_DISABLE_TELEMETRY`, value `1` by default. It is not a `Settings` field and has no
`EARMARK_` prefix. earmark never reads it, and Hugging Face's libraries read it from the
process environment.

### 4.2 Exact deploy-file text

Dockerfile, runtime stage:

```dockerfile
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EARMARK_HOST=0.0.0.0 \
    EARMARK_DATA_DIR=/data \
    EARMARK_VAULT_DIR=/vault \
    HF_HOME=/data/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1
```

compose.yaml, under `x-earmark.environment` and under `services.asr.environment`:

```yaml
    # Hugging Face telemetry off; HF_HUB_DISABLE_TELEMETRY=0 in .env turns it back on.
    HF_HUB_DISABLE_TELEMETRY: ${HF_HUB_DISABLE_TELEMETRY:-1}
```

systemd, each of the three `.service` files, in `[Service]`:

```ini
Environment=HF_HUB_DISABLE_TELEMETRY=1
```

`earmark-digest.timer` has no `[Service]` section and is unchanged.

### 4.3 CI additions (`.github/workflows/container.yml`)

Append to the "Validate compose.yaml" `run:` block, after the existing `config --quiet`
lines:

```bash
check='import json, sys; s = json.load(sys.stdin)["services"]; v = {n: s[n]["environment"]["HF_HUB_DISABLE_TELEMETRY"] for n in ("api", "worker", "asr")}; assert set(v.values()) == {sys.argv[1]}, v'
docker compose --profile gpu config --format json | python3 -c "$check" 1
HF_HUB_DISABLE_TELEMETRY=0 docker compose --profile gpu config --format json | python3 -c "$check" 0
```

Append to "Check the image":

```bash
test "$(docker run --rm --entrypoint printenv earmark:ci HF_HUB_DISABLE_TELEMETRY)" = 1
```

Do not add a step-level or job-level `env:`, because `test_container_workflow_read_only`
forbids it. The inline `VAR=0 cmd` prefix is fine.

## 5. Behaviour and edge cases

### 5.1 Only `HF_HUB_DISABLE_TELEMETRY`

It stopped every telemetry send observed (2.2, 2.3). `DO_NOT_TRACK=1` would do the same,
but it is a cross-tool convention that other software in the image or in the vLLM
container may also read. It would also be a second variable the operator must clear to
turn telemetry back on, because `huggingface_hub` ORs them. `HF_XET_TELEMETRY_ENABLED`
covers hf_xet only. Neither is set.

### 5.2 Interpolated in compose, literal elsewhere

The brief requires that the operator can override the value. Under compose, the natural
place is `.env`, and only interpolation lets `.env` beat `environment:` (2.5). The
Dockerfile `ENV` still matters for `docker run` and for any service that does not set it.
systemd uses a literal, which drop-ins override.

### 5.3 `HF_XET_LOG_DEST` stays unset

The decision is not to set it, for these reasons:

- The logs never leave the host. Telemetry is the network question, and the logs are a
  local-files question.
- Wherever earmark sets `HF_HOME` (the image, the api and worker units), the default
  location is already inside earmark's data directory (`/data/huggingface/xet/logs`,
  `/srv/earmark/data/huggingface/xet/logs`). That directory is writable under
  `ProtectSystem=strict` and lives on the backed-up volume.
- hf_xet prunes the logs itself (250 MB, 14 days, 2.4).
- Step 20 found hosts, request ids, hashes, and local paths in them, but no URL path or
  query.
- The only alternatives are another file or directory. The console is unreachable, and
  it would put unscrubbed JSON next to earmark's own log. `RUST_LOG=off` still creates
  files and is too broad (2.6).

The docs name the knob for an operator who wants the logs elsewhere.

### 5.4 `.env.example`

```text
# Hugging Face telemetry in the api, worker, and asr containers: off (1); 0 turns it on.
# A native install ignores this line; the units set it (docs/deploy.md section 17).
# HF_HUB_DISABLE_TELEMETRY=1
```

This lives in the "Docker Compose only" block, after `ASR_GPU_MEMORY_UTILIZATION`. It stays
commented, so the default comes from compose. `test_env_example_values_are_defaults` is
unaffected, because the line is not an `EARMARK_` setting.

### 5.5 `docs/deploy.md`

- **Section 4, compose-only table:** a row
  `| \`HF_HUB_DISABLE_TELEMETRY\` | \`1\` | Hugging Face telemetry in the containers; \`0\` turns it back on (section 17). Read by Hugging Face's libraries, not by earmark. |`
  (`test_compose_only_variables_documented` requires the row to start
  `` | `HF_HUB_DISABLE_TELEMETRY` | ``).
- **Section 5 (native):** after step 4, one sentence: the units set
  `HF_HUB_DISABLE_TELEMETRY=1`, and section 17 has the details.
- **Section 7a:** replace the hf_xet paragraph (lines 310–313 now) with one sentence:
  hf_xet writes its own logs under `$HF_HOME/xet/logs/`, and Hugging Face telemetry is off
  by default (section 17).
- **New `## 17. Hugging Face telemetry and logs`**, after `## 16. Troubleshooting`.
  Appending avoids renumbering sections 14–16, which tests and cross-references pin. It
  says:
  - **Off by default.** Every shipped way of running earmark sets
    `HF_HUB_DISABLE_TELEMETRY=1`: the image, the `api`, `worker`, and `asr` compose
    services, and the three systemd services.
  - **What that stops:** hf_xet's download summary to the Xet CAS server, and
    `huggingface_hub`'s daily `/api/agent-harnesses` fetch and User-Agent extras. It was
    tested with a real model download (hf_xet 1.8.0, huggingface_hub 1.33.0), outside a
    container.
  - **Turning it back on**, as a short list:
    - compose: `HF_HUB_DISABLE_TELEMETRY=0` in `.env`, then `docker compose up -d`;
    - systemd: `sudo systemctl edit earmark-worker` (and `earmark-api`,
      `earmark-digest`) with `[Service]` and `Environment=HF_HUB_DISABLE_TELEMETRY=0`,
      then restart. A line in `/srv/earmark/.env` has no effect, because earmark reads
      that file itself;
    - `docker run`: `-e HF_HUB_DISABLE_TELEMETRY=0`.
    - It stays off while `DO_NOT_TRACK=1` or `DISABLE_TELEMETRY=1` is set in the
      environment.
  - **Commands run by hand** on a native install, such as `earmark reindex` (which
    downloads the embedding model), do not get the unit's environment. Prefix them with
    `env HF_HUB_DISABLE_TELEMETRY=1`. `docker compose run` gets it from compose.
  - **hf_xet logs:** `$HF_HOME/xet/logs/` (`/data/huggingface/xet/logs` in the container),
    pruned by hf_xet at 250 MB or 14 days, with hosts and request ids but no URL paths.
    Earmark leaves them there. `HF_XET_LOG_DEST` (a directory ending in `/`, or a file)
    moves them, and an empty value is ignored.
  - **The `asr` container:** the variable covers its Hugging Face downloads. vLLM's own
    usage statistics are a separate setting that earmark does not change. Say only that
    vLLM "has its own usage-statistics setting; see its documentation", and name no
    variable, because none was verified.

Keep the existing troubleshooting row about the HF hint as it is.

## 6. Tests

All are default-suite, static, with no network. Every acceptance criterion maps to a test
below.

**A1. Every shipped way of running earmark sets `HF_HUB_DISABLE_TELEMETRY=1`.**

- `tests/test_deploy.py::test_dockerfile_disables_hf_telemetry`: take the logical lines
  after the last `FROM` (the runtime stage). Their `ENV` instructions contain
  `HF_HUB_DISABLE_TELEMETRY=1`. A build-stage `ENV` must not satisfy it, so the implementer
  needs a small helper that slices `_logical_lines(DOCKERFILE)` from the last line whose
  first word is `FROM`.
- `tests/test_deploy.py::test_compose_disables_hf_telemetry`: for `api`, `worker`, and
  `asr`, `SERVICES[name]["environment"]["HF_HUB_DISABLE_TELEMETRY"] ==
  "${HF_HUB_DISABLE_TELEMETRY:-1}"`. PyYAML's merge key (`<<: *earmark`) already gives
  `api` and `worker` the anchor's mapping, as the existing `test_compose_forces_container_paths`
  relies on.
- `tests/test_deploy.py::test_systemd_services_disable_hf_telemetry`: parametrized over
  `sorted((ROOT / "deploy" / "systemd").glob("*.service"))` (ids by file name), so a future
  unit is covered. For each file, `"HF_HUB_DISABLE_TELEMETRY=1"` is in
  `" ".join(unit["Service"].get("Environment", [])).split()`. A separate non-parametrized
  assertion, which can live in the same module, says the glob yields exactly
  `{"earmark-api.service", "earmark-worker.service", "earmark-digest.service"}`. That keeps
  the parametrized test from going vacuous.
- CI (`container.yml`, 4.3): the built image has the value, and the resolved compose config
  has it for all three services.
  `tests/test_ci.py::test_container_workflow_checks_hf_telemetry` asserts that the workflow
  runs contain `printenv HF_HUB_DISABLE_TELEMETRY`, `config --format json`, and
  `HF_HUB_DISABLE_TELEMETRY=0 docker compose`. It reuses `_container_runs()`.

**A2. An operator can still override it.**

- `test_compose_disables_hf_telemetry` also asserts the value is the interpolation form,
  not a literal: `.startswith("${HF_HUB_DISABLE_TELEMETRY:")`. A literal `"1"` must fail.
- `test_compose_only_variables_documented`: update the expected set to include
  `HF_HUB_DISABLE_TELEMETRY`. The test then also proves the deploy.md table row and the
  `# HF_HUB_DISABLE_TELEMETRY=` line in `.env.example`.
- `test_dockerfile_disables_hf_telemetry` and the systemd test use plain `ENV` and
  `Environment=`, and both are overridable by construction (2.5). The docs test below
  checks that each override path is written down.
- CI: the `HF_HUB_DISABLE_TELEMETRY=0 docker compose … config` check resolves to `0` for
  every service (4.3).

**A3. The live result is recorded, plus whatever else is needed if the one variable is
not enough.** This is planner research, recorded in 2.2 and 2.3: the one variable is
enough. There is no test. The verifier repeats it with the smoke in 7.

**A4. The `HF_XET_LOG_DEST` decision is recorded.** See 5.3. As a test, the planner's own
criterion: `tests/test_deploy.py::test_deploy_files_leave_xet_logs_alone` asserts that
`HF_XET_LOG_DEST` and `RUST_LOG` occur in none of `Dockerfile`, `compose.yaml`, or
`deploy/systemd/*`. This pins the decision.

**A5. `docs/deploy.md` and `docs/ARCHITECTURE.md` say it is off and how to turn it on.**

- `tests/test_docs.py::test_deploy_doc_hf_telemetry_section`: the section
  `## 17. Hugging Face telemetry and logs` exists, after `## 16. Troubleshooting`. Slice it
  the way `test_deploy_doc_digest_section` does, to the next `\n## ` or the end of the file.
  It contains `HF_HUB_DISABLE_TELEMETRY=1`, `HF_HUB_DISABLE_TELEMETRY=0`, `systemctl edit`,
  `.env`, `HF_XET_LOG_DEST`, and `DO_NOT_TRACK`. In
  `read_doc("docs/ARCHITECTURE.md")`, `HF_HUB_DISABLE_TELEMETRY=1` and
  `HF_HUB_DISABLE_TELEMETRY=0` both occur.

**Planner's added criterion: earmark's Python code is unchanged** (the setting lives only
in the deploy files, 9). No test is needed. The verifier checks with `git diff --stat`
that nothing under `src/` changed.

No fixtures are needed.

## 7. Verification

```bash
make check
uv run pytest tests/test_deploy.py tests/test_ci.py tests/test_docs.py -v -k "telemetry or xet_logs or compose_only"
git diff --stat            # nothing under src/; docs, deploy files, CI, tests only
grep -rn HF_HUB_DISABLE_TELEMETRY Dockerfile compose.yaml deploy/systemd .env.example
```

If `docker compose` is available (it is not in this sandbox), also run the CI check from
4.3 by hand. Otherwise CI verifies it on push. Report it as "CI only".

Live smoke, which repeats 2.2 with the shipped value. It needs about 470 MB plus 75 MB in
`/tmp`, so check `df -h /tmp` first. Never use the repo `.venv`.

```bash
S=/tmp/em21; mkdir -p $S
export UV_PROJECT_ENVIRONMENT=$S/venv UV_CACHE_DIR=$S/cache
uv sync --locked --extra transcribe
for v in 1 0; do
  rm -rf $S/hf
  env -u DO_NOT_TRACK -u DISABLE_TELEMETRY HF_HOME=$S/hf RUST_LOG=debug HF_HUB_DISABLE_TELEMETRY=$v \
    $S/venv/bin/python -I -c "import faster_whisper.utils as u; u.download_model('tiny', cache_dir='$S/hf/models')" >/dev/null 2>&1
  echo "HF_HUB_DISABLE_TELEMETRY=$v accepted=$(grep -rhc 'telemetry accepted' $S/hf/xet/logs) emitting=$(grep -rhc 'emitting telemetry' $S/hf/xet/logs) done=$(grep -rhc 'File reconstruction completed' $S/hf/xet/logs)"
done
# expect: =1 accepted=0 emitting=0 done=1 ; =0 accepted=1 emitting=1 done=1
rm -rf $S; unset UV_PROJECT_ENVIRONMENT UV_CACHE_DIR
```

If the proxy blocks the download, record that and rely on the planner's results in 2.2.
Not verifiable here: containers, compose resolution (CI only), systemd drop-ins, and the
vLLM container.

## 8. Deviations

- **ARCHITECTURE.md changed by the planner** (the contract):
  - Image environment adds the variable.
  - The compose bullet states the interpolated form, why it is interpolated, and the
    added compose-only variable.
  - The native bullet states the unit lines, the drop-in override, that `.env` has no
    effect natively, and that commands run by hand outside the units do not get it.
  - The *hf_xet* bullet now covers log retention and `HF_XET_LOG_DEST`.
  - A new *Telemetry, off by default* bullet gives the verified behaviour and
    alternatives.
  - The `test_deploy.py` testing line now covers the setting.
- **The `asr` service gets the variable too.** The brief names "both compose services".
  `asr` is earmark's own compose service and downloads its model from Hugging Face, so it
  serves the goal, "earmark's own deployments send no Hugging Face telemetry". Its effect
  inside NVIDIA's vLLM image is untested.
- **CI workflow assertions** (4.3) go beyond the static test the brief asks for. They are
  the only place the compose precedence and the built image are actually resolved.
- **Correction to step 20 (2.7):** the `/api/agent-harnesses` fetch is not sandbox-only
  (2.3). The step 20 plan is history and is left unedited. The architecture now states
  the correct behaviour.
- **deploy.md section 17 is appended after Troubleshooting** instead of being inserted,
  so that sections 14–16, which `test_docs.py` and the text pin, keep their numbers.

## 9. Out of scope

- Setting the variable from earmark's own code (for example `os.environ.setdefault` in
  the CLI entry point). That would also cover commands run by hand and dev runs, but the
  brief scopes this to the deploy files. It is offered to the user as a follow-up.
- `HF_HOME` in `earmark-digest.service`. The digest loads no models today.
- vLLM's own usage statistics in the `asr` container, and any non-Hugging-Face telemetry.
- An `earmark doctor` check that warns when telemetry is on.
- Moving or silencing hf_xet's log files (5.3).
