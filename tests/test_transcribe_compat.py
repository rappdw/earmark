"""Known-incompatible package pairs, read from metadata only."""

import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest

from earmark.transcribe import compat
from earmark.transcribe.compat import Installed, check_transcribe_packages, release


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("19.0.2", (19, 0, 2)),
        ("19.0.0rc1", (19,)),
        ("1.2.1.post1", (1, 2, 1)),
        ("1.2.1.0", (1, 2, 1)),
        ("1!2.0", (2,)),
        ("0", (0,)),
        (" 18.1.0 ", (18, 1)),
        ("dev", None),
        ("", None),
    ],
)
def test_release(version: str, expected: tuple[int, ...] | None) -> None:
    assert release(version) == expected


def _lookup(fw: str | Installed | None, av: str | Installed | None):  # type: ignore[no-untyped-def]
    values = {"faster-whisper": fw, "av": av}

    def lookup(name: str) -> Installed | None:
        value = values[name]
        return Installed(value, from_source=False) if isinstance(value, str) else value

    return lookup


@pytest.mark.parametrize(
    ("fw", "av", "status"),
    [
        ("1.2.1", "19.0.2", "incompatible"),
        ("1.2.1", "18.1.0", "ok"),
        ("1.2.2", "19.0.2", "ok"),
        ("1.3.0", "20.0", "ok"),
        ("1.2.1.post1", "19.0.0rc1", "incompatible"),
        ("1.1.1", "19.0.0", "incompatible"),
    ],
)
def test_known_incompatible_pairs(fw: str, av: str, status: str) -> None:
    result = check_transcribe_packages(_lookup(fw, av))
    assert result.status == status
    if status == "incompatible":
        assert fw in result.detail
        assert av in result.detail
        assert "metadata_errors" in result.detail
        assert "uv sync --locked --extra transcribe" in result.detail
    elif (fw, av) == ("1.2.1", "18.1.0"):
        assert result.detail == "faster-whisper 1.2.1, av 18.1.0"


def test_source_install_skips_rule() -> None:
    result = check_transcribe_packages(_lookup(Installed("1.2.1", from_source=True), "19.0.2"))
    assert result.status == "ok"
    assert "source tree" in result.detail


@pytest.mark.parametrize(
    ("fw", "av", "name"),
    [("1.2.1", None, "av"), ("weird", "18.1.0", "faster-whisper")],
)
def test_unknown_versions(fw: str, av: str | None, name: str) -> None:
    result = check_transcribe_packages(_lookup(fw, av))
    assert result.status == "unknown"
    assert name in result.detail


class _Dist:
    def __init__(self, version: object, direct_url: str | None) -> None:
        self.version = version
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url


@pytest.mark.parametrize(
    ("direct_url", "from_source"),
    [
        (None, False),
        ('{"url": "https://x", "vcs_info": {"vcs": "git"}}', True),
        ('{"url": "file:///x", "dir_info": {"editable": true}}', True),
        ('{"url": "https://x", "archive_info": {}}', False),
        ("{not json", False),
    ],
)
def test_installed_reads_metadata(
    monkeypatch: pytest.MonkeyPatch, direct_url: str | None, from_source: bool
) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _Dist("1.2.1", direct_url))
    assert compat.installed("faster-whisper") == Installed("1.2.1", from_source)


def test_installed_missing_or_unreadable_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> object:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    assert compat.installed("av") is None
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _Dist(None, None))
    assert compat.installed("av") is None


def test_compat_imports_light() -> None:
    code = (
        "import sys\n"
        "import earmark.transcribe.compat\n"
        "heavy = {'av', 'faster_whisper', 'ctranslate2', 'numpy', 'httpx2'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_real_metadata_does_not_crash(tmp_path: Path) -> None:
    result = check_transcribe_packages()
    assert result.status in ("ok", "incompatible", "unknown")
