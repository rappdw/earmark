"""The receiving skill's receiver: recognising, checking, de-duplicating and the guarded writes."""

import ast
import dataclasses
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.checker_helpers import (
    RECEIVER,
    SCRIPTS,
    Interpreter,
    Parts,
    body_of,
    floor_interpreters,
    front_value,
    old_interpreters,
    rename_instructions,
    rename_note,
    run_receiver,
    set_front,
    split,
)
from tests.contract_samples import ALL_SAMPLES, SAMPLES
from tests.receiver_helpers import (
    attachment_args,
    receive,
    records,
    sample_args,
    sample_bytes,
    tree,
    workspace,
)

FLOOR = floor_interpreters()
PYTHONS = pytest.mark.parametrize("python", FLOOR, ids=lambda i: i.id)
SAMPLE_NAMES = list(SAMPLES)
ALL_NAMES = [f"{version}/{name}" for version, name in ALL_SAMPLES]
NOTE = "2026-11-20-the-example-show-the-example-topic.md"
FILED = f"podcasts/{NOTE}"
SKIPPED = ["attachment.media-type", "message.defects", "message.structure"]


class Env:
    """A workspace and a way to feed it messages."""

    def __init__(self, python: Interpreter, tmp_path: Path) -> None:
        self.python = python
        self.tmp = tmp_path
        self.ws = workspace(tmp_path)
        self.count = 0

    def args(self, name: str) -> list[str]:
        self.count += 1
        return sample_args(name, self.tmp / f"in{self.count}")

    def parts_args(self, parts: Parts, **kwargs: Any) -> list[str]:
        self.count += 1
        return attachment_args(parts, self.tmp / f"in{self.count}", **kwargs)

    def run(self, command: str, name: str, *extra: str) -> tuple[int, dict[str, Any]]:
        return receive(self.python, command, self.ws, *self.args(name), *extra)

    def classify(self, name: str) -> dict[str, Any]:
        code, doc = self.run("classify", name)
        assert code == (0 if doc["ok"] else 1)
        return doc

    def file(self, name: str = "podcast-feed", dest: str = "podcasts") -> dict[str, Any]:
        code, doc = self.run("file", name, "--dest", dest)
        assert code == 0, doc
        return doc

    def note(self, path: str = FILED) -> bytes:
        return (self.ws / path).read_bytes()


def _expected(name: str) -> dict[str, str]:
    parts = split(sample_bytes(name))
    return {
        "guid": front_value(parts.note, "guid").strip('"'),
        "job_id": front_value(parts.instructions, "job_id").strip('"'),
        "transcript_sha256": front_value(parts.note, "transcript_sha256").strip('"'),
        "shared_at": front_value(parts.instructions, "shared_at"),
        "type": front_value(parts.note, "type").strip('"'),
        "note": parts.note_name,
        "instructions": parts.instructions_name,
    }


# --- 6.2: recognising and checking ----------------------------------------------------


@PYTHONS
@pytest.mark.parametrize("name", ALL_NAMES)
def test_every_sample_classifies_new(python: Interpreter, tmp_path: Path, name: str) -> None:
    env = Env(python, tmp_path)
    code, doc = env.run("classify", name)
    assert code == 0, doc
    assert doc["outcome"] == "new"
    assert doc["failures"] == []
    assert doc["skipped"] == SKIPPED
    for key, value in _expected(name).items():
        assert doc[key] == value, key
    assert doc["path"] is None
    assert set(tree(env.ws)) == {".pka/bin/pka-tools"}


@PYTHONS
@pytest.mark.parametrize("name", ALL_NAMES)
def test_every_sample_classifies_new_eml(python: Interpreter, tmp_path: Path, name: str) -> None:
    ws = workspace(tmp_path)
    eml = tmp_path / "m.eml"
    eml.write_bytes(sample_bytes(name))
    code, doc = receive(python, "classify", ws, "--eml", str(eml))
    assert code == 0, doc
    assert doc["outcome"] == "new"
    assert doc["skipped"] == []
    for key, value in _expected(name).items():
        assert doc[key] == value, key


@PYTHONS
def test_empty_instructions_flag(python: Interpreter, tmp_path: Path) -> None:
    flags = {
        n: Env(python, tmp_path / n.replace("/", "-")).classify(n)["instructions_empty"]
        for n in ALL_NAMES
    }
    assert {n for n, empty in flags.items() if empty} == {
        "v1/empty-instructions",
        "v2/empty-instructions",
        "v2/youtube-no-channel",
    }


def _drop_second(args: list[str]) -> list[str]:
    return args[:6] + args[10:]


def _third(args: list[str]) -> list[str]:
    return [*args[:10], "--attachment", "extra.md", args[4], "clean", *args[10:]]


def _mutated(change: Callable[[Parts], Parts], **kwargs: Any) -> Callable[[Env, Parts], list[str]]:
    return lambda env, parts: env.parts_args(change(parts), **kwargs)


def _case_big(env: Env, parts: Parts) -> list[str]:
    args = env.parts_args(parts)
    Path(args[4]).write_bytes(b"x" * (17 * 1024 * 1024))
    return args


def _note(change: Callable[[bytes], bytes]) -> Callable[[Parts], Parts]:
    return lambda parts: parts.with_note(change)


def _instr(change: Callable[[bytes], bytes]) -> Callable[[Parts], Parts]:
    return lambda parts: parts.with_instructions(change)


NOT_EARMARK: dict[str, tuple[Callable[[Env, Parts], list[str]], list[str]]] = {
    "one-attachment": (lambda e, p: _drop_second(e.parts_args(p)), ["attachments.count"]),
    "three-attachments": (lambda e, p: _third(e.parts_args(p)), ["attachments.count"]),
    "stripped": (
        lambda e, p: e.parts_args(p, dispositions=("clean", "stripped")),
        ["attachments.disposition"],
    ),
    "name-with-space": (
        _mutated(lambda p: rename_note(p, "2026-11-20 the topic.md")),
        ["attachments.names"],
    ),
    "no-instructions-suffix": (
        _mutated(lambda p: rename_instructions(p, "other.md")),
        ["attachments.names"],
    ),
    "other-stem": (
        _mutated(lambda p: rename_instructions(p, "2026-11-20-other.instructions.md")),
        ["attachments.names"],
    ),
    "source-other": (
        _mutated(_note(lambda b: set_front(b, "source", 'source: "other"'))),
        ["note.marker"],
    ),
    "source-removed": (_mutated(_note(lambda b: set_front(b, "source", None))), ["note.marker"]),
    "instructions-type": (
        _mutated(_instr(lambda b: set_front(b, "type", 'type: "notes"'))),
        ["instructions.marker"],
    ),
    "instructions-guid": (
        _mutated(
            _instr(lambda b: set_front(b, "guid", 'guid: "feed:https://other.example.com/1"'))
        ),
        ["instructions.marker"],
    ),
    "seventeen-mib": (_case_big, ["attachments.size"]),
}


@PYTHONS
@pytest.mark.parametrize("case", list(NOT_EARMARK))
def test_not_earmark(python: Interpreter, tmp_path: Path, case: str) -> None:
    env = Env(python, tmp_path)
    build, reasons = NOT_EARMARK[case]
    args = build(env, split(sample_bytes("podcast-feed")))
    before = tree(env.ws)
    code, doc = receive(python, "classify", env.ws, *args)
    assert code == 1, doc
    assert doc["outcome"] == "not-earmark"
    assert doc["error"] == "not-earmark"
    assert doc["reasons"] == reasons
    assert tree(env.ws) == before
    code, doc = receive(python, "file", env.ws, *args, "--dest", "podcasts")
    assert (code, doc["error"]) == (1, "not-new")
    assert tree(env.ws) == before


@PYTHONS
def test_unknown_contract(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    parts = split(sample_bytes("podcast-feed"))
    parts = parts.with_note(lambda b: set_front(b, "contract", "contract: 3"))
    parts = parts.with_instructions(lambda b: set_front(b, "contract", "contract: 3"))
    code, doc = receive(python, "classify", env.ws, *env.parts_args(parts))
    assert code == 1
    assert doc["outcome"] == "unknown-contract"
    assert doc["contract"] == 3


def _swap_body(old: str, new: str) -> Callable[[Parts], Parts]:
    return lambda p: p.with_note(lambda b: b.replace(old.encode(), new.encode(), 1))


INVALID: dict[str, tuple[Callable[[Parts], Parts], str]] = {
    "note.hash": (_swap_body("the example topic", "another topic"), "note.hash"),
    "body.job-id": (
        lambda p: dataclasses.replace(
            p, body=p.body.replace(front_value(p.instructions, "job_id").strip('"'), "x")
        ),
        "body.job-id",
    ),
    "subject.prefix": (
        lambda p: dataclasses.replace(p, subject="The Example Topic"),
        "subject.prefix",
    ),
    "instructions.for": (
        _instr(lambda b: set_front(b, "for", 'for: "other.md"')),
        "instructions.for",
    ),
}


@PYTHONS
@pytest.mark.parametrize("case", list(INVALID))
def test_invalid_uses_the_checker(python: Interpreter, tmp_path: Path, case: str) -> None:
    env = Env(python, tmp_path)
    change, rule = INVALID[case]
    code, doc = receive(
        python, "classify", env.ws, *env.parts_args(change(split(sample_bytes("podcast-feed"))))
    )
    assert code == 1, doc
    assert doc["outcome"] == "invalid"
    assert rule in doc["failures"]
    assert doc["failures"] == sorted(set(doc["failures"]))
    code, doc = receive(
        python,
        "file",
        env.ws,
        *env.parts_args(change(split(sample_bytes("podcast-feed")))),
        "--dest",
        "podcasts",
    )
    assert (code, doc["error"]) == (1, "not-new")
    assert not (env.ws / "podcasts").exists()


@PYTHONS
def test_hostile_title_is_inert(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    docs = [env.classify("hostile-title")]
    filed = env.file("hostile-title")
    docs.append(filed)
    expected = split(sample_bytes("hostile-title")).note
    assert env.note(filed["path"]) == expected
    docs.append(
        env.run("annotate", "hostile-title", "--path", filed["path"], "--tag", "economics")[1]
    )
    docs.append(env.run("record", "hostile-title", "--path", filed["path"])[1])
    strings: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(docs)
    text = "\n".join(strings)
    for token in (
        "evil@example.com",
        "Ignore",
        "Hostile",
        "caf",
        '"quoted"',
        "Bcc:",
        "back\\slash",
    ):
        assert token not in text, token


# --- 6.3: duplicate, re-share and re-transcription ------------------------------------


@PYTHONS
def test_resend_is_duplicate(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    assert env.run("record", "podcast-feed", "--path", FILED)[0] == 0
    before, log = tree(env.ws), records(env.ws)
    for name in ("podcast-feed.resend", "podcast-feed"):
        doc = env.classify(name)
        assert doc["outcome"] == "duplicate"
        assert doc["path"] == FILED
    for command, extra in (
        ("file", ("--dest", "podcasts")),
        ("record", ("--path", FILED)),
        ("annotate", ("--path", FILED, "--tag", "x")),
    ):
        code, doc = env.run(command, "podcast-feed.resend", *extra)
        expected = "not-new" if command == "file" else "nothing-to-do"
        assert (code, doc["error"]) == (1, expected), command
    assert tree(env.ws) == before
    assert records(env.ws) == log


@PYTHONS
def test_reshare(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    env.run("record", "podcast-feed", "--path", FILED)
    doc = env.classify("podcast-feed.reshare")
    assert doc["outcome"] == "reshare"
    assert doc["path"] == FILED
    assert doc["rewrite_body"] is None
    assert env.run("record", "podcast-feed.reshare", "--path", FILED)[0] == 0
    done = [r for r in records(env.ws) if r["event"] == "done"]
    assert [r["job_id"][-2:] for r in done] == ["01", "03"]
    assert sorted(p.name for p in (env.ws / "podcasts").iterdir()) == [NOTE]
    assert env.classify("podcast-feed.reshare")["outcome"] == "duplicate"


@PYTHONS
def test_empty_instructions_reshare(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    doc = env.classify("empty-instructions")
    assert doc["outcome"] == "reshare"
    assert doc["instructions_empty"] is True


@PYTHONS
def test_retranscription_rewrites_body(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    assert env.run("annotate", "podcast-feed", "--path", FILED, "--tag", "economics")[0] == 0
    doc = env.classify("podcast-feed.retranscription")
    assert (doc["outcome"], doc["rewrite_body"]) == ("retranscription", True)
    before = env.note().decode("utf-8")
    code, doc = env.run("rewrite", "podcast-feed.retranscription", "--path", FILED)
    assert code == 0, doc
    after = env.note()
    sample = split(sample_bytes("podcast-feed.retranscription")).note
    assert body_of(after) == body_of(sample)
    assert front_value(after, "transcript_sha256") == front_value(sample, "transcript_sha256")
    old_head = before.partition("\n---\n")[0].split("\n")
    new_head = after.decode("utf-8").partition("\n---\n")[0].split("\n")
    new_sha = front_value(sample, "transcript_sha256")
    expected = [
        f"transcript_sha256: {new_sha}" if x.startswith("transcript_sha") else x for x in old_head
    ]
    assert new_head == expected
    assert '  - "economics"' in new_head
    assert [r["event"] for r in records(env.ws)] == ["filed", "rewritten"]
    assert env.classify("podcast-feed.retranscription")["outcome"] == "reshare"
    assert env.run("record", "podcast-feed.retranscription", "--path", FILED)[0] == 0
    assert (env.ws / FILED).exists()


@PYTHONS
def test_out_of_order_keeps_newer_body(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file("podcast-feed.retranscription")
    doc = env.classify("podcast-feed.reshare")
    assert (doc["outcome"], doc["rewrite_body"]) == ("retranscription", False)
    before = tree(env.ws)
    code, doc = env.run("rewrite", "podcast-feed.reshare", "--path", FILED)
    assert (code, doc["error"]) == (1, "older-body")
    assert tree(env.ws) == before
    assert env.run("record", "podcast-feed.reshare", "--path", FILED)[0] == 0
    sample = split(sample_bytes("podcast-feed.retranscription")).note
    assert env.note() == sample


@PYTHONS
def test_lost_record(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    (env.ws / ".pka/earmark/received.jsonl").unlink()
    assert env.classify("podcast-feed.reshare")["outcome"] == "reshare"
    doc = env.classify("podcast-feed.retranscription")
    assert (doc["outcome"], doc["rewrite_body"]) == ("retranscription", None)
    before = tree(env.ws)
    code, doc = env.run("rewrite", "podcast-feed.retranscription", "--path", FILED)
    assert (code, doc["error"]) == (1, "order-unknown")
    assert tree(env.ws) == before
    code, doc = env.run(
        "rewrite", "podcast-feed.retranscription", "--path", FILED, "--owner-approved"
    )
    assert code == 0, doc
    sample = split(sample_bytes("podcast-feed.retranscription")).note
    assert body_of(env.note()) == body_of(sample)


@PYTHONS
def test_moved_note_found_by_guid(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    (env.ws / "elsewhere").mkdir()
    (env.ws / FILED).rename(env.ws / "elsewhere" / NOTE)
    doc = env.classify("podcast-feed.reshare")
    assert (doc["outcome"], doc["path"]) == ("reshare", f"elsewhere/{NOTE}")


@PYTHONS
def test_missing_note(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    (env.ws / FILED).unlink()
    doc = env.classify("podcast-feed.reshare")
    assert (doc["outcome"], doc["previously_filed"]) == ("new", FILED)


@PYTHONS
def test_ambiguous(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    (env.ws / "other").mkdir()
    shutil.copy(env.ws / FILED, env.ws / "other" / NOTE)
    (env.ws / ".pka/earmark/received.jsonl").unlink()
    code, doc = env.run("classify", "podcast-feed.reshare")
    assert code == 1
    assert doc["outcome"] == "ambiguous"
    assert doc["matches"] == [f"other/{NOTE}", FILED]


@PYTHONS
def test_interrupted_run_is_handled_again(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    assert env.classify("podcast-feed")["outcome"] == "reshare"
    assert env.run("record", "podcast-feed", "--path", FILED)[0] == 0
    assert env.classify("podcast-feed")["outcome"] == "duplicate"


@PYTHONS
def test_other_samples_are_independent(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    others = [
        n
        for n in SAMPLE_NAMES
        if n not in {"podcast-feed", "podcast-feed.resend", "podcast-feed.reshare"}
        and n not in {"podcast-feed.retranscription", "empty-instructions"}
    ]
    assert len(others) == 8
    for name in others:
        assert env.classify(name)["outcome"] == "new", name


@PYTHONS
def test_youtube_reshare(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    doc = env.file("v2/youtube")
    path = doc["path"]
    assert path.endswith("2026-11-21-the-example-channel-an-example-talk.md")
    again = env.classify("v2/youtube.reshare")
    assert (again["outcome"], again["path"]) == ("reshare", path)
    assert again["guid"] == "youtube:exampleVid1"


@PYTHONS
def test_reshare_across_contract_versions(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file("v1/podcast-feed")
    doc = env.classify("v2/podcast-feed.reshare")
    assert (doc["outcome"], doc["path"]) == ("reshare", FILED)
    assert doc["contract"] == 2


@PYTHONS
def test_v2_retranscription(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file("v2/podcast-feed")
    doc = env.classify("v2/podcast-feed.retranscription")
    assert (doc["outcome"], doc["rewrite_body"]) == ("retranscription", True)


@PYTHONS
def test_record_lines(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    parts = split(sample_bytes("podcast-feed"))
    receive(python, "file", env.ws, *env.parts_args(parts, inbox_id="id-7"), "--dest", "podcasts")
    receive(python, "record", env.ws, *env.parts_args(parts, inbox_id="id-8"), "--path", FILED)
    log = records(env.ws)
    assert [r["event"] for r in log] == ["filed", "done"]
    keys = {
        "v",
        "at",
        "event",
        "guid",
        "job_id",
        "message_id",
        "path",
        "shared_at",
        "transcript_sha256",
    }
    for line in log:
        assert set(line) == keys
        assert line["v"] == 1
        assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", line["at"])
    assert [r["message_id"] for r in log] == ["id-7", "id-8"]
    file = env.ws / ".pka/earmark/received.jsonl"
    with file.open("a", encoding="utf-8") as out:
        out.write('not json\n[1, 2]\n{"event": "done", "job_id": "x", "path": "p"}\n')
    doc = env.classify("podcast-feed.reshare")
    assert doc["record"] == {"lines": 2, "unreadable": 3}
    assert doc["outcome"] == "reshare"


# --- 6.4: preflight, writes, exit codes ------------------------------------------------


PREFLIGHT_OK = {"1.8.0": "1.8.0", "1.11.0": "1.11.0", "2.0.0": "2.0.0", "1.10.0": "1.10.0"}


@PYTHONS
@pytest.mark.parametrize("version", list(PREFLIGHT_OK))
def test_preflight_passes(python: Interpreter, tmp_path: Path, version: str) -> None:
    ws = workspace(tmp_path, pka_tools=version)
    code, doc = receive(python, "preflight", ws)
    assert code == 0, doc
    assert doc["pka_tools"] == version
    assert doc["minimum"] == "1.8.0"
    assert doc["python"].startswith(f"{python.version[0]}.{python.version[1]}.")


def _custom_tool(tmp_path: Path, script: str, mode: int = 0o755) -> Path:
    ws = workspace(tmp_path, pka_tools=None)
    tool = ws / ".pka" / "bin" / "pka-tools"
    tool.parent.mkdir()
    tool.write_text(script, encoding="utf-8")
    tool.chmod(mode)
    return ws


PREFLIGHT_FAIL: dict[str, tuple[Callable[[Path], Path], int]] = {
    "older": (lambda t: workspace(t, pka_tools="1.7.0"), 2),
    "older-numeric": (lambda t: workspace(t, pka_tools="1.7.10"), 2),
    "missing": (lambda t: workspace(t, pka_tools=None), 2),
    "not-executable": (lambda t: _custom_tool(t, "#!/bin/sh\necho x\n", 0o644), 2),
    "not-json": (lambda t: _custom_tool(t, "#!/bin/sh\necho oops\n"), 2),
    "two-part-version": (lambda t: workspace(t, pka_tools="1.8"), 2),
    "tool-fails": (lambda t: workspace(t, exit_code=1), 2),
    "tool-not-ready": (lambda t: workspace(t, exit_code=3), 3),
    "no-pka": (lambda t: t, 3),
}


@PYTHONS
@pytest.mark.parametrize("case", list(PREFLIGHT_FAIL))
def test_preflight_fails(python: Interpreter, tmp_path: Path, case: str) -> None:
    build, expected = PREFLIGHT_FAIL[case]
    code, doc = receive(python, "preflight", build(tmp_path))
    assert code == expected, doc
    assert doc["ok"] is False
    assert doc["error"] in {"prerequisite", "not-ready"}


@PYTHONS
def test_preflight_runs_only_version(python: Interpreter, tmp_path: Path) -> None:
    script = (
        '#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls.txt"\nprintf \'{"version": "1.11.0"}\\n\'\n'
    )
    ws = _custom_tool(tmp_path, script)
    env = Env(python, tmp_path / "inputs")
    env.ws = ws
    assert receive(python, "preflight", ws)[0] == 0
    assert env.run("classify", "podcast-feed")[0] == 0
    assert env.run("file", "podcast-feed", "--dest", "podcasts")[0] == 0
    assert (ws / ".pka/bin/calls.txt").read_text(encoding="utf-8") == "version --json\n"


DESTS = [
    "team-inbox",
    "team-inbox/x",
    "owner-inbox",
    ".pka",
    ".pka/earmark",
    "a/.hidden",
    "../out",
    "/abs",
    "a\\b",
    ".",
    "",
    "link/x",
    "cap/sub",
    "cap",
    "a//b",
]


@PYTHONS
@pytest.mark.parametrize("dest", DESTS)
def test_dest_refused(python: Interpreter, tmp_path: Path, dest: str) -> None:
    env = Env(python, tmp_path)
    (tmp_path / "outside").mkdir()
    (env.ws / "link").symlink_to(tmp_path / "outside")
    (env.ws / "cap").mkdir()
    (env.ws / "cap" / "meeting.json").write_text("{}", encoding="utf-8")
    before = tree(env.ws)
    code, doc = env.run("file", "podcast-feed", "--dest", dest)
    assert (code, doc["error"]) == (1, "dest-refused")
    assert tree(env.ws) == before
    assert not list((tmp_path / "outside").iterdir())


@PYTHONS
def test_file_writes_exact_bytes(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    before = tree(env.ws)
    code, doc = env.run("file", "podcast-feed", "--dest", "podcasts/nested", "--dry-run")
    assert code == 0
    assert doc["path"] == f"podcasts/nested/{NOTE}"
    assert tree(env.ws) == before
    assert not (env.ws / "podcasts").exists()
    doc = env.file(dest="podcasts/nested")
    sample = split(sample_bytes("podcast-feed"))
    assert env.note(doc["path"]) == sample.note
    new = set(tree(env.ws)) - set(before)
    assert new == {f"podcasts/nested/{NOTE}", ".pka/earmark/received.jsonl"}
    assert not any(p.name.endswith(".instructions.md") for p in env.ws.rglob("*"))
    other = Env(python, tmp_path / "second")
    (other.ws / "podcasts").mkdir()
    (other.ws / "podcasts" / NOTE).write_text("something else\n", encoding="utf-8")
    code, doc = other.run("file", "podcast-feed", "--dest", "podcasts")
    assert (code, doc["error"]) == (1, "exists")
    assert (other.ws / "podcasts" / NOTE).read_text(encoding="utf-8") == "something else\n"


def _changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}


@PYTHONS
def test_writes_touch_only_the_note(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    before = tree(env.ws)
    assert env.run("annotate", "podcast-feed", "--path", FILED, "--tag", "reading")[0] == 0
    after = tree(env.ws)
    assert _changed(before, after) == {FILED}
    before = after
    assert env.run("rewrite", "podcast-feed.retranscription", "--path", FILED)[0] == 0
    after = tree(env.ws)
    assert _changed(before, after) == {FILED, ".pka/earmark/received.jsonl"}
    before = after
    assert (
        env.run("move", "podcast-feed.retranscription", "--path", FILED, "--dest", "moved")[0] == 0
    )
    after = tree(env.ws)
    assert _changed(before, after) == {FILED, f"moved/{NOTE}", ".pka/earmark/received.jsonl"}
    doc = env.classify("podcast-feed.retranscription")
    assert (doc["outcome"], doc["path"]) == ("reshare", f"moved/{NOTE}")


@PYTHONS
def test_path_mismatch(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    env.file("podcast-apple", dest="podcasts")
    other = "podcasts/" + split(sample_bytes("podcast-apple")).note_name
    (env.ws / "plain.md").write_text("# plain\n", encoding="utf-8")
    before = tree(env.ws)
    for name in ("podcast-feed.reshare", "podcast-feed.retranscription"):
        for target in (other, "plain.md"):
            for command, extra in (
                ("annotate", ("--tag", "x")),
                ("rewrite", ()),
                ("move", ("--dest", "m")),
                ("record", ()),
            ):
                code, doc = env.run(command, name, "--path", target, *extra)
                assert (code, doc["error"]) == (1, "path-mismatch"), (name, target, command)
    assert tree(env.ws) == before


def _filed_tags(env: Env) -> list[str]:
    front = env.note().decode("utf-8").partition("\n---\n")[0]
    return [x for x in front.split("\n") if x.startswith("  - ")]


@PYTHONS
def test_annotate_tags(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    target = env.ws / FILED
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace('  - "podcast"', "  - podcast\n  - reading"), encoding="utf-8")
    code, doc = env.run(
        "annotate", "podcast-feed", "--path", FILED, "--tag", "podcast", "--tag", "reading",
        "--tag", "economics/macro", "--tag", "economics/macro",
    )  # fmt: skip
    assert code == 0, doc
    assert _filed_tags(env) == ["  - podcast", "  - reading", '  - "economics/macro"']
    assert len(records(env.ws)) == 1
    before = tree(env.ws)
    for tag in ("Economics", "a b", ""):
        code, doc = env.run("annotate", "podcast-feed", "--path", FILED, "--tag", tag)
        assert (code, doc["error"]) == (1, "bad-tag"), tag
    assert tree(env.ws) == before
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            '  - podcast\n  - reading\n  - "economics/macro"', ""
        ),
        encoding="utf-8",
    )
    target.write_text(
        target.read_text(encoding="utf-8").replace("tags:\n", 'tags: ["podcast"]\n', 1),
        encoding="utf-8",
    )
    code, doc = env.run("annotate", "podcast-feed", "--path", FILED, "--tag", "x")
    assert (code, doc["error"]) == (1, "tags-not-a-list")


@PYTHONS
def test_annotate_summary(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    summary = tmp_path / "summary.txt"
    summary.write_text('  line one\n\n says "hi" \\ there \x07 ', encoding="utf-8")
    code, doc = env.run("annotate", "podcast-feed", "--path", FILED, "--summary-file", str(summary))
    assert code == 0, doc
    assert 'summary: "line one says \\"hi\\" \\\\ there"' in env.note().decode("utf-8")
    summary.write_text("second", encoding="utf-8")
    env.run("annotate", "podcast-feed", "--path", FILED, "--summary-file", str(summary))
    front = env.note().decode("utf-8").partition("\n---\n")[0]
    assert front.count("summary:") == 1
    assert 'summary: "second"' in front
    for text in ("x" * 301, " \n ", ""):
        summary.write_text(text, encoding="utf-8")
        code, doc = env.run(
            "annotate", "podcast-feed", "--path", FILED, "--summary-file", str(summary)
        )
        assert (code, doc["error"]) == (1, "bad-summary"), len(text)
    summary.write_text("x" * 300, encoding="utf-8")
    assert (
        env.run("annotate", "podcast-feed", "--path", FILED, "--summary-file", str(summary))[0] == 0
    )
    assert env.run("annotate", "podcast-feed", "--path", FILED, "--tag", "z", "--dry-run")[0] == 0
    assert '  - "z"' not in env.note().decode("utf-8")


@PYTHONS
def test_rewrite_refusals(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    env.file()
    target = env.ws / FILED
    original = target.read_bytes()
    target.write_bytes(original.replace(b"[00:00:35]", b"[00:00:35]\r"))
    before = tree(env.ws)
    code, doc = env.run("rewrite", "podcast-feed.retranscription", "--path", FILED)
    assert (code, doc["error"]) == (1, "cr-in-note")
    assert tree(env.ws) == before
    target.write_bytes(
        b"\n".join(x for x in original.split(b"\n") if not x.startswith(b"transcript_sha256"))
    )
    before = tree(env.ws)
    code, doc = env.run("rewrite", "podcast-feed.retranscription", "--path", FILED)
    assert (code, doc["error"]) == (1, "no-sha-line")
    assert tree(env.ws) == before


# --- exit codes, usage, the floor -------------------------------------------------------


USAGE_CASES: dict[str, list[str]] = {
    "none": [],
    "bogus": ["bogus"],
    "no-workspace": ["classify", "--eml", "x"],
    "eml-and-attachment": [
        "classify",
        "--workspace",
        "w",
        "--eml",
        "x",
        "--attachment",
        "a",
        "b",
        "c",
    ],
    "neither": ["classify", "--workspace", "w"],
    "bad-inbox-id": ["classify", "--workspace", "w", "--eml", "x", "--inbox-id", "a b"],
    "two-values": ["classify", "--workspace", "w", "--attachment", "a", "b"],
    "annotate-nothing": ["annotate", "--workspace", "w", "--eml", "x", "--path", "p"],
    "unknown-option": ["classify", "--workspace", "w", "--eml", "x", "--nope"],
}


@PYTHONS
@pytest.mark.parametrize("case", list(USAGE_CASES))
def test_exit_64_usage(python: Interpreter, case: str) -> None:
    result = run_receiver(python, *USAGE_CASES[case])
    assert result.returncode == 64, result.stderr
    assert result.stdout == ""
    assert "usage" in result.stderr.lower()


@PYTHONS
def test_help_lists_the_subcommands(python: Interpreter) -> None:
    result = run_receiver(python, "--help")
    assert result.returncode == 0
    for command in ("preflight", "classify", "file", "rewrite", "move", "annotate", "record"):
        assert command in result.stdout


@PYTHONS
def test_exit_2_inputs(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    args = env.args("podcast-feed")
    missing = [*args]
    missing[4] = str(tmp_path / "gone")
    folder = [*args]
    folder[4] = str(tmp_path)
    for case in (missing, folder):
        code, doc = receive(python, "classify", env.ws, *case)
        assert (code, doc["error"]) == (2, "prerequisite")
    code, doc = receive(python, "classify", env.ws, "--eml", str(tmp_path / "gone.eml"))
    assert (code, doc["error"]) == (2, "prerequisite")


@PYTHONS
def test_exit_3_workspace(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    (env.ws / ".pka" / "real").mkdir()
    (env.ws / ".pka" / "earmark").symlink_to(env.ws / ".pka" / "real")
    code, doc = env.run("classify", "podcast-feed")
    assert (code, doc["error"]) == (3, "not-ready")
    (env.ws / ".pka" / "earmark").unlink()
    (env.ws / ".pka" / "earmark").mkdir()
    (env.ws / ".pka" / "earmark" / "received.jsonl").mkdir()
    assert env.run("classify", "podcast-feed")[0] == 3
    code, doc = receive(python, "classify", tmp_path / "nowhere", *env.args("podcast-feed"))
    assert (code, doc["error"]) == (3, "not-ready")


@pytest.mark.parametrize("python", old_interpreters(), ids=lambda i: i.id)
def test_exit_2_old_interpreter(python: Interpreter) -> None:
    result = run_receiver(python, "--help")
    assert result.returncode == 2
    assert "3.9" in result.stderr
    assert result.stdout == ""


def test_exit_2_guard_simulated() -> None:
    code = (
        "import runpy, sys; sys.version_info = (3, 8, 20, 'final', 0); "
        f"runpy.run_path({str(RECEIVER)!r}, run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "receive.py needs Python 3.9 or newer" in result.stderr
    assert result.stdout == ""


@PYTHONS
def test_output_is_one_json_line(python: Interpreter, tmp_path: Path) -> None:
    env = Env(python, tmp_path)
    runs = [
        run_receiver(python, "preflight", "--workspace", str(env.ws)),
        run_receiver(python, "classify", "--workspace", str(env.ws), *env.args("podcast-feed")),
        run_receiver(
            python, "file", "--workspace", str(env.ws), *env.args("podcast-feed"), "--dest", "x"
        ),
        run_receiver(
            python, "file", "--workspace", str(env.ws), *env.args("podcast-feed"), "--dest", "x"
        ),
        run_receiver(python, "preflight", "--workspace", str(tmp_path / "none")),
    ]
    assert [r.returncode for r in runs] == [0, 0, 0, 1, 3]
    for run in runs:
        [line] = run.stdout.splitlines()
        doc = json.loads(line)
        assert line == json.dumps(doc, ensure_ascii=True, sort_keys=True)
        assert {"receiver", "command", "ok"} <= set(doc)
        assert doc["ok"] is (run.returncode == 0)
        assert ("error" in doc) is (run.returncode != 0)


def test_receiver_imports() -> None:
    source = (SCRIPTS / "earmark_receive.py").read_text(encoding="utf-8")
    tree_ = ast.parse(source)
    banned = {"socket", "ssl", "http", "urllib", "smtplib", "ftplib", "asyncio"}
    for node in ast.walk(tree_):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            root = name.split(".")[0]
            assert root in sys.stdlib_module_names or root == "earmark_mail_contract", name
            assert root not in banned, name
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "git" not in node.value.lower().split(), node.value
    calls: list[str] = []
    for func in (n for n in ast.walk(tree_) if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run":
                calls += [func.name]
    assert calls == ["_preflight"]
    assert (
        sum(
            ast.unparse(n.func) == "subprocess.run"
            for n in ast.walk(tree_)
            if isinstance(n, ast.Call)
        )
        == 1
    )
