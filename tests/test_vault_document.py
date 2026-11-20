from datetime import UTC, date, datetime

import pytest

from earmark.vault.document import (
    DamagedNote,
    NoteText,
    begin_marker,
    emit_frontmatter,
    new_note,
    note_identity,
    region_digest,
    split_note,
    update_note,
)

KEY = "0123456789abcdef0123"
OWNED = [("earmark", "episode"), ("episode_key", KEY), ("title", "T")]
SEEDS = [("tags", ("earmark", "podcast"))]
REGION = "# Title\n\nbody\n"


def test_emit_frontmatter_shapes() -> None:
    lines = emit_frontmatter(
        [
            ("a", 'x: "y"'),
            ("b", None),
            ("c", date(2026, 1, 2)),
            ("d", datetime(2026, 1, 2, 3, 4, 5, 999)),  # noqa: DTZ001  # naive by design
            ("e", ()),
            ("f", ("p", "q")),
        ]
    )
    assert lines == [
        'a: "x: \\"y\\""\n',
        "b: null\n",
        "c: 2026-01-02\n",
        "d: 2026-01-02T03:04:05\n",
        "e: []\n",
        "f:\n",
        '  - "p"\n',
        '  - "q"\n',
    ]
    with pytest.raises(ValueError, match="naive"):
        emit_frontmatter([("a", datetime(2026, 1, 1, tzinfo=UTC))])
    for bad in ("A", "1a", "a-b", "", "a b"):
        with pytest.raises(ValueError, match="key"):
            emit_frontmatter([(bad, "x")])


def test_split_note() -> None:
    assert split_note("no frontmatter\n") == NoteText("", None, ("no frontmatter\n",))
    assert split_note("---\na: 1\nno close\n").frontmatter is None
    bom = split_note("\ufeff---\na: 1\n---\nbody")
    assert bom == NoteText("\ufeff", ("a: 1\n",), ("body",))
    crlf = split_note("---\r\na: 1\r\n---  \r\nbody\r\n")
    assert crlf == NoteText("", ("a: 1\r\n",), ("body\r\n",))
    odd = split_note("---\na\x0bb\n---\nx\u2028y\n")
    assert odd.frontmatter == ("a\x0bb\n",)
    assert odd.body == ("x\u2028y\n",)
    assert split_note("") == NoteText("", None, ())


def test_note_identity() -> None:
    def note(front: str) -> str:
        return f"---\n{front}---\nbody\n"

    assert note_identity(note(f'earmark: "episode"\nepisode_key: "{KEY}"\n')) == ("episode", KEY)
    assert note_identity(note(f"earmark: 'transcript'\nepisode_key: '{KEY}'\n")) == (
        "transcript",
        KEY,
    )
    assert note_identity(note(f"earmark: episode\nepisode_key: {KEY}\n")) == ("episode", KEY)
    assert note_identity(note(f"earmark: other\nepisode_key: {KEY}\n")) is None
    assert note_identity(note("earmark: episode\nepisode_key: nothex\n")) is None
    assert note_identity(note("earmark: episode\n")) is None
    assert note_identity(f"earmark: episode\nepisode_key: {KEY}\n") is None
    assert note_identity(note(f"  earmark: episode\n  episode_key: {KEY}\n")) is None
    outside = f'---\nx: 1\n---\nearmark: episode\nepisode_key: "{KEY}"\n'
    assert note_identity(outside) is None


def test_region_digest_crlf_insensitive() -> None:
    assert region_digest("a\r\nb\r\n") == region_digest("a\nb\n")
    assert region_digest("a\n") != region_digest("b\n")
    assert len(region_digest("")) == 16


def test_new_note_and_region_checks() -> None:
    text = new_note(OWNED, SEEDS, REGION)
    assert text.startswith('---\nearmark: "episode"\n')
    assert text.endswith(f"{begin_marker(region_digest(REGION))}\n{REGION}<!-- earmark:end -->\n")
    with pytest.raises(ValueError, match="newline"):
        new_note(OWNED, SEEDS, "no newline")
    with pytest.raises(ValueError, match="marker"):
        new_note(OWNED, SEEDS, "<!-- earmark:end -->\n")


def test_update_note_keeps_outside_lines() -> None:
    text = new_note(OWNED, SEEDS, REGION)
    edited = text.replace("---\n<!--", "---\nmy intro\n\n<!--", 1) + "my outro\r\n"
    edited = edited.replace("tags:", "rating: 5\n# a comment\ntags:", 1)
    out = update_note(edited, OWNED, SEEDS, "# New\n")
    assert not out.edited
    assert "my intro\n\n" in out.text
    assert out.text.endswith("<!-- earmark:end -->\nmy outro\r\n")
    assert "rating: 5\n# a comment\ntags:" in out.text
    assert "# New\n" in out.text
    assert "body" not in out.text
    again = update_note(out.text, OWNED, SEEDS, "# New\n")
    assert again.text == out.text


def test_update_note_frontmatter_rules() -> None:
    existing = (
        "---\n"
        'title: "mine"\n'
        "title: second\n"
        "- stray\n"
        "earmark: episode\n"
        f"episode_key: {KEY}\n"
        "tags: [mine]\n"
        "custom:\n"
        "  - keep\n"
        "---\n" + new_note([], [], REGION).split("---\n", 2)[2]
    )
    out = update_note(existing, OWNED, SEEDS, REGION).text
    front = split_note(out).frontmatter
    assert front is not None
    assert "".join(front) == (
        'earmark: "episode"\nepisode_key: "' + KEY + '"\ntitle: "T"\n'
        "tags: [mine]\ncustom:\n  - keep\n"
    )


def test_update_note_edit_detection() -> None:
    text = new_note(OWNED, SEEDS, REGION)
    hand = text.replace("body", "my edit")
    out = update_note(hand, OWNED, SEEDS, "# Other\n")
    assert out.edited
    assert out.old_digest == region_digest("# Title\n\nmy edit\n")
    same = update_note(hand, OWNED, SEEDS, "# Title\n\nmy edit\n")
    assert not same.edited
    bare = text.replace(begin_marker(region_digest(REGION)), "<!-- earmark:begin -->")
    assert update_note(bare, OWNED, SEEDS, "# Other\n").edited
    assert not update_note(bare, OWNED, SEEDS, REGION).edited


def test_marker_lines_with_trailing_whitespace() -> None:
    text = new_note(OWNED, SEEDS, REGION)
    spaced = text.replace("-->\n", "-->  \t\r\n")
    out = update_note(spaced, OWNED, SEEDS, REGION)
    assert not out.edited
    assert out.text == text


END = "<!-- earmark:end -->\n"
BEGIN = begin_marker(region_digest(REGION)) + "\n"
FRONT = "---\nearmark: episode\n---\n"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(BEGIN + REGION, "damaged", id="only-begin"),
        pytest.param(REGION + END, "damaged", id="only-end"),
        pytest.param(BEGIN + REGION + END + END, "damaged", id="two-ends"),
        pytest.param(BEGIN + BEGIN + REGION + END, "damaged", id="two-begins"),
        pytest.param(END + BEGIN + REGION, "damaged", id="end-before-begin"),
        pytest.param(REGION, "missing", id="none"),
    ],
)
def test_damaged_markers(body: str, reason: str) -> None:
    with pytest.raises(DamagedNote) as info:
        update_note(FRONT + body, OWNED, SEEDS, REGION)
    assert info.value.reason == reason


def test_markers_in_frontmatter_do_not_count() -> None:
    text = "---\nearmark: episode\n" + BEGIN + END + "---\nno markers\n"
    with pytest.raises(DamagedNote):
        update_note(text, OWNED, SEEDS, REGION)


def test_update_note_without_frontmatter() -> None:
    body = f"{begin_marker(region_digest(REGION))}\n{REGION}<!-- earmark:end -->\n"
    out = update_note("intro\n" + body, OWNED, SEEDS, REGION)
    assert out.text.startswith("---\nearmark")
    assert out.text.endswith("---\nintro\n" + body)
