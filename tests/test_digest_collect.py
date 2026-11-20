import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from earmark.artifacts import ArtifactStore
from earmark.digest.collect import collect, digest_input_hash
from earmark.digest.window import week_window
from earmark.models import ResearchLead
from tests.digest_helpers import put_episode

WINDOW = week_window(2026, 47, UTC)
IN = WINDOW.start + timedelta(hours=5)


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "data")


def test_selects_by_first_share_in_window(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(a, "before", WINDOW.start - timedelta(microseconds=1), analyses=[["x"]])
    put_episode(a, "start", WINDOW.start, analyses=[["x"]])
    put_episode(a, "last", WINDOW.end - timedelta(microseconds=1), analyses=[["x"]])
    put_episode(a, "end", WINDOW.end, analyses=[["x"]])
    got = collect(a, WINDOW)
    assert [e.episode.guid for e in got.episodes] == ["start", "last"]


def test_reshare_does_not_move_episode(tmp_path: Path) -> None:
    a = _store(tmp_path)
    ep = put_episode(a, "g", IN, analyses=[["x"]])
    a.record_share(ep.key, IN + timedelta(days=30))
    assert [e.episode.key for e in collect(a, WINDOW).episodes] == [ep.key]
    assert collect(a, week_window(2026, 51, UTC)).episodes == ()


def test_no_share_record_left_out(tmp_path: Path) -> None:
    a = _store(tmp_path)
    ep = put_episode(a, "g", IN, analyses=[["x"]])
    a.share_path(ep.key).unlink()
    got = collect(a, WINDOW)
    assert got.episodes == ()
    assert got.warnings == ()


def test_leads_from_all_analyses_oldest_first(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(a, "g", IN, analyses=[["first", "second"], ["third"]])
    leads = collect(a, WINDOW).leads
    assert [(x.lead_id, x.text) for x in leads] == [
        ("L1", "first"),
        ("L2", "second"),
        ("L3", "third"),
    ]


def test_duplicate_leads_within_episode_dropped(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(
        a, "g", IN, analyses=[["Quantum Error-Correction", "other"], ["quantum error correction!"]]
    )
    leads = collect(a, WINDOW).leads
    assert [x.text for x in leads] == ["Quantum Error-Correction", "other"]


def test_same_lead_in_two_episodes_kept(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(a, "g1", IN, analyses=[["same"]])
    put_episode(a, "g2", IN + timedelta(hours=1), analyses=[["same"]])
    assert [x.text for x in collect(a, WINDOW).leads] == ["same", "same"]


def test_unreadable_analysis_skipped_with_warning(tmp_path: Path) -> None:
    a = _store(tmp_path)
    ep = put_episode(a, "g", IN, analyses=[["good"], ["bad"]])
    hashes = a.list_analysis_hashes(ep.key)

    def text_of(h: str) -> str:
        analysis = a.read_analysis(ep.key, h)
        assert analysis is not None
        return analysis.research_leads[0].text

    broken = next(h for h in hashes if text_of(h) == "bad")
    a.analysis_path(ep.key, broken).write_text("{not json")
    got = collect(a, WINDOW)
    assert [x.text for x in got.leads] == ["good"]
    assert got.episodes[0].analyses == 1
    assert any(
        re.fullmatch(rf"episode {ep.key}: analysis {broken} is unreadable.*", w)
        for w in got.warnings
    )


def test_unreadable_episode_left_out_with_warning(tmp_path: Path) -> None:
    a = _store(tmp_path)
    ep = put_episode(a, "g", IN, analyses=[["x"]])
    other = put_episode(a, "h", IN, analyses=[["y"]])
    a.episode_path(ep.key).write_text("{broken")
    got = collect(a, WINDOW)
    assert [e.episode.key for e in got.episodes] == [other.key]
    assert got.warnings == (
        f"episode {ep.key}: unreadable files under EARMARK_DATA_DIR; left out of the digest",
    )


def test_episode_without_analyses_listed(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(a, "g", IN)
    got = collect(a, WINDOW)
    assert got.episodes[0].analyses == 0
    assert got.leads == ()


def test_lead_ids_in_share_order(tmp_path: Path) -> None:
    a = _store(tmp_path)
    late = put_episode(a, "late", IN + timedelta(days=1), analyses=[["l1"]])
    early = put_episode(a, "early", IN, analyses=[[ResearchLead(text="e1", timestamp_s=5.0), "e2"]])
    leads = collect(a, WINDOW).leads
    assert [(x.lead_id, x.episode_key, x.text) for x in leads] == [
        ("L1", early.key, "e1"),
        ("L2", early.key, "e2"),
        ("L3", late.key, "l1"),
    ]
    assert leads[0].timestamp_s == 5.0


def test_input_hash_depends_on_leads_and_model_not_window(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(a, "g", IN, analyses=[["x"]])
    got = collect(a, WINDOW)
    h = digest_input_hash("m", got)
    assert re.fullmatch(r"[0-9a-f]{16}", h)
    assert digest_input_hash("m", got) == h
    assert digest_input_hash("other", got) != h
    wider = collect(a, week_window(2026, 47, UTC))
    assert digest_input_hash("m", wider) == h
    put_episode(a, "g2", IN + timedelta(hours=1), analyses=[["y"]])
    assert digest_input_hash("m", collect(a, WINDOW)) != h


def test_naive_free_share_times_are_aware(tmp_path: Path) -> None:
    a = _store(tmp_path)
    put_episode(a, "g", datetime(2026, 11, 17, 12, tzinfo=UTC), analyses=[["x"]])
    assert collect(a, WINDOW).episodes[0].shared_at.tzinfo is not None
