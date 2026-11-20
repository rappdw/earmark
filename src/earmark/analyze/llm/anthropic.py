"""The Claude client: the official ``anthropic`` SDK behind ``LLMClient``.

Import this module only as ``earmark.analyze.llm.anthropic`` (absolute), and only from the
factory's builder: it loads the SDK.
"""

import copy
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Self

import anthropic
import httpx2
from anthropic import Omit, omit
from anthropic.types.anthropic_beta_param import AnthropicBetaParam
from anthropic.types.beta import BetaFallbacksParam, BetaOutputConfigParam, BetaRefusalStopDetails
from pydantic import SecretStr

from earmark.analyze.analyzer import LlmAnalyzer
from earmark.analyze.llm.base import LLMRequest, LLMResponse, LLMStop
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, LlmEffort, Settings
from earmark.errors import EarmarkError, PermanentError, TransientError
from earmark.fetch import parse_retry_after
from earmark.log import redact
from earmark.models import Clock, utc_now

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_TOKENS: Final = 1_000_000
DEFAULT_OUTPUT_TOKENS: Final = 64_000
MAX_OUTPUT_TOKENS: Final = 128_000
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
MAX_DETAIL_CHARS: Final = 200
NO_CREDENTIALS: Final = (
    "EARMARK_LLM_API_KEY: no Anthropic credential found; set EARMARK_LLM_API_KEY "
    "(in the environment or .env), or ANTHROPIC_API_KEY in the process environment "
    "(an ANTHROPIC_API_KEY line in .env is not read), or log in with `ant auth login`"
)
_TRANSIENT_TYPES: Final = frozenset(
    {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}
)
_STOPS: Final[dict[str, LLMStop]] = {
    "end_turn": "end",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "model_context_window_exceeded": "context_exceeded",
}


@dataclass(frozen=True, slots=True)
class AnthropicOptions:
    model: str
    api_key: SecretStr | None
    effort: LlmEffort
    fallbacks: bool
    context_tokens: int
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    max_output_tokens: int = MAX_OUTPUT_TOKENS

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            effort=settings.llm_effort,
            fallbacks=settings.llm_fallbacks,
            context_tokens=settings.llm_context_tokens or DEFAULT_CONTEXT_TOKENS,
        )


def _detail(exc: anthropic.APIStatusError, secrets: Sequence[str]) -> str:
    text = ""
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                text = message
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    text = redact(text, secrets)[:MAX_DETAIL_CHARS]
    out = f": {text}" if text else ""
    if exc.request_id:
        out += f" [request {exc.request_id}]"
    return out


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    return parse_retry_after(exc.response.headers.get("retry-after"), utc_now())


def map_api_error(
    exc: anthropic.AnthropicError, *, model: str, secrets: Sequence[str]
) -> EarmarkError:
    """Map an SDK failure onto TransientError or PermanentError, most specific class first."""
    if isinstance(exc, anthropic.APITimeoutError):
        return TransientError("the Anthropic API timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return TransientError("could not reach the Anthropic API (connection error)")
    if isinstance(exc, anthropic.CredentialsError):
        return PermanentError(
            f"could not obtain Anthropic credentials ({type(exc).__name__}); "
            "check the profile or federation settings"
        )
    if not isinstance(exc, anthropic.APIStatusError):
        return TransientError(f"unexpected Anthropic SDK error ({type(exc).__name__})")
    d = _detail(exc, secrets)
    status = exc.status_code
    if isinstance(
        exc, anthropic.EnforcedSpendLimitReachedError | anthropic.ContractSpendLimitReachedError
    ):
        return PermanentError(
            f"the Anthropic API refused the request: spend limit reached (HTTP 429){d}"
        )
    if isinstance(exc, anthropic.RateLimitError):
        return TransientError(
            f"the Anthropic API is rate limiting requests (HTTP 429){d}",
            retry_after_s=_retry_after(exc),
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return PermanentError(
            f"the Anthropic API rejected the credential (HTTP 401; check EARMARK_LLM_API_KEY){d}"
        )
    if isinstance(exc, anthropic.PaymentRequiredError):
        return PermanentError(f"the Anthropic API refused the request: billing (HTTP 402){d}")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return PermanentError(f"the Anthropic API refused the request (HTTP 403){d}")
    if isinstance(exc, anthropic.NotFoundError):
        return PermanentError(
            f"model {model} was not found or is not available to this key "
            f"(HTTP 404; check EARMARK_LLM_MODEL){d}"
        )
    if isinstance(exc, anthropic.RequestTimeoutError | anthropic.ConflictError):
        return TransientError(
            f"the Anthropic API asked to retry (HTTP {status}){d}", retry_after_s=_retry_after(exc)
        )
    if isinstance(exc, anthropic.InternalServerError):
        return TransientError(
            f"the Anthropic API is unavailable (HTTP {status}){d}", retry_after_s=_retry_after(exc)
        )
    if isinstance(
        exc,
        anthropic.BadRequestError
        | anthropic.UnprocessableEntityError
        | anthropic.RequestTooLargeError
        | anthropic.ContentTooLargeError,
    ):
        return PermanentError(f"the Anthropic API rejected the request (HTTP {status}){d}")
    if exc.type in _TRANSIENT_TYPES or status >= 500:
        return TransientError(f"the Anthropic API failed mid-response ({exc.type}){d}")
    return PermanentError(f"the Anthropic API rejected the request (HTTP {status}, {exc.type}){d}")


class AnthropicClient:
    """Satisfies ``LLMClient`` with one streamed Messages request per ``complete``."""

    def __init__(
        self,
        options: AnthropicOptions,
        *,
        http_client: httpx2.Client | None = None,
        max_retries: int = 2,
    ) -> None:
        self._options = options
        key = options.api_key.get_secret_value() if options.api_key is not None else None
        self._secrets: list[str] = [key] if key else []
        try:
            sdk = anthropic.Anthropic(api_key=key, max_retries=max_retries, http_client=http_client)
        except anthropic.CredentialsError:
            # The SDK raises this at construction when ANTHROPIC_CONFIG_DIR/ANTHROPIC_PROFILE
            # point at a profile that does not exist; for us that is "no credential".
            raise ConfigError([NO_CREDENTIALS]) from None
        if sdk.api_key is None and sdk.auth_token is None and sdk.credentials is None:
            sdk.close()
            raise ConfigError([NO_CREDENTIALS])
        self._sdk = sdk

    def __repr__(self) -> str:
        return f"AnthropicClient(model={self._options.model!r})"

    @property
    def options(self) -> AnthropicOptions:
        return self._options

    @property
    def sdk(self) -> anthropic.Anthropic:
        return self._sdk

    @property
    def model(self) -> str:
        return self._options.model

    @property
    def context_tokens(self) -> int:
        return self._options.context_tokens

    @property
    def output_tokens(self) -> int:
        return self._options.output_tokens

    @property
    def max_output_tokens(self) -> int:
        return self._options.max_output_tokens

    def _output_config(self, request: LLMRequest) -> BetaOutputConfigParam:
        schema: dict[str, Any] = copy.deepcopy(dict(request.schema))
        return {
            "effort": self._options.effort,
            "format": {"type": "json_schema", "schema": schema},
        }

    def _mapped(self, exc: anthropic.AnthropicError) -> EarmarkError:
        return map_api_error(exc, model=self._options.model, secrets=self._secrets)

    def count_tokens(self, request: LLMRequest, /) -> int:
        try:
            counted = self._sdk.messages.count_tokens(
                model=self._options.model,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                thinking={"type": "adaptive"},
                output_config=self._output_config(request),
            )
        except anthropic.AnthropicError as exc:
            raise self._mapped(exc) from exc
        return counted.input_tokens

    def complete(self, request: LLMRequest, /) -> LLMResponse:
        betas: list[AnthropicBetaParam] | Omit = (
            [FALLBACK_BETA] if self._options.fallbacks else omit
        )
        fallbacks: BetaFallbacksParam | Omit = "default" if self._options.fallbacks else omit
        try:
            with self._sdk.beta.messages.stream(
                model=self._options.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                thinking={"type": "adaptive"},
                output_config=self._output_config(request),
                betas=betas,
                fallbacks=fallbacks,
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise self._mapped(exc) from exc

        text_parts: list[str] = []
        warnings: list[str] = []
        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "fallback":
                trigger = block.trigger.category or "unspecified"
                warnings.append(
                    f"{block.from_.model} declined (refusal category: {trigger}); "
                    f"{block.to.model} answered instead"
                )
        if not warnings:
            for entry in message.usage.iterations or []:
                if entry.type == "fallback_message":
                    warnings.append(
                        f"{entry.model} answered instead of {self._options.model} "
                        "(fallback routing)"
                    )
                    break
        details = message.stop_details
        category = details.category if isinstance(details, BetaRefusalStopDetails) else None
        raw_stop = message.stop_reason
        return LLMResponse(
            text="".join(text_parts),
            stop=_STOPS.get(raw_stop or "", "other"),
            raw_stop=raw_stop,
            refusal_category=category,
            warnings=tuple(warnings),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

    def close(self) -> None:
        self._sdk.close()


def build_anthropic_analyzer(
    settings: Settings, artifacts: ArtifactStore, *, clock: Clock = utc_now
) -> LlmAnalyzer:
    client = AnthropicClient(AnthropicOptions.from_settings(settings))
    return LlmAnalyzer(artifacts, client, clock=clock)
