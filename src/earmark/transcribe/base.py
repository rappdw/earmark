"""The engine-agnostic transcriber shell: cache lookup, atomic writes, audio cleanup.

Light imports only. An engine turns an audio file into a ``Transcript``; this shell owns
everything around it, so a second engine (the remote backend) reuses it unchanged.
"""

import logging
import math
from pathlib import Path
from typing import Protocol

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.models import Episode, Segment, Transcript

logger = logging.getLogger(__name__)


class TranscriptionEngine(Protocol):
    @property
    def name(self) -> str:
        """For logs and ``Transcript.model``, e.g. ``faster-whisper:large-v3``."""
        ...

    def transcribe_file(self, audio: Path, episode_key: str, /) -> Transcript:
        """Transcribe ``audio``; write and delete nothing."""
        ...


class SegmentLike(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def text(self) -> str: ...


def segment_from(raw: SegmentLike, *, previous_start: float, duration_s: float) -> Segment | None:
    """Map a raw segment to a ``Segment`` with start >= previous start, end >= start, in range."""
    text = " ".join(raw.text.split())
    if not text:
        return None
    limit = duration_s if duration_s > 0 else math.inf
    start = raw.start if math.isfinite(raw.start) else previous_start
    start = min(max(start, previous_start, 0.0), limit)
    end = raw.end if math.isfinite(raw.end) else start
    end = min(max(end, start), limit)
    return Segment(start=start, end=end, text=text)


class UnusableAudioError(PermanentError):
    """The audio cannot be transcribed: undecodable, empty, or without speech."""


class CachedTranscriber:
    """Satisfies the ``Transcriber`` Protocol on top of a ``TranscriptionEngine``."""

    def __init__(
        self, artifacts: ArtifactStore, engine: TranscriptionEngine, *, keep_audio: bool
    ) -> None:
        self._artifacts = artifacts
        self._engine = engine
        self._keep_audio = keep_audio

    @property
    def engine(self) -> TranscriptionEngine:
        return self._engine

    @property
    def keep_audio(self) -> bool:
        return self._keep_audio

    def transcribe(self, episode: Episode, audio: Path, /) -> Transcript:
        key = episode.key
        cached = self._artifacts.read_transcript(key)
        if cached is not None:
            self._cleanup(key)
            return cached
        if not audio.is_file():
            raise PermanentError(f"audio file missing: {audio.name}")
        try:
            transcript = self._engine.transcribe_file(audio, key)
        except UnusableAudioError:
            self._cleanup(key)
            raise
        if transcript.episode_key != key:
            raise PermanentError(f"{self._engine.name} returned a transcript for another episode")
        self._artifacts.write_transcript(transcript)
        self._cleanup(key)
        logger.info("transcript for %s written (%d segments)", key, len(transcript.segments))
        return transcript

    def _cleanup(self, key: str) -> None:
        if self._keep_audio:
            return
        try:
            self._artifacts.delete_audio(key)
        except OSError:
            logger.warning("could not delete the audio for %s", key, exc_info=True)
