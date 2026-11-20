"""The analyze stage: cache, fit check, one completion, validation, mapping."""

import dataclasses
import logging

from pydantic import ValidationError

from earmark.analyze.llm.base import LLMClient, LLMRequest
from earmark.analyze.prompts import SYSTEM_PROMPT, build_user_message
from earmark.analyze.schema import (
    ANALYSIS_OUTPUT_SCHEMA,
    SCHEMA_NAME,
    AnalysisOutput,
    build_analysis,
    effective_duration,
    plural,
)
from earmark.artifacts import ArtifactStore
from earmark.errors import NeedsChunking, PermanentError
from earmark.models import Analysis, Clock, Episode, Transcript, prompt_hash, utc_now

__all__ = ["LlmAnalyzer", "effective_duration"]

logger = logging.getLogger(__name__)


class LlmAnalyzer:
    """Satisfies the ``Analyzer`` protocol on top of any ``LLMClient``."""

    def __init__(
        self, artifacts: ArtifactStore, client: LLMClient, *, clock: Clock = utc_now
    ) -> None:
        self._artifacts = artifacts
        self._client = client
        self._clock = clock

    @property
    def client(self) -> LLMClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def analyze(self, episode: Episode, transcript: Transcript, prompt: str | None, /) -> Analysis:
        client = self._client
        if transcript.episode_key != episode.key:
            raise PermanentError("analyzer was given a transcript for another episode")
        digest = prompt_hash(prompt, client.model)
        cached = self._artifacts.read_analysis(episode.key, digest)
        if cached is not None:
            return cached
        if not any(seg.text.strip() for seg in transcript.segments):
            raise PermanentError(f"the transcript of {episode.key} has no text to analyze")

        request = LLMRequest(
            system=SYSTEM_PROMPT,
            user=build_user_message(episode, transcript, prompt),
            schema=ANALYSIS_OUTPUT_SCHEMA,
            schema_name=SCHEMA_NAME,
            max_tokens=client.output_tokens,
        )
        n = client.count_tokens(request)
        limit = max(client.context_tokens - request.max_tokens, 0)
        if n + request.max_tokens > client.context_tokens:
            raise NeedsChunking(model=client.model, input_tokens=n, limit_tokens=limit)

        response = client.complete(request)
        if response.stop == "max_tokens":
            cap = min(client.max_output_tokens, client.context_tokens - n)
            if cap > request.max_tokens:
                logger.warning(
                    "analysis of %s hit the %d-token output cap; retrying once with %d",
                    episode.key,
                    request.max_tokens,
                    cap,
                )
                request = dataclasses.replace(request, max_tokens=cap)
                response = client.complete(request)
            if response.stop == "max_tokens":
                raise PermanentError(
                    f"the analysis did not fit in {request.max_tokens} output tokens"
                )
        if response.stop == "refusal":
            raise PermanentError(
                f"{client.model} declined to analyze this episode (refusal category: "
                f"{response.refusal_category or 'unspecified'})"
            )
        if response.stop == "context_exceeded":
            raise NeedsChunking(model=client.model, input_tokens=n, limit_tokens=limit)
        if response.stop == "other":
            raise PermanentError(f"the model stopped unexpectedly ({response.raw_stop})")

        try:
            output = AnalysisOutput.model_validate_json(response.text)
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_url=False)
            count = len(errors)
            loc = ".".join(str(p) for p in errors[0]["loc"]) or "(root)" if errors else "(root)"
            raise PermanentError(
                "the model's answer does not match the analysis schema "
                f"({count} {plural(count, 'error')}, first at {loc})"
            ) from None

        duration = effective_duration(transcript)
        analysis = build_analysis(
            output,
            episode_key=episode.key,
            prompt=prompt,
            model=client.model,
            created_at=self._clock(),
            duration_s=duration if duration > 0 else 1.0,
            warnings=response.warnings,
        )
        self._artifacts.write_analysis(analysis)
        logger.info(
            "analysis %s for %s written (%s input, %s output tokens)",
            analysis.prompt_hash,
            episode.key,
            response.input_tokens,
            response.output_tokens,
        )
        return analysis
