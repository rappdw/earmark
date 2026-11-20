"""Text cleanup for untrusted titles and descriptions. Stdlib only."""

import html
import re
import unicodedata
from html.parser import HTMLParser
from typing import Final

_BLOCK_TAGS: Final = frozenset({"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"})
_SKIPPED_TAGS: Final = frozenset({"script", "style"})
_SPACES = re.compile(r"[ \t]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


def clean_text(value: str | None, *, limit: int, keep_newlines: bool = False) -> str:
    """Remove control characters, collapse whitespace, and cap the length at ``limit``."""
    if value is None:
        return ""
    if keep_newlines:
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = "".join(" " if unicodedata.category(c) == "Cc" and c != "\n" else c for c in text)
        lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
        text = _BLANK_RUNS.sub("\n\n", "\n".join(lines))
    else:
        text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in value)
        text = " ".join(text.split())
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def normalize_title(value: str | None) -> str:
    """Casefolded title without punctuation or symbols, for comparing across sources."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    text = "".join(c for c in text if unicodedata.category(c)[0] not in "PS")
    return " ".join(text.split())
