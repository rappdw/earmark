from pathlib import Path

from mypy import api

from earmark.artifacts import ArtifactStore
from earmark.fakes import (
    FakeAnalyzer,
    FakeAudioFetcher,
    FakeIndexer,
    FakeNotifier,
    FakeResolver,
    FakeStore,
    FakeTranscriber,
)
from earmark.pipeline.stages import (
    Analyzer,
    AudioFetcher,
    Indexer,
    Notifier,
    Resolver,
    Store,
    Transcriber,
)
from earmark.vault.notes import VaultStore
from tests.factories import T0, make_job

ROOT = Path(__file__).resolve().parent.parent


def test_fakes_satisfy_protocols(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    resolver: Resolver = FakeResolver()
    fetcher: AudioFetcher = FakeAudioFetcher(artifacts)
    transcriber: Transcriber = FakeTranscriber(artifacts)
    analyzer: Analyzer = FakeAnalyzer(artifacts)
    store: Store = FakeStore()
    indexer: Indexer = FakeIndexer()
    notifier: Notifier = FakeNotifier()

    episode = resolver.resolve("https://example.com/e")
    audio = fetcher.fetch(episode)
    transcript = transcriber.transcribe(episode, audio)
    analysis = analyzer.analyze(episode, transcript, None)
    stored = store.store(episode, transcript, [analysis], T0)
    indexer.index(episode, transcript, [analysis], stored)
    notifier.notify(make_job(), episode)
    assert stored.note_path.endswith(f"{episode.key}.md")

    vault_store: Store = VaultStore(tmp_path / "vault", "Earmark", data_dir=tmp_path / "data")
    assert vault_store is not None


SNIPPET = """\
from pathlib import Path

from earmark.models import Episode, Transcript
from earmark.pipeline.stages import AudioFetcher, Transcriber


class BadFetcher:
    def fetch(self, episode: Episode) -> str:
        return ""


class BadTranscriber:
    def transcribe(self, episode: Episode) -> Transcript:
        raise NotImplementedError


class GoodFetcher:
    def fetch(self, ep: Episode) -> Path:
        return Path()


bad_fetcher: AudioFetcher = BadFetcher()
bad_transcriber: Transcriber = BadTranscriber()
good_fetcher: AudioFetcher = GoodFetcher()
"""


def test_protocol_mismatch_fails_mypy(tmp_path: Path) -> None:
    snippet = tmp_path / "snippet.py"
    snippet.write_text(SNIPPET)
    stdout, _stderr, status = api.run(
        [
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(snippet),
        ]
    )
    assert status == 1
    lines = SNIPPET.splitlines()
    error_lines = {
        int(line.split(":")[1]): line for line in stdout.splitlines() if "error:" in line
    }
    bad_f = lines.index("bad_fetcher: AudioFetcher = BadFetcher()") + 1
    bad_t = lines.index("bad_transcriber: Transcriber = BadTranscriber()") + 1
    good = lines.index("good_fetcher: AudioFetcher = GoodFetcher()") + 1
    assert "[assignment]" in error_lines[bad_f]
    assert "[assignment]" in error_lines[bad_t]
    assert good not in error_lines
