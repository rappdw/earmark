"""The rules every summarizer shares: check_result, version parsing, message text."""

import pytest

from earmark.errors import PermanentError
from earmark.models import SummaryResult
from earmark.summarize import base
from earmark.summarize.base import (
    CONTEXT_COUNTED,
    CONTEXT_OVERRUN,
    CONTEXT_PREFIX,
    CONTEXT_REFUSED,
    CONTEXT_UNREAD,
    CUT_OFF,
    MIN_OLLAMA_VERSION,
    NO_TEXT,
    STOPPED,
    check_result,
    clean_reason,
    context_detail,
    parse_ollama_version,
)


def _result(
    text: str = "ok",
    finish_reason: str | None = "stop",
    completion_tokens: int | None = None,
) -> SummaryResult:
    return SummaryResult(
        text=text, model="m", finish_reason=finish_reason, completion_tokens=completion_tokens
    )


def test_check_result_accepts_a_finished_summary() -> None:
    check_result(_result(), max_output_tokens=8192)
    check_result(_result(text="  \n- a point\n"), max_output_tokens=8192)


def test_check_result_cut_off_names_the_setting() -> None:
    with pytest.raises(PermanentError) as info:
        check_result(
            _result(finish_reason="length", completion_tokens=8192), max_output_tokens=8192
        )
    assert info.value.message == CUT_OFF.format(completion="8192", limit=8192)
    assert info.value.message.startswith("the summary was cut off")
    assert "EARMARK_LLM_MAX_OUTPUT_TOKENS is 8192" in info.value.message


def test_check_result_cut_off_without_counts() -> None:
    with pytest.raises(PermanentError) as info:
        check_result(_result(finish_reason="length"), max_output_tokens=256)
    assert "after ? tokens" in info.value.message


@pytest.mark.parametrize(
    ("reason", "shown"),
    [
        ("abort", "abort"),
        ("error", "error"),
        ("repetition", "repetition"),
        (None, "unknown"),
        ("a" * 30, "a" * 20),
        ("a" * 20 + "!", "a" * 20),
        ("STOP", "unknown"),
        ("stop\nmore", "unknown"),
        ("", "unknown"),
    ],
)
def test_check_result_other_finish_reasons_are_stopped(reason: str | None, shown: str) -> None:
    with pytest.raises(PermanentError) as info:
        check_result(_result(finish_reason=reason), max_output_tokens=8192)
    assert info.value.message == STOPPED.format(reason=shown)


@pytest.mark.parametrize("text", ["", " ", "\n\t \r\n"])
def test_check_result_empty_text(text: str) -> None:
    with pytest.raises(PermanentError) as info:
        check_result(_result(text=text), max_output_tokens=8192)
    assert info.value.message == NO_TEXT.format(reason="stop")


def test_check_result_order_is_length_then_other_then_empty() -> None:
    with pytest.raises(PermanentError) as info:
        check_result(_result(text="", finish_reason="length"), max_output_tokens=8192)
    assert info.value.message.startswith("the summary was cut off")
    with pytest.raises(PermanentError) as info:
        check_result(_result(text="", finish_reason="abort"), max_output_tokens=8192)
    assert info.value.message.startswith("the model stopped without finishing")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.40.17", (0, 40, 17)),
        ("0.41.0-rc1", (0, 41, 0)),
        ("v1.2.3+build", (1, 2, 3)),
        ("ollama 0.12.0", (0, 12, 0)),
        ("0.40", None),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_ollama_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_ollama_version(text) == expected


def test_min_ollama_version_is_the_first_that_refuses() -> None:
    assert MIN_OLLAMA_VERSION == (0, 40, 17)
    assert (0, 40, 16) < MIN_OLLAMA_VERSION <= (0, 40, 17) < (0, 41, 0)


def test_context_messages_share_one_prefix() -> None:
    assert CONTEXT_PREFIX == "the transcript and instructions do not fit the model's context"
    counted = CONTEXT_COUNTED.format(
        prefix=CONTEXT_PREFIX, prompt=9000, output=8192, need=17192, context=16384
    )
    assert counted == (
        f"{CONTEXT_PREFIX}: 9000 prompt tokens and 8192 for the summary need 17192, but the "
        "context is 16384 tokens; nothing was summarised"
    )
    for text in (
        CONTEXT_REFUSED.format(prefix=CONTEXT_PREFIX, detail="x"),
        CONTEXT_UNREAD.format(prefix=CONTEXT_PREFIX, seen=1, expected=2),
        CONTEXT_OVERRUN.format(prefix=CONTEXT_PREFIX, prompt=1, completion=2, context=3),
    ):
        assert text.startswith(CONTEXT_PREFIX + ": ")


@pytest.mark.parametrize(
    ("prompt", "context", "expected"),
    [
        (70000, 65536, "70000 prompt tokens, 65536-token context"),
        (None, 65536, "65536-token context"),
        (70000, None, "no token counts given"),
        (None, None, "no token counts given"),
    ],
)
def test_context_detail(prompt: int | None, context: int | None, expected: str) -> None:
    assert context_detail(prompt, context) == expected


def test_clean_reason() -> None:
    assert clean_reason("tool_calls") == "tool_calls"
    assert clean_reason("x" * 21) == "x" * 20  # cut first, then checked
    assert clean_reason("<script>") == "unknown"


def test_base_imports_only_light_modules() -> None:
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import earmark.summarize.base\n"
        "heavy = {'httpx2', 'httpcore2', 'fastapi', 'earmark.fetch'}\n"
        "print(sorted(m for m in sys.modules if m in heavy))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
    assert base.NOT_CONFIGURED.endswith("(EARMARK_LLM_BACKEND is none)")
