import math

import pytest

from earmark.config import ConfigError
from earmark.errors import (
    EarmarkError,
    NeedsChunking,
    PermanentError,
    SkipEpisode,
    TransientError,
)

CLASSES = (TransientError, PermanentError, SkipEpisode)


def test_hierarchy() -> None:
    for cls in CLASSES:
        assert issubclass(cls, EarmarkError)
        for other in CLASSES:
            if other is not cls:
                assert not issubclass(cls, other)


def test_message_and_str() -> None:
    for exc in (EarmarkError("m"), TransientError("m"), PermanentError("m"), SkipEpisode("m")):
        assert exc.message == "m"
        assert str(exc) == "m"


def test_retry_after() -> None:
    assert TransientError("m").retry_after_s is None
    assert TransientError("m", retry_after_s=2.5).retry_after_s == 2.5
    assert TransientError("m", retry_after_s=0).retry_after_s == 0
    for bad in (-1.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="retry_after_s"):
            TransientError("m", retry_after_s=bad)


def test_skip_reason() -> None:
    exc = SkipEpisode("no public feed")
    assert exc.reason == exc.message == "no public feed"


def test_config_error_is_separate() -> None:
    assert not issubclass(ConfigError, EarmarkError)


def test_needs_chunking_is_permanent() -> None:
    exc = NeedsChunking(model="m", input_tokens=9000, limit_tokens=8000)
    assert isinstance(exc, PermanentError)
    assert not isinstance(exc, TransientError)
    assert (exc.model, exc.input_tokens, exc.limit_tokens) == ("m", 9000, 8000)
    assert "9000" in exc.message
    assert "8000" in exc.message
    assert "EARMARK_LLM_CONTEXT_TOKENS" in exc.message
    assert "not truncated" in exc.message
    assert str(exc) == exc.message
