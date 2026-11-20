"""Reading the user-edited ``<VAULT_SUBDIR>/aliases.yaml``.

PyYAML is imported lazily and only used to *compose* the node tree: nothing is constructed,
so tags cannot run code, every scalar keeps its raw text (``1984``, ``No``), and every node
has a line number. YAML aliases (``*name``) are refused. See ``docs/steps/13-entities.md``
section 5.3 for the format.
"""

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from earmark.errors import PermanentError
from earmark.models import entity_key, scrub_surrogates
from earmark.vault.entities import KIND_BY_FIELD, KINDS, EntityKind
from earmark.vault.fs import SymlinkRefused, VaultDir

if TYPE_CHECKING:
    from yaml.nodes import MappingNode, Node

ALIASES_FILE: Final = "aliases.yaml"
MAX_ALIASES_BYTES: Final = 256 * 1024
MAX_ALIAS_NAMES: Final = 10_000
MAX_REPORTED: Final = 5
_QUOTE_CHARS: Final = 60
_NULL_TAG: Final = "tag:yaml.org,2002:null"
_KIND_LIST: Final = ", ".join(info.field for info in KINDS)


@dataclass(frozen=True, slots=True)
class AliasEntry:
    kind: EntityKind
    alias_key: str  # entity_key of a listed name; equals canonical_key for the entry itself
    canonical_key: str
    canonical_name: str  # whitespace-collapsed, as written


@dataclass(frozen=True, slots=True)
class AliasFile:
    entries: tuple[AliasEntry, ...]  # file order
    problems: tuple[str, ...]  # entry-level, already formatted, at most MAX_REPORTED + 1


class AliasFileError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _AliasUsed(Exception):  # noqa: N818  # internal signal
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.line = line


@functools.cache
def _loader() -> Any:
    import yaml

    class NoAliasLoader(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise _AliasUsed(self.peek_event().start_mark.line + 1)  # type: ignore[no-untyped-call]  # PyYAML stubs leave peek_event untyped
            return super().compose_node(parent, index)

    return NoAliasLoader


def _compose(text: str) -> "Node | None":
    import yaml

    try:
        return yaml.compose(text, Loader=_loader())
    except _AliasUsed as exc:
        raise AliasFileError(
            f"{ALIASES_FILE} uses a YAML alias (*name) at line {exc.line}; "
            "aliases are not supported"
        ) from None
    except yaml.YAMLError as exc:
        problem = " ".join(str(getattr(exc, "problem", None) or "invalid").split())
        if "another document" in str(getattr(exc, "problem", "")) or "another document" in str(
            getattr(exc, "context", "")
        ):
            raise AliasFileError(f"{ALIASES_FILE} holds more than one document") from None
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 1
        raise AliasFileError(
            f"{ALIASES_FILE} is not valid YAML at line {line}: {problem[:100]}"
        ) from None
    except RecursionError:
        raise AliasFileError(f"{ALIASES_FILE} nests too deeply") from None


def _shown(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > _QUOTE_CHARS:
        return collapsed[: _QUOTE_CHARS - 1] + "…"
    return collapsed


def _name_of(raw: str) -> str:
    return " ".join(scrub_surrogates(raw).split())


def _line(node: "Node") -> int:
    return int(node.start_mark.line) + 1


def _is_null(node: "Node") -> bool:
    from yaml.nodes import ScalarNode

    return isinstance(node, ScalarNode) and (node.tag == _NULL_TAG or node.value == "")


class _Reader:
    def __init__(self) -> None:
        self.entries: list[AliasEntry] = []
        self.problems: list[str] = []
        self._listed: dict[tuple[EntityKind, str], int] = {}
        self._names = 0
        self._capped = False

    def problem(self, line: int, message: str) -> None:
        self.problems.append(f"{ALIASES_FILE} line {line}: {message}")

    def _take_name(self, line: int) -> bool:
        if self._names >= MAX_ALIAS_NAMES:
            if not self._capped:
                self._capped = True
                self.problem(line, f"more than {MAX_ALIAS_NAMES} names; the rest are ignored")
            return False
        self._names += 1
        return True

    def read(self, root: "MappingNode") -> None:
        from yaml.nodes import MappingNode, ScalarNode

        for key_node, value_node in root.value:
            kind_info = (
                KIND_BY_FIELD.get(key_node.value.strip())
                if isinstance(key_node, ScalarNode)
                else None
            )
            if kind_info is None:
                shown = _shown(key_node.value) if isinstance(key_node, ScalarNode) else "(not text)"
                self.problem(_line(key_node), f"unknown kind {shown}; use {_KIND_LIST}")
                continue
            if _is_null(value_node):
                continue
            if not isinstance(value_node, MappingNode):
                self.problem(
                    _line(value_node), f"{kind_info.field} must be a mapping of names to aliases"
                )
                continue
            for name_node, alias_node in value_node.value:
                self._entry(kind_info.kind, name_node, alias_node)

    def _key_of(self, line: int, name: str) -> str | None:
        key = entity_key(name)
        if not key:
            self.problem(line, f"{_shown(name)!r} has no letters or digits")
            return None
        return key

    def _entry(self, kind: EntityKind, name_node: "Node", alias_node: "Node") -> None:
        from yaml.nodes import ScalarNode, SequenceNode

        line = _line(name_node)
        if not isinstance(name_node, ScalarNode):
            self.problem(line, "a name must be text, not a list or mapping")
            return
        name = _name_of(name_node.value)
        key = self._key_of(line, name)
        if key is None:
            return
        if (kind, key) in self._listed:
            self.problem(
                line,
                f"{_shown(name)} is already listed at line {self._listed[(kind, key)]}; "
                "aliases are not chained, so this entry is skipped",
            )
            return
        if not self._take_name(line):
            return
        self._listed[(kind, key)] = line
        self.entries.append(AliasEntry(kind, key, key, name))

        if _is_null(alias_node):
            return
        if isinstance(alias_node, ScalarNode):
            items: list[Node] = [alias_node]
        elif isinstance(alias_node, SequenceNode):
            items = list(alias_node.value)
        else:
            self.problem(_line(alias_node), "aliases must be a name or a list of names")
            return
        for item in items:
            item_line = _line(item)
            if not isinstance(item, ScalarNode):
                self.problem(item_line, "an alias must be text, not a list or mapping")
                continue
            alias = _name_of(item.value)
            alias_key = self._key_of(item_line, alias)
            if alias_key is None or alias_key == key:
                continue
            if (kind, alias_key) in self._listed:
                self.problem(
                    item_line,
                    f"{_shown(alias)} is already listed at line {self._listed[(kind, alias_key)]}",
                )
                continue
            if not self._take_name(item_line):
                continue
            self._listed[(kind, alias_key)] = item_line
            self.entries.append(AliasEntry(kind, alias_key, key, name))


def parse_aliases(text: str) -> AliasFile:
    from yaml.nodes import MappingNode

    root = _compose(text.removeprefix("﻿"))
    reader = _Reader()
    if root is not None:
        if not isinstance(root, MappingNode):
            raise AliasFileError(f"{ALIASES_FILE} must be a mapping of kinds ({_KIND_LIST})")
        reader.read(root)
    problems = reader.problems
    if len(problems) > MAX_REPORTED:
        extra = len(problems) - MAX_REPORTED
        problems = [*problems[:MAX_REPORTED], f"{ALIASES_FILE}: and {extra} more problems"]
    return AliasFile(tuple(reader.entries), tuple(problems))


def read_aliases(root: VaultDir) -> AliasFile | None:
    """The parsed file, or None when there is none. Raises ``AliasFileError``."""
    try:
        got = root.read(ALIASES_FILE, max_bytes=MAX_ALIASES_BYTES)
    except SymlinkRefused:
        raise AliasFileError(f"{ALIASES_FILE} is a symlink or not a regular file") from None
    except PermanentError:
        raise AliasFileError(f"{ALIASES_FILE} is larger than 256 KiB") from None
    if got is None:
        return None
    try:
        text = got[0].decode("utf-8")
    except UnicodeDecodeError:
        raise AliasFileError(f"{ALIASES_FILE} is not UTF-8 text") from None
    return parse_aliases(text)
