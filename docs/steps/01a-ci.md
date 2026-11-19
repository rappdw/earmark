# Step 01a: CI: the gate on every push

## 1. Outcome

Every push to `main` or `impl`, and every pull request, runs one GitHub Actions job on
a free `ubuntu-24.04-arm` runner (linux/aarch64, the deployment architecture): check
out, install uv with caching, `uv sync --locked`, `make check`. Python 3.13 comes from
`.python-version` through uv. The workflow is read-only (`contents: read`), uses no
secrets, pins both actions to full commit SHAs, cancels superseded runs, and times out.
Dependabot proposes weekly updates for the `uv` and `github-actions` ecosystems. A new
test module, `tests/test_ci.py`, runs inside `make check` and enforces all of this,
including an offline `actionlint` pass over every workflow file. The architecture's
Testing section says what CI runs and what it does not (already edited by this plan).

The live run cannot happen in the sandbox. It happens on the user's first push of
`impl`; see section 7.3 and the note on the `workflow` token scope there.

## 2. Findings

All checks were run on 2026-11-19 from this sandbox (aarch64, CPython 3.13.5, uv
0.13.9) using `gh api` against GitHub, PyPI's JSON API, and throwaway copies of the repo
in the scratchpad (deleted afterwards). Nothing was installed into the repo's venv.

### 2.1 Action versions and pinned SHAs

| Action | Latest release | Published | Tag object | Commit SHA |
|---|---|---|---|---|
| `actions/checkout` | `v7.0.1` | 2026-07-20 | lightweight tag → commit | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `astral-sh/setup-uv` | `v10.3.3` | 2026-10-28 | lightweight tag → commit | `68559e99cc807c1eb7e9238fd43a15bba460eec2` |

How:

- `gh api repos/<owner>/<repo>/releases/latest` gave the tag (both non-draft,
  non-prerelease).
- `gh api repos/<owner>/<repo>/git/ref/tags/<tag> --jq .object` returned
  `"type": "commit"` for both, so neither tag is annotated and no dereferencing was
  needed. (Had it been `"type": "tag"`, the commit is at
  `gh api repos/<o>/<r>/git/tags/<tag-object-sha> --jq .object.sha`.)
- Cross-check: `gh api repos/<o>/<r>/commits/<tag> --jq .sha` returned the same SHAs.
- Impostor-commit check: `gh api repos/<o>/<r>/compare/main...<sha>` returned
  `status: behind, ahead_by: 0` for both, so each SHA is an ancestor of the upstream
  `main` and not a commit that only exists in a fork.
- `actions/checkout` still publishes a floating `v7` tag (it points at the same SHA).
  `astral-sh/setup-uv` has **no** floating `v10` tag (404), so `@v10` would not even
  resolve. Pinning by SHA is required for setup-uv regardless.
- Both actions run on `node24` (from their `action.yml` at the pinned SHA).
- Dependabot's github-actions updater (dependabot-core
  `github_actions/.../version_commenter.rb`) recognises a trailing `# vX.Y.Z` comment on
  a SHA-pinned `uses:` and rewrites it together with the SHA. So the comment must be the
  full tag, e.g. `# v7.0.1`, with nothing else after it.

### 2.2 Runner label

- `actions/runner-images` README lists the arm64 Linux labels `ubuntu-22.04-arm`,
  `ubuntu-24.04-arm`, and `ubuntu-26.04-arm`. There is no `ubuntu-latest-arm`.
  `ubuntu-latest` is still 24.04.
- GitHub's runner reference (fetched via a web summary; `docs.github.com` itself was
  not reachable from this sandbox): "Use of the standard GitHub-hosted runners is free
  and unlimited on public repositories", and the arm64 labels are listed among the
  standard runners with 4 vCPU / 16 GB for public repositories.
- The repo is public (`gh api repos/rappdw/earmark` → `visibility: public`, default
  branch `main`). Actions are enabled, all actions allowed, default workflow token
  permission `read`, `sha_pinning_required: false`.
- **Choose `ubuntu-24.04-arm`.** It is GA, it matches the DGX Spark's Ubuntu 24.04 base,
  and actionlint 1.7.12 knows it. actionlint 1.7.12 does **not** know
  `ubuntu-26.04-arm` and would fail the lint with `[runner-label]` (observed: its
  list of known labels includes `ubuntu-24.04-arm` and `ubuntu-22.04-arm` only).
- The `ubuntu-24.04-arm` image (`images/ubuntu/Ubuntu2404-Arm64-Readme.md`, image
  20261111) has system Python 3.12.3 on `PATH`, Python 3.13.18 only in the tool cache
  (not on `PATH`), and GNU make 4.3. So uv will download a managed CPython 3.13 on the
  runner, which `cache-python: true` caches (section 2.3).

### 2.3 `astral-sh/setup-uv` behaviour (read at the pinned SHA)

From `action.yml`, `README.md`, `docs/caching.md`, and
`docs/advanced-version-configuration.md` at `68559e99…`:

- **It does not read `.python-version` itself.** Its `python-version` input only sets
  `UV_PYTHON`, which would *override* `.python-version`. Python selection is left to uv:
  `uv sync` reads `.python-version` in the project root. Verified locally: a scratch
  project with `.python-version` = `3.11` and `UV_PYTHON_DOWNLOADS=never` failed with
  "No interpreter found for Python 3.11", proving uv takes the request from the file.
  So the workflow must **not** set `python-version`, `UV_PYTHON`, or use
  `actions/setup-python`.
- **uv version.** With no `version` input, it looks for `required-version` in
  `uv.toml`/`pyproject.toml`, else installs the latest uv. The `version` input accepts
  an exact version, `latest`, a semver range, or a PEP 440 specifier such as
  `">=0.4.25,<0.5"` (highest match by default). See 2.4 for the choice.
- **Caching inputs.** `enable-cache` (default `auto`: on for GitHub-hosted runners except
  release, tag push, `pull_request_target`, and `workflow_run` events),
  `cache-dependency-glob` (default: requirements/constraints files, `**/pyproject.toml`,
  `**/uv.lock`, `**/*.py.lock`), `restore-cache`, `save-cache`, `cache-suffix`,
  `cache-local-path`, `prune-cache` (default `false`; when true, removes pre-built wheels
  but keeps wheels built from source), `cache-python` (default `false`; caches uv-managed
  Python installs), `ignore-nothing-to-cache`. The cache key includes architecture, OS
  version, Python version, pruning and Python-cache flags, and the hash of the files
  matching the glob.
- Checkout must run before setup-uv (README FAQ).

### 2.4 uv version in CI: a PEP 440 range in the workflow, not `required-version`

- Latest uv is 0.13.9 (2026-11-17), the same as the sandbox.
- Dependabot's uv updater runs uv 0.13.7 (dependabot-core `uv/Dockerfile`:
  `FROM ghcr.io/astral-sh/uv:0.13.7`). GitHub's docs table still says "v0.11"; the
  Dockerfile is the authority.
- Setting `[tool.uv] required-version = ">=0.13.9,<0.14"` in `pyproject.toml` would pin
  CI and the sandbox together, but uv enforces `required-version` on every command, so
  Dependabot's 0.13.7 would refuse to touch the project. **Do not add
  `required-version`.**
- Instead the workflow passes `version: ">=0.13.9,<0.14"` to setup-uv. It tracks uv
  patch releases, stays on the same minor as the `uv_build>=0.13.9,<0.14.0` build
  backend bound, and avoids an unrelated uv minor release breaking CI. Bump both bounds
  together, deliberately. Dependabot does not update `with: version` inputs.

### 2.5 Workflow linter: `actionlint-py` (the real actionlint, run by a test)

PyPI checks:

| Package | Latest | Files | Notes |
|---|---|---|---|
| `actionlint-py` | 1.7.12.25 (2026-09-22) | **sdist only**, 12 kB, every release | Wraps rhysd/actionlint 1.7.12 (latest upstream, 2026-03-30). Its setuptools build downloads the matching actionlint release tarball from GitHub and checks it against SHA-256 sums shipped in the sdist (`_custom_build/checksums.cfg`, includes `linux_arm64`). Build deps: setuptools, wheel, requests, semver. |
| `zizmor` | 1.32.0 | manylinux/musllinux aarch64 wheels | Security auditor for workflows, not a structural linter. Considered, not chosen (2.5.1). |
| `check-jsonschema` | 0.39.0 | pure wheel | Schema check with vendored SchemaStore schemas. Considered, not chosen (2.5.1). |
| `actionlint`, `actionlint-bin`, `py-actionlint`, `actionlint-binary`, `pyactionlint`, `actionlint-python`, `gha-actionlint` | — | 404 | Do not exist. |

Trial in a scratch copy of the repo:

- `uv add --dev --no-sync 'actionlint-py>=1.7.12.25' 'pyyaml>=6.0.3' 'types-pyyaml>=6.0.12.20261113'`
  resolved 43 packages with the existing `required-environments` (sdists do not trip
  it), and `uv lock --check` passed afterwards.
- `uv sync --locked` built `actionlint-py` from the sdist in about 20 s (first time
  only; uv caches the built wheel) and installed `.venv/bin/actionlint`, a 5.8 MB static
  Go binary (`actionlint -version` → `1.7.12 … built with go1.26.1 compiler for
  linux/arm64`).
- `make check` in that copy, with a probe test importing `yaml` under mypy strict,
  passed (65 tests).
- A probe test inside the socket-blocked suite ran
  `Path(sys.executable).parent / "actionlint"` on the draft workflow (exit 0, `[]`) and
  on a broken one (exit 1). pytest-socket only blocks sockets **in the pytest process**;
  a subprocess is not blocked, so "offline" has to be a property of the tool itself.
  For actionlint it is: its docs (`docs/checks.md`, v1.7.12) say popular-action data is
  embedded "without fetching any `action.yml` of the actions from the remote", and it
  ran green with `env -i PATH=/usr/bin:/bin HTTPS_PROXY=http://127.0.0.1:9
  HTTP_PROXY=http://127.0.0.1:9`. `unshare -rn` is not permitted in this sandbox and
  `strace` is not installed, so a hard network-namespace proof was not possible.
- Behaviour to code against:
  - Exit codes: `0` clean, `1` lint errors found, `2` bad flags, `3` fatal (e.g. file not
    found: `could not read "/nonexistent.yml"`).
  - `-format '{{json .}}'` prints a JSON array, `[]` when clean. Each error object has
    keys `message`, `filepath`, `line`, `column`, `end_column`, `kind`, `snippet`.
    Kinds seen: `runner-label`, `expression`, `syntax-check`.
  - With no file arguments it searches for a git repo and fails outside one ("no project
    was found …", exit 3). With explicit file paths it works anywhere, including a
    clone without `.git` and a cwd that is not the repo.
  - If `shellcheck` or `pyflakes` is on `PATH`, actionlint runs them on `run:` scripts.
    GitHub's Ubuntu images ship shellcheck; this sandbox does not. Pass `-shellcheck=`
    and `-pyflakes=` (empty disables, per `docs/usage.md`) so the result is identical in
    both places.
  - It caught `runs-on: ubuntu-24.04-armm` (`[runner-label]`, listing known labels) and
    `${{ github.reff }}` (`[expression]`), and passed the planned `ci.yml` (section 4.1).
  - It does not validate `dependabot.yml`.

#### 2.5.1 Decision

Use `actionlint-py` in the dev group, run by a test (`tests/test_ci.py`), not as a
separate `make lint` line:

- It is the only real workflow linter on PyPI. It type-checks expressions, knows runner
  labels and event names, and checks `needs`, `permissions` keys, and more. A JSON
  schema check (check-jsonschema) only checks shape, and pulls in jsonschema, ruamel.yaml,
  requests, and regress. zizmor audits security, which `tests/test_ci.py` already covers
  for the brief's rules, and adds about 10 MB on a tight disk.
- A test, rather than a Makefile line, globs every file under `.github/workflows/`, so
  the workflows later steps add (07, 08, 10, 16) are linted with no Makefile change. It
  also gets a negative control proving the linter is live, and it runs in `make check`
  through pytest, satisfying "run inside `make check` or by a test".
- Costs, accepted: the sdist build needs network on first `uv sync` (true in CI and
  for `uv sync` here) and its build dependencies are not locked by `uv.lock`; the
  downloaded binary is checksum-verified by the sdist, and the sdist itself is
  hash-pinned in `uv.lock`.

### 2.6 Reading YAML in tests: PyYAML + types-PyYAML

The standard library has no YAML parser. `PyYAML 6.0.3` has a cp313 manylinux aarch64
wheel; `types-PyYAML 6.0.12.20261113` is a pure wheel and makes `import yaml` pass mypy
strict (verified in the trial `make check`). Neither was already in `uv.lock`. Step 12
needs a YAML parser in tests anyway (frontmatter).

Gotchas, observed with `yaml.safe_load` on the draft workflow:

- PyYAML is YAML 1.1: the key `on:` loads as the boolean `True`, not `"on"`. The parsed
  workflow had keys `['name', True, 'permissions', 'concurrency', 'jobs']`.
- `pull_request:` with no value loads as `None`.
- `enable-cache: true` loads as Python `True`.
- Comments are discarded, so the `# vX.Y.Z` pin comments must be checked on the raw
  text.

### 2.7 Dependabot

From `github/docs` `content/code-security/reference/supply-chain-security/dependabot-options-reference.md`:

- `version: 2`; required per entry: `package-ecosystem`, `directory` (or
  `directories`), `schedule.interval`.
- Ecosystem values: `uv` (supported for version updates, not beta) and
  `github-actions`. For `github-actions`, `directory: "/"` makes Dependabot scan
  `/.github/workflows`.
- `schedule.interval: weekly` runs on Monday by default.
- `groups.<IDENTIFIER>`: identifier must start and end with a letter and may contain
  letters, `|`, `_`, `-` (no digits). Keys `patterns`, `exclude-patterns`,
  `update-types` (`major`/`minor`/`patch`), `dependency-type`, `applies-to`.
- `cooldown.default-days` is supported for both ecosystems; without it Dependabot
  applies a default 3-day cooldown to version updates. Security updates ignore it.
- Dependabot reads `dependabot.yml` from the default branch only and opens pull
  requests against it unless `target-branch` is set. Here the default branch is `main`
  and `impl` is not on the remote yet, so Dependabot activates only once this file
  reaches `main`.

### 2.8 Running the gate in CI

- `uv sync --locked` fails if `uv.lock` is stale and otherwise installs the project plus
  the `dev` group (uv's default groups), without extras.
- `make check` then calls `uv run --locked …` for each tool. After the explicit sync this
  re-checks the lock and syncs nothing, so running `make check` unchanged is correct and
  keeps CI and the local gate identical. No `UV_LOCKED`, `UV_FROZEN`, or
  `--no-sync` tweaks are needed or wanted.
- The pytester-based tests in `tests/test_tooling.py` spawn `sys.executable` and need
  nothing from the runner beyond the venv.

### 2.9 Pushing workflow files (for the user, not the implementer)

`gh auth status` shows the token scopes `admin:public_key, gist, read:org, repo`, with
no `workflow` scope. GitHub rejects an HTTPS push that creates or changes a file under
`.github/workflows/` from an OAuth token without `workflow`. Before the first push of
`impl`, run `gh auth refresh -h github.com -s workflow` (or push over SSH).

## 3. Files

Create:

| File | Purpose |
|---|---|
| `.github/workflows/ci.yml` | The CI workflow, exactly as in section 4.1. |
| `.github/dependabot.yml` | Weekly `uv` and `github-actions` updates, exactly as in section 4.2. |
| `tests/test_ci.py` | Static checks of the workflow and Dependabot files, the actionlint run, and its negative control (section 4.3, section 6). |

Change:

| File | Change |
|---|---|
| `pyproject.toml` | Add `actionlint-py>=1.7.12.25`, `pyyaml>=6.0.3`, `types-pyyaml>=6.0.12.20261113` to `[dependency-groups] dev`. Do it with `uv add --dev 'actionlint-py>=1.7.12.25' 'pyyaml>=6.0.3' 'types-pyyaml>=6.0.12.20261113'`, which appends them after `mypy` and updates the lock. Change nothing else in `pyproject.toml`; in particular do **not** add `[tool.uv] required-version` (section 2.4). |
| `uv.lock` | Regenerated by the `uv add` above. Commit it. |
| `docs/ARCHITECTURE.md` | Already edited by this plan (Testing → "Continuous integration"). The implementer does not touch it. |

Do not change the `Makefile`, `CLAUDE.md`, `docs/PLAN.md`, `.gitignore`, or anything in
`src/`.

## 4. Interfaces

### 4.1 `.github/workflows/ci.yml`

Write this file exactly (comments may be reworded, but must not contain the word
`secrets`, `python-version`, or `pull_request_target`, which tests scan for in the raw
text):

```yaml
# The gate on every push: the same `make check` the build loop runs locally,
# on the architecture the service deploys to (linux/aarch64).
name: CI

on:
  push:
    branches: [main, impl]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    name: make check (linux/arm64)
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 15
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
      - name: Install dependencies
        run: uv sync --locked
      - name: Gate
        run: make check
```

This exact text (minus the inner comment) passed `actionlint -shellcheck= -pyflakes=`
in the trial.

Notes on each choice:

- `on.pull_request` has no branch filter: PRs into any branch are checked.
- `concurrency.group` uses `github.ref`, which is `refs/heads/<branch>` for pushes and
  `refs/pull/<n>/merge` for PRs, so a newer push to the same branch or PR cancels the
  older run, and a push run and a PR run for the same commit do not cancel each other.
- `persist-credentials: false`: nothing later in the job needs git credentials, so the
  token is not left in `.git/config` where `make check`'s code could read it.
- No job-level `permissions`, `env`, `strategy`, `services`, `container`, or
  `defaults`.
- `cache-dependency-glob: uv.lock` instead of the default glob: `uv sync --locked`
  guarantees `uv.lock` changes whenever dependencies do, and edits to tool config in
  `pyproject.toml` should not invalidate the cache.
- `prune-cache: true` keeps the from-source `actionlint-py` wheel in the cache and drops
  pre-built wheels, which uv's docs say are faster to re-download on hosted runners.
- `cache-python: true` caches the managed CPython 3.13 that uv downloads (section 2.2).
- `enable-cache: true` instead of `auto`: same effect for these events, and explicit
  enough for a test to assert.

### 4.2 `.github/dependabot.yml`

Write exactly:

```yaml
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    groups:
      python-minor-patch:
        update-types: [minor, patch]

  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    groups:
      actions:
        patterns: ["*"]
```

- `python-minor-patch` bundles minor and patch Python bumps into one weekly PR; majors
  arrive as individual PRs so a breaking change is isolated. The identifier has no
  digits, as required.
- All action bumps come as one PR.
- `cooldown.default-days: 7` waits a week before proposing a fresh release (supply-chain
  hygiene; security updates are unaffected).
- No `target-branch`: Dependabot uses `main` (section 2.7).

### 4.3 `tests/test_ci.py`

Module-level constants and helpers (names are part of the plan; bodies are the
implementer's):

```python
ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
ACTIONLINT = Path(sys.executable).parent / "actionlint"
ARM64_RUNNER = "ubuntu-24.04-arm"
CI_COMMANDS = ["uv sync --locked", "make check"]
CHECKOUT = "actions/checkout"
SETUP_UV = "astral-sh/setup-uv"


def _workflow_files() -> list[Path]:
    ...
    # sorted *.yml and *.yaml under WORKFLOWS_DIR; asserts the list is non-empty and
    # contains CI_WORKFLOW, so the glob can never pass vacuously.


def _load_yaml(path: Path) -> dict[Any, Any]:
    ...
    # yaml.safe_load(path.read_text()); assert isinstance(..., dict).


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    ...
    # The `on` value, read as workflow["on"] if present else workflow[True] (PyYAML 1.1).
    # Normalise: str -> {s: None}; list -> {s: None for s in list}; dict -> dict.


def _only_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    ...
    # asserts exactly one job, returns it.


def _remote_uses(workflow: dict[Any, Any]) -> list[str]:
    ...
    # every `uses` value from job-level `uses` (reusable workflows) and step-level
    # `uses`, excluding local refs starting with "./".


def _action(step: dict[str, Any]) -> str:
    ...
    # the part of step["uses"] before "@", or "" for run steps.


def _run_actionlint(*paths: Path) -> tuple[int, list[dict[str, Any]], str]:
    ...
    # subprocess.run([str(ACTIONLINT), "-shellcheck=", "-pyflakes=",
    #                 "-format", "{{json .}}", *map(str, paths)],
    #                capture_output=True, text=True, check=False, cwd=ROOT, timeout=60)
    # returns (returncode, json.loads(stdout) if stdout.strip() else [], stderr)
```

Test functions are listed in section 6. Use `pytest.mark.parametrize` over
`_workflow_files()` for the per-workflow rules, with `ids=` set to the file name, so
later workflows are covered automatically. (Calling `_workflow_files()` at collection
time is fine; it only reads the directory.)

No new settings, CLI commands, or HTTP surface.

## 5. Behaviour and edge cases

1. **`on` is `True` under PyYAML.** Every test that looks at triggers goes through
   `_triggers`. A test that does `workflow["on"]` will `KeyError`.
2. **Empty trigger values.** `pull_request:` is `None`. The trigger test accepts `None`
   or `{}` for `pull_request` and requires no `branches`, `branches-ignore`, `paths`, or
   `types` filters under it.
3. **Exact commands.** Compare `step["run"].strip()` against `CI_COMMANDS`. The `run`
   steps must be exactly those two, in that order, and the last two steps of the job.
   Any extra `run` step (for example `uv run pytest -m network`) fails the test. This is
   the drift guard: CI may not grow its own gate.
4. **Python source.** Assert all of: the setup-uv step's `with` has no `python-version`
   key; no step's action is `actions/setup-python`; the strings `UV_PYTHON` and
   `python-version` do not occur in the raw `ci.yml` text; no `env` key exists at
   workflow, job, or step level in `ci.yml`. Separately assert `.python-version` stripped
   equals `"3.13"` and `pyproject.toml`'s `requires-python` starts with `">=3.13"`, so
   the file uv reads agrees with the project.
5. **Pins on the raw text.** For each workflow file, every line matching
   `^\s*(?:-\s+)?uses:\s*` must match
   `^\s*(?:-\s+)?uses:\s*(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@(?P<sha>[0-9a-f]{40})\s+#\s*v\d+\.\d+\.\d+\s*$`,
   unless its value starts with `./`. Also assert the number of matched lines equals
   `len(_remote_uses(workflow))`, so a flow-style or multi-line `uses:` cannot slip past
   the line regex. A `docker://` reference fails this rule on purpose. Short SHAs, tags,
   branches, and `@main` all fail.
6. **`pull_request_target`** is checked twice per workflow: not a key of `_triggers`,
   and not a substring of the raw text.
7. **Secrets.** For `ci.yml` only: `re.search(r"\bsecrets\b", text)` is `None`. This
   also forbids the word in comments, which is the price of a simple, unfoolable check.
   Step 10's manual `llm` workflow will reference a secret, which is why this rule is
   not applied to every workflow.
8. **Permissions.** Every workflow must have a top-level `permissions` key. For
   `ci.yml`: `workflow["permissions"] == {"contents": "read"}` and the job has no
   `permissions` key (job-level permissions would override the top-level ones).
9. **Concurrency.** `ci.yml`'s top-level `concurrency` is a dict whose `group` contains
   `github.ref` and whose `cancel-in-progress` is `True` (a boolean, not the string).
10. **Timeout.** The job's `timeout-minutes` is an `int` (not `bool`, which is an `int`
    subclass; check `type(x) is int`) with `1 <= x <= 30`.
11. **actionlint must not depend on the host.** Always pass `-shellcheck=` and
    `-pyflakes=`, always pass explicit absolute file paths, and run with `cwd=ROOT`.
    If `ACTIONLINT` does not exist, the test fails with a message telling the reader to
    run `uv sync` (no skip: a missing linter must not turn green).
12. **actionlint output.** On failure, the assertion message should include each
    error's `filepath:line:column: message [kind]` so a CI log is readable. A non-zero
    exit with an empty JSON array (exit 2 or 3) is also a failure; include stderr.
13. **Negative control.** Write a workflow to `tmp_path / "broken.yml"` with
    `runs-on: ubuntu-24.04-armm` and a step `run: echo ${{ github.reff }}`, run
    actionlint on it, and assert exit code `1` and that the set of `kind` values
    includes both `runner-label` and `expression`. This proves the binary runs, parses,
    and reports, so the positive test cannot pass by accident (e.g. a stub binary that
    always exits 0).
14. **Dependabot.** `version == 2`; the `package-ecosystem` values are exactly
    `["uv", "github-actions"]` as a multiset (no duplicates, no extras); each entry's
    `directory == "/"`; each `schedule.interval == "weekly"`. Do not assert `groups` or
    `cooldown` in tests: they are tuning, not the brief's contract.
15. **Imports and lint.** `yaml.safe_load` only (ruff `S506` forbids `yaml.load`).
    `subprocess` with an absolute executable path and a list argument (S603 is already
    ignored for tests; S607 does not fire on absolute paths). No `noqa`, no
    `type: ignore`. Annotate parsed YAML as `dict[Any, Any]` / `Any` and narrow with
    `isinstance` asserts so mypy strict is satisfied without casts where possible.
16. **The isolation fixture chdirs to `tmp_path`.** Read every repo file via `ROOT`,
    never via the cwd.

## 6. Tests

All tests are in `tests/test_ci.py` and run in the default suite. No fixtures need
downloading; the broken workflow for the negative control is written into `tmp_path`.

### Brief criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | Install and test commands are exactly `uv sync --locked` and `make check`, enforced by reading the workflow | `test_ci_runs_exactly_the_local_gate` (the job's `run` steps, stripped, equal `CI_COMMANDS` in order and are the last two steps; no other step has `run`); `test_ci_step_sequence` (exactly four steps: checkout, setup-uv, then the two `run` steps); `test_makefile_check_is_the_gate` (the `Makefile` has a `check:` target and `UV_RUN := uv run --locked`, so `make check` keeps meaning the locked local gate) |
| A2 | The job runs on an arm64 runner | `test_ci_runs_on_arm64` (`runs-on == ARM64_RUNNER`; no `strategy` key, so no matrix can add other platforms) |
| A3 | Python comes from `.python-version`, not the workflow | `test_ci_python_comes_from_python_version_file` (section 5 item 4: no `python-version` input, no `actions/setup-python`, no `UV_PYTHON` or `python-version` in raw text, no `env` blocks); `test_python_version_file_matches_project` (`.python-version` is `3.13`, `requires-python` starts with `>=3.13`) |
| A4 | `permissions: contents: read` | `test_ci_permissions_read_only` (top-level equals `{"contents": "read"}`, job has no `permissions`) |
| A5 | References no secrets | `test_ci_references_no_secrets` (`\bsecrets\b` absent from raw `ci.yml`) |
| A6 | Uses `pull_request`, never `pull_request_target` | `test_ci_triggers` (`_triggers(ci)` keys are exactly `{"push", "pull_request"}`; `push.branches` is `["main", "impl"]` as a set; `pull_request` is `None` or `{}`); `test_workflow_never_uses_pull_request_target[<file>]` (parametrized over all workflows) |
| A7 | Every third-party action pinned to a full SHA with its version in a comment | `test_workflow_actions_pinned_to_full_sha[<file>]` (section 5 item 5, all workflows); `test_ci_pins_expected_actions` (the two `uses` in `ci.yml` are `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` and `astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2`, so the verified SHAs are the ones shipped; Dependabot updating them later must update this test in the same PR, which CI will flag) |
| A8 | A newer push cancels the run in progress; the job has a timeout | `test_ci_concurrency_cancels_in_progress`; `test_ci_job_has_timeout` |
| A9 | The workflow passes a workflow linter, run inside `make check` or by a test | `test_workflow_passes_actionlint[<file>]` (exit 0 and `[]`, all workflows); `test_actionlint_reports_errors` (negative control, section 5 item 13) |
| A10 | `docs/ARCHITECTURE.md`'s testing section says what CI runs and what it does not | Already written by this plan (Testing → "Continuous integration"). Checked by the verifier by reading it; no unit test (a test that greps prose would only test the wording). |

On A7: `test_ci_pins_expected_actions` makes every Dependabot action bump touch a test.
That is deliberate friction on a security-relevant change; if it proves noisy, a later
step may relax it to "pinned and commented" only. Say so in the test's docstring.

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | uv is installed with caching enabled | `test_ci_caches_uv` (setup-uv `with["enable-cache"] is True`) |
| B2 | Checkout does not leave the token in the workspace | `test_ci_checkout_does_not_persist_credentials` (`with["persist-credentials"] is False`) |
| B3 | Every workflow declares top-level permissions | `test_workflow_declares_permissions[<file>]` |
| B4 | Dependabot covers `uv` and `github-actions` weekly from the repo root | `test_dependabot_config` (section 5 item 14) |
| B5 | Workflow discovery cannot pass vacuously | `_workflow_files()` asserts non-empty and that `ci.yml` is included; exercised by every parametrized test |
| B6 | The lock includes the new dev tools for linux/aarch64 | Covered by the existing `test_required_environments_include_aarch64` plus `uv lock --check` in verification; no new test |

Every test above fails if its behaviour is removed: deleting a step, changing the
runner, adding `python-version`, widening permissions, adding a `secrets.` reference,
switching to `pull_request_target`, unpinning an action, dropping `cancel-in-progress`
or `timeout-minutes`, or breaking the YAML each turns at least one test red. Section 7.2
has the verifier prove this by mutation.

## 7. Verification

### 7.1 Gate and static checks (in the repo)

```bash
cd /home/sandy/dev/earmark
uv sync
make check
uv run pytest tests/test_ci.py -v           # every test listed in section 6 appears and passes
uv lock --check
uv run actionlint -version                  # 1.7.12
uv run actionlint -shellcheck= -pyflakes= .github/workflows/*.yml; echo "exit=$?"   # exit=0, no output
grep -rn 'pull_request_target\|secrets' .github/ || echo "none"                    # none
grep -n 'uses:' .github/workflows/*.yml     # two lines, each @<40 hex> # vX.Y.Z
git diff --stat                             # only the files in section 3 (+ this plan, ARCHITECTURE.md)
```

Re-verify the pins against GitHub (read-only API calls):

```bash
gh api repos/actions/checkout/git/ref/tags/v7.0.1 --jq '.object.type + " " + .object.sha'
#   commit 3d3c42e5aac5ba805825da76410c181273ba90b1
gh api repos/astral-sh/setup-uv/git/ref/tags/v10.3.3 --jq '.object.type + " " + .object.sha'
#   commit 68559e99cc807c1eb7e9238fd43a15bba460eec2
gh api repos/actions/checkout/compare/main...3d3c42e5aac5ba805825da76410c181273ba90b1 --jq .status    # behind or identical
gh api repos/astral-sh/setup-uv/compare/main...68559e99cc807c1eb7e9238fd43a15bba460eec2 --jq .status  # behind or identical
```

### 7.2 Mutation checks (in a scratch copy, never in the repo)

```bash
D=$(mktemp -d -p "$SCRATCH")     # SCRATCH = the verifier's scratchpad directory
cd /home/sandy/dev/earmark
git ls-files -z --cached --others --exclude-standard | xargs -0 cp --parents -t "$D"
cd "$D" && uv sync --locked >/dev/null
W=.github/workflows/ci.yml
B="$D/ci.bak"
cp "$W" "$B"
for m in \
  's/ubuntu-24.04-arm/ubuntu-latest/' \
  's/run: uv sync --locked/run: uv sync/' \
  's/run: make check/run: uv run pytest/' \
  's/contents: read/contents: write/' \
  's/^  pull_request:/  pull_request_target:/' \
  's/@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1/@v7/' \
  's/cancel-in-progress: true/cancel-in-progress: false/' \
  '/timeout-minutes/d' \
  's/enable-cache: true/enable-cache: false/' \
  's/persist-credentials: false/persist-credentials: true/' \
  's/runs-on: ubuntu-24.04-arm/runs-on: ubuntu-24.04-armm/' \
  's/^          enable-cache: true/          python-version: "3.12"\n          enable-cache: true/' \
; do
  sed -i "$m" "$W"
  uv run --locked pytest -q tests/test_ci.py >/dev/null 2>&1 && echo "NOT CAUGHT: $m" || echo "caught: $m"
  cp "$B" "$W"
done
# Secrets mutation: an extra step reading a secret
printf '      - run: echo "${{ secrets.X }}"\n' >> "$W"
uv run --locked pytest -q tests/test_ci.py >/dev/null 2>&1 && echo "NOT CAUGHT: secrets" || echo "caught: secrets"
cp "$B" "$W"
# Sanity: the restored file passes again
uv run --locked pytest -q tests/test_ci.py
cd / && rm -rf "$D"
```

Every line must read `caught:`. (The copy also serves as a clean-clone check: `uv sync
--locked` there builds `actionlint-py` from its sdist, which needs network on first
build.)

### 7.3 What cannot be verified here

GitHub Actions and Dependabot do not run in the sandbox. List as unverified:

- The live CI run on `ubuntu-24.04-arm` (uv install, managed Python download, cache
  save/restore, `make check` green on the runner).
- That Dependabot parses `dependabot.yml` and opens PRs (only after the file is on
  `main`).

The real test is the user's first push of `impl`. Before it, the user's `gh` token needs
the `workflow` scope (section 2.9). If that run fails, fixing it is a follow-up to this
step.

There is no live smoke beyond this; nothing in the step runs the service.

## 8. Deviations

1. **New dev dependencies** `actionlint-py` (workflow linter, section 2.5), `pyyaml` and
   `types-pyyaml` (tests must parse YAML; section 2.6). The brief allowed "for example a
   PyPI-packaged `actionlint`" but did not name the YAML parser. Alternatives considered:
   zizmor and check-jsonschema for linting (2.5.1); parsing YAML with regexes (fragile);
   ruamel.yaml (larger, and PyYAML is what step 12 will want anyway).
2. **actionlint runs from a test, not a `make lint` line.** Allowed by the brief ("inside
   `make check` or by a test"). Chosen so later workflows are linted automatically and so
   a negative control can prove the linter is live.
3. **Rules applied to every workflow, not just `ci.yml`.** SHA pinning with a version
   comment, a top-level `permissions` block, no `pull_request_target`, and a clean
   actionlint run are enforced for all files in `.github/workflows/`. This constrains
   the workflows steps 07, 08, 10, and 16 will add; they must comply. The "no secrets"
   and exact-command rules stay specific to `ci.yml`, because step 10's workflow needs a
   secret and the others run different commands.
4. **Extras in the workflow beyond the brief:** `persist-credentials: false` on
   checkout; a uv version range `>=0.13.9,<0.14` (section 2.4); `prune-cache` and
   `cache-python`; `cache-dependency-glob: uv.lock`. Each is explained in section 4.1.
5. **Extras in `dependabot.yml`:** groups and a 7-day cooldown (section 4.2). The brief
   asked only for weekly updates for both ecosystems; that part is tested, the tuning is
   not.
6. **`docs/ARCHITECTURE.md`**: added a "Continuous integration" subsection under
   Testing describing what CI runs, what it does not (opt-in markers, extras, GPU,
   container), the later workflows, the rules `tests/test_ci.py` enforces for every
   workflow, and Dependabot's default-branch behaviour. This satisfies the brief's last
   criterion; the implementer does not edit it.
7. **Runner is `ubuntu-24.04-arm`, not the newer `ubuntu-26.04-arm`.** The newer label
   exists, but actionlint 1.7.12 rejects it and the DGX Spark's base is Ubuntu 24.04.
   Revisit when actionlint learns the label and DGX OS moves.

## 9. Out of scope

- Scheduled or manual workflows for `-m network` (step 07), `-m slow` with the
  `transcribe` extra (step 08), and `-m llm` (step 10).
- Container image build in CI (step 16).
- Branch protection and marking the CI check as required: a repository setting the user
  changes in GitHub, not a file in the repo.
- Turning on the repository setting `sha_pinning_required` (currently `false`): the
  user may enable it; the tests already enforce pinning in-repo.
- Any x86-64 job, Python version matrix, or coverage reporting.
- Pushing `impl` or opening a PR. The user does that.
