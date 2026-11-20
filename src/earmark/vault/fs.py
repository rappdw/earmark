"""Symlink-refusing, directory-fd based file access below the vault subdir. POSIX only.

Everything below ``VAULT_DIR`` is reached through ``openat``-style calls on directory file
descriptors with ``O_NOFOLLOW``, so a symlink planted inside the vault cannot redirect a
write, and swapping a directory for a symlink after a check changes nothing. Existing
files are never opened for writing: a new temp file is written and renamed over them.
"""

import contextlib
import errno
import fcntl
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Self

from earmark.errors import PermanentError, TransientError
from earmark.vault.sanitize import fold_key, is_safe_component

logger = logging.getLogger(__name__)

type EntryKind = Literal["file", "dir", "symlink", "other"]

TEMP_NAME: Final = re.compile(r"\.earmark-[0-9a-f]{16}\.tmp")
MAX_NOTE_BYTES: Final = 16 * 1024 * 1024
_RETRIES: Final = 5


@dataclass(frozen=True, slots=True)
class Entry:
    name: str  # decoded with surrogateescape
    kind: EntryKind


@dataclass(frozen=True, slots=True)
class FileStamp:
    ino: int
    size: int
    mtime_ns: int


class SymlinkRefused(PermanentError):  # noqa: N818  # name fixed by the plan
    """A symlink or non-directory where a real folder or file is required."""


class NoteChanged(TransientError):  # noqa: N818  # name fixed by the plan
    """The file changed between our read and our write."""


def _refusal(rel: PurePosixPath | str) -> SymlinkRefused:
    return SymlinkRefused(
        f"refusing to write through a symlink or non-directory at {rel} in the vault "
        "subdir; replace it with a real folder"
    )


def _check_plain_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
        or len(name.encode("utf-8", "surrogateescape")) > 255
    ):
        raise ValueError(f"not a plain file name: {name!r}")


def _open_dir_at(fd: int, name: str, rel: PurePosixPath | str, *, create: bool) -> int | None:
    """Open ``name`` below ``fd`` as a real directory, creating it when asked."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    for _ in range(_RETRIES):
        try:
            return os.open(name, flags, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                return None
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o755, dir_fd=fd)
        except OSError as exc:
            if exc.errno in (errno.ENOTDIR, errno.ELOOP):
                raise _refusal(rel) from None
            raise
    raise TransientError(f"the vault changed while opening {rel}; try again")


def _kind(entry: os.DirEntry[str]) -> EntryKind:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


class VaultDir:
    """An open directory inside the vault subdir. Close it, or use it as a context manager."""

    def __init__(self, fd: int, rel: PurePosixPath, base: PurePosixPath | None = None) -> None:
        self._fd = fd
        self._base = (
            base or PurePosixPath()
        )  # the subdir's own path inside the vault, for messages only
        self.rel = rel  # relative to the subdir; PurePosixPath(".") for the subdir itself

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._fd >= 0:
            fd, self._fd = self._fd, -1
            os.close(fd)

    def _where(self, name: str) -> PurePosixPath:
        return self._base / self.rel / name

    def entries(self) -> list[Entry]:
        with os.scandir(self._fd) as it:
            return [Entry(e.name, _kind(e)) for e in it]

    def child(self, name: str, *, create: bool) -> "VaultDir | None":
        _check_plain_name(name)
        entries = self.entries()
        chosen = name if any(e.name == name for e in entries) else None
        if chosen is None:
            fold = fold_key(name)
            matches = [e for e in entries if fold_key(e.name) == fold]
            if len(matches) == 1 and matches[0].kind == "dir":
                chosen = matches[0].name
        if chosen is None:
            if not create:
                return None
            if not is_safe_component(name):
                raise ValueError(f"not a safe directory name: {name!r}")
            chosen = name
        fd = _open_dir_at(self._fd, chosen, self._where(chosen), create=create)
        if fd is None:
            return None
        return VaultDir(fd, self.rel / chosen, self._base)

    def _open_regular(self, name: str) -> tuple[int, os.stat_result] | None:
        _check_plain_name(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _refusal(self._where(name)) from None
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _refusal(self._where(name))
        except BaseException:
            os.close(fd)
            raise
        return fd, st

    def read(self, name: str, *, max_bytes: int) -> tuple[bytes, FileStamp] | None:
        opened = self._open_regular(name)
        if opened is None:
            return None
        fd, st = opened
        try:
            if st.st_size > max_bytes:
                raise PermanentError(
                    f"{self._where(name)} is larger than {max_bytes // (1024 * 1024)} MiB"
                )
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise PermanentError(
                        f"{self._where(name)} is larger than {max_bytes // (1024 * 1024)} MiB"
                    )
        finally:
            os.close(fd)
        return b"".join(chunks), FileStamp(st.st_ino, st.st_size, st.st_mtime_ns)

    def read_head(self, name: str, *, n: int) -> tuple[bytes, FileStamp] | None:
        """The first ``n`` bytes of a regular file (for the identity scan)."""
        opened = self._open_regular(name)
        if opened is None:
            return None
        fd, st = opened
        try:
            data = os.read(fd, n)
        finally:
            os.close(fd)
        return data, FileStamp(st.st_ino, st.st_size, st.st_mtime_ns)

    def write_atomic(self, name: str, data: bytes, *, expect: FileStamp | None) -> None:
        _check_plain_name(name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        tmp = ""
        fd = -1
        for _ in range(_RETRIES):
            tmp = f".earmark-{secrets.token_hex(8)}.tmp"
            try:
                fd = os.open(tmp, flags, 0o644, dir_fd=self._fd)
                break
            except FileExistsError:
                continue
        if fd < 0:
            raise TransientError(f"could not create a temp file in {self._where('')}; try again")
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
                os.fchmod(fd, 0o644)
            finally:
                os.close(fd)
            self._check_destination(name, expect)
            os.replace(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=self._fd)
            raise
        with contextlib.suppress(OSError):
            os.fsync(self._fd)

    def _check_destination(self, name: str, expect: FileStamp | None) -> None:
        try:
            st = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            if expect is None:
                return
            raise NoteChanged(
                f"{self._where(name)} disappeared while it was being updated"
            ) from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise _refusal(self._where(name))
        if expect is None or (st.st_ino, st.st_size, st.st_mtime_ns) != (
            expect.ino,
            expect.size,
            expect.mtime_ns,
        ):
            raise NoteChanged(f"{self._where(name)} changed while it was being updated")

    def remove_stale_temps(self, *, now: float, older_than_s: float = 3600.0) -> int:
        removed = 0
        try:
            with os.scandir(self._fd) as it:
                candidates = [e for e in it if TEMP_NAME.fullmatch(e.name)]
            for entry in candidates:
                try:
                    st = os.stat(entry.name, dir_fd=self._fd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode) and st.st_mtime < now - older_than_s:
                        os.unlink(entry.name, dir_fd=self._fd)
                        removed += 1
                except OSError as exc:
                    logger.debug("could not remove stale temp file in %s: %s", self._where(""), exc)
        except OSError as exc:
            logger.debug("could not list %s for stale temp files: %s", self._where(""), exc)
        return removed


class Vault:
    def __init__(self, vault_dir: Path, subdir: str) -> None:
        self._vault_dir = vault_dir
        self._parts = PurePosixPath(subdir).parts
        if not self._parts or any(p in {".", ".."} or "\x00" in p for p in self._parts):
            raise ValueError("subdir must be a non-empty relative path without '..'")

    @property
    def subdir_parts(self) -> tuple[str, ...]:
        return self._parts

    def open_subdir(self) -> VaultDir:
        """Open (creating missing parts of) the subdir. Only ``vault_dir`` follows symlinks."""
        try:
            fd = os.open(self._vault_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            raise TransientError(
                "EARMARK_VAULT_DIR does not exist (is the vault mounted?)"
            ) from None
        except NotADirectoryError:
            raise PermanentError("EARMARK_VAULT_DIR is not a directory") from None
        rel = PurePosixPath()
        try:
            for part in self._parts:
                rel = rel / part
                child = _open_dir_at(fd, part, rel, create=True)
                os.close(fd)
                fd = -1
                if child is None:  # pragma: no cover  # create=True never returns None
                    raise TransientError(f"could not open {rel}")
                fd = child
        except BaseException:
            if fd >= 0:
                os.close(fd)
            raise
        return VaultDir(fd, PurePosixPath(), PurePosixPath(*self._parts))

    def open_existing_subdir(self) -> VaultDir | None:
        """Like ``open_subdir``, but creates nothing; ``None`` if the vault or a part is absent."""
        try:
            fd = os.open(self._vault_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except (FileNotFoundError, NotADirectoryError):
            return None
        rel = PurePosixPath()
        try:
            for part in self._parts:
                rel = rel / part
                child = _open_dir_at(fd, part, rel, create=False)
                os.close(fd)
                fd = -1
                if child is None:
                    return None
                fd = child
        except BaseException:
            if fd >= 0:
                os.close(fd)
            raise
        return VaultDir(fd, PurePosixPath(), PurePosixPath(*self._parts))


@contextmanager
def vault_lock(
    path: Path,
    *,
    timeout_s: float,
    busy_message: str = "another earmark process is writing to the vault; try again later",
) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path``; wait up to ``timeout_s`` for it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TransientError(busy_message) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
