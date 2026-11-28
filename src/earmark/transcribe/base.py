"""The engine-agnostic transcriber shell: cache lookup, atomic writes, audio cleanup.

Light imports only. An engine turns an audio file into a ``Transcript``; this shell owns
everything around it, so a second engine (the remote backend) reuses it unchanged.
"""

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.models import (
    GUID_SHA256_PREFIX,
    DecodeOptions,
    Episode,
    Segment,
    Transcript,
    decode_key,
)

logger = logging.getLogger(__name__)


def asr_profile(backend: str, options: Mapping[str, str | int | bool | None]) -> str:
    """The cache profile of a backend: its name and a canonical JSON of its options."""
    return backend + " " + json.dumps(options, sort_keys=True, separators=(",", ":"))


def with_decode_options(
    fields: Mapping[str, str | int | bool | None], options: DecodeOptions | None
) -> dict[str, str | int | bool | None]:
    """The profile fields with a /v1 job's options applied; the prompt only as a hash."""
    merged = dict(fields)
    if options is None:
        return merged
    merged["language"] = options.language
    if options.initial_prompt is not None:
        merged["initial_prompt_sha256"] = hashlib.sha256(
            options.initial_prompt.encode("utf-8")
        ).hexdigest()
    return merged


def decoded_key_for(episode: Episode, options: DecodeOptions) -> str:
    """The per-options transcript key of an upload or url episode."""
    if episode.source not in ("upload", "url") or not episode.guid.startswith(GUID_SHA256_PREFIX):
        raise PermanentError("decode options apply to uploaded audio only")
    return decode_key(episode.guid[len(GUID_SHA256_PREFIX) :], options)


class TranscriptionEngine(Protocol):
    @property
    def name(self) -> str:
        """For logs and ``Transcript.model``, e.g. ``faster-whisper:large-v3``."""
        ...

    @property
    def profile(self) -> str:
        """Backend, model and options (see ``asr_profile``); a transcript made with another
        profile is not reused."""
        ...

    def profile_for(self, options: DecodeOptions | None, /) -> str:
        """The profile with a /v1 job's options applied (``profile`` for ``None``)."""
        ...

    def transcribe_file(
        self, audio: Path, episode_key: str, options: DecodeOptions | None = None, /
    ) -> Transcript:
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
    """The file is not decodable audio, or decodes to no samples. The audio is deleted."""


class NoSpeechError(PermanentError):
    """The audio decoded but the engine found no speech. Permanent; the audio is kept."""


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

    @property
    def profile(self) -> str:
        return self._engine.profile

    def cached(
        self, episode: Episode, options: DecodeOptions | None = None, /
    ) -> Transcript | None:
        """The stored transcript if it was made with this engine's profile; loads no model."""
        if options is None:
            stored = self._artifacts.read_transcript(episode.key)
        else:
            stored = self._artifacts.read_decoded(decoded_key_for(episode, options), episode.key)
        if stored is None or stored.asr_profile != self._engine.profile_for(options):
            return None
        return stored

    def transcribe(
        self, episode: Episode, audio: Path, options: DecodeOptions | None = None, /
    ) -> Transcript:
        key = episode.key
        dkey = None if options is None else decoded_key_for(episode, options)
        cached = self.cached(episode, options)
        if cached is not None:
            self._cleanup(key)
            return cached
        if not audio.is_file():
            raise PermanentError(f"audio file missing: {audio.name}")
        try:
            transcript = self._engine.transcribe_file(audio, key, options)
        except UnusableAudioError:
            self._cleanup(key)
            raise
        if transcript.episode_key != key:
            raise PermanentError(f"{self._engine.name} returned a transcript for another episode")
        transcript = transcript.model_copy(
            update={"asr_profile": self._engine.profile_for(options)}
        )
        if dkey is None:
            self._artifacts.write_transcript(transcript)
        else:
            self._artifacts.write_decoded(dkey, transcript)
        self._cleanup(key)
        logger.info(
            "transcript for %s written (%d segments)%s",
            key,
            len(transcript.segments),
            "" if options is None else " (per-request options)",
        )
        return transcript

    def _cleanup(self, key: str) -> None:
        if self._keep_audio:
            return
        try:
            self._artifacts.delete_audio(key)
        except OSError:
            logger.warning("could not delete the audio for %s", key, exc_info=True)
