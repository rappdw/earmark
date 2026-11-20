"""Merging the notes of transcript parts. Pure: no I/O.

Entities repeated across parts are merged in code (normalised names, union of
timestamps), before the reduce request and again on its answer.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from earmark.analyze.schema import AnalysisOutput, WireClaim, WireEntity, WireLead, parse_timestamp
from earmark.models import entity_key, format_hms

__all__ = [
    "ENTITY_FIELDS",
    "PartNote",
    "entity_key",
    "finalize_entities",
    "merge_entities",
    "merged_part_entities",
    "restrict_timestamps",
]

ENTITY_FIELDS: Final = ("people", "papers", "companies", "tools", "topics")


def _union_timestamps(groups: Sequence[Sequence[str]]) -> list[str]:
    readable: set[float] = set()
    unreadable: list[str] = []
    for group in groups:
        for raw in group:
            seconds = parse_timestamp(raw)
            if seconds is None:
                if raw not in unreadable:
                    unreadable.append(raw)
            else:
                readable.add(seconds)
    return [format_hms(s) for s in sorted(readable)] + unreadable


def merge_entities(groups: Sequence[Sequence[WireEntity]]) -> list[WireEntity]:
    """Merge by ``entity_key`` in first-seen order. The first name and the first non-blank
    context win; timestamps are unioned. A name with an empty key is passed through."""
    slots: list[list[WireEntity] | WireEntity] = []
    by_key: dict[str, list[WireEntity]] = {}
    for group in groups:
        for entity in group:
            key = entity_key(entity.name)
            if not key:
                slots.append(entity)
            elif key in by_key:
                by_key[key].append(entity)
            else:
                members = [entity]
                by_key[key] = members
                slots.append(members)
    merged: list[WireEntity] = []
    for slot in slots:
        if isinstance(slot, WireEntity):
            merged.append(slot)
            continue
        context = next((e.context for e in slot if e.context.strip()), "")
        merged.append(
            WireEntity(
                name=slot[0].name,
                context=context,
                timestamps=_union_timestamps([e.timestamps for e in slot]),
            )
        )
    return merged


@dataclass(frozen=True, slots=True)
class PartNote:
    index: int  # 1-based
    count: int
    start_s: float
    end_s: float
    output: AnalysisOutput


def restrict_timestamps(
    output: AnalysisOutput, start_s: float, end_s: float
) -> tuple[AnalysisOutput, int]:
    """Drop readable timestamps outside [floor(start_s), ceil(end_s)]; return the count dropped.
    Unreadable ones stay: ``build_analysis`` counts them."""
    low, high = math.floor(start_s), math.ceil(end_s)
    dropped = 0

    def outside(raw: str) -> bool:
        seconds = parse_timestamp(raw)
        return seconds is not None and not low <= seconds <= high

    def single(raw: str | None) -> str | None:
        nonlocal dropped
        if raw is not None and outside(raw):
            dropped += 1
            return None
        return raw

    def entities(items: list[WireEntity]) -> list[WireEntity]:
        nonlocal dropped
        result: list[WireEntity] = []
        for e in items:
            kept = [t for t in e.timestamps if not outside(t)]
            dropped += len(e.timestamps) - len(kept)
            result.append(e.model_copy(update={"timestamps": kept}))
        return result

    claims = [WireClaim(text=c.text, timestamp=single(c.timestamp)) for c in output.key_claims]
    leads = [
        WireLead(text=ld.text, rationale=ld.rationale, timestamp=single(ld.timestamp))
        for ld in output.research_leads
    ]
    update: dict[str, object] = {"key_claims": claims, "research_leads": leads}
    for name in ENTITY_FIELDS:
        update[name] = entities(getattr(output, name))
    return output.model_copy(update=update), dropped


def merged_part_entities(notes: Sequence[PartNote]) -> dict[str, list[WireEntity]]:
    """For each entity kind: ``merge_entities`` over the notes' lists, in part order."""
    return {
        name: merge_entities([getattr(note.output, name) for note in notes])
        for name in ENTITY_FIELDS
    }


def finalize_entities(
    output: AnalysisOutput, premerged: Mapping[str, Sequence[WireEntity]]
) -> AnalysisOutput:
    """Merge duplicates inside each final list, then union each entity's timestamps with the
    premerged entity of the same key and kind."""
    update: dict[str, object] = {}
    for name in ENTITY_FIELDS:
        earlier = {entity_key(e.name): e for e in premerged.get(name, ())}
        result: list[WireEntity] = []
        for entity in merge_entities([getattr(output, name)]):
            key = entity_key(entity.name)
            if key and key in earlier:
                entity = merge_entities([[entity], [earlier[key]]])[0]
            result.append(entity)
        update[name] = result
    return output.model_copy(update=update)
