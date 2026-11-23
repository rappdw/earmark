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


_HEADING: Final = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")
_ANCHORED_LINK: Final = re.compile(
    r"!?\[[^\]\n]*\]\((?P<target>[^)\s]*#[^)\s]*)(?:\s+\"[^\"]*\")?\)"
)


def table_rows(text: str) -> list[list[str]]:
    """Cells of every Markdown table row outside fences, stripped, header and separator
    rows excluded (a separator row is all '-', ':' and spaces; the header is the row
    directly above it)."""
    rows: list[list[str]] = []
    previous: list[str] | None = None  # the row above, which is a header if a separator follows
    for line in _outside_fences(text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            previous = None
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.fullmatch(r"[-: ]*", c) for c in cells) and any(cells):
            if previous is not None and rows and rows[-1] is previous:
                rows.pop()  # that row was the header
            previous = None
            continue
        rows.append(cells)
        previous = cells
    return rows


def _headings(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for line in _outside_fences(text).splitlines():
        match = _HEADING.match(line)
        if match:
            found.append((len(match["hashes"]), match["text"]))
    return found


def heading_anchors(text: str) -> set[str]:
    """GitHub anchors of the ATX headings outside fences: text lower-cased, every character
    that is not a word character, '-' or ' ' removed, spaces replaced by '-'; a repeated
    anchor gets '-1', '-2', ... in order."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for _, title in _headings(text):
        base = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def anchored_links(text: str) -> list[tuple[str, str]]:
    """(path, anchor) for inline links outside fences that carry a '#anchor' and no scheme;
    path is '' for a same-document link."""
    found: list[tuple[str, str]] = []
    for match in _ANCHORED_LINK.finditer(_outside_fences(text)):
        target = match["target"]
        if _SCHEME.match(target):
            continue
        path, anchor = target.split("#", 1)
        if anchor:
            found.append((path, anchor))
    return found


def without_blocks(text: str, name: str) -> str:
    """text with every '<!-- name:begin -->' ... '<!-- name:end -->' span removed (markers
    included); AssertionError if they are unbalanced or nested."""
    begin = f"<!-- {name}:begin -->"
    end = f"<!-- {name}:end -->"
    out: list[str] = []
    rest = text
    while True:
        start = rest.find(begin)
        stop = rest.find(end)
        if start == -1 and stop == -1:
            out.append(rest)
            return "".join(out)
        assert start != -1, f"unbalanced {name} markers: {end!r} without {begin!r}"
        assert stop != -1, f"unbalanced {name} markers: {begin!r} without {end!r}"
        assert start < stop, f"{end!r} comes before {begin!r}"
        assert begin not in rest[start + len(begin) : stop], f"nested {name} markers"
        out.append(rest[:start])
        rest = rest[stop + len(end) :]


def section(text: str, heading: str) -> str:
    """From the heading line that starts with `heading` (for example '## 8. ') to the next
    heading of the same or a higher level; AssertionError unless exactly one matches."""
    lines = text.splitlines()
    fenced = {
        i
        for fence in fences(text)
        for i in range(fence.line - 1, fence.line + fence.body.count("\n") + 2)
    }
    starts = [i for i, line in enumerate(lines) if i not in fenced and line.startswith(heading)]
    assert len(starts) == 1, f"{heading!r} matches {len(starts)} headings"
    first = starts[0]
    level = len(lines[first]) - len(lines[first].lstrip("#"))
    end = len(lines)
    for i in range(first + 1, len(lines)):
        match = _HEADING.match(lines[i])
        if i not in fenced and match and len(match["hashes"]) <= level:
            end = i
            break
    return "\n".join(lines[first:end])
