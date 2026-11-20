"""Untrusted text to file names, fold keys, Markdown and YAML. Standard library only."""

import re
import unicodedata
from typing import Final

MAX_NAME_BYTES: Final = 200  # any file or directory name earmark creates
TITLE_BYTES: Final = 120  # title part of a note name
SHOW_BYTES: Final = 100  # show directory name
ELLIPSIS: Final = "…"

_ZWNJ: Final = "\u200c"
_ZWJ: Final = "\u200d"
_SURROGATES: Final = re.compile("[\ud800-\udfff]")
_DEVICE_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_UNSAFE_NAME_CHARS: Final = frozenset(':*?"<>|#^[]')
_MD_SPECIAL: Final = frozenset("`*_[]|~=$%^#")
_YAML_ESCAPED: Final = frozenset(
    [*range(0x20), *range(0x7F, 0xA0), 0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF]
)


def _blen(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def clean_unicode(text: str) -> str:
    """Lone surrogates become U+FFFD, then NFC."""
    return unicodedata.normalize("NFC", _SURROGATES.sub("�", text))


def _is_trailing_joiner(ch: str) -> bool:
    cp = ord(ch)
    return (
        unicodedata.category(ch) in {"Mn", "Mc", "Me"}
        or ch in {_ZWNJ, _ZWJ}
        or 0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or 0x1F3FB <= cp <= 0x1F3FF
    )


def _is_regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _repair_cut(text: str) -> str:
    """Drop a dangling accent, joiner, modifier, or half a flag from the end of a cut."""
    while True:
        before = text
        while text and _is_trailing_joiner(text[-1]):
            text = text[:-1]
        if len(text) >= 2 and text[-2] == _ZWJ:
            text = text[:-2]
        run = 0
        while run < len(text) and _is_regional(text[-1 - run]):
            run += 1
        if run % 2:
            text = text[:-1]
        text = text.rstrip(" .")
        if text == before:
            return text


def fit_bytes(text: str, max_bytes: int) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes at a safe boundary, ending in ``…``."""
    if max_bytes < 8:
        raise ValueError("max_bytes must be at least 8")
    if _blen(text) <= max_bytes:
        return text
    budget = max_bytes - _blen(ELLIPSIS)
    used = 0
    end = 0
    for ch in text:
        size = _blen(ch)
        if used + size > budget:
            break
        used += size
        end += 1
    return _repair_cut(text[:end]) + ELLIPSIS


def is_safe_component(name: str) -> bool:
    if not name or name != unicodedata.normalize("NFC", name):
        return False
    try:
        if len(name.encode("utf-8")) > MAX_NAME_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    if name[0] in {".", " "} or name[-1] in {".", " "} or "%%" in name:
        return False
    for ch in name:
        if ch in "/\\\x00" or ch in _UNSAFE_NAME_CHARS:
            return False
        if unicodedata.category(ch) in {"Cc", "Cf"} and ch not in {_ZWNJ, _ZWJ}:
            return False
    return True


def file_component(text: str, *, max_bytes: int, fallback: str) -> str:
    """Turn untrusted text into one safe, non-empty file or directory name."""
    if not is_safe_component(fallback):
        raise ValueError("fallback must itself be a safe name")
    max_bytes = min(max_bytes, MAX_NAME_BYTES)
    out: list[str] = []
    for ch in clean_unicode(text):
        cat = unicodedata.category(ch)
        if cat in {"Cc", "Zl", "Zp"} or ch.isspace():
            out.append(" ")
        elif (cat in {"Cf", "Cs", "Cn"} or ch == "�") and ch not in {_ZWNJ, _ZWJ}:
            continue
        else:
            out.append(ch)
    name = "".join(out)
    name = name.replace(": ", " - ")
    for ch, repl in (
        ("/", "-"),
        ("\\", "-"),
        ("|", "-"),
        (":", "-"),
        ('"', "'"),
        ("[", "("),
        ("]", ")"),
    ):
        name = name.replace(ch, repl)
    name = re.sub(r"[*?<>#^]", "", name)
    name = re.sub(r"%{2,}", "%", name)
    name = re.sub(r" {2,}", " ", name)
    name = name.strip(" .")
    head, dot, tail = name.partition(".")
    if head.strip().casefold() in _DEVICE_NAMES:
        name = head + "_" + dot + tail
    if not name:
        name = fallback
    name = unicodedata.normalize("NFC", fit_bytes(name, max_bytes))
    if not is_safe_component(name):
        # Only reachable through exotic inputs (for example a `%%` made by the cut); fall back.
        name = fallback
    return name


def fold_key(name: str) -> str:
    """Comparison key for names on case- and normalization-insensitive filesystems."""
    nfkc = unicodedata.normalize("NFKC", name)
    return unicodedata.normalize("NFKC", nfkc.casefold())


def escape_markdown(text: str) -> str:
    """Whitespace-collapse and Markdown-escape untrusted inline text."""
    out: list[str] = []
    for ch in " ".join(text.split()):
        if ch == "\\":
            out.append("\\\\")
        elif ch in _MD_SPECIAL:
            out.append("\\" + ch)
        elif ch == "<":
            out.append("&lt;")
        else:
            out.append(ch)
    return "".join(out)


def yaml_quote(text: str) -> str:
    """A one-line YAML double-quoted scalar; only quote, backslash and control characters escape."""
    out: list[str] = []
    for ch in clean_unicode(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) in _YAML_ESCAPED:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def wikilink_target_ok(target: str) -> bool:
    if not target or "%%" in target:
        return False
    return not any(ch in target for ch in "#|^:[]\n\r")
