import pytest

from tests.doc_helpers import (
    LayoutEntry,
    after_marker,
    anchored_links,
    between_markers,
    fences,
    first_fence,
    heading_anchors,
    layout_entries,
    paragraphs,
    plan_status,
    read_doc,
    relative_links,
    section,
    step_numbers,
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


LAYOUT = """\
.claude-plugin/marketplace.json   the marketplace "earmark", listing ./plugins/earmark-pka (step 30)
plugins/earmark-pka/              part 3: the plugin (step 30)
  .claude-plugin/plugin.json
  skills/earmark-receive/SKILL.md
  skills/earmark-receive/scripts/   the contract checker, its only copy (step 29)
contract/                         the mail seam only (step 29)
  README.md                         what the seam is
  samples/v1/                       sample emails
src/earmark/                      part 2: the service
deploy/, Dockerfile, Dockerfile.gpu, compose.yaml   part 2: deployment
"""


def test_layout_entries_children_and_steps() -> None:
    entries = layout_entries(LAYOUT)
    assert LayoutEntry("plugins/earmark-pka/.claude-plugin/plugin.json", "", "30") in entries
    assert (
        LayoutEntry(
            "plugins/earmark-pka/skills/earmark-receive/scripts/",
            "the contract checker, its only copy (step 29)",
            "29",
        )
        in entries
    )
    assert LayoutEntry("contract/samples/v1/", "sample emails", "29") in entries
    assert LayoutEntry("src/earmark/", "part 2: the service", None) in entries


def test_layout_entries_multi_path_line() -> None:
    line = "deploy/, Dockerfile, Dockerfile.gpu, compose.yaml   part 2: deployment"
    entries = layout_entries(line)
    assert [e.path for e in entries] == ["deploy/", "Dockerfile", "Dockerfile.gpu", "compose.yaml"]
    assert {e.description for e in entries} == {"part 2: deployment"}
    assert {e.step for e in entries} == {None}


def test_layout_entries_rejects_bad_nesting() -> None:
    with pytest.raises(AssertionError):
        layout_entries("deploy/, Dockerfile   x\n  child   y")
    with pytest.raises(AssertionError):
        layout_entries("file.txt   x\n  child   y")
    with pytest.raises(AssertionError):
        layout_entries("dir/   x\n   child   y")


def test_plan_status() -> None:
    sample = (
        "# Plan\n\n## Status\n\n"
        "| Step | Title | Depends on | Status |\n|---|---|---|---|\n"
        "| 01 | A | - | done |\n| 01a | B | 01 | todo |\n\n## Next\n"
    )
    assert plan_status(sample) == {"01": "done", "01a": "todo"}
    real = plan_status(read_doc("docs/PLAN.md"))
    assert real["27"] == "done"
    assert {"28", "29", "30", "31"} <= set(real)
    with pytest.raises(AssertionError):
        plan_status("## Status\n\nno table\n")


def test_step_numbers() -> None:
    text = "steps 29, 30 and 31; step 28; steps 22\u201324; Step 9; step 01a"
    assert step_numbers(text) == {"09", "01a", "22", "23", "24", "28", "29", "30", "31"}
    assert step_numbers("no steps here") == set()


def test_paragraphs() -> None:
    text = "one\ntwo\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```text\nhidden\n\nalso\n```\n\nlast"
    assert paragraphs(text) == ["one\ntwo", "| a | b |\n|---|---|\n| 1 | 2 |", "last"]
