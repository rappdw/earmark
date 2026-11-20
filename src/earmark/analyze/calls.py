"""One model call at a time: fit check, completion with the ``max_tokens`` retry, stop
handling, validation, and the single repair request. Shared by the one-shot path and
map-reduce. Standard library plus pydantic and ``earmark.analyze``."""

import dataclasses
import logging
from dataclasses import dataclass, field

from pydantic import ValidationError

from earmark.analyze.llm.base import LLMClient, LLMRequest, LLMResponse
from earmark.analyze.prompts import REPAIR_SYSTEM_PROMPT, build_repair_message
from earmark.analyze.schema import AnalysisOutput, plural
from earmark.errors import PermanentError
from earmark.models import scrub_surrogates

logger = logging.getLogger(__name__)

ANALYSIS: str = "the analysis"


class OutputCapReached(PermanentError):  # noqa: N818  # name fixed by the plan
    """A ``max_tokens`` stop with no larger cap left."""

    def __init__(self, what: str, cap: int) -> None:
        super().__init__(f"{what} did not fit in {cap} output tokens")


@dataclass
class CallStats:
    completions: int = 0
    repairs: int = 0
    warnings: list[str] = field(default_factory=list)  # client warnings, de-duplicated


def _problems(exc: ValidationError) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        found.append((loc, err["type"]))
    return found


class ModelCaller:
    def __init__(self, client: LLMClient, *, repair: bool, episode_key: str) -> None:
        self._client = client
        self._repair = repair
        self._key = episode_key
        self._stats = CallStats()

    @property
    def client(self) -> LLMClient:
        return self._client

    @property
    def stats(self) -> CallStats:
        return self._stats

    def count(self, request: LLMRequest) -> int:
        return self._client.count_tokens(request)

    def fits(self, request: LLMRequest, tokens: int, *, slack: int = 0) -> bool:
        return tokens + request.max_tokens + slack <= self._client.context_tokens

    def _send(self, request: LLMRequest) -> LLMResponse:
        response = self._client.complete(request)
        self._stats.completions += 1
        for warning in response.warnings:
            if warning not in self._stats.warnings:
                self._stats.warnings.append(warning)
        return response

    def complete(self, request: LLMRequest, tokens: int, *, what: str) -> LLMResponse:
        """One completion plus the ``max_tokens`` retry. The stop of the result is ``end`` or
        ``context_exceeded``; anything else raises ``PermanentError``."""
        client = self._client
        response = self._send(request)
        if response.stop == "max_tokens":
            cap = min(client.max_output_tokens, client.context_tokens - tokens)
            if cap > request.max_tokens:
                logger.warning(
                    "%s of %s hit the %d-token output cap; retrying once with %d",
                    what,
                    self._key,
                    request.max_tokens,
                    cap,
                )
                request = dataclasses.replace(request, max_tokens=cap)
                response = self._send(request)
            if response.stop == "max_tokens":
                raise OutputCapReached(what, request.max_tokens)
        if response.stop == "refusal":
            raise PermanentError(
                f"{client.model} declined to analyze this episode"
                f"{'' if what == ANALYSIS else f' ({what})'} (refusal category: "
                f"{response.refusal_category or 'unspecified'})"
            )
        if response.stop == "other":
            raise PermanentError(f"the model stopped unexpectedly ({response.raw_stop})")
        return response

    @staticmethod
    def _mismatch(what: str, problems: list[tuple[str, str]], middle: str = "") -> str:
        count = len(problems)
        subject = "the model's answer" + ("" if what == ANALYSIS else f" for {what}")
        return (
            f"{subject} does not match the analysis schema{middle} "
            f"({count} {plural(count, 'error')}, first at {problems[0][0]})"
        )

    def parse(self, response: LLMResponse, request: LLMRequest, *, what: str) -> AnalysisOutput:
        """Validate the answer. With repair on, an invalid answer gets exactly one repair
        request; otherwise (or if that fails too) the error is permanent. No message
        contains model text."""
        try:
            return AnalysisOutput.model_validate_json(scrub_surrogates(response.text))
        except ValidationError as exc:
            problems = _problems(exc) or [("(root)", "invalid")]
        if not self._repair:
            raise PermanentError(self._mismatch(what, problems)) from None

        lines = [f"{loc}: {kind}" for loc, kind in problems]
        repair_request = LLMRequest(
            system=REPAIR_SYSTEM_PROMPT,
            user=build_repair_message(response.text, lines),
            schema=request.schema,
            schema_name=request.schema_name,
            max_tokens=request.max_tokens,
        )
        n = self.count(repair_request)
        if not self.fits(repair_request, n):
            raise PermanentError(self._mismatch(what, problems) + " (too long to repair)")
        logger.warning(
            "the model's answer for %s of %s does not match the schema (%d problems); "
            "sending one repair request",
            what,
            self._key,
            len(problems),
        )
        repaired = self.complete(repair_request, n, what=f"the repair of {what}")
        if repaired.stop != "end":
            raise PermanentError(self._mismatch(what, problems) + " after one repair attempt")
        try:
            output = AnalysisOutput.model_validate_json(scrub_surrogates(repaired.text))
        except ValidationError as exc:
            again = _problems(exc) or [("(root)", "invalid")]
            raise PermanentError(self._mismatch(what, again, " after one repair attempt")) from None
        self._stats.repairs += 1
        return output
