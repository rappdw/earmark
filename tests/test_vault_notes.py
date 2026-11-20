import errno
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from earmark.analyze.render import render_analysis
from earmark.errors import PermanentError, TransientError
from earmark.fakes import make_fake_backends
from earmark.models import Analysis, Entity, Episode, StoreResult, Transcript
from earmark.vault import notes
from earmark.vault.document import begin_marker, region_digest, split_note
from earmark.vault.fs import SymlinkRefused, vault_lock
from earmark.vault.notes import VaultStore
from tests.factories import (
    PST,
    T0,
    make_analysis,
    make_episode,
    make_transcript,
)
from tests.pipeline_env import CANON, make_env

SHOW = "Le Podcast ☕"
EP_NAME = "2026-01-02 Épisode 1 - «Tests».md"
TR_NAME = "2026-01-02 Épisode 1 - «Tests» (transcript).md"
EP_PATH = f"Earmark/Podcasts/{SHOW}/{EP_NAME}"
TR_PATH = f"Earmark/Podcasts/{SHOW}/transcripts/{TR_NAME}"
KEY = make_episode().key


_FOLDERS = {
    "people": "People",
    "papers": "Papers",
    "companies": "Companies",
    "tools": "Tools",
    "topics": "Topics",
}


def zoe_linker(field: str, entity: Entity) -> str | None:
    return notes.note_link(f"Earmark/Entities/{_FOLDERS[field]}/{entity.name}.md", entity.name)


def make_store(tmp_path: Path, **kw: Any) -> tuple[VaultStore, Path]:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    kw.setdefault("tz", UTC)
    subdir = kw.pop("subdir", "Earmark")
    return VaultStore(vault, subdir, data_dir=tmp_path / "data", **kw), vault


def store_once(
    store: VaultStore,
    *,
    episode: Episode | None = None,
    transcript: Transcript | None = None,
    analyses: list[Analysis] | None = None,
    shared_at: datetime = T0,
) -> StoreResult:
    return store.store(
        episode or make_episode(),
        transcript or make_transcript(),
        [make_analysis()] if analyses is None else analyses,
        shared_at,
    )


def front(text: str) -> dict[str, Any]:
    lines = split_note(text).frontmatter
    assert lines is not None
    loaded = yaml.safe_load("".join(lines))
    assert isinstance(loaded, dict)
    return loaded


def body(text: str) -> str:
    return "".join(split_note(text).body)


def region(text: str) -> str:
    return text.split(" -->\n", 1)[1].rsplit("<!-- earmark:end -->", 1)[0]


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def sections(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


# -- A1: paths --------------------------------------------------------------------------


def test_paths_match_layout(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store)
    assert result == StoreResult(note_path=EP_PATH, transcript_note_path=TR_PATH)
    assert (vault / EP_PATH).is_file()
    assert (vault / TR_PATH).is_file()


def test_unknown_publish_date_uses_share_date(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    episode = make_episode().model_copy(update={"published_at": None})
    result = store_once(store, episode=episode)
    assert result.note_path == f"Earmark/Podcasts/{SHOW}/2026-01-01 Épisode 1 - «Tests».md"
    text = (vault / result.note_path).read_text()
    assert front(text)["date"] is None
    assert "**Published:**" not in text


def test_date_uses_tz(tmp_path: Path) -> None:
    episode = make_episode().model_copy(
        update={"published_at": datetime(2026, 1, 2, 23, 30, tzinfo=PST)}
    )
    utc_store, _ = make_store(tmp_path / "a")
    assert "2026-01-03 " in store_once(utc_store, episode=episode).note_path
    la = ZoneInfo("America/Los_Angeles")
    la_store, vault = make_store(tmp_path / "b", tz=la)
    result = store_once(la_store, episode=episode)
    assert "2026-01-02 " in result.note_path
    assert front((vault / result.note_path).read_text())["date"] == date(2026, 1, 2)


def test_system_zone_is_used_when_tz_is_none(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, tz=None)
    assert store_once(store).note_path.startswith("Earmark/Podcasts/")


def _foreign(vault: Path, name: str, directory: str = f"Earmark/Podcasts/{SHOW}") -> Path:
    path = vault / directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("foreign\n")
    return path


def test_collision_gets_key_suffix(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    foreign = _foreign(vault, EP_NAME)
    result = store_once(store)
    assert result.note_path == f"Earmark/Podcasts/{SHOW}/{EP_NAME[:-3]} ({KEY[:8]}).md"
    assert result.transcript_note_path == (
        f"Earmark/Podcasts/{SHOW}/transcripts/{EP_NAME[:-3]} ({KEY[:8]}) (transcript).md"
    )
    assert foreign.read_text() == "foreign\n"
    assert (vault / result.note_path).is_file()


def test_second_collision_uses_full_key(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    _foreign(vault, EP_NAME)
    _foreign(vault, f"{EP_NAME[:-3]} ({KEY[:8]}).md")
    result = store_once(store)
    assert result.note_path.endswith(f" ({KEY}).md")


def test_no_free_name_is_permanent(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    for name in (EP_NAME, f"{EP_NAME[:-3]} ({KEY[:8]}).md", f"{EP_NAME[:-3]} ({KEY}).md"):
        _foreign(vault, name)
    with pytest.raises(PermanentError, match="no free note name"):
        store_once(store)


@pytest.mark.parametrize(
    "occupant",
    [EP_NAME.lower(), "2026-01-02 E\u0301pisode 1 - «Tests».md"],
    ids=["lower-case", "nfd"],
)
def test_collision_is_case_and_normalization_insensitive(tmp_path: Path, occupant: str) -> None:
    store, vault = make_store(tmp_path)
    _foreign(vault, occupant)
    assert store_once(store).note_path.endswith(f" ({KEY[:8]}).md")


def test_show_dir_fold_match_reused(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    (vault / "Earmark" / "Podcasts" / "le podcast ☕").mkdir(parents=True)
    result = store_once(store)
    assert result.note_path.startswith("Earmark/Podcasts/le podcast ☕/")
    assert [p.name for p in (vault / "Earmark" / "Podcasts").iterdir()] == ["le podcast ☕"]


def test_long_names_are_capped(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    episode = make_episode(title="é" * 400).model_copy(update={"show_title": "ñ" * 400})
    result = store_once(store, episode=episode)
    for part in (result.note_path, result.transcript_note_path or ""):
        assert all(len(p.encode()) <= 200 for p in part.split("/"))
    assert len(result.note_path.split("/")[2].encode()) <= 100


# -- A2: frontmatter --------------------------------------------------------------------


def test_frontmatter_fields(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store)
    fm = front((vault / result.note_path).read_text())
    assert list(fm) == [
        "earmark",
        "show",
        "episode",
        "date",
        "url",
        "guid",
        "episode_key",
        "duration",
        "prompts",
        "shared_at",
        "model",
        "transcript",
        "tags",
    ]
    episode = make_episode()
    assert fm["earmark"] == "episode"
    assert fm["show"] == SHOW
    assert fm["episode"] == episode.title
    assert fm["date"] == date(2026, 1, 2)
    assert fm["url"] == episode.url
    assert fm["guid"] == episode.guid
    assert fm["episode_key"] == KEY
    assert fm["duration"] == "00:02:05"
    assert fm["prompts"] == ["Quels articles sont cités ?"]
    assert fm["shared_at"] == datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001  # naive by design
    assert fm["model"] == "fake-llm"
    assert fm["transcript"] == "[[" + TR_PATH.removesuffix(".md") + "]]"
    assert fm["tags"] == ["earmark", "podcast"]


def test_duration_falls_back_to_episode(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    transcript = make_transcript().model_copy(update={"duration_s": 0.0})
    result = store_once(store, transcript=transcript)
    assert front((vault / result.note_path).read_text())["duration"] == "01:00:00"
    no_duration = make_episode(duration_s=None)
    result = store_once(store, episode=no_duration, transcript=transcript)
    assert front((vault / result.note_path).read_text())["duration"] is None


def test_transcript_frontmatter_fields(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store)
    assert result.transcript_note_path is not None
    fm = front((vault / result.transcript_note_path).read_text())
    assert list(fm) == [
        "earmark",
        "show",
        "episode",
        "date",
        "url",
        "guid",
        "episode_key",
        "duration",
        "language",
        "model",
        "episode_note",
        "tags",
    ]
    assert fm["earmark"] == "transcript"
    assert fm["language"] == "fr"
    assert fm["model"] == "fake-asr"
    assert fm["episode_note"] == "[[" + EP_PATH.removesuffix(".md") + "]]"
    assert fm["tags"] == ["earmark", "transcript"]
    assert fm["episode_key"] == KEY


HOSTILE = [
    'Part 1: the "real" story',
    "---",
    "a\n---\nshow: evil",
    "# comment",
    "- item",
    "yes",
    "null",
    "!!python/object:os.system",
    "&a *a",
    "{x: 1}",
    "[a]",
    "' quote",
    "back\\slash",
    "tab\tand\x1bESC",
    " \x85\ufeff",
]


@pytest.mark.parametrize("hostile", HOSTILE)
@pytest.mark.parametrize("field", ["title", "show"])
def test_hostile_titles_in_yaml(tmp_path: Path, hostile: str, field: str) -> None:
    store, vault = make_store(tmp_path)
    episode = make_episode(title=hostile if field == "title" else "Title")
    if field == "show":
        episode = episode.model_copy(update={"show_title": hostile})
    result = store_once(store, episode=episode)
    for path, kind in ((result.note_path, "episode"), (result.transcript_note_path, "transcript")):
        assert path is not None
        text = (vault / path).read_text()
        fm = front(text)
        assert fm["earmark"] == kind
        assert fm["episode"] == episode.title
        assert fm["show"] == episode.show_title
        assert set(fm) <= {
            "earmark",
            "show",
            "episode",
            "date",
            "url",
            "guid",
            "episode_key",
            "duration",
            "prompts",
            "shared_at",
            "model",
            "transcript",
            "language",
            "episode_note",
            "tags",
        }
        assert body(text).startswith("<!-- earmark:begin hash=")


# -- A3: body ---------------------------------------------------------------------------


def test_sections_newest_first(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    default = make_analysis(prompt=None)
    a = make_analysis(prompt="Prompt A", created_at=T0 + timedelta(hours=1))
    b = make_analysis(prompt="Prompt B", created_at=T0 + timedelta(hours=2))
    result = store_once(store, analyses=[default, a, b])
    text = (vault / result.note_path).read_text()
    reg = region(text)
    positions = [
        reg.index(render_analysis(x, entity_link=zoe_linker).removesuffix("\n"))
        for x in (b, a, default)
    ]
    assert positions == sorted(positions)
    assert "> Prompt A\n" in reg
    assert "## Default analysis" in reg
    assert "[00:00:03]" in reg
    assert reg.count("> [!info]- Analysis details") == 3
    assert front(text)["prompts"] == ["Prompt B", "Prompt A"]
    assert front(text)["model"] == "fake-llm"


def test_details_callout_has_model_and_warnings(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    analysis = make_analysis()
    analysis = analysis.model_copy(update={"warnings": ("fell back <b> to $model",)})
    result = store_once(store, analyses=[analysis])
    reg = region((vault / result.note_path).read_text())
    assert "> [!info]- Analysis details\n> - Model: fake-llm\n> - Created: 2026-01-01 12:00" in reg
    assert "> - fell back &lt;b> to \\$model" in reg


def test_no_analyses_placeholder(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store, analyses=[])
    text = (vault / result.note_path).read_text()
    assert "*No analysis yet.*" in text
    fm = front(text)
    assert fm["model"] is None
    assert fm["prompts"] == []


APPLE_LINK = (
    "[Apple Podcasts](https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000700000001)"
)
EXACT_REGION = f"""# Épisode 1: «Tests»

**Show:** Le Podcast ☕ · **Published:** 2026-01-02 · **Duration:** 00:02:05

{APPLE_LINK} · [[{TR_PATH.removesuffix(".md")}|Transcript]]

## Default analysis

Résumé ☕

### Key claims

- [00:00:03] Une affirmation

### People

- **[[Earmark/Entities/People/Zoë|Zoë]]**: Cité en exemple [00:00:01] [00:00:02]

### Papers

- **[[Earmark/Entities/Papers/Zoë|Zoë]]**: Cité en exemple [00:00:01] [00:00:02]

### Companies

- **[[Earmark/Entities/Companies/Zoë|Zoë]]**: Cité en exemple [00:00:01] [00:00:02]

### Tools

- **[[Earmark/Entities/Tools/Zoë|Zoë]]**: Cité en exemple [00:00:01] [00:00:02]

### Topics

- **[[Earmark/Entities/Topics/Zoë|Zoë]]**: Cité en exemple [00:00:01] [00:00:02]

### Open questions

- Pourquoi ?

### Research leads

- [00:00:04] Piste — Parce que

> [!info]- Analysis details
> - Model: fake-llm
> - Created: 2026-01-01 12:00
> - an analysis warning
"""


def test_episode_region_exact(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store, analyses=[make_analysis(prompt=None)])
    assert region((vault / result.note_path).read_text()) == EXACT_REGION
    assert (
        notes.render_episode_region(
            make_episode(),
            make_transcript(),
            [make_analysis(prompt=None)],
            TR_PATH,
            UTC,
            entity_link=zoe_linker,
        )
        == EXACT_REGION
    )


# -- A4: transcript note ----------------------------------------------------------------


def test_transcript_note_and_links(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store)
    ep_text = (vault / result.note_path).read_text()
    tr_text = (vault / TR_PATH).read_text()
    assert f"[[{TR_PATH.removesuffix('.md')}|Transcript]]" in region(ep_text)
    assert f"[[{EP_PATH.removesuffix('.md')}|Episode note]]" in region(tr_text)
    assert (vault / (TR_PATH.removesuffix(".md") + ".md")).is_file()
    assert (vault / (EP_PATH.removesuffix(".md") + ".md")).is_file()
    assert "[00:00:00] Bonjour à tous\n\n[00:01:05] Merci" in tr_text
    assert "> - Language: fr" in tr_text
    assert "> - a transcript warning" in tr_text


def test_transcript_text_escaped(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    transcript = make_transcript()
    segment = transcript.segments[0].model_copy(update={"text": "$5 and $10 #ad [[x]] <b>"})
    transcript = transcript.model_copy(update={"segments": (segment, transcript.segments[1])})
    store_once(store, transcript=transcript)
    assert "[00:00:00] \\$5 and \\$10 \\#ad \\[\\[x\\]\\] &lt;b>" in (vault / TR_PATH).read_text()


def test_empty_transcript_and_empty_segment(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    empty = make_transcript().model_copy(update={"segments": ()})
    store_once(store, transcript=empty)
    assert "*The transcript is empty.*" in (vault / TR_PATH).read_text()
    blank = make_transcript().segments[0].model_copy(update={"text": "  "})
    store_once(store, transcript=empty.model_copy(update={"segments": (blank,)}))
    assert "\n[00:00:00]\n" in (vault / TR_PATH).read_text()


def test_link_falls_back_to_markdown_link(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path, subdir="Notes#1")
    result = store_once(store)
    tr_rel = f"Notes#1/Podcasts/{SHOW}/transcripts/{TR_NAME}"
    assert result.transcript_note_path == tr_rel
    text = (vault / result.note_path).read_text()
    assert "[Transcript](Notes%231/Podcasts/Le%20Podcast%20%E2%98%95/transcripts/" in text
    assert front(text)["transcript"] is None
    assert front((vault / tr_rel).read_text())["episode_note"] is None


def test_note_link() -> None:
    assert notes.note_link("A/b.md", "X") == "[[A/b|X]]"
    assert notes.note_link("A#/b c.md", "X") == "[X](A%23/b%20c.md)"


# -- A5: user text ----------------------------------------------------------------------


def _edit(path: Path, fn: Any) -> None:
    path.write_text(fn(path.read_text()))


def test_user_text_survives(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store_once(store)
    path = vault / result.note_path

    def edit(text: str) -> str:
        text = text.replace(
            'tags:\n  - "earmark"\n  - "podcast"\n', "rating: 5\n# my comment\ntags: [mine]\n"
        )
        text = text.replace("---\n<!--", "---\nMy intro paragraph.\n\n<!--", 1)
        return text + "\nMy outro.\n"

    _edit(path, edit)
    edited = path.read_text()
    second = make_analysis(prompt="Second", created_at=T0 + timedelta(hours=1))
    store_once(store, analyses=[make_analysis(), second])
    new = path.read_text()
    assert "---\nMy intro paragraph.\n\n<!--" in new
    assert new.endswith("<!-- earmark:end -->\n\nMy outro.\n")
    assert "rating: 5\n# my comment\ntags: [mine]\n" in new
    fm = front(new)
    assert fm["rating"] == 5
    assert fm["tags"] == ["mine"]
    assert len(sections(region(new))) == 2
    assert body(new).split("<!--", 1)[0] == body(edited).split("<!--", 1)[0]


def test_owned_keys_rewritten(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    _edit(
        path,
        lambda t: t.replace('episode: "Épisode', 'episode: "mine" #', 1).replace(
            "---\n<!--", 'show: "dup"\n---\n<!--', 1
        ),
    )
    store_once(store)
    text = path.read_text()
    assert text.count("\nshow:") == 1
    assert front(text)["episode"] == "Épisode 1: «Tests»"
    assert front(text)["show"] == SHOW


def test_obsidian_reformatted_frontmatter(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    text = path.read_text()
    reformatted = (
        "---\n"
        "earmark: episode\n"
        "show: Le Podcast ☕\n"
        f"episode_key: {KEY}\n"
        "prompts:\n- Old prompt\n- Another\n"
        "tags:\n- mine\n"
        "rating: 5\n"
        "---\n" + text.split("---\n", 2)[2]
    )
    path.write_text(reformatted)
    store_once(store)
    fm = front(path.read_text())
    assert fm["prompts"] == ["Quels articles sont cités ?"]
    assert fm["tags"] == ["mine"]
    assert fm["rating"] == 5
    assert fm["show"] == SHOW


def test_deleted_tags_reseeded(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    _edit(path, lambda t: t.replace('tags:\n  - "earmark"\n  - "podcast"\n', ""))
    assert "tags" not in front(path.read_text())
    store_once(store)
    assert front(path.read_text())["tags"] == ["earmark", "podcast"]


def test_crlf_and_invalid_utf8_user_text_preserved(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    raw = path.read_bytes()
    raw = raw.replace(b"---\n<!--", b"---\r\nintro \xff\xfe line\r\n<!--", 1)
    raw += b"outro \xff\xfe\r\nlast\r\n"
    path.write_bytes(raw)
    second = make_analysis(prompt="Second", created_at=T0 + timedelta(hours=1))
    store_once(store, analyses=[make_analysis(), second])
    new = path.read_bytes()
    assert b"---\nintro \xff\xfe line\r\n<!--" in new or b"---\r\nintro" in new
    assert b"intro \xff\xfe line\r\n" in new
    assert new.endswith(b"<!-- earmark:end -->\noutro \xff\xfe\r\nlast\r\n")


def test_note_without_frontmatter_is_not_recognised(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    path.write_text("my own text now\n")
    result = store_once(store)
    assert result.note_path.endswith(f" ({KEY[:8]}).md")
    assert path.read_text() == "my own text now\n"


# -- A6: determinism --------------------------------------------------------------------


def test_store_twice_identical(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    before = tree(vault)
    stamps = {p: p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()}
    listing = sorted(str(p) for p in vault.rglob("*"))
    second = store_once(store)
    assert second == first
    assert tree(vault) == before
    assert {p: p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()} == stamps
    assert sorted(str(p) for p in vault.rglob("*")) == listing


def test_two_vaults_identical(tmp_path: Path) -> None:
    analyses = [make_analysis(), make_analysis(prompt="B", created_at=T0 + timedelta(hours=1))]
    trees = []
    for name in ("one", "two"):
        store, vault = make_store(tmp_path / name)
        store_once(store, analyses=analyses)
        trees.append(tree(vault))
    assert trees[0] == trees[1]
    assert len([p for p in trees[0] if "/Entities/" not in p]) == 2


def test_store_after_user_edit_outside_is_stable(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    path.write_text(path.read_text() + "\nuser text\n")
    store_once(store)
    first = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    store_once(store)
    assert path.read_bytes() == first
    assert path.stat().st_mtime_ns == mtime


# -- A7: containment --------------------------------------------------------------------

HOSTILE_NAMES = [
    "../../x",
    "/etc/passwd",
    "..",
    ".",
    ".obsidian",
    "a/../../b",
    "\\..\\..\\x",
    "x\x00y",
    "CON",
    "é" * 1000,
]


@pytest.mark.parametrize("hostile", HOSTILE_NAMES)
@pytest.mark.parametrize("field", ["title", "show"])
def test_hostile_title_stays_inside(tmp_path: Path, hostile: str, field: str) -> None:
    store, vault = make_store(tmp_path)
    episode = make_episode(title=hostile if field == "title" else "Title")
    if field == "show":
        episode = episode.model_copy(update={"show_title": hostile})
    result = store_once(store, episode=episode)
    inside = vault / "Earmark"
    for p in tmp_path.rglob("*"):
        if p == tmp_path / "data" or (tmp_path / "data") in p.parents:
            continue
        assert p == vault or inside == p or inside in p.parents or p in {vault}, p
        assert not p.name.startswith("."), p
    assert result.note_path.startswith("Earmark/Podcasts/")
    assert (result.transcript_note_path or "").startswith("Earmark/Podcasts/")
    assert (vault / result.note_path).is_file()


def test_symlinked_show_dir_refused(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    podcasts = vault / "Earmark" / "Podcasts"
    podcasts.mkdir(parents=True)
    (podcasts / SHOW).symlink_to(outside)
    with pytest.raises(SymlinkRefused, match=f"Earmark/Podcasts/{SHOW}") as info:
        store_once(store)
    assert isinstance(info.value, PermanentError)
    assert str(tmp_path) not in str(info.value)
    assert list(outside.iterdir()) == []


def test_symlinked_subdir_refused(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "Earmark").symlink_to(outside)
    with pytest.raises(SymlinkRefused, match="Earmark"):
        store_once(store)
    assert list(outside.iterdir()) == []


def test_symlink_at_candidate_name_is_occupied(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    show = vault / "Earmark" / "Podcasts" / SHOW
    show.mkdir(parents=True)
    link = show / EP_NAME
    link.symlink_to(outside / "x.md")
    result = store_once(store)
    assert result.note_path.endswith(f" ({KEY[:8]}).md")
    assert not (outside / "x.md").exists()
    assert link.is_symlink()
    assert link.readlink() == outside / "x.md"


def test_scan_ignores_symlinked_notes(tmp_path: Path) -> None:
    other_store, other_vault = make_store(tmp_path / "other")
    other = other_vault / store_once(other_store).note_path
    original = other.read_bytes()
    store, vault = make_store(tmp_path)
    show = vault / "Earmark" / "Podcasts" / SHOW
    show.mkdir(parents=True)
    (show / "linked.md").symlink_to(other)
    result = store_once(store)
    assert result.note_path == EP_PATH
    assert other.read_bytes() == original
    assert (show / "linked.md").is_symlink()


def test_vault_dir_itself_may_be_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store = VaultStore(link, "Earmark", data_dir=tmp_path / "data", tz=UTC)
    result = store_once(store)
    assert (real / result.note_path).is_file()


# -- A9: light import -------------------------------------------------------------------


def test_vault_modules_import_light() -> None:
    code = (
        "import sys, earmark.vault.notes, earmark.vault.entities, earmark.vault.aliases\n"
        "heavy = ('fastapi', 'httpx2', 'anthropic', 'yaml', 'faster_whisper', 'lancedb')\n"
        "print(sorted(m for m in heavy if m in sys.modules))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# -- A10: identity ----------------------------------------------------------------------


def test_retitled_episode_keeps_path(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    retitled = make_episode(title="A New Title")
    second = store_once(store, episode=retitled)
    assert second == first
    text = (vault / second.note_path).read_text()
    assert front(text)["episode"] == "A New Title"
    assert "# A New Title\n" in text
    assert len(list((vault / "Earmark" / "Podcasts" / SHOW).glob("*.md"))) == 1


def test_user_renamed_note_is_updated(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    archive = vault / "Earmark" / "Archive"
    archive.mkdir()
    (vault / first.note_path).rename(archive / "x.md")
    second = store_once(store, analyses=[make_analysis(), make_analysis(prompt="B")])
    assert second.note_path == "Earmark/Archive/x.md"
    assert second.transcript_note_path == first.transcript_note_path
    assert list((vault / "Earmark" / "Podcasts" / SHOW).glob("*.md")) == []
    ep_text = (archive / "x.md").read_text()
    assert f"[[{TR_PATH.removesuffix('.md')}|Transcript]]" in ep_text
    assert len(sections(ep_text)) == 2
    tr_text = (vault / TR_PATH).read_text()
    assert "[[Earmark/Archive/x|Episode note]]" in tr_text
    assert front(tr_text)["episode_note"] == "[[Earmark/Archive/x]]"


def test_user_renamed_transcript_keeps_name_pattern(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    assert first.transcript_note_path is not None
    moved = vault / "Earmark" / "my transcript #1.md"
    (vault / first.transcript_note_path).rename(moved)
    second = store_once(store)
    assert second.transcript_note_path == "Earmark/my transcript #1.md"
    assert front(moved.read_text())["earmark"] == "transcript"
    assert front((vault / second.note_path).read_text())["transcript"] is None


def test_duplicate_identity_warns(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    path = vault / first.note_path
    copy = path.with_name(path.stem + " (conflicted copy).md")
    shutil.copy(path, copy)
    copy_bytes = copy.read_bytes()
    result = store_once(store, analyses=[make_analysis(), make_analysis(prompt="B")])
    assert result.note_path == first.note_path
    assert copy.read_bytes() == copy_bytes
    assert len(sections(path.read_text())) == 2
    assert result.warnings == (f"found 2 notes for episode {KEY}; updated {first.note_path}",)


def test_duplicate_identity_without_candidate_uses_smallest(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    path = vault / first.note_path
    b = vault / "Earmark" / "b.md"
    a = vault / "Earmark" / "a.md"
    shutil.copy(path, a)
    path.rename(b)
    result = store_once(store)
    assert result.note_path == "Earmark/a.md"
    assert "found 2 notes" in result.warnings[0]


def test_backups_dir_not_scanned(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    backups = vault / "Earmark" / "Backups"
    backups.mkdir()
    shutil.copy(vault / first.note_path, backups / "x (0123456789abcdef).md")
    hidden = vault / "Earmark" / ".trash"
    hidden.mkdir()
    shutil.copy(vault / first.note_path, hidden / "y.md")
    second = store_once(store)
    assert second == first
    assert second.warnings == ()


# -- A11: markers -----------------------------------------------------------------------


def test_edit_inside_region_backed_up(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    path = vault / first.note_path
    _edit(path, lambda t: t.replace("Résumé ☕", "My edit", 1))
    previous = path.read_bytes()
    digest = region_digest(region(path.read_text()))
    second = make_analysis(prompt="Second", created_at=T0 + timedelta(hours=1))
    result = store_once(store, analyses=[make_analysis(), second])
    backup_rel = f"Earmark/Backups/{EP_NAME[:-3]} ({digest}).md"
    assert (vault / backup_rel).read_bytes() == previous
    assert result.warnings == (
        f"{first.note_path}: text inside the earmark markers was edited by hand and has been "
        f"replaced; the previous version was saved as {backup_rel}",
    )
    assert "My edit" not in path.read_text()
    assert len(sections(region(path.read_text()))) == 2
    again = store_once(store, analyses=[make_analysis(), second])
    assert again.warnings == ()
    assert len(list((vault / "Earmark" / "Backups").iterdir())) == 1


def test_missing_markers_freeze_note(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    path = vault / first.note_path
    _edit(
        path,
        lambda t: "".join(
            line for line in t.splitlines(keepends=True) if "<!-- earmark:" not in line
        ),
    )
    frozen = path.read_bytes()
    transcript = make_transcript().model_copy(update={"model": "other-asr"})
    result = store_once(store, transcript=transcript, analyses=[make_analysis(prompt="New")])
    assert path.read_bytes() == frozen
    assert result.note_path == first.note_path
    assert result.warnings == (
        f"{first.note_path}: the earmark markers are missing or damaged, so the note was not "
        "updated; restore the <!-- earmark:begin --> and <!-- earmark:end --> lines, or "
        "delete the note to regenerate it",
    )
    assert front((vault / TR_PATH).read_text())["model"] == "other-asr"


@pytest.mark.parametrize("damage", ["two-begins", "end-first", "only-begin"])
def test_damaged_markers_freeze_note(tmp_path: Path, damage: str) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    text = path.read_text()
    begin = begin_marker(region_digest(region(text)))
    end = "<!-- earmark:end -->"
    if damage == "two-begins":
        damaged = text.replace(begin, begin + "\n" + begin)
    elif damage == "end-first":
        damaged = text.replace(begin, end + "\n" + begin, 1).rsplit(end, 1)[0]
    else:
        damaged = text.replace(end, "")
    path.write_text(damaged)
    result = store_once(store, analyses=[make_analysis(prompt="New")])
    assert path.read_text() == damaged
    assert "markers are missing or damaged" in result.warnings[0]


def test_begin_without_hash_accepted(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = vault / store_once(store).note_path
    canonical = path.read_text()
    path.write_text(
        canonical.replace(begin_marker(region_digest(region(canonical))), "<!-- earmark:begin -->")
    )
    result = store_once(store)
    assert result.warnings == ()
    assert path.read_text() == canonical
    assert not (vault / "Earmark" / "Backups").exists()
    path.write_text(
        canonical.replace("Résumé ☕", "edited").replace(
            begin_marker(region_digest(region(canonical))), "<!-- earmark:begin -->"
        )
    )
    result = store_once(store)
    assert path.read_text() == canonical
    assert len(list((vault / "Earmark" / "Backups").iterdir())) == 1
    assert "edited by hand" in result.warnings[0]


# -- A12: concurrency -------------------------------------------------------------------


def test_lock_timeout_is_transient(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path, lock_timeout_s=0.2)
    with vault_lock(tmp_path / "data" / "vault.lock", timeout_s=1), pytest.raises(TransientError):
        store_once(store)
    assert list(vault.iterdir()) == []


def test_user_edit_during_store_not_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    path = vault / first.note_path
    seen: list[str] = []
    real = notes._after_read

    def hook(rel: str) -> None:
        real(rel)
        seen.append(rel)
        if rel == first.note_path and seen.count(rel) == 1:
            with path.open("a") as handle:
                handle.write("\nuser text added mid-store\n")

    monkeypatch.setattr(notes, "_after_read", hook)
    store_once(store, analyses=[make_analysis(), make_analysis(prompt="B")])
    text = path.read_text()
    assert text.endswith("\nuser text added mid-store\n")
    assert len(sections(region(text))) == 2
    assert seen.count(first.note_path) == 2


def test_persistent_change_gives_up_transiently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    path = vault / first.note_path
    counter = [0]

    def hook(rel: str) -> None:
        if rel == first.note_path:
            counter[0] += 1
            with path.open("a") as handle:
                handle.write(f"edit {counter[0]}\n")

    monkeypatch.setattr(notes, "_after_read", hook)
    with pytest.raises(TransientError, match="changed"):
        store_once(store, analyses=[make_analysis(prompt="B")])
    assert counter[0] == 3


# -- A13: errors ------------------------------------------------------------------------


def test_vault_dir_gone_is_transient(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    shutil.rmtree(vault)
    with pytest.raises(TransientError, match="EARMARK_VAULT_DIR"):
        store_once(store)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_dir_is_permanent(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = store_once(store)
    show = (vault / first.note_path).parent
    show.chmod(0o555)
    try:
        with pytest.raises(PermanentError, match="EARMARK_VAULT_DIR") as info:
            store_once(store, analyses=[make_analysis(prompt="B")])
    finally:
        show.chmod(0o755)
    assert str(tmp_path) not in str(info.value)
    assert "EACCES" in str(info.value)
    assert f"Earmark/Podcasts/{SHOW}" in str(info.value)


def test_disk_full_is_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, vault = make_store(tmp_path)

    def full(*args: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("earmark.vault.fs.os.write", full)
    with pytest.raises(TransientError, match="full: ENOSPC"):
        store_once(store)
    monkeypatch.undo()
    assert list(vault.rglob(".earmark-*")) == []


def test_other_oserror_is_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = make_store(tmp_path)

    def boom(*args: object) -> int:
        raise OSError(errno.EIO, "secret text /abs/path")

    monkeypatch.setattr("earmark.vault.fs.os.write", boom)
    with pytest.raises(TransientError, match="vault write failed: EIO") as info:
        store_once(store)
    assert "/abs/path" not in str(info.value)


def test_store_rejects_foreign_key(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    with pytest.raises(PermanentError, match="another episode"):
        store_once(store, analyses=[make_analysis(guid="other")])
    with pytest.raises(PermanentError, match="another episode"):
        store_once(store, transcript=make_transcript(guid="other"))
    assert list(vault.iterdir()) == []


def test_stale_temp_files_are_cleaned(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path, now=lambda: 10_000_000.0)
    first = store_once(store)
    stale = (vault / first.note_path).parent / ".earmark-0123456789abcdef.tmp"
    stale.write_text("x")
    os.utime(stale, (1.0, 1.0))
    store_once(store)
    assert not stale.exists()


# -- pipeline ---------------------------------------------------------------------------


def _vault_env(tmp_path: Path, tz: tzinfo = UTC):  # type: ignore[no-untyped-def]
    (tmp_path / "vault").mkdir()
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    store = VaultStore(tmp_path / "vault", "Earmark", data_dir=tmp_path / "data", tz=tz)
    return make_env(tmp_path, backends=replace(fakes.backends(), store=store), fakes=fakes)


def test_pipeline_records_note_paths(tmp_path: Path) -> None:
    env = _vault_env(tmp_path)
    env.jobs.enqueue(CANON, prompt="first?")
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done"
    assert job.result is not None
    assert job.result.note_path is not None
    assert job.result.transcript_note_path is not None
    note = env.vault / job.result.note_path
    assert note.is_file()
    assert (env.vault / job.result.transcript_note_path).is_file()
    assert job.result.note_path.startswith("Earmark/Podcasts/Fake Show/")
    assert len(sections(region(note.read_text()))) == 1

    env.jobs.enqueue(CANON, prompt="second?")
    job2 = env.worker.run_once()
    assert job2 is not None
    assert job2.result is not None
    assert job2.result.note_path == job.result.note_path
    headings = sections(region(note.read_text()))
    key = job.episode_key
    assert key is not None
    newest_first = sorted(
        env.fakes.artifacts.list_analyses(key),
        key=lambda a: (a.created_at, a.prompt_hash),
        reverse=True,
    )
    assert [h.split(": ", 1)[1] for h in headings] == [a.prompt for a in newest_first]
    assert {a.prompt for a in newest_first} == {"first?", "second?"}

    before = note.read_bytes()
    env.jobs.enqueue(CANON, prompt="second?")
    job3 = env.worker.run_once()
    assert job3 is not None
    assert job3.status == "done"
    assert note.read_bytes() == before


def test_pipeline_store_warning_reaches_job(tmp_path: Path) -> None:
    env = _vault_env(tmp_path)
    env.jobs.enqueue(CANON, prompt="first?")
    job = env.worker.run_once()
    assert job is not None
    assert job.result is not None
    assert job.result.note_path is not None
    note = env.vault / job.result.note_path
    note.write_text("---\nearmark: episode\nepisode_key: " + str(job.episode_key) + "\n---\nmine\n")
    env.jobs.enqueue(CANON, prompt="second?")
    job2 = env.worker.run_once()
    assert job2 is not None
    assert job2.status == "done"
    assert any("markers are missing or damaged" in w for w in job2.warnings)
