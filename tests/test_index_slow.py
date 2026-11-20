"""The real embedding model (downloaded once, ~66 MB) with real LanceDB. Opt-in: `pytest -m slow`
with the index extra."""

from pathlib import Path

import pytest

from earmark.config import Settings
from earmark.index.chunks import EpisodeDocs
from earmark.index.embed import (
    FastEmbedEmbedder,
    IndexExtraMissing,
    build_embedder,
    load_fastembed_runtime,
)
from earmark.index.lancedb_index import LanceIndex, load_lance_runtime
from earmark.index.search import parse_query
from earmark.models import StoreResult
from tests import slow_assets
from tests.index_helpers import BGE, make_analysis_of, make_docs, make_segments

pytestmark = pytest.mark.slow


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        embed_model=BGE,
        embed_model_dir=slow_assets.embed_model_dir(),
    )


@pytest.fixture(scope="module")
def embedder() -> FastEmbedEmbedder:
    try:
        load_fastembed_runtime()
        load_lance_runtime()
    except IndexExtraMissing as exc:
        pytest.fail(f"{exc} (the slow tests need the index extra)")
    built = build_embedder(
        Settings(_env_file=None, embed_model=BGE, embed_model_dir=slow_assets.embed_model_dir()),
        allow_download=True,
    )
    assert isinstance(built, FastEmbedEmbedder)
    return built


def test_real_model_ranks_semantically(embedder: FastEmbedEmbedder, tmp_path: Path) -> None:
    assert embedder.dim == 384
    assert embedder.model == BGE
    texts = {
        "telephone": "Alexander Graham Bell was granted the first patent for the telephone.",
        "plants": "Photosynthesis turns sunlight, water and carbon dioxide into sugar and oxygen.",
        "rome": "The Roman empire built roads, aqueducts and legions across the Mediterranean.",
    }
    episodes: list[EpisodeDocs] = []
    for i, (name, text) in enumerate(texts.items()):
        base = make_docs(guid=f"urn:slow:{i}", title=name)
        analysis = make_analysis_of(base.episode, summary=text, claims=[], leads=[], questions=[])
        episodes.append(
            make_docs(
                guid=f"urn:slow:{i}",
                title=name,
                segments=make_segments(1, prefix=name),
                analyses=[analysis],
            )
        )
    index = LanceIndex(tmp_path / "lancedb", embedder, lock_path=tmp_path / "index.lock")
    for docs in episodes:
        stored = StoreResult(note_path=f"Earmark/{docs.episode.title}.md")
        index.index(docs.episode, docs.transcript, docs.analyses, stored)
    hits = index.search(parse_query("who invented the telephone", kinds=["summary"])).hits
    assert hits[0].episode_title == "telephone"
    assert hits[0].score > hits[1].score
    assert [h.episode_title for h in hits][-1] != "telephone"
    other = index.search(parse_query("how do green leaves make food from light", kinds=["summary"]))
    assert other.hits[0].episode_title == "plants"


def test_real_model_fits_detects_overflow(embedder: FastEmbedEmbedder) -> None:
    assert embedder.fits("word " * 100) is True
    assert embedder.fits("word " * 600) is False


def test_real_model_loads_offline_from_cache(embedder: FastEmbedEmbedder, tmp_path: Path) -> None:
    embedder.embed_query("warm the cache")
    offline = build_embedder(_settings(tmp_path), allow_download=False)
    vectors = offline.embed_documents(["a short sentence", "another one"])
    assert len(vectors) == 2
    assert all(len(v) == 384 for v in vectors)
    assert sum(x * x for x in vectors[0]) == pytest.approx(1.0, abs=1e-3)
    # Building and loading never wrote under the data dir.
    assert not (tmp_path / "data").exists()
