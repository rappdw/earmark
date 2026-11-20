from earmark.analyze.merge import (
    PartNote,
    entity_key,
    finalize_entities,
    merge_entities,
    merged_part_entities,
    restrict_timestamps,
)
from earmark.analyze.schema import AnalysisOutput, WireClaim, WireEntity
from tests.analysis_samples import wire_payload


def ent(name: str, context: str = "", *stamps: str) -> WireEntity:
    return WireEntity(name=name, context=context, timestamps=list(stamps))


def output(**overrides: object) -> AnalysisOutput:
    return AnalysisOutput.model_validate(wire_payload(**overrides))


def test_entity_key() -> None:
    assert entity_key("Dr. Mara  Ellison") == "dr mara ellison"
    assert entity_key("ADA LOVELACE") == entity_key("Ada Lovelace,") == "ada lovelace"
    assert entity_key("\uff21\uff44\uff41\u3000Lovelace") == "ada lovelace"
    assert entity_key("C++ & Rust") == "c rust"
    assert entity_key("!!!") == ""


def test_merge_entities_unions_timestamps() -> None:
    merged = merge_entities(
        [
            [ent("Ada Lovelace", " ", "00:10:00", "00:1:5")],
            [ent("ada  lovelace", "Wrote the first program.", "[00:05:00]", "10:00")],
            [ent("ADA LOVELACE.", "Another context.", "00:00:03", "00:1:5")],
        ]
    )
    assert merged == [
        ent(
            "Ada Lovelace",
            "Wrote the first program.",
            "00:00:03",
            "00:05:00",
            "00:10:00",
            "00:1:5",
        )
    ]


def test_merge_keeps_first_seen_order_and_symbol_names() -> None:
    merged = merge_entities(
        [[ent("B"), ent("???", "", "00:00:01")], [ent("A"), ent("b", "", "00:00:02")]]
    )
    assert [e.name for e in merged] == ["B", "???", "A"]
    assert merged[0].timestamps == ["00:00:02"]
    assert merged[1] == ent("???", "", "00:00:01")


def test_kinds_not_merged_across() -> None:
    note = PartNote(
        1,
        2,
        0,
        10,
        output(
            people=[ent("Apple", "", "00:00:01").model_dump()],
            companies=[ent("Apple").model_dump()],
        ),
    )
    merged = merged_part_entities([note, note])
    assert [e.name for e in merged["people"]] == ["Apple"]
    assert [e.name for e in merged["companies"]] == ["Apple"]
    assert merged["people"][0].timestamps == ["00:00:01"]


def test_finalize_entities() -> None:
    premerged = {
        "people": [
            ent("Ada Lovelace", "Pre.", "00:01:00", "00:02:00"),
            ent("Dropped", "", "00:00:05"),
        ],
        "topics": [ent("memory", "", "00:03:00")],
    }
    final = output(
        people=[
            ent("Ada Lovelace", "Final.", "00:01:00").model_dump(),
            ent("ada lovelace", "", "00:09:00").model_dump(),
            ent("Invented", "", "00:00:09").model_dump(),
        ],
        topics=[ent("Memory!", "", "").model_dump() | {"timestamps": []}],
    )
    result = finalize_entities(final, premerged)
    assert [e.name for e in result.people] == ["Ada Lovelace", "Invented"]  # Dropped stays dropped
    assert result.people[0].context == "Final."
    assert result.people[0].timestamps == ["00:01:00", "00:02:00", "00:09:00"]
    assert result.people[1].timestamps == ["00:00:09"]
    assert result.topics[0].timestamps == ["00:03:00"]
    assert result.summary == final.summary


def test_restrict_timestamps_drops_outside() -> None:
    out = output(
        key_claims=[
            {"text": "in", "timestamp": "00:01:40"},
            {"text": "out", "timestamp": "00:05:00"},
            {"text": "garbled", "timestamp": "soon"},
        ],
        people=[ent("A", "", "00:01:38", "00:03:22", "00:03:20", "00:01:39", "nope").model_dump()],
        research_leads=[{"text": "r", "rationale": "", "timestamp": "00:00:01"}],
    )
    restricted, dropped = restrict_timestamps(out, 99.5, 200.2)
    assert dropped == 4  # claim 00:05:00, lead 00:00:01, entity 00:01:38 and 00:03:22
    assert [c.timestamp for c in restricted.key_claims] == ["00:01:40", None, "soon"]
    assert restricted.people[0].timestamps == ["00:03:20", "00:01:39", "nope"]
    assert restricted.research_leads[0].timestamp is None
    assert restricted.key_claims[1] == WireClaim(text="out", timestamp=None)
