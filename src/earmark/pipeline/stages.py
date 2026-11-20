"""One Protocol per pipeline stage.

Every parameter is positional-only, so implementations may name them freely and the
runner must call stages positionally.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from earmark.models import Analysis, Episode, Job, StoreResult, Transcript


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


class Transcriber(Protocol):
    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript:
        """Return the cached transcript if ``transcript.json`` exists (loading no model).

        Otherwise transcribe ``audio`` and write both transcript files through
        ``ArtifactStore.write_transcript``.
        """
        ...


class Analyzer(Protocol):
    def analyze(self, episode: Episode, transcript: Transcript, prompt: str | None, /) -> Analysis:
        """Return the cached analysis for ``prompt_hash(prompt, model)`` without an LLM call.

        Otherwise analyze and write it. ``prompt`` is already normalised (None = default).
        """
        ...


class Store(Protocol):
    def store(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
        /,
    ) -> StoreResult:
        """Write the notes for the episode from all its analyses, deterministically.

        ``shared_at`` is the episode's share time for the frontmatter.
        """
        ...


class Indexer(Protocol):
    def index(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        stored: StoreResult,
        /,
    ) -> None:
        """Replace the episode's rows in the search index.

        The runner records failures as job warnings; they never fail the job.
        """
        ...


class Notifier(Protocol):
    def notify(self, job: Job, episode: Episode | None, /) -> None:
        """Called with the job in a terminal state, and the episode if resolution got that far.

        Must not raise for delivery problems; the runner also catches anything it does raise.
        """
        ...
