"""The mail code reproduces every sample email byte for byte, and each sample shows what it says."""

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from tests import contract_samples
from tests.checker_helpers import ROOT, Parts, front_value, split
from tests.contract_samples import SAMPLES, SAMPLES_DIR, build, drifted, write
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
    assert len(NAMES) == 10
    guids = {n: _note(n, "guid") for n in NAMES}
    assert any(g.startswith("feed:") for g in guids.values())
    assert any(g.startswith("apple:") for g in guids.values())
    assert sum(g.startswith("sha256:") for g in guids.values()) >= 2
    assert not _parts("empty-instructions").instructions.partition(b"\n---\n")[2]
    assert "podcast-feed.resend" in NAMES
    assert "hostile-title" in NAMES


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


def test_samples_use_placeholders_only() -> None:
    for name in NAMES:
        parts = _parts(name)
        raw = (SAMPLES_DIR / f"{name}.eml").read_bytes().decode("ascii")
        texts = [raw, parts.note.decode(), parts.instructions.decode(), parts.subject]
        for text in texts:
            for match in ADDRESS.finditer(text):
                domain = match[0].rsplit("@", 1)[1].lower().rstrip(".")
                assert domain in {"example.org", "example.net", "example.com"}, (name, match[0])
            assert IPV4.search(text) is None, name


def test_samples_are_wire_format() -> None:
    for path in sorted(SAMPLES_DIR.glob("*.eml")):
        data = path.read_bytes()
        data.decode("ascii")
        assert data.count(b"\n") == data.count(b"\r\n"), path.name
        assert data.endswith(b"\r\n")
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "/contract/samples/** -text" in attributes
