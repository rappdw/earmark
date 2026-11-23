import pytest

from tests.doc_helpers import (
    after_marker,
    anchored_links,
    between_markers,
    fences,
    first_fence,
    heading_anchors,
    relative_links,
    section,
    table_rows,
    table_variables,
    without_blocks,
)


def test_between_markers() -> None:
    text = "a\n<!-- x:begin -->\nmiddle\n<!-- x:end -->\nz"
    assert between_markers(text, "x").strip() == "middle"


@pytest.mark.parametrize(
    "text",
    [
        "no markers",
        "<!-- x:begin -->\nonly begin",
        "<!-- x:end -->\n<!-- x:begin -->",
        "<!-- x:begin --><!-- x:end --><!-- x:begin --><!-- x:end -->",
    ],
)
def test_between_markers_rejects_bad_markers(text: str) -> None:
    with pytest.raises(AssertionError):
        between_markers(text, "x")


def test_after_marker() -> None:
    assert after_marker("a\n<!-- m -->\nrest", "m").strip() == "rest"
    with pytest.raises(AssertionError):
        after_marker("<!-- m -->\n<!-- m -->", "m")
    with pytest.raises(AssertionError):
        after_marker("nothing", "m")


def test_fences_backtick_and_tilde() -> None:
    text = "```bash\nls\n```\n\n~~~json\n{}\n~~~\n"
    found = fences(text)
    assert [(f.info, f.body, f.line) for f in found] == [("bash", "ls", 1), ("json", "{}", 5)]


def test_fence_closes_only_at_a_long_enough_same_character_fence() -> None:
    text = "````md\n```bash\nls\n```\n~~~~\nstill inside\n````\nafter\n"
    [fence] = fences(text)
    assert fence.info == "md"
    assert fence.body == "```bash\nls\n```\n~~~~\nstill inside"


def test_indented_fence_in_list_item_is_dedented() -> None:
    text = '1. step\n\n   ```json\n   {\n     "a": 1\n   }\n   ```\n'
    [fence] = fences(text)
    assert fence.body == '{\n  "a": 1\n}'


def test_first_fence() -> None:
    text = "```json\n{}\n```\n```bash\nls\n```\n"
    assert first_fence(text, "json").body == "{}"
    with pytest.raises(AssertionError):
        first_fence(text, "bash")
    with pytest.raises(AssertionError):
        first_fence("no fence", "json")


def test_table_variables() -> None:
    text = (
        "| Variable | Default | Meaning |\n"
        "|---|---|---|\n"
        "| `EARMARK_A` | `1` | first |\n"
        "| `EARMARK_B` | unset | second, see `EARMARK_A` |\n"
        "| not a variable | x | y |\n"
    )
    assert table_variables(text) == ["EARMARK_A", "EARMARK_B"]


@pytest.mark.parametrize(
    "row",
    [
        "| `EARMARK_A`, `EARMARK_B` | 1 | two |",
        "| `EARMARK_A` and more | 1 | extra text |",
        "| `EARMARK_A` | | no default |",
    ],
)
def test_table_variables_rejects_bad_rows(row: str) -> None:
    with pytest.raises(AssertionError):
        table_variables(row)


def test_table_rows_inside_fences_are_ignored() -> None:
    text = "```\n| `EARMARK_A`, `EARMARK_B` | 1 | x |\n```\n| `EARMARK_C` | 1 | y |\n"
    assert table_variables(text) == ["EARMARK_C"]


def test_relative_links() -> None:
    text = (
        "[a](docs/a.md) [b](docs/b.md#part) [c](#anchor) [d](https://example.com/x) "
        "[e](mailto:me@example.com) ![img](pics/p.png)\n"
        "```\n[ignored](nowhere.md)\n```\n"
    )
    assert relative_links(text) == ["docs/a.md", "docs/b.md", "pics/p.png"]


def test_table_rows() -> None:
    text = (
        "intro\n"
        "| Status | Message |\n"
        "|:---|---:|\n"
        "| 401 | `missing` |\n"
        "| 422 | `again` |\n"
        "text\n"
        "```\n| in | fence |\n```\n"
        "| H2 | H3 |\n"
        "| --- | --- |\n"
        "| x | y |\n"
    )
    assert table_rows(text) == [
        ["401", "`missing`"],
        ["422", "`again`"],
        ["x", "y"],
    ]


def test_heading_anchors() -> None:
    text = (
        "# Top\n"
        "### 7.3 The Spark smoke test\n"
        "## 14. Upgrading from v0.1.0\n"
        "## Phase 2: Get the code and write .env\n"
        "#### 6.1.6 DNS rebinding protection (a gotcha)\n"
        "## Again\n"
        "## Again\n"
        "## Again\n"
        "```bash\n# not a heading\n```\n"
    )
    assert heading_anchors(text) == {
        "top",
        "73-the-spark-smoke-test",
        "14-upgrading-from-v010",
        "phase-2-get-the-code-and-write-env",
        "616-dns-rebinding-protection-a-gotcha",
        "again",
        "again-1",
        "again-2",
    }


def test_anchored_links() -> None:
    text = "[a](x.md#b) [c](#d) [e](https://h/#f) [g](plain.md)\n```\n[h](y.md#i)\n```\n"
    assert anchored_links(text) == [("x.md", "b"), ("", "d")]


def test_without_blocks() -> None:
    one = "a\n<!-- x:begin -->\nmiddle\n<!-- x:end -->\nz"
    assert without_blocks(one, "x") == "a\n\nz"
    two = "1<!-- x:begin -->2<!-- x:end -->3<!-- x:begin -->4<!-- x:end -->5"
    assert without_blocks(two, "x") == "135"
    assert without_blocks("none", "x") == "none"
    assert without_blocks(one, "y") == one


@pytest.mark.parametrize(
    "text",
    [
        "<!-- x:begin -->only",
        "only<!-- x:end -->",
        "<!-- x:end --><!-- x:begin -->",
        "<!-- x:begin --><!-- x:begin --><!-- x:end --><!-- x:end -->",
    ],
)
def test_without_blocks_rejects_bad_markers(text: str) -> None:
    with pytest.raises(AssertionError):
        without_blocks(text, "x")


def test_section() -> None:
    text = (
        "## 7. A\nseven\n### 7.1 Deeper\ndeep\n```\n## 8. in a fence\n```\n"
        "## 8. B\neight\n# Top\ntop\n"
    )
    assert section(text, "## 7. ") == (
        "## 7. A\nseven\n### 7.1 Deeper\ndeep\n```\n## 8. in a fence\n```"
    )
    assert section(text, "## 8. ") == "## 8. B\neight"
    assert section(text, "### 7.1") == "### 7.1 Deeper\ndeep\n```\n## 8. in a fence\n```"
    with pytest.raises(AssertionError):
        section(text, "## 9. ")
    with pytest.raises(AssertionError):
        section(text + "## 8. again\n", "## 8. ")
