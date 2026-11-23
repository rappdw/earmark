import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_SOCKET_MARKERS = ("network", "slow", "smtp")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if any(item.get_closest_marker(m) for m in _SOCKET_MARKERS):
            item.add_marker(pytest.mark.enable_socket)


@pytest.fixture(autouse=True)
def _isolate_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    for key in list(os.environ):
        if key.startswith("EARMARK_"):
            monkeypatch.delenv(key)
    if not any(request.node.get_closest_marker(m) for m in _SOCKET_MARKERS):
        # Defence in depth: sockets are blocked anyway, but nothing may reach for a model.
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def restore_library_loggers() -> Iterator[None]:
    """Undo level changes on the wire loggers and handlers added to library loggers."""
    from earmark import log

    levels = {name: logging.getLogger(name).level for name in log.QUIET_LOGGERS}
    handlers = {
        name: list(logging.getLogger(name).handlers) for name in log.LIBRARY_HANDLER_LOGGERS
    }
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    for name, saved in handlers.items():
        logging.getLogger(name).handlers[:] = saved
