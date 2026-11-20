"""The analysis output schema, its wire models, and the mapping to ``Analysis``."""

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from earmark.models import (
    ANALYSIS_SCHEMA_VERSION,
    Analysis,
    Claim,
    Entity,
    ResearchLead,
    Transcript,
    format_hms,
    prompt_hash,
)

SCHEMA_NAME: Final = "earmark_analysis"
MAX_ENTITY_TIMESTAMPS: Final = 20

_TS_DESC = "HH:MM:SS copied from the transcript marker of the supporting line"


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _nullable(description: str) -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}], "description": description}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _array(items: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "array", "items": items, "description": description}


def _entity(kind: str) -> dict[str, Any]:
    return _object(
        {
            "name": _string(f"Name of the {kind}"),
            "context": _string("One sentence on why it comes up in the episode"),
            "timestamps": _array(
                {"type": "string"}, f"Every {_TS_DESC}, where it is discussed (at most five)"
            ),
        }
    )


def _entities(kind: str, description: str) -> dict[str, Any]:
    return _array(_entity(kind), description)


ANALYSIS_OUTPUT_SCHEMA: Final[Mapping[str, Any]] = _object(
    {
        "summary": _string("What the episode is about and its main arguments, in short paragraphs"),
        "key_claims": _array(
            _object(
                {
                    "text": _string("The claim or argument, saying who makes it when clear"),
                    "timestamp": _nullable(_TS_DESC),
                }
            ),
            "The most important claims or arguments",
        ),
        "people": _entities("person", "People discussed or quoted"),
        "papers": _entities("paper", "Papers, books, articles, reports, and studies mentioned"),
        "companies": _entities("company", "Companies, organisations, and institutions"),
        "tools": _entities("tool", "Software, products, methods, datasets, and techniques"),
        "topics": _entities("topic", "The main subjects, as short noun phrases"),
        "open_questions": _array(
            {"type": "string"}, "Questions the episode raises but does not settle"
        ),
        "research_leads": _array(
            _object(
                {
                    "text": _string("A concrete thing worth following up"),
                    "rationale": _string("Why it is worth following up"),
                    "timestamp": _nullable(_TS_DESC),
                }
            ),
            "Concrete follow-ups",
        ),
        "prompt_response": _nullable(
            "The answer to the listener's prompt, or null when there is no prompt"
        ),
    }
)


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WireClaim(_Wire):
    text: str
    timestamp: str | None


class WireEntity(_Wire):
    name: str
    context: str
    timestamps: list[str]


class WireLead(_Wire):
    text: str
    rationale: str
    timestamp: str | None


class AnalysisOutput(_Wire):
    summary: str
    key_claims: list[WireClaim]
    people: list[WireEntity]
    papers: list[WireEntity]
    companies: list[WireEntity]
    tools: list[WireEntity]
    topics: list[WireEntity]
    open_questions: list[str]
    research_leads: list[WireLead]
    prompt_response: str | None


_HMS = re.compile(r"(\d+):([0-5]\d):([0-5]\d)(?:\.\d+)?")
_MS = re.compile(r"(\d{1,2}):([0-5]\d)(?:\.\d+)?")


def parse_timestamp(value: str) -> float | None:
    """Seconds from ``H+:MM:SS`` or ``M?M:SS`` (optionally in brackets); else ``None``."""
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    m = _HMS.fullmatch(text)
    if m is not None:
        h, mi, s = (int(g) for g in m.groups())
        return float(h * 3600 + mi * 60 + s)
    m = _MS.fullmatch(text)
    if m is not None:
        mi, s = (int(g) for g in m.groups())
        return float(mi * 60 + s)
    return None


def effective_duration(transcript: Transcript) -> float:
    """max(transcript.duration_s, end of the last segment)."""
    end = max((seg.end for seg in transcript.segments), default=0.0)
    return max(transcript.duration_s, end)


def plural(n: int, noun: str) -> str:
    return noun if n == 1 else noun + "s"


def _line(value: str) -> str:
    return " ".join(value.split())


class _Counts:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.beyond = 0
        self.unreadable = 0
        self.extra = 0
        self.empty = 0

    def timestamp(self, raw: str | None) -> float | None:
        if raw is None:
            return None
        seconds = parse_timestamp(raw)
        if seconds is None:
            self.unreadable += 1
            return None
        if seconds > self.limit:
            self.beyond += 1
            return None
        return seconds

    def entity_timestamps(self, raws: list[str]) -> tuple[float, ...]:
        kept = {s for raw in raws if (s := self.timestamp(raw)) is not None}
        ordered = sorted(kept)
        self.extra += max(len(ordered) - MAX_ENTITY_TIMESTAMPS, 0)
        return tuple(ordered[:MAX_ENTITY_TIMESTAMPS])


def build_analysis(
    output: AnalysisOutput,
    *,
    episode_key: str,
    prompt: str | None,
    model: str,
    created_at: datetime,
    duration_s: float,
    warnings: Sequence[str] = (),
) -> Analysis:
    """Validate-and-map the model's answer. Pure: no I/O and no clock."""
    counts = _Counts(math.ceil(duration_s))

    claims: list[Claim] = []
    for c in output.key_claims:
        text = _line(c.text)
        if not text:
            counts.empty += 1
            continue
        claims.append(Claim(text=text, timestamp_s=counts.timestamp(c.timestamp)))

    def entities(items: list[WireEntity]) -> tuple[Entity, ...]:
        result: list[Entity] = []
        for e in items:
            name = _line(e.name)
            if not name:
                counts.empty += 1
                continue
            result.append(
                Entity(
                    name=name,
                    context=_line(e.context),
                    timestamps_s=counts.entity_timestamps(e.timestamps),
                )
            )
        return tuple(result)

    people = entities(output.people)
    papers = entities(output.papers)
    companies = entities(output.companies)
    tools = entities(output.tools)
    topics = entities(output.topics)

    questions: list[str] = []
    for q in output.open_questions:
        text = _line(q)
        if text:
            questions.append(text)
        else:
            counts.empty += 1

    leads: list[ResearchLead] = []
    for lead in output.research_leads:
        text = _line(lead.text)
        if not text:
            counts.empty += 1
            continue
        leads.append(
            ResearchLead(
                text=text,
                rationale=_line(lead.rationale),
                timestamp_s=counts.timestamp(lead.timestamp),
            )
        )

    response: str | None = None
    unanswered = False
    if prompt is not None:
        stripped = (output.prompt_response or "").strip()
        if stripped:
            response = stripped
        else:
            unanswered = True

    notes = list(warnings)
    if counts.beyond:
        notes.append(
            f"dropped {counts.beyond} {plural(counts.beyond, 'timestamp')} beyond the "
            f"episode's duration ({format_hms(duration_s)})"
        )
    if counts.unreadable:
        notes.append(
            f"dropped {counts.unreadable} unreadable {plural(counts.unreadable, 'timestamp')}"
        )
    if counts.extra:
        notes.append(
            f"dropped {counts.extra} extra entity {plural(counts.extra, 'timestamp')} "
            f"(at most {MAX_ENTITY_TIMESTAMPS} each)"
        )
    if counts.empty:
        notes.append(f"dropped {counts.empty} empty {plural(counts.empty, 'item')}")
    if unanswered:
        notes.append("the model did not answer the prompt")

    return Analysis(
        episode_key=episode_key,
        prompt=prompt,
        prompt_hash=prompt_hash(prompt, model),
        model=model,
        schema_version=ANALYSIS_SCHEMA_VERSION,
        created_at=created_at,
        summary=output.summary.strip(),
        key_claims=tuple(claims),
        people=people,
        papers=papers,
        companies=companies,
        tools=tools,
        topics=topics,
        open_questions=tuple(questions),
        research_leads=tuple(leads),
        prompt_response=response,
        warnings=tuple(notes),
    )
