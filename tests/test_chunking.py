import math

import pytest

from earmark.analyze.chunking import (
    Chunk,
    Piece,
    estimate_request_tokens,
    estimate_tokens,
    plan_chunks,
    split_chunk,
)
from earmark.analyze.llm.base import LLMRequest
from earmark.models import Segment
from tests.analysis_samples import sample_transcript
from tests.long_transcripts import long_transcript

BUDGETS = (2_000, 5_000, 9_000)


@pytest.mark.parametrize("budget", BUDGETS)
def test_pieces_partition_segments(budget: int) -> None:
    transcript = long_transcript(hours=3)
    plan = plan_chunks(transcript.segments, budget_tokens=budget)
    assert len(plan.chunks) > 1
    seen = [(p.segment_index, p.start, p.split) for c in plan.chunks for p in c.pieces]
    assert seen == [(i, seg.start, False) for i, seg in enumerate(transcript.segments)]
    assert plan.split_segments == 0
    for chunk in plan.chunks:  # a chunk starts and ends on whole segments
        first, last = chunk.pieces[0], chunk.pieces[-1]
        assert chunk.start_s == transcript.segments[first.segment_index].start
        assert chunk.end_s == transcript.segments[last.segment_index].end


@pytest.mark.parametrize("budget", BUDGETS)
def test_chunk_texts_concatenate_to_transcript_text(budget: int) -> None:
    transcript = long_transcript(hours=3)
    plan = plan_chunks(transcript.segments, budget_tokens=budget)
    assert "".join(c.to_text() for c in plan.chunks) == transcript.to_text()


def test_absolute_timestamps() -> None:
    transcript = long_transcript(hours=3)
    plan = plan_chunks(transcript.segments, budget_tokens=2_000)
    chunk = next(c for c in plan.chunks if any(p.segment_index == 1440 for p in c.pieces))
    piece = next(p for p in chunk.pieces if p.segment_index == 1440)
    assert piece.line() == f"[02:00:00] Segment 1440 talks about topic {1440 % 37}.\n"
    assert chunk.start_s <= 7200.0 <= chunk.end_s
    assert "[02:00:00] Segment 1440" in chunk.to_text()


def test_first_line_of_late_chunk_is_absolute() -> None:
    transcript = long_transcript(hours=3)
    plan = plan_chunks(transcript.segments, budget_tokens=2_000)
    last = plan.chunks[-1]
    assert last.start_s > 3600
    assert last.to_text().startswith(f"[{int(last.start_s) // 3600:02d}:")


@pytest.mark.parametrize("budget", BUDGETS)
def test_chunks_within_budget(budget: int) -> None:
    plan = plan_chunks(long_transcript(hours=3).segments, budget_tokens=budget)
    assert all(c.tokens() <= budget for c in plan.chunks)


@pytest.mark.parametrize("budget", BUDGETS)
def test_chunks_balanced(budget: int) -> None:
    plan = plan_chunks(long_transcript(hours=3).segments, budget_tokens=budget)
    total = sum(c.tokens() for c in plan.chunks)
    assert len(plan.chunks) <= math.ceil(total / budget) + 1
    target = math.ceil(total / math.ceil(total / budget))
    largest = max(estimate_tokens(p.line()) for c in plan.chunks for p in c.pieces)
    for chunk in plan.chunks[:-1]:
        assert chunk.tokens() >= target - largest


def test_blank_segments_covered() -> None:
    segments = (
        Segment(start=0, end=1, text="hello"),
        Segment(start=1, end=2, text="   "),
        Segment(start=2, end=3, text=""),
        Segment(start=3, end=4, text="bye"),
    )
    plan = plan_chunks(segments, budget_tokens=10)
    assert len(plan.chunks) >= 2
    pieces = [p for c in plan.chunks for p in c.pieces]
    assert [p.segment_index for p in pieces] == [0, 1, 2, 3]
    assert pieces[1].line() == "[00:00:01]\n"
    assert "".join(c.to_text() for c in plan.chunks) == (
        "[00:00:00] hello\n[00:00:01]\n[00:00:02]\n[00:00:03] bye\n"
    )


def test_plan_is_deterministic() -> None:
    segments = long_transcript(hours=1).segments
    assert plan_chunks(segments, budget_tokens=3_000) == plan_chunks(segments, budget_tokens=3_000)


def test_one_chunk_when_it_fits() -> None:
    transcript = sample_transcript()
    plan = plan_chunks(transcript.segments, budget_tokens=10_000)
    (chunk,) = plan.chunks
    assert chunk.to_text() == transcript.to_text()
    assert (chunk.start_s, chunk.end_s) == (0.0, 600.4)


def test_empty_transcript_has_no_chunks() -> None:
    assert plan_chunks((), budget_tokens=100).chunks == ()


def test_oversized_segment_split() -> None:
    sentences = [
        f"Sentence number {i} says something rather long about nothing." for i in range(500)
    ]
    text = " ".join(sentences)
    assert len(text) > 25_000
    segment = Segment(start=3725.0, end=3900.0, text=text)
    budget = 2_000
    plan = plan_chunks((segment,), budget_tokens=budget)
    pieces = [p for c in plan.chunks for p in c.pieces]
    assert len(pieces) > 1
    assert all(p.split and p.start == 3725.0 and p.segment_index == 0 for p in pieces)
    assert " ".join(p.text for p in pieces) == text
    assert all(estimate_tokens(p.line()) <= budget for p in pieces)
    assert all(c.tokens() <= budget for c in plan.chunks)
    assert plan.split_segments == 1
    assert all(c.end_s == 3900.0 for c in plan.chunks)
    assert all(
        line.startswith("[01:02:05] ") for c in plan.chunks for line in c.to_text().splitlines()
    )


def test_giant_word_split_by_characters() -> None:
    word = "x" * 20_000
    segment = Segment(start=0.0, end=10.0, text=f"Intro. {word} outro.")
    budget = 1_500
    plan = plan_chunks((segment,), budget_tokens=budget)
    pieces = [p for c in plan.chunks for p in c.pieces]
    assert all(p.split for p in pieces)
    assert all(estimate_tokens(p.line()) <= budget for p in pieces)
    joined = " ".join(p.text for p in pieces)
    assert joined.replace(" ", "") == f"Intro.{word}outro."
    assert word.count("x") == sum(p.text.count("x") for p in pieces)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("[00:00:05] hi\n") == 5 + 4
    assert estimate_tokens("日本") == 2


def test_estimate_request_tokens_counts_schema() -> None:
    small = LLMRequest("sys", "user", {"type": "object"}, "n", 10)
    big = LLMRequest("sys", "user", {"type": "object", "x": "y" * 300}, "n", 10)
    assert estimate_request_tokens(small) == 1 + 2 + estimate_tokens('{"type":"object"}') + 64
    assert estimate_request_tokens(big) > estimate_request_tokens(small) + 90


def test_split_chunk() -> None:
    segments = long_transcript(hours=0.1).segments
    plan = plan_chunks(segments, budget_tokens=10_000)
    (chunk,) = plan.chunks
    first, second = split_chunk(chunk, segments) or pytest.fail("expected halves")
    mid = len(chunk.pieces) // 2
    assert first.pieces == chunk.pieces[:mid]
    assert second.pieces == chunk.pieces[mid:]
    assert first.start_s == chunk.start_s
    assert second.end_s == chunk.end_s
    assert first.end_s == segments[first.pieces[-1].segment_index].end
    assert second.start_s == second.pieces[0].start
    single = Chunk((Piece(0, 0.0, "x"),), 0.0, 1.0)
    assert split_chunk(single, segments) is None
