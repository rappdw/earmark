from pathlib import Path

from mypy import api

from earmark.artifacts import ArtifactStore
from earmark.fakes import FakeAudioFetcher, FakeNotifier, FakeResolver, FakeTranscriber
from earmark.pipeline.stages import AudioFetcher, Notifier, Resolver, Transcriber
from tests.factories import make_job

ROOT = Path(__file__).resolve().parent.parent


def test_fakes_satisfy_protocols(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    resolver: Resolver = FakeResolver()
    fetcher: AudioFetcher = FakeAudioFetcher(artifacts)
    transcriber: Transcriber = FakeTranscriber(artifacts)
    notifier: Notifier = FakeNotifier()

    episode = resolver.resolve("https://example.com/e")
    audio = fetcher.fetch(episode)
    assert transcriber.cached(episode) is None
    transcript = transcriber.transcribe(episode, audio)
    assert transcriber.cached(episode) == transcript
    downloaded = fetcher.download("https://example.com/a.mp3", lambda ext: tmp_path / f"d.{ext}")
    assert downloaded.read_bytes() == b"fake audio\n"
    notifier.notify(make_job(), episode)
    assert transcript.episode_key == episode.key


SNIPPET = """\
from collections.abc import Callable
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

    def download(self, u: str, d: Callable[[str], Path]) -> Path:
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
