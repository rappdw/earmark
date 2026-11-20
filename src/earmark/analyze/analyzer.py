"""The analyze stage: cache, strategy (one request or map-reduce), validation, mapping."""

import logging

from earmark.analyze.calls import ANALYSIS, ModelCaller
from earmark.analyze.llm.base import LLMClient, LLMRequest
from earmark.analyze.mapreduce import run_map_reduce
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
    """Satisfies the ``Analyzer`` protocol on top of any ``LLMClient``.

    A transcript that fits one request within the client's context window is analyzed in
    one request; a longer one goes through map-reduce. Nothing is ever truncated.
    """

    def __init__(
        self,
        artifacts: ArtifactStore,
        client: LLMClient,
        *,
        clock: Clock = utc_now,
        repair_invalid_json: bool = False,
    ) -> None:
        self._artifacts = artifacts
        self._client = client
        self._clock = clock
        self._repair = repair_invalid_json

    @property
    def client(self) -> LLMClient:
        return self._client

    @property
    def repairs_invalid_json(self) -> bool:
        return self._repair

    def close(self) -> None:
        self._client.close()

    def _one_shot(
        self, caller: ModelCaller, episode: Episode, transcript: Transcript, prompt: str | None
    ) -> AnalysisOutput:
        """The single request. Raises ``NeedsChunking`` when it does not fit."""
        client = self._client
        request = LLMRequest(
            system=SYSTEM_PROMPT,
            user=build_user_message(episode, transcript, prompt),
            schema=ANALYSIS_OUTPUT_SCHEMA,
            schema_name=SCHEMA_NAME,
            max_tokens=client.output_tokens,
        )
        n = caller.count(request)
        limit = max(client.context_tokens - request.max_tokens, 0)
        if not caller.fits(request, n):
            raise NeedsChunking(model=client.model, input_tokens=n, limit_tokens=limit)
        response = caller.complete(request, n, what=ANALYSIS)
        if response.stop == "context_exceeded":
            raise NeedsChunking(model=client.model, input_tokens=n, limit_tokens=limit)
        return caller.parse(response, request, what=ANALYSIS)

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

        caller = ModelCaller(client, repair=self._repair, episode_key=episode.key)
        extra: tuple[str, ...] = ()
        try:
            output = self._one_shot(caller, episode, transcript, prompt)
        except NeedsChunking as exc:
            if exc.input_tokens + client.output_tokens > client.context_tokens:
                logger.info(
                    "analysis of %s needs %d input tokens, more than one request allows "
                    "(%d); analyzing in parts",
                    episode.key,
                    exc.input_tokens,
                    exc.limit_tokens,
                )
            else:
                logger.warning(
                    "the model reported that the analysis of %s exceeds its context; "
                    "analyzing in parts",
                    episode.key,
                )
            result = run_map_reduce(
                caller,
                self._artifacts,
                episode=episode,
                transcript=transcript,
                prompt=prompt,
                prompt_hash=digest,
            )
            output, extra = result.output, result.warnings

        warnings = [*extra, *caller.stats.warnings]
        repairs = caller.stats.repairs
        if repairs:
            warnings.append(
                f"{repairs} model {plural(repairs, 'answer')} did not match the analysis "
                f"schema and {'was' if repairs == 1 else 'were'} repaired"
            )
        duration = effective_duration(transcript)
        analysis = build_analysis(
            output,
            episode_key=episode.key,
            prompt=prompt,
            model=client.model,
            created_at=self._clock(),
            duration_s=duration if duration > 0 else 1.0,
            warnings=warnings,
        )
        self._artifacts.write_analysis(analysis)
        self._artifacts.clear_analysis_parts(episode.key, digest)
        logger.info(
            "analysis %s for %s written (%d model %s)",
            analysis.prompt_hash,
            episode.key,
            caller.stats.completions,
            plural(caller.stats.completions, "request"),
        )
        return analysis
