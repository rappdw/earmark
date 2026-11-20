"""Deterministic Markdown rendering of an ``Analysis``."""

import re
from collections.abc import Callable
from typing import Final

from earmark.models import Analysis, Entity, format_hms

MAX_HEADING_CHARS: Final = 80
type EntityLinker = Callable[[str, Entity], str | None]  # (field, entity) -> name markup
_BLOCK_SYNTAX = re.compile(r"^(?:[#>\-+*=](?: |$)|\d+[.)] |(?:-{3,}|\*{3,}|_{3,}|={3,})$)")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _escape_inline(text: str) -> str:
    return text.replace("<", "&lt;")


def _escape_block(paragraph: str) -> str:
    escaped = _escape_inline(paragraph)
    if _BLOCK_SYNTAX.match(escaped):
        return "\\" + escaped
    return escaped


def _collapse(text: str) -> str:
    return _escape_inline(" ".join(text.split()))


def _paragraphs(text: str) -> list[str]:
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for part in _PARAGRAPH_BREAK.split(normalised):
        collapsed = " ".join(part.split())
        if collapsed:
            out.append(_escape_block(collapsed))
    return out


def _hms(seconds: float | None) -> str | None:
    return None if seconds is None else f"[{format_hms(seconds)}]"


def _entity_line(field: str, e: Entity, entity_link: EntityLinker | None) -> str:
    linked = entity_link(field, e) if entity_link is not None else None
    line = f"- **{linked if linked is not None else _collapse(e.name)}**"
    context = _collapse(e.context)
    if context:
        line += f": {context}"
    if e.timestamps_s:
        line += " " + " ".join(f"[{format_hms(t)}]" for t in e.timestamps_s)
    return line


def render_analysis(
    analysis: Analysis, *, heading_level: int = 2, entity_link: EntityLinker | None = None
) -> str:
    if not 1 <= heading_level <= 5:
        raise ValueError("heading_level must be between 1 and 5")
    h = "#" * heading_level
    s = h + "#"
    blocks: list[str] = []

    prompt = analysis.prompt
    if prompt is None:
        blocks.append(f"{h} Default analysis")
    else:
        heading = " ".join(prompt.split())
        if len(heading) > MAX_HEADING_CHARS:
            heading = heading[: MAX_HEADING_CHARS - 1] + "…"
        blocks.append(f"{h} Analysis: {_escape_inline(heading)}")
        lines = prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        quoted = ["> " + _escape_inline(line.rstrip()) if line.strip() else ">" for line in lines]
        blocks.append("\n".join(quoted))

    blocks.extend(_paragraphs(analysis.summary))
    if analysis.prompt_response is not None:
        blocks.append(f"{s} Answer")
        blocks.extend(_paragraphs(analysis.prompt_response))

    def section(title: str, lines: list[str]) -> None:
        if lines:
            blocks.append(f"{s} {title}")
            blocks.append("\n".join(lines))

    def with_time(ts: float | None, text: str) -> str:
        stamp = _hms(ts)
        return f"- {stamp} {text}" if stamp else f"- {text}"

    section(
        "Key claims", [with_time(c.timestamp_s, _collapse(c.text)) for c in analysis.key_claims]
    )
    section("People", [_entity_line("people", e, entity_link) for e in analysis.people])
    section("Papers", [_entity_line("papers", e, entity_link) for e in analysis.papers])
    section("Companies", [_entity_line("companies", e, entity_link) for e in analysis.companies])
    section("Tools", [_entity_line("tools", e, entity_link) for e in analysis.tools])
    section("Topics", [_entity_line("topics", e, entity_link) for e in analysis.topics])
    section("Open questions", [f"- {_collapse(q)}" for q in analysis.open_questions])
    leads: list[str] = []
    for lead in analysis.research_leads:
        line = with_time(lead.timestamp_s, _collapse(lead.text))
        rationale = _collapse(lead.rationale)
        if rationale:
            line += f" — {rationale}"
        leads.append(line)
    section("Research leads", leads)
    return "\n\n".join(blocks) + "\n"
