"""The ``http`` AudioFetcher: downloads the episode enclosure through the guarded fetcher."""

import logging
import time
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from earmark.artifacts import ArtifactStore
from earmark.config import Settings
from earmark.fetch import (
    DOWNLOAD_DEADLINE_S,
    MIB,
    GuardedFetcher,
    PermanentFetchError,
    guarded_fetcher,
)
from earmark.models import Episode

logger = logging.getLogger(__name__)

STALE_PART_AGE_S: Final = 2 * DOWNLOAD_DEADLINE_S


class HttpAudioFetcher:
    def __init__(
        self, artifacts: ArtifactStore, fetcher: GuardedFetcher, *, max_bytes: int
    ) -> None:
        self._artifacts = artifacts
        self._fetcher = fetcher
        self._max_bytes = max_bytes

    @property
    def fetcher(self) -> GuardedFetcher:
        return self._fetcher

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def fetch(self, episode: Episode, /) -> Path:
        key = episode.key
        existing = self._artifacts.find_audio(key)
        if existing is not None:
            return existing
        self._remove_stale_parts(key)
        if not episode.audio_url:
            raise PermanentFetchError("episode has no audio URL")
        artifacts = self._artifacts
        downloaded = self._fetcher.download(
            episode.audio_url,
            lambda ext: artifacts.audio_path(key, ext),
            max_bytes=self._max_bytes,
            purpose="audio",
            limit_name="EARMARK_MAX_AUDIO_MB",
        )
        logger.info(
            "downloaded audio for %s: %d bytes from %s after %d redirects",
            key,
            downloaded.size,
            urlsplit(downloaded.url).hostname,
            downloaded.redirects,
        )
        return downloaded.path

    def _remove_stale_parts(self, key: str) -> None:
        directory = self._artifacts.episode_dir(key)
        if not directory.is_dir():
            return
        cutoff = time.time() - STALE_PART_AGE_S
        for part in directory.glob(".audio.*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except FileNotFoundError:
                continue

    def close(self) -> None:
        self._fetcher.close()


def build_http_fetcher(settings: Settings, artifacts: ArtifactStore) -> HttpAudioFetcher:
    return HttpAudioFetcher(
        artifacts, guarded_fetcher(settings), max_bytes=settings.max_audio_mb * MIB
    )
