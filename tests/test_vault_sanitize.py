import random
import unicodedata

import pytest
import yaml

from earmark.vault.sanitize import (
    ELLIPSIS,
    MAX_NAME_BYTES,
    escape_markdown,
    file_component,
    fit_bytes,
    fold_key,
    is_safe_component,
    wikilink_target_ok,
    yaml_quote,
)


@pytest.mark.parametrize(
    ("text", "max_bytes", "expected"),
    [
        ("../../x", 100, "-..-x"),
        (".hidden", 100, "hidden"),
        ("..", 100, "FB"),
        (".", 100, "FB"),
        ("a/b\\c", 100, "a-b-c"),
        ("Ep. 1: Intro", 100, "Ep. 1 - Intro"),
        ("12:30", 100, "12-30"),
        ("[Live] #42 ^x", 100, "(Live) 42 x"),
        ('say "hi"', 100, "say 'hi'"),
        ("What?*<>", 100, "What"),
        ("100%% sure", 100, "100% sure"),
        ("CON", 100, "CON_"),
        ("con.txt", 100, "con_.txt"),
        ("a\x00b\u202ec", 100, "a bc"),
        ("Cafe\u0301", 100, "Café"),
        ("trailing. ", 100, "trailing"),
        ("", 100, "FB"),
        ("  \t\n ", 100, "FB"),
        ("a\ud800b", 100, "ab"),
        ("a|b", 100, "a-b"),
        ("%#%", 100, "%"),
    ],
)
def test_file_component_cases(text: str, max_bytes: int, expected: str) -> None:
    got = file_component(text, max_bytes=max_bytes, fallback="FB")
    assert got == expected
    assert is_safe_component(got)


UNICODE_NAMES = [
    "日本語のタイトル",
    "👨\u200d👩\u200d👧 family",
    "عنوان عربي",  # deliberate non-Latin test text
    "می\u200cخواهم",  # noqa: RUF001
    "☕ Café",
]


@pytest.mark.parametrize("text", UNICODE_NAMES)
def test_file_component_keeps_unicode(text: str) -> None:
    assert file_component(text, max_bytes=100, fallback="FB") == text


def test_file_component_byte_cap() -> None:
    rng = random.Random(7)  # noqa: S311  # a fixed seed for test inputs
    alphabet = "abc é日👨\u200d🇫🇷\u0301./:#% ."
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 300)))
        for cap in (8, 20, 100, MAX_NAME_BYTES):
            got = file_component(text, max_bytes=cap, fallback="FB")
            assert len(got.encode()) <= cap
            assert is_safe_component(got)
            assert unicodedata.is_normalized("NFC", got)
    long = file_component("é" * 1000, max_bytes=120, fallback="FB")
    assert long.endswith(ELLIPSIS)
    assert len(long.encode()) <= 120


def test_fallback_must_be_safe() -> None:
    with pytest.raises(ValueError, match="fallback"):
        file_component("x", max_bytes=50, fallback="a/b")


@pytest.mark.parametrize(
    "text",
    [
        "x" * 20 + "e\u0301" + "y" * 5,
        "x" * 20 + "\U0001f468\u200d\U0001f469\u200d\U0001f467" + "y" * 5,
        "x" * 20 + "\U0001f1eb\U0001f1f7\U0001f1e9\U0001f1ea" + "y" * 5,
        "x" * 20 + "\U0001f44d\U0001f3fd" + "y" * 5,
    ],
)
def test_cut_never_splits_graphemes(text: str) -> None:
    full = len(text.encode())
    for cap in range(24, full):
        out = fit_bytes(text, cap)
        assert len(out.encode()) <= cap
        body = out.removesuffix(ELLIPSIS)
        last = body[-1]
        assert unicodedata.category(last) not in {"Mn", "Mc", "Me"}
        assert last != "\u200d"
        assert not 0x1F3FB <= ord(last) <= 0x1F3FF
        tail = 0
        while tail < len(body) and 0x1F1E6 <= ord(body[-1 - tail]) <= 0x1F1FF:
            tail += 1
        assert tail % 2 == 0


def test_fit_bytes_edges() -> None:
    assert fit_bytes("short", 8) == "short"
    with pytest.raises(ValueError, match="at least 8"):
        fit_bytes("x", 7)
    assert fit_bytes("abcdefghij. . .k", 12) == "abcdefghi" + ELLIPSIS


def test_fold_key() -> None:
    assert fold_key("AI") == fold_key("ai")
    assert fold_key("Café") == fold_key("Cafe\u0301")
    assert fold_key("ﬁ") == fold_key("fi")
    assert fold_key("a") != fold_key("b")


def test_is_safe_component() -> None:
    assert is_safe_component("Épisode 1 - «Tests».md")
    assert is_safe_component("a\u200db")
    for bad in [
        "",
        ".hidden",
        " lead",
        "trail.",
        "trail ",
        "a/b",
        "a\\b",
        "a\x00b",
        "a\u202eb",
        "a:b",
        "a*b",
        "a#b",
        "a^b",
        "a[b",
        "a]b",
        'a"b',
        "a<b",
        "a|b",
        "a%%b",
        "e\u0301",
        "x" * (MAX_NAME_BYTES + 1),
        "a\ud800b",
    ]:
        assert not is_safe_component(bad), repr(bad)


def test_escape_markdown() -> None:
    assert escape_markdown("$5 and $10 #ad [[x]] <b>") == r"\$5 and \$10 \#ad \[\[x\]\] &lt;b>"
    assert escape_markdown("a\n\n  b\tc") == "a b c"
    assert escape_markdown(r"a\b `c` *d* _e_ |f| ~g~ =h= %i ^j") == (
        r"a\\b \`c\` \*d\* \_e\_ \|f\| \~g\~ \=h\= \%i \^j"
    )
    assert escape_markdown("   ") == ""


@pytest.mark.parametrize(
    ("target", "ok"),
    [
        ("Earmark/Podcasts/Show/Note", True),
        ("", False),
        ("a#b", False),
        ("a|b", False),
        ("a^b", False),
        ("a:b", False),
        ("a[b", False),
        ("a]b", False),
        ("a%%b", False),
        ("a\nb", False),
        ("a\rb", False),
    ],
)
def test_wikilink_target_ok(target: str, ok: bool) -> None:
    assert wikilink_target_ok(target) is ok


YAML_CASES = [
    "plain",
    "a: b",
    'say "hi"',
    "---",
    "a\n---\nb: c",
    "# comment",
    "- item",
    "\x85",
    "\u2028",
    "\u2029",
    "\ufeff",
    "\U0001f468\u200d\U0001f469\u200d\U0001f467",
    "tab\there",
    "\x00",
    "\x1b",
    "\x7f",
    "\x9f",
    "yes",
    "null",
    "~",
    "2026-01-01",
    "123",
    "!tag",
    "&anchor *alias",
    "{flow: x}",
    "[a, b]",
    "%YAML 1.2",
    "`tick`",
    "\u00a0",
    "\U000e0001",
    "back\\slash",
    "￾￿",
    "",
]


@pytest.mark.parametrize("text", YAML_CASES)
def test_yaml_quote_round_trip(text: str) -> None:
    quoted = yaml_quote(text)
    assert "\n" not in quoted
    assert quoted.startswith('"')
    doc = f"k: {quoted}\n"
    assert yaml.safe_load(doc) == {"k": text}
    assert yaml.load(doc, Loader=yaml.CSafeLoader) == {"k": text}  # safe loader


def test_yaml_quote_lone_surrogate_and_escapes() -> None:
    assert yaml.safe_load("k: " + yaml_quote("a\ud800b")) == {"k": "a�b"}
    assert yaml_quote("\x00\x85") == '"\\u0000\\u0085"'
    assert yaml_quote('a"b\\') == '"a\\"b\\\\"'
