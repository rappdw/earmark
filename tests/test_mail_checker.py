"""The contract checker against the samples, one mutation per rule, and its exit codes."""

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.checker_helpers import (
    CHECKER,
    Interpreter,
    Parts,
    b64_corrupt,
    change_body,
    encode_word,
    floor_interpreters,
    front_value,
    interpreters,
    old_interpreters,
    rebuild,
    rename_instructions,
    rename_note,
    report,
    run_checker,
    set_front,
    set_guid,
    split,
    write_parts,
)
from tests.contract_samples import ALL_SAMPLES, SAMPLES, SAMPLES_DIR, sample_path

FLOOR = floor_interpreters()
PYTHONS = pytest.mark.parametrize("python", FLOOR, ids=lambda i: i.id)
SAMPLE_NAMES = list(SAMPLES)
ALL = pytest.mark.parametrize(("version", "name"), ALL_SAMPLES)


def _sample(name: str, version: str = "v2") -> bytes:
    return sample_path(version, name).read_bytes()


# --- 6.2: every sample passes ---------------------------------------------------------


@PYTHONS
@ALL
def test_samples_pass_message_mode(python: Interpreter, version: str, name: str) -> None:
    result = run_checker(python, "message", "--json", str(sample_path(version, name)))
    assert result.returncode == 0, result.stdout + result.stderr
    document = report(result)
    assert document["ok"] is True
    assert document["failures"] == []
    assert document["skipped"] == []
    parts = split(_sample(name, version))
    assert document["contract"] == int(version[1:])
    summary = document["summary"]
    assert summary["note"] == parts.note_name
    assert summary["instructions"] == parts.instructions_name
    assert summary["guid"] == front_value(parts.note, "guid").strip('"')
    assert summary["job_id"] == front_value(parts.instructions, "job_id").strip('"')
    assert summary["transcript_sha256"] == front_value(parts.note, "transcript_sha256").strip('"')
    assert summary["type"] in {"podcast", "audio", "video"}
    assert summary["message_id"] == dict(parts.headers)["Message-ID"]


@PYTHONS
@ALL
def test_samples_pass_files_mode(
    python: Interpreter, version: str, name: str, tmp_path: Path
) -> None:
    args = write_parts(split(_sample(name, version)), tmp_path)
    result = run_checker(python, "files", "--json", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    document = report(result)
    assert document["ok"] is True
    assert document["skipped"] == ["message.defects", "message.structure"]
    assert document["summary"]["message_id"] is None
    assert document["contract"] == int(version[1:])


def test_text_output_names_the_contract(tmp_path: Path) -> None:
    for version, name in (("v1", "podcast-feed"), ("v2", "youtube")):
        result = run_checker(interpreters()[0], "message", str(sample_path(version, name)))
        assert result.returncode == 0, result.stdout
        assert result.stdout.splitlines()[0] == f"OK: earmark mail, contract {version[1:]}"


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_rebuild_round_trip(name: str, tmp_path: Path) -> None:
    rebuilt = tmp_path / "rebuilt.eml"
    rebuilt.write_bytes(rebuild(split(_sample(name))))
    result = run_checker(interpreters()[0], "message", "--json", str(rebuilt))
    assert result.returncode == 0, result.stdout
    assert report(result)["failures"] == []


def test_files_mode_skips_what_it_was_not_given(tmp_path: Path) -> None:
    parts = split(_sample("podcast-feed"))
    write_parts(parts, tmp_path)  # the note and instructions files; only those are passed
    minimal = [
        "--note",
        str(tmp_path / "note"),
        "--note-name",
        parts.note_name,
        "--instructions",
        str(tmp_path / "instructions"),
        "--instructions-name",
        parts.instructions_name,
    ]
    result = run_checker(interpreters()[0], "files", "--json", *minimal)
    assert result.returncode == 0, result.stdout
    skipped = report(result)["skipped"]
    assert {
        "message.defects",
        "message.structure",
        "attachment.media-type",
        "subject.prefix",
        "subject.length",
        "subject.control",
        "body.names",
        "body.job-id",
        "body.third-party",
        "body.no-guid",
    } == set(skipped)


# --- 6.3: a mutation per rule -----------------------------------------------------------

Mutate = Callable[[Parts, bytes], bytes]
LONG = "a" * 81


def _note(key: str, line: str | None) -> Mutate:
    return lambda p, raw: rebuild(dataclasses.replace(p, note=set_front(p.note, key, line)))


def _instr(key: str, line: str | None) -> Mutate:
    return lambda p, raw: rebuild(
        dataclasses.replace(p, instructions=set_front(p.instructions, key, line))
    )


def _rename(name: str) -> Mutate:
    return lambda p, raw: rebuild(rename_note(p, name))


def _rename_and_date(name: str, date: str) -> Mutate:
    def mutate(p: Parts, raw: bytes) -> bytes:
        moved = rename_note(p, name)
        return rebuild(dataclasses.replace(moved, note=set_front(moved.note, "date", date)))

    return mutate


def _note_bytes(change: Callable[[bytes], bytes]) -> Mutate:
    return lambda p, raw: rebuild(p.with_note(change))


def _body_edit(old: str, new: str) -> Mutate:
    return lambda p, raw: rebuild(p.with_note(lambda n: change_body(n, old, new)))


def _guid(guid: str) -> Mutate:
    return lambda p, raw: rebuild(set_guid(p, guid))


def _subject(text: str) -> Mutate:
    return lambda p, raw: rebuild(dataclasses.replace(p, subject=text))


def _body(change: Callable[[Parts], str]) -> Mutate:
    return lambda p, raw: rebuild(dataclasses.replace(p, body=change(p)))


def _media(note: str) -> Mutate:
    return lambda p, raw: rebuild(dataclasses.replace(p, note_media_type=note))


def _raw_subject_control(p: Parts, raw: bytes) -> bytes:
    word = encode_word("[earmark] bell\x07")
    out = rebuild(dataclasses.replace(p, subject="[earmark] x"))
    return out.replace(b"Subject: [earmark] x", b"Subject: " + word.encode())


def _long_title(n: int) -> Mutate:
    return _note("title", 'title: "' + "t" * n + '"')


def _long_show(n: int) -> Mutate:
    return _note("show", 'show: "' + "s" * n + '"')


def _both_contracts(line: str) -> Mutate:
    return lambda p, raw: rebuild(
        dataclasses.replace(
            p,
            note=set_front(p.note, "contract", line),
            instructions=set_front(p.instructions, "contract", line),
        )
    )


def _first_job_id(p: Parts) -> str:
    return front_value(p.instructions, "job_id").strip('"')


@dataclasses.dataclass(frozen=True)
class Case:
    rule: str
    variant: str
    base: str
    mutate: Mutate
    files: bool = False
    version: str = "v2"

    @property
    def id(self) -> str:
        return f"{self.rule}-{self.variant}"


def _upper_hash(note: bytes) -> bytes:
    digest = front_value(note, "transcript_sha256").strip('"').upper()
    return set_front(note, "transcript_sha256", f'transcript_sha256: "{digest}"')


F = "podcast-feed"
Y = "youtube"
CASES: list[Case] = [
    Case("message.defects", "base64", F, lambda p, raw: b64_corrupt(raw)),
    Case(
        "message.structure",
        "third-attachment",
        F,
        lambda p, raw: rebuild(p, extra_attachments=(("extra.md", b"x"),)),
    ),
    Case("message.structure", "html", F, lambda p, raw: rebuild(p, extra_html=True)),
    Case("attachment.media-type", "plain", F, _media("text/plain; charset=utf-8"), files=True),
    Case("attachment.media-type", "charset", F, _media("text/markdown; charset=iso-8859-1"), True),
    Case(
        "attachment.names",
        "notes",
        F,
        lambda p, raw: rebuild(
            rename_instructions(p, p.note_name[:-3] + ".notes.md"),
        ),
    ),
    Case("filename.note", "uppercase", F, _rename("2026-11-20-THE-EXAMPLE.md")),
    Case("filename.note", "bad-date", F, _rename("2026-02-30-the-example.md")),
    Case("filename.note", "upper-extension", F, _rename("2026-11-20-the-example.MD")),
    Case("filename.note", "long-slug", F, _rename(f"2026-11-20-{LONG}.md")),
    Case("filename.note", "double-hyphen", F, _rename("2026-11-20-the--example.md")),
    Case(
        "filename.instructions",
        "other-stem",
        F,
        lambda p, raw: rebuild(rename_instructions(p, "2026-11-20-another.instructions.md")),
    ),
    Case(
        "file.utf8",
        "note",
        F,
        _note_bytes(lambda n: n.replace(b"The Example Topic", b"The Example \xff", 1)),
    ),
    Case(
        "file.utf8",
        "instructions",
        F,
        lambda p, raw: rebuild(p.with_instructions(lambda i: i + b"\xff")),
    ),
    Case("file.bom", "note", "upload", _note_bytes(lambda n: b"\xef\xbb\xbf" + n)),
    Case(
        "file.lf",
        "crlf",
        "direct-url",
        lambda p, raw: rebuild(p.with_instructions(lambda i: i.replace(b"\n", b"\r\n"))),
    ),
    Case("frontmatter.fence", "closing", F, _note_bytes(lambda n: n.replace(b"\n---\n", b"\n", 1))),
    Case("frontmatter.syntax", "single-quote", F, _note("title", "title: 'x'")),
    Case("frontmatter.syntax", "duplicate", F, _note("again", 'type: "podcast"')),
    Case("frontmatter.syntax", "comment", F, _note("c", "# a comment")),
    Case("frontmatter.syntax", "flow-list", F, _note("tags", 'tags: ["podcast"]')),
    Case("frontmatter.syntax", "plain", F, _note("type", "type: podcast")),
    Case("frontmatter.empty", "string", F, _note("language", 'language: ""')),
    Case("frontmatter.empty", "list", F, _note("tags", "tags:")),
    Case("string.control", "bell", F, _note("title", 'title: "The \\u0007 Topic"')),
    Case("string.control", "line-separator", F, _note("title", f'title: "a{chr(0x2028)}b"')),
    Case("string.length", "title", F, _long_title(201)),
    Case("string.length", "show", F, _long_show(301)),
    Case("note.keys", "extra", F, _note("summary", 'summary: "x"')),
    Case("note.keys", "channel-v1", F, _note("channel", 'channel: "x"'), version="v1"),
    Case("note.required", "duration", F, _note("duration", None)),
    Case("note.required", "show", F, _note("show", None)),
    Case("note.required", "video-source-url", Y, _note("source_url", None)),
    Case("note.omitted", "audio-show", "upload", _note("show", 'show: "x"')),
    Case("note.omitted", "podcast-channel", F, _note("channel", 'channel: "x"')),
    Case("note.omitted", "video-show", Y, _note("show", 'show: "x"')),
    Case(
        "note.omitted", "video-apple-url", Y, _note("apple_url", 'apple_url: "https://x.example/a"')
    ),
    Case(
        "note.omitted",
        "audio-source-url",
        "upload",
        _note("source_url", 'source_url: "https://audio.example.net/x"'),
    ),
    Case(
        "note.omitted",
        "podcast-source-url",
        F,
        _note("source_url", 'source_url: "https://audio.example.net/x"'),
    ),
    Case("note.types", "contract", F, _note("contract", 'contract: "1"')),
    Case("note.types", "date", F, _note("date", 'date: "2026-11-20"')),
    Case("note.types", "no-offset", F, _note("shared_at", "shared_at: 2026-11-22T10:00:00")),
    Case("note.types", "tags", F, _note("tags", 'tags: "podcast"')),
    Case("note.type", "video-in-v1", F, _note("type", 'type: "video"'), version="v1"),
    Case("note.contract", "three", "youtube", _both_contracts("contract: 3")),
    Case("note.source", "other", F, _note("source", 'source: "other"')),
    Case("note.origin", "upload", F, _note("origin", 'origin: "upload"')),
    Case("note.origin", "youtube", F, _note("origin", 'origin: "youtube"')),
    Case("note.origin", "url-video", Y, _note("origin", 'origin: "url"')),
    Case("note.date", "other-day", F, _note("date", "date: 2026-11-21")),
    Case(
        "note.filename-date",
        "moved",
        F,
        _rename_and_date("2026-11-21-the-example-show-the-example-topic.md", "date: 2026-11-21"),
    ),
    Case("note.duration", "short", F, _note("duration", 'duration: "1:02:03"')),
    Case("note.language", "word", F, _note("language", 'language: "english!"')),
    Case(
        "note.source-url",
        "query",
        "direct-url",
        _note("source_url", 'source_url: "https://audio.example.net/talks/keynote-2026.mp3?x=1"'),
    ),
    Case(
        "note.source-url",
        "userinfo",
        "direct-url",
        _note("source_url", 'source_url: "https://user@audio.example.net/talks/keynote-2026.mp3"'),
    ),
    Case(
        "note.source-url",
        "fragment",
        "direct-url",
        _note("source_url", 'source_url: "https://audio.example.net/talks/keynote-2026.mp3#a"'),
    ),
    Case("note.guid", "podcast-scheme", F, _guid("podcast:1")),
    Case("note.guid", "apple-letters", F, _guid("apple:abc")),
    Case("note.guid", "audio-feed", "upload", _guid("feed:x")),
    Case("note.guid", "video-short", Y, _guid("youtube:short")),
    Case("note.guid", "video-sha256", Y, _guid("sha256:" + "ab" * 32)),
    Case("note.guid", "podcast-youtube", F, _guid("youtube:exampleVid1")),
    Case("note.guid", "audio-uppercase", "upload", _guid("sha256:" + "AB" * 32)),
    Case(
        "note.source-url-youtube",
        "other-id",
        Y,
        _note("source_url", 'source_url: "https://youtu.be/exampleVid2"'),
    ),
    Case(
        "note.source-url-youtube",
        "watch-path",
        Y,
        _note("source_url", 'source_url: "https://www.youtube.com/watch"'),
    ),
    Case("note.sha256-form", "uppercase", F, lambda p, raw: rebuild(p.with_note(_upper_hash))),
    Case(
        "note.hash",
        "word-changed",
        F,
        _note_bytes(lambda n: n.replace(b"Welcome", b"Welcomed", 1)),
    ),
    Case("note.tags", "audio", F, _note("tags", 'tags:\n  - "audio"')),
    Case("note.tags", "two", F, _note("tags", 'tags:\n  - "podcast"\n  - "extra"')),
    Case(
        "note.heading",
        "no-show",
        F,
        _body_edit("# The Example Show — The Example Topic", "# The Example Topic"),
    ),
    Case(
        "note.heading",
        "video-no-channel",
        Y,
        _body_edit("# The Example Channel — An Example Talk", "# An Example Talk"),
    ),
    Case("note.tags", "video-podcast", Y, _note("tags", 'tags:\n  - "podcast"')),
    Case("string.length", "channel", Y, _note("channel", 'channel: "' + "c" * 301 + '"')),
    Case("note.paragraphs", "no-marker", F, _body_edit("[00:00:50] ", "")),
    Case("note.paragraphs", "extra-newline", F, _body_edit("\n\n[00:00:35]", "\n\n\n[00:00:35]")),
    Case("instructions.keys", "missing", F, _instr("job_id", None)),
    Case("instructions.keys", "extra", F, _instr("note", 'note: "x"')),
    Case("instructions.types", "contract", F, _instr("contract", 'contract: "1"')),
    Case(
        "instructions.types",
        "shared-at",
        F,
        _instr("shared_at", 'shared_at: "2026-11-22T18:00:00+00:00"'),
    ),
    Case("instructions.type", "note", F, _instr("type", 'type: "earmark-note"')),
    Case("instructions.contract", "one", Y, _instr("contract", "contract: 1")),
    Case("instructions.for", "other", F, _instr("for", 'for: "2026-11-20-other.md"')),
    Case("instructions.guid", "other", F, _instr("guid", 'guid: "feed:other"')),
    Case("subject.prefix", "colon", F, _subject("earmark: x"), files=True),
    Case("subject.length", "121", F, _subject("[earmark] " + "x" * 111), files=True),
    Case("subject.control", "bell", F, _raw_subject_control, files=True),
    Case(
        "body.names",
        "note-name",
        F,
        _body(lambda p: p.body.replace(p.note_name, "other.md")),
        files=True,
    ),
    Case(
        "body.job-id",
        "other",
        F,
        _body(lambda p: p.body.replace(_first_job_id(p), "f" * 32)),
        files=True,
    ),
    Case(
        "body.third-party",
        "removed",
        F,
        _body(lambda p: p.body.replace("third-party", "other")),
        files=True,
    ),
    Case(
        "body.no-guid",
        "appended",
        F,
        _body(lambda p: p.body + front_value(p.note, "guid").strip('"') + "\n"),
        files=True,
    ),
]


def _rules_of(python: Interpreter) -> list[str]:
    result = run_checker(python, "rules", "--json")
    assert result.returncode == 0
    listing = json.loads(result.stdout)
    return [str(item["rule"]) for item in listing]


def test_every_rule_has_a_mutation() -> None:
    assert {c.rule for c in CASES} == set(_rules_of(interpreters()[0]))


def test_case_ids_are_unique() -> None:
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))


@PYTHONS
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_mutation_fails(python: Interpreter, case: Case, tmp_path: Path) -> None:
    raw = _sample(case.base, case.version)
    mutated = case.mutate(split(raw), raw)
    path = tmp_path / "mutated.eml"
    path.write_bytes(mutated)
    result = run_checker(python, "message", "--json", str(path))
    assert result.returncode == 1, result.stdout + result.stderr
    rules = {f["rule"] for f in report(result)["failures"]}
    assert case.rule in rules, rules
    assert "internal" not in rules
    if case.files:
        args = write_parts(split(mutated), tmp_path)
        again = run_checker(python, "files", "--json", *args)
        assert again.returncode == 1, again.stdout + again.stderr
        files_rules = {f["rule"] for f in report(again)["failures"]}
        assert case.rule in files_rules, files_rules
        assert "internal" not in files_rules


# --- 6.4: exit codes ----------------------------------------------------------------------


@PYTHONS
def test_exit_2_missing_input(python: Interpreter, tmp_path: Path) -> None:
    sample = SAMPLES_DIR / "podcast-feed.eml"
    note = tmp_path / "note"
    note.write_bytes(split(sample.read_bytes()).note)
    cases = [
        ("message", str(tmp_path / "missing.eml")),
        ("message", str(tmp_path)),
        (
            "files",
            "--note",
            str(tmp_path / "gone"),
            "--note-name",
            "a.md",
            "--instructions",
            str(note),
            "--instructions-name",
            "a.instructions.md",
        ),
    ]
    for args in cases:
        result = run_checker(python, *args)
        assert result.returncode == 2, args
        assert result.stdout == ""
        assert len(result.stderr.strip().splitlines()) == 1


@pytest.mark.parametrize("python", old_interpreters(), ids=lambda i: i.id)
def test_exit_2_old_interpreter(python: Interpreter) -> None:
    result = run_checker(python, "rules")
    assert result.returncode == 2
    assert "3.9" in result.stderr
    assert result.stdout == ""


def test_exit_2_guard_simulated() -> None:
    code = (
        "import runpy, sys; sys.version_info = (3, 8, 20, 'final', 0); "
        f"runpy.run_path({str(CHECKER)!r}, run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "needs Python 3.9 or newer" in result.stderr
    assert result.stdout == ""


USAGE_ARGS = [
    [],
    ["bogus"],
    ["message"],
    ["message", "a", "b"],
    ["message", "--nope", "x"],
    ["files", "--note", "n", "--instructions", "i", "--instructions-name", "x.md"],
]


@PYTHONS
@pytest.mark.parametrize("args", USAGE_ARGS, ids=lambda a: " ".join(a) or "none")
def test_exit_64_usage(python: Interpreter, args: list[str]) -> None:
    result = run_checker(python, *args)
    assert result.returncode == 64, result.stderr
    assert "usage:" in result.stderr
    assert result.stdout == ""


@PYTHONS
@pytest.mark.parametrize("args", [["--help"], ["message", "--help"]])
def test_help_exits_0(python: Interpreter, args: list[str]) -> None:
    result = run_checker(python, *args)
    assert result.returncode == 0
    assert "usage:" in result.stdout


# --- the rest of the checker's promises ----------------------------------------------------


@PYTHONS
def test_output_never_echoes_mail_text(python: Interpreter, tmp_path: Path) -> None:
    base = split(_sample(F))
    note = base.note
    note = note.replace(b"The Example Topic", b"TOKENTITLE9x").replace(
        b"The Example Show", b"TOKENSHOW9x"
    )
    note = set_front(note, "guid", 'guid: "feed:TOKENGUID9x"')
    instructions = set_front(base.instructions, "guid", 'guid: "feed:TOKENGUID9x"')
    instructions += b"TOKENINSTR9x\n"
    mutated = dataclasses.replace(
        base,
        note=note,
        instructions=instructions,
        body=base.body + "TOKENBODY9x\n",
        subject="[earmark] TOKENSUBJECT9x" + "x" * 120,
    )
    path = tmp_path / "m.eml"
    path.write_bytes(rebuild(mutated))
    tokens = ("TOKENTITLE9x", "TOKENSHOW9x", "TOKENINSTR9x", "TOKENBODY9x", "TOKENSUBJECT9x")
    plain = run_checker(python, "message", str(path))
    as_json = run_checker(python, "message", "--json", str(path))
    assert plain.returncode == as_json.returncode == 1
    for token in tokens:
        assert token not in plain.stdout
        assert token not in as_json.stdout
    assert "TOKENGUID9x" not in plain.stdout
    document = report(as_json)
    assert document["summary"]["guid"] == "feed:TOKENGUID9x"
    assert as_json.stdout.count("TOKENGUID9x") == 1
    assert {f["rule"] for f in document["failures"]} >= {"note.hash", "subject.length"}


def test_rules_subcommand() -> None:
    python = interpreters()[0]
    plain = run_checker(python, "rules")
    as_json = run_checker(python, "rules", "--json")
    assert plain.returncode == as_json.returncode == 0
    listing = json.loads(as_json.stdout)
    assert [line.split("\t")[0] for line in plain.stdout.splitlines()] == [
        item["rule"] for item in listing
    ]
    needs = {"message", "media-type", "subject", "body", "attachments"}
    for item in listing:
        assert item["needs"] in needs
        assert item["description"].isascii()
        assert item["description"].strip()


def test_floor_interpreters_present_when_required() -> None:
    import os

    if os.environ.get("MAIL_CHECKER_REQUIRE_FLOOR") != "1":
        pytest.skip("MAIL_CHECKER_REQUIRE_FLOOR is not set; `make check` sets it")
    versions = [i.version for i in interpreters()]
    assert (3, 9) in versions, versions
    assert any(v < (3, 9) for v in versions), versions
