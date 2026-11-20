"""``DATA_DIR/digests/``: the theme record of each digest input, and the answers of an
unfinished multi-request run. Derived data: safe to delete."""

import contextlib
import logging
import re
import shutil
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from earmark.artifacts import atomic_write_bytes
from earmark.digest.model import ThemeRecord
from earmark.digest.prompts import DIGEST_SCHEMA_VERSION

logger = logging.getLogger(__name__)

_ID: Final = re.compile(r"[0-9a-f]{16}")


def _check(value: str) -> None:
    if not _ID.fullmatch(value):
        raise ValueError("digest cache ids must be 16 lower-case hex characters")


class DigestCache:
    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir / "digests"

    @property
    def root(self) -> Path:
        return self._root

    def _record_path(self, input_hash: str) -> Path:
        _check(input_hash)
        return self._root / f"{input_hash}.json"

    def _calls_dir(self, input_hash: str) -> Path:
        _check(input_hash)
        return self._root / f"{input_hash}.calls"

    def _call_path(self, input_hash: str, call_id: str) -> Path:
        _check(call_id)
        return self._calls_dir(input_hash) / f"{call_id}.json"

    def read(self, input_hash: str) -> ThemeRecord | None:
        path = self._record_path(input_hash)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            record = ThemeRecord.model_validate_json(raw)
        except ValidationError:
            logger.warning("digest cache %s is invalid; it will be recomputed", path.name)
            return None
        if record.input_hash != input_hash or record.version != DIGEST_SCHEMA_VERSION:
            logger.warning("digest cache %s is for another input; it will be recomputed", path.name)
            return None
        return record

    def write(self, record: ThemeRecord) -> Path:
        path = self._record_path(record.input_hash)
        atomic_write_bytes(path, record.model_dump_json().encode())
        return path

    def read_call(self, input_hash: str, call_id: str) -> bytes | None:
        try:
            return self._call_path(input_hash, call_id).read_bytes()
        except FileNotFoundError:
            return None

    def write_call(self, input_hash: str, call_id: str, data: bytes) -> None:
        atomic_write_bytes(self._call_path(input_hash, call_id), data)

    def delete_call(self, input_hash: str, call_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._call_path(input_hash, call_id).unlink()

    def clear_calls(self, input_hash: str) -> None:
        shutil.rmtree(self._calls_dir(input_hash), ignore_errors=True)
