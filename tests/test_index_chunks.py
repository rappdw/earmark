import dataclasses
from pathlib import Path

import pytest

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.index.chunks import (
    MAX_CHUNK_SPAN_S,
    MAX_ROW_CHARS,
    PROMPT_CHARS,
    EpisodeDocs,
    IndexRow,
    NotePaths,
    build_rows,
    load_episode_docs,
    rows_digest,
)
from earmark.models import Segment, StoreResult
from tests.factories import T0
from tests.index_helpers import (
    NOTES,
    make_analysis_of,
    make_docs,
    make_segments,
    with_analyses,
)


def always(_: str) -> bool:
    return True


def transcript_rows(rows: tuple[IndexRow, ...]) -> list[IndexRow]:
    return [r for r in rows if r.kind == "transcript"]


def collapsed(segments: tuple[Segment, ...]) -> list[str]:
    return [" ".join(s.text.split()) for s in segments if s.text.strip()]


def test_chunks_are_runs_of_whole_segments() -> None:
    segments = make_segments(40, words=12, step=7.0)
    docs = make_docs(segments=segments)
    rows = transcript_rows(build_rows(docs, NOTES, fits=always))
    assert len(rows) > 1
    texts = collapsed(segments)
    index = 0
    for row in rows:
        first = index
        joined = ""
        while joined != row.text:
            joined = f"{joined} {texts[index]}".strip()
            index += 1
            assert len(joined) <= len(row.text)
        assert row.start_s == segments[first].start
        assert row.end_s == max(s.end for s in segments[first:index])
    assert index == len(texts)


def test_chunks_cover_every_segment_once() -> None:
    state = 14

    def draw(low: int, high: int) -> int:  # a seeded LCG: deterministic, no stdlib random
        nonlocal state
        state = (state * 1103515245 + 12345) % 2**31
        return low + state % (high - low + 1)

    segments: list[Segment] = []
    clock = 0.0
    for i in range(500):
        words = " ".join(f"s{i}w{j}" for j in range(draw(0, 60)))
        dur = draw(5, 200) / 10
        segments.append(Segment(start=clock, end=clock + dur, text=f"  {words} \n"))
        clock += dur
    docs = make_docs(segments=segments)
    rows = transcript_rows(build_rows(docs, NOTES, fits=always))
    assert " ".join(r.text for r in rows) == " ".join(collapsed(tuple(segments)))


def test_chunk_limits() -> None:
    segments = make_segments(200, words=15, step=9.0)
    rows = transcript_rows(build_rows(make_docs(segments=segments), NOTES, fits=always))
    for row in rows:
        assert len(row.text) <= MAX_ROW_CHARS
        assert row.start_s is not None
        assert row.end_s is not None
        assert row.end_s >= row.start_s
        assert row.end_s - row.start_s <= MAX_CHUNK_SPAN_S


def test_long_segment_split_keeps_times() -> None:
    sentence = "This is a sentence with several words in it. "
    text = sentence * 110
    segments = (
        Segment(start=1.0, end=2.0, text="before"),
        Segment(start=10.0, end=40.0, text=text),
        Segment(start=40.0, end=41.0, text="after"),
    )
    rows = transcript_rows(build_rows(make_docs(segments=segments), NOTES, fits=always))
    assert len(rows) >= 5
    middle = [r for r in rows if r.text not in {"before", "after"}]
    assert len(middle) >= 3
    assert all((r.start_s, r.end_s) == (10.0, 40.0) for r in middle)
    assert all(len(r.text) <= MAX_ROW_CHARS for r in rows)
    assert rows[0].text == "before"
    assert rows[-1].text == "after"
    assert " ".join(r.text for r in middle) == " ".join(text.split())


def test_fits_drives_splitting() -> None:
    def small(t: str) -> bool:
        return len(t) <= 100

    segments = make_segments(30, words=6)
    docs = make_docs(
        segments=[*segments, Segment(start=500.0, end=510.0, text="word " * 200)],
        analyses=[make_analysis_of(make_docs().episode, summary="long summary " * 50)],
    )
    rows = build_rows(docs, NOTES, fits=small)
    assert all(len(r.text) <= 100 for r in rows)
    assert len([r for r in rows if r.kind == "summary"]) > 1


def test_analysis_rows_kinds() -> None:
    episode = make_docs().episode
    analysis = make_analysis_of(
        episode,
        prompt="What about X?",
        summary="  The   summary. ",
        claims=[("Claim one", 12.5), ("   ", 1.0), ("Claim two", None)],
        leads=[("Lead one", "because it matters", 30.0), ("Lead two", "  ", None)],
        questions=["Q1?", "", "Q2?"],
        response="The answer.",
    )
    docs = make_docs(segments=(), analyses=[analysis])
    rows = build_rows(docs, NOTES, fits=always)
    assert [(r.kind, r.text) for r in rows] == [
        ("summary", "The summary."),
        ("claim", "Claim one"),
        ("claim", "Claim two"),
        ("lead", "Lead one — because it matters"),
        ("lead", "Lead two"),
        ("question", "Q1?"),
        ("question", "Q2?"),
        ("answer", "The answer."),
    ]
    by = {(r.kind, r.ordinal): r for r in rows}
    assert by[("claim", 0)].start_s == 12.5
    assert by[("claim", 0)].end_s is None
    assert by[("claim", 1)].start_s is None
    assert by[("lead", 0)].start_s == 30.0
    assert by[("summary", 0)].start_s is None
    for r in rows:
        assert r.prompt_hash == analysis.prompt_hash
        assert r.prompt == "What about X?"
    assert not any(r.kind in {"entity", "person"} for r in rows)


def test_prompt_label_is_capped() -> None:
    episode = make_docs().episode
    analysis = make_analysis_of(episode, prompt="p" * 500)
    rows = build_rows(make_docs(segments=(), analyses=[analysis]), NOTES, fits=always)
    assert rows[0].prompt == "p" * PROMPT_CHARS + "…"


def test_no_answer_without_prompt() -> None:
    rows = build_rows(make_docs(), NOTES, fits=always)
    assert not [r for r in rows if r.kind == "answer"]
    assert all(r.prompt is None for r in rows if r.kind != "transcript")


def test_rows_deterministic_and_ids_unique() -> None:
    episode = make_docs().episode
    a1 = make_analysis_of(episode, prompt="one", response="r1")
    a2 = make_analysis_of(episode, prompt=None, response=None)
    docs = with_analyses(make_docs(segments=make_segments(50)), a1, a2)
    first = build_rows(docs, NOTES, fits=always)
    assert first == build_rows(docs, NOTES, fits=always)
    ids = [r.id for r in first]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith(f"{episode.key}:") for i in ids)
    transcript_ordinals = [r.ordinal for r in transcript_rows(first)]
    assert transcript_ordinals == list(range(len(transcript_ordinals)))
    assert f"{episode.key}:summary:{a2.prompt_hash}:0" in ids
    assert f"{episode.key}:transcript:-:0" in ids


def test_note_paths_on_rows() -> None:
    docs = make_docs()
    rows = build_rows(docs, NOTES, fits=always)
    assert {r.note_path for r in rows} == {NOTES.note_path}
    assert {r.transcript_note_path for r in rows} == {NOTES.transcript_note_path}
    assert {r.show_title for r in rows} == {docs.episode.show_title}
    assert {r.episode_url for r in rows} == {docs.episode.url}
    assert {r.published_at for r in rows} == {"2026-01-02T11:04:05+00:00"}
    bare = build_rows(docs, NotePaths(), fits=always)
    assert {r.note_path for r in bare} == {None}


def test_note_paths_from_store_result() -> None:
    stored = StoreResult(note_path="E/a.md", transcript_note_path="E/b.md")
    assert NotePaths.from_store_result(stored) == NotePaths("E/a.md", "E/b.md")


def test_digest_changes_with_inputs() -> None:
    docs = make_docs()
    rows = build_rows(docs, NOTES, fits=always)
    base = rows_digest(rows, model="m", dim=8)
    assert len(base) == 32
    assert base == rows_digest(build_rows(docs, NOTES, fits=always), model="m", dim=8)
    changed_text = (dataclasses.replace(rows[0], text=rows[0].text + "!"), *rows[1:])
    other_notes = build_rows(docs, NotePaths("Elsewhere/a.md", None), fits=always)
    variants = [
        rows_digest(changed_text, model="m", dim=8),
        rows_digest(other_notes, model="m", dim=8),
        rows_digest(rows, model="other", dim=8),
        rows_digest(rows, model="m", dim=9),
    ]
    assert len({base, *variants}) == 5


def test_empty_transcript_still_has_analysis_rows() -> None:
    rows = build_rows(make_docs(segments=()), NOTES, fits=always)
    assert not transcript_rows(rows)
    assert rows


def _artifacts_with(tmp_path: Path) -> tuple[ArtifactStore, EpisodeDocs]:
    docs = make_docs()
    store = ArtifactStore(tmp_path)
    store.write_episode(docs.episode)
    return store, docs


def test_load_episode_docs(tmp_path: Path) -> None:
    store, docs = _artifacts_with(tmp_path)
    key = docs.episode.key
    assert load_episode_docs(store, key) is None  # no transcript
    store.write_transcript(docs.transcript)
    assert load_episode_docs(store, key) is None  # no analysis
    late = make_analysis_of(docs.episode, prompt="later", created_at=T0.replace(year=2027))
    for a in (late, *docs.analyses):
        store.write_analysis(a)
    loaded = load_episode_docs(store, key)
    assert loaded is not None
    assert loaded.analyses == (docs.analyses[0], late)
    assert load_episode_docs(store, "b" * 20) is None


def test_load_episode_docs_corrupt(tmp_path: Path) -> None:
    store, docs = _artifacts_with(tmp_path)
    store.write_transcript(docs.transcript)
    store.write_analysis(docs.analyses[0])
    store.transcript_json_path(docs.episode.key).write_text("{not json")
    with pytest.raises(PermanentError):
        load_episode_docs(store, docs.episode.key)
