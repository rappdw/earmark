import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from earmark.artifacts import (
    ArtifactStore,
    atomic_write_bytes,
    atomic_write_text,
    atomic_writer,
)
from earmark.errors import PermanentError
from tests.factories import T0, make_analysis, make_episode, make_transcript

KEY = make_episode().key


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_atomic_write_creates_file_and_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "f.txt"
    atomic_write_text(target, "héllo\r\n")
    assert target.read_bytes() == "héllo\r\n".encode()
    assert _files(target.parent) == ["f.txt"]


def test_atomic_write_failure_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(fd: int) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk"):
        atomic_write_bytes(tmp_path / "f", b"x")
    assert _files(tmp_path) == []


def test_atomic_write_failure_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f"
    target.write_text("old")

    def boom(fd: int) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk"):
        atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert _files(tmp_path) == ["f"]


def test_atomic_writer_body_exception(tmp_path: Path) -> None:
    def body() -> None:
        with atomic_writer(tmp_path / "f") as f:
            f.write(b"partial")
            raise RuntimeError("body")

    with pytest.raises(RuntimeError, match="body"):
        body()
    assert _files(tmp_path) == []


def test_atomic_write_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: object, dst: object) -> None:
        raise OSError("replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="replace"):
        atomic_write_bytes(tmp_path / "f", b"x")
    assert _files(tmp_path) == []


def test_atomic_write_mode(tmp_path: Path) -> None:
    atomic_write_bytes(tmp_path / "a", b"x")
    atomic_write_bytes(tmp_path / "b", b"x", mode=0o600)
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "b").stat().st_mode) == 0o600


def test_atomic_writer_temp_suffix(tmp_path: Path) -> None:
    with atomic_writer(tmp_path / "audio.mp3", temp_suffix=".part") as f:
        f.write(b"x")
        (temp,) = list(tmp_path.iterdir())
        assert temp.name.startswith(".")
        assert temp.name.endswith(".part")
    assert _files(tmp_path) == ["audio.mp3"]


BAD_KEYS = [
    "",
    "0" * 19,
    "0" * 21,
    "A" * 20,
    "g" * 20,
    "../" + "0" * 17,
    "0" * 20 + "\n",
    "0" * 19 + "/",
    "٣" * 20,
]


@pytest.mark.parametrize("key", BAD_KEYS)
def test_rejects_bad_keys(tmp_path: Path, key: str) -> None:
    store = ArtifactStore(tmp_path)
    for call in (
        store.episode_dir,
        store.transcript_json_path,
        store.find_audio,
        store.read_episode,
        lambda k: store.analysis_path(k, "0" * 16),
    ):
        with pytest.raises(ValueError, match="not an episode key"):
            call(key)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bad", ["", "0" * 15, "0" * 17, "A" * 16, "0" * 15 + "\n", "../" + "0" * 13]
)
def test_rejects_bad_prompt_hash(tmp_path: Path, bad: str) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="prompt hash"):
        store.analysis_path(KEY, bad)
    with pytest.raises(ValueError, match="prompt hash"):
        store.read_analysis(KEY, bad)


@pytest.mark.parametrize("ext", ["MP3", ".mp3", "mp3/x", "", "abcdef"])
def test_rejects_bad_audio_ext(tmp_path: Path, ext: str) -> None:
    with pytest.raises(ValueError, match="audio extension"):
        ArtifactStore(tmp_path).audio_path(KEY, ext)


def test_layout_paths(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    d = tmp_path / "episodes" / KEY
    assert s.root == tmp_path / "episodes"
    assert s.episode_dir(KEY) == d
    assert s.episode_path(KEY) == d / "episode.json"
    assert s.transcript_json_path(KEY) == d / "transcript.json"
    assert s.transcript_text_path(KEY) == d / "transcript.txt"
    assert s.analyses_dir(KEY) == d / "analyses"
    assert s.analysis_path(KEY, "a" * 16) == d / "analyses" / ("a" * 16 + ".json")
    assert s.audio_path(KEY, "m4a") == d / "audio.m4a"


def test_constructor_creates_nothing(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "data")
    store.episode_dir(KEY)
    assert not (tmp_path / "data").exists()


def test_episode_round_trip(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    ep = make_episode()
    path = s.write_episode(ep)
    assert path == s.episode_path(KEY)
    assert s.read_episode(KEY) == ep


def test_transcript_round_trip(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    tr = make_transcript()
    assert s.write_transcript(tr) == s.transcript_json_path(KEY)
    assert s.transcript_text_path(KEY).read_text(encoding="utf-8") == tr.to_text()
    assert s.transcript_json_path(KEY).read_text(encoding="utf-8").endswith("}\n")
    assert s.read_transcript(KEY) == tr


def test_analysis_round_trip(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    an = make_analysis()
    assert s.write_analysis(an) == s.analysis_path(KEY, an.prompt_hash)
    assert s.read_analysis(KEY, an.prompt_hash) == an


def test_read_missing_returns_none(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    assert s.read_episode(KEY) is None
    assert s.read_transcript(KEY) is None
    assert s.read_analysis(KEY, "a" * 16) is None


@pytest.mark.parametrize("content", ["not json SENTINEL", '{"SENTINEL": 1}'])
def test_corrupt_artifact_is_permanent(tmp_path: Path, content: str) -> None:
    s = ArtifactStore(tmp_path)
    s.episode_dir(KEY).mkdir(parents=True)
    s.episode_path(KEY).write_text(content)
    with pytest.raises(PermanentError) as info:
        s.read_episode(KEY)
    assert str(s.episode_path(KEY)) in info.value.message
    assert "SENTINEL" not in info.value.message


def test_mismatched_key_is_permanent(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    other = make_episode(guid="urn:other")
    s.write_episode(other)
    other_dir = s.episode_dir(other.key)
    target = s.episode_dir(KEY)
    target.mkdir(parents=True)
    (target / "episode.json").write_bytes((other_dir / "episode.json").read_bytes())
    with pytest.raises(PermanentError, match="corrupt artifact"):
        s.read_episode(KEY)
    # transcript and analysis likewise
    s.write_transcript(make_transcript(guid="urn:other"))
    (target / "transcript.json").write_bytes((other_dir / "transcript.json").read_bytes())
    with pytest.raises(PermanentError, match="corrupt artifact"):
        s.read_transcript(KEY)
    an = make_analysis(guid="urn:other")
    s.write_analysis(an)
    (target / "analyses").mkdir()
    (target / "analyses" / f"{an.prompt_hash}.json").write_bytes(
        s.analysis_path(other.key, an.prompt_hash).read_bytes()
    )
    with pytest.raises(PermanentError, match="corrupt artifact"):
        s.read_analysis(KEY, an.prompt_hash)


def test_mismatched_analysis_hash_is_permanent(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    an = make_analysis()
    s.write_analysis(an)
    wrong = s.analysis_path(KEY, "f" * 16)
    wrong.write_bytes(s.analysis_path(KEY, an.prompt_hash).read_bytes())
    with pytest.raises(PermanentError, match="corrupt artifact"):
        s.read_analysis(KEY, "f" * 16)


def test_find_and_delete_audio(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    d = s.episode_dir(KEY)
    assert s.find_audio(KEY) is None
    assert s.delete_audio(KEY) is False
    d.mkdir(parents=True)
    (d / ".audio.mp3.x.part").write_bytes(b"")
    (d / "audio.mp3.part").write_bytes(b"")
    assert s.find_audio(KEY) is None
    (d / "audio.m4a").write_bytes(b"x")
    assert s.find_audio(KEY) == d / "audio.m4a"
    assert s.delete_audio(KEY) is True
    assert s.delete_audio(KEY) is False
    assert s.find_audio(KEY) is None
    assert (d / "audio.mp3.part").exists()


def test_list_analyses_sorted_and_filtered(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    a = make_analysis(prompt="a", created_at=T0 + timedelta(hours=2))
    b = make_analysis(prompt="b", created_at=T0)
    c = make_analysis(prompt=None, created_at=T0 + timedelta(hours=1))
    for an in (a, b, c):
        s.write_analysis(an)
    d = s.analyses_dir(KEY)
    (d / "notes.json").write_text("{}")
    (d / f".{a.prompt_hash}.json.abc.tmp").write_text("junk")
    assert s.list_analyses(KEY) == [b, c, a]
    assert s.list_analyses(make_episode(guid="urn:none").key) == []


def test_list_episode_keys(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    assert s.list_episode_keys() == []
    e1, e2 = make_episode(), make_episode(guid="urn:two")
    s.write_episode(e1)
    s.write_episode(e2)
    (s.root / "not-a-key").mkdir()
    (s.root / "not-a-key" / "episode.json").write_text("{}")
    s.episode_dir("1" * 20).mkdir()
    assert s.list_episode_keys() == sorted([e1.key, e2.key])


def test_has_transcript(tmp_path: Path) -> None:
    s = ArtifactStore(tmp_path)
    assert not s.has_transcript(KEY)
    s.write_transcript(make_transcript())
    assert s.has_transcript(KEY)


def test_record_share(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    assert store.read_share(KEY) is None
    assert store.record_share(KEY, T0) == T0
    path = store.share_path(KEY)
    assert path == tmp_path / "episodes" / KEY / "shared.json"
    record = store.read_share(KEY)
    assert record is not None
    assert record.first_shared_at == T0
    before = path.read_bytes()
    assert store.record_share(KEY, T0 + timedelta(hours=1)) == T0
    assert store.record_share(KEY, T0) == T0
    assert path.read_bytes() == before
    assert store.record_share(KEY, T0 - timedelta(days=1)) == T0 - timedelta(days=1)
    assert path.read_bytes() != before
    reread = store.read_share(KEY)
    assert reread is not None
    assert reread.first_shared_at == T0 - timedelta(days=1)


def test_record_share_rejects_naive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        ArtifactStore(tmp_path).record_share(KEY, T0.replace(tzinfo=None))


def test_read_share_mismatched_key_is_corrupt(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    other = make_episode(guid="urn:example:other").key
    store.record_share(other, T0)
    store.share_path(KEY).parent.mkdir(parents=True)
    store.share_path(KEY).write_bytes(store.share_path(other).read_bytes())
    with pytest.raises(PermanentError, match="corrupt artifact"):
        store.read_share(KEY)


def test_read_share_garbage_is_corrupt(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.share_path(KEY).parent.mkdir(parents=True)
    store.share_path(KEY).write_text("{nope")
    with pytest.raises(PermanentError, match="corrupt artifact"):
        store.read_share(KEY)
