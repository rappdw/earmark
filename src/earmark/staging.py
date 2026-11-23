"""Where received and downloaded audio waits for its job: ``data/uploads`` and ``data/downloads``.

Standard library plus ``earmark.models``. Nothing here uses the system temp directory: an
upload is written straight to a ``.part`` file inside ``uploads/`` (mode 0600, created
exclusively), hashed as it streams, and renamed into place on commit. Names that reach a path are
generated here or match a strict pattern, never taken from a client.
"""

import contextlib
import logging
import os
import re
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Final, Self

from earmark.models import Job

logger = logging.getLogger(__name__)

UPLOADS_DIR: Final = "uploads"
DOWNLOADS_DIR: Final = "downloads"
_EXT: Final = re.compile(r"[a-z0-9]{1,5}")
_JOB_ID: Final = re.compile(r"[0-9a-f]{32}")
_UPLOAD_NAME: Final = re.compile(r"[0-9a-f]{32}\.[a-z0-9]{1,5}")
_DOWNLOAD_NAME: Final = re.compile(r"\.?(?P<id>[0-9a-f]{32})\.")
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@dataclass(frozen=True, slots=True)
class StagedUpload:
    name: str  # "<upload_id>.<ext>", matches UploadFileName
    path: Path
    size: int
    sha256: str  # 64 hex


class UploadWriter:
    """Appends to one ``.part`` file and hashes what it writes. Not thread-safe."""

    def __init__(self, directory: Path, name: str, file: BinaryIO, part: Path) -> None:
        self._directory = directory
        self._name = name
        self._file: BinaryIO | None = file
        self._part = part
        self._hash = sha256()
        self._size = 0
        self._committed = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def part_path(self) -> Path:
        return self._part

    def write(self, data: bytes, /) -> None:
        if self._file is None:
            raise ValueError("the upload writer is closed")
        self._file.write(data)
        self._hash.update(data)
        self._size += len(data)

    def commit(self) -> StagedUpload:
        if self._file is None:
            raise ValueError("the upload writer is closed")
        final = self._directory / self._name
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            os.fchmod(self._file.fileno(), _FILE_MODE)
            self._file.close()
            self._file = None
            self._part.replace(final)
        except BaseException:
            self.abort()
            raise
        self._committed = True
        _fsync_dir(self._directory)
        return StagedUpload(self._name, final, self._size, self._hash.hexdigest())

    def abort(self) -> None:
        """Close and unlink the partial file. Idempotent; a no-op after a commit."""
        if self._committed:
            return
        file, self._file = self._file, None
        if file is not None:
            with contextlib.suppress(OSError):
                file.close()
        with contextlib.suppress(FileNotFoundError):
            self._part.unlink()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.abort()


class Staging:
    def __init__(self, data_dir: Path) -> None:
        self._uploads = data_dir / UPLOADS_DIR
        self._downloads = data_dir / DOWNLOADS_DIR

    @property
    def uploads_dir(self) -> Path:
        return self._uploads

    @property
    def downloads_dir(self) -> Path:
        return self._downloads

    # -- uploads ----------------------------------------------------------------------

    def begin_upload(self, ext: str) -> UploadWriter:
        if _EXT.fullmatch(ext) is None:
            raise ValueError("not an audio extension")
        self._uploads.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{ext}"
        part = self._uploads / f".{name}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(part, flags, _FILE_MODE)
        return UploadWriter(self._uploads, name, os.fdopen(fd, "wb"), part)

    def upload_path(self, name: str) -> Path:
        if _UPLOAD_NAME.fullmatch(name) is None:
            raise ValueError("not a staged upload name")
        return self._uploads / name

    def release_staged(self, staged: StagedUpload) -> None:
        """Delete a staged upload nobody owns; never raises."""
        try:
            self.remove_upload(staged.name)
        except OSError as exc:
            logger.warning("could not delete a staged upload: %s", type(exc).__name__)

    def remove_upload(self, name: str) -> bool:
        path = self.upload_path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    # -- downloads --------------------------------------------------------------------

    def download_destination(self, job_id: str) -> Callable[[str], Path]:
        if _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("not a job id")

        def destination(ext: str) -> Path:
            if _EXT.fullmatch(ext) is None:
                raise ValueError("not an audio extension")
            self._downloads.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
            return self._downloads / f"{job_id}.{ext}"

        return destination

    def clear_download(self, job_id: str) -> int:
        """Unlink ``downloads/<job_id>.*`` and its partial files; return how many."""
        if _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("not a job id")
        if not self._downloads.is_dir():
            return 0
        removed = 0
        for pattern in (f"{job_id}.*", f".{job_id}.*"):
            for path in self._downloads.glob(pattern):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                    removed += 1
        return removed

    # -- lifecycle --------------------------------------------------------------------

    def release(self, job: Job) -> None:
        """Delete the staged audio of a job that reached a terminal state. Never raises."""
        try:
            if job.source == "upload" and job.upload_file is not None:
                self.remove_upload(job.upload_file)
            elif job.source == "url":
                self.clear_download(job.id)
        except (OSError, ValueError) as exc:
            logger.warning(
                "could not delete the staged audio of job %s: %s", job.id, type(exc).__name__
            )

    def sweep_uploads(self, keep: Collection[str]) -> int:
        """Unlink every file in ``uploads/`` not named in ``keep`` (partials and orphans)."""
        removed = self._sweep(self._uploads, lambda name: name in keep)
        if removed:
            logger.info("removed %d stale file(s) from the upload staging directory", removed)
        return removed

    def sweep_downloads(self, running: Collection[str]) -> int:
        """Unlink every file in ``downloads/`` unless it belongs to a running job."""

        def belongs_to_running(name: str) -> bool:
            match = _DOWNLOAD_NAME.match(name)
            return match is not None and match["id"] in running

        removed = self._sweep(self._downloads, belongs_to_running)
        if removed:
            logger.info("removed %d stale file(s) from the download staging directory", removed)
        return removed

    @staticmethod
    def _sweep(directory: Path, keep: Callable[[str], bool]) -> int:
        if not directory.is_dir():
            return 0
        removed = 0
        for entry in directory.iterdir():
            if entry.is_dir(follow_symlinks=False) or keep(entry.name):
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("could not remove a stale staged file: %s", type(exc).__name__)
                continue
            removed += 1
        return removed
