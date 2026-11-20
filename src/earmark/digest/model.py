"""Shared digest types and the ``Themer`` and ``DigestWriter`` protocols. Light imports only:
``vault/notes.py`` and ``fakes.py`` import this module."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from earmark.digest.window import DigestWindow
from earmark.index.chunks import NotePaths
from earmark.models import Episode, Model
from earmark.vault.document import YamlValue


@dataclass(frozen=True, slots=True)
class DigestLead:
    lead_id: str  # "L1", "L2", ... in digest order
    episode_key: str
    text: str
    rationale: str
    timestamp_s: float | None


@dataclass(frozen=True, slots=True)
class DigestEpisode:
    episode: Episode
    shared_at: datetime  # first share, aware
    analyses: int  # readable analyses on disk
    leads: tuple[DigestLead, ...]


class ThemeEntry(Model):
    title: str
    summary: str
    leads: tuple[str, ...]  # lead ids


class ThemeRecord(Model):
    """The content of ``digests/<input_hash>.json``."""

    version: int  # DIGEST_SCHEMA_VERSION
    input_hash: str  # 16 hex
    model: str  # Themer.model
    themes: tuple[ThemeEntry, ...]
    requests: int  # completions it took (repairs included)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ThemeRequest:
    input_hash: str
    leads: tuple[DigestLead, ...]  # non-empty
    episodes: Mapping[str, Episode]  # by key: show and title for the prompt


class Themer(Protocol):
    @property
    def model(self) -> str: ...

    def themes(self, request: ThemeRequest, /) -> ThemeRecord: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Digest:
    window: DigestWindow
    episodes: tuple[DigestEpisode, ...]  # by (shared_at, key)
    themes: tuple[ThemeEntry, ...]  # validated: known ids, each once, none empty
    other: tuple[str, ...]  # lead ids in no theme, in lead order
    model: str | None  # None when there were no leads
    requests: int  # 0 on a cache hit or with no leads
    cached: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DigestNote:
    digest_id: str
    file_stem: str
    owned: tuple[tuple[str, YamlValue], ...]
    seeds: tuple[tuple[str, YamlValue], ...]
    episode_keys: tuple[str, ...]
    render: Callable[[Mapping[str, NotePaths]], str]  # paths found -> the region


@dataclass(frozen=True, slots=True)
class DigestWriteResult:
    note_path: str  # vault-relative, starts with VAULT_SUBDIR
    status: Literal["created", "updated", "unchanged"]
    warnings: tuple[str, ...] = ()


class DigestWriter(Protocol):
    def write_digest(self, note: DigestNote, /) -> DigestWriteResult: ...
