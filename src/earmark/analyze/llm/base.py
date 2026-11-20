"""The provider-neutral model client protocol. Standard library only."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

LLMStop = Literal["end", "max_tokens", "refusal", "context_exceeded", "other"]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    user: str
    schema: Mapping[str, Any]
    schema_name: str
    max_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str  # all text blocks, concatenated in order
    stop: LLMStop
    raw_stop: str | None = None  # the provider's stop reason, for messages
    refusal_category: str | None = None
    warnings: tuple[str, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMClient(Protocol):
    @property
    def model(self) -> str:
        """Model id used in prompt_hash (EARMARK_LLM_MODEL)."""
        ...

    @property
    def context_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int:
        """max_tokens of the first attempt."""
        ...

    @property
    def max_output_tokens(self) -> int:
        """max_tokens of the one retry after a max_tokens stop."""
        ...

    def count_tokens(self, request: LLMRequest, /) -> int: ...

    def complete(self, request: LLMRequest, /) -> LLMResponse:
        """One request. Raises TransientError / PermanentError for transport and API errors."""
        ...

    def close(self) -> None: ...
