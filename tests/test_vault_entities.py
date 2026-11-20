import re
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest
import yaml

from earmark.artifacts import ArtifactStore
from earmark.db import database_path, open_database
from earmark.errors import PermanentError, TransientError
from earmark.fakes import make_fake_backends
from earmark.models import Analysis, Entity, Episode, StoreResult, entity_key
from earmark.vault.document import split_note
from earmark.vault.entities import EntityRegistry, mentions_from
from earmark.vault.fs import SymlinkRefused
from earmark.vault.notes import RebuildRefused, VaultStore
from earmark.vault.sanitize import is_safe_component
from tests.factories import T0, make_analysis, make_episode, make_transcript
from tests.pipeline_env import PipelineEnv, make_env

ADA_ID = "82a355902603f38e"
PEOPLE = "Earmark/Entities/People"


def make_store(tmp_path: Path, **kw: Any) -> tuple[VaultStore, Path]:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    kw.setdefault("tz", UTC)
    subdir = kw.pop("subdir", "Earmark")
    return VaultStore(vault, subdir, data_dir=tmp_path / "data", **kw), vault


def ep(n: int) -> Episode:
    return make_episode(guid=f"g-{n}", title=f"Épisode {n}")


def an(n: int, *, prompt: str | None = None, created_at: datetime = T0, **fields: Any) -> Analysis:
    base = make_analysis(guid=f"g-{n}", prompt=prompt, created_at=created_at).model_dump()
    empty = dict.fromkeys(("people", "papers", "companies", "tools", "topics"), ())
    return Analysis.model_validate({**base, **empty, **fields})


def put(
    store: VaultStore,
    n: int,
    analyses: list[Analysis],
    shared_at: datetime | None = None,
    *,
    artifacts: bool = True,
) -> StoreResult:
    """Store episode ``n``; also write its artifacts, which later relinks read."""
    shared = shared_at or T0 + timedelta(hours=n)
    episode = ep(n)
    transcript = make_transcript(guid=f"g-{n}")
    if artifacts:
        data_dir: Path = store._data_dir
        store_artifacts = ArtifactStore(data_dir)
        store_artifacts.write_episode(episode)
        store_artifacts.write_transcript(transcript)
        store_artifacts.record_share(episode.key, shared)
        for a in analyses:
            store_artifacts.write_analysis(a)
    return store.store(episode, transcript, analyses, shared)


def person(name: str, context: str = "ctx", ts: tuple[float, ...] = (1.0,)) -> Entity:
    return Entity(name=name, context=context, timestamps_s=ts)


def front(text: str) -> dict[str, Any]:
    lines = split_note(text).frontmatter
    assert lines is not None
    loaded = yaml.safe_load("".join(lines))
    assert isinstance(loaded, dict)
    return loaded


def region(text: str) -> str:
    return text.split(" -->\n", 1)[1].rsplit("<!-- earmark:end -->", 1)[0]


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def mtimes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}


def links(vault: Path) -> list[str]:
    """Every wikilink target and decoded Markdown link target, as a vault-relative path."""
    found: list[str] = []
    for f in vault.rglob("*.md"):
        text = "".join(split_note(f.read_text(encoding="utf-8")).body)
        found += [m + ".md" for m in re.findall(r"\[\[([^\]|]*)(?:\|[^\]]*)?\]\]", text)]
        found += [unquote(m) for m in re.findall(r"\]\((Notes[^)\s]*\.md)\)", text)]
    return found


def assert_links_resolve(vault: Path) -> None:
    found = links(vault)
    assert found
    missing = [t for t in found if not (vault / t).is_file()]
    assert missing == []


def db_rows(data_dir: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = sqlite3.connect(database_path(data_dir))
    try:
        return {
            t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}"))  # noqa: S608
            for t in ("episodes", "mentions", "aliases", "entities")
        }
    finally:
        conn.close()


def entity_files(vault: Path) -> list[str]:
    root = vault / "Earmark" / "Entities"
    return sorted(str(p.relative_to(vault)) for p in root.rglob("*.md")) if root.exists() else []


def write_aliases(vault: Path, text: str) -> None:
    (vault / "Earmark").mkdir(parents=True, exist_ok=True)
    (vault / "Earmark" / "aliases.yaml").write_text(text, encoding="utf-8")


# -- B1: one note per entity -----------------------------------------------------------------


def test_same_person_two_episodes(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Ada Lovelace", "Wrote the first program", (65.0,)),))])
    put(store, 2, [an(2, people=(person("ada  LOVELACE.", "Mentioned again", (3.0,)),))])
    notes = sorted((vault / PEOPLE).iterdir())
    assert [p.name for p in notes] == ["Ada Lovelace.md"]
    text = notes[0].read_text(encoding="utf-8")
    meta = front(text)
    assert meta["name"] == "Ada Lovelace"
    assert meta["entity_id"] == ADA_ID
    assert meta["kind"] == "person"
    body = region(text)
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(lines) == 2
    assert "Épisode 2" in lines[0]
    assert "Épisode 1" in lines[1]
    assert all("2026-01-02" in ln for ln in lines)
    assert "— Mentioned again [00:00:03]" in lines[0]
    assert "— Wrote the first program [00:01:05]" in lines[1]
    assert "Also mentioned as: ada LOVELACE." in body
    assert "mentioned in 2 episodes" in body


def test_entity_region_exact(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    store.store(make_episode(), make_transcript(), [make_analysis(prompt=None)], T0)
    text = (vault / PEOPLE / "Zoë.md").read_text(encoding="utf-8")
    assert region(text) == (
        "# Zoë\n\n"
        "**Person** · mentioned in 1 episode\n\n"
        "## Episodes\n\n"
        "- 2026-01-02 · [[Earmark/Podcasts/Le Podcast ☕/2026-01-02 Épisode 1 - «Tests»"
        "|Épisode 1: «Tests»]] · Le Podcast ☕ — Cité en exemple [00:00:01] [00:00:02]\n"
    )
    assert text.startswith(
        '---\nearmark: "entity"\nentity_id: "ebd1f8ca95b62ffe"\nkind: "person"\nname: "Zoë"\n'
        'tags:\n  - "earmark"\n  - "entity"\n---\n'
    )


# -- B2: re-storing --------------------------------------------------------------------------


def test_restore_no_duplicate_mentions(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    first = an(1, people=(person("Ada Lovelace", "one", (1.0,)),))
    put(store, 1, [first])
    put(store, 1, [first])
    second = an(
        1,
        prompt="Q?",
        created_at=T0 + timedelta(hours=1),
        people=(person("ada lovelace", "two", (2.0,)),),
    )
    before = mtimes(vault)
    put(store, 1, [first])
    assert mtimes(vault) == before  # the second store wrote nothing
    put(store, 1, [first, second])
    rows = db_rows(tmp_path / "data")["mentions"]
    assert len(rows) == 2
    assert {(r[1], r[3], r[4]) for r in rows} == {
        (first.prompt_hash, "person", "ada lovelace"),
        (second.prompt_hash, "person", "ada lovelace"),
    }
    body = region((vault / PEOPLE / "Ada Lovelace.md").read_text(encoding="utf-8"))
    assert body.count("\n- 2026") == 1
    assert "[00:00:01] [00:00:02]" in body
    assert "mentioned in 1 episode\n" in body


def test_duplicates_within_one_analysis_merge() -> None:
    analysis = an(
        1,
        people=(
            person("Ada Lovelace", "", (5.0, 1.0)),
            person("ada lovelace", "second", (3.0,)),
            person("???", "skipped"),
            person("Bob"),
        ),
    )
    found = mentions_from([analysis])
    assert [(m.name, m.position, m.context, m.timestamps_s) for m in found] == [
        ("Ada Lovelace", 0, "second", (1.0, 3.0, 5.0)),
        ("Bob", 3, "ctx", (1.0,)),
    ]


# -- B3: aliases -----------------------------------------------------------------------------


def test_alias_merges_entities(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    write_aliases(vault, "people:\n  Ada Lovelace: [Ada King]\n")
    put(store, 1, [an(1, people=(person("Ada King"),))])
    put(store, 2, [an(2, people=(person("Ada Lovelace"),))])
    assert [p.name for p in (vault / PEOPLE).iterdir()] == ["Ada Lovelace.md"]
    body = region((vault / PEOPLE / "Ada Lovelace.md").read_text(encoding="utf-8"))
    assert "Épisode 1" in body
    assert "Épisode 2" in body
    assert "Also mentioned as: Ada King" in body
    note = next((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md"))
    assert f"[[{PEOPLE}/Ada Lovelace|Ada King]]" in note.read_text(encoding="utf-8")


def test_alias_added_later_merges_and_stubs(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Ada King"),))])
    put(store, 2, [an(2, people=(person("Ada Lovelace"),))])
    assert sorted(p.name for p in (vault / PEOPLE).iterdir()) == ["Ada King.md", "Ada Lovelace.md"]
    king = vault / PEOPLE / "Ada King.md"
    old_front = "".join(split_note(king.read_text(encoding="utf-8")).frontmatter or ())
    write_aliases(vault, "people:\n  Ada Lovelace: [Ada King]\n")
    result = put(store, 3, [an(3, topics=(person("Unrelated"),))])
    assert result.warnings == ()
    new_text = king.read_text(encoding="utf-8")
    assert "".join(split_note(new_text).frontmatter or ()) == old_front
    assert region(new_text) == (
        f"# Ada King\n\nMerged into [[{PEOPLE}/Ada Lovelace|Ada Lovelace]] by aliases.yaml.\n"
    )
    ep1 = next((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md"))
    assert f"[[{PEOPLE}/Ada Lovelace|Ada King]]" in ep1.read_text(encoding="utf-8")
    assert not (vault / "Earmark" / "Backups").exists()
    assert_links_resolve(vault)


def test_alias_removed_splits_again(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Ada King"),))])
    put(store, 2, [an(2, people=(person("Ada Lovelace"),))])
    write_aliases(vault, "people:\n  Ada Lovelace: [Ada King]\n")
    put(store, 3, [an(3, topics=(person("Unrelated"),))])
    (vault / "Earmark" / "aliases.yaml").unlink()
    result = put(store, 4, [an(4, topics=(person("Unrelated"),))])
    assert result.warnings == ()
    king = region((vault / PEOPLE / "Ada King.md").read_text(encoding="utf-8"))
    assert "Épisode 1" in king
    assert "Merged into" not in king
    lovelace = region((vault / PEOPLE / "Ada Lovelace.md").read_text(encoding="utf-8"))
    assert "Épisode 1" not in lovelace
    assert_links_resolve(vault)


def test_alias_pins_display_name(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    write_aliases(vault, 'topics:\n  "Large language models": LLMs\n')
    put(store, 1, [an(1, topics=(person("LLMs"),))])
    path = vault / "Earmark/Entities/Topics/Large language models.md"
    assert path.is_file()
    assert front(path.read_text(encoding="utf-8"))["name"] == "Large language models"
    assert "Also mentioned as: LLMs" in path.read_text(encoding="utf-8")


def test_broken_aliases_keep_previous(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    write_aliases(vault, "people:\n  Ada Lovelace: [Ada King]\n")
    put(store, 1, [an(1, people=(person("Ada King"),))])
    write_aliases(vault, "people: [oops\n")
    result = put(store, 2, [an(2, people=(person("Ada Lovelace"),))])
    assert any(
        "not valid YAML at line" in w and "the aliases loaded before are kept" in w
        for w in result.warnings
    )
    assert [p.name for p in (vault / PEOPLE).iterdir()] == ["Ada Lovelace.md"]


def test_entry_problems_become_warnings(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    write_aliases(vault, "foods: {}\npeople:\n  Ada: [Bob]\n")
    result = put(store, 1, [an(1, people=(person("Bob"),))])
    assert any("unknown kind foods" in w for w in result.warnings)
    assert (vault / PEOPLE / "Ada.md").is_file()


def test_missing_aliases_file_means_none(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    write_aliases(vault, "people:\n  Ada Lovelace: [Ada King]\n")
    put(store, 1, [an(1, people=(person("Ada King"),))])
    assert [p.name for p in (vault / PEOPLE).iterdir()] == ["Ada Lovelace.md"]
    (vault / "Earmark" / "aliases.yaml").unlink()
    put(store, 2, [an(2, people=(person("Ada Lovelace"),))])
    assert sorted(p.name for p in (vault / PEOPLE).iterdir()) == ["Ada King.md", "Ada Lovelace.md"]


# -- B4: links -------------------------------------------------------------------------------


def test_episode_note_links_every_entity(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = store.store(make_episode(), make_transcript(), [make_analysis(prompt=None)], T0)
    text = region((vault / result.note_path).read_text(encoding="utf-8"))
    for heading, folder in [
        ("People", "People"),
        ("Papers", "Papers"),
        ("Companies", "Companies"),
        ("Tools", "Tools"),
        ("Topics", "Topics"),
    ]:
        assert (
            f"### {heading}\n\n- **[[Earmark/Entities/{folder}/Zoë|Zoë]]**: Cité en exemple "
            "[00:00:01] [00:00:02]\n"
        ) in text
    assert_links_resolve(vault)


def test_all_links_resolve_after_many_operations(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    for n in (1, 2, 3):
        put(store, n, [an(n, people=(person("Ada Lovelace"),), topics=(person(f"Topic {n}"),))])
        assert_links_resolve(vault)
    (vault / PEOPLE / "Ada Lovelace.md").rename(vault / PEOPLE / "Ada K.md")
    archive = vault / "Earmark" / "Archive"
    archive.mkdir()
    note = next((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md"))
    shutil.move(note, archive / note.name)
    write_aliases(vault, "topics:\n  Topic 1: [Topic 2]\n")
    put(store, 4, [an(4, people=(person("Ada Lovelace"),), topics=(person("Topic 2"),))])
    assert_links_resolve(vault)
    ada = (vault / PEOPLE / "Ada K.md").read_text(encoding="utf-8")
    assert "Earmark/Archive/2026-01-02 Épisode 1|Épisode 1" in ada
    assert ada.count("\n- 2026") == 4


def test_unlinkable_name_rendered_plain(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = put(store, 1, [an(1, people=(person("???"),))])
    text = (vault / result.note_path).read_text(encoding="utf-8")
    assert "- **???**" in text
    assert entity_files(vault) == []
    assert db_rows(tmp_path / "data")["mentions"] == []
    assert db_rows(tmp_path / "data")["entities"] == []


def test_markdown_link_fallback(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path, subdir="Notes#1")
    result = store.store(make_episode(), make_transcript(), [make_analysis(prompt=None)], T0)
    text = (vault / result.note_path).read_text(encoding="utf-8")
    assert "- **[Zoë](Notes%231/Entities/People/Zo%C3%AB.md)**" in text
    entity = (vault / "Notes#1/Entities/People/Zoë.md").read_text(encoding="utf-8")
    assert "](Notes%231/Podcasts/Le%20Podcast%20%E2%98%95/" in entity
    assert_links_resolve(vault)


# -- B5: user text ---------------------------------------------------------------------------


def _first_zoe(store: VaultStore, vault: Path) -> Path:
    store.store(make_episode(), make_transcript(), [make_analysis(prompt=None)], T0)
    return vault / PEOPLE / "Zoë.md"


def test_entity_note_user_text_survives(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = _first_zoe(store, vault)
    text = path.read_text(encoding="utf-8")
    text = text.replace('name: "Zoë"\n', 'name: "Zoë"\nborn: 1815\n').replace(
        '  - "entity"\n', '  - "entity"\n  - "mine"\n'
    )
    text = text.replace("<!-- earmark:begin", "My own paragraph.\n\n<!-- earmark:begin", 1)
    text += "\nTrailing user notes.\n"
    path.write_text(text, encoding="utf-8")
    put(store, 2, [an(2, people=(person("Zoë"),))])
    new = path.read_text(encoding="utf-8")
    assert "My own paragraph.\n\n<!-- earmark:begin" in new
    assert new.endswith("<!-- earmark:end -->\n\nTrailing user notes.\n")
    meta = front(new)
    assert meta["born"] == 1815
    assert meta["tags"] == ["earmark", "entity", "mine"]
    assert region(new).count("\n- 2026") == 2
    assert not (vault / "Earmark" / "Backups").exists()


def test_entity_note_hand_edit_backed_up(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = _first_zoe(store, vault)
    path.write_text(
        path.read_text(encoding="utf-8").replace("# Zoë", "# Zoë edited by me"), encoding="utf-8"
    )
    result = put(store, 2, [an(2, people=(person("Zoë"),))])
    assert any("edited by hand" in w and "Backups/" in w for w in result.warnings)
    backups = list((vault / "Earmark" / "Backups").iterdir())
    assert len(backups) == 1
    assert "edited by me" in backups[0].read_text(encoding="utf-8")
    assert "edited by me" not in path.read_text(encoding="utf-8")


def test_entity_note_damaged_markers_frozen(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    path = _first_zoe(store, vault)
    damaged = path.read_text(encoding="utf-8").replace("<!-- earmark:end -->\n", "")
    path.write_text(damaged, encoding="utf-8")
    result = put(store, 2, [an(2, people=(person("Zoë"),))])
    assert any(str(PEOPLE) in w and "markers are missing or damaged" in w for w in result.warnings)
    assert path.read_text(encoding="utf-8") == damaged
    again = put(store, 3, [an(3, topics=(person("Else"),))])
    assert again.warnings == ()
    assert path.read_text(encoding="utf-8") == damaged
    assert_links_resolve(vault)


def test_obsidian_link_rewrite_not_an_edit(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    result = put(store, 1, [an(1, people=(person("Zoë"),))])
    note = vault / result.note_path
    text = note.read_text(encoding="utf-8")
    old = f"[[{PEOPLE}/Zoë|Zoë]]"
    assert old in text
    note.write_text(text.replace(old, "[[Zoë2|Zoë]]"), encoding="utf-8")
    (vault / PEOPLE / "Zoë.md").rename(vault / PEOPLE / "Zoë2.md")
    out = put(store, 2, [an(2, topics=(person("Else"),))])
    assert out.warnings == ()
    assert not (vault / "Earmark" / "Backups").exists()
    assert f"[[{PEOPLE}/Zoë2|Zoë]]" in note.read_text(encoding="utf-8")
    assert_links_resolve(vault)


# -- B7: hostile names -----------------------------------------------------------------------

HOSTILE = [
    "../../x",
    "/etc/passwd",
    ".obsidian",
    "CON",
    "a/b\\c",
    "[[x]]",
    "a|b",
    "#tag ^x",
    "<!-- earmark:end -->",
    "---",
    "x: y",
    "1000 × é",  # noqa: RUF001
    "Zoë‮",
]


@pytest.mark.parametrize("name", HOSTILE)
@pytest.mark.parametrize("field", ["people", "topics"])
def test_hostile_entity_names(tmp_path: Path, name: str, field: str) -> None:
    store, vault = make_store(tmp_path)
    fields: dict[str, Any] = {field: (person(name),)}
    result = put(store, 1, [an(1, **fields)])
    folder = "People" if field == "people" else "Topics"
    assert {p.name for p in vault.iterdir()} == {"Earmark"}
    files = entity_files(vault)
    if not entity_key(name):
        assert files == []
        return
    assert len(files) == 1
    rel = Path(files[0])
    assert rel.parent == Path("Earmark/Entities") / folder
    assert is_safe_component(rel.name)
    assert len(rel.name.encode()) <= 200
    text = (vault / rel).read_text(encoding="utf-8")
    assert front(text)["name"] == " ".join(name.split())
    lines = text.splitlines()
    assert sum(ln.startswith("<!-- earmark:begin") for ln in lines) == 1
    assert lines.count("<!-- earmark:end -->") == 1
    note = (vault / result.note_path).read_text(encoding="utf-8")
    assert f"[[{rel.with_suffix('')}|" in note or f"]({rel.parent}" in note
    assert_links_resolve(vault)


def test_entity_name_collision_with_user_note(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    (vault / PEOPLE).mkdir(parents=True)
    mine = vault / PEOPLE / "Ada Lovelace.md"
    mine.write_bytes(b"my note, no frontmatter\n")
    put(store, 1, [an(1, people=(person("Ada Lovelace"),))])
    assert mine.read_bytes() == b"my note, no frontmatter\n"
    assert (vault / PEOPLE / "Ada Lovelace (82a35590).md").is_file()
    assert_links_resolve(vault)


def test_symbol_names_do_not_collide(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, tools=(person("C"), person("C++"), person("C#")))])
    names = sorted(p.name for p in (vault / "Earmark/Entities/Tools").iterdir())
    from earmark.vault.entities import entity_id

    cs = f"C ({entity_id('tool', 'c#')[:8]}).md"
    assert names == sorted(["C.md", "C++.md", cs])
    assert_links_resolve(vault)


# -- C1: surrogates through the store --------------------------------------------------------


def test_lone_surrogates_through_store(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    bad = "x\ud800y"
    episode = make_episode().model_copy(update={"title": bad, "show_title": bad})
    transcript = make_transcript()
    segment = transcript.segments[0].model_copy(update={"text": bad})
    transcript = transcript.model_copy(update={"segments": (segment, *transcript.segments[1:])})
    entity = Entity.model_construct(name=bad, context=bad, timestamps_s=(1.0,))
    analysis = make_analysis(prompt="p").model_copy(
        update={"summary": bad, "prompt": bad, "people": (entity,)}
    )
    analysis = analysis.model_copy(update={"prompt_hash": analysis.prompt_hash})
    result = store.store(episode, transcript, [analysis], T0)
    for path in vault.rglob("*.md"):
        text = path.read_bytes().decode("utf-8")  # strict
        assert "\ud800" not in text
    assert "�" in (vault / result.note_path).read_text(encoding="utf-8")
    before = mtimes(vault)
    store.store(episode, transcript, [analysis], T0)
    assert mtimes(vault) == before


# -- C2: renames and moves -------------------------------------------------------------------


def test_renamed_entity_relinks_other_episodes(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Zoë"),))])
    old = vault / PEOPLE / "Zoë.md"
    old_bytes = old.read_bytes()
    old.rename(vault / PEOPLE / "Zoë Z.md")
    put(store, 2, [an(2, topics=(person("Else"),))])
    ep1 = next((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md"))
    assert f"[[{PEOPLE}/Zoë Z|Zoë]]" in ep1.read_text(encoding="utf-8")
    assert (vault / PEOPLE / "Zoë Z.md").read_bytes() == old_bytes
    assert_links_resolve(vault)


def test_moved_episode_note_updates_entity_and_transcript(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Zoë"),))])
    note = next((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md"))
    (vault / "Earmark" / "Archive").mkdir()
    moved = vault / "Earmark" / "Archive" / note.name
    shutil.move(note, moved)
    put(store, 2, [an(2, topics=(person("Else"),))])
    assert "Earmark/Archive/2026-01-02 Épisode 1|Épisode 1" in (
        vault / PEOPLE / "Zoë.md"
    ).read_text(encoding="utf-8")
    tr = next((vault / "Earmark/Podcasts/Le Podcast ☕/transcripts").glob("*Épisode 1 (tr*"))
    assert "[[Earmark/Archive/2026-01-02 Épisode 1|Episode note]]" in tr.read_text(encoding="utf-8")
    assert_links_resolve(vault)


def test_deleted_entity_note_recreated(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Zoë"),))])
    (vault / PEOPLE / "Zoë.md").unlink()
    put(store, 2, [an(2, topics=(person("Else"),))])
    assert "Épisode 1" in region((vault / PEOPLE / "Zoë.md").read_text(encoding="utf-8"))
    assert_links_resolve(vault)


def test_deleted_episode_note_dropped_from_entities(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Zoë"),))])
    next((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md")).unlink()
    put(store, 2, [an(2, people=(person("Zoë"),))])
    body = region((vault / PEOPLE / "Zoë.md").read_text(encoding="utf-8"))
    assert "Épisode 1" not in body
    assert "Épisode 2" in body
    assert not list((vault / "Earmark/Podcasts/Le Podcast ☕").glob("*Épisode 1.md"))


def test_relink_with_missing_artifacts_warns_once(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Zoë"),))])
    shutil.rmtree(ArtifactStore(tmp_path / "data").episode_dir(ep(1).key))
    (vault / PEOPLE / "Zoë.md").rename(vault / PEOPLE / "Zoë Z.md")
    first = put(store, 2, [an(2, topics=(person("Else"),))])
    assert any("could not update its links" in w for w in first.warnings)
    second = put(store, 3, [an(3, topics=(person("Else"),))])
    assert not any("could not update its links" in w for w in second.warnings)


@pytest.mark.parametrize("tz", [UTC, ZoneInfo("America/Los_Angeles")])
def test_entity_dates_use_tz(tmp_path: Path, tz: Any) -> None:
    store, vault = make_store(tmp_path, tz=tz)
    episode = make_episode().model_copy(
        update={"published_at": datetime(2026, 1, 2, 3, 30, tzinfo=UTC)}
    )
    store.store(episode, make_transcript(), [make_analysis(prompt=None)], T0)
    expected = "2026-01-02" if tz is UTC else "2026-01-01"
    assert f"- {expected} · " in (vault / PEOPLE / "Zoë.md").read_text(encoding="utf-8")


# -- C3: idempotence -------------------------------------------------------------------------


def test_second_store_writes_nothing(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    put(store, 1, [an(1, people=(person("Zoë"),))])
    put(store, 2, [an(2, people=(person("Zoë"),))])
    before = mtimes(vault)
    rows = db_rows(tmp_path / "data")["entities"]
    put(store, 2, [an(2, people=(person("Zoë"),))])
    assert mtimes(vault) == before
    assert db_rows(tmp_path / "data")["entities"] == rows


def test_two_vaults_identical_with_entities(tmp_path: Path) -> None:
    trees = []
    for name in ("one", "two"):
        store, vault = make_store(tmp_path / name)
        put(store, 1, [an(1, people=(person("Ada"),), tools=(person("C"), person("C#")))])
        put(store, 2, [an(2, people=(person("Ada"),))])
        trees.append(tree(vault))
    assert trees[0] == trees[1]


# -- C4: errors ------------------------------------------------------------------------------


def test_registry_busy_is_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, vault = make_store(tmp_path)

    def busy(*_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(EntityRegistry, "record_episode", busy)
    with pytest.raises(TransientError, match="busy or unavailable"):
        put(store, 1, [an(1, people=(person("Zoë"),))])
    assert list(vault.rglob("*.md")) == []


def test_newer_registry_schema_is_permanent(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    db = open_database(database_path(tmp_path / "data"))
    with db.transaction() as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(PermanentError, match="entity registry database cannot be used"):
        put(store, 1, [an(1, people=(person("Zoë"),))])
    assert list(vault.rglob("*.md")) == []


def test_symlinked_entities_dir_refused(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "Earmark").mkdir()
    (vault / "Earmark" / "Entities").symlink_to(outside)
    with pytest.raises(SymlinkRefused):
        put(store, 1, [an(1, people=(person("Zoë"),))])
    assert list(outside.iterdir()) == []


# -- B6 and C5: rebuild and the pipeline -----------------------------------------------------

URL = "https://podcasts.apple.com/us/podcast/id1200361736?i=100080290798"


def _vault_env(tmp_path: Path) -> PipelineEnv:
    (tmp_path / "vault").mkdir(exist_ok=True)
    fakes = make_fake_backends(tmp_path / "data", vault_dir=tmp_path / "vault")
    store = VaultStore(tmp_path / "vault", "Earmark", data_dir=tmp_path / "data", tz=UTC)
    return make_env(tmp_path, backends=replace(fakes.backends(), store=store), fakes=fakes)


def _ingest(env: PipelineEnv, n: int, prompt: str | None = None) -> None:
    env.jobs.enqueue(f"{URL}{n}", prompt=prompt)
    job = env.worker.run_once()
    assert job is not None
    assert job.status == "done", job.error


def _store_of(tmp_path: Path) -> VaultStore:
    return VaultStore(tmp_path / "vault", "Earmark", data_dir=tmp_path / "data", tz=UTC)


def _three_episodes(tmp_path: Path) -> PipelineEnv:
    env = _vault_env(tmp_path)
    write_aliases(tmp_path / "vault", "companies:\n  Acme: [Acme Corp]\n")
    _ingest(env, 1)
    _ingest(env, 2)
    _ingest(env, 2, prompt="What else?")
    _ingest(env, 3)
    return env


def test_pipeline_end_to_end_entities(tmp_path: Path) -> None:
    env = _vault_env(tmp_path)
    _ingest(env, 1)
    _ingest(env, 2)
    ada = (tmp_path / "vault" / PEOPLE / "Ada Lovelace.md").read_text(encoding="utf-8")
    assert region(ada).count("\n- 2026") == 2
    assert_links_resolve(tmp_path / "vault")


def test_rebuild_reproduces_registry_and_notes(tmp_path: Path) -> None:
    _three_episodes(tmp_path)
    vault = tmp_path / "vault"
    files, stamps, rows = tree(vault), mtimes(vault), db_rows(tmp_path / "data")
    assert any(r[0] == "companies" or "company" in r for r in rows["aliases"])
    for suffix in ("", "-wal", "-shm"):
        Path(f"{database_path(tmp_path / 'data')}{suffix}").unlink(missing_ok=True)
    open_database(database_path(tmp_path / "data"))
    report = _store_of(tmp_path).rebuild()
    assert report.written == 0
    assert report.episodes == 3
    assert report.warnings == ()
    assert tree(vault) == files
    assert mtimes(vault) == stamps
    rebuilt = db_rows(tmp_path / "data")
    for table in ("episodes", "mentions", "aliases", "entities"):
        assert rebuilt[table] == rows[table], table


def test_rebuild_repairs_vault(tmp_path: Path) -> None:
    _three_episodes(tmp_path)
    vault = tmp_path / "vault"
    files = tree(vault)
    (vault / PEOPLE / "Ada Lovelace.md").unlink()
    report = _store_of(tmp_path).rebuild()
    assert report.written == 1
    assert tree(vault) == files


def test_rebuild_links_step12_notes(tmp_path: Path) -> None:
    env = _vault_env(tmp_path)
    _ingest(env, 1)
    vault = tmp_path / "vault"
    shutil.rmtree(vault / "Earmark" / "Entities")
    note = next((vault / "Earmark/Podcasts/Fake Show").glob("*.md"))
    note.write_text(note.read_text(encoding="utf-8").replace("[[Earmark/Entities/People/", "[["))
    report = _store_of(tmp_path).rebuild()
    assert report.written >= 1
    assert "[[Earmark/Entities/People/Ada Lovelace|Ada Lovelace]]" in note.read_text(
        encoding="utf-8"
    )
    assert not (vault / "Earmark" / "Backups").exists()
    assert_links_resolve(vault)


def test_rebuild_refuses_without_artifacts(tmp_path: Path) -> None:
    env = _three_episodes(tmp_path)
    vault = tmp_path / "vault"
    key = next(iter(env.fakes.artifacts.list_episode_keys()))
    shutil.rmtree(env.fakes.artifacts.episode_dir(key))
    files, rows = tree(vault), db_rows(tmp_path / "data")
    with pytest.raises(RebuildRefused, match="1 episode notes in the vault"):
        _store_of(tmp_path).rebuild()
    assert tree(vault) == files
    assert db_rows(tmp_path / "data") == rows
    report = _store_of(tmp_path).rebuild(prune=True)
    assert report.episodes == 2
    ada = (vault / PEOPLE / "Ada Lovelace.md").read_text(encoding="utf-8")
    assert region(ada).count("\n- 2026") == 2


def test_rebuild_bad_aliases_aborts(tmp_path: Path) -> None:
    from earmark.vault.aliases import AliasFileError

    _three_episodes(tmp_path)
    vault = tmp_path / "vault"
    write_aliases(vault, "people: [oops\n")
    files, rows = tree(vault), db_rows(tmp_path / "data")
    with pytest.raises(AliasFileError, match="line 2"):
        _store_of(tmp_path).rebuild()
    assert db_rows(tmp_path / "data") == rows
    assert tree(vault) == files


def test_rebuild_skips_incomplete(tmp_path: Path) -> None:
    env = _vault_env(tmp_path)
    _ingest(env, 1)
    key = env.fakes.artifacts.list_episode_keys()[0]
    other = env.fakes.artifacts.episode_dir("0" * 20)
    other.mkdir(parents=True)
    (other / "audio.mp3").write_bytes(b"x")  # a directory without episode.json
    env.fakes.artifacts.transcript_json_path(key).unlink()
    report = _store_of(tmp_path).rebuild(prune=True)
    assert report.skipped == 2
    assert report.episodes == 0


def test_rebuild_unlinked_registry_is_empty_vault(tmp_path: Path) -> None:
    store, vault = make_store(tmp_path)
    report = store.rebuild()
    assert (report.episodes, report.entities, report.written) == (0, 0, 0)
    assert vault.exists()
