"""Opt-in: analyze on a real local LLM server. See docs/llm.md.

Set LLM_TEST_BASE_URL and LLM_TEST_MODEL (optionally LLM_TEST_CONTEXT_TOKENS, default
32768, LLM_TEST_API_KEY, and LLM_TEST_RESPONSE_FORMAT, default json_schema) and run
`uv run pytest -m network tests/test_openai_compat_live.py -v -rs`. The variables are not
EARMARK_-prefixed because conftest.py strips those.
"""

import math
import os
from pathlib import Path

import pytest

from earmark.analyze.analyzer import LlmAnalyzer
from earmark.analyze.llm.openai_compat import build_openai_compat_analyzer
from earmark.artifacts import ArtifactStore
from earmark.config import Settings
from earmark.models import Analysis
from tests.long_transcripts import long_transcript
from tests.test_analyze_live import LINES, _episode, _transcript

pytestmark = pytest.mark.network


def _analyzer(tmp_path: Path, *, context: int | None = None) -> LlmAnalyzer:
    base_url = os.environ.get("LLM_TEST_BASE_URL")
    if not base_url:
        pytest.skip("set LLM_TEST_BASE_URL (and LLM_TEST_MODEL) to run against a real server")
    model = os.environ.get("LLM_TEST_MODEL")
    if not model:
        pytest.fail("LLM_TEST_MODEL is required when LLM_TEST_BASE_URL is set")
    key = os.environ.get("LLM_TEST_API_KEY")
    settings = Settings.model_validate(
        {
            "llm_provider": "openai_compat",
            "llm_base_url": base_url,
            "llm_model": model,
            "llm_api_key": key,
            "llm_context_tokens": context
            or int(os.environ.get("LLM_TEST_CONTEXT_TOKENS", "32768")),
            "llm_response_format": os.environ.get("LLM_TEST_RESPONSE_FORMAT", "json_schema"),
        }
        | {"data_dir": str(tmp_path / "data")}
    )
    return build_openai_compat_analyzer(settings, ArtifactStore(settings.data_dir))


def _check(analysis: Analysis, duration_s: float) -> None:
    assert analysis.summary.strip()
    limit = math.ceil(duration_s)
    stamps = [c.timestamp_s for c in analysis.key_claims if c.timestamp_s is not None]
    for entity in (*analysis.people, *analysis.papers, *analysis.companies, *analysis.tools):
        stamps.extend(entity.timestamps_s)
    assert all(0 <= s <= limit for s in stamps)


def test_live_local_one_shot(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path)
    episode = _episode()
    transcript = _transcript(episode.key)
    assert len(LINES) == len(transcript.segments)
    try:
        analysis = analyzer.analyze(episode, transcript, None)
    finally:
        analyzer.close()
    _check(analysis, transcript.duration_s)


def test_live_local_map_reduce(tmp_path: Path) -> None:
    analyzer = _analyzer(tmp_path, context=8192)
    episode = _episode()
    transcript = long_transcript(hours=25 / 60).model_copy(update={"episode_key": episode.key})
    try:
        analysis = analyzer.analyze(episode, transcript, None)
    finally:
        analyzer.close()
    _check(analysis, transcript.duration_s)
    assert any("analyzed in" in w and "parts" in w for w in analysis.warnings)
