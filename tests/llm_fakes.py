"""A scripted LLMClient for analyzer tests."""

from collections.abc import Sequence

from earmark.analyze.llm.base import LLMRequest, LLMResponse
from tests.analysis_samples import valid_wire_json


class ScriptedLLMClient:
    def __init__(
        self,
        responses: Sequence[LLMResponse | Exception] = (),
        *,
        model: str = "claude-test",
        context_tokens: int = 1_000_000,
        output_tokens: int = 64_000,
        max_output_tokens: int = 128_000,
        token_count: int = 1000,
    ) -> None:
        self._script = list(responses)
        self._model = model
        self._context_tokens = context_tokens
        self._output_tokens = output_tokens
        self._max_output_tokens = max_output_tokens
        self.token_count = token_count
        self.count_requests: list[LLMRequest] = []
        self.complete_requests: list[LLMRequest] = []
        self.closed = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    @property
    def output_tokens(self) -> int:
        return self._output_tokens

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

    @property
    def count_calls(self) -> int:
        return len(self.count_requests)

    @property
    def complete_calls(self) -> int:
        return len(self.complete_requests)

    def count_tokens(self, request: LLMRequest, /) -> int:
        self.count_requests.append(request)
        return self.token_count

    def complete(self, request: LLMRequest, /) -> LLMResponse:
        self.complete_requests.append(request)
        if not self._script:
            return LLMResponse(text=valid_wire_json(), stop="end")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True
