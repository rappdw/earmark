"""Synthetic long transcripts and a scripted responder for map-reduce tests."""

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from earmark.analyze.llm.base import LLMRequest, LLMResponse
from earmark.analyze.prompts import MAP_SYSTEM_PROMPT, REDUCE_SYSTEM_PROMPT
from earmark.models import Segment, Transcript
from tests.analysis_samples import wire_payload
from tests.factories import make_episode

FROM = "FROM"  # in a people_by_part timestamp: replaced by the part's start
_PART_TAG = re.compile(r'<transcript part="(\d+)" of="(\d+)" from="([\d:]+)" to="([\d:]+)">')


def long_transcript(*, hours: float, seconds_per_segment: float = 5.0) -> Transcript:
    count = int(hours * 3600 / seconds_per_segment)
    segments = tuple(
        Segment(
            start=i * seconds_per_segment,
            end=(i + 1) * seconds_per_segment,
            text=f"Segment {i} talks about topic {i % 37}.",
        )
        for i in range(count)
    )
    return Transcript(
        episode_key=make_episode(guid="urn:example:guid-1").key,
        language="en",
        duration_s=count * seconds_per_segment,
        model="fake-asr",
        segments=segments,
    )


def kind_of(request: LLMRequest) -> str:
    if request.system == MAP_SYSTEM_PROMPT:
        return "map"
    if request.system == REDUCE_SYSTEM_PROMPT:
        return "reduce"
    return "other"


class MapReduceResponder:
    """Answers map requests with a payload inside the part, reduce requests with a default."""

    def __init__(
        self,
        people_by_part: Mapping[int, list[dict[str, Any]]] | None = None,
        reduce_payload: dict[str, Any] | None = None,
        map_summary: str | None = None,
    ) -> None:
        self._people = people_by_part or {}
        self._reduce = reduce_payload
        self._map_summary = map_summary
        self.maps: list[LLMRequest] = []
        self.reduces: list[LLMRequest] = []
        self.others: list[LLMRequest] = []

    def __call__(self, request: LLMRequest) -> LLMResponse:
        kind = kind_of(request)
        if kind == "map":
            self.maps.append(request)
            match = _PART_TAG.search(request.user)
            assert match is not None
            index, start = int(match.group(1)), match.group(3)
            people = [
                {**p, "timestamps": [start if t == FROM else t for t in p["timestamps"]]}
                for p in self._people.get(index, [])
            ]
            payload = wire_payload(
                summary=self._map_summary or f"Part {index} summary.",
                key_claims=[{"text": f"Claim {index}", "timestamp": start}],
                people=people,
                topics=[],
                open_questions=[],
                research_leads=[],
            )
        elif kind == "reduce":
            self.reduces.append(request)
            payload = self._reduce or wire_payload(
                summary="Combined.",
                key_claims=[{"text": "Overall claim", "timestamp": "00:00:00"}],
                people=[],
                topics=[],
                open_questions=[],
                research_leads=[],
            )
        else:
            self.others.append(request)
            payload = wire_payload()
        return LLMResponse(text=json.dumps(payload), stop="end")


def map_reduce_responder(
    *,
    people_by_part: Mapping[int, list[dict[str, Any]]] | None = None,
    reduce_payload: dict[str, Any] | None = None,
    map_summary: str | None = None,
) -> MapReduceResponder:
    return MapReduceResponder(people_by_part, reduce_payload, map_summary)


RespondFn = Callable[[LLMRequest], LLMResponse]
