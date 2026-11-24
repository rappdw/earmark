# Step 29: The mail contract as a tested seam

The brief is in `docs/PLAN.md`, "Step 29", with the operator's decisions in "System scope:
one repo for the whole system" above step 28, and pka-skills' answer on Python in
`docs/decisions/2026-11-24-pka-receiving-skill.md` section 5.

The step can be built, with one change to the brief's route: **the locked mypy (2.4.1)
cannot target Python 3.9**, so the 3.9 type check runs on a second, pinned mypy (1.20.2)
in its own small uv project, `tools/py39/` (2.1, D1). Nothing in this plan needs a product
decision. The interpretations the operator may want to look at are D5, D6 and D8.

Rules for the implementer that are not about the code itself:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Never run `uv python install` without `--no-bin`.** Without it, uv writes `python3.X`
  links into `~/.local/bin`, and uv then prefers them over `/usr/bin/python3.13` for the
  project itself (2.4). The Makefile passes `--no-bin`; do the same by hand.
- **Do not install anything into the repo's `.venv`.** `tools/py39/.venv` is created by
  `uv run --project tools/py39`, which is how the gate runs; that is expected.
- **Placeholders only**, in samples and docs: addresses at `example.org`, `example.net` or
  `example.com`; hosts such as `feeds.example.com` and `audio.example.net`; no real LAN
  address, host name, mail address or token.
- **The contract text does not change by one character** (section 5.1).

## 1. Outcome

When the step is done:

- **`contract/`** holds the mail seam: `README.md` (what the seam is, its versions, where
  the checker lives, how samples are made), `mail-v1.md` (the `contract: 1` text, moved
  verbatim from `docs/PLAN.md` and `docs/ARCHITECTURE.md`, which now link to it), and
  `samples/v1/` with ten sample emails and a `README.md` saying what each one shows.
- **The samples are built by the real mail code** (`earmark.mail.message.compose_mail`)
  from fixed inputs in `tests/contract_samples.py`. `make samples` regenerates them; a
  test fails when the mail code's output differs from them by one byte.
- **The contract checker** lives only at
  `plugins/earmark-pka/skills/earmark-receive/scripts/`: `check_mail.py`, an old-syntax
  launcher with the version guard, and `earmark_mail_contract.py`, the stdlib-only checker
  written to Python 3.9. It checks a whole `.eml` message, or the parts a PKA's `inbox`
  tools give it, against every mechanically checkable rule of `contract: 1`, and exits 0,
  1, 2 or 64 as pka-tools does. Step 30 puts the plugin manifests and the skill around it.
- **The gate enforces the 3.9 floor.** `make check` lints the checker with ruff at `py39`
  (per-file target version), type-checks it with mypy 1.20.2 at `--python-version 3.9`,
  and runs every checker test under CPython 3.13, 3.9.6 and 3.8 (the last only for the
  guard). CI runs exactly `make check`, as before.
- Tests prove that every sample passes, that a mutation breaking each rule fails, that the
  contract text and the checker each exist once, and the exit codes.

## 2. Findings

### 2.1 mypy cannot target 3.9 any more (checked 2026-11-24)

- `uv run --locked mypy --version` is `mypy 2.4.1`. `mypy --python-version 3.9` exits 2
  with "Python 3.9 is not supported (must be 3.10 or higher)". mypy's `CHANGELOG.md`
  (master, fetched 2026-11-24): mypy 2.0 "Drop Support for Targeting Python 3.9"; mypy
  1.20 already ran only on 3.10+, "it is still possible to type check code that needs to
  support Python 3.9 with the `--python-version 3.9` argument". PyPI: 1.20.2 (2026-04-21)
  is the last 1.x; `requires_python >=3.10`.
- **mypy 1.20.2 works for this.** In a scratch venv on 3.13, `mypy --strict
  --python-version 3.9` on a probe file reported every 3.10+ construct tried:
  `int | None` in a runtime annotation ("X | Y syntax for unions requires Python 3.10"),
  `match` ("Pattern matching is only supported in Python 3.10 and greater"),
  `itertools.pairwise`, `int.bit_count`, `zip(..., strict=True)`, `isinstance(x, int |
  str)`, `datetime.UTC`, `import tomllib`. So its bundled typeshed still gates the 3.9
  stdlib. For contrast, mypy 2.4.1 at `--python-version 3.10` reported only `datetime.UTC`
  and `tomllib`.
- A guard `if sys.version_info < (3, 9):` is not reported as unreachable by 1.20.2 with
  `warn_unreachable` (mypy treats version checks specially).
- The same strict settings as the main config (`strict`, `warn_unreachable`, and the five
  `enable_error_code` values) are all accepted by 1.20.2.
- **A separate uv project works** (prototyped in a scratch copy of the repo):
  `tools/py39/pyproject.toml` with `[tool.uv] package = false`, `requires-python =
  ">=3.13,<3.14"` and a dev group `mypy==1.20.2` locks to six packages (`mypy`, `librt`,
  `mypy-extensions`, `pathspec`, `typing-extensions`, the project itself). The root lock is
  unaffected (`uv lock --check` at the root still resolves 61 packages), and
  `uv run --locked --project tools/py39 mypy --config-file tools/py39/pyproject.toml
  plugins` passed from the repo root. `--config-file` is required: without it mypy 1.20.2
  would read the root `[tool.mypy]`, whose `pydantic.mypy` plugin is not installed there.
  The cache lands in `.mypy_cache/3.9/`, apart from the main run's `3.13/`.
- **Dead ends:** pinning the main project's mypy to 1.20.2 (loosens everything else); a
  conflicting dependency group in the root project (uv would swap mypy versions in the one
  `.venv`); `uvx mypy==1.20.2` (its dependencies are not locked); pyright (a second type
  checker and a node runtime).

### 2.2 ruff 0.17.6 per-file target version (checked)

- `uv run --locked ruff --version` is `ruff 0.17.6`. With
  `per-file-target-version = { "plugins/**/*.py" = "py39" }` under `[tool.ruff]` and the
  root `target-version = "py313"` unchanged, a scratch tree gave:
  - `plugins/p/match_stmt.py`: `invalid-syntax: Cannot use match statement on Python 3.9`;
  - `src/control.py` with the same `match`: no error, so the override is per file;
  - `plugins/p/pep604.py` (`def f(x: int | None)` with no `from __future__ import
    annotations`): `FA102`, but only when `FA` is selected. `FA` is not selected today.
    `ruff check --select FA src tests` reports nothing, so adding `FA` changes nothing for
    the 3.13 code.
- Parenthesized context managers are not flagged at `py39`; CPython 3.9's parser accepts
  them, so that is correct.
- The launcher's deliberate old style trips `UP036` (the version guard is "outdated" for
  py39), `E402` (imports after the guard), and, if written that way, `UP031` and `PTH`.
  Use per-line `# noqa: UP036` and `# noqa: E402` with a comment, and `pathlib` after the
  guard (section 5.3). `RUF100` keeps every `noqa` honest.

### 2.3 What a PKA actually has (checked against the `inbox` tool schemas)

The `inbox` MCP tools available in this session describe what a PKA sees: `read_message`
returns "headers, plain-text body, and any attachment descriptors
(filename/media_type/size/disposition)", and `read_attachment(message_id, index)` returns
one clean attachment's bytes. There is no raw RFC 5322 message. So the checker has two
input modes: a whole `.eml` (the samples, the tests) and the parts (the PKA; section 4.4).

### 2.4 Python 3.9 interpreters (checked in this sandbox)

- The sandbox has only `/usr/bin/python3.13` (3.13.5). uv 0.13.9 offers managed builds of
  CPython 3.9.6 to 3.9.25 and 3.8.20 for linux/aarch64 (`uv python list --all-versions
  --only-downloads`), and nothing older than 3.8. The network was reachable, and
  `UV_PYTHON_INSTALL_DIR=<scratch> uv python install --no-bin 3.9.6 3.9 3.8` worked.
  `uv python find --no-project --managed-python 3.9.6` prints the interpreter's path
  (`--no-project` silences a warning about the project's `requires-python`).
- **Trap:** the first install without `--no-bin` created `~/.local/bin/python3.13`,
  `python3.9` and `python3.8` links, and the next `uv run` in a project picked
  `~/.local/bin/python3.13` (3.13.18) over `/usr/bin/python3.13`. The planner removed
  those three links. Always pass `--no-bin`.
- **3.9.6 is the floor version tested**, because it is the `python3` that macOS's Command
  Line Tools ship, the likeliest 3.9 on a PKA host, and the oldest 3.9 uv offers here. 3.8
  is tested only for the version guard.
- CI: `ubuntu-24.04-arm` has no 3.9 on `PATH`; uv downloads it, and setup-uv's existing
  `cache-python: true` caches it. `tests/test_ci.py` forbids `actions/setup-python` and the
  text `python-version` in `ci.yml`, so uv is the only route anyway.

### 2.5 The email package across versions (measured)

- A prototype ran the real `compose_mail` on four inputs, including a hostile non-ASCII
  title capped to a 120-character subject and a long non-ASCII upload name, under 3.13.5
  (the sandbox venv) and 3.13.18 (a scratch venv from `uv export`; the version uv picks in
  CI). **The bytes were identical.** So samples can be compared byte for byte, and nothing
  needs masking (D6).
- CPython 3.9.6, 3.9.25 and 3.13 parse those messages the same with `email.policy.default`:
  the same decoded subject, attachment names, media types, charsets and decoded bytes, and
  no defects on any header or part.
- **Defects appear only after decoding.** `InvalidBase64CharactersDefect` and
  `InvalidBase64PaddingDefect` are added to a part's `defects` when
  `get_payload(decode=True)` runs, on 3.9.6 and on 3.13. The checker decodes every part
  before it looks at `defects`.
- **3.9 traps for the checker:** `datetime.fromisoformat` on 3.9 rejects a `Z` suffix and
  accepts fractions of exactly 3 or 6 digits, so timestamps are parsed by regular
  expression and built by hand. `unicodedata` is Unicode 13 on 3.9 and 15.1 on 3.13, so a
  check on `Cn`, `Cf` or `Co` would answer differently by version; the checker uses only
  `Cc`, `Zl` and `Zp`, which are stable. No `zip(strict=)`, `str | None` at run time,
  `match`, `itertools.pairwise`, `datetime.UTC`.

### 2.6 The launcher pattern (prototyped)

The brief wants the guard first, in old syntax, and allows `from __future__ import
annotations`. Both cannot hold in one file: the future import must be the first statement,
and on 3.6 it is itself a `SyntaxError`. pka-tools has the same split (a launcher and a
package). A two-file prototype was run with `-I`:

- 3.8.20: printed "check_mail.py needs Python 3.9 or newer; this is 3.8" and exited 2;
- 3.9.6, 3.9.25 and 3.13: imported the module (which uses the future import and
  `int | None` annotations) and ran;
- with `sys.dont_write_bytecode = True` set before the import, no `__pycache__` was
  written next to the module;
- `-I` leaves the script's directory off `sys.path`, so the launcher must add it itself.

`ast.parse(..., feature_version=...)` on 3.13 rejects walrus and positional-only
parameters for old versions, but not `f"{x=}"`. It is best effort, so the launcher test
also walks the tree (section 6.5).

### 2.7 The contract text and its two copies

- `docs/PLAN.md` lines 669–735 and `docs/ARCHITECTURE.md` lines 813–879 (commit `0d59b64`)
  are identical: 67 lines from `- **Both files**` to `    newer body wins.`. Their sha256,
  taken as those lines joined with LF and ending in one LF, is
  `7361f3d0fb20fa3edb0c5b98529c0c998532ba035f53ac8392fb47aea9dcd82d`.
- 48 of the 67 lines are at least 40 characters long. Among all tracked `*.md` files,
  only those two documents contain any of them. So "the text exists in one place" can be
  tested by scanning every Markdown file, step plans included.
- Each copy is preceded by its own preamble: PLAN's "**The mail contract (pka-skills,
  `contract: 1`, 2026-11-23).** Status: **DRAFT**…" paragraph, and ARCHITECTURE's
  "Provenance" and "Status" bullets. The preambles are history and status, not contract
  rules, and stay where they are (5.1).

### 2.8 Step 28's doc tests when `plugins/` and `contract/` appear (read `tests/test_docs.py`)

- `test_architecture_repo_layout_matches_repo` passes an existing path whatever its step.
  So `plugins/earmark-pka/skills/earmark-receive/scripts/` and `contract/...` existing
  while step 29 is still `todo` pass, and so does everything step 30 owns
  (`plugin.json`, `SKILL.md`, `marketplace.json`), which is still missing under a `todo`
  step.
- **`test_planned_paths_are_marked_planned` breaks:** it asserts `"contract/" in names`,
  and `contract/` stops being planned once it exists. Step 29 must change that assertion.
- **It also goes slack.** The aliases `earmark-pka` and `earmark-receive` are planned
  "while `plugins/earmark-pka/`" (resp. `.../earmark-receive/`) "is missing". Step 29
  creates both directories for the checker, so a doc could then call the unbuilt plugin
  by name with no step. This plan keys those aliases on the files step 30 creates
  (`plugins/earmark-pka/.claude-plugin/plugin.json`,
  `plugins/earmark-pka/skills/earmark-receive/SKILL.md`). Then **every paragraph in the
  README, `CLAUDE.md`, the `docs/` guides or ARCHITECTURE that names the checker's path
  must also say "step 30"**, because the path contains `earmark-pka`.
- `test_architecture_repo_layout_covers_top_level` requires every top-level directory in
  the layout. The planner added `tools/py39/` with `(step 29)` to ARCHITECTURE's layout
  (D9); the doc tests still pass (131 passed).
- `test_architecture_mail_contract_provenance` splits ARCHITECTURE's contract section at
  `- **Both files**`; once that text moves, the split returns the whole section. The test
  is updated (6.6).

### 2.9 What the samples must mirror

`Pipeline._compose` (`src/earmark/pipeline/runner.py` line 262) calls `compose_mail(job,
episode, transcript, first_shared_at=…, sender=mailer.sender, date=clock(),
max_attachment_bytes=…)` with no `boundary`, so the boundary is `=_earmark_<job_id>` and
the `Message-ID` is `<earmark.<job_id>@<sender domain>>`. A resend recomposes the same job
at a later clock: only `Date` changes. The generator makes the same call. Audio episodes
come from `earmark.sources.audio_episode(job, sha256)`, as in the runner.

### 2.10 Not verifiable here

The checker running inside a PKA (step 30, then runbook phase 12), and AMAP's handling of
the samples. CI's own run of the floor tests is seen only after the push. Everything
else, including the 3.9.6 and 3.8 runs, ran in this sandbox, which has network access for
the one-time downloads.

## 3. Files

| File | Change |
|---|---|
| `contract/README.md` | New: the seam, versions, checker, samples, what is not checkable (4.6) |
| `contract/mail-v1.md` | New: the `contract: 1` text, verbatim, between markers (4.6) |
| `contract/samples/v1/*.eml` | New: ten generated sample emails (4.5) |
| `contract/samples/v1/README.md` | New: one row per sample (4.5) |
| `plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py` | New: the launcher (4.2) |
| `plugins/earmark-pka/skills/earmark-receive/scripts/earmark_mail_contract.py` | New: the checker (4.3, 4.4) |
| `tools/py39/pyproject.toml`, `tools/py39/uv.lock` | New: mypy 1.20.2 and its 3.9 config (4.1) |
| `tests/contract_samples.py` | New: the fixed inputs and the generator (4.5) |
| `tests/checker_helpers.py` | New: running the checker, interpreters, sample parts, mutations (4.7) |
| `tests/test_mail_checker.py` | New: samples pass, mutations fail, exit codes, output (6.2–6.4) |
| `tests/test_contract_samples.py` | New: reproduction, sample set, relations (6.1) |
| `tests/test_checker_floor.py` | New: one copy, imports, launcher syntax, gate wiring, ruff and mypy probes (6.5) |
| `tests/test_docs.py` | Contract-text and pointer tests; step 28's planned-names fix; `PLANS` gains this plan (6.6) |
| `tests/test_ci.py` | The cache glob covers `tools/py39/uv.lock` (6.5) |
| `pyproject.toml` | `[tool.ruff]` gains `per-file-target-version`; lint `select` gains `FA` (4.1) |
| `Makefile` | Floor mypy in `typecheck`; floor interpreters for `test`; `samples` (4.1) |
| `.github/workflows/ci.yml` | `cache-dependency-glob` lists both locks; nothing else (4.1) |
| `.gitattributes` | New: `/contract/samples/** -text`, so the CRLF samples are never converted |
| `docs/PLAN.md` | Re-scope: the contract text replaced by a pointer (5.1) |
| `docs/ARCHITECTURE.md` | System (parts, seams, new "The mail seam"), contract pointer, Testing, CI, Settled questions, Known risks (4.8) |
| `CLAUDE.md`, `README.md` | The contract's new home, the checker rule, `make samples` (4.8) |

Nothing else changes: not `src/`, `deploy/`, the Dockerfiles, `compose.yaml`,
`.env.example`, `uv.lock`, `.github/dependabot.yml`, `docs/runbook-spark.md` or
`docs/deploy.md`. No `.claude-plugin/` directory and no `SKILL.md` are created (step 30).

## 4. Interfaces

### 4.1 The gate

**`pyproject.toml`** (two additions, nothing removed):

```toml
[tool.ruff]
target-version = "py313"
per-file-target-version = { "plugins/**/*.py" = "py39" }
```

and `"FA"` added to `[tool.ruff.lint] select`. `[tool.mypy]` is unchanged
(`python_version = "3.13"`, `files = ["src", "tests"]`).

**`tools/py39/pyproject.toml`**:

```toml
# The type checker for code written to the PKA's Python 3.9 floor (the contract checker in
# plugins/). mypy 2 cannot target 3.9, so this stays on 1.20.2, the last release that can.
# Not covered by Dependabot on purpose. See docs/steps/29-mail-contract-seam.md 2.1.
[project]
name = "earmark-py39-gate"
version = "0"
requires-python = ">=3.13,<3.14"
dependencies = []

[dependency-groups]
dev = ["mypy==1.20.2"]

[tool.uv]
package = false
required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]

[tool.mypy]
python_version = "3.9"
strict = true
warn_unreachable = true
enable_error_code = [
  "ignore-without-code",
  "redundant-expr",
  "truthy-bool",
  "possibly-undefined",
  "deprecated",
]
```

Create the lock with `uv lock --project tools/py39`.

**`Makefile`** (`UV_RUN` and `check:` unchanged):

```make
UV_RUN := uv run --locked
FLOOR_RUN := uv run --locked --project tools/py39
# The contract checker's interpreters: the PKA floor (macOS's python3), and one below it
# for the version guard. --no-bin: never put python3.X links in ~/.local/bin.
CHECKER_PYTHONS := 3.9.6 3.8
FIND_PYTHON := uv python find --no-project --managed-python

.PHONY: check lint fmt typecheck test checker-pythons samples
check: lint typecheck test

typecheck:
	$(UV_RUN) mypy
	$(FLOOR_RUN) mypy --config-file tools/py39/pyproject.toml plugins

test: checker-pythons
	MAIL_CHECKER_REQUIRE_FLOOR=1 \
	MAIL_CHECKER_PYTHONS="$$($(FIND_PYTHON) 3.9.6):$$($(FIND_PYTHON) 3.8)" \
	$(UV_RUN) pytest

checker-pythons:
	uv python install --no-bin $(CHECKER_PYTHONS)

samples:
	$(UV_RUN) python -m tests.contract_samples
```

`lint` and `fmt` stay as they are. Ruff already covers `plugins/` through `ruff check .`
and `ruff format --check .`.

**`ci.yml`**: only the setup-uv `cache-dependency-glob` changes, to a block listing
`uv.lock` and `tools/py39/uv.lock`. The run steps stay exactly `uv sync --locked` and
`make check`.

**Environment variables read by the tests** (not `EARMARK_*`, which `conftest` strips):

| Variable | Meaning |
|---|---|
| `MAIL_CHECKER_PYTHONS` | `:`-separated extra interpreters to run the checker under, besides `sys.executable` |
| `MAIL_CHECKER_REQUIRE_FLOOR` | `1`: fail unless those include a 3.9 and a pre-3.9 interpreter |

### 4.2 The launcher, `check_mail.py`

In this order, and with nothing else at module level:

1. a docstring;
2. `import sys`;
3. the guard, in syntax any Python 3 parses (no f-strings, annotations, walrus, `print`
   statement or function):
   ```text
   if sys.version_info < (3, 9):  # noqa: UP036 -- the guard is the point; see the docstring
       sys.stderr.write("check_mail.py needs Python 3.9 or newer; this is "
                        + ".".join(str(n) for n in sys.version_info[:2]) + "\n")
       sys.exit(2)
   ```
4. `sys.dont_write_bytecode = True`;
5. `from pathlib import Path  # noqa: E402`;
6. `sys.path.insert(0, str(Path(__file__).resolve().parent))`;
7. `import earmark_mail_contract  # noqa: E402`;
8. `sys.exit(earmark_mail_contract.main(sys.argv[1:]))`.

The scripts directory holds exactly these two files, so inserting it first on `sys.path`
cannot shadow a standard library module.

### 4.3 The checker module, `earmark_mail_contract.py`

Starts with `from __future__ import annotations`. Imports only the standard library
(`argparse`, `base64` if needed, `dataclasses`, `datetime`, `email`, `email.message`,
`email.policy`, `hashlib`, `json`, `pathlib`, `re`, `sys`, `unicodedata`,
`urllib.parse`). Public surface:

```text
CONTRACT: Final = 1
EXIT_OK, EXIT_FAILED, EXIT_PREREQUISITE, EXIT_USAGE = 0, 1, 2, 64

@dataclass(frozen=True)
class Rule:
    id: str            # "note.hash"
    needs: str         # "message" | "media-type" | "subject" | "body" | "attachments"
    description: str   # one ASCII sentence, citing the contract clause

RULES: tuple[Rule, ...]          # every rule of 4.4, in that order

@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    media_type: Optional[str]    # "text/markdown; charset=utf-8" style, or None if unknown

@dataclass(frozen=True)
class Mail:
    attachments: tuple[Attachment, ...]
    subject: Optional[str]       # decoded; None: not given
    body: Optional[str]          # the text/plain body; None: not given
    message_id: Optional[str]    # message mode only
    raw: Optional[email.message.EmailMessage]  # message mode only

@dataclass(frozen=True)
class Failure:
    rule: str
    file: Optional[str]          # "note" | "instructions" | None
    message: str                 # ASCII, never quotes text from the mail (5.4)

@dataclass(frozen=True)
class Report:
    failures: tuple[Failure, ...]
    skipped: tuple[str, ...]     # rule ids not run because an input was not given
    summary: dict[str, Optional[str]]  # 4.4 "Summary"

def read_message(data: bytes) -> Mail
def check(mail: Mail) -> Report
def main(argv: list[str]) -> int
```

`Optional` from `typing` or `X | None` (both fine under the future import; ruff's `UP`
rules at py39 will ask for `X | None` in annotations, which is fine because annotations are
never evaluated). Nothing evaluates an annotation at run time: no `get_type_hints`, no
`dataclasses` field types that are evaluated.

**Command line** (`argparse`, with `ArgumentParser.error` overridden to print usage and the
message to stderr and exit 64; `--help` exits 0):

```text
python3 -I check_mail.py message [--json] PATH
python3 -I check_mail.py files [--json] --note PATH --note-name NAME
                         --instructions PATH --instructions-name NAME
                         [--note-media-type TYPE] [--instructions-media-type TYPE]
                         [--subject-file PATH] [--body-file PATH]
python3 -I check_mail.py rules [--json]
```

- `message`: `PATH` is an RFC 5322 message (`.eml`, CRLF or LF).
- `files`: the attachments' bytes in files, their attachment names given separately, so the
  skill never has to create a file under an untrusted name. Subject and body come from
  files, never from the command line, because they hold third-party text and the PKA runs
  this through a shell. `--subject-file` is UTF-8 with one trailing newline stripped;
  `--body-file` is UTF-8. Rules whose input is not given are skipped and listed.
- `rules`: prints `RULES` (`id<TAB>description` lines, or a JSON list of
  `{"rule", "needs", "description"}`), exits 0.

**Exit codes** (pka-tools' set):

| Code | When |
|---|---|
| 0 | every rule that ran passed (some may be skipped in `files` mode) |
| 1 | at least one rule failed; also an unexpected exception inside the checker, reported as rule `internal` (5.5) |
| 2 | prerequisite missing: Python older than 3.9 (the launcher), or an input path that does not exist, is not a regular file, or cannot be read |
| 64 | usage error: no or unknown subcommand, a missing or extra argument, an unknown option |

**Output.** Without `--json`: one line per failure, `FAIL <rule> [<file>]: <message>`, then
either `OK: earmark mail, contract 1` or `FAILED: <n> check(s)`, then, if any,
`not checked (input not given): <rule ids>`. With `--json`, exactly one JSON object
(`ensure_ascii=True`, `sort_keys=True`) on stdout:

```json
{
  "checker": "earmark-mail-contract",
  "contract": 1,
  "ok": false,
  "failures": [{"rule": "note.hash", "file": "note", "message": "transcript_sha256 does not match the note's body"}],
  "skipped": ["subject.prefix"],
  "summary": {"type": "podcast", "guid": "feed:...", "job_id": "...", "transcript_sha256": "...",
              "note": "2026-11-20-....md", "instructions": "2026-11-20-....instructions.md",
              "message_id": "<earmark....@example.org>"}
}
```

Usage and prerequisite errors go to stderr as one line and print no report.

### 4.4 The rules

Each rule has an id, runs only when its inputs exist, and runs a value check only when the
key is present with the right type (so a mutation usually fails one rule). If the two
attachment names do not tell note and instructions apart, the per-file rules fall back to
attachment order (first note, then instructions). "String" below means a value that was
double-quoted in the frontmatter.

**The frontmatter grammar the checker accepts** (D5). Lines between the opening `---` and
the first later line that is exactly `---`:

- `key: "<YAML double-quoted scalar, one line>"`, with the YAML 1.2 escapes `\0 \a \b \t
  \<TAB> \n \v \f \r \e \<space> \" \/ \\ \N \_ \L \P \xHH \uHHHH \UHHHHHHHH`; an unknown
  escape, an unescaped `"`, a raw control character, or an escaped surrogate is a syntax
  failure;
- `key: <digits>` (an integer, `[0-9]+`);
- `key: YYYY-MM-DD` (a date, valid in the calendar);
- `key: YYYY-MM-DDTHH:MM:SS[.fraction](Z|+HH:MM|-HH:MM)` (an RFC 3339 timestamp, valid);
- `key:` followed by one or more `  - "<double-quoted scalar>"` lines (a list of strings).

Keys match `[a-z][a-z0-9_]*`. Anything else (comments, blank lines, plain or single-quoted
strings, flow lists, `null`, `~`, a duplicate key) fails `frontmatter.syntax`.

| Rule | Needs | Check | Contract clause |
|---|---|---|---|
| `message.defects` | message | after decoding every part, no header and no part has parser defects | the attachments arrive intact |
| `message.structure` | message | the top level is `multipart/mixed`; its leaves are one inline `text/plain` part and exactly two `attachment` parts | two attachments; the body is earmark's |
| `attachment.media-type` | media-type | each attachment's type is `text/markdown`, with no charset or `utf-8` | "the media type is `text/markdown`"; UTF-8 |
| `attachment.names` | attachments | exactly one name ends in `.instructions.md` | attachments 1 and 2 |
| `filename.note` | attachments | the note's name is `YYYY-MM-DD-<slug>.md`: a valid date, a slug of `[a-z0-9]` words joined by single hyphens, at most 80 characters, lower-case `.md` | Filenames |
| `filename.instructions` | attachments | the instructions' name is the note's stem plus `.instructions.md` | "the same date and slug" |
| `file.utf8` | attachments | the bytes decode as UTF-8 | Encoding |
| `file.bom` | attachments | no BOM (checking continues after it) | Encoding |
| `file.lf` | attachments | no CR byte | Encoding |
| `frontmatter.fence` | attachments | starts with `---\n` and has a closing `---` line | Frontmatter |
| `frontmatter.syntax` | attachments | every frontmatter line fits the grammar above; no duplicate key | Frontmatter; strings double-quoted on one line |
| `frontmatter.empty` | attachments | no `""` and no `key:` without items | "A value is never an empty string or null" |
| `string.control` | attachments | no string value holds a `Cc`, `Zl` or `Zp` character | "control characters stripped", "on one line" |
| `string.length` | attachments | every string value is at most 300 characters; `title` at most 200 | the caps |
| `note.keys` | attachments | no key outside the note table | the note table |
| `note.required` | attachments | every key required for the note's type is present (`show` for podcasts) | the note table |
| `note.omitted` | attachments | `show` and `apple_url` absent for audio; `source_url` absent unless `origin` is `url` | the note table |
| `note.types` | attachments | each present key has its table type (string, integer, date, timestamp, list of strings) | the note table |
| `note.type` | attachments | `type` is `podcast` or `audio` | `type` |
| `note.contract` | attachments | `contract` is `1` | `contract` |
| `note.source` | attachments | `source` is `earmark` | `source` |
| `note.origin` | attachments | `origin` is `apple_podcasts`, `upload` or `url`; `apple_podcasts` exactly when `type` is `podcast` (D8) | `origin` |
| `note.date` | attachments | `date` equals the filename's date | `date`: "the filename date" |
| `note.filename-date` | attachments | `date` equals `published` when present, else the calendar date of `shared_at` as written (D8) | Filename date |
| `note.duration` | attachments | `duration` matches `\d{2,}:[0-5]\d:[0-5]\d` | `duration` |
| `note.language` | attachments | `language` matches `[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*` | BCP 47 |
| `note.source-url` | attachments | `source_url` has no userinfo, no `?` and no `#` | `source_url` |
| `note.guid` | attachments | podcast: `feed:` plus one or more characters, or `apple:` plus digits; audio: `sha256:` plus 64 lower-case hex (D8) | `guid`; no audio hash for podcasts |
| `note.sha256-form` | attachments | `transcript_sha256` is 64 lower-case hex | `transcript_sha256` |
| `note.hash` | attachments | it equals the sha256 of the bytes after the closing `---\n` | `transcript_sha256` scope |
| `note.tags` | attachments | `tags` is exactly `[type]` | `tags` |
| `note.heading` | attachments | the body starts `# <show> — <title>\n\n## Transcript\n\n` (podcast) or `# <title>\n\n## Transcript\n\n` (audio), using the frontmatter values | Body |
| `note.paragraphs` | attachments | after that, with one final `\n` allowed, blocks split on `\n\n` are non-empty and each starts with `[hh:mm:ss] ` | Body |
| `instructions.keys` | attachments | exactly `type`, `contract`, `for`, `guid`, `job_id`, `shared_at` | Keys |
| `instructions.types` | attachments | `contract` an integer, `shared_at` a timestamp, the others strings | Keys |
| `instructions.type` | attachments | `type` is `earmark-instructions` | Keys |
| `instructions.contract` | attachments | `contract` is `1` | Keys |
| `instructions.for` | attachments | `for` is the note's filename | Keys |
| `instructions.guid` | attachments | `guid` equals the note's | Keys |
| `subject.prefix` | subject | starts with `[earmark] ` | Subject |
| `subject.length` | subject | at most 120 characters | Subject |
| `subject.control` | subject | no `Cc`, `Zl` or `Zp` character | "sanitised" |
| `body.names` | body | contains both attachment names | Body: "carries … the two filenames" |
| `body.job-id` | body | contains the instructions' `job_id` | Body: "carries the `job_id`" |
| `body.third-party` | body | contains `third-party` (any case) | Body: "says that the subject and the note contain third-party text" |
| `body.no-guid` | body | does not contain the note's `guid` | "The guid stays out of the body" |

Plus `internal` (5.5), which is not in `RULES`.

**Not mechanically checkable**, and listed as such in `contract/README.md`: that paragraphs
cover 30–60 seconds or one speaker turn (the note carries no segment ends or speakers);
that the body holds only text earmark generates (beyond the three body rules above); that
the instructions are the requester's words verbatim; that `job_id` is stable across a
resend and new on a re-share, and the de-duplication rules (relations between messages:
the sample tests check them for the service, and step 30's skill applies them).

**Summary.** `type`, `guid`, `job_id`, `transcript_sha256`, `note`, `instructions` and
`message_id`, each the value from the mail only if every rule about that value passed,
else `null`. `message_id` is the `Message-ID` header in message mode if it is printable
ASCII, else `null`; in files mode always `null`. This is what step 30 records for
de-duplication.

### 4.5 The samples and their generator

**`tests/contract_samples.py`**:

```text
SAMPLES_DIR: Final = ROOT / "contract" / "samples" / "v1"
SENDER: Final = "earmark@example.org"

@dataclass(frozen=True)
class SampleInputs:
    job: Job
    episode: Episode
    transcript: Transcript
    first_shared_at: datetime
    date: datetime                  # the injected clock: the Date header

SAMPLES: Final[dict[str, SampleInputs]]   # name (no extension) -> inputs
def build(name: str) -> bytes             # compose_mail(...).data, the runner's call (2.9)
def drifted(directory: Path = SAMPLES_DIR) -> list[str]   # names whose file differs or is missing
def write(directory: Path = SAMPLES_DIR) -> list[str]     # writes every sample, deletes stale *.eml
def main(argv: list[str] | None = None) -> int            # no args: write; --check: 1 if drifted
```

`build` calls `compose_mail(job, episode, transcript, first_shared_at=…, sender=SENDER,
date=…, max_attachment_bytes=DEFAULT_MAX_ATTACHMENT_BYTES)` with no `boundary`. Inputs are
written out in full in this module, with no `tests/factories.py` defaults, so a factory
change cannot move a sample. Podcast episodes are built with `Episode(...)` and
`episode_key(...)`; audio episodes with `earmark.sources.audio_episode(job, sha256)`. The
output never depends on the working directory, the clock or the environment. `main`
writes progress with `sys.stdout.write`, not `print` (ruff `T20`).

**The ten samples** (`contract/samples/v1/<name>.eml`). Recipient
`pka-personal@example.net` unless stated; job ids are 32 lower-case hex, `29` then zeros
then the sample's number. Titles, segment text and instructions are the implementer's,
in English, short (a few paragraphs), and placeholder-only.

| # | Name | Inputs that matter | What it shows |
|---|---|---|---|
| 1 | `podcast-feed` | podcast; `feed_guid` a verbatim URL-like guid at `feeds.example.com`; `published_at` known with a time; `language="en"`; `utc_offset_min=-480`; an `apple_url` with `?i=`; segments with a speaker change and a paragraph over 30 s; two lines of instructions | `feed:` guid, `published`, `apple_url`, paragraph merge, LF instructions |
| 2 | `podcast-feed.resend` | exactly sample 1's inputs, `date` 30 minutes later | a resend keeps `Message-ID` and `job_id`; only `Date` differs |
| 3 | `podcast-feed.reshare` | sample 1's episode, transcript and `first_shared_at`; new job id, a later `job.shared_at`, new instructions; later `date` | same `guid`, new `job_id`; the note is byte-identical to sample 1's |
| 4 | `podcast-feed.retranscription` | sample 1's episode and `first_shared_at`; one segment's text changed; new job id; later `date` | same `guid`, different `transcript_sha256` |
| 5 | `podcast-apple` | podcast; `feed_guid=None`; `published_date_only=True`; `language=None`; `utc_offset_min=None` | `apple:` guid; date-only `published`; `language` omitted; UTC offsets |
| 6 | `upload` | `source="upload"`, `title=None`, `upload_name` a `.m4a` with spaces; a fixed sha256; `utc_offset_min=60` | `sha256:` guid; title from the file stem; no `published`; date from `shared_at` |
| 7 | `direct-url` | `source="url"`, a path-only URL at `audio.example.net`, a `title` | `sha256:` guid; `source_url` present |
| 8 | `direct-url-query` | `source="url"`, a URL with `?download=1`, no `title` | `source_url` omitted; title from the last path segment |
| 9 | `empty-instructions` | podcast with a feed guid; `instructions=""` | the instructions file with an empty body |
| 10 | `hostile-title` | podcast; a title over 200 characters with CR, LF, tab, `"`, `\`, `=?utf-8?b?QmNjOiB4?=`, U+202E, U+200B, an emoji ZWJ sequence, a decomposed accent and `Bcc: evil@example.com`; a show with tabs; a `feed_guid` containing a newline (not verbatim); instructions holding `Bcc: evil@example.com` and the line `--=_earmark_<job id>`; recipient `pka-work@example.net` | sanitising, the 200 cap with `…`, the 120-character RFC 2047 subject, `apple:` fallback for a non-verbatim feed guid, inert instructions |

**`contract/samples/v1/README.md`**: a sentence that the samples are generated by
`make samples` from `tests/contract_samples.py`, never edited by hand, and that a test fails
when the mail code drifts; then a table `| Sample | Shows |` with one row per `.eml`,
first cell the file name in backticks (``` `podcast-feed.eml` ```).

**`.gitattributes`**: `/contract/samples/** -text`, so no checkout converts the CRLF wire
format.

### 4.6 `contract/README.md` and `contract/mail-v1.md`

**`contract/mail-v1.md`**:

1. `# The mail contract, version 1`
2. A short preamble: `contract: 1`, status **DRAFT** (final once the operator approves the
   receiving procedure, which is now the receiving skill, step 30); drafted by pka-skills
   on 2026-11-23, owned by this repo since 2026-11-24, moved here from `docs/PLAN.md` and
   `docs/ARCHITECTURE.md` in step 29 without change; a revision is a new
   `contract/mail-vN.md` with its own `samples/vN/`, and this file stays, because v1 mail
   can still be waiting in an inbox.
3. `<!-- contract-v1:begin -->`, a blank line, the 67 lines exactly as in 2.7, a blank
   line, `<!-- contract-v1:end -->`.

Copy the block mechanically (for example `git show 0d59b64:docs/PLAN.md | sed -n
'669,735p'`), never by retyping.

**`contract/README.md`**: sections in this order:

- **What this is.** The mail seam: the service sends, AMAP (outside this repo) delivers,
  the receiving skill (step 30) checks and files. The request seam has no folder.
- **Versions.** A table `| Version | Text | Samples | Status |` with one row: `1`,
  `mail-v1.md`, `samples/v1/`, `DRAFT`. How a revision is made (a new file and folder, the
  old ones kept, `docs/PLAN.md` and `docs/ARCHITECTURE.md` updated in the same commit).
- **The checker.** Its path,
  `plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py`, and that this is its
  only copy (a test enforces it); the three command forms of 4.3; exit codes 0, 1, 2 and
  64; standard library only, Python 3.9 or newer, run as `python3 -I`; `check_mail.py
  rules` lists what it checks.
- **What the checker cannot check** (4.4).
- **Samples.** `samples/v1/README.md`, and `make samples`.

### 4.7 `tests/checker_helpers.py`

```text
CHECKER: Final = ROOT / "plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py"
SCRIPTS: Final = CHECKER.parent

@dataclass(frozen=True)
class Interpreter:
    path: str
    version: tuple[int, int]
    @property
    def id(self) -> str            # "py3.9", "py3.13"

def interpreters() -> list[Interpreter]   # sys.executable, then MAIL_CHECKER_PYTHONS in order,
                                          # each probed once with -I -c (cached)
def floor_interpreters() -> list[Interpreter]   # version >= (3, 9)
def old_interpreters() -> list[Interpreter]     # version < (3, 9)
def run_checker(python: Interpreter, *args: str) -> subprocess.CompletedProcess[str]
    # [python.path, "-I", str(CHECKER), *args], text, UTF-8, timeout 60, check=False
def report(result) -> dict[str, Any]      # json.loads(result.stdout), asserting one object

@dataclass(frozen=True)
class Parts:                              # a sample taken apart with the stdlib email package
    subject: str
    body: str
    note_name: str
    note: bytes
    instructions_name: str
    instructions: bytes

def split(data: bytes) -> Parts
def rebuild(parts: Parts, *, note_type: str = "text/markdown", ...) -> bytes
    # EmailMessage(policy=email.policy.SMTP), as compose_mail builds it, same headers and
    # boundary; rebuild(split(sample)) passes the checker
def write_parts(parts: Parts, directory: Path) -> list[str]   # files-mode arguments
def set_front(raw: bytes, key: str, line: str | None) -> bytes  # replace, add or (None) drop a line
def rehash(note: bytes) -> bytes          # rewrite transcript_sha256 to match the body
```

Probing an interpreter runs `[path, "-I", "-c", "import sys; sys.stdout.write('%d.%d' %
sys.version_info[:2])"]`.

### 4.8 Documentation changes

- **`docs/PLAN.md` "Re-scope".** Keep the paragraph "**The mail contract (pka-skills,
  `contract: 1`, 2026-11-23).** Status: **DRAFT**…", add a sentence that the text is in
  `contract/mail-v1.md`, moved there unchanged in step 29, and delete the 67 lines.
- **`docs/ARCHITECTURE.md`.**
  - *The three parts*, part 3's last cell: "Not built (step 30): no plugin manifest and no
    skill yet. The contract checker the skill will run exists (step 29) and is tested
    under CPython 3.9.6 and 3.13; it has never run in a PKA." It must keep `Not built` and
    the step 30 mention.
  - *The two seams*, the Mail row: contract cell "the mail contract, `contract: 1`, in
    `contract/mail-v1.md`"; tested-by cell names `contract/samples/v1/` (step 29), the
    byte-for-byte reproduction by the service's tests, the checker passing them on 3.9.6
    and 3.13, and the receiving skill's tests from step 30.
  - A new `### The mail seam` after "The two seams": `contract/` (text, samples, README),
    the checker's path and its two files, the three command forms and the exit codes, the
    3.9 floor and how the gate enforces it (ruff `py39` per file, mypy 1.20.2 in
    `tools/py39/`, the checker tests under 3.9.6 and 3.8), and that the plugin around it is
    step 30. Each paragraph naming the checker's path also says "step 30" (2.8).
  - *Delivering* → `#### The mail contract (`contract: 1`)`: keep the heading. Replace
    the section with the provenance and status bullets (updated: the source of truth is
    `contract/mail-v1.md` since step 29) and a link
    `[contract/mail-v1.md](../contract/mail-v1.md)`. No contract line stays.
  - *Decisions*, the Delivery row: add "the text is `contract/mail-v1.md`".
  - *Testing*: a paragraph on the contract tests (6.1–6.5), and in "What
    `tests/test_docs.py` checks": the contract text is pinned by hash and exists once, and
    the step plans scanned for placeholders are 27, 28 and 29.
  - *Continuous integration*: `make check` also installs uv-managed CPython 3.9.6 and 3.8
    (`--no-bin`, cached by setup-uv's `cache-python`) and runs the floor mypy, which syncs
    `tools/py39/.venv`; the cache key covers both locks.
  - *Settled questions*: "**Settled by step 29**", one bullet per decision in section 8.
  - *Known risks*: a new item, "The checker's 3.9 floor rests on a frozen mypy and on the
    tests": mypy 1.20.2 will not get newer typeshed, and a 3.10+ API in a branch the tests
    never run could pass both; and if a CPython 3.13 patch release changes how `email`
    folds headers, the sample test fails and the samples must be regenerated after
    checking the diff is only folding.
- **`CLAUDE.md`.**
  - The "Mail follows the contract exactly" rule: the text is `contract/mail-v1.md` (step
    29), pinned by a hash test; a revision is a new `contract/mail-vN.md` and
    `contract/samples/vN/`, with `docs/PLAN.md` and `docs/ARCHITECTURE.md` updated together.
    Drop "copied in `docs/PLAN.md` and `docs/ARCHITECTURE.md`".
  - A new rule after it, **The contract checker has one copy and a 3.9 floor** (step 29):
    its path; that the plugin and skill around it are step 30; stdlib only, nothing from
    `earmark`; ruff `py39`, mypy 1.20.2 in `tools/py39/` (never bumped: mypy 2 cannot
    target 3.9), and the tests under 3.9.6 and 3.8; never copy it.
  - *Commands*: `make samples   # regenerate contract/samples/v1/ from the mail code`.
  - *Environment*: `make check` downloads uv-managed CPython 3.9.6 and 3.8 the first time
    (with `--no-bin`), and syncs `tools/py39/.venv`.
- **`README.md`**, "The system", part 3: "Not built yet (step 30). Its contract checker
  exists (step 29) …". It must keep `Not built yet` and mention steps 29 and 30. The
  development section, if it lists make targets, gains `make samples`.

## 5. Behaviour and edge cases

### 5.1 The contract text is moved, not revised

- The 67 lines go into `contract/mail-v1.md` byte for byte, and out of `docs/PLAN.md` and
  `docs/ARCHITECTURE.md`. No key, file name, format or rule changes, so this is not a
  revision and `contract: 1` stays.
- The preambles stay in their documents, updated only where they said "From step 29 its
  source of truth is…" or "Until then these two copies are it".
- **The hash test** (6.6) pins the block forever. A later edit to it fails the test, which
  is the point: a change is `mail-v2.md`.
- Do not quote contract lines in other documents, including docs and comments written in
  this step. Refer to rules by name ("the `source_url` rule").

### 5.2 Samples

- **Nothing is masked** (D6). `Date` is an input (the injected clock), the boundary and
  `Message-ID` derive from the job id, and the bytes were identical on 3.13.5 and 3.13.18
  (2.5). The comparison is byte-exact.
- **A resend shows the kept `Message-ID`** by being a separate sample with the same inputs
  and a later `Date`. The test proves the two files differ only in the `Date` line.
- Samples are wire format: CRLF, ASCII, base64 attachments. `.gitattributes` keeps git
  from converting them. Never edit one by hand; run `make samples` and review the diff.
- `write` deletes `.eml` files in the folder that are not in `SAMPLES`, and never touches
  `README.md`.

### 5.3 The launcher

- The guard is the first statement after `import sys`, and nothing before it uses syntax
  newer than Python 3.0. That covers 2.7 to 3.8 too: they print the message and exit 2.
- `sys.dont_write_bytecode = True` comes before the module import. `-I` ignores
  `PYTHONDONTWRITEBYTECODE`, so this is what keeps `__pycache__` out of the plugin cache.
- The script directory goes first on `sys.path`, so the checker imports its own module
  and not one of the same name elsewhere. The folder holds only the two files (a test
  enforces it).

### 5.4 Untrusted text in the output

The PKA reads the checker's output. Failure messages are fixed ASCII sentences that may
name a key (already matched against `[a-z][a-z0-9_]*`, cut to 40 characters) or an
attachment name that passed its filename rule. They never quote a title, show, guid, URL,
subject, body or instructions text. The JSON summary carries `guid` (publisher text for a
`feed:` guid) only after its rules passed, JSON-escaped with `ensure_ascii`. The skill
treats it as data.

### 5.5 Failing safe

- Any exception that escapes the checks is caught in `main` (`except Exception`, with
  `# noqa: BLE001` and a comment), reported as rule `internal` with the exception's class
  name only, and exits 1, so a checker bug never makes mail look valid. Tests assert that
  no sample and no mutation reports `internal`.
- Header access, decoding and parsing are inside that net, so a malformed message from
  3.9's older `email` package fails a rule rather than crashing.
- Decode every part before reading `defects` (2.5).
- Timestamps: regular expression, then `datetime(…, tzinfo=timezone(timedelta(…)))`. Do
  not use `datetime.fromisoformat` for them (2.5). Dates: `date.fromisoformat` on a
  `\d{4}-\d{2}-\d{2}` match, which works on 3.9.
- Character checks use only `Cc`, `Zl` and `Zp` (2.5).

### 5.6 Interpretations (D5, D8)

- **The frontmatter subset.** The contract says "YAML". The stdlib has no YAML parser, so
  the checker accepts the form earmark emits (4.4), where every string is double-quoted.
  A hand-made note with a plain `type: podcast` fails `frontmatter.syntax`, though it is
  valid YAML. earmark is the only sender, so no real mail is refused.
- **`type` and `origin` agree**, `date` follows `published` or `shared_at`, an `apple:`
  guid is digits, and "says that the subject and the note contain third-party text" is
  the word `third-party`. These are readings of the contract's wording, checked against
  what step 25 emits. They do not change the contract.

### 5.7 Doc-test traps (2.8)

- Every paragraph in README, `CLAUDE.md`, `docs/*.md` guides and ARCHITECTURE that
  contains `earmark-pka` or `earmark-receive` must also contain "step 30" or "steps 29 and
  30", since the aliases now track `plugin.json` and `SKILL.md`.
- Do not write `.claude`, "Claude" or `vault` in README or the `docs/` guides
  (`REMOVED` scan, step 28 2.2).
- Do not link to a path that does not exist; links into `contract/` are fine once it
  exists.

## 6. Tests

Fixtures: the ten samples, generated by the implementer with `make samples` and
committed. No network fixtures. The 3.9.6 and 3.8 interpreters come from `make
checker-pythons` (`uv python install --no-bin`). The default `uv run pytest` runs every
checker test under the 3.13 interpreter only; `make check` and CI add 3.9.6 and 3.8.

### 6.1 Brief: "the mail code reproduces every sample", and the sample set

`tests/test_contract_samples.py`:

| Test | Proves |
|---|---|
| `test_samples_reproduce[name]` | `build(name)` equals `SAMPLES_DIR/<name>.eml` byte for byte, for each of the ten |
| `test_sample_set_is_complete` | `SAMPLES` keys, the `.eml` files and the README table's first cells are the same ten names; the brief's kinds are among them |
| `test_generator_check_and_write(tmp_path)` | with a copy of the folder, one sample altered, one deleted and a stale `x.eml` added: `drifted` names exactly the first two; `write` fixes both and removes `x.eml`; `main(["--check"])` returns 1 then 0 |
| `test_sample_properties` | per sample, from `split()` and the frontmatter: the guid prefixes (`feed:`, `apple:`, `sha256:`); `published` present in 1, date-only in 5, absent in 6–8; `language` absent in 5; `source_url` present in 7 and absent in 8; the empty instructions body in 9; in 10 the title is 200 characters ending `…` with no `Cc`, the subject is 120 characters and RFC 2047 encoded on the wire, and the guid is `apple:` |
| `test_resend_keeps_message_id` | samples 1 and 2 have equal `Message-ID`, `job_id` and attachments, different `Date`, and their bytes differ only in the `Date:` line |
| `test_reshare_and_retranscription` | 3: same `guid`, new `job_id` and `Message-ID`, note bytes equal to 1's, instructions differ; 4: same `guid`, new `job_id`, different `transcript_sha256` |
| `test_samples_use_placeholders_only` | every address in the raw samples and in their decoded attachments is at `example.org`, `example.net` or `example.com`; no IPv4 address appears |
| `test_samples_are_wire_format` | each file is ASCII, every line ends in CRLF; `.gitattributes` holds `/contract/samples/** -text` |

### 6.2 Brief: "Every sample passes the checker"

`tests/test_mail_checker.py`, parametrized over `floor_interpreters()`:

| Test | Proves |
|---|---|
| `test_samples_pass_message_mode[python-name]` | `message --json` exits 0, `ok` true, no failures, no skips; the summary's `guid`, `job_id`, `transcript_sha256`, file names and `message_id` equal the values read from the sample |
| `test_samples_pass_files_mode[python-name]` | `files --json` with all optional inputs exits 0; only the `message`-needing rules are skipped |
| `test_rebuild_round_trip` | `rebuild(split(sample))` passes, so a mutation's failure comes from the mutation |

### 6.3 Brief: "For each contract rule, a mutated sample breaking that rule makes the checker fail"

`test_mutation_fails[python-case]`: each case is a base sample and a mutation, run in
message mode (and in files mode for the `subject.*`, `body.*` and `attachment.media-type`
cases); it asserts exit 1 and the expected rule among the reported failures.
`test_every_rule_has_a_mutation` asserts that the case table's rules equal `rules --json`'s
ids, so a new rule without a mutation fails. Mutations that change the note's body call
`rehash` unless they target `note.hash`; mutations that change the note's guid or name
change the instructions to match, so the rule under test is the one that breaks. Cases (1
is `podcast-feed`, 6 `upload`, 7 `direct-url`):

| Rule | Base | Mutation |
|---|---|---|
| `message.defects` | 1 | three characters of the note's base64 replaced with `@@@` |
| `message.structure` | 1 | a third attachment; separately, an extra inline `text/html` part |
| `attachment.media-type` | 1 | the note as `text/plain`; separately, charset `iso-8859-1` |
| `attachment.names` | 1 | the instructions renamed `<stem>.notes.md` |
| `filename.note` | 1 | uppercase slug; `2026-02-30`; `.MD`; an 81-character slug; a double hyphen (each with matching instructions name and `for`) |
| `filename.instructions` | 1 | the instructions named for another stem |
| `file.utf8` | 1 | a `\xff` byte in the note's title line; separately in the instructions body |
| `file.bom` | 6 | a BOM before the note |
| `file.lf` | 7 | CRLF line endings in the instructions |
| `frontmatter.fence` | 1 | the closing `---` removed |
| `frontmatter.syntax` | 1 | single-quoted `title`; a duplicate key; a comment line; `tags: ["podcast"]`; `type: podcast` unquoted |
| `frontmatter.empty` | 1 | `language: ""`; `tags:` with no items |
| `string.control` | 1 | `title` with `\u0007`; separately ` ` |
| `string.length` | 1 | a 201-character title; a 301-character show |
| `note.keys` | 1 | `summary: "x"` added |
| `note.required` | 1 | `duration` dropped; `show` dropped |
| `note.omitted` | 6 | `show` added; `source_url` added (also on 1) |
| `note.types` | 1 | `contract: "1"`; `date: "2026-11-20"`; `shared_at` without offset; `tags: "podcast"` |
| `note.type` | 1 | `type: "video"` |
| `note.contract` | 1 | `contract: 2` |
| `note.source` | 1 | `source: "other"` |
| `note.origin` | 1 | `origin: "upload"`; `origin: "youtube"` |
| `note.date` | 1 | `date` a different valid date |
| `note.filename-date` | 1 | the file name, `date` and `for` all moved one day from `published` |
| `note.duration` | 1 | `duration: "1:02:03"` |
| `note.language` | 1 | `language: "english!"` |
| `note.source-url` | 7 | `?x=1` appended; userinfo; a fragment |
| `note.guid` | 1 | `podcast:1`; `apple:abc`; and on 6 `feed:x` and uppercase hex (instructions guid kept equal) |
| `note.sha256-form` | 1 | uppercase hex |
| `note.hash` | 1 | one transcript word changed, not rehashed |
| `note.tags` | 1 | `["audio"]`; two tags |
| `note.heading` | 1 | heading without the show |
| `note.paragraphs` | 1 | a paragraph without its marker; three newlines between two paragraphs |
| `instructions.keys` | 1 | `job_id` dropped; `note: "x"` added |
| `instructions.types` | 1 | `contract: "1"`; `shared_at` quoted |
| `instructions.type` | 1 | `type: "earmark-note"` |
| `instructions.contract` | 1 | `contract: 2` |
| `instructions.for` | 1 | `for` naming another file |
| `instructions.guid` | 1 | a different guid |
| `subject.prefix` | 1 | `earmark: …` |
| `subject.length` | 1 | 121 characters |
| `subject.control` | 1 | a subject containing U+0007 (an encoded word on the wire) |
| `body.names` | 1 | the note's name removed from the body |
| `body.job-id` | 1 | another job id in the body |
| `body.third-party` | 1 | the third-party paragraph removed |
| `body.no-guid` | 1 | the guid appended to the body |

### 6.4 Brief: "Its exit codes are tested: 0, 1, 2 and 64"

| Test | Proves |
|---|---|
| exit 0 | 6.2 |
| exit 1 | 6.3 |
| `test_exit_2_missing_input[python]` | `message` with a missing path, `message` with a directory, `files` with a missing `--note`: exit 2, stderr one line, stdout empty |
| `test_exit_2_old_interpreter[python]` | over `old_interpreters()` (3.8 under `make check`): exit 2, stderr names 3.9, stdout empty |
| `test_exit_2_guard_simulated` | always runs: `sys.executable -I -c` that sets `sys.version_info = (3, 8, 20, "final", 0)` and `runpy.run_path(CHECKER, run_name="__main__")`: exit 2 and the message |
| `test_exit_64_usage[python-args]` | no arguments; `bogus`; `message`; `message a b`; `message --nope x`; `files` without `--note-name`: exit 64, usage on stderr |
| `test_help_exits_0` | `--help` and `message --help` exit 0 |

Criteria this plan adds, in the same file:

| Test | Proves |
|---|---|
| `test_output_never_echoes_mail_text[python]` | tokens put in the title, show, instructions and body of failing mutations never appear on stdout in either output form; a guid token appears only as `summary.guid` |
| `test_rules_subcommand` | `rules` and `rules --json` list the same ids, each with a `needs` from 4.3 and a non-empty ASCII description; exit 0 |
| `test_no_internal_failures` | no sample and no mutation case reports `internal` (folded into 6.2 and 6.3 assertions) |
| `test_floor_interpreters_present_when_required` | if `MAIL_CHECKER_REQUIRE_FLOOR=1`: `interpreters()` includes a `(3, 9)` and one older than `(3, 9)`, else fails. Skipped, with the reason, when the variable is unset |

### 6.5 Brief: one copy, the 3.9 floor in the gate

`tests/test_checker_floor.py`:

| Test | Proves |
|---|---|
| `test_checker_has_one_copy` | walking `ROOT` (skipping `.git`, any `.venv`, caches and `__pycache__`), no file outside `SCRIPTS` is named `check_mail.py` or `earmark_mail_contract.py`, and no file outside it has the same bytes as either |
| `test_scripts_folder_holds_only_the_checker` | `SCRIPTS` contains exactly the two files |
| `test_checker_imports_only_stdlib` | the AST of both files: every import is in `sys.stdlib_module_names` or is `earmark_mail_contract`; nothing named `earmark` |
| `test_launcher_is_old_syntax` | the launcher parses with `ast.parse(..., feature_version=(3, 4))`, has no `JoinedStr`, annotation, `NamedExpr`, positional-only parameter or `Match`; its statements are, in order, the docstring, `import sys`, the guard `if` comparing `sys.version_info` with `(3, 9)` whose body ends in `sys.exit(2)`, then `sys.dont_write_bytecode = True` before the import of `earmark_mail_contract` |
| `test_checker_writes_no_bytecode(tmp_path)` | a copy of `SCRIPTS` run under each floor interpreter on a sample leaves no `__pycache__` |
| `test_ruff_lints_plugins_as_py39(tmp_path)` | the root `[tool.ruff]` table copied into a tmp `pyproject.toml`; the venv's `ruff check --no-cache` on `plugins/p/a.py` (a `match`) and `plugins/p/b.py` (`int \| None` without the future import) reports both; on `src/c.py` with the same `match`, nothing |
| `test_floor_mypy_rejects_newer_python(tmp_path)` | `uv run --offline --locked --project ROOT/tools/py39 mypy --config-file ROOT/tools/py39/pyproject.toml --cache-dir <tmp>` (without `VIRTUAL_ENV` in its environment) over one tmp file per construct of 2.1 reports an error in each, and passes a 3.9-clean control file |
| `test_floor_config_is_strict` | `tools/py39/pyproject.toml`: `mypy==1.20.2` exactly; `python_version = "3.9"`, `strict`, `warn_unreachable`, and `enable_error_code` at least the root's; `requires-python` equal to the root's. The root keeps `target-version = "py313"`, `python_version = "3.13"`, `files = ["src", "tests"]`, gains exactly `{"plugins/**/*.py": "py39"}`, and `select` contains `FA` and every code it had |
| `test_makefile_runs_the_floor` | `typecheck` runs `$(FLOOR_RUN) mypy --config-file tools/py39/pyproject.toml plugins` with `FLOOR_RUN := uv run --locked --project tools/py39`; `test` depends on `checker-pythons` and sets `MAIL_CHECKER_REQUIRE_FLOOR=1` and both interpreters; `checker-pythons` passes `--no-bin` and `3.9.6 3.8`; `samples` runs `python -m tests.contract_samples` |

`tests/test_ci.py`: `test_ci_cache_covers_both_locks` (setup-uv's
`cache-dependency-glob` lists `uv.lock` and `tools/py39/uv.lock`). All existing CI tests
stay as they are and pass: the run steps are still exactly `uv sync --locked` and `make
check`, which with `test_makefile_runs_the_floor` is what makes CI run the checker on 3.9.

**"A 3.10+ construct in it fails the gate"** is proved by the ruff and mypy probes above
and by the 3.9.6 runs, and the verifier also mutates the real checker (section 7).

### 6.6 Brief: "The contract text exists in one place"

`tests/test_docs.py`:

| Test | Proves |
|---|---|
| `test_contract_v1_text_is_pinned` | sha256 of `between_markers(contract/mail-v1.md, "contract-v1").strip("\n") + "\n"` is `7361f3d0fb20fa3edb0c5b98529c0c998532ba035f53ac8392fb47aea9dcd82d` |
| `test_contract_text_exists_once` | no `*.md` file in the repo other than `contract/mail-v1.md` (skipping `.git` and `.venv`) contains any line of the marked block that is at least 40 characters after stripping |
| `test_plan_and_architecture_point_to_contract` | both contain `contract/mail-v1.md`; ARCHITECTURE's `#### The mail contract` section contains no `- **Both files**` |
| `test_architecture_mail_contract_provenance` (updated) | the heading check stays; the needles are looked for in the whole section, plus `contract/mail-v1.md` and step 29 |
| `test_contract_readme` | `contract/README.md` names the checker's path, `0`, `1`, `2`, `64`, `3.9`, `python3 -I`, `mail-v1.md`, `samples/v1/`, `DRAFT`, `make samples`, and has a "cannot check" section |
| `test_contract_docs_use_placeholders_only[doc]` | `_assert_placeholders` on the three contract READMEs and `mail-v1.md` |
| `test_planned_paths_are_marked_planned` (updated) | the `earmark-pka` and `earmark-receive` aliases are planned while `plugin.json` and `SKILL.md` are missing; `assert "contract/" in names` becomes: every missing layout entry with a step has its derived name in `names` |
| `test_architecture_system_seams`, `test_architecture_parts_say_what_is_unbuilt`, `test_readme_system_section_says_what_is_unbuilt` (kept) | still pass with the 4.8 wording |
| `test_docs_use_placeholders_only` | `PLANS` gains `docs/steps/29-mail-contract-seam.md` |

## 7. Verification

From the repository, with the default uv Python directory or, to keep downloads in the
scratchpad, `export UV_PYTHON_INSTALL_DIR=<scratch>/pythons` first (never omit `--no-bin`
when installing by hand):

```bash
make check                                    # downloads 3.9.6 and 3.8 once; syncs tools/py39
GITHUB_ACTIONS=true make check
ls ~/.local/bin | grep -c python || true      # expect 0: nothing put on PATH
uv run --locked pytest tests/test_mail_checker.py tests/test_contract_samples.py \
  tests/test_checker_floor.py -q              # 3.13 only, the floor test skipped
uv run --locked python -m tests.contract_samples --check            # expect exit 0
git status --porcelain contract/              # expect nothing after --check
git show 0d59b64:docs/PLAN.md | sed -n '669,735p' | sha256sum       # expect 7361f3d0…d82d
python3 -I plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py \
  message --json contract/samples/v1/hostile-title.eml             # exit 0
"$(uv python find --no-project --managed-python 3.9.6)" -I \
  plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py rules   # exit 0
"$(uv python find --no-project --managed-python 3.8)" -I \
  plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py rules; echo $?  # 2
ls plugins/earmark-pka/skills/earmark-receive/scripts/              # exactly the two files
ls -a plugins/earmark-pka .claude-plugin 2>&1                       # no .claude-plugin anywhere
git status --porcelain -- src deploy Dockerfile Dockerfile.gpu compose.yaml .env.example \
  uv.lock .github/dependabot.yml docs/runbook-spark.md docs/deploy.md   # expect no output
git stash list | wc -l                        # same count as before
```

**Gate mutations** (required). Copy the tree without `.venv` and `.git` to the scratchpad
(`rsync -a --exclude .venv --exclude .git ./ <scratch>/earmark/`), and in the copy, one at
a time, with `make -C <scratch>/earmark check` (or the single relevant target):

1. add `match` to a function in `earmark_mail_contract.py`: `lint` fails;
2. use `datetime.UTC` (keep it lint-clean): `typecheck` fails;
3. use `int.bit_count()` in a path the tests run: `typecheck` fails, and so does the 3.9.6
   run;
4. move the guard below `sys.dont_write_bytecode = True` or turn its message into an
   f-string: `test_launcher_is_old_syntax` fails;
5. copy `check_mail.py` into `contract/`: `test_checker_has_one_copy` fails;
6. change one character in `contract/mail-v1.md`'s marked block: the hash test fails;
7. change the body template in `src/earmark/mail/message.py`: the sample tests fail;
8. delete the per-file target version from `pyproject.toml`: `test_floor_config_is_strict`
   and `test_ruff_lints_plugins_as_py39` fail.

**Static review.** Read `earmark_mail_contract.py` against 4.4 rule by rule; read the
mutation table in the tests against 6.3; read the samples' decoded attachments by eye
(`split` in a Python shell) for placeholders; diff ARCHITECTURE and PLAN to confirm only
the 67 lines and the stated preamble sentences moved.

**Live smoke.** The quickstart still works unchanged (the service is untouched); running
it is optional. Nothing of the PKA side can be run here: say so in the report.

## 8. Deviations

- **D1: The 3.9 type check uses mypy 1.20.2 in `tools/py39/`.** The brief's "mypy pass
  over `plugins/` with `--python-version 3.9`" is impossible with the locked mypy 2.4.1
  (2.1). `tools/py39/` is a separate, locked uv project (`package = false`) holding only
  that mypy and the 3.9 config. Chosen over pinning the main mypy (loosens every other
  check), a conflicting dependency group (uv would swap mypy in the one `.venv`), `uvx`
  (unlocked) and pyright (a second checker and a node runtime). It is left out of
  Dependabot on purpose, and its pin is tested. **Dependency added:** `mypy==1.20.2`, in
  that project only.
- **D2: `make check` installs the floor interpreters.** CI keeps running exactly
  `uv sync --locked` and `make check`, so CI and the local gate stay the same command
  (step 01a's rule). The cost: the first `make check` on a machine downloads CPython 3.9.6
  and 3.8 (uv-managed, `--no-bin`), and syncs `tools/py39/.venv`. The pytest driver is
  3.13; the checker process is 3.9.6. That is "CI runs its tests on 3.9" for a program
  that is run as a subprocess, which is how a PKA runs it too.
- **D3: The checker is two files**, a launcher and a module (2.6), because the guard must
  precede the future import and parse on old interpreters. It is still one copy, in one
  folder.
- **D4: The checker has a `files` mode, JSON output and a `rules` subcommand.** A PKA has
  no raw message, only the parts the `inbox` tools return (2.3), so without `files` step
  30 could not use the checker. JSON gives step 30 the de-duplication fields without its
  own YAML parsing, and `rules` lets the test prove every rule has a mutation.
- **D5: The checker accepts the frontmatter subset earmark emits** (5.6), stricter than
  "YAML". No real mail is affected, because earmark is the only sender.
- **D6: Nothing is masked in the sample comparison.** The brief allows masking `Date`;
  here `Date` is an input. Byte-exact comparison is stricter and was shown to hold across
  3.13 patch versions (2.5).
- **D7: Three more samples than the brief's minimum:** `reshare`, `retranscription`
  (step 30's acceptance names those cases) and `direct-url-query` (the `source_url`
  omission rule).
- **D8: Readings of the contract's wording** (5.6): `type`/`origin` consistency,
  `note.filename-date`, `apple:` digits, the word `third-party`. Checker behaviour only;
  the contract text is unchanged.
- **D9: The planner added `tools/py39/` to ARCHITECTURE's repo layout**, with `(step 29)`,
  because the layout must name every top-level directory. The doc tests pass with it.
  ARCHITECTURE's other changes (4.8) are the implementer's.
- **D10: Step 28's planned-names test is tightened** (2.8), so creating the checker's
  folders does not let docs name the unbuilt plugin without its step.
- **D11: Lint gains `FA`** (stricter; it changes nothing for 3.13 code) and a
  `.gitattributes` rule for the samples.

## 9. Out of scope

- The plugin manifests, the marketplace, `SKILL.md`, the skill's recognition,
  de-duplication and filing, and runbook phase 11 (step 30). Step 30 decides how the skill
  feeds the `inbox` tools' output to `files` mode.
- Any change to the contract (that would be `contract: 2`), to the mail code, or to the
  service.
- Calling `contract: 1` final; that waits for the operator's approval of the receiving
  skill.
- An HMAC over the attachments (a possible `contract: 2`).
- Testing the checker on interpreters uv cannot provide here (3.7 and older; non-CPython).
