"""The mail code reproduces every sample email byte for byte, and each sample shows what it says."""

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from tests import contract_samples
from tests.checker_helpers import ROOT, Parts, front_value, split
from tests.contract_samples import (
    ALL_SAMPLES,
    SAMPLES,
    SAMPLES_DIR,
    V1_DIR,
    V1_NAMES,
    V1_SHA256,
    build,
    drifted,
    write,
)
from tests.doc_helpers import table_rows

NAMES = list(SAMPLES)
ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _parts(name: str) -> Parts:
    return split((SAMPLES_DIR / f"{name}.eml").read_bytes())


def _note(name: str, key: str) -> str:
    return front_value(_parts(name).note, key).strip('"')


@pytest.mark.parametrize("name", NAMES)
def test_samples_reproduce(name: str) -> None:
    assert (SAMPLES_DIR / f"{name}.eml").read_bytes() == build(name)


def test_sample_set_is_complete() -> None:
    files = sorted(p.stem for p in SAMPLES_DIR.glob("*.eml"))
    readme = (SAMPLES_DIR / "README.md").read_text(encoding="utf-8")
    cells = [row[0].strip("`").removesuffix(".eml") for row in table_rows(readme)]
    assert sorted(NAMES) == files == sorted(cells)
    assert len(NAMES) == 13
    v1_files = sorted(p.stem for p in V1_DIR.glob("*.eml"))
    v1_readme = (V1_DIR / "README.md").read_text(encoding="utf-8")
    v1_cells = [row[0].strip("`").removesuffix(".eml") for row in table_rows(v1_readme)]
    assert sorted(V1_NAMES) == v1_files == sorted(v1_cells)
    assert NAMES[: len(V1_NAMES)] == list(V1_NAMES)
    guids = {n: _note(n, "guid") for n in NAMES}
    assert any(g.startswith("feed:") for g in guids.values())
    assert any(g.startswith("apple:") for g in guids.values())
    assert sum(g.startswith("sha256:") for g in guids.values()) >= 2
    assert not _parts("empty-instructions").instructions.partition(b"\n---\n")[2]
    assert "podcast-feed.resend" in NAMES
    assert "hostile-title" in NAMES


def test_v1_samples_frozen() -> None:
    assert sorted(p.stem for p in V1_DIR.glob("*.eml")) == sorted(V1_NAMES)
    assert set(V1_SHA256) == set(V1_NAMES)
    for name, digest in V1_SHA256.items():
        assert hashlib.sha256((V1_DIR / f"{name}.eml").read_bytes()).hexdigest() == digest, name


def test_generator_check_and_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for path in SAMPLES_DIR.glob("*.eml"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / "podcast-apple.eml").write_bytes(b"changed")
    (tmp_path / "upload.eml").unlink()
    (tmp_path / "x.eml").write_bytes(b"stale")
    (tmp_path / "README.md").write_text("keep", encoding="utf-8")
    assert drifted(tmp_path) == ["podcast-apple", "upload"]
    assert write(tmp_path) == NAMES
    assert not (tmp_path / "x.eml").exists()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "keep"
    assert drifted(tmp_path) == []
    monkeypatch.setattr(contract_samples, "SAMPLES_DIR", tmp_path)
    (tmp_path / "direct-url.eml").write_bytes(b"changed")
    monkeypatch.setattr(contract_samples, "drifted", lambda: drifted(tmp_path))
    assert contract_samples.main(["--check"]) == 1
    (tmp_path / "direct-url.eml").write_bytes(build("direct-url"))
    assert contract_samples.main(["--check"]) == 0
    # A changed v1 file fails the check too.
    changed = tmp_path / "v1"
    changed.mkdir()
    for path in V1_DIR.glob("*.eml"):
        (changed / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(contract_samples, "V1_DIR", changed)
    assert contract_samples.main(["--check"]) == 0
    (changed / "upload.eml").write_bytes(b"changed")
    assert contract_samples.main(["--check"]) == 1
    (changed / "upload.eml").unlink()
    assert contract_samples.main(["--check"]) == 1


def test_generator_check_of_the_committed_samples() -> None:
    assert contract_samples.main(["--check"]) == 0


def test_sample_properties() -> None:
    assert _note("podcast-feed", "guid").startswith("feed:https://feeds.example.com/")
    assert "published" in _parts("podcast-feed").note.decode()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", _note("podcast-apple", "published"))
    assert _note("podcast-apple", "guid").startswith("apple:")
    assert b"\nlanguage:" not in _parts("podcast-apple").note
    assert b"\nlanguage:" in _parts("podcast-feed").note
    for name in ("upload", "direct-url", "direct-url-query"):
        assert _note(name, "guid").startswith("sha256:")
        assert b"\npublished:" not in _parts(name).note
    assert b"\nsource_url:" in _parts("direct-url").note
    assert b"\nsource_url:" not in _parts("direct-url-query").note
    assert _note("direct-url-query", "title") == "lecture-01.mp3"
    assert _note("upload", "title") == "Team standup notes"
    assert _note("upload", "shared_at").endswith("+01:00")
    assert _note("podcast-apple", "shared_at").endswith("+00:00")
    assert _note("podcast-feed", "shared_at").endswith("-08:00")


def test_youtube_sample_properties() -> None:
    assert _note("youtube", "type") == "video"
    assert _note("youtube", "guid") == "youtube:exampleVid1"
    assert _note("youtube", "source_url") == "https://youtu.be/exampleVid1"
    assert _note("youtube", "channel") == "The Example Channel"
    assert _note("youtube", "published") == "2026-11-21"
    assert _note("youtube", "shared_at").endswith("+02:00")
    parts = _parts("youtube")
    body = parts.note.partition(b"\n---\n")[2].decode()
    assert body.startswith("# The Example Channel — An Example Talk\n\n## Transcript\n\n")
    assert b'tags:\n  - "video"\n' in parts.note
    quiet = _parts("youtube-no-channel")
    assert b"\nchannel:" not in quiet.note
    assert b"\nlanguage:" not in quiet.note
    assert _note("youtube-no-channel", "published") == "2026-11-19"
    assert _note("youtube-no-channel", "guid") == "youtube:ex_ample-02"
    assert quiet.note.partition(b"\n---\n")[2].startswith(b"# Untitled Upload Example\n")
    reshare = _parts("youtube.reshare")
    assert reshare.note == parts.note
    assert front_value(reshare.instructions, "job_id") != front_value(parts.instructions, "job_id")


def test_every_v2_message_is_contract_2() -> None:
    for name in NAMES:
        parts = _parts(name)
        assert front_value(parts.note, "contract") == "2", name
        assert front_value(parts.instructions, "contract") == "2", name


def test_v1_messages_are_contract_1() -> None:
    for name in V1_NAMES:
        parts = split((V1_DIR / f"{name}.eml").read_bytes())
        assert front_value(parts.note, "contract") == "1", name
        assert front_value(parts.instructions, "contract") == "1", name


def test_hostile_sample() -> None:
    parts = _parts("hostile-title")
    front = yaml.safe_load(parts.note.partition(b"\n---\n")[0].removeprefix(b"---\n"))
    text = front["title"]
    assert len(text) == 200
    assert text.endswith("…")
    assert not any(ord(c) < 32 for c in text)
    assert len(parts.subject) == 120
    wire = (SAMPLES_DIR / "hostile-title.eml").read_bytes()
    assert b"=?utf-8?" in wire.partition(b"\r\nDate:")[0]
    assert _note("hostile-title", "guid").startswith("apple:")
    # The instructions stay inert: attachment text, and no header of their own.
    assert b"Bcc: evil@example.com" in parts.instructions
    assert b"\r\nBcc:" not in wire
    assert parts.body.count("Bcc") == 0


def test_resend_keeps_message_id() -> None:
    one = (SAMPLES_DIR / "podcast-feed.eml").read_bytes()
    two = (SAMPLES_DIR / "podcast-feed.resend.eml").read_bytes()
    a, b = split(one), split(two)
    assert dict(a.headers)["Message-ID"] == dict(b.headers)["Message-ID"]
    assert dict(a.headers)["Date"] != dict(b.headers)["Date"]
    assert (a.note, a.instructions) == (b.note, b.instructions)
    assert front_value(a.instructions, "job_id") == front_value(b.instructions, "job_id")
    differing = [
        (x, y) for x, y in zip(one.split(b"\r\n"), two.split(b"\r\n"), strict=True) if x != y
    ]
    assert len(differing) == 1
    assert differing[0][0].startswith(b"Date: ")
    assert differing[0][1].startswith(b"Date: ")


def test_reshare_and_retranscription() -> None:
    first, reshare, retranscription = (
        _parts(n) for n in ("podcast-feed", "podcast-feed.reshare", "podcast-feed.retranscription")
    )
    assert front_value(first.note, "guid") == front_value(reshare.note, "guid")
    assert front_value(first.instructions, "job_id") != front_value(reshare.instructions, "job_id")
    assert dict(first.headers)["Message-ID"] != dict(reshare.headers)["Message-ID"]
    assert first.note == reshare.note
    assert first.instructions != reshare.instructions
    assert front_value(first.note, "guid") == front_value(retranscription.note, "guid")
    assert front_value(first.instructions, "job_id") != front_value(
        retranscription.instructions, "job_id"
    )
    assert front_value(first.note, "transcript_sha256") != front_value(
        retranscription.note, "transcript_sha256"
    )
    body = retranscription.note.partition(b"\n---\n")[2]
    digest = hashlib.sha256(body).hexdigest()
    assert front_value(retranscription.note, "transcript_sha256").strip('"') == digest


@pytest.mark.parametrize(("version", "name"), ALL_SAMPLES)
def test_samples_use_placeholders_only(version: str, name: str) -> None:
    path = contract_samples.sample_path(version, name)
    parts = split(path.read_bytes())
    raw = path.read_bytes().decode("ascii")
    texts = [raw, parts.note.decode(), parts.instructions.decode(), parts.subject]
    for text in texts:
        for match in ADDRESS.finditer(text):
            domain = match[0].rsplit("@", 1)[1].lower().rstrip(".")
            assert domain in {"example.org", "example.net", "example.com"}, (name, match[0])
        assert IPV4.search(text) is None, name


def test_samples_are_wire_format() -> None:
    for path in [*sorted(SAMPLES_DIR.glob("*.eml")), *sorted(V1_DIR.glob("*.eml"))]:
        data = path.read_bytes()
        data.decode("ascii")
        assert data.count(b"\n") == data.count(b"\r\n"), path.name
        assert data.endswith(b"\r\n")
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "/contract/samples/** -text" in attributes
