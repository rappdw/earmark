import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from earmark.analyze.schema import (
    ANALYSIS_OUTPUT_SCHEMA,
    MAX_ENTITY_TIMESTAMPS,
    AnalysisOutput,
    WireClaim,
    WireEntity,
    WireLead,
    build_analysis,
    parse_timestamp,
)
from earmark.models import prompt_hash
from tests.analysis_samples import wire_payload

NOW = datetime(2026, 5, 1, tzinfo=UTC)
KEY = "0123456789abcdef0123"


def build(duration_s: float = 600.4, prompt: str | None = None, **payload: Any) -> Any:
    return build_analysis(
        AnalysisOutput.model_validate(wire_payload(**payload)),
        episode_key=KEY,
        prompt=prompt,
        model="m",
        created_at=NOW,
        duration_s=duration_s,
        warnings=(),
    )


def test_wire_model_rejects_extra_and_missing() -> None:
    AnalysisOutput.model_validate(wire_payload())
    with pytest.raises(ValidationError):
        AnalysisOutput.model_validate(wire_payload(extra="x"))
    payload = wire_payload()
    del payload["summary"]
    with pytest.raises(ValidationError):
        AnalysisOutput.model_validate(payload)
    with pytest.raises(ValidationError):
        AnalysisOutput.model_validate(wire_payload(open_questions="not a list"))
    with pytest.raises(ValidationError):
        AnalysisOutput.model_validate(
            wire_payload(key_claims=[{"text": "x", "timestamp": None, "extra": 1}])
        )


def _walk(schema: dict[str, Any], model: Any) -> None:
    if schema.get("type") == "object":
        assert list(schema["properties"]) == list(model.model_fields)
        assert schema["required"] == list(schema["properties"])
        assert schema["additionalProperties"] is False
        for name, prop in schema["properties"].items():
            _walk_prop(prop, model.model_fields[name].annotation)


def _walk_prop(prop: dict[str, Any], annotation: Any) -> None:
    if prop.get("type") == "array" and prop["items"].get("type") == "object":
        inner = annotation.__args__[0]
        _walk(prop["items"], inner)


def test_schema_matches_wire_model() -> None:
    _walk(dict(ANALYSIS_OUTPUT_SCHEMA), AnalysisOutput)
    props = ANALYSIS_OUTPUT_SCHEMA["properties"]
    assert props["key_claims"]["items"]["required"] == list(WireClaim.model_fields)
    assert props["people"]["items"]["required"] == list(WireEntity.model_fields)
    assert props["research_leads"]["items"]["required"] == list(WireLead.model_fields)


def _keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k != "properties":
                found |= _keys(v)
            else:
                for sub in v.values():
                    found |= _keys(sub)
    elif isinstance(node, list):
        for item in node:
            found |= _keys(item)
    return found


def test_schema_uses_supported_keywords_only() -> None:
    forbidden = {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "pattern",
        "format",
        "$ref",
        "$defs",
        "default",
    }
    assert not (_keys(dict(ANALYSIS_OUTPUT_SCHEMA)) & forbidden)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:01:05", 65.0),
        ("[1:02:03]", 3723.0),
        ("12:34", 754.0),
        ("10:00:00.5", 36000.0),
        (" 100:00:00 ", 360000.0),
        ("1:2:3", None),
        ("00:61:00", None),
        ("00:00:61", None),
        ("abc", None),
        ("", None),
        ("-00:00:01", None),
        ("[00:01:05", None),
    ],
)
def test_parse_timestamp(value: str, expected: float | None) -> None:
    assert parse_timestamp(value) == expected


def test_timestamps_beyond_duration_dropped() -> None:
    a = build(
        key_claims=[
            {"text": "ok", "timestamp": "00:10:01"},
            {"text": "late", "timestamp": "00:10:02"},
        ],
        people=[{"name": "P", "context": "", "timestamps": ["00:10:01", "00:10:02", "01:00:00"]}],
        research_leads=[{"text": "l", "rationale": "r", "timestamp": "01:00:00"}],
    )
    assert a.key_claims[0].timestamp_s == 601.0
    assert a.key_claims[1].timestamp_s is None
    assert a.people[0].timestamps_s == (601.0,)
    assert a.research_leads[0].timestamp_s is None
    assert "dropped 4 timestamps beyond the episode's duration (00:10:00)" in a.warnings


def test_beyond_duration_warning_exact() -> None:
    a = build(
        key_claims=[],
        people=[{"name": "P", "context": "", "timestamps": ["00:10:02", "01:00:00"]}],
        research_leads=[],
    )
    assert a.warnings == ("dropped 2 timestamps beyond the episode's duration (00:10:00)",)


def test_singular_warning() -> None:
    a = build(
        key_claims=[{"text": "late", "timestamp": "09:00:00"}],
        people=[],
        research_leads=[],
    )
    assert a.warnings == ("dropped 1 timestamp beyond the episode's duration (00:10:00)",)


def test_unreadable_timestamps_dropped() -> None:
    a = build(key_claims=[{"text": "c", "timestamp": "soon"}], research_leads=[])
    assert a.key_claims[0].timestamp_s is None
    assert "dropped 1 unreadable timestamp" in a.warnings


def test_entity_timestamps_sorted_deduplicated_capped() -> None:
    stamps = ["00:05:00", "00:01:00", "00:05:00", "[00:02:00]"]
    stamps += [f"00:00:{s:02d}" for s in range(30)]
    a = build(people=[{"name": "P", "context": "", "timestamps": stamps}])
    ts = a.people[0].timestamps_s
    assert len(ts) == MAX_ENTITY_TIMESTAMPS
    assert list(ts) == sorted(set(ts))
    assert ts[0] == 0.0
    distinct = len(set(stamps)) - 0
    extra = distinct - MAX_ENTITY_TIMESTAMPS
    assert any(w.startswith(f"dropped {extra} extra entity timestamps") for w in a.warnings)


def test_blank_items_dropped() -> None:
    a = build(
        key_claims=[{"text": "  ", "timestamp": None}, {"text": "a\n b", "timestamp": None}],
        people=[{"name": " \n ", "context": "x", "timestamps": []}],
        open_questions=["", "Q?"],
        research_leads=[{"text": "", "rationale": "r", "timestamp": None}],
    )
    assert [c.text for c in a.key_claims] == ["a b"]
    assert a.people == ()
    assert a.open_questions == ("Q?",)
    assert a.research_leads == ()
    assert "dropped 4 empty items" in a.warnings


def test_prompt_response_forced_none_without_prompt() -> None:
    a = build(prompt_response="an answer")
    assert a.prompt_response is None
    assert not a.warnings


def test_prompt_response_kept_with_prompt() -> None:
    a = build(prompt="Q", prompt_response="  an answer \n")
    assert a.prompt_response == "an answer"
    assert not a.warnings


@pytest.mark.parametrize("response", [None, "", "  \n"])
def test_unanswered_prompt_warns(response: str | None) -> None:
    a = build(prompt="Q", prompt_response=response)
    assert a.prompt_response is None
    assert a.warnings == ("the model did not answer the prompt",)


def test_build_analysis_maps_fields() -> None:
    out = AnalysisOutput.model_validate(
        wire_payload(
            summary="  Para one.\n\nPara two.  ",
            key_claims=[{"text": " Claim\n here ", "timestamp": "00:00:05"}],
            papers=[{"name": " Paper  X ", "context": " ctx\nline", "timestamps": ["01:00"]}],
            companies=[{"name": "Co", "context": "", "timestamps": []}],
            tools=[{"name": "Tool", "context": "", "timestamps": []}],
            research_leads=[{"text": "Lead", "rationale": " why ", "timestamp": "00:00:07"}],
            prompt_response="Answer.",
        )
    )
    a = build_analysis(
        out,
        episode_key=KEY,
        prompt="Q",
        model="claude-x",
        created_at=NOW,
        duration_s=600,
        warnings=("from the client",),
    )
    assert a.prompt_hash == prompt_hash("Q", "claude-x")
    assert a.episode_key == KEY
    assert a.created_at == NOW
    assert a.summary == "Para one.\n\nPara two."
    assert a.key_claims[0].text == "Claim here"
    assert a.key_claims[0].timestamp_s == 5.0
    assert a.papers[0].name == "Paper X"
    assert a.papers[0].context == "ctx line"
    assert a.papers[0].timestamps_s == (60.0,)
    assert [e.name for e in a.companies] == ["Co"]
    assert [e.name for e in a.tools] == ["Tool"]
    assert [e.name for e in a.people] == ["Mara Ellison"]
    assert [e.name for e in a.topics] == ["Memory"]
    assert a.open_questions == ("Why?",)
    assert a.research_leads[0].rationale == "why"
    assert a.research_leads[0].timestamp_s == 7.0
    assert a.prompt_response == "Answer."
    assert a.warnings == ("from the client",)


def test_analyze_modules_import_light() -> None:
    code = (
        "import sys, earmark.analyze.analyzer, earmark.analyze.schema, earmark.analyze.prompts, "
        "earmark.analyze.render, earmark.analyze.llm.base, earmark.analyze.chunking, "
        "earmark.analyze.merge, earmark.analyze.calls, earmark.analyze.mapreduce, "
        "earmark.endpoint\n"
        "heavy = {'anthropic', 'httpx2', 'fastapi'}\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in heavy)\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
