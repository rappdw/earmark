"""The digest note: owned frontmatter and the managed region. Pure and deterministic: no
clock, and every link is built by code from a path in ``paths``, never from model text."""

from collections.abc import Mapping
from datetime import timedelta, tzinfo
from typing import Final

from earmark.analyze.render import escape_block_start
from earmark.analyze.schema import plural
from earmark.digest.model import Digest, DigestEpisode, DigestLead
from earmark.index.chunks import NotePaths
from earmark.models import format_hms
from earmark.vault.document import YamlValue
from earmark.vault.notes import note_link
from earmark.vault.sanitize import escape_markdown

MAX_HEADING_CHARS: Final = 80
NO_LEADS: Final = "*No research leads in these episodes.*"


def digest_fields(digest: Digest) -> list[tuple[str, YamlValue]]:
    return [
        ("earmark", "digest"),
        ("digest", digest.window.digest_id),
        ("start", digest.window.first_day),
        ("end", digest.window.last_day),
        ("model", digest.model),
    ]


def _heading(title: str) -> str:
    collapsed = " ".join(title.split())
    if len(collapsed) > MAX_HEADING_CHARS:
        collapsed = collapsed[: MAX_HEADING_CHARS - 1] + "…"
    return "## " + escape_markdown(collapsed)


def render_digest_region(
    digest: Digest, paths: Mapping[str, NotePaths], *, tz: tzinfo | None, tz_label: str
) -> str:
    e = escape_markdown
    by_key = {ep.episode.key: ep for ep in digest.episodes}
    leads: dict[str, DigestLead] = {
        lead.lead_id: lead for ep in digest.episodes for lead in ep.leads
    }

    def episode_ref(ep: DigestEpisode) -> str:
        title = ep.episode.title or "Untitled"
        found = paths.get(ep.episode.key)
        if found is not None and found.note_path is not None:
            return note_link(found.note_path, title)
        return f"{e(title)} (no note)"

    def time_ref(lead: DigestLead) -> str | None:
        if lead.timestamp_s is None:
            return None
        stamp = format_hms(lead.timestamp_s)
        found = paths.get(lead.episode_key)
        if found is not None and found.transcript_note_path is not None:
            return note_link(found.transcript_note_path, stamp)
        return f"[{stamp}]"

    def lead_line(lead_id: str) -> str:
        lead = leads[lead_id]
        ep = by_key[lead.episode_key]
        line = f"- **{e(lead.text) or '(empty)'}**"
        rationale = e(lead.rationale)
        if rationale:
            line += f" — {rationale}"
        line += f" · {episode_ref(ep)} ({e(ep.episode.show_title)})"
        stamp = time_ref(lead)
        if stamp is not None:
            line += f" · {stamp}"
        return line

    lead_count = sum(len(ep.leads) for ep in digest.episodes)
    ep_count = len(digest.episodes)
    window = digest.window
    head = (
        f"**{window.first_day.isoformat()} to {window.last_day.isoformat()}** · "
        f"{ep_count} {plural(ep_count, 'episode')} · "
        f"{lead_count} research {plural(lead_count, 'lead')}"
    )
    if lead_count:
        head += f" · {len(digest.themes)} {plural(len(digest.themes), 'theme')}"

    blocks = [f"# Digest {window.label}", head]
    if lead_count == 0:
        blocks.append(NO_LEADS)
    for theme in digest.themes:
        blocks.append(_heading(theme.title))
        summary = e(theme.summary)
        if summary:
            blocks.append(escape_block_start(summary))
        blocks.append("\n".join(lead_line(i) for i in theme.leads))
    if digest.other:
        blocks.append("## Other leads")
        blocks.append("\n".join(lead_line(i) for i in digest.other))

    rows: list[str] = []
    for ep in digest.episodes:
        shared = ep.shared_at.astimezone(tz).date().isoformat()
        count = (
            f"{len(ep.leads)} research {plural(len(ep.leads), 'lead')}"
            if ep.analyses
            else "no analysis yet"
        )
        rows.append(f"- {shared} · {episode_ref(ep)} · {e(ep.episode.show_title)} · {count}")
    blocks.append("## Episodes")
    blocks.append("\n".join(rows))

    after = (window.last_day + timedelta(days=1)).isoformat()
    callout = [
        "> [!info]- Digest details",
        f"> - Window: {window.first_day.isoformat()} 00:00 to {after} 00:00, "
        f"{e(tz_label)}, by first share time",
        f"> - Themes: {e(digest.model) if digest.model else 'none (no research leads)'}",
    ]
    callout.extend(f"> - {e(w)}" for w in digest.warnings)
    blocks.append("\n".join(callout))
    return "\n\n".join(blocks) + "\n"
