"""Turning an episode's transcript and analyses into index rows. Pure.

Transcript rows follow segment boundaries (a segment is cut only when it alone is too long
for one row); every analysis becomes rows of its own with a ``kind``. Nothing here embeds or
touches a database. ``fits`` is supplied by the embedder so a row is never truncated by the
model's tokenizer.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC
from typing import Final, Self

from earmark.analyze.chunking import split_text
from earmark.artifacts import ArtifactStore
from earmark.index.search import RowKind
from earmark.models import Analysis, Episode, StoreResult, Transcript

INDEX_VERSION: Final = 1
MAX_ROW_CHARS: Final = 1000  # a row's text, after whitespace collapsing
MAX_CHUNK_SPAN_S: Final = 120.0  # a transcript chunk covers at most this much time
PROMPT_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class NotePaths:
    note_path: str | None = None
    transcript_note_path: str | None = None

    @classmethod
    def from_store_result(cls, stored: StoreResult) -> Self:
        return cls(stored.note_path, stored.transcript_note_path)


@dataclass(frozen=True, slots=True)
class EpisodeDocs:
    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]  # sorted by (created_at, prompt_hash), at least one


@dataclass(frozen=True, slots=True)
class IndexRow:
    id: str
    episode_key: str
    kind: RowKind
    prompt_hash: str | None
    prompt: str | None
    ordinal: int
    text: str
    start_s: float | None
    end_s: float | None
    show_title: str
    episode_title: str
    published_at: str | None  # ISO 8601 in UTC, or None
    episode_url: str
    note_path: str | None
    transcript_note_path: str | None


@dataclass(slots=True)
class _Run:
    """A transcript chunk under construction."""

    start: float
    end: float
    texts: list[str]


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _prompt_label(prompt: str | None) -> str | None:
    if prompt is None:
        return None
    return prompt if len(prompt) <= PROMPT_CHARS else prompt[:PROMPT_CHARS] + "…"


def _pieces(text: str, fits: Callable[[str], bool]) -> list[str]:
    """``text`` as one or more pieces, each within MAX_ROW_CHARS and embeddable."""

    def ok(candidate: str) -> bool:
        return len(candidate) <= MAX_ROW_CHARS and fits(candidate)

    if ok(text):
        return [text]
    return [p for p in split_text(text, ok) if p]


def build_rows(
    docs: EpisodeDocs, notes: NotePaths, *, fits: Callable[[str], bool]
) -> tuple[IndexRow, ...]:
    episode = docs.episode
    published = (
        None if episode.published_at is None else episode.published_at.astimezone(UTC).isoformat()
    )
    rows: list[IndexRow] = []

    def add(
        kind: RowKind,
        text: str,
        ordinal: int,
        *,
        analysis: Analysis | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> None:
        h = None if analysis is None else analysis.prompt_hash
        rows.append(
            IndexRow(
                id=f"{episode.key}:{kind}:{h or '-'}:{ordinal}",
                episode_key=episode.key,
                kind=kind,
                prompt_hash=h,
                prompt=None if analysis is None else _prompt_label(analysis.prompt),
                ordinal=ordinal,
                text=text,
                start_s=start_s,
                end_s=end_s,
                show_title=episode.show_title,
                episode_title=episode.title,
                published_at=published,
                episode_url=episode.url,
                note_path=notes.note_path,
                transcript_note_path=notes.transcript_note_path,
            )
        )

    # Transcript rows, in segment order.
    ordinal = 0
    run: _Run | None = None

    def close() -> None:
        nonlocal run, ordinal
        if run is not None:
            add(
                "transcript",
                " ".join(run.texts),
                ordinal,
                start_s=run.start,
                end_s=max(run.end, run.start),
            )
            ordinal += 1
            run = None

    for seg in docs.transcript.segments:
        text = _collapse(seg.text)
        if not text:
            continue
        if len(text) > MAX_ROW_CHARS or not fits(text):
            close()
            for piece in _pieces(text, fits):
                add("transcript", piece, ordinal, start_s=seg.start, end_s=seg.end)
                ordinal += 1
            continue
        if run is not None:
            joined = " ".join([*run.texts, text])
            if (
                len(joined) <= MAX_ROW_CHARS
                and seg.end - run.start <= MAX_CHUNK_SPAN_S
                and fits(joined)
            ):
                run.texts.append(text)
                run.end = max(run.end, seg.end)
                continue
            close()
        run = _Run(seg.start, seg.end, [text])
    close()

    # Analysis rows.
    for analysis in docs.analyses:
        counters: dict[RowKind, int] = {}

        def emit(
            kind: RowKind,
            raw: str,
            timestamp: float | None = None,
            *,
            analysis: Analysis = analysis,
            counters: dict[RowKind, int] = counters,
        ) -> None:
            text = _collapse(raw)
            if not text:
                return
            for piece in _pieces(text, fits):
                n = counters.get(kind, 0)
                counters[kind] = n + 1
                add(kind, piece, n, analysis=analysis, start_s=timestamp)

        emit("summary", analysis.summary)
        for claim in analysis.key_claims:
            emit("claim", claim.text, claim.timestamp_s)
        for lead in analysis.research_leads:
            text = _collapse(lead.text)
            why = _collapse(lead.rationale)
            emit("lead", f"{text} — {why}" if why and text else text, lead.timestamp_s)
        for question in analysis.open_questions:
            emit("question", question)
        if analysis.prompt_response is not None:
            emit("answer", analysis.prompt_response)
    return tuple(rows)


def rows_digest(rows: Sequence[IndexRow], *, model: str, dim: int) -> str:
    payload = json.dumps(
        {
            "index_version": INDEX_VERSION,
            "model": model,
            "dim": dim,
            "rows": [asdict(r) for r in rows],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def load_episode_docs(artifacts: ArtifactStore, key: str) -> EpisodeDocs | None:
    """The episode's inputs, or ``None`` when it has no episode, transcript, or analysis.

    ``PermanentError`` from a corrupt file propagates.
    """
    episode = artifacts.read_episode(key)
    if episode is None:
        return None
    transcript = artifacts.read_transcript(key)
    if transcript is None:
        return None
    analyses = artifacts.list_analyses(key)
    if not analyses:
        return None
    return EpisodeDocs(episode, transcript, tuple(analyses))
