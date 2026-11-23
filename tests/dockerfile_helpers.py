"""Dockerfile parsers shared by the static container tests. They work on text, not files."""

import re
from typing import Final

PINNED_IMAGE: Final = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")


def logical_lines(text: str) -> list[str]:
    """Dockerfile instructions with comments dropped and continuations joined."""
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        joined.append((pending + raw).strip())
        pending = ""
    return [line for line in joined if line]


def instructions(text: str, name: str) -> list[str]:
    return [line for line in logical_lines(text) if line.split()[0].upper() == name]


def froms(text: str) -> list[tuple[str, str | None]]:
    """Each FROM as (image, stage name or None)."""
    result: list[tuple[str, str | None]] = []
    for line in instructions(text, "FROM"):
        words = line.split()
        stage = words[words.index("AS") + 1] if "AS" in words else None
        image = next(w for w in words[1:] if not w.startswith("--"))
        result.append((image, stage))
    return result


def external_froms(text: str) -> list[str]:
    stages = {s for _, s in froms(text) if s}
    return [image for image, _ in froms(text) if image not in stages]


def stage_lines(text: str, stage: str) -> list[str]:
    """The logical lines from ``FROM ... AS stage`` up to the next FROM."""
    lines = logical_lines(text)
    out: list[str] = []
    inside = False
    for line in lines:
        words = line.split()
        if words[0].upper() == "FROM":
            inside = "AS" in words and words[words.index("AS") + 1] == stage
        if inside:
            out.append(line)
    return out


def runtime_stage_lines(text: str) -> list[str]:
    lines = logical_lines(text)
    last_from = max(i for i, line in enumerate(lines) if line.split()[0].upper() == "FROM")
    return lines[last_from:]
