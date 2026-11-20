"""``VaultStore``: the real ``Store``, writing an episode note and a transcript note, plus
one note per recurring entity and the links between them.

See ``docs/ARCHITECTURE.md`` (Storing, Entities, Data layout) for the contract. Notes are
found again by the ``episode_key`` or ``entity_id`` in their frontmatter, rewritten only
inside their managed region and their own frontmatter keys, and never opened for writing in
place.
"""

import errno
import hashlib
import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Final
from urllib.parse import quote
from zoneinfo import ZoneInfo

from earmark.analyze.render import EntityLinker, render_analysis
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.db import Database, DatabaseSetupError, database_path, open_database
from earmark.errors import EarmarkError, PermanentError, TransientError
from earmark.models import (
    Analysis,
    Entity,
    Episode,
    StoreResult,
    Transcript,
    entity_key,
    format_hms,
    is_episode_key,
    scrub_surrogates,
)
from earmark.vault.aliases import AliasEntry, AliasFileError, read_aliases
from earmark.vault.document import (
    DamagedNote,
    NoteKind,
    YamlValue,
    content_digest,
    new_note,
    note_identity,
    update_note,
)
from earmark.vault.entities import (
    ENTITIES_DIR,
    KIND_BY_FIELD,
    KIND_BY_NAME,
    KIND_INDEX,
    NAME_FALLBACK,
    EntityRegistry,
    EntityRow,
    EntityView,
    EpisodeLink,
    EpisodeRow,
    canonical,
    canonical_key_of,
    entity_fields,
    entity_id,
    render_entity_region,
    render_orphan_region,
    resolve_entities,
)
from earmark.vault.fs import (
    MAX_NOTE_BYTES,
    NoteChanged,
    SymlinkRefused,
    Vault,
    VaultDir,
    vault_lock,
)
from earmark.vault.sanitize import (
    MAX_NAME_BYTES,
    SHOW_BYTES,
    TITLE_BYTES,
    escape_markdown,
    file_component,
    fit_bytes,
    fold_key,
    is_safe_component,
    wikilink_label,
    wikilink_target_ok,
)

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_S: Final = 60.0
SCAN_HEAD_BYTES: Final = 64 * 1024
BACKUPS_DIR: Final = "Backups"
PODCASTS_DIR: Final = "Podcasts"
TRANSCRIPTS_DIR: Final = "transcripts"
_SCAN_DEPTH: Final = 16
_ATTEMPTS: Final = 3
_TRANSCRIPT_SUFFIX: Final = " (transcript)"
_PROMPT_CHARS: Final = 200
RENDER_VERSION: Final = 1  # bump when note rendering changes; part of both digests
_APPLE_URL: Final = re.compile(r"https://[!-~]+")
_PERMANENT_ERRNOS: Final = frozenset({errno.EROFS, errno.EACCES, errno.EPERM, errno.ENAMETOOLONG})
_FULL_ERRNOS: Final = frozenset({errno.ENOSPC, errno.EDQUOT})


def _after_read(vault_rel_path: str) -> None:
    """Called after each read of an existing note, before the merge. A test seam."""


def _local(moment: datetime, tz: tzinfo | None) -> datetime:
    """``moment`` in ``tz``; ``None`` means the system zone (DST-correct)."""
    return moment.astimezone(tz)


def note_stem(episode: Episode, shared_at: datetime, tz: tzinfo | None) -> str:
    when = episode.published_at if episode.published_at is not None else shared_at
    title = file_component(episode.title, max_bytes=TITLE_BYTES, fallback="Untitled")
    return f"{_local(when, tz).date().isoformat()} {title}"


def show_dir_name(episode: Episode) -> str:
    return file_component(episode.show_title, max_bytes=SHOW_BYTES, fallback="Unknown show")


def note_link(vault_rel_path: str, label: str) -> str:
    target = vault_rel_path.removesuffix(".md")
    if wikilink_target_ok(target):
        return f"[[{target}|{wikilink_label(label)}]]"
    return f"[{escape_markdown(label)}]({quote(vault_rel_path, safe='/')})"


def _hms(episode: Episode, transcript: Transcript) -> str | None:
    if transcript.duration_s > 0:
        return format_hms(transcript.duration_s)
    if episode.duration_s:
        return format_hms(episode.duration_s)
    return None


def _published(episode: Episode, tz: tzinfo | None) -> date | None:
    if episode.published_at is None:
        return None
    return _local(episode.published_at, tz).date()


def _wikilink_property(vault_rel_path: str | None) -> str | None:
    if vault_rel_path is None:
        return None
    target = vault_rel_path.removesuffix(".md")
    return f"[[{target}]]" if wikilink_target_ok(target) else None


def _newest_first(analyses: Sequence[Analysis]) -> list[Analysis]:
    return sorted(analyses, key=lambda a: (a.created_at, a.prompt_hash), reverse=True)


def _prompt_property(prompt: str) -> str:
    collapsed = " ".join(prompt.split())
    if len(collapsed) > _PROMPT_CHARS:
        return collapsed[: _PROMPT_CHARS - 1] + "…"
    return collapsed


def episode_fields(
    episode: Episode,
    transcript: Transcript,
    analyses: Sequence[Analysis],
    shared_at: datetime,
    transcript_path: str | None,
    tz: tzinfo | None,
) -> list[tuple[str, YamlValue]]:
    ordered = _newest_first(analyses)
    prompts = tuple(
        dict.fromkeys(_prompt_property(a.prompt) for a in ordered if a.prompt is not None)
    )
    return [
        ("earmark", "episode"),
        ("show", episode.show_title),
        ("episode", episode.title),
        ("date", _published(episode, tz)),
        ("url", episode.url),
        ("guid", episode.guid),
        ("episode_key", episode.key),
        ("duration", _hms(episode, transcript)),
        ("prompts", prompts),
        ("shared_at", _local(shared_at, tz).replace(tzinfo=None, microsecond=0)),
        ("model", ordered[0].model if ordered else None),
        ("transcript", _wikilink_property(transcript_path)),
    ]


def transcript_fields(
    episode: Episode, transcript: Transcript, episode_path: str | None, tz: tzinfo | None
) -> list[tuple[str, YamlValue]]:
    return [
        ("earmark", "transcript"),
        ("show", episode.show_title),
        ("episode", episode.title),
        ("date", _published(episode, tz)),
        ("url", episode.url),
        ("guid", episode.guid),
        ("episode_key", episode.key),
        ("duration", _hms(episode, transcript)),
        ("language", transcript.language),
        ("model", transcript.model),
        ("episode_note", _wikilink_property(episode_path)),
    ]


def render_episode_region(
    episode: Episode,
    transcript: Transcript,
    analyses: Sequence[Analysis],
    transcript_link: str | None,
    tz: tzinfo | None,
    *,
    entity_link: EntityLinker | None = None,
) -> str:
    """The generated region of the episode note.

    ``transcript_link`` is the vault-relative path of the transcript note (or None).
    """
    e = escape_markdown
    blocks = [f"# {e(episode.title) or 'Untitled'}"]
    meta = [f"**Show:** {e(episode.show_title)}"]
    published = _published(episode, tz)
    if published is not None:
        meta.append(f"**Published:** {published.isoformat()}")
    hms = _hms(episode, transcript)
    if hms is not None:
        meta.append(f"**Duration:** {hms}")
    blocks.append(" · ".join(meta))
    links: list[str] = []
    if _APPLE_URL.fullmatch(episode.url) and not any(c in episode.url for c in "()<>"):
        links.append(f"[Apple Podcasts]({episode.url})")
    if transcript_link is not None:
        links.append(note_link(transcript_link, "Transcript"))
    if links:
        blocks.append(" · ".join(links))
    ordered = _newest_first(analyses)
    for a in ordered:
        blocks.append(
            render_analysis(a, heading_level=2, entity_link=entity_link).removesuffix("\n")
        )
        created = _local(a.created_at, tz).strftime("%Y-%m-%d %H:%M")
        callout = [
            "> [!info]- Analysis details",
            f"> - Model: {e(a.model)}",
            f"> - Created: {created}",
            *(f"> - {e(w)}" for w in a.warnings),
        ]
        blocks.append("\n".join(callout))
    if not ordered:
        blocks.append("*No analysis yet.*")
    return "\n\n".join(blocks) + "\n"


def render_transcript_region(
    episode: Episode, transcript: Transcript, episode_link: str | None, tz: tzinfo | None
) -> str:
    """The generated region of the transcript note (``episode_link`` is a vault path)."""
    e = escape_markdown
    blocks = [f"# {e(episode.title) or 'Untitled'} (transcript)"]
    parts: list[str] = []
    if episode_link is not None:
        parts.append(note_link(episode_link, "Episode note"))
    parts.append(f"**Show:** {e(episode.show_title)}")
    published = _published(episode, tz)
    if published is not None:
        parts.append(f"**Published:** {published.isoformat()}")
    blocks.append(" · ".join(parts))
    callout = ["> [!info]- Transcript details", f"> - Model: {e(transcript.model)}"]
    if transcript.language:
        callout.append(f"> - Language: {e(transcript.language)}")
    callout.extend(f"> - {e(w)}" for w in transcript.warnings)
    blocks.append("\n".join(callout))
    for seg in transcript.segments:
        marker = f"[{format_hms(seg.start)}]"
        text = e(seg.text)
        blocks.append(f"{marker} {text}" if text else marker)
    if not transcript.segments:
        blocks.append("*The transcript is empty.*")
    return "\n\n".join(blocks) + "\n"


type Snapshot = dict[tuple[NoteKind, str], list[tuple[str, ...]]]


@dataclass(slots=True)
class _Run:
    """State of one ``store`` or ``rebuild`` call."""

    where: str = ""  # deepest vault-relative path known, for error messages
    warnings: list[str] = field(default_factory=list)
    written: int = 0


@dataclass(slots=True)
class _Located:
    """A note's directory (open) and file name."""

    directory: VaultDir
    dir_parts: tuple[str, ...]
    name: str

    @property
    def parts(self) -> tuple[str, ...]:
        return (*self.dir_parts, self.name)


@dataclass(frozen=True, slots=True)
class _Doc:
    """Everything needed to render the notes of one episode."""

    episode: Episode
    transcript: Transcript
    analyses: tuple[Analysis, ...]
    shared_at: datetime


@dataclass(frozen=True, slots=True)
class RebuildReport:
    episodes: int  # episodes in the rebuilt registry
    skipped: int  # episode directories without episode.json, transcript, share, or analysis
    entities: int  # entity notes managed (resolved + orphans)
    written: int  # notes created or changed
    warnings: tuple[str, ...]


class RebuildRefused(PermanentError):  # noqa: N818  # name fixed by the plan
    """``rebuild`` would drop episode notes that have no files under the data directory."""


class _Dirs:
    """Opens each vault directory once per call, so a large vault does not exhaust file
    descriptors. All of them close when the ``ExitStack`` does."""

    def __init__(self, root: VaultDir, stack: ExitStack) -> None:
        self.root = root
        self._stack = stack
        self._cache: dict[tuple[str, ...], VaultDir] = {}

    def open(self, parts: Sequence[str], *, create: bool) -> VaultDir | None:
        key = tuple(parts)
        if not key:
            return self.root
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        parent = self.open(key[:-1], create=create)
        if parent is None:
            return None
        child = parent.child(key[-1], create=create)
        if child is None:
            return None
        self._stack.enter_context(child)
        self._cache[key] = child
        return child


@dataclass(slots=True)
class _Ctx:
    run: _Run
    dirs: _Dirs
    snapshot: Snapshot
    taken: dict[tuple[str, ...], set[str]] = field(default_factory=dict)


@dataclass(slots=True)
class _Stale:
    """Another episode whose links need regenerating."""

    row: EpisodeRow
    ep_loc: _Located
    tr_loc: _Located


def _scannable(name: str) -> bool:
    if name.startswith(".") or "\\" in name:
        return False
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class VaultStore:
    """The ``vault`` ``Store``: episode and transcript notes, entity notes, and their links."""

    def __init__(
        self,
        vault_dir: Path,
        subdir: str,
        *,
        data_dir: Path,
        tz: tzinfo | None = None,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        now: Callable[[], float] = time.time,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._vault = Vault(vault_dir, subdir)
        self._data_dir = data_dir
        self._tz = tz
        self._lock_timeout_s = lock_timeout_s
        self._now = now
        self._artifacts = artifacts if artifacts is not None else ArtifactStore(data_dir)
        self._registry_cache: EntityRegistry | None = None

    # -- public ---------------------------------------------------------------------------

    def store(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
        /,
    ) -> StoreResult:
        if transcript.episode_key != episode.key or any(
            a.episode_key != episode.key for a in analyses
        ):
            raise PermanentError("store received data for another episode")
        run = _Run()
        doc = _Doc(episode, transcript, tuple(analyses), shared_at)
        return self._guard(run, lambda: self._store(run, doc))

    def rebuild(self, *, prune: bool = False) -> RebuildReport:
        """Empty the registry, rebuild it from the artifacts, and regenerate every note."""
        run = _Run()
        return self._guard(run, lambda: self._rebuild(run, prune=prune))

    # -- errors ---------------------------------------------------------------------------

    def _guard[T](self, run: _Run, fn: Callable[[], T]) -> T:
        try:
            with vault_lock(self._data_dir / "vault.lock", timeout_s=self._lock_timeout_s):
                return fn()
        except EarmarkError:
            raise
        except OSError as exc:
            raise self._map_oserror(exc, run) from exc
        except DatabaseSetupError as exc:
            reason = str(exc).replace(str(self._data_dir), "EARMARK_DATA_DIR")
            raise PermanentError(f"the entity registry database cannot be used: {reason}") from exc
        except sqlite3.OperationalError as exc:
            raise TransientError(
                f"the entity registry is busy or unavailable ({type(exc).__name__})"
            ) from exc
        except sqlite3.Error as exc:
            raise TransientError(f"entity registry error: {type(exc).__name__}") from exc

    def _map_oserror(self, exc: OSError, run: _Run) -> EarmarkError:
        name = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        if isinstance(exc, PermissionError) or exc.errno in _PERMANENT_ERRNOS:
            rel = run.where or "/".join(self._vault.subdir_parts)
            return PermanentError(
                f"cannot write to the vault (check EARMARK_VAULT_DIR permissions): {name} at {rel}"
            )
        if exc.errno in _FULL_ERRNOS:
            return TransientError(f"the vault is full: {name}")
        return TransientError(f"vault write failed: {name}")

    def _path(self, parts: Sequence[str]) -> str:
        return "/".join((*self._vault.subdir_parts, *parts))

    def _registry(self) -> EntityRegistry:
        if self._registry_cache is None:
            db: Database = open_database(database_path(self._data_dir))
            self._registry_cache = EntityRegistry(db)
        return self._registry_cache

    # -- store and rebuild ----------------------------------------------------------------

    def _store(self, run: _Run, doc: _Doc) -> StoreResult:
        registry = self._registry()
        with ExitStack() as stack:
            root = stack.enter_context(self._vault.open_subdir())
            run.where = self._path(())
            ctx = _Ctx(run, _Dirs(root, stack), self._scan(root))
            aliases = self._read_aliases(root, run)
            registry.record_episode(doc.episode, doc.analyses, doc.shared_at, aliases)
            paths, _ = self._sync(ctx, registry, [doc])
        ep_path, tr_path = paths[doc.episode.key]
        return StoreResult(
            note_path=ep_path,
            transcript_note_path=tr_path,
            warnings=tuple(dict.fromkeys(run.warnings)),
        )

    def _rebuild(self, run: _Run, *, prune: bool) -> RebuildReport:
        registry = self._registry()
        with ExitStack() as stack:
            root = stack.enter_context(self._vault.open_subdir())
            run.where = self._path(())
            ctx = _Ctx(run, _Dirs(root, stack), self._scan(root))
            parsed = read_aliases(root)  # AliasFileError propagates: nothing has changed
            entries: tuple[AliasEntry, ...] = ()
            if parsed is not None:
                entries = parsed.entries
                run.warnings.extend(parsed.problems)
            docs, skipped = self._collect_docs(run)
            kept = {d.episode.key for d in docs}
            unmatched = sum(
                1 for kind, key in ctx.snapshot if kind == "episode" and key not in kept
            )
            if unmatched and not prune:
                raise RebuildRefused(
                    f"{unmatched} episode notes in the vault have no usable files under "
                    "EARMARK_DATA_DIR; rebuilding would drop them from every entity note. "
                    "Restore the data directory, or pass --prune"
                )
            registry.reset([(d.episode, d.analyses, d.shared_at) for d in docs], entries)
            _, entity_count = self._sync(ctx, registry, docs)
        return RebuildReport(
            episodes=len(docs),
            skipped=skipped,
            entities=entity_count,
            written=run.written,
            warnings=tuple(dict.fromkeys(run.warnings)),
        )

    def _collect_docs(self, run: _Run) -> tuple[list[_Doc], int]:
        root = self._artifacts.root
        names = (
            sorted(p.name for p in root.iterdir() if is_episode_key(p.name) and p.is_dir())
            if root.is_dir()
            else []
        )
        docs: list[_Doc] = []
        skipped = 0
        for key in names:
            try:
                episode = self._artifacts.read_episode(key)
                transcript = self._artifacts.read_transcript(key)
                share = self._artifacts.read_share(key)
                analyses = self._artifacts.list_analyses(key)
            except PermanentError:
                run.warnings.append(
                    f"episode {key}: unreadable files under EARMARK_DATA_DIR; skipped"
                )
                skipped += 1
                continue
            if episode is None or transcript is None or share is None or not analyses:
                skipped += 1
                continue
            docs.append(_Doc(episode, transcript, tuple(analyses), share.first_shared_at))
        docs.sort(key=lambda d: (d.shared_at, d.episode.key))
        return docs, skipped

    def _load_doc(self, row: EpisodeRow) -> _Doc | None:
        """Re-read an episode from its artifacts; None when they are missing or unreadable."""
        try:
            episode = self._artifacts.read_episode(row.key)
            transcript = self._artifacts.read_transcript(row.key)
            analyses = [self._artifacts.read_analysis(row.key, h) for h in row.analyses]
        except (PermanentError, OSError, ValueError):
            return None
        found = [a for a in analyses if a is not None]
        if episode is None or transcript is None or len(found) != len(analyses):
            return None
        return _Doc(episode, transcript, tuple(found), row.first_shared_at)

    def _read_aliases(self, root: VaultDir, run: _Run) -> tuple[AliasEntry, ...] | None:
        try:
            parsed = read_aliases(root)
        except AliasFileError as exc:
            run.warnings.append(f"{exc.message}; the aliases loaded before are kept")
            return None
        if parsed is None:
            return ()
        run.warnings.extend(parsed.problems)
        return parsed.entries

    def _sync(
        self, ctx: _Ctx, registry: EntityRegistry, docs: Sequence[_Doc]
    ) -> tuple[dict[str, tuple[str, str]], int]:
        """Settle every path, then write: targets' transcripts, entity notes, orphans, other
        episodes whose links went stale, and finally the targets' episode notes. Returns the
        targets' ``(episode path, transcript path)`` and the number of entity rows."""
        run = ctx.run
        state = registry.state()
        views = resolve_entities(state)
        table = canonical(state.aliases)
        targets = {d.episode.key: d for d in docs}

        # -- settle ---------------------------------------------------------------------
        t_locs: dict[str, tuple[_Located, _Located]] = {}
        for doc in docs:
            episode = doc.episode
            stem = note_stem(episode, doc.shared_at, self._tz)
            show = show_dir_name(episode)
            candidate = (PODCASTS_DIR, show, f"{stem}.md")
            ep_loc = self._settle_episode(ctx, episode, candidate, stem, show)
            t_locs[episode.key] = (ep_loc, self._settle_transcript(ctx, episode, ep_loc))

        e_locs: dict[str, _Located] = {}
        fresh: set[str] = set()
        for view in views.values():
            loc, is_new = self._settle_entity(ctx, view)
            e_locs[view.entity_id] = loc
            if is_new:
                fresh.add(view.entity_id)
        paths_by_id = {i: self._path(loc.parts) for i, loc in e_locs.items()}

        orphans: dict[str, _Located] = {}
        for (kind, ident), found in sorted(ctx.snapshot.items()):
            if kind == "entity" and ident not in views:
                verified = self._verify(ctx, min(found, key="/".join), "entity", ident)
                if verified is not None:
                    orphans[ident] = verified

        ep_paths = {key: self._path(locs[0].parts) for key, locs in t_locs.items()}
        mention_keys: dict[str, set[tuple[str, str]]] = {}
        for m in state.mentions:
            mention_keys.setdefault(m.episode_key, set()).add((m.kind, m.name_key))

        def links_digest(key: str, ep_path: str, tr_path: str | None) -> str:
            links = sorted(
                {
                    (KIND_BY_NAME[kind].field, name_key, paths_by_id[ident])  # type: ignore[index]
                    for kind, name_key in mention_keys.get(key, ())
                    if (ident := entity_id(kind, canonical_key_of(table, kind, name_key)))  # type: ignore[arg-type]
                    in paths_by_id
                }
            )
            payload = {
                "v": RENDER_VERSION,
                "note": ep_path,
                "transcript": tr_path,
                "links": [list(item) for item in links],
            }
            text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        stale: list[_Stale] = []
        stale_digest: dict[str, str] = {}
        for key, row in sorted(
            state.episodes.items(), key=lambda kv: (kv[1].first_shared_at, kv[0])
        ):
            if key in targets:
                continue
            ep_found = ctx.snapshot.get(("episode", key))
            if not ep_found:
                continue  # its note is gone: not recreated here
            ep_parts = self._choose(ep_found, row.note_path)
            tr_found = ctx.snapshot.get(("transcript", key))
            tr_parts = self._choose(tr_found, row.transcript_note_path) if tr_found else None
            ep_path = self._path(ep_parts)
            ep_paths[key] = ep_path
            tr_known = None if tr_parts is None else self._path(tr_parts)
            if tr_known is not None and links_digest(key, ep_path, tr_known) == row.links_digest:
                continue
            other_ep = self._verify(ctx, ep_parts, "episode", key)
            if other_ep is None:
                continue
            other_tr = self._settle_other_transcript(ctx, key, tr_parts, other_ep)
            stale.append(_Stale(row, other_ep, other_tr))
            stale_digest[key] = links_digest(key, ep_path, self._path(other_tr.parts))

        links = {
            key: EpisodeLink(
                path=path,
                title=state.episodes[key].title,
                show_title=state.episodes[key].show_title,
                day=_local(
                    state.episodes[key].published_at or state.episodes[key].first_shared_at,
                    self._tz,
                ).date(),
                shared_at=state.episodes[key].first_shared_at,
            )
            for key, path in ep_paths.items()
            if key in state.episodes
        }

        def linker(field_name: str, entity: Entity) -> str | None:
            info = KIND_BY_FIELD.get(field_name)
            key = entity_key(scrub_surrogates(entity.name))
            if info is None or not key:
                return None
            ident = entity_id(info.kind, canonical_key_of(table, info.kind, key))
            path = paths_by_id.get(ident)
            return None if path is None else note_link(path, entity.name)

        # -- stale temps ----------------------------------------------------------------
        now = self._now()
        locs: list[_Located] = [loc for pair in t_locs.values() for loc in pair]
        locs += [*e_locs.values(), *orphans.values()]
        for s in stale:
            locs += [s.ep_loc, s.tr_loc]
        cleaned: set[tuple[str, ...]] = set()
        for loc in locs:
            if loc.dir_parts not in cleaned:
                cleaned.add(loc.dir_parts)
                loc.directory.remove_stale_temps(now=now)

        # -- write ----------------------------------------------------------------------
        for doc in docs:
            ep_loc, tr_loc = t_locs[doc.episode.key]
            self._write_transcript(ctx, doc, self._path(ep_loc.parts), tr_loc)

        entity_rows: list[EntityRow] = []
        tags: list[tuple[str, YamlValue]] = [("tags", ("earmark", "entity"))]
        for view in sorted(views.values(), key=lambda v: (KIND_INDEX[v.kind], v.entity_id)):
            loc = e_locs[view.entity_id]
            path = paths_by_id[view.entity_id]
            owned = entity_fields(view)
            region = render_entity_region(view, links, note_link=note_link)
            digest = content_digest(owned, f"v{RENDER_VERSION}\n{region}")
            recorded = state.entities.get(view.entity_id)
            if (
                view.entity_id in fresh
                or recorded is None
                or recorded.content_digest != digest
                or recorded.note_path != path
            ):
                self._write_note(ctx, loc, "entity", view.entity_id, owned, tags, region)
            entity_rows.append(
                EntityRow(view.entity_id, view.kind, view.key, view.name, path, digest)
            )

        for ident, loc in sorted(orphans.items()):
            path = self._path(loc.parts)
            merged: tuple[str, str] | None = None
            kind_value = key_value = None
            for alias in state.aliases:
                if alias.alias_key != alias.canonical_key and (
                    entity_id(alias.kind, alias.alias_key) == ident
                ):
                    kind_value, key_value = alias.kind, alias.alias_key
                    target = views.get(entity_id(alias.kind, alias.canonical_key))
                    if target is not None:
                        merged = (paths_by_id[target.entity_id], target.name)
                    break
            region = render_orphan_region(loc.name.removesuffix(".md"), merged, note_link=note_link)
            digest = content_digest((), region)
            recorded = state.entities.get(ident)
            if recorded is None or recorded.content_digest != digest or recorded.note_path != path:
                self._write_note(ctx, loc, "entity", ident, [], [], region, create=False)
            entity_rows.append(EntityRow(ident, kind_value, key_value, None, path, digest))

        recorded_episodes: dict[str, tuple[str | None, str | None, str]] = {}
        for s in stale:
            key = s.row.key
            ep_path = self._path(s.ep_loc.parts)
            tr_path = self._path(s.tr_loc.parts)
            other = self._load_doc(s.row)
            if other is None:
                run.warnings.append(
                    f"{ep_path}: could not update its links: the episode's files under "
                    "EARMARK_DATA_DIR are missing or unreadable"
                )
            else:
                self._write_transcript(ctx, other, ep_path, s.tr_loc)
                self._write_episode(ctx, other, s.ep_loc, tr_path, linker, create=False)
            recorded_episodes[key] = (ep_path, tr_path, stale_digest[key])

        results: dict[str, tuple[str, str]] = {}
        for doc in docs:
            key = doc.episode.key
            ep_loc, tr_loc = t_locs[key]
            ep_path = self._path(ep_loc.parts)
            tr_path = self._path(tr_loc.parts)
            self._write_episode(ctx, doc, ep_loc, tr_path, linker, create=True)
            recorded_episodes[key] = (ep_path, tr_path, links_digest(key, ep_path, tr_path))
            results[key] = (ep_path, tr_path)

        registry.record_notes(recorded_episodes, entity_rows)
        return results, len(entity_rows)

    # -- rendering one episode ------------------------------------------------------------

    def _write_transcript(self, ctx: _Ctx, doc: _Doc, episode_path: str, tr_loc: _Located) -> bool:
        region = render_transcript_region(doc.episode, doc.transcript, episode_path, self._tz)
        owned = transcript_fields(doc.episode, doc.transcript, episode_path, self._tz)
        return self._write_note(
            ctx,
            tr_loc,
            "transcript",
            doc.episode.key,
            owned,
            [("tags", ("earmark", "transcript"))],
            region,
        )

    def _write_episode(
        self,
        ctx: _Ctx,
        doc: _Doc,
        ep_loc: _Located,
        transcript_path: str,
        linker: EntityLinker,
        *,
        create: bool,
    ) -> bool:
        region = render_episode_region(
            doc.episode,
            doc.transcript,
            doc.analyses,
            transcript_path,
            self._tz,
            entity_link=linker,
        )
        owned = episode_fields(
            doc.episode, doc.transcript, doc.analyses, doc.shared_at, transcript_path, self._tz
        )
        return self._write_note(
            ctx,
            ep_loc,
            "episode",
            doc.episode.key,
            owned,
            [("tags", ("earmark", "podcast"))],
            region,
            create=create,
        )

    # -- scanning -------------------------------------------------------------------------

    def _scan(self, root: VaultDir) -> Snapshot:
        """Paths (relative to the subdir) of every note with an earmark identity."""
        found: Snapshot = {}

        def walk(directory: VaultDir, parts: tuple[str, ...], depth: int) -> None:
            try:
                entries = directory.entries()
            except PermissionError:
                logger.warning("cannot list %s; skipped by the note scan", self._path(parts))
                return
            for entry in sorted(entries, key=lambda e: e.name):
                name = entry.name
                if not _scannable(name):
                    continue
                if entry.kind == "dir":
                    if depth == 0 and fold_key(name) == fold_key(BACKUPS_DIR):
                        continue
                    if depth >= _SCAN_DEPTH:
                        continue
                    try:
                        sub = directory.child(name, create=False)
                    except (SymlinkRefused, PermissionError):
                        logger.warning("cannot open %s; skipped by the note scan", name)
                        continue
                    if sub is not None:
                        with sub:
                            walk(sub, (*parts, name), depth + 1)
                elif entry.kind == "file" and name.endswith(".md"):
                    try:
                        head = directory.read_head(name, n=SCAN_HEAD_BYTES)
                    except (SymlinkRefused, OSError):
                        continue
                    if head is None:
                        continue
                    identity = note_identity(head[0].decode("utf-8", "surrogateescape"))
                    if identity is not None:
                        found.setdefault(identity, []).append((*parts, name))

        walk(root, (), 0)
        return found

    def _pick(
        self,
        kind: str,
        paths: list[tuple[str, ...]],
        candidate: tuple[str, ...],
        key: str,
        run: _Run,
        subject: str = "episode",
    ) -> tuple[str, ...] | None:
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]
        wanted = fold_key("/".join(candidate))
        chosen = next((p for p in sorted(paths) if fold_key("/".join(p)) == wanted), None)
        if chosen is None:
            chosen = min(paths, key=lambda p: "/".join(p))
        run.warnings.append(
            f"found {len(paths)} notes for {subject} {key}; updated {self._path(chosen)}"
        )
        logger.warning("found %d %s notes for %s %s", len(paths), kind, subject, key)
        return chosen

    def _choose(self, paths: list[tuple[str, ...]], recorded: str | None) -> tuple[str, ...]:
        """The recorded path if it is among ``paths``, else the smallest. No warning."""
        match = next((p for p in paths if self._path(p) == recorded), None)
        return match if match is not None else min(paths, key="/".join)

    def _verify(
        self, ctx: _Ctx, parts: tuple[str, ...], kind: NoteKind, key: str
    ) -> _Located | None:
        """Re-open a scanned note and check that it still carries the identity."""
        directory = ctx.dirs.open(parts[:-1], create=False)
        if directory is None:
            return None
        head = directory.read_head(parts[-1], n=SCAN_HEAD_BYTES)
        if head is None:
            return None
        if note_identity(head[0].decode("utf-8", "surrogateescape")) != (kind, key):
            return None
        return _Located(directory, parts[:-1], parts[-1])

    def _free_name(
        self,
        ctx: _Ctx,
        directory: VaultDir,
        base: str,
        key: str,
        dir_parts: Sequence[str],
        subject: str,
    ) -> str:
        taken = ctx.taken.setdefault(tuple(dir_parts), set())
        used = {fold_key(e.name) for e in directory.entries()} | taken
        for extra in ("", f" ({key[:8]})", f" ({key})"):
            tail = f"{extra}.md"
            name = fit_bytes(base, MAX_NAME_BYTES - len(tail.encode())) + tail
            if not is_safe_component(name):
                name = file_component(base, max_bytes=MAX_NAME_BYTES - len(tail), fallback="note")
                name += tail
            if fold_key(name) not in used:
                taken.add(fold_key(name))
                return name
        raise PermanentError(
            f"no free note name for {subject} {key} in {self._path(dir_parts) or '.'}"
        )

    def _settle_episode(
        self,
        ctx: _Ctx,
        episode: Episode,
        candidate: tuple[str, ...],
        stem: str,
        show: str,
    ) -> _Located:
        found = ctx.snapshot.get(("episode", episode.key), [])
        picked = self._pick("episode", found, candidate, episode.key, ctx.run)
        if picked is not None:
            loc = self._verify(ctx, picked, "episode", episode.key)
            if loc is not None:
                return loc
        directory = ctx.dirs.open((PODCASTS_DIR, show), create=True)
        if directory is None:  # pragma: no cover  # create=True never returns None
            raise TransientError("the vault changed while it was being written; try again")
        dir_parts = directory.rel.parts  # the names on disk, which may differ in case
        ctx.run.where = self._path(dir_parts)
        name = self._free_name(ctx, directory, stem, episode.key, dir_parts, "episode")
        return _Located(directory, dir_parts, name)

    def _settle_transcript(self, ctx: _Ctx, episode: Episode, ep_loc: _Located) -> _Located:
        candidate = (*ep_loc.dir_parts, TRANSCRIPTS_DIR, self._transcript_name(ep_loc.name))
        found = ctx.snapshot.get(("transcript", episode.key), [])
        picked = self._pick("transcript", found, candidate, episode.key, ctx.run)
        if picked is not None:
            loc = self._verify(ctx, picked, "transcript", episode.key)
            if loc is not None:
                return loc
        return self._allocate_transcript(ctx, episode.key, ep_loc)

    def _settle_other_transcript(
        self, ctx: _Ctx, key: str, parts: tuple[str, ...] | None, ep_loc: _Located
    ) -> _Located:
        if parts is not None:
            loc = self._verify(ctx, parts, "transcript", key)
            if loc is not None:
                return loc
        return self._allocate_transcript(ctx, key, ep_loc)

    def _allocate_transcript(self, ctx: _Ctx, key: str, ep_loc: _Located) -> _Located:
        directory = ctx.dirs.open((*ep_loc.dir_parts, TRANSCRIPTS_DIR), create=True)
        if directory is None:  # pragma: no cover  # create=True never returns None
            raise TransientError("the vault changed while it was being written; try again")
        dir_parts = directory.rel.parts
        ctx.run.where = self._path(dir_parts)
        base = self._transcript_name(ep_loc.name).removesuffix(".md")
        name = self._free_name(ctx, directory, base, key, dir_parts, "episode")
        return _Located(directory, dir_parts, name)

    def _settle_entity(self, ctx: _Ctx, view: EntityView) -> tuple[_Located, bool]:
        """The entity's note, and whether it has to be created."""
        info = KIND_BY_NAME[view.kind]
        stem = file_component(view.name, max_bytes=TITLE_BYTES, fallback=NAME_FALLBACK)
        candidate = (ENTITIES_DIR, info.folder, f"{stem}.md")
        found = ctx.snapshot.get(("entity", view.entity_id), [])
        picked = self._pick("entity", found, candidate, view.entity_id, ctx.run, "entity")
        if picked is not None:
            loc = self._verify(ctx, picked, "entity", view.entity_id)
            if loc is not None:
                return loc, False
        directory = ctx.dirs.open((ENTITIES_DIR, info.folder), create=True)
        if directory is None:  # pragma: no cover  # create=True never returns None
            raise TransientError("the vault changed while it was being written; try again")
        dir_parts = directory.rel.parts
        ctx.run.where = self._path(dir_parts)
        name = self._free_name(ctx, directory, stem, view.entity_id, dir_parts, "entity")
        return _Located(directory, dir_parts, name), True

    @staticmethod
    def _transcript_name(episode_name: str) -> str:
        stem = episode_name.removesuffix(".md")
        budget = MAX_NAME_BYTES - len(_TRANSCRIPT_SUFFIX) - len(".md")
        name = fit_bytes(stem, budget) + _TRANSCRIPT_SUFFIX + ".md"
        if not is_safe_component(name):
            safe = file_component(stem, max_bytes=budget, fallback="note")
            name = safe + _TRANSCRIPT_SUFFIX + ".md"
        return name

    # -- writing --------------------------------------------------------------------------

    def _write_note(
        self,
        ctx: _Ctx,
        loc: _Located,
        kind: NoteKind,
        key: str,
        owned: Sequence[tuple[str, YamlValue]],
        seeds: Sequence[tuple[str, YamlValue]],
        region: str,
        *,
        create: bool = True,
    ) -> bool:
        """Create or update one note; True when the file was written."""
        run = ctx.run
        path = self._path(loc.parts)
        run.where = path
        directory = loc.directory
        for attempt in range(_ATTEMPTS):
            got = directory.read(loc.name, max_bytes=MAX_NOTE_BYTES)
            try:
                if got is None:
                    if not create:
                        return False
                    text = new_note(owned, seeds, region)
                    directory.write_atomic(
                        loc.name, text.encode("utf-8", "surrogateescape"), expect=None
                    )
                    run.written += 1
                    return True
                raw, stamp = got
                _after_read(path)
                existing = raw.decode("utf-8", "surrogateescape")
                if note_identity(existing) != (kind, key):
                    raise NoteChanged(f"{path} no longer carries this note's frontmatter")
                try:
                    updated = update_note(existing, owned, seeds, region)
                except DamagedNote:
                    run.warnings.append(
                        f"{path}: the earmark markers are missing or damaged, so the note was "
                        "not updated; restore the <!-- earmark:begin --> and "
                        "<!-- earmark:end --> lines, or delete the note to regenerate it"
                    )
                    return False
                if updated.text == existing:
                    return False
                if updated.edited:
                    backup = self._backup(ctx.dirs.root, loc, raw, updated.old_digest)
                    run.warnings.append(
                        f"{path}: text inside the earmark markers was edited by hand and has "
                        f"been replaced; the previous version was saved as {backup}"
                    )
                directory.write_atomic(
                    loc.name, updated.text.encode("utf-8", "surrogateescape"), expect=stamp
                )
                run.written += 1
                return True
            except NoteChanged:
                if attempt == _ATTEMPTS - 1:
                    raise
        return False  # pragma: no cover  # the loop returns or raises

    def _backup(self, root: VaultDir, loc: _Located, raw: bytes, digest: str) -> str:
        suffix = f" ({digest}).md"
        stem = file_component(
            loc.name.removesuffix(".md"), max_bytes=MAX_NAME_BYTES - len(suffix), fallback="note"
        )
        name = stem + suffix
        backups = root.child(BACKUPS_DIR, create=True)
        if backups is None:  # pragma: no cover  # create=True never returns None
            raise TransientError("the vault changed while it was being written; try again")
        with backups:
            if fold_key(name) not in {fold_key(e.name) for e in backups.entries()}:
                backups.write_atomic(name, raw, expect=None)
        return self._path((BACKUPS_DIR, name))


def build_vault_store(settings: Settings) -> VaultStore:
    """Build the store from settings. Creates nothing in the vault or the data dir."""
    vault_dir = settings.vault_dir
    if vault_dir is None:
        raise ConfigError(["EARMARK_VAULT_DIR: required when EARMARK_STORE_BACKEND=vault"])
    if not vault_dir.exists():
        raise ConfigError([f"EARMARK_VAULT_DIR: {vault_dir} does not exist"])
    if not vault_dir.is_dir():
        raise ConfigError([f"EARMARK_VAULT_DIR: {vault_dir} is not a directory"])
    tz = ZoneInfo(settings.tz) if settings.tz else None
    return VaultStore(
        vault_dir.absolute(), settings.vault_subdir, data_dir=settings.data_dir, tz=tz
    )
