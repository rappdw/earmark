import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from earmark.analyze.analyzer import LlmAnalyzer
from earmark.analyze.chunking import estimate_request_tokens
from earmark.analyze.llm.base import LLMRequest, LLMResponse
from earmark.analyze.prompts import (
    MAP_SYSTEM_PROMPT,
    REDUCE_SYSTEM_PROMPT,
    REPAIR_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_message,
)
from earmark.analyze.schema import AnalysisOutput
from earmark.artifacts import ArtifactStore
from earmark.errors import NeedsChunking, PermanentError, TransientError
from earmark.models import Analysis, Transcript, prompt_hash
from tests.analysis_samples import sample_episode, sample_transcript, wire_payload
from tests.llm_fakes import ScriptedLLMClient
from tests.long_transcripts import (
    FROM,
    MapReduceResponder,
    kind_of,
    long_transcript,
    map_reduce_responder,
)

NOW = datetime(2026, 5, 1, tzinfo=UTC)
CONTEXT = 20_000
OUTPUT = 4_000
PART_TAG = re.compile(r'<transcript part="(\d+)" of="(\d+)" from="([\d:]+)" to="([\d:]+)">')
REDUCE_PART = re.compile(r'<part index="(\d+)" of="(\d+)" from="([\d:]+)" to="([\d:]+)">')
MARKER = re.compile(r"\[\d\d:\d\d:\d\d\] Segment")
ANA = "the model's answer does not match the analysis schema"


def small_client(
    responder: MapReduceResponder | Any,
    *,
    script: list[LLMResponse | Exception] | None = None,
    **overrides: Any,
) -> ScriptedLLMClient:
    values: dict[str, Any] = {
        "context_tokens": CONTEXT,
        "output_tokens": OUTPUT,
        "max_output_tokens": 8_000,
        "token_count": estimate_request_tokens,
        "respond": responder,
    }
    values.update(overrides)
    return ScriptedLLMClient(script or (), **values)


def make(
    tmp_path: Path, client: ScriptedLLMClient, *, repair: bool = False
) -> tuple[LlmAnalyzer, ArtifactStore]:
    artifacts = ArtifactStore(tmp_path / "data")
    return LlmAnalyzer(artifacts, client, clock=lambda: NOW, repair_invalid_json=repair), artifacts


def analyze(
    analyzer: LlmAnalyzer, transcript: Transcript | None = None, prompt: str | None = None
) -> Analysis:
    return analyzer.analyze(sample_episode(), transcript or long_transcript(hours=3), prompt)


def part_tag(request: LLMRequest) -> tuple[int, int, str, str]:
    m = re.search(
        r'<transcript part="(\d+)" of="(\d+)" from="([\d:]+)" to="([\d:]+)">', request.user
    )
    assert m is not None
    return int(m[1]), int(m[2]), m[3], m[4]


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def valid(**overrides: Any) -> LLMResponse:
    return LLMResponse(text=json.dumps(wire_payload(**overrides)), stop="end")


def invalid() -> LLMResponse:
    return LLMResponse(text="{not json SECRET-MODEL-TEXT", stop="end")


# -- A2: map-reduce ---------------------------------------------------------------------------


def test_three_hour_transcript_map_reduce(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    client = small_client(responder)
    analyzer, artifacts = make(tmp_path, client)
    analysis = analyze(analyzer)

    assert len(responder.maps) >= 3
    assert len(responder.reduces) == 1
    assert responder.others == []
    for request in client.complete_requests:
        assert estimate_request_tokens(request) + request.max_tokens <= CONTEXT
    assert client.complete_requests[-1].system is REDUCE_SYSTEM_PROMPT
    path = artifacts.analysis_path(sample_episode().key, analysis.prompt_hash)
    assert Analysis.model_validate_json(path.read_text()) == analysis
    assert analysis.warnings[0].startswith(
        f"the transcript was analyzed in {len(responder.maps)} parts"
    )
    assert analysis.summary == "Combined."
    assert not artifacts.analysis_parts_dir(sample_episode().key, analysis.prompt_hash).exists()


def test_map_reduce_covers_every_segment(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    analyzer, _ = make(tmp_path, small_client(responder))
    transcript = long_transcript(hours=3)
    analyze(analyzer, transcript)
    for line in transcript.to_text().splitlines():
        hits = sum(request.user.count(line + "\n") for request in responder.maps)
        assert hits == 1, line


def test_map_requests_carry_absolute_markers(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    analyzer, _ = make(tmp_path, small_client(responder))
    transcript = long_transcript(hours=3)
    analyze(analyzer, transcript)
    n = len(responder.maps)
    for position, request in enumerate(responder.maps, start=1):
        index, count, start, end = part_tag(request)
        assert (index, count) == (position, n)
        lines = [
            line
            for line in request.user.splitlines()
            if re.match(r"\[\d\d:\d\d:\d\d\] Segment", line)
        ]
        first = int(lines[0].split()[2])
        last = int(lines[-1].split()[2])
        assert lines[0].startswith(f"[{hms(transcript.segments[first].start)}]")
        assert start == hms(transcript.segments[first].start)
        assert end == hms(transcript.segments[last].end)
        assert "Description" not in request.user.split("<transcript")[0]
        assert request.system is MAP_SYSTEM_PROMPT
    assert part_tag(responder.maps[1])[2] != "00:00:00"


def test_reduce_request_layout(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    analyzer, _ = make(tmp_path, small_client(responder))
    analyze(analyzer)
    (reduce,) = responder.reduces
    parts = re.findall(
        r'<part index="(\d+)" of="(\d+)" from="([\d:]+)" to="([\d:]+)">', reduce.user
    )
    expected = [part_tag(r) for r in responder.maps]
    assert [(int(i), int(n), f, t) for i, n, f, t in parts] == expected
    assert "<entities>" in reduce.user
    assert sample_episode().description in reduce.user
    assert all(sample_episode().description not in m.user for m in responder.maps)
    assert "<user_prompt>" not in reduce.user


def test_map_reduce_prompt_response(tmp_path: Path) -> None:
    responder = map_reduce_responder(
        reduce_payload=wire_payload(
            summary="S", people=[], topics=[], prompt_response="The answer.", key_claims=[]
        )
    )
    analyzer, _ = make(tmp_path, small_client(responder))
    analysis = analyze(analyzer, prompt="What about X?")
    for request in [*responder.maps, *responder.reduces]:
        assert "<user_prompt>\nWhat about X?\n</user_prompt>" in request.user
    assert analysis.prompt_response == "The answer."


def test_fitting_transcript_is_one_request(tmp_path: Path) -> None:
    client = ScriptedLLMClient()
    analyzer, artifacts = make(tmp_path, client)
    episode, transcript = sample_episode(), sample_transcript()
    analysis = analyzer.analyze(episode, transcript, None)
    assert client.count_calls == 1
    assert client.complete_calls == 1
    (request,) = client.complete_requests
    assert request.system is SYSTEM_PROMPT
    assert request.user == build_user_message(episode, transcript, None)
    assert not artifacts.analysis_parts_dir(episode.key, analysis.prompt_hash).exists()
    assert analysis.warnings == ()


# -- A3: entities -------------------------------------------------------------------------------


def test_entities_merged_across_parts(tmp_path: Path) -> None:
    def ada(name: str) -> list[dict[str, Any]]:
        return [{"name": name, "context": "", "timestamps": [FROM]}]

    responder = map_reduce_responder(
        people_by_part={1: ada("Ada Lovelace"), 2: ada("ada  lovelace"), 3: ada("ADA LOVELACE.")},
        reduce_payload=wire_payload(
            people=[
                {"name": "Ada Lovelace", "context": "Pioneer.", "timestamps": []},
                {"name": "Ada Lovelace", "context": "", "timestamps": ["00:00:00"]},
            ],
            topics=[],
        ),
    )
    analyzer, _ = make(tmp_path, small_client(responder))
    analysis = analyze(analyzer)
    assert len(responder.maps) >= 3
    starts = [part_tag(r)[2] for r in responder.maps[:3]]
    assert len(set(starts)) == 3

    (reduce,) = responder.reduces
    block = reduce.user.split("<entities>\n")[1].split("\n</entities>")[0]
    entities = json.loads(block.replace("&lt;", "<").replace("&gt;", ">"))
    (merged,) = entities["people"]
    assert merged["name"] == "Ada Lovelace"
    assert merged["timestamps"] == sorted(starts)

    (person,) = analysis.people
    assert person.name == "Ada Lovelace"
    assert [round(t) for t in person.timestamps_s] == [
        int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in (x.split(":") for x in sorted(starts))
    ]


def test_timestamps_outside_part_dropped(tmp_path: Path) -> None:
    far = [{"name": "Ada", "context": "", "timestamps": ["02:00:00"]}]
    analyzer, _ = make(tmp_path, small_client(map_reduce_responder(people_by_part={1: far})))
    analysis = analyze(analyzer)
    assert "dropped 1 timestamp outside their transcript part" in analysis.warnings


# -- A4: repair ---------------------------------------------------------------------------------


def test_repair_fixes_invalid_json(tmp_path: Path) -> None:
    client = ScriptedLLMClient([invalid(), valid()])
    analyzer, artifacts = make(tmp_path, client, repair=True)
    analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_calls == 2
    repair = client.complete_requests[1]
    assert repair.system is REPAIR_SYSTEM_PROMPT
    assert "<invalid_answer>\n{not json SECRET-MODEL-TEXT\n</invalid_answer>" in repair.user
    assert "(root): json_invalid" in repair.user
    assert "1 model answer did not match the analysis schema and was repaired" in analysis.warnings
    assert artifacts.read_analysis(sample_episode().key, analysis.prompt_hash) == analysis
    assert analyzer.repairs_invalid_json is True


def test_repair_fixes_schema_mismatch(tmp_path: Path) -> None:
    payload = wire_payload()
    del payload["topics"]
    client = ScriptedLLMClient([LLMResponse(json.dumps(payload), "end"), valid()])
    analyzer, _ = make(tmp_path, client, repair=True)
    analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert "topics: missing" in client.complete_requests[1].user


def test_second_invalid_answer_is_permanent(tmp_path: Path) -> None:
    client = ScriptedLLMClient([invalid(), invalid()])
    analyzer, artifacts = make(tmp_path, client, repair=True)
    with pytest.raises(PermanentError) as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_calls == 2
    assert "after one repair attempt" in excinfo.value.message
    assert ANA in excinfo.value.message
    assert "SECRET-MODEL-TEXT" not in excinfo.value.message
    assert not artifacts.analyses_dir(sample_episode().key).exists()


def test_repair_in_a_map_part(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    responder = map_reduce_responder()
    seen: list[str] = []

    def respond(request: LLMRequest) -> LLMResponse:
        if kind_of(request) == "map" and part_tag(request)[0] == 2 and "bad" not in seen:
            seen.append("bad")
            responder(request)
            return invalid()
        return responder(request)

    client = small_client(respond)
    analyzer, _ = make(tmp_path, client, repair=True)
    with caplog.at_level(logging.WARNING, logger="earmark.analyze"):
        analysis = analyze(analyzer)
    repairs = [r for r in client.complete_requests if r.system is REPAIR_SYSTEM_PROMPT]
    assert len(repairs) == 1
    assert any("part 2 of" in rec.getMessage() for rec in caplog.records)
    assert "1 model answer did not match the analysis schema and was repaired" in analysis.warnings
    assert all("SECRET-MODEL-TEXT" not in rec.getMessage() for rec in caplog.records)
    assert len(responder.reduces) == 1


def test_repair_off_for_anthropic(tmp_path: Path) -> None:
    client = ScriptedLLMClient([invalid()])
    analyzer, _ = make(tmp_path, client)
    with pytest.raises(PermanentError) as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_calls == 1
    assert excinfo.value.message == f"{ANA} (1 error, first at (root))"


# -- B3: context exceeded ----------------------------------------------------------------------


def test_context_exceeded_one_shot_switches_to_map_reduce(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    exceeded = LLMResponse("", "context_exceeded", raw_stop="HTTP 400")
    client = ScriptedLLMClient([exceeded], respond=responder)
    analyzer, artifacts = make(tmp_path, client)
    analysis = analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert client.complete_requests[0].system is SYSTEM_PROMPT
    assert len(responder.maps) >= 1
    assert len(responder.reduces) == 1
    assert artifacts.read_analysis(sample_episode().key, analysis.prompt_hash) == analysis


def test_context_exceeded_in_a_part_is_permanent(tmp_path: Path) -> None:
    exceeded = LLMResponse("", "context_exceeded", raw_stop="HTTP 400")
    client = ScriptedLLMClient([exceeded, exceeded])
    analyzer, _ = make(tmp_path, client)
    with pytest.raises(PermanentError) as excinfo:
        analyzer.analyze(sample_episode(), sample_transcript(), None)
    assert not isinstance(excinfo.value, NeedsChunking)
    assert "part 1 of 1" in excinfo.value.message
    assert "EARMARK_LLM_CONTEXT_TOKENS" in excinfo.value.message
    assert "not truncated" in excinfo.value.message


# -- B6: planning edge cases ---------------------------------------------------------------


def test_context_too_small(tmp_path: Path) -> None:
    client = small_client(map_reduce_responder(), context_tokens=6_000, output_tokens=4_000)
    analyzer, _ = make(tmp_path, client)
    with pytest.raises(PermanentError) as excinfo:
        analyze(analyzer)
    assert "EARMARK_LLM_CONTEXT_TOKENS (6000)" in excinfo.value.message
    assert "not truncated" in excinfo.value.message
    assert client.complete_calls == 0


def test_verify_splits_parts_that_do_not_fit(tmp_path: Path) -> None:
    responder = map_reduce_responder()

    def inflated(request: LLMRequest) -> int:
        estimate = estimate_request_tokens(request)
        return estimate * 2 if kind_of(request) == "map" and estimate > 9_000 else estimate

    plain = map_reduce_responder()
    plain_analyzer, _ = make(tmp_path / "plain", small_client(plain))
    analyze(plain_analyzer)

    analyzer, _ = make(tmp_path / "split", small_client(responder, token_count=inflated))
    transcript = long_transcript(hours=3)
    analyze(analyzer, transcript)
    assert len(responder.maps) > len(plain.maps)
    for line in transcript.to_text().splitlines():
        assert sum(r.user.count(line + "\n") for r in responder.maps) == 1


def test_output_cap_splits_part(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    calls: list[LLMRequest] = []

    def respond(request: LLMRequest) -> LLMResponse:
        calls.append(request)
        answer = responder(request)
        if kind_of(request) == "map" and len([c for c in calls if kind_of(c) == "map"]) == 1:
            return LLMResponse("", "max_tokens", raw_stop="length")
        return answer

    client = small_client(respond, max_output_tokens=OUTPUT)
    analyzer, _ = make(tmp_path, client)
    plain = map_reduce_responder()
    base_analyzer, _ = make(tmp_path / "base", small_client(plain))
    analyze(base_analyzer)
    transcript = long_transcript(hours=3)
    analysis = analyze(analyzer, transcript)
    assert len(responder.maps) == len(plain.maps) + 2  # the failed call, then two halves
    first = responder.maps[0].user
    halves = responder.maps[1].user + responder.maps[2].user
    assert all(line in halves for line in first.splitlines() if MARKER.match(line))
    assert analysis.warnings[0].startswith("the transcript was analyzed in ")


def test_output_cap_in_a_one_piece_part_is_permanent(tmp_path: Path) -> None:
    capped = LLMResponse("", "max_tokens", raw_stop="length")
    client = ScriptedLLMClient(
        [LLMResponse("", "context_exceeded"), capped],
        max_output_tokens=64_000,
    )
    transcript = sample_transcript().model_copy(
        update={"segments": sample_transcript().segments[:1]}
    )
    analyzer, _ = make(tmp_path, client)
    with pytest.raises(PermanentError) as excinfo:
        analyzer.analyze(sample_episode(), transcript, None)
    assert excinfo.value.message == "part 1 of 1 did not fit in 64000 output tokens"


def test_tree_reduce(tmp_path: Path) -> None:
    def ada(*stamps: str) -> list[dict[str, Any]]:
        return [{"name": "Ada", "context": "", "timestamps": list(stamps)}]

    responder = map_reduce_responder(
        people_by_part={1: ada(FROM), 3: ada(FROM)},
        reduce_payload=wire_payload(
            people=[{"name": "ada", "context": "", "timestamps": []}], topics=[]
        ),
        map_summary="x" * 15_000,
    )
    analyzer, _ = make(tmp_path, small_client(responder))
    analysis = analyze(analyzer)
    assert len(responder.maps) == 4
    assert len(responder.reduces) == 3  # two intermediate (parts 1+2, 3+4), one final
    assert [r.user.count("<part ") for r in responder.reduces] == [2, 2, 2]
    assert 'index="1" of="2"' in responder.reduces[-1].user
    starts = [part_tag(r)[2] for r in responder.maps]
    (person,) = analysis.people
    assert person.name == "ada"
    # the premerged timestamps come from the original parts, not from the intermediate level
    assert [hms(t) for t in person.timestamps_s] == [starts[0], starts[2]]


def test_tree_reduce_impossible(tmp_path: Path) -> None:
    responder = map_reduce_responder(map_summary="x" * 60_000)
    analyzer, artifacts = make(tmp_path, small_client(responder))
    with pytest.raises(PermanentError) as excinfo:
        analyze(analyzer)
    assert "too long to combine within EARMARK_LLM_CONTEXT_TOKENS" in excinfo.value.message
    assert responder.reduces == []
    assert not list(artifacts.analyses_dir(sample_episode().key).glob("*.json"))


# -- B7: part cache ----------------------------------------------------------------------


def failing_third_map(responder: MapReduceResponder, state: dict[str, bool]) -> Any:
    def respond(request: LLMRequest) -> LLMResponse:
        if kind_of(request) == "map" and part_tag(request)[0] == 3 and not state["done"]:
            state["done"] = True
            raise TransientError("server hiccup")
        return responder(request)

    return respond


def test_transient_failure_resumes_parts(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    state = {"done": False}
    client = small_client(failing_third_map(responder, state))
    analyzer, artifacts = make(tmp_path, client)
    with pytest.raises(TransientError):
        analyze(analyzer)
    digest = prompt_hash(None, client.model)
    parts_dir = artifacts.analysis_parts_dir(sample_episode().key, digest)
    assert len(list(parts_dir.glob("*.json"))) == 2
    before = len(client.complete_requests)

    analysis = analyze(analyzer)
    second = client.complete_requests[before:]
    second_maps = [r for r in second if kind_of(r) == "map"]
    assert part_tag(second_maps[0])[0] == 3  # parts 1 and 2 were not redone
    assert len(second_maps) == len(responder.maps) - 2  # the responder never saw the raised call
    assert not parts_dir.exists()
    assert artifacts.read_analysis(sample_episode().key, digest) == analysis


def test_corrupt_part_recomputed(tmp_path: Path) -> None:
    responder = map_reduce_responder()
    state = {"done": False}
    client = small_client(failing_third_map(responder, state))
    analyzer, artifacts = make(tmp_path, client)
    with pytest.raises(TransientError):
        analyze(analyzer)
    digest = prompt_hash(None, client.model)
    parts_dir = artifacts.analysis_parts_dir(sample_episode().key, digest)
    first = sorted(parts_dir.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)[0]
    first.write_bytes(b"garbage")
    before = len(client.complete_requests)

    analyze(analyzer)
    second_maps = [r for r in client.complete_requests[before:] if kind_of(r) == "map"]
    indexes = [part_tag(r)[0] for r in second_maps]
    assert indexes[0] == 1  # the corrupt part is recomputed
    assert 2 not in indexes  # the intact one is not
    assert not parts_dir.exists()


def test_valid_part_bytes_roundtrip() -> None:
    output = AnalysisOutput.model_validate(wire_payload())
    assert AnalysisOutput.model_validate_json(output.model_dump_json()) == output
