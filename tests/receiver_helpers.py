"""Workspaces, a fake pka-tools and sample inputs for the receiving skill's tests."""

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from tests.checker_helpers import Interpreter, Parts, run_receiver, split
from tests.contract_samples import SAMPLES_DIR

FAKE_PKA_TOOLS: Final = '#!/bin/sh\nprintf \'{\\n  "version": "%s"\\n}\\n\' "{version}"\n'


def workspace(tmp_path: Path, *, pka_tools: str | None = "1.11.0", exit_code: int = 0) -> Path:
    """``tmp_path/ws`` with a ``.pka`` folder and a fake pka-tools printing ``pka_tools``."""
    ws = tmp_path / "ws"
    (ws / ".pka").mkdir(parents=True)
    if pka_tools is not None:
        tool = ws / ".pka" / "bin" / "pka-tools"
        tool.parent.mkdir()
        script = FAKE_PKA_TOOLS.replace("{version}", pka_tools)
        tool.write_text(script + (f"exit {exit_code}\n" if exit_code else ""), encoding="utf-8")
        tool.chmod(0o755)
    return ws


def attachment_args(
    parts: Parts,
    directory: Path,
    *,
    dispositions: tuple[str, str] = ("clean", "clean"),
    inbox_id: str = "notice-test-1",
) -> list[str]:
    """Write the inputs as sidecar files and give the INPUT arguments for them."""
    directory.mkdir(parents=True, exist_ok=True)
    first, second = directory / "0", directory / "1"
    first.write_bytes(parts.note)
    second.write_bytes(parts.instructions)
    subject, body = directory / "subject.txt", directory / "body.txt"
    subject.write_bytes(parts.subject.encode("utf-8") + b"\n")
    body.write_bytes(parts.body.encode("utf-8"))
    return [
        "--inbox-id",
        inbox_id,
        "--attachment",
        parts.note_name,
        str(first),
        dispositions[0],
        "--attachment",
        parts.instructions_name,
        str(second),
        dispositions[1],
        "--subject-file",
        str(subject),
        "--body-file",
        str(body),
    ]


def sample_bytes(name: str) -> bytes:
    return (SAMPLES_DIR / f"{name}.eml").read_bytes()


def sample_args(name: str, directory: Path) -> list[str]:
    return attachment_args(split(sample_bytes(name)), directory)


def receive(python: Interpreter, command: str, ws: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = run_receiver(python, command, "--workspace", str(ws), *args)
    lines = result.stdout.splitlines()
    assert len(lines) == 1, (result.stdout, result.stderr)
    assert lines[0].isascii()
    document = json.loads(lines[0])
    assert isinstance(document, dict)
    assert document["receiver"] == "earmark-receive"
    assert document["command"] == command
    assert document["ok"] is (result.returncode == 0)
    return result.returncode, document


def records(ws: Path) -> list[dict[str, Any]]:
    file = ws / ".pka" / "earmark" / "received.jsonl"
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line]


def tree(ws: Path) -> dict[str, str]:
    """Every file under ``ws``, relative path to sha256."""
    return {
        path.relative_to(ws).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(ws.rglob("*"))
        if path.is_file()
    }
