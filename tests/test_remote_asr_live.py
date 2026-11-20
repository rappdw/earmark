"""Opt-in: transcribe a real clip on a real ASR server. See docs/asr.md.

Set ASR_TEST_BASE_URL (and ASR_TEST_MODEL, ASR_TEST_API_KEY) and run
`uv run pytest -m network tests/test_remote_asr_live.py -v -rs`. The variables are not
EARMARK_-prefixed because conftest.py strips those.
"""

import os
import shutil
from pathlib import Path

import pytest
from pydantic import SecretStr

from earmark.artifacts import ArtifactStore
from earmark.fakes import FakeResolver
from earmark.transcribe.base import CachedTranscriber
from earmark.transcribe.remote import RemoteAsrEngine, RemoteOptions
from tests import slow_assets
from tests.pipeline_env import CANON

pytestmark = pytest.mark.network


def test_live_server_transcribes_clip(tmp_path: Path) -> None:
    base_url = os.environ.get("ASR_TEST_BASE_URL")
    if not base_url:
        pytest.skip(
            "set ASR_TEST_BASE_URL (and ASR_TEST_MODEL, ASR_TEST_API_KEY) "
            "to run against a real server"
        )
    api_key = os.environ.get("ASR_TEST_API_KEY")
    options = RemoteOptions(
        base_url=base_url.rstrip("/"),
        model=os.environ.get("ASR_TEST_MODEL", "whisper-1"),
        language=None,
        api_key=SecretStr(api_key) if api_key else None,
        max_upload_bytes=None,
        timeout_s=600,
    )
    episode = FakeResolver().resolve(CANON)
    artifacts = ArtifactStore(tmp_path / "data")
    audio = artifacts.audio_path(episode.key, "flac")
    audio.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(slow_assets.fetch_clip(), audio)
    engine = RemoteAsrEngine(options)
    try:
        transcript = CachedTranscriber(artifacts, engine, keep_audio=True).transcribe(
            episode, audio
        )
    finally:
        engine.close()
    assert "country" in " ".join(s.text for s in transcript.segments).lower()
    assert transcript.warnings == (), "the server returned no segment timestamps"
    assert any(s.end > 0 for s in transcript.segments)
    assert 10.0 <= transcript.duration_s <= 12.0
