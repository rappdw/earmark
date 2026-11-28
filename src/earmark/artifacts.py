"""Atomic file writes and ArtifactStore, the owner of every path under the episodes directory."""

import contextlib
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Final

from pydantic import BaseModel, ValidationError

from earmark.errors import PermanentError
from earmark.models import (
    Episode,
    ShareRecord,
    Transcript,
    is_episode_key,
)

DECODED_DIR: Final = "transcripts"
_AUDIO_NAME = re.compile(r"audio\.[a-z0-9]{1,5}")


@contextmanager
def atomic_writer(
    path: Path, *, mode: int = 0o644, temp_suffix: str = ".tmp"
) -> Iterator[BinaryIO]:
    """Yield a binary file that replaces ``path`` atomically when the block exits normally.

    On any failure the temp file is removed and ``path`` keeps its old content (or stays
    absent).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=temp_suffix)
    tmp = Path(tmp_name)
    f = os.fdopen(fd, "wb")
    try:
        yield f
        f.flush()
        os.fsync(fd)
        os.fchmod(fd, mode)
        f.close()
        tmp.replace(path)
    except BaseException:
        f.close()
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    with atomic_writer(path, mode=mode) as f:
        f.write(data)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def _check_key(key: str) -> None:
    if not is_episode_key(key):
        raise ValueError(f"not an episode key: {key!r}")


def _corrupt(path: Path) -> PermanentError:
    return PermanentError(f"corrupt artifact: {path}")


class ArtifactStore:
    """Reads and writes ``<data_dir>/episodes/<key>/...``. Creates nothing until a write."""

    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir / "episodes"
        self._decoded_root = data_dir / DECODED_DIR

    @property
    def root(self) -> Path:
        return self._root

    def episode_dir(self, key: str) -> Path:
        _check_key(key)
        return self._root / key

    def episode_path(self, key: str) -> Path:
        return self.episode_dir(key) / "episode.json"

    def share_path(self, key: str) -> Path:
        return self.episode_dir(key) / "shared.json"

    def transcript_json_path(self, key: str) -> Path:
        return self.episode_dir(key) / "transcript.json"

    def transcript_text_path(self, key: str) -> Path:
        return self.episode_dir(key) / "transcript.txt"

    def audio_path(self, key: str, ext: str) -> Path:
        if not re.fullmatch("[a-z0-9]{1,5}", ext):
            raise ValueError(f"not an audio extension: {ext!r}")
        return self.episode_dir(key) / f"audio.{ext}"

    def _audio_files(self, key: str) -> list[Path]:
        directory = self.episode_dir(key)
        if not directory.is_dir():
            return []
        return sorted(
            p for p in directory.iterdir() if _AUDIO_NAME.fullmatch(p.name) and p.is_file()
        )

    def find_audio(self, key: str) -> Path | None:
        files = self._audio_files(key)
        return files[0] if files else None

    def delete_audio(self, key: str) -> bool:
        files = self._audio_files(key)
        for p in files:
            p.unlink(missing_ok=True)
        return bool(files)

    @staticmethod
    def _write_json(path: Path, model: BaseModel) -> Path:
        atomic_write_text(path, model.model_dump_json(indent=2) + "\n")
        return path

    @staticmethod
    def _read_json[M: BaseModel](path: Path, model_type: type[M]) -> M | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return model_type.model_validate_json(raw)
        except ValidationError as exc:
            raise _corrupt(path) from exc

    def write_episode(self, episode: Episode) -> Path:
        return self._write_json(self.episode_path(episode.key), episode)

    def has_episode(self, key: str) -> bool:
        return self.episode_path(key).is_file()

    def read_episode(self, key: str) -> Episode | None:
        path = self.episode_path(key)
        episode = self._read_json(path, Episode)
        if episode is not None and episode.key != key:
            raise _corrupt(path)
        return episode

    def read_share(self, key: str) -> ShareRecord | None:
        path = self.share_path(key)
        record = self._read_json(path, ShareRecord)
        if record is not None and record.episode_key != key:
            raise _corrupt(path)
        return record

    def record_share(self, key: str, shared_at: datetime) -> datetime:
        """Keep the earliest share time of the episode and return it."""
        if shared_at.tzinfo is None or shared_at.utcoffset() is None:
            raise ValueError("shared_at must be timezone-aware")
        existing = self.read_share(key)
        if existing is not None and existing.first_shared_at <= shared_at:
            return existing.first_shared_at
        self._write_json(
            self.share_path(key), ShareRecord(episode_key=key, first_shared_at=shared_at)
        )
        return shared_at

    def write_transcript(self, transcript: Transcript) -> Path:
        key = transcript.episode_key
        atomic_write_text(self.transcript_text_path(key), transcript.to_text())
        return self._write_json(self.transcript_json_path(key), transcript)

    def read_transcript(self, key: str) -> Transcript | None:
        path = self.transcript_json_path(key)
        transcript = self._read_json(path, Transcript)
        if transcript is not None and transcript.episode_key != key:
            raise _corrupt(path)
        return transcript

    def has_transcript(self, key: str) -> bool:
        return self.transcript_json_path(key).is_file()

    def list_episode_keys(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            p.name
            for p in self._root.iterdir()
            if is_episode_key(p.name) and (p / "episode.json").is_file()
        )

    # -- per-options transcripts (/v1) --------------------------------------------------

    def decoded_dir(self, dkey: str) -> Path:
        _check_key(dkey)
        return self._decoded_root / dkey

    def decoded_json_path(self, dkey: str) -> Path:
        return self.decoded_dir(dkey) / "transcript.json"

    def decoded_text_path(self, dkey: str) -> Path:
        return self.decoded_dir(dkey) / "transcript.txt"

    def read_decoded(self, dkey: str, episode_key: str) -> Transcript | None:
        path = self.decoded_json_path(dkey)
        transcript = self._read_json(path, Transcript)
        if transcript is not None and transcript.episode_key != episode_key:
            raise _corrupt(path)
        return transcript

    def write_decoded(self, dkey: str, transcript: Transcript) -> Path:
        atomic_write_text(self.decoded_text_path(dkey), transcript.to_text())
        return self._write_json(self.decoded_json_path(dkey), transcript)

    def has_decoded(self, dkey: str) -> bool:
        return self.decoded_json_path(dkey).is_file()

    def remove_decoded(self, dkey: str) -> int:
        """Delete the transcript files of ``dkey`` and its directory; return the file count.

        Symlinks are neither followed nor removed, and nothing else in the directory is
        touched (``rmdir`` then fails and is ignored).
        """
        directory = self.decoded_dir(dkey)
        if not directory.is_dir():
            return 0
        removed = 0
        for entry in sorted(directory.iterdir()):
            name = entry.name
            if name not in ("transcript.json", "transcript.txt") and not name.startswith(
                ".transcript."
            ):
                continue
            if entry.is_symlink() or not entry.is_file():
                continue
            entry.unlink()
            removed += 1
        with contextlib.suppress(OSError):
            directory.rmdir()
        return removed
