"""Cutting a transcript into parts for map-reduce. Pure: standard library plus ``models``.

Parts break only at segment boundaries (a segment too long for one part is split at
sentence ends, see ``plan_chunks``), cover every segment exactly once, and keep the
``[HH:MM:SS]`` markers absolute.
"""

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from earmark.analyze.llm.base import LLMRequest
from earmark.models import Segment, format_hms

BYTES_PER_TOKEN: Final = 3
LINE_TOKENS: Final = 4
REQUEST_OVERHEAD_TOKENS: Final = 64

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def estimate_tokens(text: str) -> int:
    """ceil(utf8 bytes / 3) + 4 per newline. Conservative for modern tokenizers."""
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN) + LINE_TOKENS * text.count("\n")


def estimate_request_tokens(request: LLMRequest) -> int:
    """system + user + the compact JSON schema + a fixed allowance for the chat template."""
    schema = json.dumps(request.schema, separators=(",", ":"))
    return (
        estimate_tokens(request.system)
        + estimate_tokens(request.user)
        + estimate_tokens(schema)
        + REQUEST_OVERHEAD_TOKENS
    )


@dataclass(frozen=True, slots=True)
class Piece:
    segment_index: int  # index into transcript.segments
    start: float  # absolute, the segment's start
    text: str  # whitespace-collapsed segment text (or one split of it)
    split: bool = False  # True if the segment was too long and was split

    def line(self) -> str:
        """``[HH:MM:SS] text\\n``, or ``[HH:MM:SS]\\n`` for empty text, as Transcript.to_text."""
        marker = f"[{format_hms(self.start)}]"
        return (f"{marker} {self.text}" if self.text else marker) + "\n"


@dataclass(frozen=True, slots=True)
class Chunk:
    pieces: tuple[Piece, ...]  # non-empty
    start_s: float  # first piece's start
    end_s: float  # max(segment end) over its segments

    def to_text(self) -> str:
        return "".join(p.line() for p in self.pieces)

    def tokens(self) -> int:
        return sum(estimate_tokens(p.line()) for p in self.pieces)


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    chunks: tuple[Chunk, ...]
    split_segments: int  # how many segments had to be split


def _make_chunk(pieces: Sequence[Piece], segments: Sequence[Segment]) -> Chunk:
    start = pieces[0].start
    end = max(segments[i].end for i in {p.segment_index for p in pieces})
    return Chunk(pieces=tuple(pieces), start_s=start, end_s=max(end, start))


def split_chunk(chunk: Chunk, segments: Sequence[Segment]) -> tuple[Chunk, Chunk] | None:
    """Halve at piece index ``len // 2``; ``None`` for a one-piece chunk."""
    if len(chunk.pieces) < 2:
        return None
    mid = len(chunk.pieces) // 2
    return _make_chunk(chunk.pieces[:mid], segments), _make_chunk(chunk.pieces[mid:], segments)


def _pack(
    units: Sequence[str],
    fits: Callable[[str], bool],
    finer: Callable[[str], list[str]],
) -> list[str]:
    """Greedily join ``units`` with single spaces into texts that fit; split a unit that
    does not fit alone with ``finer``."""
    out: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current} {unit}" if current else unit
        if fits(candidate):
            current = candidate
            continue
        if current:
            out.append(current)
            current = ""
        if fits(unit):
            current = unit
        else:
            parts = finer(unit)
            out.extend(parts[:-1])
            current = parts[-1]
    if current:
        out.append(current)
    return out


def _split_characters(word: str, fits: Callable[[str], bool]) -> list[str]:
    parts: list[str] = []
    rest = word
    while rest:
        low, high = 1, len(rest)  # the largest prefix length that fits (at least one char)
        while low < high:
            mid = (low + high + 1) // 2
            if fits(rest[:mid]):
                low = mid
            else:
                high = mid - 1
        parts.append(rest[:low])
        rest = rest[low:]
    return parts


def _split_text(text: str, fits: Callable[[str], bool]) -> list[str]:
    def by_words(sentence: str) -> list[str]:
        return _pack(sentence.split(), fits, lambda word: _split_characters(word, fits))

    return _pack(_SENTENCE_END.split(text), fits, by_words)


def _pieces_of(index: int, segment: Segment, budget_tokens: int) -> list[Piece]:
    text = " ".join(segment.text.split())
    whole = Piece(index, segment.start, text)
    if estimate_tokens(whole.line()) <= budget_tokens:
        return [whole]

    def fits(candidate: str) -> bool:
        return estimate_tokens(Piece(index, segment.start, candidate).line()) <= budget_tokens

    return [Piece(index, segment.start, t, split=True) for t in _split_text(text, fits)]


def plan_chunks(segments: Sequence[Segment], *, budget_tokens: int) -> ChunkPlan:
    """Cut ``segments`` into about ``ceil(total / budget)`` chunks of similar size, none over
    ``budget_tokens``. Deterministic."""
    pieces: list[Piece] = []
    split_segments = 0
    for i, segment in enumerate(segments):
        made = _pieces_of(i, segment, budget_tokens)
        if made and made[0].split:
            split_segments += 1
        pieces.extend(made)
    if not pieces:
        return ChunkPlan(chunks=(), split_segments=0)

    costs = [estimate_tokens(p.line()) for p in pieces]
    total = sum(costs)
    k = max(math.ceil(total / budget_tokens), 1)
    target = math.ceil(total / k)

    chunks: list[Chunk] = []
    current: list[Piece] = []
    used = 0
    for piece, cost in zip(pieces, costs, strict=True):
        if current and (used + cost > budget_tokens or used >= target):
            chunks.append(_make_chunk(current, segments))
            current, used = [], 0
        current.append(piece)
        used += cost
    if current:
        chunks.append(_make_chunk(current, segments))
    return ChunkPlan(chunks=tuple(chunks), split_segments=split_segments)
