"""The local LLM client: one non-streaming chat completion per request, sent to an
operator-run OpenAI-compatible server (vLLM, llama.cpp, Ollama, LM Studio).

Like the remote ASR client this uses earmark's own small httpx2 client: the URL comes only
from operator configuration, so there is no private-address guard, but redirects are never
followed, proxy variables are ignored, cookies are cleared per request, the response is
capped, messages name only the host, and the key travels only in a per-request header.

Import this module only as ``earmark.analyze.llm.openai_compat`` (absolute), and only from
the factory's builder: it loads httpx2 and pydantic.
"""

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Self

import httpx2
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from earmark.analyze.analyzer import LlmAnalyzer
from earmark.analyze.chunking import estimate_request_tokens
from earmark.analyze.llm.base import LLMRequest, LLMResponse, LLMStop
from earmark.analyze.prompts import schema_section
from earmark.artifacts import ArtifactStore
from earmark.config import ConfigError, LlmResponseFormat, Settings
from earmark.endpoint import error_detail, host_label
from earmark.errors import PermanentError, TransientError
from earmark.fetch import MIB, USER_AGENT, parse_retry_after
from earmark.models import Clock, utc_now

logger = logging.getLogger(__name__)

CHAT_PATH: Final = "/chat/completions"
CONNECT_TIMEOUT_S: Final = 10.0
MAX_RESPONSE_BYTES: Final = 16 * MIB
MAX_ERROR_BYTES: Final = 64 * 1024
MIN_CONTEXT_TOKENS: Final = 8192
DEFAULT_OUTPUT_TOKENS: Final = 16_384
MAX_OUTPUT_TOKENS: Final = 32_768
CONTEXT_MARKERS: Final = (
    "exceed_context_size",
    "context length",
    "context window",
    "context size",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)


@dataclass(frozen=True, slots=True)
class OpenAICompatOptions:
    base_url: str  # validated, no trailing slash
    model: str
    api_key: SecretStr | None
    context_tokens: int
    response_format: LlmResponseFormat
    timeout_s: float

    @property
    def endpoint(self) -> str:
        return self.base_url + CHAT_PATH

    @property
    def host(self) -> str:
        return host_label(self.base_url)

    @property
    def output_tokens(self) -> int:
        return min(DEFAULT_OUTPUT_TOKENS, self.context_tokens // 4)

    @property
    def max_output_tokens(self) -> int:
        return min(MAX_OUTPUT_TOKENS, self.context_tokens // 2)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Collect every configuration problem into one ``ConfigError`` (no values shown)."""
        problems: list[str] = []
        if settings.llm_base_url is None:
            problems.append(
                "EARMARK_LLM_BASE_URL: required when EARMARK_LLM_PROVIDER=openai_compat "
                "(the server's OpenAI-compatible base URL, such as http://spark:8000/v1)"
            )
        context = settings.llm_context_tokens
        if context is None:
            problems.append(
                "EARMARK_LLM_CONTEXT_TOKENS: required when EARMARK_LLM_PROVIDER=openai_compat "
                "(the context window the server runs the model with, such as 32768; "
                "see docs/llm.md)"
            )
        elif context < MIN_CONTEXT_TOKENS:
            problems.append(
                f"EARMARK_LLM_CONTEXT_TOKENS: must be at least {MIN_CONTEXT_TOKENS} for "
                "openai_compat (the prompt, schema, and output cap need that much)"
            )
        if settings.llm_model.lower().startswith("claude-"):
            problems.append(
                "EARMARK_LLM_MODEL: a Claude model id is not a local model; use "
                "EARMARK_LLM_PROVIDER=anthropic for Claude, or set the local server's model id"
            )
        if problems or settings.llm_base_url is None or context is None:
            raise ConfigError(problems)
        return cls(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            context_tokens=context,
            response_format=settings.llm_response_format,
            timeout_s=settings.llm_timeout_seconds,
        )


# -- response mapping ---------------------------------------------------------------------


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str | None = None
    refusal: str | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: _Message
    finish_reason: str | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class _Completion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    choices: list[_Choice]
    usage: _Usage | None = None


_FINISH: Final[dict[str, LLMStop]] = {
    "stop": "end",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def clean_content(text: str) -> str:
    """Strip a leading ``<think>`` block and one surrounding code fence; nothing else."""
    text = text.strip()
    if text.startswith("<think>") and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip().endswith("```"):
            text = "\n".join(lines[:-1]).strip()
        else:
            text = "\n".join(lines)
    return text


class OpenAICompatClient:
    """Satisfies ``LLMClient`` with one POST to ``{base_url}/chat/completions`` per request."""

    def __init__(
        self,
        options: OpenAICompatOptions,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._options = options
        self._transport = transport
        self._client: httpx2.Client | None = None

    def __repr__(self) -> str:
        return f"OpenAICompatClient(model={self._options.model!r}, host={self._options.host!r})"

    @property
    def options(self) -> OpenAICompatOptions:
        return self._options

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

    def count_tokens(self, request: LLMRequest, /) -> int:
        """A conservative local estimate: these servers have no common counting endpoint."""
        return estimate_request_tokens(request)

    def request_body(self, request: LLMRequest) -> dict[str, Any]:
        o = self._options
        user = request.user
        response_format: dict[str, Any]
        if o.response_format == "json_object":
            user = user + "\n" + schema_section(request.schema)
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": copy.deepcopy(dict(request.schema)),
                },
            }
        return {
            "model": o.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user},
            ],
            "max_tokens": request.max_tokens,
            "stream": False,
            "response_format": response_format,
        }

    def _get_client(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(
                transport=self._transport or httpx2.HTTPTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx2.Timeout(
                    self._options.timeout_s, connect=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S
                ),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _secrets(self) -> list[str]:
        key = self._options.api_key
        return [key.get_secret_value()] if key is not None else []

    def complete(self, request: LLMRequest, /) -> LLMResponse:
        o = self._options
        headers = {"Content-Type": "application/json"}
        if o.api_key is not None:
            headers["Authorization"] = f"Bearer {o.api_key.get_secret_value()}"
        content = json.dumps(self.request_body(request), ensure_ascii=False).encode()
        client = self._get_client()
        client.cookies.clear()
        try:
            with client.stream("POST", o.endpoint, content=content, headers=headers) as response:
                outcome = self._read(response)
        except httpx2.ReadTimeout as exc:
            raise TransientError(
                f"local LLM at {o.host} did not answer within {o.timeout_s:g} s "
                "(EARMARK_LLM_TIMEOUT_SECONDS)"
            ) from exc
        except httpx2.TimeoutException as exc:
            raise TransientError(f"local LLM request to {o.host} timed out") from exc
        except (httpx2.TransportError, httpx2.DecodingError) as exc:
            raise TransientError(
                f"local LLM request to {o.host} failed: {type(exc).__name__}"
            ) from exc
        except httpx2.InvalidURL as exc:
            raise PermanentError("invalid EARMARK_LLM_BASE_URL") from exc
        if isinstance(outcome, LLMResponse):
            return outcome
        return self._map_completion(outcome)

    # -- reading --------------------------------------------------------------------------

    def _read(self, response: httpx2.Response) -> bytes | LLMResponse:
        status = response.status_code
        if status == 200:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise PermanentError(
                        f"local LLM at {self._options.host} returned more than "
                        f"{MAX_RESPONSE_BYTES // MIB} MiB"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        return self._status_outcome(response)

    def _status_outcome(self, response: httpx2.Response) -> LLMResponse:
        status = response.status_code
        host = self._options.host
        if 300 <= status < 400:
            raise PermanentError(
                f"local LLM at {host} answered with a redirect (HTTP {status}); "
                "redirects are not followed, check EARMARK_LLM_BASE_URL"
            )
        if not 400 <= status < 600:
            raise PermanentError(f"local LLM at {host} returned unexpected HTTP {status}")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) >= MAX_ERROR_BYTES:
                break
        raw = bytes(body[:MAX_ERROR_BYTES])
        if status in (400, 413):
            lowered = raw.decode("utf-8", errors="replace").lower()
            if any(marker in lowered for marker in CONTEXT_MARKERS):
                return LLMResponse(text="", stop="context_exceeded", raw_stop=f"HTTP {status}")
        found = error_detail(raw, response.headers.get("Content-Type"), self._secrets())
        detail = "" if found is None else f": {found}"
        if status in (408, 429) or status >= 500:
            raise TransientError(
                f"local LLM at {host} failed: HTTP {status}{detail}",
                retry_after_s=parse_retry_after(response.headers.get("Retry-After"), utc_now()),
            )
        if status in (401, 403):
            raise PermanentError(
                f"local LLM at {host} refused the request: HTTP {status} "
                f"(check EARMARK_LLM_API_KEY){detail}"
            )
        if status == 404:
            raise PermanentError(
                f"local LLM at {host}: HTTP 404 (check EARMARK_LLM_BASE_URL, which should end "
                f"in /v1, and EARMARK_LLM_MODEL){detail}"
            )
        hint = ""
        if status in (400, 422) and self._options.response_format == "json_schema":
            hint = (
                " (if the server does not support response_format json_schema, try "
                "EARMARK_LLM_RESPONSE_FORMAT=json_object)"
            )
        raise PermanentError(
            f"local LLM at {host} rejected the request: HTTP {status}{detail}{hint}"
        )

    def _map_completion(self, body: bytes) -> LLMResponse:
        host = self._options.host
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise PermanentError(
                f"local LLM at {host} returned a response that is not JSON"
            ) from exc
        try:
            completion = _Completion.model_validate(payload)
        except ValidationError:
            completion = None
        if completion is None or not completion.choices:
            raise PermanentError(
                f"local LLM at {host} returned a response that is not a chat completion"
            )
        choice = completion.choices[0]
        text = clean_content(choice.message.content or "")
        finish = choice.finish_reason
        stop: LLMStop
        if choice.message.refusal is not None and choice.message.refusal.strip():
            stop = "refusal"
        elif finish is None:
            stop = "end" if (choice.message.content or "").strip() else "other"
        else:
            stop = _FINISH.get(finish, "other")
        usage = completion.usage
        return LLMResponse(
            text=text,
            stop=stop,
            raw_stop=finish,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )


def build_openai_compat_analyzer(
    settings: Settings,
    artifacts: ArtifactStore,
    *,
    clock: Clock = utc_now,
    transport: httpx2.BaseTransport | None = None,
) -> LlmAnalyzer:
    """Build the analyzer without making a request. Raises ``ConfigError`` for bad settings."""
    options = OpenAICompatOptions.from_settings(settings)
    logger.info(
        "local LLM at %s (model %r, context %d tokens, response_format %s, timeout %g s)",
        options.host,
        options.model,
        options.context_tokens,
        options.response_format,
        options.timeout_s,
    )
    return LlmAnalyzer(
        artifacts,
        OpenAICompatClient(options, transport=transport),
        clock=clock,
        repair_invalid_json=True,
    )
