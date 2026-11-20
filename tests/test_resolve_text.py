import pytest

from earmark.resolve.text import clean_text, html_to_text, normalize_title


@pytest.mark.parametrize(
    ("value", "limit", "keep", "expected"),
    [
        (None, 100, False, ""),
        ("a\x00b\x1b[31mc", 100, False, "a b [31mc"),
        ("  a \t b\n\n c  ", 100, False, "a b c"),
        ("a\r\nb\rc", 100, True, "a\nb\nc"),
        ("a \t b\n\n\n\n\nc  \n d", 100, True, "a b\n\nc\nd"),
        ("a\x07b\nc", 100, True, "a b\nc"),
        ("abcdef", 4, False, "abc\u2026"),
        ("abcd", 4, False, "abcd"),
    ],
)
def test_clean_text(value: str | None, limit: int, keep: bool, expected: str) -> None:
    assert clean_text(value, limit=limit, keep_newlines=keep) == expected


def test_html_to_text() -> None:
    text = html_to_text("<p>a &amp; b</p><p>b<br>c</p><script>x</script><style>y</style>d")
    assert text == "a & b\n\nb\nc\n\nd"
    assert "x" not in text.replace("\n", "")
    assert html_to_text(None) == ""


def test_normalize_title_quotes_and_punctuation() -> None:
    curly = "\u2018Did A.I. Break the Internet?\u2019"
    assert normalize_title(curly) == normalize_title("'Did A.I. Break the Internet?'")
    assert normalize_title(curly) == "did ai break the internet"


def test_normalize_title_entities_case_width() -> None:
    assert normalize_title("A &amp; B") == normalize_title("A & B")
    assert normalize_title("\uff21\uff22\uff23  Def") == "abc def"
    assert normalize_title(None) == ""


def test_normalize_title_keeps_distinguishing_words() -> None:
    assert normalize_title("Something Rotten | 2") != normalize_title("Something Rotten")
