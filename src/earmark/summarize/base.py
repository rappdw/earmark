"""The rules every summarizer shares: messages, the result check, version parsing.

Imports only the standard library, ``earmark.errors`` and ``earmark.models``, so the runner
and the factory can import it without httpx2. Every message is earmark's own: none carries
the transcript, the instructions, the model's output or a model server's error text.
"""

import re
from typing import Final

from earmark.errors import PermanentError
from earmark.models import SummaryResult

CONTEXT_PREFIX: Final = "the transcript and instructions do not fit the model's context"
CONTEXT_COUNTED: Final = (
    "{prefix}: {prompt} prompt tokens and {output} for the summary need {need}, but the "
    "context is {context} tokens; nothing was summarised"
)
CONTEXT_REFUSED: Final = (
    "{prefix}: the model server refused the prompt as longer than its context ({detail}); "
    "nothing was summarised"
)
CONTEXT_UNREAD: Final = (
    "{prefix}: the model server read {seen} of the prompt's {expected} tokens; nothing was kept"
)
CONTEXT_OVERRUN: Final = (
    "{prefix}: {prompt} prompt and {completion} output tokens passed the {context}-token "
    "context, so the server dropped part of it; nothing was kept"
)
CUT_OFF: Final = (
    "the summary was cut off: the model stopped at its output limit or the end of its "
    "context after {completion} tokens; nothing was kept "
    "(EARMARK_LLM_MAX_OUTPUT_TOKENS is {limit})"
)
NO_TEXT: Final = "the model returned no summary text (finish reason: {reason}); nothing was kept"
STOPPED: Final = "the model stopped without finishing (finish reason: {reason}); nothing was kept"
NOT_CONFIGURED: Final = "summaries are not configured on the worker (EARMARK_LLM_BACKEND is none)"
INPUT_MISSING: Final = "the summary's input is missing; send it again"
INPUT_CHANGED: Final = "the summary's input changed after it was received"
INPUT_UNREADABLE: Final = "the summary's input is unreadable; send it again"
# Until step 37b builds the clients, a real server type is a configuration error.
NOT_BUILT_YET: Final = (
    "backend {backend!r} is a model server client that step 37b adds; it is not built yet "
    "(use fake, or none to turn summaries off)"
)

MIN_OLLAMA_VERSION: Final = (0, 40, 17)
_VERSION: Final = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_REASON: Final = re.compile(r"[a-z_]{1,20}")


def clean_reason(reason: str | None) -> str:
    """A finish reason cut to 20 characters and made of ``[a-z_]``, else ``unknown``."""
    if reason is None:
        return "unknown"
    cut = reason[:20]
    return cut if _REASON.fullmatch(cut) is not None else "unknown"


def context_detail(prompt_tokens: int | None, context_tokens: int | None) -> str:
    """The ``{detail}`` of CONTEXT_REFUSED: the integers that were parsed, else a fallback."""
    if prompt_tokens is not None and context_tokens is not None:
        return f"{prompt_tokens} prompt tokens, {context_tokens}-token context"
    if context_tokens is not None:
        return f"{context_tokens}-token context"
    return "no token counts given"


def check_result(result: SummaryResult, *, max_output_tokens: int) -> None:
    """Raise PermanentError unless the model stopped on its own with some text."""
    if result.finish_reason == "length":
        completion = "?" if result.completion_tokens is None else str(result.completion_tokens)
        raise PermanentError(CUT_OFF.format(completion=completion, limit=max_output_tokens))
    if result.finish_reason != "stop":
        raise PermanentError(STOPPED.format(reason=clean_reason(result.finish_reason)))
    if not result.text.strip():
        raise PermanentError(NO_TEXT.format(reason=clean_reason(result.finish_reason)))


def parse_ollama_version(text: str) -> tuple[int, int, int] | None:
    """The first ``major.minor.patch`` of the string (a pre-release suffix is ignored)."""
    match = _VERSION.search(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)
