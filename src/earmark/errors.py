"""Job outcomes as exceptions. Each subclass states what the runner should do next.

Messages are shown to the user and stored in SQLite: keep them short and free of secrets
and untrusted bulk text. Chain the cause with ``raise ... from exc``.
"""

import math


class EarmarkError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TransientError(EarmarkError):
    """Worth retrying. ``retry_after_s`` is a server-provided delay, if any."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        if retry_after_s is not None and (not math.isfinite(retry_after_s) or retry_after_s < 0):
            raise ValueError("retry_after_s must be finite and >= 0")
        super().__init__(message)
        self.retry_after_s = retry_after_s


class PermanentError(EarmarkError):
    """Retrying cannot help; the job fails."""


class SkipEpisode(EarmarkError):  # noqa: N818  # name fixed by the plan; a job outcome, not an error
    """The episode cannot be processed (no public feed, subscriber-only); the job is skipped."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NeedsChunking(PermanentError):  # noqa: N818  # name fixed by the plan
    """The transcript does not fit one request; it must be chunked, never truncated."""

    def __init__(self, *, model: str, input_tokens: int, limit_tokens: int) -> None:
        super().__init__(
            f"the transcript is too long for one {model} request: {input_tokens} input "
            f"tokens, at most {limit_tokens} fit (EARMARK_LLM_CONTEXT_TOKENS); "
            "it was not truncated"
        )
        self.model = model
        self.input_tokens = input_tokens
        self.limit_tokens = limit_tokens
