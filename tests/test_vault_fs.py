import errno
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from earmark.errors import PermanentError, TransientError
from earmark.vault.fs import (
    FileStamp,
    NoteChanged,
    SymlinkRefused,
    Vault,
    VaultDir,
    vault_lock,
)


def _open(tmp_path: Path, subdir: str = "Earmark") -> VaultDir:
    (tmp_path / "vault").mkdir(exist_ok=True)
    return Vault(tmp_path / "vault", subdir).open_subdir()


def _tmps(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(".earmark-")]


def test_open_subdir_creates_parts(tmp_path: Path) -> None:
    with _open(tmp_path, "A/B") as d:
        assert str(d.rel) == "."
    assert (tmp_path / "vault" / "A" / "B").is_dir()


def test_open_subdir_errors(tmp_path: Path) -> None:
    with pytest.raises(TransientError, match="EARMARK_VAULT_DIR"):
        Vault(tmp_path / "missing", "E").open_subdir()
    (tmp_path / "file").write_text("x")
    with pytest.raises(PermanentError, match="EARMARK_VAULT_DIR"):
        Vault(tmp_path / "file", "E").open_subdir()


def test_vault_dir_may_be_symlink_but_subdir_may_not(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    Vault(link, "E").open_subdir().close()
    assert (real / "E").is_dir()
    other = tmp_path / "other"
    other.mkdir()
    (real / "S").symlink_to(other)
    with pytest.raises(SymlinkRefused, match="S"):
        Vault(real, "S").open_subdir()
    (real / "F").write_text("x")
    with pytest.raises(SymlinkRefused):
        Vault(real, "F").open_subdir()


def test_child_refuses_symlink_and_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        (sub / "link").symlink_to(outside)
        (sub / "file").write_text("x")
        with pytest.raises(SymlinkRefused, match="link"):
            d.child("link", create=True)
        with pytest.raises(SymlinkRefused):
            d.child("link", create=False)
        with pytest.raises(SymlinkRefused):
            d.child("file", create=True)
        assert d.child("nothing", create=False) is None
        with pytest.raises(ValueError, match="plain"):
            d.child("a/b", create=True)
        with pytest.raises(ValueError, match="safe"):
            d.child("a#b", create=True)
    assert list(outside.iterdir()) == []


def test_child_creates_and_reuses_fold_equal(tmp_path: Path) -> None:
    with _open(tmp_path) as d:
        made = d.child("le podcast ☕", create=True)
        assert made is not None
        made.close()
        again = d.child("Le Podcast ☕", create=True)
        assert again is not None
        assert str(again.rel) == "le podcast ☕"
        again.close()
    assert [p.name for p in (tmp_path / "vault" / "Earmark").iterdir()] == ["le podcast ☕"]


def test_entries_kinds(tmp_path: Path) -> None:
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        (sub / "f").write_text("x")
        (sub / "d").mkdir()
        (sub / "s").symlink_to(sub / "f")
        os.mkfifo(sub / "p")
        kinds = {e.name: e.kind for e in d.entries()}
    assert kinds == {"f": "file", "d": "dir", "s": "symlink", "p": "other"}


def test_read_and_write_round_trip(tmp_path: Path) -> None:
    with _open(tmp_path) as d:
        assert d.read("n.md", max_bytes=100) is None
        d.write_atomic("n.md", b"hello", expect=None)
        got = d.read("n.md", max_bytes=100)
        assert got is not None
        data, stamp = got
        assert data == b"hello"
        assert stamp.size == 5
        d.write_atomic("n.md", b"hello2", expect=stamp)
        head = d.read_head("n.md", n=3)
        assert head is not None
        assert head[0] == b"hel"
        with pytest.raises(PermanentError, match="larger"):
            d.read("n.md", max_bytes=2)
    assert _tmps(tmp_path / "vault" / "Earmark") == []


def test_read_refuses_symlink_and_fifo(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        (sub / "l.md").symlink_to(outside)
        os.mkfifo(sub / "p.md")
        result: list[BaseException] = []

        def attempt() -> None:
            for name in ("l.md", "p.md"):
                try:
                    d.read(name, max_bytes=100)
                except SymlinkRefused as exc:
                    result.append(exc)

        t = threading.Thread(target=attempt, daemon=True)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
    assert len(result) == 2


def test_write_atomic_never_follows(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        (sub / "n.md").symlink_to(outside)
        with pytest.raises(SymlinkRefused):
            d.write_atomic("n.md", b"x", expect=None)
        assert not outside.exists()
        assert (sub / "n.md").is_symlink()
        assert _tmps(sub) == []


def test_write_atomic_does_not_write_through_hard_link(tmp_path: Path) -> None:
    other = tmp_path / "other.md"
    other.write_text("orig")
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        os.link(other, sub / "n.md")
        stamp = (d.read("n.md", max_bytes=100) or (b"", None))[1]
        assert stamp is not None
        d.write_atomic("n.md", b"new", expect=stamp)
    assert other.read_text() == "orig"
    assert (tmp_path / "vault" / "Earmark" / "n.md").read_text() == "new"


@pytest.mark.parametrize("target", ["fsync", "replace"])
def test_write_atomic_failure_leaves_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        d.write_atomic("n.md", b"old", expect=None)
        stamp = (d.read("n.md", max_bytes=10) or (b"", None))[1]

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EIO, "boom")

        monkeypatch.setattr(f"earmark.vault.fs.os.{target}", boom)
        with pytest.raises(OSError, match="boom"):
            d.write_atomic("n.md", b"new", expect=stamp)
        monkeypatch.undo()
        assert (sub / "n.md").read_bytes() == b"old"
        assert _tmps(sub) == []


def test_write_atomic_mode_0644(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        with _open(tmp_path) as d:
            d.write_atomic("n.md", b"x", expect=None)
    finally:
        os.umask(old)
    mode = stat.S_IMODE((tmp_path / "vault" / "Earmark" / "n.md").stat().st_mode)
    assert mode == 0o644


def test_write_atomic_expect_mismatch(tmp_path: Path) -> None:
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        d.write_atomic("n.md", b"old", expect=None)
        stamp = (d.read("n.md", max_bytes=10) or (b"", None))[1]
        assert stamp is not None
        (sub / "n.md").write_bytes(b"changed by the user")
        with pytest.raises(NoteChanged):
            d.write_atomic("n.md", b"new", expect=stamp)
        assert (sub / "n.md").read_bytes() == b"changed by the user"
        with pytest.raises(NoteChanged):
            d.write_atomic("n.md", b"new", expect=None)
        with pytest.raises(NoteChanged):
            d.write_atomic("gone.md", b"new", expect=FileStamp(1, 1, 1))
        assert _tmps(sub) == []


def test_remove_stale_temps(tmp_path: Path) -> None:
    with _open(tmp_path) as d:
        sub = tmp_path / "vault" / "Earmark"
        old = sub / ".earmark-0123456789abcdef.tmp"
        fresh = sub / ".earmark-fedcba9876543210.tmp"
        user = sub / ".x.tmp"
        note = sub / "n.md"
        for p in (old, fresh, user, note):
            p.write_text("x")
        now = time.time()
        os.utime(old, (now - 7200, now - 7200))
        os.utime(user, (now - 7200, now - 7200))
        os.utime(note, (now - 7200, now - 7200))
        assert d.remove_stale_temps(now=now) == 1
        assert not old.exists()
        assert fresh.exists()
        assert user.exists()
        assert note.exists()


def test_write_atomic_disk_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _open(tmp_path) as d:

        def full(*args: object) -> int:
            raise OSError(errno.ENOSPC, "full")

        monkeypatch.setattr("earmark.vault.fs.os.write", full)
        with pytest.raises(OSError, match="full"):
            d.write_atomic("n.md", b"x", expect=None)
        monkeypatch.undo()
        assert _tmps(tmp_path / "vault" / "Earmark") == []


def test_vault_lock(tmp_path: Path) -> None:
    lock = tmp_path / "data" / "vault.lock"
    with (
        vault_lock(lock, timeout_s=1),
        pytest.raises(TransientError, match="another earmark process"),
        vault_lock(lock, timeout_s=0.1),
    ):
        pytest.fail("the lock must not be granted twice")
    with vault_lock(lock, timeout_s=0.1):
        pass


def test_vault_lock_busy_message(tmp_path: Path) -> None:
    lock = tmp_path / "index.lock"
    with (
        vault_lock(lock, timeout_s=1, busy_message="the search index is busy"),
        pytest.raises(TransientError, match="the search index is busy"),
        vault_lock(lock, timeout_s=0.1, busy_message="the search index is busy"),
    ):
        pytest.fail("the lock must not be granted twice")


def test_open_existing_subdir_creates_nothing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    assert Vault(vault, "A/B").open_existing_subdir() is None  # no vault at all
    vault.mkdir()
    assert Vault(vault, "A/B").open_existing_subdir() is None
    assert list(vault.iterdir()) == []
    (vault / "A").mkdir()
    assert Vault(vault, "A/B").open_existing_subdir() is None
    assert list((vault / "A").iterdir()) == []
    (vault / "A" / "B").mkdir()
    opened = Vault(vault, "A/B").open_existing_subdir()
    assert opened is not None
    with opened:
        assert opened.entries() == []


def test_open_existing_subdir_refuses_symlinks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (vault / "A").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SymlinkRefused):
        Vault(vault, "A").open_existing_subdir()
