"""Real LanceDB in a temp directory with a deterministic fake embedder (``-m index``).

These tests need the `index` extra and fail, not skip, without it: their fixtures call
``load_lance_runtime``. Sockets stay blocked.
"""

import dataclasses
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from earmark.api.app import create_app
from earmark.artifacts import ArtifactStore
from earmark.cli import app as cli_app
from earmark.config import Settings
from earmark.db import open_database
from earmark.errors import PermanentError, TransientError
from earmark.fakes import FakeEmbedder, make_fake_backends
from earmark.index.chunks import (
    EpisodeDocs,
    NotePaths,
    build_rows,
    load_episode_docs,
    rows_digest,
)
from earmark.index.lancedb_index import (
    META_DIM,
    META_MODEL,
    META_VERSION,
    TABLE,
    LanceIndex,
    LanceRuntime,
    LanceTable,
    ReplaceResult,
    build_lancedb_index,
    load_lance_runtime,
)
from earmark.index.rebuild import reindex
from earmark.index.search import IndexMismatchError, parse_query
from earmark.jobs import JobStore
from earmark.models import Segment, StoreResult
from earmark.vault.fs import vault_lock
from tests.factories import T0, MutableClock
from tests.index_helpers import (
    make_analysis_of,
    make_docs,
    make_segments,
    with_analyses,
)
from tests.pipeline_env import CANON, make_env

pytestmark = pytest.mark.index

runner = CliRunner()
TOKEN = "index-token-1c2d"


@pytest.fixture(scope="module")
def runtime() -> LanceRuntime:
    return load_lance_runtime()


class Env:
    def __init__(self, tmp_path: Path, runtime: LanceRuntime) -> None:
        self.tmp_path = tmp_path
        self.runtime = runtime
        self.embedder = FakeEmbedder()
        self.db_dir = tmp_path / "lancedb"
        self.index = self.make_index()

    def make_index(
        self,
        embedder: FakeEmbedder | None = None,
        *,
        lock_timeout_s: float = 300.0,
        optimize_every: int = 20,
    ) -> LanceIndex:
        return LanceIndex(
            self.db_dir,
            embedder or self.embedder,
            lock_path=self.tmp_path / "index.lock",
            runtime=self.runtime,
            lock_timeout_s=lock_timeout_s,
            optimize_every=optimize_every,
        )

    def table(self) -> LanceTable:
        return self.runtime.connect(self.db_dir).open_table()

    def rows(self) -> list[dict[str, object]]:
        found = self.table().search(self.embedder.embed_query("everything"), None, 10_000)
        return [dict(r) for r in found]

    def version_files(self) -> int:
        versions = self.db_dir / f"{TABLE}.lance" / "_versions"
        return len([p for p in versions.iterdir() if p.name.endswith(".manifest")])


@pytest.fixture
def env(tmp_path: Path, runtime: LanceRuntime) -> Env:
    return Env(tmp_path, runtime)


def ids(rows: Sequence[dict[str, object]]) -> list[str]:
    return sorted(str(r["id"]) for r in rows)


def telephone_docs() -> EpisodeDocs:
    episode = make_docs(guid="urn:phone").episode
    segments = (
        *make_segments(3, prefix="x"),
        Segment(start=65.0, end=70.0, text="Bell invented the telephone"),
    )
    analysis = make_analysis_of(
        episode,
        summary="A talk about nineteenth century inventors.",
        claims=[("The telephone was patented in 1876", 65.0)],
        leads=[("Read about Antonio Meucci", "a rival claimant", 80.0)],
        questions=[],
    )
    return make_docs(guid="urn:phone", title="Phones", segments=segments, analyses=[analysis])


def plants_docs() -> EpisodeDocs:
    episode = make_docs(guid="urn:plants").episode
    analysis = make_analysis_of(
        episode,
        summary="How photosynthesis feeds the plants.",
        claims=[("Chlorophyll absorbs red light", 10.0)],
        leads=[],
        questions=[],
    )
    return make_docs(guid="urn:plants", title="Plants", analyses=[analysis])


def empire_docs() -> EpisodeDocs:
    episode = make_docs(guid="urn:rome").episode
    analysis = make_analysis_of(
        episode,
        summary="The Roman empire at its height.",
        claims=[("Roads connected the provinces", 12.0)],
        leads=[],
        questions=[],
    )
    return make_docs(guid="urn:rome", title="Rome", analyses=[analysis])


def notes_for(docs: EpisodeDocs) -> NotePaths:
    return NotePaths(
        f"Earmark/Podcasts/{docs.episode.title}.md",
        f"Earmark/Podcasts/{docs.episode.title} (transcript).md",
    )


def stored_for(docs: EpisodeDocs) -> StoreResult:
    notes = notes_for(docs)
    assert notes.note_path is not None
    return StoreResult(note_path=notes.note_path, transcript_note_path=notes.transcript_note_path)


def put(env: Env, docs: EpisodeDocs) -> None:
    env.index.index(docs.episode, docs.transcript, docs.analyses, stored_for(docs))


# -- A1, A2 ----------------------------------------------------------------------------------


def test_rows_in_table_have_kinds_and_times(env: Env) -> None:
    docs = telephone_docs()
    put(env, docs)
    rows = {str(r["id"]): r for r in env.rows()}
    expected = build_rows(docs, notes_for(docs), fits=env.embedder.fits)
    assert sorted(rows) == sorted(r.id for r in expected)
    kinds = {str(r["kind"]) for r in rows.values()}
    assert kinds == {"transcript", "summary", "claim", "lead"}
    claim = next(r for r in rows.values() if r["kind"] == "claim")
    assert claim["start_s"] == 65.0
    assert claim["end_s"] is None
    transcript = next(r for r in rows.values() if r["kind"] == "transcript")
    assert transcript["start_s"] == 0.0
    assert transcript["note_path"] == notes_for(docs).note_path
    table = env.table()
    assert table.metadata() == {
        META_VERSION: "1",
        META_MODEL: "fake-embed-v1",
        META_DIM: "64",
    }
    assert table.vector_dim() == 64
    digest = rows_digest(expected, model="fake-embed-v1", dim=64)
    assert table.episode_digest(docs.episode.key) == digest


def test_index_twice_no_duplicates(env: Env) -> None:
    docs = telephone_docs()
    put(env, docs)
    expected = build_rows(docs, notes_for(docs), fits=env.embedder.fits)
    embedded = list(env.embedder.documents)
    version = env.table().version()
    put(env, docs)
    rows = env.rows()
    assert len(rows) == len(expected)
    assert ids(rows) == sorted(r.id for r in expected)
    assert env.embedder.documents == embedded
    assert env.table().version() == version
    result = env.index.replace(docs, notes_for(docs))
    assert result == ReplaceResult(len(expected), skipped=True)


def test_replace_after_change(env: Env) -> None:
    docs = telephone_docs()
    put(env, docs)
    second = make_analysis_of(
        docs.episode, prompt="Who else?", response="Meucci.", created_at=T0.replace(year=2027)
    )
    bigger = with_analyses(docs, *docs.analyses, second)
    put(env, bigger)
    assert ids(env.rows()) == sorted(
        r.id for r in build_rows(bigger, notes_for(bigger), fits=env.embedder.fits)
    )
    shorter = EpisodeDocs(
        docs.episode,
        docs.transcript.model_copy(update={"segments": docs.transcript.segments[:1]}),
        docs.analyses[:1],
    )
    put(env, shorter)
    expected = build_rows(shorter, notes_for(shorter), fits=env.embedder.fits)
    assert ids(env.rows()) == sorted(r.id for r in expected)
    assert not [r for r in env.rows() if r["kind"] == "answer"]


def test_other_episodes_untouched(env: Env) -> None:
    a, b = telephone_docs(), plants_docs()
    put(env, a)
    put(env, b)
    b_digest = env.table().episode_digest(b.episode.key)
    before = [r for r in env.rows() if r["episode_key"] == b.episode.key]
    second = make_analysis_of(a.episode, prompt="Again?", created_at=T0.replace(year=2027))
    put(env, with_analyses(a, *a.analyses, second))
    after = [r for r in env.rows() if r["episode_key"] == b.episode.key]
    assert before == after
    assert env.table().episode_digest(b.episode.key) == b_digest


def test_force_replace_reembeds(env: Env) -> None:
    docs = telephone_docs()
    put(env, docs)
    count = len(env.embedder.documents)
    result = env.index.replace(docs, notes_for(docs), force=True)
    assert result.skipped is False
    assert len(env.embedder.documents) == 2 * count
    assert len(env.rows()) == result.rows


def test_index_rejects_foreign_data(env: Env) -> None:
    a, b = telephone_docs(), plants_docs()
    with pytest.raises(PermanentError, match="another episode"):
        env.index.index(a.episode, b.transcript, a.analyses, stored_for(a))


def test_rows_with_only_analysis(env: Env) -> None:
    docs = make_docs(segments=())
    put(env, docs)
    assert {r["kind"] for r in env.rows()} == {"summary", "claim", "lead", "question"}


def test_wrong_vector_size_is_permanent(env: Env) -> None:
    class Short(FakeEmbedder):
        def embed_documents(self, texts: Sequence[str], /) -> list[list[float]]:
            return [[1.0] for _ in texts]

    index = env.make_index(Short())
    docs = telephone_docs()
    with pytest.raises(PermanentError, match="wrong size"):
        index.index(docs.episode, docs.transcript, docs.analyses, stored_for(docs))


# -- A3: search --------------------------------------------------------------------------------


def seed_three(env: Env) -> list[EpisodeDocs]:
    docs = [telephone_docs(), plants_docs(), empire_docs()]
    for d in docs:
        put(env, d)
    return docs


def test_search_ranks_relevant_rows_first(env: Env) -> None:
    docs = seed_three(env)
    results = env.index.search(parse_query("telephone patented 1876"))
    assert results.index_exists
    hits = results.hits
    assert hits[0].kind == "claim"
    assert hits[0].rank == 1
    assert hits[0].episode_key == docs[0].episode.key
    assert hits[0].timestamp == "00:01:05"
    assert hits[0].snippet == "The telephone was patented in 1876"
    assert hits[0].note_path == notes_for(docs[0]).note_path
    assert hits[0].transcript_note_path == notes_for(docs[0]).transcript_note_path
    assert hits[0].show_title == docs[0].episode.show_title
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]
    assert hits[0].published_at is not None
    assert env.embedder.queries == ["telephone patented 1876"]


def test_filters(env: Env) -> None:
    docs = seed_three(env)
    everything = env.index.search(parse_query("the", limit=50)).hits
    assert {h.kind for h in everything} >= {"claim", "summary", "transcript", "lead"}
    only = env.index.search(parse_query("the", kinds=["claim", "lead"], limit=50)).hits
    assert only
    assert {h.kind for h in only} == {"claim", "lead"}
    one = env.index.search(parse_query("the", episode_key=docs[1].episode.key, limit=50)).hits
    assert one
    assert {h.episode_key for h in one} == {docs[1].episode.key}
    both = env.index.search(
        parse_query("the", kinds=["summary"], episode_key=docs[2].episode.key, limit=50)
    ).hits
    assert [(h.kind, h.episode_key) for h in both] == [("summary", docs[2].episode.key)]
    # The limit applies after the filter: a rare kind still fills the page.
    limited = env.index.search(parse_query("telephone", kinds=["lead"], limit=1)).hits
    assert [h.kind for h in limited] == ["lead"]
    assert len(env.index.search(parse_query("the", limit=2)).hits) == 2


def test_hostile_text_cannot_inject(env: Env) -> None:
    docs = seed_three(env)
    hits = env.index.search(parse_query("x' OR 'a'='a")).hits
    assert hits
    assert len(env.index.search(parse_query("the", episode_key=docs[0].episode.key)).hits) <= 50


def test_search_before_any_write_creates_nothing(env: Env) -> None:
    before = sorted(p.name for p in env.tmp_path.iterdir())
    results = env.index.search(parse_query("anything"))
    assert results.index_exists is False
    assert results.hits == ()
    assert sorted(p.name for p in env.tmp_path.iterdir()) == before
    assert env.embedder.queries == []  # no model needed for a missing table


def test_build_creates_nothing(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    index = build_lancedb_index(settings, embedder=FakeEmbedder())
    assert isinstance(index, LanceIndex)
    assert list(tmp_path.iterdir()) == []


def api_client(tmp_path: Path, index: LanceIndex, *, token: bool) -> TestClient:
    settings = Settings(_env_file=None, api_token=SecretStr(TOKEN), data_dir=tmp_path / "api")
    store = JobStore(open_database(tmp_path / "earmark.db"), clock=MutableClock())
    headers = {"Authorization": f"Bearer {TOKEN}"} if token else {}
    return TestClient(create_app(settings, store=store, searcher=index), headers=headers)


def test_api_search_real(env: Env) -> None:
    docs = seed_three(env)
    with api_client(env.tmp_path, env.index, token=True) as client:
        response = client.get("/search", params={"q": "telephone patented", "limit": 3})
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 3
    assert hits[0]["kind"] == "claim"
    assert hits[0]["episode_key"] == docs[0].episode.key
    assert hits[0]["timestamp"] == "00:01:05"
    assert hits[0]["note_path"] == notes_for(docs[0]).note_path
    with api_client(env.tmp_path, env.index, token=False) as anon:
        assert anon.get("/search", params={"q": "telephone"}).status_code == 401


def test_api_mismatch_is_409(env: Env) -> None:
    put(env, telephone_docs())
    other = env.make_index(FakeEmbedder(model="fake-b"))
    with api_client(env.tmp_path, other, token=True) as client:
        response = client.get("/search", params={"q": "x"})
    assert response.status_code == 409
    assert "earmark reindex" in response.json()["detail"]


@pytest.fixture
def cli_env(env: Env, monkeypatch: pytest.MonkeyPatch, restore_root_logging: None) -> Env:
    monkeypatch.setenv("EARMARK_DATA_DIR", str(env.tmp_path))
    monkeypatch.setenv("EARMARK_STORE_BACKEND", "fake")
    monkeypatch.setattr(
        "earmark.index.embed.build_embedder", lambda settings, *, allow_download: FakeEmbedder()
    )
    return env


def test_cli_search_real(cli_env: Env) -> None:
    docs = seed_three(cli_env)
    result = runner.invoke(cli_app, ["search", "telephone patented 1876"])
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("1. ")
    assert lines[0].endswith("[00:01:05]  claim")
    assert lines[1] == "   The telephone was patented in 1876"
    assert lines[2] == f"   {notes_for(docs[0]).note_path}"


def test_cli_search_empty_index(cli_env: Env) -> None:
    result = runner.invoke(cli_app, ["search", "anything"])
    assert result.exit_code == 0
    assert result.stdout.startswith("the search index is empty")
    assert not cli_env.db_dir.exists()


# -- A5: mismatch ---------------------------------------------------------------------------


def test_model_mismatch_on_index(env: Env) -> None:
    put(env, telephone_docs())
    other = FakeEmbedder(model="fake-b")
    docs = plants_docs()
    with pytest.raises(IndexMismatchError) as info:
        env.make_index(other).index(docs.episode, docs.transcript, docs.analyses, stored_for(docs))
    message = info.value.message
    assert "'fake-embed-v1'" in message
    assert "'fake-b'" in message
    assert "earmark reindex" in message
    assert other.documents == []  # nothing was embedded
    assert other.queries == []


def test_dim_mismatch_on_search(env: Env) -> None:
    put(env, telephone_docs())
    other = FakeEmbedder(dim=32)
    with pytest.raises(IndexMismatchError, match=r"64 dimensions.*32 dimensions"):
        env.make_index(other).search(parse_query("x"))
    assert other.queries == []


def test_index_version_mismatch(env: Env) -> None:
    db = env.runtime.connect(env.db_dir)
    db.create_table(64, {META_VERSION: "0", META_MODEL: "fake-embed-v1", META_DIM: "64"})
    with pytest.raises(IndexMismatchError, match="index version 0"):
        env.index.search(parse_query("x"))
    docs = telephone_docs()
    with pytest.raises(IndexMismatchError):
        put(env, docs)
    assert env.embedder.documents == []


def test_foreign_table_mismatch(env: Env) -> None:
    env.runtime.connect(env.db_dir).create_table(64, {})
    with pytest.raises(IndexMismatchError, match="'unknown'") as info:
        env.index.search(parse_query("x"))
    assert "earmark reindex" in info.value.message


def test_mismatch_message_is_cleaned(env: Env) -> None:
    env.runtime.connect(env.db_dir).create_table(
        64, {META_VERSION: "1", META_MODEL: "evil\x1b[31m" + "m" * 300, META_DIM: "64"}
    )
    with pytest.raises(IndexMismatchError) as info:
        env.index.search(parse_query("x"))
    assert "\x1b" not in info.value.message
    assert "m" * 101 not in info.value.message


def test_mismatch_is_job_warning(tmp_path: Path, runtime: LanceRuntime) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    env = Env(tmp_path / "idx", runtime)
    put(env, telephone_docs())
    index = env.make_index(FakeEmbedder(model="fake-b"))
    pipeline_env = make_env(
        tmp_path, backends=dataclasses.replace(fakes.backends(), indexer=index), fakes=fakes
    )
    pipeline_env.jobs.enqueue(CANON)
    job = pipeline_env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert any(w.startswith("index failed: the search index was built with") for w in job.warnings)


def test_reshare_embeds_nothing(tmp_path: Path, runtime: LanceRuntime) -> None:
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    env = Env(tmp_path / "idx", runtime)
    pipeline_env = make_env(
        tmp_path, backends=dataclasses.replace(fakes.backends(), indexer=env.index), fakes=fakes
    )
    pipeline_env.jobs.enqueue(CANON)
    first = pipeline_env.worker.run_once()
    assert first is not None
    assert first.status == "done"
    assert not first.warnings
    embedded = list(env.embedder.documents)
    assert embedded
    version = env.table().version()
    pipeline_env.jobs.enqueue(CANON)
    second = pipeline_env.worker.run_once()
    assert second is not None
    assert second.status == "done"
    assert env.embedder.documents == embedded
    assert env.table().version() == version


# -- A6: reindex ------------------------------------------------------------------------------


def write_artifacts(root: Path, docs: EpisodeDocs) -> None:
    store = ArtifactStore(root)
    store.write_episode(docs.episode)
    store.write_transcript(docs.transcript)
    for analysis in docs.analyses:
        store.write_analysis(analysis)


def test_reindex_rebuilds_from_artifacts(env: Env) -> None:
    docs = [telephone_docs(), plants_docs()]
    for d in docs:
        write_artifacts(env.tmp_path, d)
        put(env, d)
    before = {str(r["id"]): r for r in env.rows()}
    digests = {d.episode.key: env.table().episode_digest(d.episode.key) for d in docs}
    import shutil

    shutil.rmtree(env.db_dir)
    artifacts = ArtifactStore(env.tmp_path)
    report = reindex(
        artifacts,
        env.index,
        lambda keys: {k: notes_for(next(d for d in docs if d.episode.key == k)) for k in keys},
    )
    assert report.episodes == 2
    assert report.rows == len(before)
    assert report.warnings == ()
    after = {str(r["id"]): r for r in env.rows()}
    assert after == before
    assert {d.episode.key: env.table().episode_digest(d.episode.key) for d in docs} == digests
    # A later re-share of the same inputs embeds nothing.
    count = len(env.embedder.documents)
    put(env, docs[0])
    assert len(env.embedder.documents) == count


def test_reindex_after_model_change(env: Env) -> None:
    docs = telephone_docs()
    write_artifacts(env.tmp_path, docs)
    old = env.make_index(FakeEmbedder(model="fake-a"))
    old.index(docs.episode, docs.transcript, docs.analyses, stored_for(docs))
    new_embedder = FakeEmbedder(model="fake-b", dim=32)
    new = env.make_index(new_embedder)
    with pytest.raises(IndexMismatchError):
        new.search(parse_query("telephone"))
    report = reindex(
        ArtifactStore(env.tmp_path), new, lambda keys: {k: notes_for(docs) for k in keys}
    )
    assert report.episodes == 1
    table = env.table()
    assert table.metadata()[META_MODEL] == "fake-b"
    assert table.vector_dim() == 32
    assert new.search(parse_query("telephone patented")).hits[0].kind == "claim"


def test_reindex_episode_only_keeps_others(env: Env) -> None:
    docs = [telephone_docs(), plants_docs()]
    for d in docs:
        write_artifacts(env.tmp_path, d)
        put(env, d)
    before = [r for r in env.rows() if r["episode_key"] == docs[1].episode.key]
    report = reindex(
        ArtifactStore(env.tmp_path),
        env.index,
        lambda keys: {k: notes_for(docs[0]) for k in keys},
        only=docs[0].episode.key,
    )
    assert report.episodes == 1
    assert [r for r in env.rows() if r["episode_key"] == docs[1].episode.key] == before


def test_cli_reindex_real(cli_env: Env) -> None:
    docs = [telephone_docs(), plants_docs()]
    for d in docs:
        write_artifacts(cli_env.tmp_path, d)
    result = runner.invoke(cli_app, ["reindex"])
    assert result.exit_code == 0, result.stderr
    rows = sum(
        len(
            build_rows(
                d, NotePaths(f"Earmark/Podcasts/{d.episode.key}.md", None), fits=FakeEmbedder().fits
            )
        )
        for d in docs
    )
    assert result.stdout == f"reindexed 2 episodes ({rows} rows)\n"
    search = runner.invoke(cli_app, ["search", "telephone patented"])
    assert search.exit_code == 0
    assert search.stdout.splitlines()[2] == f"   Earmark/Podcasts/{docs[0].episode.key}.md"
    assert load_episode_docs(ArtifactStore(cli_env.tmp_path), docs[0].episode.key) is not None


def test_reindex_episode_against_mismatch(cli_env: Env) -> None:
    docs = telephone_docs()
    write_artifacts(cli_env.tmp_path, docs)
    cli_env.make_index(FakeEmbedder(model="fake-old")).index(
        docs.episode, docs.transcript, docs.analyses, stored_for(docs)
    )
    result = runner.invoke(cli_app, ["reindex", "--episode", docs.episode.key])
    assert result.exit_code == 1
    assert "(without --episode)" in result.stderr
    assert "fake-old" in result.stderr
    full = runner.invoke(cli_app, ["reindex"])
    assert full.exit_code == 0, full.stderr


# -- B8, B9: locking and versions -------------------------------------------------------------


def test_lock_timeout_is_transient(env: Env) -> None:
    index = env.make_index(lock_timeout_s=0.2)
    docs = telephone_docs()
    with vault_lock(env.tmp_path / "index.lock", timeout_s=1):
        with pytest.raises(TransientError, match="search index"):
            index.index(docs.episode, docs.transcript, docs.analyses, stored_for(docs))
        with pytest.raises(TransientError, match="search index"):
            index.reset()
    index.index(docs.episode, docs.transcript, docs.analyses, stored_for(docs))


def test_optimize_every(env: Env, tmp_path: Path, runtime: LanceRuntime) -> None:
    plain = Env(tmp_path / "plain", runtime)
    tidy = Env(tmp_path / "tidy", runtime)
    tidy.index = tidy.make_index(optimize_every=2)
    plain.index = plain.make_index(optimize_every=1000)
    docs = [telephone_docs(), plants_docs(), empire_docs()]
    for d in docs:
        put(plain, d)
        put(tidy, d)
    second = make_analysis_of(docs[0].episode, prompt="Again?", created_at=T0.replace(year=2027))
    changed = with_analyses(docs[0], *docs[0].analyses, second)
    put(plain, changed)
    put(tidy, changed)
    assert plain.table().version() == 5  # created, then four merges
    assert tidy.table().version() > 5  # compaction ran
    assert ids(tidy.rows()) == ids(plain.rows())
    query = parse_query("telephone patented", limit=50)
    assert [(h.kind, h.snippet) for h in tidy.index.search(query).hits] == [
        (h.kind, h.snippet) for h in plain.index.search(query).hits
    ]
    tidy.index.optimize()  # explicit call also works
    tidy.index.reset()
    assert tidy.version_files() == 1
    assert tidy.rows() == []
    assert tidy.table().metadata()[META_MODEL] == "fake-embed-v1"


def test_reset_keeps_working_after_directory_deleted(env: Env) -> None:
    put(env, telephone_docs())
    import shutil

    shutil.rmtree(env.db_dir)
    assert env.index.search(parse_query("x")).index_exists is False
    put(env, plants_docs())
    assert len(env.rows()) > 0


def test_unreadable_table_is_transient(env: Env) -> None:
    put(env, telephone_docs())
    for p in (env.db_dir / f"{TABLE}.lance" / "_versions").iterdir():
        p.write_bytes(b"garbage")
    fresh = env.make_index()
    with pytest.raises(TransientError, match="could not be read") as info:
        fresh.search(parse_query("x"))
    assert str(env.tmp_path) not in info.value.message
    assert os.fspath(env.db_dir) not in info.value.message


def test_check_existing_creates_nothing_and_embeds_nothing(env: Env) -> None:
    assert env.index.check_existing() is False
    assert not env.db_dir.exists()
    assert env.embedder.documents == []
    assert env.embedder.queries == []


def test_check_existing_true_after_a_write(env: Env) -> None:
    put(env, telephone_docs())
    assert env.index.check_existing() is True
    assert env.embedder.queries == []


def test_check_existing_mismatch(env: Env) -> None:
    put(env, telephone_docs())
    with pytest.raises(IndexMismatchError, match="earmark reindex"):
        env.make_index(FakeEmbedder(model="fake-b")).check_existing()
