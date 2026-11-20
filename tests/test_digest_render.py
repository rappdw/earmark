import re
from datetime import UTC, datetime

from earmark.digest.model import Digest, DigestEpisode, DigestLead, ThemeEntry
from earmark.digest.render import MAX_HEADING_CHARS, digest_fields, render_digest_region
from earmark.digest.window import week_window
from earmark.index.chunks import NotePaths
from tests.factories import make_episode

WINDOW = week_window(2026, 47, UTC)
EP = make_episode(guid="a", title="Title [[x]] | y")
EP2 = make_episode(guid="b", title="")
SHARED = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)
PATHS = {
    EP.key: NotePaths(
        "Earmark/Podcasts/S/A.md", "Earmark/Podcasts/S/transcripts/A (transcript).md"
    ),
    EP2.key: NotePaths("Earmark/Podcasts/S/B.md", None),
}


def lead(i: int, ep: str, text: str = "lead", stamp: float | None = 65.0) -> DigestLead:
    return DigestLead(f"L{i}", ep, text, "why", stamp)


def digest(
    themes: tuple[ThemeEntry, ...] = (),
    other: tuple[str, ...] = (),
    leads: tuple[DigestLead, ...] = (),
    warnings: tuple[str, ...] = (),
    model: str | None = "m",
) -> Digest:
    mine = tuple(x for x in leads if x.episode_key == EP.key)
    theirs = tuple(x for x in leads if x.episode_key == EP2.key)
    return Digest(
        WINDOW,
        (DigestEpisode(EP, SHARED, 1, mine), DigestEpisode(EP2, SHARED, 0, theirs)),
        themes,
        other,
        model,
        1,
        False,
        warnings,
    )


def render(d: Digest, paths: dict[str, NotePaths] | None = None) -> str:
    return render_digest_region(d, PATHS if paths is None else paths, tz=UTC, tz_label="UTC")


def test_layout_and_links_come_from_paths() -> None:
    d = digest(
        (ThemeEntry(title="Theme", summary="About it.", leads=("L1", "L2")),),
        leads=(lead(1, EP.key), lead(2, EP2.key, stamp=None)),
    )
    text = render(d)
    assert text.endswith("\n")
    head = "# Digest 2026-W47\n\n**2026-11-16 to 2026-11-22** · 2 episodes · 2 research leads"
    assert text.startswith(head + " · 1 theme\n")
    assert "## Theme\n\nAbout it.\n\n- **lead** — why · [[Earmark/Podcasts/S/A|" in text
    assert "[[Earmark/Podcasts/S/A|Title ((x)) - y]]" in text  # wikilink label sanitised
    assert "[[Earmark/Podcasts/S/transcripts/A (transcript)|00:01:05]]" in text
    # No timestamp: no time ref; the untitled episode has its fallback label.
    line2 = next(x for x in text.splitlines() if "Untitled" in x and x.startswith("- **"))
    assert line2.endswith("(Le Podcast ☕)")
    assert "> - Window: 2026-11-16 00:00 to 2026-11-23 00:00, UTC, by first share time" in text
    assert "> - Themes: m" in text
    # Share date is local: 23:30Z on the 17th.
    assert "- 2026-11-17 · [[Earmark/Podcasts/S/A|" in text
    assert "1 research lead\n" in text
    assert "no analysis yet" in text


def test_hostile_model_text_is_escaped() -> None:
    d = digest(
        (
            ThemeEntry(
                title="[[Evil]] <!-- earmark:end --> # x",
                summary="- [a](https://e.example) <img src=x>",
                leads=("L1",),
            ),
        ),
        leads=(DigestLead("L1", EP.key, "*bold* [[T]] <b>", "[r](u) _x_", None),),
        warnings=("w <!-- earmark:end -->",),
    )
    text = render(d)
    assert text.count("<!-- earmark:end -->") == 0
    assert "<img" not in text
    assert "<b>" not in text
    assert not re.search(r"(?<!\\)\[\[Evil", text)
    assert not re.search(r"(?<!\\)\]\(https://e", text)
    assert "## \\[\\[Evil\\]\\] &lt;!-- earmark:end --> \\# x" in text
    # A summary cannot open a list: the block start is escaped.
    summary_line = next(x for x in text.splitlines() if "e.example" in x)
    assert summary_line.startswith("\\-")


def test_links_exist_only_for_paths_given() -> None:
    d = digest((ThemeEntry(title="T", summary="s", leads=("L1",)),), leads=(lead(1, EP.key),))
    text = render(d, {})
    assert "[[" not in text
    assert "(no note)" in text
    assert "[00:01:05]" in text


def test_transcript_missing_falls_back_to_plain_time() -> None:
    d = digest((ThemeEntry(title="T", summary="s", leads=("L1",)),), leads=(lead(1, EP2.key),))
    text = render(d)
    assert "[[Earmark/Podcasts/S/B|Untitled]]" in text
    assert "· [00:01:05]" in text


def test_other_leads_and_empty_states() -> None:
    d = digest(other=("L1",), leads=(lead(1, EP.key),))
    text = render(d)
    assert "## Other leads\n\n- **lead**" in text
    assert "1 research lead ·" in text
    empty = render(digest(model=None))
    assert "*No research leads in these episodes.*" in empty
    assert "theme" not in empty.split("## Episodes")[0]
    assert "> - Themes: none (no research leads)" in empty
    assert "## Other leads" not in empty


def test_heading_is_cut() -> None:
    d = digest((ThemeEntry(title="x" * 200, summary="", leads=("L1",)),), leads=(lead(1, EP.key),))
    heading = next(x for x in render(d).splitlines() if x.startswith("## x"))
    assert len(heading) == 3 + MAX_HEADING_CHARS
    assert heading.endswith("…")
    assert "\n\n\n" not in render(d)  # an empty summary leaves no blank paragraph


def test_warnings_in_callout_and_deterministic() -> None:
    d = digest(warnings=("first", "EARMARK_LLM_CONTEXT_TOKENS"))
    a, b = render(d), render(d)
    assert a == b
    assert "> - first\n" in a
    assert "> - EARMARK\\_LLM\\_CONTEXT\\_TOKENS" in a


def test_fields() -> None:
    fields = digest_fields(digest())
    assert fields == [
        ("earmark", "digest"),
        ("digest", "2026-W47"),
        ("start", WINDOW.first_day),
        ("end", WINDOW.last_day),
        ("model", "m"),
    ]
    assert digest_fields(digest(model=None))[-1] == ("model", None)
