import logging
from datetime import timedelta
from pathlib import Path

import pytest

from earmark.artifacts import ArtifactStore
from earmark.db import open_database
from earmark.episodes import (
    EPISODE_NOT_FOUND,
    EPISODES_LIMIT_MAX,
    NO_TRANSCRIPT,
    EpisodeNotFoundError,
    TranscriptMissingError,
    enqueue_reanalyze,
    list_episodes,
    require_transcript,
)
from earmark.errors import TransientError
from earmark.jobs import JobStore
from earmark.models import ReanalyzeRequest
from earmark.vault.notes import VaultStore
from tests.episodes_helpers import corrupt, seed_episode
from tests.factories import T0, make_episode


def test_list_empty_creates_nothing(tmp_path: Path) -> None:
    page = list_episodes(ArtifactStore(tmp_path / "data"))
    assert (page.episodes, page.total, page.offset, page.limit) == ((), 0, 0, 50)
    assert page.next_offset is None
    assert not (tmp_path / "data").exists()


def test_list_order_and_fields(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    old = seed_episode(s, 1, shared_at=T0, prompts=(None, "q"))
    new = seed_episode(s, 2, shared_at=T0 + timedelta(days=1), transcript=False)
    none = seed_episode(s, 3, shared_at=None)
    page = list_episodes(s)
    assert [e.key for e in page.episodes] == [new.key, old.key, none.key]
    assert page.total == 3
    first = page.episodes[1]
    assert (first.show, first.title, first.url) == (old.show_title, old.title, old.url)
    assert first.published_at == old.published_at
    assert first.shared_at == T0
    assert first.has_transcript
    assert [a.prompt for a in first.analyses] == ["q", None]  # newest first
    assert not page.episodes[0].has_transcript
    assert page.episodes[2].shared_at is None


def test_list_pages(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    for n in range(5):
        seed_episode(s, n, shared_at=T0 + timedelta(hours=n))
    seen: list[str] = []
    offset = 0
    while True:
        page = list_episodes(s, limit=2, offset=offset)
        seen.extend(e.key for e in page.episodes)
        assert page.total == 5
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert len(seen) == len(set(seen)) == 5
    past = list_episodes(s, limit=2, offset=10)
    assert (past.episodes, past.total, past.next_offset) == ((), 5, None)


def test_list_bounds_value_error(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    for kwargs in ({"limit": 0}, {"limit": EPISODES_LIMIT_MAX + 1}, {"offset": -1}):
        with pytest.raises(ValueError, match="must be between"):
            list_episodes(s, **kwargs)
    with pytest.raises(ValueError, match="offset"):
        list_episodes(s, offset=1_000_001)


def test_list_skips_unreadable(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = ArtifactStore(tmp_path)
    good = seed_episode(s, 1, prompts=(None, "q"))
    bad = seed_episode(s, 2)
    corrupt(s.episode_path(bad.key))
    s.write_analysis_part(good.key, "a" * 16, "b" * 16, b"{}")
    (s.analyses_dir(good.key) / f"{'c' * 16}.parts").mkdir(exist_ok=True)
    victim = s.list_analysis_hashes(good.key)[0]
    corrupt(s.analysis_path(good.key, victim))
    with caplog.at_level(logging.WARNING):
        page = list_episodes(s)
    assert [e.key for e in page.episodes] == [good.key]
    assert page.total == 1
    assert len(page.episodes[0].analyses) == 1
    assert page.episodes[0].analyses[0].prompt_hash != victim
    assert bad.key in caplog.text
    assert victim in caplog.text


def test_list_never_parses_transcript(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    ep = seed_episode(s, 1)
    corrupt(s.transcript_json_path(ep.key))
    assert list_episodes(s).episodes[0].has_transcript


def test_list_unreadable_root_is_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: ArtifactStore) -> list[str]:
        raise PermissionError("no")

    monkeypatch.setattr(ArtifactStore, "list_episode_keys", boom)
    with pytest.raises(TransientError, match="cannot be read"):
        list_episodes(ArtifactStore(tmp_path))


def test_list_independent_of_registry(tmp_path: Path) -> None:
    data = tmp_path / "data"
    vault = tmp_path / "vault"
    vault.mkdir()
    s = ArtifactStore(data)
    ep = seed_episode(s, 1, prompts=(None,))
    before = list_episodes(s)
    store = VaultStore(vault, "Earmark", data_dir=data)
    transcript = s.read_transcript(ep.key)
    assert transcript is not None
    store.store(ep, transcript, tuple(s.list_analyses(ep.key)), T0)
    store.rebuild()
    assert list_episodes(s) == before
    (data / "earmark.db").unlink(missing_ok=True)
    assert list_episodes(s) == before


def test_require_transcript(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    absent = make_episode(guid="urn:absent").key
    for key in (absent, "nothex", make_episode().key.upper(), make_episode().key + "0"):
        with pytest.raises(EpisodeNotFoundError) as exc:
            require_transcript(s, key)
        assert exc.value.message == EPISODE_NOT_FOUND
    assert not (tmp_path / "episodes").exists()
    ep = seed_episode(s, 1, transcript=False)
    with pytest.raises(TranscriptMissingError) as missing:
        require_transcript(s, ep.key)
    assert missing.value.message == NO_TRANSCRIPT
    corrupt_ep = seed_episode(s, 2)
    corrupt(s.transcript_json_path(corrupt_ep.key))
    require_transcript(s, corrupt_ep.key)  # parses nothing


def test_enqueue_reanalyze(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path / "data")
    ep = seed_episode(s, 1)
    jobs = JobStore(open_database(tmp_path / "earmark.db"))
    first = enqueue_reanalyze(jobs, s, ep.key, ReanalyzeRequest(prompt=" why? "))
    again = enqueue_reanalyze(jobs, s, ep.key, ReanalyzeRequest(prompt="why?"))
    assert first.created
    assert not again.created
    assert first.job.id == again.job.id
    assert (first.job.kind, first.job.url, first.job.prompt) == ("reanalyze", None, "why?")
    with pytest.raises(EpisodeNotFoundError):
        enqueue_reanalyze(jobs, s, "0" * 20, ReanalyzeRequest())
    assert len(jobs.list_recent(10)) == 1
