"""End to end: collect, theme, write, with a real VaultStore in tmp_path."""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from earmark.artifacts import ArtifactStore
from earmark.digest.cache import DigestCache
from earmark.digest.collect import collect, digest_input_hash
from earmark.digest.run import DigestReport, run_digest
from earmark.digest.themes import LlmThemer
from earmark.digest.window import DigestWindow, since_window, week_window
from earmark.errors import PermanentError, TransientError
from earmark.fakes import FakeStore, FakeThemer
from earmark.models import Episode
from earmark.vault.fs import vault_lock
from earmark.vault.notes import VaultStore
from tests.digest_helpers import (
    Lead,
    assert_digest_links_resolve,
    put_episode,
    small_client,
    themes_answer,
)
from tests.llm_fakes import ScriptedLLMClient

WINDOW = week_window(2026, 47, UTC)
SHARED = WINDOW.start + timedelta(hours=5)
NOTE = "Earmark/Digests/2026-W47.md"


class Env:
    def __init__(self, tmp_path: Path, *, lock_timeout_s: float = 60.0) -> None:
        self.data = tmp_path / "data"
        self.vault = tmp_path / "vault"
        self.vault.mkdir()
        self.artifacts = ArtifactStore(self.data)
        self.store = VaultStore(
            self.vault, "Earmark", data_dir=self.data, tz=UTC, lock_timeout_s=lock_timeout_s
        )
        self.cache = DigestCache(self.data)

    def episode(
        self,
        guid: str,
        analyses: list[list[Lead]],
        *,
        hours: float = 5,
        show: str = "Show A",
        store: bool = True,
    ) -> Episode:
        shared = WINDOW.start + timedelta(hours=hours)
        ep = put_episode(self.artifacts, guid, shared, show=show, analyses=analyses)
        if store:
            self.restore(ep)
        return ep

    def restore(self, ep: Episode) -> None:
        transcript = self.artifacts.read_transcript(ep.key)
        share = self.artifacts.read_share(ep.key)
        assert transcript is not None
        assert share is not None
        self.store.store(
            ep, transcript, self.artifacts.list_analyses(ep.key), share.first_shared_at
        )

    def run(
        self,
        client: ScriptedLLMClient,
        *,
        window: DigestWindow = WINDOW,
        refresh: bool = False,
        tz: ZoneInfo | None = None,
    ) -> DigestReport:
        return run_digest(
            window,
            artifacts=self.artifacts,
            cache=self.cache,
            themer=LlmThemer(client, self.cache, repair_invalid_json=False),
            writer=self.store,
            tz=tz or UTC,
            tz_label="UTC",
            refresh=refresh,
        )

    @property
    def note(self) -> Path:
        return self.vault / NOTE

    def text(self) -> str:
        return self.note.read_text(encoding="utf-8")


def client_for(*themes: tuple[str, str, list[str]], **kw: object) -> ScriptedLLMClient:
    params: dict[str, object] = {"token_count": 100}
    params.update(kw)
    return ScriptedLLMClient([themes_answer(*themes)], **params)  # type: ignore[arg-type]


def three_episodes(env: Env) -> None:
    env.episode("e1", [["alpha lead", ("beta lead", "why beta", 65.0)]], hours=1)
    env.episode("e2", [["gamma lead"]], hours=2, show="Show B")
    env.episode("e3", [["delta lead"]], hours=3)


# -- acceptance: grouping and links ---------------------------------------------------------------


def test_groups_leads_into_themes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    three_episodes(env)
    client = client_for(
        ("Theme One", "First summary.", ["L1", "L3"]),
        ("Theme Two", "Second summary.", ["L2", "L4"]),
    )
    report = env.run(client)
    assert (report.status, report.episodes, report.leads, report.themes) == ("created", 3, 4, 2)
    assert report.requests == 1
    assert report.note_path == NOTE
    text = env.text()
    assert text.index("## Theme One") < text.index("## Theme Two") < text.index("## Episodes")
    one = text[text.index("## Theme One") : text.index("## Theme Two")]
    two = text[text.index("## Theme Two") : text.index("## Episodes")]
    assert "alpha lead" in one
    assert "gamma lead" in one
    assert "delta" not in one
    assert "beta lead" in two
    assert "delta lead" in two
    assert "alpha" not in two
    # The episode link and the timestamp link, to the transcript note.
    assert "[[Earmark/Podcasts/Show A/" in one
    assert re.search(r"beta lead\*\* — why beta · \[\[Earmark/Podcasts/Show A/[^|\]]+\|", two)
    assert re.search(
        r"\[\[Earmark/Podcasts/Show A/transcripts/[^|\]]+ \(transcript\)\|00:01:05\]\]", two
    )
    assert 'model: "claude-test"' in text
    assert 'digest: "2026-W47"' in text


def test_every_link_resolves(tmp_path: Path) -> None:
    env = Env(tmp_path)
    three_episodes(env)
    env.run(client_for(("Only", "s", ["L1", "L2", "L3", "L4"])))
    assert assert_digest_links_resolve(env.vault, env.text()) >= 8


def test_lead_without_note_is_plain_text(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("with", [["has a note"]], hours=1)
    env.episode("without", [[("no note lead", "r", 7.0)]], hours=2, store=False)
    env.run(client_for(("T", "s", ["L1", "L2"])))
    text = env.text()
    assert "(no note)" in text
    assert "[00:00:07]" in text
    assert_digest_links_resolve(env.vault, text)


def test_model_cannot_add_links(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(
        client_for(
            (
                "[[Evil]] <b>x</b>",
                "[x](https://evil.example) <b> <!-- earmark:end -->",
                ["L1", "L99"],
            )
        )
    )
    text = env.text()
    assert "[[Evil" not in text
    assert not re.search(r"(?<!\\)\]\(https://evil", text)
    assert "\\[x\\](https://evil.example)" in text
    assert "<b>" not in text
    assert text.count("<!-- earmark:end -->") == 1
    assert "\\[\\[Evil\\]\\]" in text
    assert_digest_links_resolve(env.vault, text)
    # The invented id attached nothing and is reported.
    assert "L99" not in text
    report = env.run(client_for(("x", "y", ["L1"])), refresh=True)
    assert report.status in {"updated", "unchanged"}


def test_invented_id_is_reported(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    report = env.run(client_for(("A", "s", ["L1", "L99"])))
    assert any("not in the request" in w for w in report.warnings)
    assert "not in the request" in env.text()


def test_fake_themer_end_to_end(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    vault = tmp_path / "vault"
    vault.mkdir()
    fake = FakeStore(vault, "Earmark")
    eps = [
        put_episode(artifacts, "a", SHARED, show="Show A", analyses=[["one"]]),
        put_episode(artifacts, "b", SHARED + timedelta(hours=1), show="Show B", analyses=[["two"]]),
        put_episode(
            artifacts, "c", SHARED + timedelta(hours=2), show="Show A", analyses=[["three"]]
        ),
    ]
    for ep in eps:
        transcript = artifacts.read_transcript(ep.key)
        assert transcript is not None
        fake.store(ep, transcript, artifacts.list_analyses(ep.key), SHARED)
    themer = FakeThemer()
    report = run_digest(
        WINDOW,
        artifacts=artifacts,
        cache=DigestCache(tmp_path / "data"),
        themer=themer,
        writer=fake,
        tz=UTC,
        tz_label="UTC",
    )
    assert report.status == "created"
    assert report.themes == 2
    text = (vault / NOTE).read_text()
    assert text.index("## Show A") < text.index("## Show B")
    assert_digest_links_resolve(vault, text)
    again = run_digest(
        WINDOW,
        artifacts=artifacts,
        cache=DigestCache(tmp_path / "data"),
        themer=themer,
        writer=fake,
        tz=UTC,
        tz_label="UTC",
    )
    assert again.status == "unchanged"
    assert again.cached


# -- empty and lead-less windows -----------------------------------------------------------------


def test_empty_window_no_call_no_note(tmp_path: Path) -> None:
    env = Env(tmp_path)
    put_episode(env.artifacts, "out", WINDOW.end + timedelta(hours=1), analyses=[["x"]])
    client = client_for(("A", "s", ["L1"]))
    report = env.run(client)
    assert report.status == "empty"
    assert report.note_path is None
    assert (report.episodes, report.leads, report.themes) == (0, 0, 0)
    assert (client.complete_calls, client.count_calls) == (0, 0)
    assert not (env.vault / "Earmark").exists()
    assert not (env.data / "digests").exists()
    fake = FakeStore()
    again = run_digest(
        WINDOW,
        artifacts=env.artifacts,
        cache=env.cache,
        themer=FakeThemer(),
        writer=fake,
        tz=UTC,
        tz_label="UTC",
    )
    assert again.status == "empty"
    assert fake.digest_calls == []


def test_no_leads_no_call_note_written(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [], hours=1)
    env.episode("e2", [[]], hours=2)  # analysed, no leads
    client = client_for(("A", "s", ["L1"]))
    report = env.run(client)
    assert report.status == "created"
    assert (client.complete_calls, client.count_calls) == (0, 0)
    text = env.text()
    assert "*No research leads in these episodes.*" in text
    assert "no analysis yet" in text
    assert "0 research leads" in text
    assert "model: null" in text
    assert "## " not in text.split("## Episodes")[0].split("\n\n", 2)[2]
    assert not (env.data / "digests").exists()


# -- determinism ---------------------------------------------------------------


def test_second_run_same_bytes_no_calls(tmp_path: Path) -> None:
    env = Env(tmp_path)
    three_episodes(env)
    client = client_for(("T", "s", ["L1", "L2", "L3", "L4"]))
    first = env.run(client)
    before = env.note.stat().st_mtime_ns
    data = env.note.read_bytes()
    calls = (client.count_calls, client.complete_calls)
    second = env.run(client)
    assert first.status == "created"
    assert second.status == "unchanged"
    assert second.cached
    assert second.requests == 0
    assert (client.count_calls, client.complete_calls) == calls
    assert env.note.read_bytes() == data
    assert env.note.stat().st_mtime_ns == before
    assert second.warnings == first.warnings


def test_user_text_outside_markers_survives(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    raw = env.text()
    head, rest = raw.split("---\n", 2)[1], raw.split("---\n", 2)[2]
    edited = f"---\n{head}custom: kept\n---\nmy intro\r\n{rest}\nmy outro\r\nlast\r\n"
    env.note.write_bytes(edited.encode())
    same = env.run(client_for(("T", "s", ["L1"])))
    assert same.status == "unchanged"
    assert env.note.read_bytes() == edited.encode()
    env.episode("e2", [["beta"]], hours=2)
    changed = env.run(client_for(("T", "s", ["L1", "L2"])))
    assert changed.status == "updated"
    assert changed.warnings == ()  # not a hand edit
    new = env.note.read_bytes().decode()
    assert "custom: kept\n" in new
    assert "my intro\r\n" in new
    assert new.endswith("\nmy outro\r\nlast\r\n")
    assert "beta" in new


def test_hand_edit_backed_up(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    env.note.write_text(env.text().replace("## Episodes", "## My own heading"))
    env.episode("e2", [["beta"]], hours=2)
    report = env.run(client_for(("T", "s", ["L1", "L2"])))
    assert report.status == "updated"
    assert any("edited by hand" in w and "Backups/" in w for w in report.warnings)
    backups = list((env.vault / "Earmark" / "Backups").iterdir())
    assert len(backups) == 1
    assert "My own heading" in backups[0].read_text()
    assert "My own heading" not in env.text()


# -- cache ---------------------------------------------------------------------


def _hash(env: Env, model: str = "claude-test") -> str:
    return digest_input_hash(model, collect(env.artifacts, WINDOW))


def test_corrupt_record_recomputed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    (env.data / "digests" / f"{_hash(env)}.json").write_text("{corrupt")
    client = client_for(("T", "s", ["L1"]))
    report = env.run(client)
    assert client.complete_calls == 1
    assert report.status == "unchanged"
    assert json.loads((env.data / "digests" / f"{_hash(env)}.json").read_text())["themes"]


def test_record_with_stale_ids_revalidated(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    path = env.data / "digests" / f"{_hash(env)}.json"
    record = json.loads(path.read_text())
    record["themes"][0]["leads"].append("L99")
    path.write_text(json.dumps(record))
    client = client_for(("never", "s", ["L1"]))
    report = env.run(client)
    assert client.complete_calls == 0
    assert any("named 1 ids that are not in this digest" in w for w in report.warnings)
    assert "L99" not in env.text()


def test_new_analysis_changes_hash_and_regenerates(tmp_path: Path) -> None:
    env = Env(tmp_path)
    ep = env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    first = _hash(env)
    put_episode(env.artifacts, "e1", SHARED, analyses=[["alpha"], ["extra"]])
    env.restore(ep)
    assert _hash(env) != first
    client = client_for(("T", "s", ["L1", "L2"]))
    report = env.run(client)
    assert client.complete_calls == 1
    assert report.status == "updated"
    assert "extra" in env.text()


def test_model_change_regenerates(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    other = client_for(("T2", "s", ["L1"]), model="another-model")
    report = env.run(other)
    assert other.complete_calls == 1
    assert report.status == "updated"
    assert 'model: "another-model"' in env.text()


def test_refresh_asks_again(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    client = client_for(("Better title", "s", ["L1"]))
    report = env.run(client, refresh=True)
    assert client.complete_calls == 1
    assert not report.cached
    assert "Better title" in env.text()
    # The new record is what a later plain run reads.
    quiet = client_for(("never", "s", ["L1"]))
    assert env.run(quiet).status == "unchanged"
    assert quiet.complete_calls == 0


def test_calls_cleared_after_record(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for i in range(40):
        env.episode(
            f"g{i}",
            [[f"a research lead about a subject number {i} with detail"]],
            hours=i % 100 / 10,
        )
    client = small_client()
    report = env.run(client)
    assert report.requests >= 3
    root = env.data / "digests"
    assert [p.name for p in root.iterdir() if p.is_dir()] == []
    assert len(list(root.glob("*.json"))) == 1


# -- vault behaviour -----------------------------------------------------------


def test_digest_note_found_after_move(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    moved = env.vault / "Earmark" / "Notes" / "x.md"
    moved.parent.mkdir()
    env.note.rename(moved)
    env.episode("e2", [["beta"]], hours=2)
    report = env.run(client_for(("T", "s", ["L1", "L2"])))
    assert report.status == "updated"
    assert report.note_path == "Earmark/Notes/x.md"
    assert "beta" in moved.read_text()
    assert not env.note.exists()


def test_episode_rename_relinks_without_llm(tmp_path: Path) -> None:
    env = Env(tmp_path)
    ep = env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    old = next((env.vault / "Earmark" / "Podcasts").rglob(f"*Episode {ep.guid}.md"))
    new = old.with_name("Renamed.md")
    old.rename(new)
    client = client_for(("never", "s", ["L1"]))
    report = env.run(client)
    assert client.complete_calls == 0
    assert report.status == "updated"
    assert "Renamed|" in env.text()
    assert_digest_links_resolve(env.vault, env.text())


def test_name_collision_gets_suffix(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    taken = env.vault / "Earmark" / "Digests" / "2026-W47.md"
    taken.parent.mkdir(parents=True)
    taken.write_text("mine\n")
    report = env.run(client_for(("T", "s", ["L1"])))
    assert report.note_path == "Earmark/Digests/2026-W47 (digest).md"
    assert taken.read_text() == "mine\n"
    again = env.run(client_for(("T", "s", ["L1"])))
    assert again.status == "unchanged"
    assert again.note_path == report.note_path


def test_damaged_markers_untouched_with_warning(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    damaged = env.text().replace("<!-- earmark:end -->\n", "")
    env.note.write_text(damaged)
    env.episode("e2", [["beta"]], hours=2)
    report = env.run(client_for(("T", "s", ["L1", "L2"])))
    assert report.status == "unchanged"
    assert any("markers are missing or damaged" in w for w in report.warnings)
    assert env.text() == damaged


def test_store_and_rebuild_ignore_digest_notes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    digest = env.note.read_bytes()
    before = {p: p.read_bytes() for p in (env.vault / "Earmark" / "Podcasts").rglob("*.md")}
    report = env.store.rebuild()
    assert report.warnings == ()
    assert {p: p.read_bytes() for p in before} == before
    assert env.note.read_bytes() == digest
    ep = env.artifacts.read_episode(next(iter(env.artifacts.list_episode_keys())))
    assert ep is not None
    paths = env.store.locate_notes([ep.key])
    note_path = paths[ep.key].note_path
    assert note_path is not None
    assert note_path.startswith("Earmark/Podcasts/")


def test_digests_dir_symlink_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    (env.vault / "Earmark" / "Digests").symlink_to(outside)
    with pytest.raises(PermanentError):
        env.run(client_for(("T", "s", ["L1"])))
    assert list(outside.iterdir()) == []


def test_lock_timeout_transient(tmp_path: Path) -> None:
    env = Env(tmp_path, lock_timeout_s=0.2)
    env.episode("e1", [["alpha"]], hours=1)
    with (
        vault_lock(env.data / "vault.lock", timeout_s=1),
        pytest.raises(TransientError),
    ):
        env.run(client_for(("T", "s", ["L1"])))
    assert not env.note.exists()


def test_stale_temp_files_removed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    stale = env.note.with_name(".earmark-0123456789abcdef.tmp")
    stale.write_text("x")
    old = datetime.now(UTC).timestamp() - 7200
    os.utime(stale, (old, old))
    env.episode("e2", [["beta"]], hours=2)
    env.run(client_for(("T", "s", ["L1", "L2"])))
    assert not stale.exists()


# -- time zones and windows ----------------------------------------------------


def test_window_uses_settings_tz(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "data")
    # Sunday 2026-11-22 20:00Z: Monday 09:00 in Auckland (W48), Sunday 12:00 in Los Angeles (W47).
    put_episode(artifacts, "g", datetime(2026, 11, 22, 20, tzinfo=UTC), analyses=[["x"]])
    counts = {}
    for name in ("Pacific/Auckland", "America/Los_Angeles"):
        tz = ZoneInfo(name)
        fake = FakeStore()
        report = run_digest(
            week_window(2026, 47, tz),
            artifacts=artifacts,
            cache=DigestCache(tmp_path / "data"),
            themer=FakeThemer(),
            writer=fake,
            tz=tz,
            tz_label=name,
        )
        counts[name] = report.episodes
    assert counts == {"Pacific/Auckland": 0, "America/Los_Angeles": 1}


def test_since_window_digest_has_its_own_note(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.episode("e1", [["alpha"]], hours=1)
    env.run(client_for(("T", "s", ["L1"])))
    window = since_window(7, now=WINDOW.start + timedelta(days=2), tz=UTC)
    report = env.run(client_for(("T", "s", ["L1"])), window=window)
    assert report.note_path == "Earmark/Digests/2026-11-12 to 2026-11-18.md"
    assert report.status == "created"
    assert env.note.exists()  # the weekly digest is untouched
    assert 'digest: "2026-11-12..2026-11-18"' in (env.vault / report.note_path).read_text()
