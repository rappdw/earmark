"""``earmark reindex``: rebuild the index from the artifacts on disk and the vault's notes."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError
from earmark.errors import PermanentError
from earmark.index.chunks import EpisodeDocs, NotePaths, load_episode_docs
from earmark.models import is_episode_key

type NoteLocator = Callable[[Sequence[str]], Mapping[str, NotePaths]]


class RebuildTarget(Protocol):
    def reset(self) -> None: ...

    def rebuild_episode(
        self, key: str, load: Callable[[], EpisodeDocs | None], notes: NotePaths, /
    ) -> int | None: ...

    def optimize(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReindexReport:
    episodes: int
    rows: int
    skipped: int
    warnings: tuple[str, ...]


def reindex(
    artifacts: ArtifactStore,
    target: RebuildTarget,
    locate: NoteLocator,
    *,
    only: str | None = None,
) -> ReindexReport:
    if only is not None:
        if not is_episode_key(only):
            raise ConfigError(["--episode: must be an episode key (20 hexadecimal characters)"])
        keys: list[str] = [only]
    else:
        target.reset()
        # Listed after the reset, so an episode the worker finishes meanwhile is either
        # listed here or indexed by the worker after the reset.
        keys = artifacts.list_episode_keys()
    notes = locate(keys)

    episodes = 0
    rows = 0
    skipped = 0
    warnings: list[str] = []
    for key in keys:
        paths = notes.get(key, NotePaths())
        unreadable: list[PermanentError] = []

        def load(
            key: str = key, unreadable: list[PermanentError] = unreadable
        ) -> EpisodeDocs | None:
            try:
                return load_episode_docs(artifacts, key)
            except PermanentError as exc:
                unreadable.append(exc)
                return None

        written = target.rebuild_episode(key, load, paths)
        if written is None:
            if only is not None:
                if unreadable:
                    raise unreadable[0]
                raise PermanentError(
                    f"episode {key} has no transcript or analyses under EARMARK_DATA_DIR"
                )
            if unreadable:
                warnings.append(f"episode {key}: unreadable files under EARMARK_DATA_DIR; skipped")
            skipped += 1
            continue
        episodes += 1
        rows += written
        if written and paths.note_path is None and paths.transcript_note_path is None:
            warnings.append(
                f"episode {key}: no note found in the vault; its hits have no note path"
            )
    target.optimize()
    return ReindexReport(episodes, rows, skipped, tuple(dict.fromkeys(warnings)))
