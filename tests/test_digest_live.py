"""Opt-in live test of the digest model. Needs ANTHROPIC_API_KEY or `ant auth login`.

Run with ``pytest -m llm tests/test_digest_live.py``. It fails (does not skip) without a
credential. Two requests: one token count and one completion.
"""

from pathlib import Path

import pytest

from earmark.config import ConfigError, Settings
from earmark.digest.cache import DigestCache
from earmark.digest.model import DigestLead, ThemeRequest
from earmark.digest.themes import LlmThemer
from earmark.pipeline.factory import build_themer
from tests.factories import make_episode

pytestmark = pytest.mark.llm

LEADS = (
    ("Replicate the tidal sediment study in colder seas.", "The effect may not hold there."),
    ("Read the SpectraLog sensor calibration paper.", "It explains hourly sediment logging."),
    ("Find out who funded the eleven-year coastal survey.", "Funding sources shape the data."),
    ("Compare storm survival of sediment signals across sites.", "Storms did not erase them."),
    ("Look up how sourdough starters keep their culture alive.", "Mentioned in the baking show."),
    ("Try a 78 percent hydration loaf with rye flour.", "The host's favourite recipe."),
    ("Check why autolyse improves dough extensibility.", "A claim made without evidence."),
    ("Learn how steam changes crust formation in a home oven.", "Suggested a Dutch oven."),
)


def test_real_model_groups_leads(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", llm_provider="anthropic", llm_effort="low"
    )
    try:
        themer = build_themer(settings, DigestCache(tmp_path / "data"))
    except ConfigError:
        pytest.fail("set ANTHROPIC_API_KEY (or log in with `ant auth login`) to run the llm tests")
    assert isinstance(themer, LlmThemer)
    sea = make_episode(guid="sea").model_copy(update={"show_title": "Deep Currents"})
    bread = make_episode(guid="bread").model_copy(update={"show_title": "Crumb"})
    leads = tuple(
        DigestLead(f"L{i}", (sea if i <= 4 else bread).key, text, why, None)
        for i, (text, why) in enumerate(LEADS, start=1)
    )
    try:
        record = themer.themes(
            ThemeRequest("0123456789abcdef", leads, {sea.key: sea, bread.key: bread})
        )
    finally:
        themer.close()
    assert record.themes
    placed = [i for t in record.themes for i in t.leads]
    assert len(placed) == len(set(placed))
    assert set(placed) <= {f"L{i}" for i in range(1, 9)}
    assert all(t.title and t.summary for t in record.themes)
