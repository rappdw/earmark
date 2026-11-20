import os
import subprocess
import sys
from pathlib import Path

import pytest

from earmark.vault.aliases import (
    ALIASES_FILE,
    MAX_ALIASES_BYTES,
    AliasEntry,
    AliasFileError,
    parse_aliases,
    read_aliases,
)
from earmark.vault.fs import Vault

VALID = """\
# comment
people:
  Ada Lovelace:
    - Ada King
    - Augusta Ada King
  "Dr. Mara Ellison": [Mara Ellison]
companies:
  Meta: [Facebook, Meta Platforms]
topics:
  Large language models: LLMs
  AI:
"""


def test_parse_valid() -> None:
    parsed = parse_aliases(VALID)
    assert parsed.problems == ()
    assert parsed.entries == (
        AliasEntry("person", "ada lovelace", "ada lovelace", "Ada Lovelace"),
        AliasEntry("person", "ada king", "ada lovelace", "Ada Lovelace"),
        AliasEntry("person", "augusta ada king", "ada lovelace", "Ada Lovelace"),
        AliasEntry("person", "dr mara ellison", "dr mara ellison", "Dr. Mara Ellison"),
        AliasEntry("person", "mara ellison", "dr mara ellison", "Dr. Mara Ellison"),
        AliasEntry("company", "meta", "meta", "Meta"),
        AliasEntry("company", "facebook", "meta", "Meta"),
        AliasEntry("company", "meta platforms", "meta", "Meta"),
        AliasEntry(
            "topic", "large language models", "large language models", "Large language models"
        ),
        AliasEntry("topic", "llms", "large language models", "Large language models"),
        AliasEntry("topic", "ai", "ai", "AI"),
    )


def test_empty_and_comment_only() -> None:
    assert parse_aliases("").entries == ()
    assert parse_aliases("# nothing\n").entries == ()


def test_raw_scalars() -> None:
    parsed = parse_aliases("topics:\n  1984: [No, 'null']\n  Yes: ~x\n")
    names = [(e.alias_key, e.canonical_name) for e in parsed.entries]
    assert ("1984", "1984") in names
    assert ("no", "1984") in names
    assert ("null", "1984") in names
    assert ("yes", "Yes") in names
    assert ("~x", "Yes") not in names  # '~' is punctuation: the key is "x"
    assert ("x", "Yes") in names


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("people: [oops\n", "is not valid YAML at line 2", id="bad-yaml"),
        pytest.param(
            "people:\n  A: &x [B]\n  C: *x\n",
            "uses a YAML alias (*name) at line 3; aliases are not supported",
            id="alias",
        ),
        pytest.param(
            "people: {}\n---\ntopics: {}\n", "holds more than one document", id="two-docs"
        ),
        pytest.param("- people\n", "must be a mapping of kinds", id="list-root"),
        pytest.param("just text\n", "must be a mapping of kinds", id="scalar-root"),
        pytest.param("people: [" * 5000 + "]" * 5000, "nests too deeply", id="deep"),
    ],
)
def test_whole_file_errors(text: str, message: str) -> None:
    with pytest.raises(AliasFileError) as excinfo:
        parse_aliases(text)
    assert message in excinfo.value.message
    assert excinfo.value.message.startswith(ALIASES_FILE)


def _root(tmp_path: Path) -> tuple[Vault, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    return Vault(vault, "Earmark"), vault / "Earmark"


def test_read_missing_is_none(tmp_path: Path) -> None:
    v, _ = _root(tmp_path)
    with v.open_subdir() as root:
        assert read_aliases(root) is None


def test_read_file_errors(tmp_path: Path) -> None:
    v, sub = _root(tmp_path)
    with v.open_subdir() as root:
        (sub / ALIASES_FILE).write_bytes(b"x" * (MAX_ALIASES_BYTES + 1))
        with pytest.raises(AliasFileError, match="larger than 256 KiB"):
            read_aliases(root)
        (sub / ALIASES_FILE).write_bytes(b"people:\n  \xff\xfe: x\n")
        with pytest.raises(AliasFileError, match="not UTF-8 text"):
            read_aliases(root)


def test_bom_allowed(tmp_path: Path) -> None:
    v, sub = _root(tmp_path)
    with v.open_subdir() as root:
        (sub / ALIASES_FILE).write_bytes(b"\xef\xbb\xbfpeople:\n  Ada: [A]\n")
        parsed = read_aliases(root)
    assert parsed is not None
    assert [e.alias_key for e in parsed.entries] == ["ada", "a"]


def test_symlinked_file_refused(tmp_path: Path) -> None:
    v, sub = _root(tmp_path)
    with v.open_subdir() as root:
        target = tmp_path / "fifo"
        os.mkfifo(target)
        (sub / ALIASES_FILE).symlink_to(target)
        with pytest.raises(AliasFileError, match="symlink or not a regular file"):
            read_aliases(root)  # would hang if it opened the FIFO
        (sub / ALIASES_FILE).unlink()
        os.mkfifo(sub / ALIASES_FILE)
        with pytest.raises(AliasFileError, match="symlink or not a regular file"):
            read_aliases(root)


def test_entry_problems() -> None:
    text = """\
foods:
  X: [Y]
people: [not, a, mapping]
topics:
  ? [a, list]
  : value
  "???": [Q]
  Ada: [Ada, Bob]
  Bob: [Carl]
  Carl: [Bob, Dan]
  Eve: [{a: b}]
  Frank: {a: b}
  Gus: [Hal]
  Zed: [Ok]
"""
    parsed = parse_aliases(text)
    assert [p.split(":")[0] for p in parsed.problems[:5]] == [
        f"{ALIASES_FILE} line {n}" for n in (1, 3, 5, 7, 9)
    ]
    assert "unknown kind" in parsed.problems[0]
    assert "must be a mapping" in parsed.problems[1]
    assert "not a list or mapping" in parsed.problems[2]
    assert "no letters or digits" in parsed.problems[3]
    assert "already listed at line 8" in parsed.problems[4]
    assert parsed.problems[-1] == f"{ALIASES_FILE}: and 3 more problems"
    keys = {(e.alias_key, e.canonical_key) for e in parsed.entries}
    assert ("bob", "ada") in keys  # first listing wins
    assert ("bob", "bob") not in keys  # the chain entry is skipped
    assert ("carl", "bob") not in keys
    assert ("ok", "zed") in keys
    assert ("hal", "gus") in keys
    assert ("q", "???") not in keys


def test_entry_problems_one_by_one() -> None:
    cases = {
        "foods: {}\n": "line 1: unknown kind foods; use people, papers, companies, tools, topics",
        "people: [a]\n": "line 1: people must be a mapping",
        "people:\n  ? [a]\n  : b\n": "line 2: a name must be text",
        "people:\n  '???': [A]\n": "line 2: '???' has no letters or digits",
        "people:\n  A: [B]\n  B: [C]\n": "line 3: B is already listed at line 2",
        "people:\n  A: [B]\n  C: [B]\n": "line 3: B is already listed at line 2",
        "people:\n  A: [[x]]\n": "line 2: an alias must be text",
        "people:\n  A: {k: v}\n": "line 2: aliases must be a name or a list of names",
    }
    for text, expected in cases.items():
        parsed = parse_aliases(text)
        assert len(parsed.problems) == 1, text
        assert expected in parsed.problems[0], text


def test_problem_cap() -> None:
    text = (
        "foods:\n"
        + "".join(f"  k{i}: x\n" for i in range(1))
        + "".join(f"kind{i}: {{}}\n" for i in range(7))
    )
    parsed = parse_aliases(text)
    assert len(parsed.problems) == 6
    assert parsed.problems[-1] == f"{ALIASES_FILE}: and 3 more problems"


def test_tags_ignored() -> None:
    parsed = parse_aliases(
        "topics:\n  !!python/object:os.system X: [!!python/object:os.system Y]\n"
    )
    assert [(e.alias_key, e.canonical_name) for e in parsed.entries] == [("x", "X"), ("y", "X")]


def test_names_are_collapsed_and_scrubbed() -> None:
    parsed = parse_aliases('people:\n  "Ada   \\ud800  Lovelace": [Ada]\n')
    assert parsed.entries[0].canonical_name == "Ada � Lovelace"


def test_duplicate_kind_keys_and_alias_equal_to_canonical() -> None:
    parsed = parse_aliases("people:\n  A: [a., A]\npeople:\n  B: [C]\n")
    assert [(e.alias_key, e.canonical_key) for e in parsed.entries] == [
        ("a", "a"),
        ("b", "b"),
        ("c", "b"),
    ]
    assert parsed.problems == ()


def test_long_text_in_problem_is_capped() -> None:
    parsed = parse_aliases(
        "people:\n  A: [B]\n  C: [" + "B" + "x" * 200 + ", " + "B" + "x" * 200 + "]\n"
    )
    assert "…" in parsed.problems[0]
    assert len(parsed.problems[0]) < 200


def test_yaml_stays_lazy() -> None:
    code = (
        "import sys, earmark.vault.notes, earmark.vault.entities, earmark.vault.aliases\n"
        "print('yaml' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
