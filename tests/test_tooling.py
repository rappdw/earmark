import asyncio
import socket
import tomllib
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pytest_socket import SocketBlockedError

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_inet_socket_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_dns_lookup_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.com", 443)


def test_http_client_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        httpx2.get("http://127.0.0.1:9/")


def test_unix_socketpair_allowed() -> None:
    async def coro() -> int:
        await asyncio.sleep(0)
        return 1

    assert asyncio.run(coro()) == 1


def test_live_markers_registered(pytestconfig: pytest.Config) -> None:
    markers = pytestconfig.getini("markers")
    for name in ("network", "slow", "llm", "gpu"):
        assert any(m.startswith(f"{name}:") for m in markers), name


def _make_inner(pytester: pytest.Pytester, inner_test: str) -> None:
    pytester.makepyprojecttoml((ROOT / "pyproject.toml").read_text())
    tests = pytester.path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text())
    (tests / "test_inner.py").write_text(inner_test)


_INNER_MARKERS = """
import socket
import pytest

def test_plain():
    pass

@pytest.mark.network
def test_network():
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()

@pytest.mark.slow
def test_slow():
    pass

@pytest.mark.llm
def test_llm():
    pass

@pytest.mark.gpu
def test_gpu():
    pass
"""


def test_live_markers_deselected_by_default(pytester: pytest.Pytester) -> None:
    _make_inner(pytester, _INNER_MARKERS)
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1, deselected=4)


def test_network_opt_in_reenables_sockets(pytester: pytest.Pytester) -> None:
    _make_inner(pytester, _INNER_MARKERS)
    result = pytester.runpytest_subprocess("-m", "network")
    result.assert_outcomes(passed=1, deselected=4)


def test_unregistered_marker_fails(pytester: pytest.Pytester) -> None:
    _make_inner(
        pytester,
        "import pytest\n\n@pytest.mark.netwrok\ndef test_typo():\n    pass\n",
    )
    result = pytester.runpytest_subprocess()
    assert result.ret != 0


def test_mypy_strict_over_src_and_tests() -> None:
    mypy = _pyproject()["tool"]["mypy"]
    assert mypy["strict"] is True
    assert mypy["files"] == ["src", "tests"]
    assert "pydantic.mypy" in mypy["plugins"]


def test_pytest_guards_configured() -> None:
    pytest_cfg = _pyproject()["tool"]["pytest"]
    assert pytest_cfg["strict"] is True
    assert "--disable-socket" in pytest_cfg["addopts"]
    assert pytest_cfg["filterwarnings"][0] == "error"


def test_ruff_lint_groups() -> None:
    select = set(_pyproject()["tool"]["ruff"]["lint"]["select"])
    assert {"E", "F", "I", "B", "UP", "S", "RUF", "DTZ", "T20"} <= select


def test_required_environments_include_aarch64() -> None:
    envs = _pyproject()["tool"]["uv"]["required-environments"]
    assert any("aarch64" in e and "linux" in e for e in envs)
