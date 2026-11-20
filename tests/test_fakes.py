import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from earmark.errors import PermanentError, TransientError
from earmark.fakes import (
    FAKE_NOW,
    AnalyzeCall,
    FakeAnalyzer,
    FakeAudioFetcher,
    FakeBackends,
    FakeIndexer,
    FakeNotifier,
    FakeResolver,
    FakeStore,
    FakeTranscriber,
    FetchCall,
    ResolveCall,
    TranscribeCall,
    fixed_clock,
    make_fake_backends,
)
from earmark.models import Analysis, Episode, StoreResult, Transcript, prompt_hash
from tests.factories import T0, make_analysis, make_episode, make_job, make_transcript

URL = "https://podcasts.apple.com/us/podcast/x/id1?i=2"


def _run(
    fb: FakeBackends, prompt: str | None = "Q?"
) -> tuple[Episode, Transcript, Analysis, StoreResult]:
    ep = fb.resolver.resolve(URL)
    audio = fb.fetcher.fetch(ep)
    tr = fb.transcriber.transcribe(ep, audio)
    an = fb.analyzer.analyze(ep, tr, prompt)
    return ep, tr, an, fb.store.store(ep, tr, [an], FAKE_NOW)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_fakes_deterministic(tmp_path: Path) -> None:
    a = make_fake_backends(tmp_path / "a" / "data", vault_dir=tmp_path / "a" / "vault")
    b = make_fake_backends(tmp_path / "b" / "data", vault_dir=tmp_path / "b" / "vault")
    assert _run(a) == _run(b)
    tree_a, tree_b = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    assert tree_a == tree_b
    assert len(tree_a) == 5  # audio, transcript x2, analysis, note


def test_resolver_same_url_same_episode() -> None:
    r = FakeResolver()
    first = r.resolve(URL)
    assert r.resolve(URL) == first
    assert r.resolve(URL + "x") != first
    digest = hashlib.sha256(URL.encode()).hexdigest()[:12]
    assert first.episode_id == digest
    assert first.audio_url.endswith(".mp3")
    assert ".invalid" in first.audio_url


def test_resolver_mapping_wins() -> None:
    ep = make_episode()
    r = FakeResolver({URL: ep})
    assert r.resolve(URL) is ep
    assert r.resolve("other") != ep


def test_call_counts(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, tr, an, res = _run(fb)
    audio = fb.artifacts.audio_path(ep.key, "mp3")
    job = make_job()
    fb.indexer.index(ep, tr, [an], res)
    fb.notifier.notify(job, None)
    assert fb.resolver.calls == [ResolveCall(URL)]
    assert fb.fetcher.calls == [FetchCall(ep)]
    assert fb.transcriber.calls == [TranscribeCall(ep, audio)]
    assert fb.analyzer.calls == [AnalyzeCall(ep, tr, "Q?")]
    assert fb.store.call_count == 1
    assert fb.store.calls[0].analyses == (an,)
    assert fb.store.calls[0].shared_at == FAKE_NOW
    assert fb.indexer.calls[0].stored == res
    assert fb.notifier.calls[0].job == job
    assert fb.notifier.calls[0].episode is None
    assert fb.notifier.call_count == 1


def test_fetcher_cache_hit_not_computed(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    first = fb.fetcher.fetch(ep)
    assert fb.fetcher.fetch(ep) == first
    assert (fb.fetcher.call_count, fb.fetcher.computed) == (2, 1)
    assert first.read_bytes() == b"fake audio\n"


def test_transcriber_cache_hit_not_computed(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    audio = fb.fetcher.fetch(ep)
    first = fb.transcriber.transcribe(ep, audio)
    audio.unlink()  # a cache hit needs no audio
    assert fb.transcriber.transcribe(ep, audio) == first
    assert (fb.transcriber.call_count, fb.transcriber.computed) == (2, 1)
    assert first.duration_s == 900.0


def test_analyzer_cache_hit_not_computed(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, tr, an, _ = _run(fb)
    assert fb.analyzer.analyze(ep, tr, "Q?") == an
    assert (fb.analyzer.call_count, fb.analyzer.computed) == (2, 1)


def test_analyzer_new_prompt_computes(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, tr, _, _ = _run(fb)
    fb.analyzer.analyze(ep, tr, "Other?")
    assert fb.analyzer.computed == 2
    assert len(list(fb.artifacts.analyses_dir(ep.key).glob("*.json"))) == 2


def test_fail_next_once_then_succeeds(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    audio = fb.fetcher.fetch(ep)
    fb.transcriber.fail_next(TransientError("later"))
    with pytest.raises(TransientError):
        fb.transcriber.transcribe(ep, audio)
    assert fb.transcriber.computed == 0
    fb.transcriber.transcribe(ep, audio)
    assert (fb.transcriber.call_count, fb.transcriber.computed) == (2, 1)


def test_fail_next_times_and_order() -> None:
    r = FakeResolver()
    first, second = TransientError("1"), PermanentError("2")
    r.fail_next(first, times=2)
    r.fail_next(second)
    for expected in (first, first, second):
        with pytest.raises(type(expected)) as info:
            r.resolve(URL)
        assert info.value is expected
    r.resolve(URL)
    assert r.call_count == 4


def test_fail_next_rejects_zero() -> None:
    with pytest.raises(ValueError, match="times"):
        FakeResolver().fail_next(PermanentError("x"), times=0)


def _stages(
    tmp_path: Path,
) -> list[tuple[Callable[[], int], Callable[[Exception], None], Callable[[], object]]]:
    fb = make_fake_backends(tmp_path)
    ep, tr, an, res = _run(fb)
    audio = fb.artifacts.audio_path(ep.key, "mp3")
    return [
        (
            lambda: fb.resolver.call_count,
            fb.resolver.fail_next,
            lambda: fb.resolver.resolve(URL),
        ),
        (lambda: fb.fetcher.call_count, fb.fetcher.fail_next, lambda: fb.fetcher.fetch(ep)),
        (
            lambda: fb.transcriber.call_count,
            fb.transcriber.fail_next,
            lambda: fb.transcriber.transcribe(ep, audio),
        ),
        (
            lambda: fb.analyzer.call_count,
            fb.analyzer.fail_next,
            lambda: fb.analyzer.analyze(ep, tr, None),
        ),
        (
            lambda: fb.store.call_count,
            fb.store.fail_next,
            lambda: fb.store.store(ep, tr, [an], FAKE_NOW),
        ),
        (
            lambda: fb.indexer.call_count,
            fb.indexer.fail_next,
            lambda: fb.indexer.index(ep, tr, [an], res),
        ),
        (
            lambda: fb.notifier.call_count,
            fb.notifier.fail_next,
            lambda: fb.notifier.notify(make_job(), ep),
        ),
    ]


@pytest.mark.parametrize("index", range(7))
def test_every_fake_supports_fail_next(tmp_path: Path, index: int) -> None:
    count, fail_next, invoke = _stages(tmp_path)[index]
    before = count()
    fail_next(PermanentError("boom"))
    with pytest.raises(PermanentError, match="boom"):
        invoke()
    assert count() == before + 1
    invoke()
    assert count() == before + 2


def test_fake_pipeline_writes_artifacts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    fb = make_fake_backends(tmp_path / "data", vault_dir=vault)
    ep, _, an, res = _run(fb)
    a = fb.artifacts
    assert not a.episode_path(ep.key).exists()
    assert a.find_audio(ep.key) == a.audio_path(ep.key, "mp3")
    assert a.transcript_json_path(ep.key).is_file()
    assert a.transcript_text_path(ep.key).is_file()
    assert a.analysis_path(ep.key, an.prompt_hash).is_file()
    assert (vault / res.note_path).is_file()
    assert res.note_path == f"Earmark/Podcasts/{ep.key}.md"
    note = (vault / res.note_path).read_text()
    assert note.startswith(f"# {ep.title}\n")
    assert "## Q?" in note


def test_store_twice_same_bytes(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path / "d", vault_dir=tmp_path / "v")
    ep, tr, an, res = _run(fb, prompt=None)
    path = tmp_path / "v" / res.note_path
    first = path.read_bytes()
    second_an = make_analysis(guid=ep.guid, prompt="newer", created_at=T0)
    for analyses in ([an], [an, second_an], [second_an, an]):
        fb.store.store(ep, tr, analyses, FAKE_NOW)
        fb.store.store(ep, tr, analyses, FAKE_NOW)
    assert path.read_bytes() == _two_analysis_note(ep.title, an, second_an)
    assert first.startswith(b"# ")
    assert b"## Default analysis" in first


def _two_analysis_note(title: str, a: Analysis, b: Analysis) -> bytes:
    newest, oldest = sorted((a, b), key=lambda x: (x.created_at, x.prompt_hash), reverse=True)
    return (
        f"# {title}\n\n## {newest.prompt or 'Default analysis'}\n\n{newest.summary}\n"
        f"\n## {oldest.prompt or 'Default analysis'}\n\n{oldest.summary}\n"
    ).encode()


def test_store_without_vault_writes_nothing(tmp_path: Path) -> None:
    store = FakeStore()
    ep = make_episode()
    res = store.store(ep, make_transcript(), [], FAKE_NOW)
    assert res.transcript_note_path is None
    assert list(tmp_path.iterdir()) == []


def test_store_rejects_bad_subdir() -> None:
    for bad in ("/abs", "../x", ""):
        with pytest.raises(ValueError, match="subdir"):
            FakeStore(subdir=bad)


def test_transcriber_missing_audio_is_permanent(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep = fb.resolver.resolve(URL)
    with pytest.raises(PermanentError, match=r"audio file missing: nope\.mp3"):
        fb.transcriber.transcribe(ep, tmp_path / "nope.mp3")
    assert fb.transcriber.computed == 0


def test_analyzer_prompt_response(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, tr, _, _ = _run(fb, prompt=None)
    assert fb.analyzer.analyze(ep, tr, None).prompt_response is None
    answer = fb.analyzer.analyze(ep, tr, "Why?").prompt_response
    assert answer is not None
    assert "Why?" in answer
    assert fb.analyzer.analyze(ep, tr, "Why?").prompt_hash == prompt_hash("Why?", "fake-llm")


def test_fake_warnings_flow(tmp_path: Path) -> None:
    artifacts = make_fake_backends(tmp_path).artifacts
    transcriber = FakeTranscriber(artifacts, warnings=["tw"])
    analyzer = FakeAnalyzer(artifacts, warnings=["aw"], clock=fixed_clock(T0))
    ep = FakeResolver().resolve(URL)
    audio = FakeAudioFetcher(artifacts).fetch(ep)
    tr = transcriber.transcribe(ep, audio)
    an = analyzer.analyze(ep, tr, None)
    assert tr.warnings == ("tw",)
    assert an.warnings == ("aw",)
    assert an.created_at == T0
    assert transcriber.transcribe(ep, audio).warnings == ("tw",)
    assert analyzer.analyze(ep, tr, None).warnings == ("aw",)


def test_indexer_rows_replace(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    ep, tr, an, res = _run(fb)
    idx = FakeIndexer()
    idx.index(ep, tr, [an], res)
    idx.index(ep, tr, [an], res)
    assert idx.rows == {ep.key: 2}
    idx.index(ep, tr, [], res)
    assert idx.rows == {ep.key: 1}


def test_notifier_records() -> None:
    n = FakeNotifier()
    n.notify(make_job(), None)
    assert n.call_count == 1


def test_fake_backends_as_backends(tmp_path: Path) -> None:
    fb = make_fake_backends(tmp_path)
    b = fb.backends()
    assert b.resolver is fb.resolver
    assert b.fetcher is fb.fetcher
    assert b.transcriber is fb.transcriber
    assert b.analyzer is fb.analyzer
    assert b.store is fb.store
    assert b.indexer is fb.indexer
    assert b.notifier is fb.notifier
