"""Stored-episode listing and the reanalyze preconditions. Shared by the API and the CLI.

Reads only the artifacts under ``EARMARK_DATA_DIR/episodes``: never the job store, the
registry, the vault, or a transcript's content.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import AwareDatetime

from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError, TransientError
from earmark.jobs import Enqueued, JobStore
from earmark.models import (
    Analysis,
    Episode,
    EpisodeKey,
    Model,
    PromptHash,
    ReanalyzeRequest,
    is_episode_key,
)

logger = logging.getLogger(__name__)

EPISODES_LIMIT_DEFAULT: Final = 50
EPISODES_LIMIT_MAX: Final = 200
EPISODES_OFFSET_MAX: Final = 1_000_000
EPISODE_NOT_FOUND: Final = "episode not found"
NO_TRANSCRIPT: Final = (
    "this episode has no transcript yet; share it again, or wait for its job to finish"
)
UNREADABLE_ROOT: Final = "the episode files under EARMARK_DATA_DIR cannot be read"


class AnalysisInfo(Model):
    prompt: str | None  # None = the default analysis
    prompt_hash: PromptHash
    model: str
    created_at: AwareDatetime


class EpisodeInfo(Model):
    key: EpisodeKey
    show: str
    title: str
    published_at: AwareDatetime | None
    shared_at: AwareDatetime | None
    url: str
    has_transcript: bool
    analyses: tuple[AnalysisInfo, ...] = ()  # newest first


class EpisodePage(Model):
    episodes: tuple[EpisodeInfo, ...]
    total: int
    offset: int
    limit: int
    next_offset: int | None


class EpisodeLookupError(Exception):
    """Base of the two reanalyze preconditions; ``message`` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EpisodeNotFoundError(EpisodeLookupError): ...


class TranscriptMissingError(EpisodeLookupError): ...


@dataclass(frozen=True)
class _Listable:
    episode: Episode
    shared_at: datetime | None


def _sort_key(item: _Listable) -> tuple[int, float, str]:
    if item.shared_at is None:
        return (1, 0.0, item.episode.key)
    # Newest share first; ties by key ascending.
    return (0, -item.shared_at.astimezone(UTC).timestamp(), item.episode.key)


def _analysis_info(analysis: Analysis) -> AnalysisInfo:
    return AnalysisInfo(
        prompt=analysis.prompt,
        prompt_hash=analysis.prompt_hash,
        model=analysis.model,
        created_at=analysis.created_at,
    )


def _page_analyses(artifacts: ArtifactStore, key: str) -> tuple[AnalysisInfo, ...]:
    found: list[Analysis] = []
    for prompt_hash in artifacts.list_analysis_hashes(key):
        try:
            analysis = artifacts.read_analysis(key, prompt_hash)
        except (PermanentError, OSError):
            logger.warning(
                "episode %s: analysis %s is unreadable; left out of the listing", key, prompt_hash
            )
            continue
        if analysis is not None:
            found.append(analysis)
    found.sort(key=lambda a: (a.created_at, a.prompt_hash), reverse=True)
    return tuple(_analysis_info(a) for a in found)


def list_episodes(
    artifacts: ArtifactStore,
    *,
    limit: int = EPISODES_LIMIT_DEFAULT,
    offset: int = 0,
) -> EpisodePage:
    """Stored episodes, newest first share first; a page of at most ``limit``."""
    if not 1 <= limit <= EPISODES_LIMIT_MAX:
        raise ValueError(f"limit must be between 1 and {EPISODES_LIMIT_MAX}")
    if not 0 <= offset <= EPISODES_OFFSET_MAX:
        raise ValueError(f"offset must be between 0 and {EPISODES_OFFSET_MAX}")
    try:
        keys = artifacts.list_episode_keys()
    except OSError:
        raise TransientError(UNREADABLE_ROOT) from None

    listable: list[_Listable] = []
    for key in keys:
        try:
            episode = artifacts.read_episode(key)
            share = artifacts.read_share(key)
        except (PermanentError, OSError):
            logger.warning(
                "episode %s: unreadable files under EARMARK_DATA_DIR; left out of the listing",
                key,
            )
            continue
        if episode is None:
            continue
        listable.append(_Listable(episode, share.first_shared_at if share else None))
    listable.sort(key=_sort_key)

    total = len(listable)
    entries = []
    for item in listable[offset : offset + limit]:
        ep = item.episode
        entries.append(
            EpisodeInfo(
                key=ep.key,
                show=ep.show_title,
                title=ep.title,
                published_at=ep.published_at,
                shared_at=item.shared_at,
                url=ep.url,
                has_transcript=artifacts.has_transcript(ep.key),
                analyses=_page_analyses(artifacts, ep.key),
            )
        )
    next_offset = offset + limit if offset + limit < total else None
    return EpisodePage(
        episodes=tuple(entries), total=total, offset=offset, limit=limit, next_offset=next_offset
    )


def require_transcript(artifacts: ArtifactStore, key: str) -> None:
    """Raise when ``key`` is not a stored episode, or has no transcript yet."""
    if not is_episode_key(key) or not artifacts.has_episode(key):
        raise EpisodeNotFoundError(EPISODE_NOT_FOUND)
    if not artifacts.has_transcript(key):
        raise TranscriptMissingError(NO_TRANSCRIPT)


def enqueue_reanalyze(
    jobs: JobStore, artifacts: ArtifactStore, key: str, request: ReanalyzeRequest
) -> Enqueued:
    """Check the episode is stored with a transcript, then enqueue a reanalyze job."""
    require_transcript(artifacts, key)
    enqueued = jobs.enqueue_reanalyze(key, prompt=request.prompt)
    logger.info(
        "enqueued reanalyze job %s for %s (created=%s, prompt=%s)",
        enqueued.job.id,
        key,
        enqueued.created,
        "yes" if request.prompt else "no",
    )
    return enqueued
