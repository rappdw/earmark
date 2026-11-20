"""Extract content from the project's Markdown documents for the doc-vs-code tests."""

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]

_FENCE_OPEN: Final = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_VARIABLE_CELL: Final = re.compile(r"`(EARMARK_[A-Z0-9_]+)`")
_LINK: Final = re.compile(r"!?\[[^\]\n]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SCHEME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def read_doc(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _only_once(text: str, marker: str) -> int:
    count = text.count(marker)
    assert count == 1, f"{marker!r} must occur exactly once, found {count}"
    return text.index(marker)


def between_markers(text: str, name: str) -> str:
    """Text between '<!-- {name}:begin -->' and '<!-- {name}:end -->'.

    AssertionError unless each marker occurs exactly once, begin before end.
    """
    begin = f"<!-- {name}:begin -->"
    end = f"<!-- {name}:end -->"
    start = _only_once(text, begin)
    stop = _only_once(text, end)
    assert start < stop, f"{begin!r} must come before {end!r}"
    return text[start + len(begin) : stop]


def after_marker(text: str, name: str) -> str:
    """Text after the single '<!-- {name} -->' marker; AssertionError otherwise."""
    marker = f"<!-- {name} -->"
    return text[_only_once(text, marker) + len(marker) :]


@dataclass(frozen=True)
class Fence:
    info: str  # the info string, stripped ("json", "bash", "")
    body: str  # the lines between the fences, without the common indentation
    line: int  # 1-based line of the opening fence


def fences(text: str) -> list[Fence]:
    """Fenced code blocks (``` or ~~~, length >= 3, optionally indented, as inside list
    items); a block closes at a line of the same character, at least as long."""
    found: list[Fence] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _FENCE_OPEN.match(lines[i])
        if match is None:
            i += 1
            continue
        fence = match["fence"]
        info = match["info"].strip()
        if fence[0] == "`" and "`" in info:
            i += 1  # inline code such as ```x``` is not a fence
            continue
        closing = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and closing.match(lines[j]) is None:
            j += 1
        body = textwrap.dedent("\n".join(lines[i + 1 : j]))
        found.append(Fence(info=info, body=body, line=i + 1))
        i = j + 1
    return found


def first_fence(text: str, info: str) -> Fence:
    """The first fence in text; AssertionError unless its info string's first word is info."""
    found = fences(text)
    assert found, "no fenced code block found"
    words = found[0].info.split()
    assert words, "the first fence has no info string"
    assert words[0] == info, f"first fence is {found[0].info!r}, expected {info!r}"
    return found[0]


def _outside_fences(text: str) -> str:
    lines = text.splitlines()
    keep = [True] * len(lines)
    for fence in fences(text):
        j = fence.line  # 0-based index of the first body line
        keep[fence.line - 1] = False
        while j < len(lines) and not re.match(r"^[ \t]*(`{3,}|~{3,})[ \t]*$", lines[j]):
            keep[j] = False
            j += 1
        if j < len(lines):
            keep[j] = False
    return "\n".join(line for line, k in zip(lines, keep, strict=True) if k)


def table_variables(text: str) -> list[str]:
    """Every `EARMARK_*` name in a table row's first cell, in order, from rows shaped
    '| `EARMARK_X` | <default> | <meaning> |'. Header and separator rows are skipped.

    AssertionError for a row whose first cell holds a backticked EARMARK_ name plus
    anything else (e.g. two variables in one row), or whose other cells are empty.
    """
    names: list[str] = []
    for line in _outside_fences(text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        first = cells[0]
        if _VARIABLE_CELL.search(first) is None:
            continue
        assert _VARIABLE_CELL.fullmatch(first), f"table row must hold one variable: {line!r}"
        assert len(cells) >= 3, f"table row needs three cells: {line!r}"
        assert all(cells[1:]), f"table row has an empty cell: {line!r}"
        names.append(first.strip("`"))
    return names


def relative_links(text: str) -> list[str]:
    """Targets of inline Markdown links and images that are not URLs (no scheme), not
    mailto:, and not pure '#anchors', with '#...' stripped. Links inside fences are
    ignored."""
    targets: list[str] = []
    for match in _LINK.finditer(_outside_fences(text)):
        target = match["target"]
        if _SCHEME.match(target) or target.startswith("#"):
            continue
        target = target.split("#", 1)[0]
        if target:
            targets.append(target)
    return targets
