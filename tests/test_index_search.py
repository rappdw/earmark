import subprocess
import sys
from datetime import UTC, datetime

import pytest

from earmark.errors import EarmarkError, PermanentError
from earmark.fakes import FakeEmbedder, FakeSearcher
from earmark.index.embed import Embedder
from earmark.index.search import (
    MAX_QUERY_CHARS,
    ROW_KINDS,
    IndexMismatchError,
    InvalidQuery,
    SearchHit,
    SearchQuery,
    SearchResults,
    SearchUnavailableError,
    UnavailableSearcher,
    parse_query,
    snippet,
)

KEY = "a" * 20


def make_hit(rank: int = 1, **overrides: object) -> SearchHit:
    values: dict[str, object] = {
        "rank": rank,
        "score": 0.8312,
        "episode_key": KEY,
        "show_title": "Fake Show",
        "episode_title": "Fake episode",
        "published_at": datetime(2026, 1, 1, tzinfo=UTC),
        "episode_url": "https://podcasts.apple.com/us/podcast/id1?i=2",
        "kind": "claim",
        "prompt_hash": "b" * 16,
        "prompt": "why?",
        "start_s": 65.0,
        "end_s": None,
        "timestamp": "00:01:05",
        "snippet": "The fake episode makes a claim.",
        "note_path": "Earmark/Podcasts/x.md",
        "transcript_note_path": None,
    }
    values.update(overrides)
    return SearchHit.model_validate(values)


def test_parse_query_normalises() -> None:
    q = parse_query("  fake \n claim ", kinds=["summary", "claim", "summary"], episode_key=KEY)
    assert q == SearchQuery("fake claim", 10, ("summary", "claim"), KEY)
    assert parse_query("x", kinds=["answer", "transcript"]).kinds == ("transcript", "answer")


@pytest.mark.parametrize(
    ("text", "limit", "kinds", "episode_key", "message"),
    [
        ("   \n", 10, (), None, "q must not be blank"),
        ("x" * (MAX_QUERY_CHARS + 1), 10, (), None, "q must be at most 500 characters"),
        ("x", 0, (), None, "limit must be between 1 and 50"),
        ("x", 51, (), None, "limit must be between 1 and 50"),
        (
            "x",
            10,
            ("entity",),
            None,
            "kind must be one of: transcript, summary, claim, lead, question, answer",
        ),
        ("x", 10, (), "XYZ", "episode must be an episode key (20 hexadecimal characters)"),
    ],
)
def test_parse_query_messages(
    text: str, limit: int, kinds: tuple[str, ...], episode_key: str | None, message: str
) -> None:
    with pytest.raises(InvalidQuery) as info:
        parse_query(text, limit=limit, kinds=kinds, episode_key=episode_key)
    assert str(info.value) == message


def test_parse_query_rejects_bad_kind_and_key() -> None:
    for bad_kind in ("claim' OR 1=1 --", "CLAIM", ""):
        with pytest.raises(InvalidQuery):
            parse_query("x", kinds=[bad_kind])
    for bad_key in (f"{KEY}' OR 'a'='a", "A" * 20, "a" * 19, ""):
        with pytest.raises(InvalidQuery):
            parse_query("x", episode_key=bad_key)


def test_parse_query_error_never_contains_the_query() -> None:
    with pytest.raises(InvalidQuery) as info:
        parse_query("sentinel-secret " * 100)
    assert "sentinel" not in str(info.value)


def test_row_kinds_match_literal() -> None:
    assert ROW_KINDS == ("transcript", "summary", "claim", "lead", "question", "answer")


def test_snippet_short_and_collapsed() -> None:
    assert snippet("a  b\n c") == "a b c"


def test_snippet_cuts_at_last_space() -> None:
    text = " ".join(["word"] * 100)
    out = snippet(text)
    assert out.endswith("…")
    body = out[:-1]
    assert len(body) <= 299
    assert text.startswith(body)
    assert not body.endswith(" ")
    assert text[len(body)] == " "


def test_snippet_without_spaces_cuts_at_299() -> None:
    out = snippet("x" * 1000)
    assert out == "x" * 299 + "…"
    assert snippet("x" * 300) == "x" * 300


def test_hit_round_trip() -> None:
    results = SearchResults(hits=(make_hit(1), make_hit(2, kind="summary", start_s=None)))
    again = SearchResults.model_validate_json(results.model_dump_json())
    assert again == results


def test_fake_searcher_limits_and_records() -> None:
    hits = [make_hit(i) for i in range(1, 6)]
    searcher = FakeSearcher(hits)
    query = parse_query("x", limit=2)
    assert searcher.search(query).hits == tuple(hits[:2])
    assert searcher.calls == [query]
    assert FakeSearcher(index_exists=False).search(query).index_exists is False


def test_fake_searcher_fail_next() -> None:
    searcher = FakeSearcher()
    searcher.fail_next(IndexMismatchError("nope"))
    with pytest.raises(IndexMismatchError):
        searcher.search(parse_query("x"))


def test_unavailable_searcher_raises_reason() -> None:
    with pytest.raises(SearchUnavailableError, match="because") as info:
        UnavailableSearcher("because").search(parse_query("x"))
    assert isinstance(info.value, EarmarkError)


def test_mismatch_is_permanent() -> None:
    assert issubclass(IndexMismatchError, PermanentError)


def test_fake_embedder_deterministic() -> None:
    a, b = FakeEmbedder(), FakeEmbedder()
    v = a.embed_query("the telephone rang")
    assert v == b.embed_query("the telephone rang")
    assert sum(x * x for x in v) == pytest.approx(1.0)
    assert len(v) == a.dim == 64

    def dot(x: list[float], y: list[float]) -> float:
        return sum(p * q for p, q in zip(x, y, strict=True))

    close = a.embed_documents(["the telephone was invented"])[0]
    far = a.embed_documents(["photosynthesis in plants"])[0]
    assert dot(v, close) > dot(v, far)
    assert a.embed_documents([""])[0][0] == 1.0
    assert a.documents == ["the telephone was invented", "photosynthesis in plants", ""]
    assert a.queries == ["the telephone rang"]


def test_fake_embedder_fits_and_failures() -> None:
    e = FakeEmbedder(max_chars=5)
    assert e.fits("12345")
    assert not e.fits("123456")
    e.fail_next(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        e.embed_documents(["x"])
    assert e.embed_documents(["x"])
    embedder: Embedder = FakeEmbedder()  # structural conformance, checked by mypy
    assert embedder.model == "fake-embed-v1"


def test_index_modules_import_light() -> None:
    code = (
        "import sys\n"
        "import earmark.index.search, earmark.index.chunks, earmark.index.embed\n"
        "import earmark.index.lancedb_index, earmark.index.rebuild, earmark.fakes\n"
        "import earmark.api.routes\n"
        "heavy = {'lancedb', 'pyarrow', 'fastembed', 'onnxruntime', 'numpy', 'loguru'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"
