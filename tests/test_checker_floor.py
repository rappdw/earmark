"""The contract checker has one copy, runs on Python 3.9, and the gate keeps it there."""

import ast
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests.checker_helpers import (
    CHECKER,
    ROOT,
    SCRIPTS,
    Interpreter,
    floor_interpreters,
)
from tests.contract_samples import SAMPLES_DIR

MODULE = SCRIPTS / "earmark_mail_contract.py"
SKIPPED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
FLOOR = floor_interpreters()


def _all_files() -> list[Path]:
    found: list[Path] = []
    for directory, names, files in os.walk(ROOT):
        names[:] = [n for n in names if n not in SKIPPED_DIRECTORIES]
        found.extend(Path(directory) / f for f in files)
    return found


def test_checker_has_one_copy() -> None:
    digests = {hashlib.sha256(p.read_bytes()).digest(): p for p in (CHECKER, MODULE)}
    for path in _all_files():
        if path.parent == SCRIPTS:
            continue
        assert path.name not in {CHECKER.name, MODULE.name}, path
        if path.stat().st_size and hashlib.sha256(path.read_bytes()).digest() in digests:
            pytest.fail(f"{path} is a copy of the checker")


def test_scripts_folder_holds_only_the_checker() -> None:
    assert sorted(p.name for p in SCRIPTS.iterdir()) == [CHECKER.name, MODULE.name]


def test_checker_imports_only_stdlib() -> None:
    for path in (CHECKER, MODULE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, path
                names = [(node.module or "").split(".")[0]]
            for name in names:
                assert name in sys.stdlib_module_names or name == "earmark_mail_contract", (
                    path.name,
                    name,
                )
                assert name != "earmark"


def _is_exit_2(node: ast.stmt) -> bool:
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    call = node.value
    return (
        ast.unparse(call.func) == "sys.exit"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 2
    )


def test_launcher_is_old_syntax() -> None:
    source = CHECKER.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 4))
    for node in ast.walk(tree):
        assert not isinstance(
            node, ast.JoinedStr | ast.AnnAssign | ast.NamedExpr | ast.Match | ast.AsyncFunctionDef
        )
        if isinstance(node, ast.arguments):
            assert not node.posonlyargs
        if isinstance(node, ast.arg):
            assert node.annotation is None
        if isinstance(node, ast.FunctionDef):
            assert node.returns is None
    body = tree.body
    assert isinstance(body[0], ast.Expr)
    assert isinstance(body[0].value, ast.Constant)
    assert isinstance(body[0].value.value, str)
    assert ast.unparse(body[1]) == "import sys"
    guard = body[2]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "sys.version_info < (3, 9)"
    assert _is_exit_2(guard.body[-1])
    assert ast.unparse(body[3]) == "sys.dont_write_bytecode = True"
    later = [ast.unparse(node) for node in body[4:]]
    assert any(text == "import earmark_mail_contract" for text in later)
    assert ast.unparse(body[-1]) == "sys.exit(earmark_mail_contract.main(sys.argv[1:]))"


@pytest.mark.parametrize("python", FLOOR, ids=lambda i: i.id)
def test_checker_writes_no_bytecode(python: Interpreter, tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, scripts)
    sample = SAMPLES_DIR / "podcast-feed.eml"
    result = subprocess.run(
        [python.path, "-I", str(scripts / CHECKER.name), "message", str(sample)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


MATCH = (
    "def f(x: int) -> int:\n    match x:\n        case 1:\n            return 1\n"
    "        case _:\n            return 0\n"
)
UNION = "def f(x: int | None) -> int:\n    return 0 if x is None else x\n"


def test_ruff_lints_plugins_as_py39(tmp_path: Path) -> None:
    ruff = Path(sys.executable).parent / "ruff"
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for name, text in (("plugins/p/a.py", MATCH), ("plugins/p/b.py", UNION), ("src/c.py", MATCH)):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [str(ruff), "check", "--no-cache", "--output-format", "concise", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.splitlines()
    assert any(line.startswith("plugins/p/a.py") and "match" in line.lower() for line in lines)
    assert any(line.startswith("plugins/p/b.py") and "FA102" in line for line in lines)
    assert not any(line.startswith("src/c.py") for line in lines), lines


CONSTRUCTS = {
    "union": UNION,
    "match": MATCH,
    "pairwise": "import itertools\nx = list(itertools.pairwise([1, 2]))\n",
    "bit_count": "n = (5).bit_count()\n",
    "zip_strict": "x = list(zip([1], [2], strict=True))\n",
    "isinstance_union": "def f(x: object) -> bool:\n    return isinstance(x, int | str)\n",
    "datetime_utc": "import datetime\nx = datetime.UTC\n",
    "tomllib": "import tomllib\n",
}
CLEAN = (
    "from typing import Optional\n\n\n"
    "def f(x: Optional[int]) -> int:\n    return 0 if x is None else x\n"
)


def test_floor_mypy_rejects_newer_python(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is needed to run the floor type check"
    probes = tmp_path / "probes"
    probes.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    tools = ROOT / "tools" / "py39"

    def run(name: str, text: str) -> list[str]:
        probe = probes / f"p_{name}.py"
        probe.write_text(text, encoding="utf-8")
        result = subprocess.run(
            [
                uv,
                "run",
                "--offline",
                "--locked",
                "--project",
                str(tools),
                "mypy",
                "--config-file",
                str(tools / "pyproject.toml"),
                "--cache-dir",
                str(tmp_path / "cache"),
                str(probe),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.splitlines()

    for name, text in CONSTRUCTS.items():
        lines = run(name, text)
        assert any(f"p_{name}.py:" in line and "error" in line for line in lines), (name, lines)
    control = run("control", CLEAN)
    assert control == ["Success: no issues found in 1 source file"], control


def _toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_floor_config_is_strict() -> None:
    floor, root = _toml(ROOT / "tools/py39/pyproject.toml"), _toml(ROOT / "pyproject.toml")
    assert floor["dependency-groups"]["dev"] == ["mypy==1.20.2"]
    mypy, root_mypy = floor["tool"]["mypy"], root["tool"]["mypy"]
    assert mypy["python_version"] == "3.9"
    assert mypy["strict"] is True
    assert mypy["warn_unreachable"] is True
    assert set(root_mypy["enable_error_code"]) <= set(mypy["enable_error_code"])
    assert floor["project"]["requires-python"] == root["project"]["requires-python"]
    assert floor["tool"]["uv"]["package"] is False
    ruff = root["tool"]["ruff"]
    assert ruff["target-version"] == "py313"
    assert ruff["per-file-target-version"] == {"plugins/**/*.py": "py39"}
    assert root_mypy["python_version"] == "3.13"
    assert root_mypy["files"] == ["src", "tests"]
    assert "FA" in ruff["lint"]["select"]
    original = {
        "E", "W", "F", "I", "N", "UP", "B", "A", "C4", "DTZ", "T20", "PT", "PIE", "RET",
        "SIM", "PTH", "S", "BLE", "LOG", "G", "ASYNC", "FAST", "PGH", "PERF", "RUF",
    }  # fmt: skip
    assert original <= set(ruff["lint"]["select"])


def test_makefile_runs_the_floor() -> None:
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    lines = [line.strip() for line in make.splitlines()]
    assert "FLOOR_RUN := uv run --locked --project tools/py39" in lines
    assert "$(FLOOR_RUN) mypy --config-file tools/py39/pyproject.toml plugins" in lines
    assert "test: checker-pythons" in lines
    assert "MAIL_CHECKER_REQUIRE_FLOOR=1 \\" in lines
    test_recipe = make.partition("\ntest:")[2].partition("\n\n")[0]
    assert "MAIL_CHECKER_PYTHONS=" in test_recipe
    assert "3.9.6" in test_recipe
    assert "FIND_PYTHON) 3.8" in test_recipe
    assert "CHECKER_PYTHONS := 3.9.6 3.8" in lines
    assert "uv python install --no-bin $(CHECKER_PYTHONS)" in lines
    assert "$(UV_RUN) python -m tests.contract_samples" in lines
