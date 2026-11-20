import pytest

from tests.doc_helpers import (
    after_marker,
    between_markers,
    fences,
    first_fence,
    relative_links,
    table_variables,
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
