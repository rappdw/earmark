"""``run_digest``: collect, theme (cached), validate, write."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import tzinfo
from typing import Literal

from earmark.artifacts import ArtifactStore
from earmark.digest.cache import DigestCache
from earmark.digest.collect import Collected, collect, digest_input_hash
from earmark.digest.model import (
    Digest,
    DigestNote,
    DigestWriter,
    ThemeEntry,
    Themer,
    ThemeRecord,
    ThemeRequest,
)
from earmark.digest.render import digest_fields, render_digest_region
from earmark.digest.themes import ThemesOutput, WireTheme, place
from earmark.digest.window import DigestWindow
from earmark.index.chunks import NotePaths

logger = logging.getLogger(__name__)


def assemble(
    window: DigestWindow, collected: Collected, record: ThemeRecord | None, *, cached: bool
) -> Digest:
    """Build the digest, revalidating the record against the collected lead ids."""
    warnings = list(collected.warnings)
    lead_ids = [lead.lead_id for lead in collected.leads]
    themes: tuple[ThemeEntry, ...] = ()
    other: tuple[str, ...] = tuple(lead_ids)
    model: str | None = None
    requests = 0
    if record is not None:
        model = record.model
        requests = 0 if cached else record.requests
        warnings.extend(record.warnings)
        # A cache file could be stale or edited: place() drops unknown and repeated ids.
        wire = ThemesOutput(
            themes=[
                WireTheme(title=t.title, summary=t.summary, items=list(t.leads))
                for t in record.themes
            ]
        )
        placement = place(wire, lead_ids)
        themes = tuple(
            ThemeEntry(title=title, summary=summary, leads=ids)
            for title, summary, ids in placement.themes
        )
        other = placement.unplaced
        if cached:
            # Counts of a fresh answer are already in the record's own warnings.
            if placement.unknown:
                warnings.append(
                    f"the cached themes named {placement.unknown} ids that are not in this "
                    "digest; they were ignored"
                )
            if placement.repeated:
                warnings.append(
                    f"the cached themes placed {placement.repeated} research leads more than "
                    "once; each is kept in the first"
                )
    return Digest(
        window=window,
        episodes=collected.episodes,
        themes=themes,
        other=other,
        model=model,
        requests=requests,
        cached=cached,
        warnings=tuple(dict.fromkeys(warnings)),
    )


@dataclass(frozen=True, slots=True)
class DigestReport:
    window: DigestWindow
    episodes: int
    leads: int
    themes: int  # themes in the note, "Other leads" not counted
    note_path: str | None  # None: empty window, nothing written
    status: Literal["created", "updated", "unchanged", "empty"]
    requests: int
    cached: bool
    warnings: tuple[str, ...]


def run_digest(
    window: DigestWindow,
    *,
    artifacts: ArtifactStore,
    cache: DigestCache,
    themer: Themer,
    writer: DigestWriter,
    tz: tzinfo | None,
    tz_label: str,
    refresh: bool = False,
) -> DigestReport:
    collected = collect(artifacts, window)
    if not collected.episodes:
        return DigestReport(
            window, 0, 0, 0, None, "empty", 0, False, tuple(dict.fromkeys(collected.warnings))
        )

    record: ThemeRecord | None = None
    cached = False
    leads = collected.leads
    if leads:
        input_hash = digest_input_hash(themer.model, collected)
        if refresh:
            cache.clear_calls(input_hash)
        else:
            record = cache.read(input_hash)
            cached = record is not None
        if record is None:
            record = themer.themes(
                ThemeRequest(
                    input_hash=input_hash,
                    leads=leads,
                    episodes={e.episode.key: e.episode for e in collected.episodes},
                )
            )
            cache.write(record)
            cache.clear_calls(input_hash)

    digest = assemble(window, collected, record, cached=cached)

    def render(paths: Mapping[str, NotePaths]) -> str:
        return render_digest_region(digest, paths, tz=tz, tz_label=tz_label)

    written = writer.write_digest(
        DigestNote(
            digest_id=window.digest_id,
            file_stem=window.file_stem,
            owned=tuple(digest_fields(digest)),
            seeds=(("tags", ("earmark", "digest")),),
            episode_keys=tuple(e.episode.key for e in collected.episodes),
            render=render,
        )
    )
    warnings = tuple(dict.fromkeys((*digest.warnings, *written.warnings)))
    return DigestReport(
        window=window,
        episodes=len(digest.episodes),
        leads=len(leads),
        themes=len(digest.themes),
        note_path=written.note_path,
        status=written.status,
        requests=digest.requests,
        cached=cached,
        warnings=warnings,
    )
