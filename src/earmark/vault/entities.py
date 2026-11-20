"""The entity registry: recurring people, papers, companies, tools, and topics.

SQLite tables (``episodes``, ``mentions``, ``aliases``, ``entities``) are derived data kept
in ``earmark.db``; ``resolve_entities`` turns them into one view per entity, and the render
functions turn a view into the managed region of an entity note. ``aliases.yaml`` entries
(``AliasEntry``) merge names; this module never imports ``vault.aliases`` at run time.

See ``docs/ARCHITECTURE.md`` (Entities) and ``docs/steps/13-entities.md``.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Literal

from earmark.db import Database
from earmark.models import Analysis, Episode, entity_key, format_hms, scrub_surrogates
from earmark.vault.document import YamlValue
from earmark.vault.sanitize import escape_markdown, fold_key

if TYPE_CHECKING:
    from earmark.vault.aliases import AliasEntry

type EntityKind = Literal["person", "paper", "company", "tool", "topic"]


@dataclass(frozen=True, slots=True)
class KindInfo:
    field: str  # Analysis field
    kind: EntityKind
    folder: str  # directory under Entities/
    label: str  # shown in the note


KINDS: Final[tuple[KindInfo, ...]] = (
    KindInfo("people", "person", "People", "Person"),
    KindInfo("papers", "paper", "Papers", "Paper"),
    KindInfo("companies", "company", "Companies", "Company"),
    KindInfo("tools", "tool", "Tools", "Tool"),
    KindInfo("topics", "topic", "Topics", "Topic"),
)
ENTITIES_DIR: Final = "Entities"
NAME_FALLBACK: Final = "Unnamed"
KIND_BY_NAME: Final = {info.kind: info for info in KINDS}
KIND_BY_FIELD: Final = {info.field: info for info in KINDS}
KIND_INDEX: Final = {info.kind: i for i, info in enumerate(KINDS)}


def entity_id(kind: EntityKind, key: str) -> str:
    return hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:16]


def _collapse(text: str) -> str:
    return " ".join(scrub_surrogates(text).split())


def _to_db(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _from_db(value: str) -> datetime:
    return datetime.fromisoformat(value)


# -- mentions ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mention:
    episode_key: str
    prompt_hash: str
    created_at: datetime
    kind: EntityKind
    name_key: str  # entity_key(name), never ""
    position: int  # index in the analysis's list for that kind
    name: str
    context: str
    timestamps_s: tuple[float, ...]


def mentions_from(analyses: Sequence[Analysis]) -> list[Mention]:
    """One mention per ``(analysis, kind, entity_key)``. Names with an empty key are skipped;
    repeats inside one list merge like ``analyze.merge`` does (first name and position, first
    non-blank context, sorted union of the timestamps)."""
    found: list[Mention] = []
    for analysis in analyses:
        for info in KINDS:
            slots: dict[str, Mention] = {}
            for position, entity in enumerate(getattr(analysis, info.field)):
                name = _collapse(entity.name)
                key = entity_key(name)
                if not key:
                    continue
                context = _collapse(entity.context)
                previous = slots.get(key)
                if previous is None:
                    slots[key] = Mention(
                        analysis.episode_key,
                        analysis.prompt_hash,
                        analysis.created_at,
                        info.kind,
                        key,
                        position,
                        name,
                        context,
                        tuple(sorted(set(entity.timestamps_s))),
                    )
                else:
                    slots[key] = Mention(
                        previous.episode_key,
                        previous.prompt_hash,
                        previous.created_at,
                        previous.kind,
                        key,
                        previous.position,
                        previous.name,
                        previous.context or context,
                        tuple(sorted({*previous.timestamps_s, *entity.timestamps_s})),
                    )
            found.extend(slots.values())
    return found


# -- the registry --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    key: str
    title: str
    show_title: str
    published_at: datetime | None
    first_shared_at: datetime
    analyses: tuple[str, ...]
    note_path: str | None
    transcript_note_path: str | None
    links_digest: str | None


@dataclass(frozen=True, slots=True)
class EntityRow:
    entity_id: str
    kind: EntityKind | None
    entity_key: str | None
    name: str | None
    note_path: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class RegistryState:
    episodes: Mapping[str, EpisodeRow]
    mentions: tuple[Mention, ...]
    aliases: tuple["AliasEntry", ...]
    entities: Mapping[str, EntityRow]


def _insert_episode(
    conn: sqlite3.Connection,
    episode: Episode,
    analyses: Sequence[Analysis],
    first_shared_at: datetime,
) -> None:
    ordered = sorted(analyses, key=lambda a: (a.created_at, a.prompt_hash))
    conn.execute(
        """
        INSERT INTO episodes (episode_key, title, show_title, published_at, first_shared_at,
                              analyses)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (episode_key) DO UPDATE SET
            title = excluded.title,
            show_title = excluded.show_title,
            published_at = excluded.published_at,
            first_shared_at = excluded.first_shared_at,
            analyses = excluded.analyses
        """,
        (
            episode.key,
            scrub_surrogates(episode.title),
            scrub_surrogates(episode.show_title),
            None if episode.published_at is None else _to_db(episode.published_at),
            _to_db(first_shared_at),
            json.dumps([a.prompt_hash for a in ordered]),
        ),
    )
    conn.execute("DELETE FROM mentions WHERE episode_key = ?", (episode.key,))
    conn.executemany(
        """
        INSERT INTO mentions (episode_key, prompt_hash, created_at, kind, name_key, position,
                              name, context, timestamps)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                m.episode_key,
                m.prompt_hash,
                _to_db(m.created_at),
                m.kind,
                m.name_key,
                m.position,
                m.name,
                m.context,
                json.dumps(list(m.timestamps_s)),
            )
            for m in mentions_from(analyses)
        ],
    )


def _replace_aliases(conn: sqlite3.Connection, aliases: Sequence["AliasEntry"]) -> None:
    conn.execute("DELETE FROM aliases")
    conn.executemany(
        "INSERT OR REPLACE INTO aliases (kind, alias_key, canonical_key, canonical_name) "
        "VALUES (?, ?, ?, ?)",
        [
            (a.kind, a.alias_key, a.canonical_key, scrub_surrogates(a.canonical_name))
            for a in aliases
        ],
    )


class EntityRegistry:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record_episode(
        self,
        episode: Episode,
        analyses: Sequence[Analysis],
        first_shared_at: datetime,
        aliases: Sequence["AliasEntry"] | None,
    ) -> None:
        """Upsert the episode, replace its mentions, and (unless ``aliases`` is None) the
        alias table. Recorded note paths and digests are left alone."""
        with self._db.transaction() as conn:
            _insert_episode(conn, episode, analyses, first_shared_at)
            if aliases is not None:
                _replace_aliases(conn, aliases)

    def reset(
        self,
        episodes: Sequence[tuple[Episode, Sequence[Analysis], datetime]],
        aliases: Sequence["AliasEntry"],
    ) -> None:
        with self._db.transaction() as conn:
            for table in ("mentions", "episodes", "entities", "aliases"):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608  # fixed table names
            for episode, analyses, shared in episodes:
                _insert_episode(conn, episode, analyses, shared)
            _replace_aliases(conn, aliases)

    def state(self) -> RegistryState:
        from earmark.vault.aliases import AliasEntry

        with self._db.connect() as conn:
            conn.execute("BEGIN")
            try:
                episode_rows = conn.execute("SELECT * FROM episodes").fetchall()
                mention_rows = conn.execute(
                    "SELECT * FROM mentions ORDER BY episode_key, prompt_hash, kind, name_key"
                ).fetchall()
                alias_rows = conn.execute(
                    "SELECT * FROM aliases ORDER BY kind, alias_key"
                ).fetchall()
                entity_rows = conn.execute("SELECT * FROM entities").fetchall()
            finally:
                conn.execute("ROLLBACK")
        episodes = {
            r["episode_key"]: EpisodeRow(
                key=r["episode_key"],
                title=r["title"],
                show_title=r["show_title"],
                published_at=None if r["published_at"] is None else _from_db(r["published_at"]),
                first_shared_at=_from_db(r["first_shared_at"]),
                analyses=tuple(json.loads(r["analyses"])),
                note_path=r["note_path"],
                transcript_note_path=r["transcript_note_path"],
                links_digest=r["links_digest"],
            )
            for r in episode_rows
        }
        mentions = tuple(
            Mention(
                episode_key=r["episode_key"],
                prompt_hash=r["prompt_hash"],
                created_at=_from_db(r["created_at"]),
                kind=r["kind"],
                name_key=r["name_key"],
                position=r["position"],
                name=r["name"],
                context=r["context"],
                timestamps_s=tuple(float(t) for t in json.loads(r["timestamps"])),
            )
            for r in mention_rows
        )
        aliases = tuple(
            AliasEntry(r["kind"], r["alias_key"], r["canonical_key"], r["canonical_name"])
            for r in alias_rows
        )
        entities = {
            r["entity_id"]: EntityRow(
                r["entity_id"],
                r["kind"],
                r["entity_key"],
                r["name"],
                r["note_path"],
                r["content_digest"],
            )
            for r in entity_rows
        }
        return RegistryState(episodes, mentions, aliases, entities)

    def record_notes(
        self,
        episodes: Mapping[str, tuple[str | None, str | None, str]],
        entities: Sequence[EntityRow],
    ) -> None:
        """Record each episode's note paths and links digest, and replace the entity rows."""
        with self._db.transaction() as conn:
            conn.executemany(
                "UPDATE episodes SET note_path = ?, transcript_note_path = ?, links_digest = ? "
                "WHERE episode_key = ?",
                [(ep, tr, digest, key) for key, (ep, tr, digest) in episodes.items()],
            )
            conn.execute("DELETE FROM entities")
            conn.executemany(
                "INSERT INTO entities (entity_id, kind, entity_key, name, note_path, "
                "content_digest) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.entity_id,
                        e.kind,
                        e.entity_key,
                        None if e.name is None else scrub_surrogates(e.name),
                        e.note_path,
                        e.content_digest,
                    )
                    for e in entities
                ],
            )


# -- resolution ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodeMention:
    episode_key: str
    context: str
    timestamps_s: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class EntityView:
    entity_id: str
    kind: EntityKind
    key: str  # canonical key
    name: str  # display name
    other_names: tuple[str, ...]
    episodes: tuple[EpisodeMention, ...]  # one per mentioning episode, any order
    first_seen: tuple[str, str, str, str, int]  # sort key of the earliest mention


def canonical(aliases: Sequence["AliasEntry"]) -> Mapping[tuple[EntityKind, str], "AliasEntry"]:
    return {(a.kind, a.alias_key): a for a in aliases}


def canonical_key_of(
    table: Mapping[tuple[EntityKind, str], "AliasEntry"], kind: EntityKind, name_key: str
) -> str:
    entry = table.get((kind, name_key))
    return name_key if entry is None else entry.canonical_key


def resolve_entities(state: RegistryState) -> dict[str, EntityView]:
    """One view per entity with at least one mention, keyed by ``entity_id`` and ordered by
    ``(first_seen, kind index, key)``."""
    table = canonical(state.aliases)
    groups: dict[tuple[EntityKind, str], list[Mention]] = {}
    for m in state.mentions:
        groups.setdefault((m.kind, canonical_key_of(table, m.kind, m.name_key)), []).append(m)

    def seen(m: Mention) -> tuple[str, str, str, str, int]:
        return (
            _to_db(state.episodes[m.episode_key].first_shared_at),
            m.episode_key,
            _to_db(m.created_at),
            m.prompt_hash,
            m.position,
        )

    views: list[EntityView] = []
    for (kind, key), mentions in groups.items():
        earliest = min(mentions, key=seen)
        own = table.get((kind, key))
        display = own.canonical_name if own is not None else earliest.name
        others = sorted(
            {m.name for m in mentions if m.name != display}, key=lambda n: (fold_key(n), n)
        )
        by_episode: dict[str, list[Mention]] = {}
        for m in mentions:
            by_episode.setdefault(m.episode_key, []).append(m)
        episodes: list[EpisodeMention] = []
        for ep_key, ms in sorted(by_episode.items()):
            newest_first = sorted(ms, key=lambda m: (m.position,))
            newest_first.sort(key=lambda m: (m.created_at, m.prompt_hash), reverse=True)
            context = next((m.context for m in newest_first if m.context.strip()), "")
            stamps = tuple(sorted({t for m in ms for t in m.timestamps_s}))
            episodes.append(EpisodeMention(ep_key, context, stamps))
        views.append(
            EntityView(
                entity_id(kind, key),
                kind,
                key,
                display,
                tuple(others),
                tuple(episodes),
                seen(earliest),
            )
        )
    views.sort(key=lambda v: (v.first_seen, KIND_INDEX[v.kind], v.key))
    return {v.entity_id: v for v in views}


# -- rendering -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodeLink:
    path: str  # vault-relative path of the episode note
    title: str
    show_title: str
    day: date
    shared_at: datetime | None = None  # first share; orders episodes of the same day


def entity_fields(view: EntityView) -> list[tuple[str, YamlValue]]:
    return [
        ("earmark", "entity"),
        ("entity_id", view.entity_id),
        ("kind", view.kind),
        ("name", view.name),
    ]


_OLDEST: Final = datetime.min.replace(tzinfo=UTC)


def render_entity_region(
    view: EntityView,
    links: Mapping[str, EpisodeLink],
    *,
    note_link: Callable[[str, str], str],
) -> str:
    e = escape_markdown
    listed = [(m, links[m.episode_key]) for m in view.episodes if m.episode_key in links]
    # newest day first, then newest share first, then episode key (sorts are stable)
    listed.sort(key=lambda p: p[0].episode_key)
    listed.sort(key=lambda p: (p[1].day, p[1].shared_at or _OLDEST), reverse=True)
    n = len(listed)
    info = KIND_BY_NAME[view.kind]
    blocks = [
        f"# {e(view.name)}",
        f"**{info.label}** · mentioned in {n} {'episode' if n == 1 else 'episodes'}",
    ]
    if view.other_names:
        blocks.append("Also mentioned as: " + " · ".join(e(x) for x in view.other_names))
    if not listed:
        blocks.append("*No episode note mentions this entity.*")
    else:
        lines: list[str] = []
        for mention, link in listed:
            line = (
                f"- {link.day.isoformat()} · {note_link(link.path, link.title or 'Untitled')}"
                f" · {e(link.show_title)}"
            )
            if mention.context:
                line += f" — {e(mention.context)}"
            stamps = list(dict.fromkeys(f"[{format_hms(t)}]" for t in mention.timestamps_s))
            if stamps:
                line += " " + " ".join(stamps)
            lines.append(line)
        blocks.append("## Episodes")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def render_orphan_region(
    stem: str,
    merged_into: tuple[str, str] | None,
    *,
    note_link: Callable[[str, str], str],
) -> str:
    if merged_into is None:
        status = "*No episode mentions this entity any more.*"
    else:
        path, name = merged_into
        status = f"Merged into {note_link(path, name)} by aliases.yaml."
    return f"# {escape_markdown(stem)}\n\n{status}\n"
