"""``VaultStore``: the real ``Store``, writing an episode note and a transcript note.

See ``docs/ARCHITECTURE.md`` (Storing, Data layout) for the contract. Notes are found again
by the ``episode_key`` in their frontmatter, rewritten only inside their managed region and
their own frontmatter keys, and never opened for writing in place.
"""

import errno
import logging
import re
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

from earmark.analyze.render import render_analysis
from earmark.config import ConfigError, Settings
from earmark.errors import EarmarkError, PermanentError, TransientError
from earmark.models import Analysis, Episode, StoreResult, Transcript, format_hms
from earmark.vault.document import (
    DamagedNote,
    YamlValue,
    new_note,
    note_identity,
    update_note,
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
        return f"[[{target}|{label}]]"
    return f"[{label}]({quote(vault_rel_path, safe='/')})"


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
        blocks.append(render_analysis(a, heading_level=2).removesuffix("\n"))
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


@dataclass(slots=True)
class _Run:
    """State of one ``store`` call."""

    where: str = ""  # deepest vault-relative path known, for error messages
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Located:
    """A note's directory (open) and file name."""

    directory: VaultDir
    dir_parts: tuple[str, ...]
    name: str

    @property
    def parts(self) -> tuple[str, ...]:
        return (*self.dir_parts, self.name)


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
    """The ``vault`` ``Store``: writes the episode note and the transcript note."""

    def __init__(
        self,
        vault_dir: Path,
        subdir: str,
        *,
        data_dir: Path,
        tz: tzinfo | None = None,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._vault = Vault(vault_dir, subdir)
        self._data_dir = data_dir
        self._tz = tz
        self._lock_timeout_s = lock_timeout_s
        self._now = now

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
        try:
            with vault_lock(self._data_dir / "vault.lock", timeout_s=self._lock_timeout_s):
                return self._store(run, episode, transcript, analyses, shared_at)
        except EarmarkError:
            raise
        except OSError as exc:
            raise self._map_oserror(exc, run) from exc

    # -- errors ---------------------------------------------------------------------------

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

    # -- the store ------------------------------------------------------------------------

    def _store(
        self,
        run: _Run,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
    ) -> StoreResult:
        with ExitStack() as stack:
            root = stack.enter_context(self._vault.open_subdir())
            run.where = self._path(())
            found = self._scan(root, episode.key, run)
            stem = note_stem(episode, shared_at, self._tz)
            show = show_dir_name(episode)
            candidate = (PODCASTS_DIR, show, f"{stem}.md")
            ep_loc = self._settle_episode(root, stack, run, episode, found, candidate, stem, show)
            tr_loc = self._settle_transcript(root, stack, run, episode, found, ep_loc, candidate)
            now = self._now()
            for loc in (ep_loc, tr_loc):
                loc.directory.remove_stale_temps(now=now)
            ep_path = self._path(ep_loc.parts)
            tr_path = self._path(tr_loc.parts)

            tr_region = render_transcript_region(episode, transcript, ep_path, self._tz)
            tr_owned = transcript_fields(episode, transcript, ep_path, self._tz)
            self._write_note(
                run,
                root,
                tr_loc,
                "transcript",
                episode.key,
                tr_owned,
                [("tags", ("earmark", "transcript"))],
                tr_region,
            )
            ep_region = render_episode_region(episode, transcript, analyses, tr_path, self._tz)
            ep_owned = episode_fields(episode, transcript, analyses, shared_at, tr_path, self._tz)
            self._write_note(
                run,
                root,
                ep_loc,
                "episode",
                episode.key,
                ep_owned,
                [("tags", ("earmark", "podcast"))],
                ep_region,
            )
        return StoreResult(
            note_path=ep_path,
            transcript_note_path=tr_path,
            warnings=tuple(dict.fromkeys(run.warnings)),
        )

    # -- scanning -------------------------------------------------------------------------

    def _scan(self, root: VaultDir, key: str, run: _Run) -> dict[str, list[tuple[str, ...]]]:
        """Paths (relative to the subdir) of notes whose frontmatter names ``key``."""
        found: dict[str, list[tuple[str, ...]]] = {"episode": [], "transcript": []}

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
                    if identity is not None and identity[1] == key:
                        found[identity[0]].append((*parts, name))

        walk(root, (), 0)
        return found

    def _pick(
        self,
        kind: str,
        paths: list[tuple[str, ...]],
        candidate: tuple[str, ...],
        key: str,
        run: _Run,
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
            f"found {len(paths)} notes for episode {key}; updated {self._path(chosen)}"
        )
        logger.warning("found %d %s notes for episode %s", len(paths), kind, key)
        return chosen

    def _open_dirs(
        self, root: VaultDir, stack: ExitStack, parts: Sequence[str], *, create: bool
    ) -> VaultDir | None:
        current = root
        for part in parts:
            nxt = current.child(part, create=create)
            if nxt is None:
                return None
            stack.enter_context(nxt)
            current = nxt
        return current

    def _verify(
        self,
        root: VaultDir,
        stack: ExitStack,
        parts: tuple[str, ...],
        kind: str,
        key: str,
    ) -> _Located | None:
        """Re-open a scanned note and check that it still carries the identity."""
        directory = self._open_dirs(root, stack, parts[:-1], create=False)
        if directory is None:
            return None
        head = directory.read_head(parts[-1], n=SCAN_HEAD_BYTES)
        if head is None:
            return None
        if note_identity(head[0].decode("utf-8", "surrogateescape")) != (kind, key):
            return None
        return _Located(directory, parts[:-1], parts[-1])

    def _free_name(self, directory: VaultDir, base: str, key: str, dir_parts: Sequence[str]) -> str:
        taken = {fold_key(e.name) for e in directory.entries()}
        for extra in ("", f" ({key[:8]})", f" ({key})"):
            tail = f"{extra}.md"
            name = fit_bytes(base, MAX_NAME_BYTES - len(tail.encode())) + tail
            if not is_safe_component(name):
                name = file_component(base, max_bytes=MAX_NAME_BYTES - len(tail), fallback="note")
                name += tail
            if fold_key(name) not in taken:
                return name
        raise PermanentError(
            f"no free note name for episode {key} in {self._path(dir_parts) or '.'}"
        )

    def _settle_episode(
        self,
        root: VaultDir,
        stack: ExitStack,
        run: _Run,
        episode: Episode,
        found: dict[str, list[tuple[str, ...]]],
        candidate: tuple[str, ...],
        stem: str,
        show: str,
    ) -> _Located:
        picked = self._pick("episode", found["episode"], candidate, episode.key, run)
        if picked is not None:
            loc = self._verify(root, stack, picked, "episode", episode.key)
            if loc is not None:
                return loc
        directory = self._open_dirs(root, stack, (PODCASTS_DIR, show), create=True)
        if directory is None:  # pragma: no cover  # create=True never returns None
            raise TransientError("the vault changed while it was being written; try again")
        dir_parts = directory.rel.parts  # the names on disk, which may differ in case
        run.where = self._path(dir_parts)
        name = self._free_name(directory, stem, episode.key, dir_parts)
        return _Located(directory, dir_parts, name)

    def _settle_transcript(
        self,
        root: VaultDir,
        stack: ExitStack,
        run: _Run,
        episode: Episode,
        found: dict[str, list[tuple[str, ...]]],
        ep_loc: _Located,
        ep_candidate: tuple[str, ...],
    ) -> _Located:
        candidate = (*ep_loc.dir_parts, TRANSCRIPTS_DIR, self._transcript_name(ep_loc.name))
        picked = self._pick("transcript", found["transcript"], candidate, episode.key, run)
        if picked is not None:
            loc = self._verify(root, stack, picked, "transcript", episode.key)
            if loc is not None:
                return loc
        directory = self._open_dirs(root, stack, (*ep_loc.dir_parts, TRANSCRIPTS_DIR), create=True)
        if directory is None:  # pragma: no cover  # create=True never returns None
            raise TransientError("the vault changed while it was being written; try again")
        dir_parts = directory.rel.parts
        run.where = self._path(dir_parts)
        base = self._transcript_name(ep_loc.name).removesuffix(".md")
        name = self._free_name(directory, base, episode.key, dir_parts)
        return _Located(directory, dir_parts, name)

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
        run: _Run,
        root: VaultDir,
        loc: _Located,
        kind: str,
        key: str,
        owned: list[tuple[str, YamlValue]],
        seeds: list[tuple[str, YamlValue]],
        region: str,
    ) -> None:
        path = self._path(loc.parts)
        run.where = path
        directory = loc.directory
        for attempt in range(_ATTEMPTS):
            got = directory.read(loc.name, max_bytes=MAX_NOTE_BYTES)
            try:
                if got is None:
                    text = new_note(owned, seeds, region)
                    directory.write_atomic(
                        loc.name, text.encode("utf-8", "surrogateescape"), expect=None
                    )
                    return
                raw, stamp = got
                _after_read(path)
                existing = raw.decode("utf-8", "surrogateescape")
                if note_identity(existing) != (kind, key):
                    raise NoteChanged(f"{path} no longer carries this episode's frontmatter")
                try:
                    updated = update_note(existing, owned, seeds, region)
                except DamagedNote:
                    run.warnings.append(
                        f"{path}: the earmark markers are missing or damaged, so the note was "
                        "not updated; restore the <!-- earmark:begin --> and "
                        "<!-- earmark:end --> lines, or delete the note to regenerate it"
                    )
                    return
                if updated.text == existing:
                    return
                if updated.edited:
                    backup = self._backup(root, loc, raw, updated.old_digest)
                    run.warnings.append(
                        f"{path}: text inside the earmark markers was edited by hand and has "
                        f"been replaced; the previous version was saved as {backup}"
                    )
                directory.write_atomic(
                    loc.name, updated.text.encode("utf-8", "surrogateescape"), expect=stamp
                )
                return
            except NoteChanged:
                if attempt == _ATTEMPTS - 1:
                    raise

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
