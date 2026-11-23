"""One Protocol per pipeline stage.

Every parameter is positional-only, so implementations may name them freely and the
runner must call stages positionally.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from earmark.models import Episode, Job, Transcript


class Resolver(Protocol):
    def resolve(self, url: str, /) -> Episode:
        """Turn a shared URL into an Episode. Not cached.

        Raises SkipEpisode when there is no public feed or the episode is subscriber-only.
        """
        ...


class AudioFetcher(Protocol):
    def fetch(self, episode: Episode, /) -> Path:
        """Return the local audio path under ``ArtifactStore.audio_path(episode.key, ext)``.

        If ``find_audio`` already finds a file, return it without a request.
        """
        ...

    def download(self, url: str, destination: Callable[[str], Path], /) -> Path:
        """Download a client-supplied direct audio URL to ``destination(ext)``; return the path.

        Always downloads (no cache). Errors follow ``fetch.py``.
        """
        ...


class Transcriber(Protocol):
    def cached(self, episode: Episode, /) -> Transcript | None:
        """The cached transcript if one exists for this transcriber's profile.

        Never loads a model.
        """
        ...

    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript:
        """Return the cached transcript if ``cached`` has one (loading no model).

        Otherwise transcribe ``audio`` and write both transcript files through
        ``ArtifactStore.write_transcript``.
        """
        ...


class Notifier(Protocol):
    def notify(self, job: Job, episode: Episode | None, /) -> None:
        """Called with the job in a terminal state, and the episode if resolution got that far.

        Must not raise for delivery problems; the runner also catches anything it does raise.
        """
        ...


@dataclass(frozen=True, slots=True)
class Backends:
    """The four stage implementations the runner drives."""

    resolver: Resolver
    fetcher: AudioFetcher
    transcriber: Transcriber
    notifier: Notifier | None  # None when EARMARK_NOTIFY_BACKEND=none
