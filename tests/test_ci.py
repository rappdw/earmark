"""Static checks of the CI workflow files and Dependabot config, plus an offline actionlint run.

`test_ci_pins_expected_actions` makes every Dependabot action bump touch a test. That is
deliberate friction on a security-relevant change; a later step may relax it to "pinned and
commented" only.
"""

import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
ACTIONLINT = Path(sys.executable).parent / "actionlint"
ARM64_RUNNER = "ubuntu-24.04-arm"
CI_COMMANDS = ["uv sync --locked", "make check"]
CHECKOUT = "actions/checkout"
SETUP_UV = "astral-sh/setup-uv"

_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*", re.MULTILINE)
_PINNED_USES = re.compile(
    r"^\s*(?:-\s+)?uses:\s*(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)"
    r"@(?P<sha>[0-9a-f]{40})\s+#\s*v\d+\.\d+\.\d+\s*$"
)


def _workflow_files() -> list[Path]:
    files = sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])
    assert files, f"no workflow files under {WORKFLOWS_DIR}"
    assert CI_WORKFLOW in files
    return files


def _load_yaml(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML is YAML 1.1, so the key `on` loads as the boolean True.
    raw = workflow["on"] if "on" in workflow else workflow[True]
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    assert isinstance(raw, dict)
    return {str(k): v for k, v in raw.items()}


def _jobs(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _only_job(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs = _jobs(workflow)
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


def _network_job(job_id: str) -> dict[str, Any]:
    job = _jobs(_network())[job_id]
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(s, dict) for s in steps)
    return steps


def _remote_uses(workflow: dict[Any, Any]) -> list[str]:
    found: list[str] = []
    for job in _jobs(workflow).values():
        candidates = [job.get("uses")]
        candidates += [step.get("uses") for step in job.get("steps", [])]
        found += [c for c in candidates if isinstance(c, str) and not c.startswith("./")]
    return found


def _action(step: dict[str, Any]) -> str:
    uses = step.get("uses")
    return uses.split("@", 1)[0] if isinstance(uses, str) else ""


def _ci() -> dict[Any, Any]:
    return _load_yaml(CI_WORKFLOW)


def _ci_step(action: str) -> dict[str, Any]:
    matches = [s for s in _steps(_only_job(_ci())) if _action(s) == action]
    assert len(matches) == 1
    return matches[0]


def _run_actionlint(*paths: Path) -> tuple[int, list[dict[str, Any]], str]:
    assert ACTIONLINT.exists(), f"{ACTIONLINT} is missing; run `uv sync` to install actionlint-py"
    proc = subprocess.run(
        [
            str(ACTIONLINT),
            "-shellcheck=",
            "-pyflakes=",
            "-format",
            "{{json .}}",
            *map(str, paths),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=60,
    )
    errors = json.loads(proc.stdout) if proc.stdout.strip() else []
    assert isinstance(errors, list)
    return proc.returncode, errors, proc.stderr


WORKFLOW_PARAMS = [pytest.param(p, id=p.name) for p in _workflow_files()]


# --- A1 -------------------------------------------------------------------------------


def test_ci_runs_exactly_the_local_gate() -> None:
    steps = _steps(_only_job(_ci()))
    run_steps = [s for s in steps if "run" in s]
    assert [s["run"].strip() for s in run_steps] == CI_COMMANDS
    assert steps[-2:] == run_steps


def test_ci_step_sequence() -> None:
    steps = _steps(_only_job(_ci()))
    assert [_action(s) for s in steps] == [CHECKOUT, SETUP_UV, "", ""]
    assert [s["run"].strip() for s in steps[2:]] == CI_COMMANDS


def test_makefile_check_is_the_gate() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^check:", makefile, re.MULTILINE)
    assert re.search(r"^UV_RUN := uv run --locked\s*$", makefile, re.MULTILINE)


# --- A2 -------------------------------------------------------------------------------


def test_ci_runs_on_arm64() -> None:
    job = _only_job(_ci())
    assert job["runs-on"] == ARM64_RUNNER
    assert "strategy" not in job


# --- A3 -------------------------------------------------------------------------------


def test_ci_python_comes_from_python_version_file() -> None:
    workflow = _ci()
    job = _only_job(workflow)
    steps = _steps(job)
    assert "python-version" not in (_ci_step(SETUP_UV).get("with") or {})
    assert all(_action(s) != "actions/setup-python" for s in steps)
    text = CI_WORKFLOW.read_text()
    assert "UV_PYTHON" not in text
    assert "python-version" not in text
    assert "env" not in workflow
    assert "env" not in job
    assert all("env" not in s for s in steps)


def test_python_version_file_matches_project() -> None:
    assert (ROOT / ".python-version").read_text().strip() == "3.13"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["requires-python"].startswith(">=3.13")


# --- A4 / B3 ----------------------------------------------------------------------------


def test_ci_permissions_read_only() -> None:
    workflow = _ci()
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in _only_job(workflow)


@pytest.mark.parametrize("path", WORKFLOW_PARAMS)
def test_workflow_declares_permissions(path: Path) -> None:
    assert "permissions" in _load_yaml(path)


# --- A5 -------------------------------------------------------------------------------


def test_ci_references_no_secrets() -> None:
    assert re.search(r"\bsecrets\b", CI_WORKFLOW.read_text()) is None


# --- A6 -------------------------------------------------------------------------------


def test_ci_triggers() -> None:
    triggers = _triggers(_ci())
    assert set(triggers) == {"push", "pull_request"}
    assert set(triggers["push"]["branches"]) == {"main", "impl"}
    pr = triggers["pull_request"]
    assert pr is None or pr == {}


@pytest.mark.parametrize("path", WORKFLOW_PARAMS)
def test_workflow_never_uses_pull_request_target(path: Path) -> None:
    assert "pull_request_target" not in _triggers(_load_yaml(path))
    assert "pull_request_target" not in path.read_text()


# --- A7 -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", WORKFLOW_PARAMS)
def test_workflow_actions_pinned_to_full_sha(path: Path) -> None:
    text = path.read_text()
    lines = [
        line
        for line in text.splitlines()
        if _USES_LINE.match(line) and not line.split("uses:", 1)[1].strip().startswith("./")
    ]
    bad = [line for line in lines if not _PINNED_USES.match(line)]
    assert not bad, f"unpinned uses: {bad}"
    assert len(lines) == len(_remote_uses(_load_yaml(path)))


def test_ci_pins_expected_actions() -> None:
    assert _remote_uses(_ci()) == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2",
    ]


# --- A8 -------------------------------------------------------------------------------


def test_ci_concurrency_cancels_in_progress() -> None:
    concurrency = _ci()["concurrency"]
    assert isinstance(concurrency, dict)
    assert "github.ref" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


def test_ci_job_has_timeout() -> None:
    timeout = _only_job(_ci())["timeout-minutes"]
    assert type(timeout) is int
    assert 1 <= timeout <= 30


# --- B1 / B2 ----------------------------------------------------------------------------


def test_ci_caches_uv() -> None:
    assert _ci_step(SETUP_UV)["with"]["enable-cache"] is True


def test_ci_checkout_does_not_persist_credentials() -> None:
    assert _ci_step(CHECKOUT)["with"]["persist-credentials"] is False


# --- A9 -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", WORKFLOW_PARAMS)
def test_workflow_passes_actionlint(path: Path) -> None:
    code, errors, stderr = _run_actionlint(path)
    detail = "\n".join(
        f"{e['filepath']}:{e['line']}:{e['column']}: {e['message']} [{e['kind']}]" for e in errors
    )
    message = f"actionlint exit {code}\n{detail}\n{stderr}"
    assert code == 0, message
    assert not errors, message


def test_actionlint_reports_errors(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yml"
    broken.write_text(
        "on: push\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-24.04-armm\n"
        "    steps:\n"
        "      - run: echo ${{ github.reff }}\n"
    )
    code, errors, _ = _run_actionlint(broken)
    assert code == 1
    assert {"runner-label", "expression"} <= {e["kind"] for e in errors}


# --- B4 -------------------------------------------------------------------------------


def test_dependabot_config() -> None:
    config = _load_yaml(DEPENDABOT)
    assert config["version"] == 2
    updates = config["updates"]
    assert Counter(u["package-ecosystem"] for u in updates) == Counter(["uv", "github-actions"])
    for update in updates:
        assert update["directory"] == "/"
        assert update["target-branch"] == "impl"
        assert update["schedule"]["interval"] == "weekly"


# --- network workflow (step 07) ------------------------------------------------------------

NETWORK_WORKFLOW = WORKFLOWS_DIR / "network.yml"
NETWORK_COMMANDS = ["uv sync --locked", "uv run --locked pytest -m network -v -rs"]


def _network() -> dict[Any, Any]:
    return _load_yaml(NETWORK_WORKFLOW)


def test_network_workflow_triggers() -> None:
    triggers = _triggers(_network())
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    schedule = triggers["schedule"]
    assert isinstance(schedule, list)
    assert len(schedule) == 1
    fields = schedule[0]["cron"].split()
    assert len(fields) == 5
    minute, _hour, day_of_month, month, day_of_week = fields
    assert (day_of_month, month) == ("*", "*")
    assert re.fullmatch(r"[0-6]", day_of_week)
    assert minute != "0"


def test_live_workflow_jobs() -> None:
    assert set(_jobs(_network())) == {"network", "slow"}


def test_network_workflow_runs_network_marker() -> None:
    job = _network_job("network")
    assert job["runs-on"] == ARM64_RUNNER
    timeout = job["timeout-minutes"]
    assert type(timeout) is int
    assert 1 <= timeout <= 30
    steps = _steps(job)
    assert [s["run"].strip() for s in steps if "run" in s] == NETWORK_COMMANDS
    assert [_action(s) for s in steps] == [CHECKOUT, SETUP_UV, "", ""]


def test_network_workflow_read_only_no_secrets() -> None:
    workflow = _network()
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets" not in NETWORK_WORKFLOW.read_text()
    assert "env" not in workflow
    job = _network_job("network")
    assert "env" not in job
    steps = _steps(job)
    assert all("env" not in s for s in steps)
    [checkout] = [s for s in steps if _action(s) == CHECKOUT]
    assert checkout["with"]["persist-credentials"] is False


def _job_remote_uses(job: dict[str, Any]) -> list[str]:
    return [s["uses"] for s in _steps(job) if isinstance(s.get("uses"), str)]


def test_network_workflow_pins_match_ci() -> None:
    job = _network_job("network")
    assert _job_remote_uses(job) == _remote_uses(_ci())
    [network_uv] = [s for s in _steps(job) if _action(s) == SETUP_UV]
    assert network_uv["with"] == _ci_step(SETUP_UV)["with"]


def test_network_workflow_is_not_the_gate() -> None:
    # The default suite must never run the live tests: the marker expression excludes them.
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    addopts = pyproject["tool"]["pytest"]["addopts"]
    expression = addopts[addopts.index("-m") + 1]
    assert "not network" in expression
    assert "not slow" in expression
    assert "not llm" in expression


# --- slow job (step 08) --------------------------------------------------------------------

SLOW_COMMANDS = [
    "uv sync --locked --extra transcribe --extra index",
    "uv run --locked --extra transcribe --extra index pytest -m slow -v -rs",
]
CACHE_ACTION = "actions/cache"
CACHE_PIN = "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"


def test_slow_job_runs_slow_marker() -> None:
    job = _network_job("slow")
    assert job["runs-on"] == ARM64_RUNNER
    timeout = job["timeout-minutes"]
    assert type(timeout) is int
    assert 1 <= timeout <= 30
    steps = _steps(job)
    assert [s["run"].strip() for s in steps if "run" in s] == SLOW_COMMANDS
    assert [_action(s) for s in steps] == [CHECKOUT, SETUP_UV, CACHE_ACTION, "", ""]


def test_slow_job_read_only_no_secrets() -> None:
    workflow = _network()
    assert "env" not in workflow
    job = _network_job("slow")
    assert "env" not in job
    steps = _steps(job)
    assert all("env" not in s for s in steps)
    [checkout] = [s for s in steps if _action(s) == CHECKOUT]
    assert checkout["with"]["persist-credentials"] is False


def test_slow_job_pins() -> None:
    job = _network_job("slow")
    assert _job_remote_uses(job) == [*_remote_uses(_ci()), CACHE_PIN]
    [slow_uv] = [s for s in _steps(job) if _action(s) == SETUP_UV]
    assert slow_uv["with"] == {**_ci_step(SETUP_UV)["with"], "cache-suffix": "slow"}


def test_slow_job_caches_test_assets() -> None:
    from tests.slow_assets import DEFAULT_CACHE_DIR

    job = _network_job("slow")
    [cache] = [s for s in _steps(job) if _action(s) == CACHE_ACTION]
    assert cache["with"]["path"] == "~/" + DEFAULT_CACHE_DIR.relative_to(Path.home()).as_posix()
    assert "hashFiles('tests/slow_assets.py')" in cache["with"]["key"]


# --- llm workflow (step 10) ----------------------------------------------------------------

LLM_WORKFLOW = WORKFLOWS_DIR / "llm.yml"
LLM_COMMANDS = ["uv sync --locked", "uv run --locked pytest -m llm -v -rs"]


def _llm() -> dict[Any, Any]:
    return _load_yaml(LLM_WORKFLOW)


def test_llm_workflow_triggers() -> None:
    assert set(_triggers(_llm())) == {"workflow_dispatch"}


def test_llm_workflow_job() -> None:
    jobs = _jobs(_llm())
    assert set(jobs) == {"llm"}
    job = jobs["llm"]
    assert job["runs-on"] == ARM64_RUNNER
    timeout = job["timeout-minutes"]
    assert type(timeout) is int
    assert 1 <= timeout <= 30
    assert job["environment"] == "llm"
    assert job["if"] == "github.event_name == 'workflow_dispatch'"
    steps = _steps(job)
    assert [_action(s) for s in steps] == [CHECKOUT, SETUP_UV, "", ""]
    assert [s["run"].strip() for s in steps if "run" in s] == LLM_COMMANDS
    assert _job_remote_uses(job) == _remote_uses(_ci())
    [llm_uv] = [s for s in steps if _action(s) == SETUP_UV]
    assert llm_uv["with"] == _ci_step(SETUP_UV)["with"]


def test_llm_workflow_secret_scope() -> None:
    workflow = _llm()
    assert workflow["permissions"] == {"contents": "read"}
    assert "env" not in workflow
    job = _jobs(workflow)["llm"]
    assert "permissions" not in job
    assert "env" not in job
    steps = _steps(job)
    assert all("env" not in s for s in steps[:-1])
    assert steps[-1]["env"] == {"ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}"}
    assert LLM_WORKFLOW.read_text().count("secrets.") == 1
    [checkout] = [s for s in steps if _action(s) == CHECKOUT]
    assert checkout["with"]["persist-credentials"] is False


def test_only_llm_workflow_references_secrets() -> None:
    for path in _workflow_files():
        if path != LLM_WORKFLOW:
            assert re.search(r"\bsecrets\b", path.read_text()) is None, path.name


# --- index workflow (step 14) --------------------------------------------------------------

INDEX_WORKFLOW = WORKFLOWS_DIR / "index.yml"
INDEX_COMMANDS = [
    "uv sync --locked --extra index",
    "uv run --locked --extra index pytest -m index -v -rs",
]


def _index() -> dict[Any, Any]:
    return _load_yaml(INDEX_WORKFLOW)


def test_index_workflow_triggers() -> None:
    triggers = _triggers(_index())
    assert set(triggers) == {"push", "pull_request"}
    assert set(triggers["push"]["branches"]) == {"main", "impl"}
    pr = triggers["pull_request"]
    assert pr is None or pr == {}


def test_index_workflow_job() -> None:
    workflow = _index()
    assert set(_jobs(workflow)) == {"index"}
    job = _only_job(workflow)
    assert job["runs-on"] == ARM64_RUNNER
    timeout = job["timeout-minutes"]
    assert type(timeout) is int
    assert 1 <= timeout <= 30
    steps = _steps(job)
    assert [_action(s) for s in steps] == [CHECKOUT, SETUP_UV, "", ""]
    assert [s["run"].strip() for s in steps if "run" in s] == INDEX_COMMANDS
    assert _job_remote_uses(job) == _remote_uses(_ci())
    [uv_step] = [s for s in steps if _action(s) == SETUP_UV]
    assert uv_step["with"] == {**_ci_step(SETUP_UV)["with"], "cache-suffix": "index"}
    [checkout] = [s for s in steps if _action(s) == CHECKOUT]
    assert checkout["with"]["persist-credentials"] is False


def test_index_workflow_read_only_no_secrets() -> None:
    workflow = _index()
    assert workflow["permissions"] == {"contents": "read"}
    job = _only_job(workflow)
    assert "permissions" not in job
    assert "env" not in workflow
    assert "env" not in job
    assert all("env" not in s for s in _steps(job))
    assert re.search(r"\bsecrets\b", INDEX_WORKFLOW.read_text()) is None


def test_index_workflow_concurrency() -> None:
    assert _index()["concurrency"] == _ci()["concurrency"]
    assert _index()["concurrency"]["cancel-in-progress"] is True
