"""Opt-in live test of the analysis model. Needs ANTHROPIC_API_KEY or `ant auth login`.

Run with ``pytest -m llm``. It fails (does not skip) without a credential.
"""

from pathlib import Path

import pytest

from earmark.analyze.analyzer import LlmAnalyzer
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, Settings
from earmark.models import Episode, Segment, Transcript, episode_key, prompt_hash
from earmark.pipeline.factory import build_analyzer

pytestmark = pytest.mark.llm

LINES = (
    "Welcome to Deep Currents. Today I'm talking with Dr. Mara Ellison.",
    "Mara, you led The Tidal Memory Study. What did you find?",
    "We found that coastal sediment records seasonal tides for decades.",
    "The Tidal Memory Study followed forty sites over eleven years.",
    "How did you collect the data?",
    "We used a logging tool called SpectraLog, built by Northwind Labs.",
    "SpectraLog records sediment layers every hour.",
    "And what surprised you most?",
    "That the signal survived storms; we expected them to erase it.",
    "Did Northwind Labs fund the work?",
    "Partly. The rest came from a public grant.",
    "The open question is whether the effect holds in colder seas.",
)


def _transcript(key: str) -> Transcript:
    segments = tuple(
        Segment(start=i * 10.0, end=i * 10.0 + 10.0, text=text) for i, text in enumerate(LINES)
    )
    return Transcript(
        episode_key=key, language="en", duration_s=120.0, model="synthetic", segments=segments
    )


def _episode() -> Episode:
    key = episode_key("apple_podcasts", "1", "live-test-guid")
    return Episode(
        key=key,
        source="apple_podcasts",
        show_id="1",
        episode_id="2",
        guid="live-test-guid",
        title="Tidal Memory",
        show_title="Deep Currents",
        published_at=None,
        duration_s=120.0,
        audio_url="https://example.com/a.mp3",
        feed_url=None,
        url="https://podcasts.apple.com/us/podcast/x/id1?i=2",
        description="A fictional interview.",
    )


def _build(tmp_path: Path) -> LlmAnalyzer:
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", llm_provider="anthropic", llm_effort="low"
    )
    try:
        analyzer = build_analyzer(settings)
    except ConfigError:
        pytest.fail("set ANTHROPIC_API_KEY (or log in with `ant auth login`) to run the llm tests")
    assert isinstance(analyzer, LlmAnalyzer)
    return analyzer


def test_live_default_analysis(tmp_path: Path) -> None:
    analyzer = _build(tmp_path)
    episode = _episode()
    transcript = _transcript(episode.key)
    try:
        analysis = analyzer.analyze(episode, transcript, None)
        assert analysis.summary
        assert analysis.people or analysis.papers or analysis.companies
        stamps = [c.timestamp_s for c in analysis.key_claims if c.timestamp_s is not None]
        for entity in (*analysis.people, *analysis.papers, *analysis.companies, *analysis.tools):
            stamps.extend(entity.timestamps_s)
        assert all(s <= transcript.duration_s for s in stamps)
        path = ArtifactStore(tmp_path / "data").analysis_path(
            episode.key, prompt_hash(None, analyzer.client.model)
        )
        assert path.is_file()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        assert analyzer.analyze(episode, transcript, None) == analysis
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    finally:
        analyzer.close()


def test_live_prompt_answered(tmp_path: Path) -> None:
    analyzer = _build(tmp_path)
    episode = _episode()
    try:
        analysis = analyzer.analyze(
            episode, _transcript(episode.key), "Which study is discussed, and who led it?"
        )
        assert analysis.prompt_response
    finally:
        analyzer.close()
