"""The note file format: frontmatter emit, split and merge, identity, managed regions.

Existing frontmatter is never parsed as YAML. It is split into lines and top-level keys, so
whatever the user (or Obsidian) wrote is kept verbatim, except earmark's own keys. Steps 13
and 18 reuse these functions.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal

from earmark.models import scrub_surrogates
from earmark.vault.sanitize import yaml_quote

type YamlValue = str | date | datetime | tuple[str, ...] | None  # datetime is naive local
type NoteKind = Literal["episode", "transcript", "entity", "digest"]

BEGIN_MARKER: Final = re.compile(r"<!-- earmark:begin(?: hash=([0-9a-f]{16}))? -->")
END_MARKER: Final = "<!-- earmark:end -->"
_KEY_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_KEY_LINE: Final = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]|\r?$)")
_DIGEST_ID: Final = re.compile(r"\d{4}-W\d{2}|\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}")
_BOM: Final = "\ufeff"
_LINE_END: Final = "\r\n \t"
_WIKILINK: Final = re.compile(r"\[\[([^\[\]|\n]*)(\|[^\[\]\n]*)?\]\]")
_MD_LINK_DEST: Final = re.compile(r"\]\(([^()\s]*)\)")


def begin_marker(digest: str) -> str:
    return f"<!-- earmark:begin hash={digest} -->"


def region_digest(region: str) -> str:
    data = region.replace("\r\n", "\n").encode("utf-8", "surrogateescape")
    return hashlib.sha256(data).hexdigest()[:16]


def emit_frontmatter(fields: Sequence[tuple[str, YamlValue]]) -> list[str]:
    lines: list[str] = []
    for key, value in fields:
        if not _KEY_NAME.fullmatch(key):
            raise ValueError(f"bad frontmatter key: {key!r}")
        if value is None:
            lines.append(f"{key}: null\n")
        elif isinstance(value, str):
            lines.append(f"{key}: {yaml_quote(value)}\n")
        elif isinstance(value, datetime):
            if value.tzinfo is not None:
                raise ValueError("frontmatter date-times must be naive local time")
            lines.append(f"{key}: {value.isoformat(timespec='seconds')}\n")
        elif isinstance(value, date):
            lines.append(f"{key}: {value.isoformat()}\n")
        elif not value:
            lines.append(f"{key}: []\n")
        else:
            lines.append(f"{key}:\n")
            lines.extend(f"  - {yaml_quote(item)}\n" for item in value)
    return lines


def _split_lines(text: str) -> tuple[str, ...]:
    """Split on ``\\n`` only, keeping the endings."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class NoteText:
    bom: str  # "\ufeff" or ""
    frontmatter: tuple[str, ...] | None  # lines between the delimiters, endings kept
    body: tuple[str, ...]  # lines after the closing delimiter, endings kept


def _is_delimiter(line: str) -> bool:
    return line.rstrip(_LINE_END) == "---"


def split_note(text: str) -> NoteText:
    bom = _BOM if text.startswith(_BOM) else ""
    lines = _split_lines(text[len(bom) :])
    if lines and _is_delimiter(lines[0]):
        for i in range(1, len(lines)):
            if _is_delimiter(lines[i]):
                return NoteText(bom, lines[1:i], lines[i + 1 :])
    return NoteText(bom, None, lines)


def _unquote(value: str) -> str:
    value = value.strip(_LINE_END)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def note_identity(text: str) -> tuple[NoteKind, str] | None:
    """``(kind, key)`` from the frontmatter, or None. The key is the ``episode_key`` of an
    episode or transcript note, the ``entity_id`` of an entity note, or the ``digest`` id of a
    digest note."""
    frontmatter = split_note(text).frontmatter
    if frontmatter is None:
        return None
    kind: NoteKind | None = None
    episode_key: str | None = None
    entity_id: str | None = None
    digest_id: str | None = None
    for line in frontmatter:
        m = _KEY_LINE.match(line)
        if m is None:
            continue
        value = _unquote(line[line.index(":") + 1 :])
        name = m.group(1)
        if name == "earmark" and kind is None:
            if value == "episode":
                kind = "episode"
            elif value == "transcript":
                kind = "transcript"
            elif value == "entity":
                kind = "entity"
            elif value == "digest":
                kind = "digest"
        elif name == "episode_key" and episode_key is None and re.fullmatch("[0-9a-f]{20}", value):
            episode_key = value
        elif name == "entity_id" and entity_id is None and re.fullmatch("[0-9a-f]{16}", value):
            entity_id = value
        elif name == "digest" and digest_id is None and _DIGEST_ID.fullmatch(value):
            digest_id = value
    if kind == "digest":
        return None if digest_id is None else (kind, digest_id)
    if kind == "entity":
        return None if entity_id is None else (kind, entity_id)
    if kind is None or episode_key is None:
        return None
    return kind, episode_key


def content_digest(owned: Sequence[tuple[str, YamlValue]], region: str) -> str:
    """Digest of what earmark would write: its own frontmatter plus the region."""
    return region_digest("".join(emit_frontmatter(owned)) + scrub_surrogates(region))


def unlinked(region: str) -> str:
    """The region with LF endings and every link destination blanked, to compare two regions
    while ignoring where their links point (Obsidian rewrites targets on a rename)."""
    text = region.replace("\r\n", "\n")
    text = _WIKILINK.sub(lambda m: "[[" + (m.group(2) or "") + "]]", text)
    return _MD_LINK_DEST.sub("]()", text)


class DamagedNote(Exception):  # noqa: N818  # name fixed by the plan
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "missing" or "damaged"


@dataclass(frozen=True, slots=True)
class Updated:
    text: str
    edited: bool  # the old region was changed by hand
    old_digest: str  # region_digest of the old region


def _check_region(region: str) -> None:
    if region and not region.endswith("\n"):
        raise ValueError("a region must be empty or end with a newline")
    for line in _split_lines(region):
        stripped = line.rstrip(_LINE_END)
        if BEGIN_MARKER.fullmatch(stripped) or stripped == END_MARKER:
            raise ValueError("a region must not contain a marker line")


def _managed_block(region: str) -> str:
    return begin_marker(region_digest(region)) + "\n" + region + END_MARKER + "\n"


def new_note(
    owned: Sequence[tuple[str, YamlValue]],
    seeds: Sequence[tuple[str, YamlValue]],
    region: str,
) -> str:
    region = scrub_surrogates(region)
    _check_region(region)
    front = "".join(emit_frontmatter(owned) + emit_frontmatter(seeds))
    return "---\n" + front + "---\n" + _managed_block(region)


def _merge_frontmatter(
    lines: Sequence[str],
    owned: Sequence[tuple[str, YamlValue]],
    seeds: Sequence[tuple[str, YamlValue]],
) -> list[str]:
    owned_names = {k for k, _ in owned}
    kept: list[str] = []
    seen: set[str] = set()
    dropping = False
    for line in lines:
        m = _KEY_LINE.match(line)
        if m is not None:
            name = m.group(1)
            if name in owned_names:
                dropping = True
                continue
            dropping = False
            seen.add(name)
            kept.append(line)
            continue
        bare = line.rstrip(_LINE_END)
        if dropping and (line[:1] in {" ", "\t"} or bare == "-" or line.startswith("- ")):
            continue
        dropping = False
        kept.append(line)
    missing = [(k, v) for k, v in seeds if k not in seen]
    return emit_frontmatter(owned) + kept + emit_frontmatter(missing)


def _find_markers(body: Sequence[str]) -> tuple[int, int]:
    begins: list[int] = []
    ends: list[int] = []
    for i, line in enumerate(body):
        stripped = line.rstrip(_LINE_END)
        if BEGIN_MARKER.fullmatch(stripped):
            begins.append(i)
        elif stripped == END_MARKER:
            ends.append(i)
    if not begins and not ends:
        raise DamagedNote("missing")
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise DamagedNote("damaged")
    return begins[0], ends[0]


def update_note(
    existing: str,
    owned: Sequence[tuple[str, YamlValue]],
    seeds: Sequence[tuple[str, YamlValue]],
    region: str,
) -> Updated:
    region = scrub_surrogates(region)
    _check_region(region)
    note = split_note(existing)
    begin, end = _find_markers(note.body)
    match = BEGIN_MARKER.fullmatch(note.body[begin].rstrip(_LINE_END))
    stamped = match.group(1) if match else None
    old_region = "".join(note.body[begin + 1 : end])
    old_digest = region_digest(old_region)
    edited = (stamped is None or stamped != old_digest) and unlinked(old_region) != unlinked(region)
    front = _merge_frontmatter(note.frontmatter or (), owned, seeds)
    text = (
        note.bom
        + "---\n"
        + "".join(front)
        + "---\n"
        + "".join(note.body[:begin])
        + _managed_block(region)
        + "".join(note.body[end + 1 :])
    )
    return Updated(text=text, edited=edited, old_digest=old_digest)
