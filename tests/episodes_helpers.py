"""Seed stored episodes for the listing and reanalyze tests."""

from datetime import datetime, timedelta
from pathlib import Path

from earmark.artifacts import ArtifactStore
from earmark.models import Episode
from tests.factories import T0, make_analysis, make_episode, make_transcript


def seed_episode(
    artifacts: ArtifactStore,
    n: int,
    *,
    shared_at: datetime | None = T0,
    transcript: bool = True,
    prompts: tuple[str | None, ...] = (),
    title: str | None = None,
) -> Episode:
    """Write episode ``n`` with an optional share record, transcript, and analyses."""
    guid = f"urn:example:guid-{n}"
    episode = make_episode(guid=guid, title=title or f"Episode {n}")
    artifacts.write_episode(episode)
    if shared_at is not None:
        artifacts.record_share(episode.key, shared_at)
    if transcript:
        artifacts.write_transcript(make_transcript(guid=guid))
    for i, prompt in enumerate(prompts):
        artifacts.write_analysis(
            make_analysis(guid=guid, prompt=prompt, created_at=T0 + timedelta(minutes=i))
        )
    return episode


def corrupt(path: Path) -> None:
    path.write_text("not json")
