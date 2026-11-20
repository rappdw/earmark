"""The model and test clip the ``slow`` tests need, cached outside the repository."""

import hashlib
import os
from pathlib import Path
from typing import Final

import pytest

from earmark.fetch import MIB, GuardedFetcher
from tests.http_helpers import dns_intercepted

CLIP_URL: Final = (
    "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/"
    "65882eee9f5cdbeeb2d877f1131d48cf241b327d/tests/data/jfk.flac"
)
CLIP_SHA256: Final = "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715"
CLIP_SIZE: Final = 1_152_693
# JFK inaugural address, 20 January 1961: a US federal government work, public domain
# (17 U.S.C. 105). Copied in faster-whisper's tests at tag v1.2.1.
SLOW_MODEL: Final = "tiny"
CACHE_ENV: Final = "SLOW_TEST_CACHE"
DEFAULT_CACHE_DIR: Final = Path.home() / ".cache" / "earmark-tests"


def cache_dir() -> Path:
    override = os.environ.get(CACHE_ENV)
    return Path(override) if override else DEFAULT_CACHE_DIR


def model_dir() -> Path:
    return cache_dir() / "whisper"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_clip() -> Path:
    target = cache_dir() / "clips" / "jfk.flac"
    if target.is_file() and _sha256(target) == CLIP_SHA256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with GuardedFetcher(allow_private=dns_intercepted()) as fetcher:
        fetcher.download(CLIP_URL, lambda _ext: target, max_bytes=2 * MIB, purpose="test clip")
    actual = _sha256(target)
    if target.stat().st_size != CLIP_SIZE or actual != CLIP_SHA256:
        target.unlink(missing_ok=True)
        pytest.fail(f"test clip mismatch: expected sha256 {CLIP_SHA256}, got {actual}")
    return target
