from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError
from earmark.errors import PermanentError, TransientError
from earmark.fakes import FakeStore
from earmark.index.chunks import EpisodeDocs, NotePaths
from earmark.index.rebuild import reindex
from earmark.vault.notes import VaultStore
from tests.factories import T0, make_analysis, make_episode, make_transcript
from tests.index_helpers import RecordingTarget, make_analysis_of, make_docs, notes_map


def write_episode(
    store: ArtifactStore, guid: str, *, transcript: bool = True, analysis: bool = True
) -> str:
    docs = make_docs(guid=guid)
    store.write_episode(docs.episode)
    if transcript:
        store.write_transcript(docs.transcript)
    if analysis:
        store.write_analysis(make_analysis_of(docs.episode))
    return docs.episode.key


def test_full_reindex_resets_then_rebuilds_each_episode(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    keys = sorted(write_episode(store, f"urn:r:{i}") for i in range(3))
    target = RecordingTarget(rows_per_episode=4)
    seen: list[list[str]] = []

    def locate(requested: Sequence[str]) -> dict[str, NotePaths]:
        seen.append(list(requested))
        return {k: NotePaths(f"E/{k}.md", None) for k in keys}

    report = reindex(store, target, locate)
    assert target.calls == ["reset", *(f"rebuild:{k}" for k in keys), "optimize"]
    assert seen == [keys]
    assert (report.episodes, report.rows, report.skipped, report.warnings) == (3, 12, 0, ())
    assert target.notes[keys[0]] == NotePaths(f"E/{keys[0]}.md", None)


def test_loader_reads_artifacts_inside_the_call(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    key = write_episode(store, "urn:late")
    loaded: list[object] = []

    class Target(RecordingTarget):
        def rebuild_episode(
            self, key: str, load: Callable[[], EpisodeDocs | None], notes: NotePaths, /
        ) -> int | None:
            # An analysis written after reindex started is seen, because load() runs here.
            store.write_analysis(
                make_analysis_of(
                    make_episode(guid="urn:late"),
                    prompt="added later",
                    created_at=T0.replace(year=2030),
                )
            )
            docs = load()
            assert docs is not None
            loaded.append(len(docs.analyses))
            return 1

    reindex(store, Target(), lambda keys: notes_map(keys))
    assert loaded == [2]
    assert key


def test_incomplete_and_corrupt_episodes_skipped(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    good = write_episode(store, "urn:good")
    write_episode(store, "urn:no-transcript", transcript=False)
    write_episode(store, "urn:no-analysis", analysis=False)
    bad = write_episode(store, "urn:corrupt")
    store.transcript_json_path(bad).write_text("{not json")
    target = RecordingTarget()
    report = reindex(store, target, lambda keys: notes_map(keys))
    assert (report.episodes, report.skipped) == (1, 3)
    assert report.warnings == (f"episode {bad}: unreadable files under EARMARK_DATA_DIR; skipped",)
    assert f"rebuild:{good}" in target.calls


def test_missing_note_warns(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    key = write_episode(store, "urn:nonote")
    report = reindex(store, RecordingTarget(), lambda keys: {})
    assert report.warnings == (
        f"episode {key}: no note found in the vault; its hits have no note path",
    )
    assert report.episodes == 1


def test_only_one_episode(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    a = write_episode(store, "urn:a")
    write_episode(store, "urn:b")
    target = RecordingTarget()
    report = reindex(store, target, lambda keys: notes_map(keys), only=a)
    assert target.calls == [f"rebuild:{a}", "optimize"]
    assert (report.episodes, report.rows) == (1, 3)

    with pytest.raises(ConfigError, match="--episode"):
        reindex(store, RecordingTarget(), lambda keys: {}, only="XYZ")
    with pytest.raises(PermanentError, match="has no transcript or analyses"):
        reindex(store, RecordingTarget(), lambda keys: {}, only="c" * 20)

    corrupt = write_episode(store, "urn:c")
    store.transcript_json_path(corrupt).write_text("{")
    with pytest.raises(PermanentError, match="corrupt artifact"):
        reindex(store, RecordingTarget(), lambda keys: {}, only=corrupt)


def test_transient_error_stops(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    keys = sorted(write_episode(store, f"urn:t:{i}") for i in range(3))
    target = RecordingTarget(fail_on={keys[1]: TransientError("disk full")})
    with pytest.raises(TransientError):
        reindex(store, target, lambda k: notes_map(k))
    assert target.calls == ["reset", f"rebuild:{keys[0]}", f"rebuild:{keys[1]}"]


def test_empty_data_dir(tmp_path: Path) -> None:
    target = RecordingTarget()
    report = reindex(ArtifactStore(tmp_path), target, lambda keys: {})
    assert (report.episodes, report.rows, report.skipped) == (0, 0, 0)
    assert target.calls == ["reset", "optimize"]


# -- locate_notes -----------------------------------------------------------------------


def _vault_store(tmp_path: Path) -> VaultStore:
    (tmp_path / "vault").mkdir(exist_ok=True)
    return VaultStore(tmp_path / "vault", "Earmark", data_dir=tmp_path / "data")


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    out: dict[str, tuple[int, bytes | None]] = {}
    for p in sorted(root.rglob("*")):
        out[str(p.relative_to(root))] = (
            p.stat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None,
        )
    return out


def test_locate_notes_reads_vault(tmp_path: Path) -> None:
    store = _vault_store(tmp_path)
    ep1, ep2 = make_episode(guid="urn:l:1"), make_episode(guid="urn:l:2")
    r1 = store.store(ep1, make_transcript(guid="urn:l:1"), [make_analysis(guid="urn:l:1")], T0)
    r2 = store.store(ep2, make_transcript(guid="urn:l:2"), [make_analysis(guid="urn:l:2")], T0)
    moved = tmp_path / "vault" / r2.note_path
    renamed = moved.with_name("Renamed by the user.md")
    moved.rename(renamed)
    before = _tree(tmp_path)
    found = store.locate_notes([ep1.key, ep2.key, "d" * 20])
    assert set(found) == {ep1.key, ep2.key}
    assert found[ep1.key] == NotePaths(r1.note_path, r1.transcript_note_path)
    assert found[ep2.key].note_path == str(renamed.relative_to(tmp_path / "vault"))
    assert found[ep2.key].transcript_note_path == r2.transcript_note_path
    assert _tree(tmp_path) == before


def test_locate_notes_missing_subdir_creates_nothing(tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    store = VaultStore(tmp_path / "vault", "Earmark/Deep", data_dir=tmp_path / "data")
    assert store.locate_notes([make_episode().key]) == {}
    assert list((tmp_path / "vault").iterdir()) == []
    assert not (tmp_path / "data").exists()
    gone = VaultStore(tmp_path / "nowhere", "Earmark", data_dir=tmp_path / "data")
    assert gone.locate_notes([make_episode().key]) == {}


def test_fake_store_locate_notes() -> None:
    store = FakeStore(None, "Notes")
    assert store.locate_notes(["a" * 20]) == {
        "a" * 20: NotePaths(f"Notes/Podcasts/{'a' * 20}.md", None)
    }
