"""Sample episode, transcript, and wire payloads for the analysis tests."""

import json
from typing import Any

from earmark.models import Episode, Segment, Transcript
from tests.factories import make_episode

DURATION_S = 600.4


def sample_episode(**kwargs: Any) -> Episode:
    return make_episode(**kwargs)


def sample_transcript(
    *, segments: tuple[Segment, ...] | None = None, guid: str = "urn:example:guid-1"
) -> Transcript:
    key = make_episode(guid=guid).key
    if segments is None:
        segments = (
            Segment(start=0.0, end=65.0, text="Hello and welcome."),
            Segment(start=65.0, end=300.0, text="Dr. Mara Ellison discusses the study."),
            Segment(start=300.0, end=DURATION_S, text="Thanks for listening."),
        )
    return Transcript(
        episode_key=key, language="en", duration_s=DURATION_S, model="fake-asr", segments=segments
    )


def wire_payload(**overrides: Any) -> dict[str, Any]:
    entity = {"name": "Mara Ellison", "context": "Led the study.", "timestamps": ["00:01:05"]}
    payload: dict[str, Any] = {
        "summary": "A summary.",
        "key_claims": [{"text": "A claim", "timestamp": "00:01:05"}],
        "people": [entity],
        "papers": [],
        "companies": [],
        "tools": [],
        "topics": [{"name": "Memory", "context": "", "timestamps": []}],
        "open_questions": ["Why?"],
        "research_leads": [{"text": "Read it", "rationale": "Cited.", "timestamp": None}],
        "prompt_response": None,
    }
    payload.update(overrides)
    return payload


def valid_wire_json(**overrides: Any) -> str:
    return json.dumps(wire_payload(**overrides))
