"""Map-reduce over a transcript that does not fit one request.

The transcript is cut into parts at segment boundaries (``chunking``); one map request per
part extracts a partial analysis; entities are merged in code; one reduce request combines
the part notes. Nothing is truncated: if a part cannot fit, the job fails instead.
"""

import hashlib
import logging
from dataclasses import dataclass, replace
from typing import Final

from pydantic import ValidationError

from earmark.analyze.calls import ModelCaller, OutputCapReached
from earmark.analyze.chunking import Chunk, plan_chunks, split_chunk
from earmark.analyze.llm.base import LLMRequest
from earmark.analyze.merge import (
    PartNote,
    finalize_entities,
    merged_part_entities,
    restrict_timestamps,
)
from earmark.analyze.prompts import (
    MAP_SYSTEM_PROMPT,
    REDUCE_SYSTEM_PROMPT,
    build_map_message,
    build_reduce_message,
)
from earmark.analyze.schema import (
    ANALYSIS_OUTPUT_SCHEMA,
    SCHEMA_NAME,
    AnalysisOutput,
    effective_duration,
    plural,
)
from earmark.artifacts import ArtifactStore
from earmark.errors import PermanentError
from earmark.models import Episode, Transcript, format_hms

logger = logging.getLogger(__name__)

MIN_PART_TOKENS: Final = 1024
LABEL_SLACK_TOKENS: Final = 16
_WIDEST_SECONDS: Final = 999 * 3600 + 59 * 60 + 59  # 999:59:59, the widest label in practice


@dataclass(frozen=True, slots=True)
class MapReduceResult:
    output: AnalysisOutput  # final, entities finalized
    parts: int
    warnings: tuple[str, ...]  # map-reduce notes + dropped-in-part timestamps + split segments


def _too_long(what: str, context: int) -> PermanentError:
    return PermanentError(
        f"the model rejected {what} as too long although EARMARK_LLM_CONTEXT_TOKENS "
        f"({context}) says it fits; set it to the server's context window (see "
        "docs/llm.md); it was not truncated"
    )


def _part_id(request: LLMRequest) -> str:
    raw = f"{request.system}\x00{request.user}\x00{request.schema_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def run_map_reduce(
    caller: ModelCaller,
    artifacts: ArtifactStore,
    *,
    episode: Episode,
    transcript: Transcript,
    prompt: str | None,
    prompt_hash: str,
) -> MapReduceResult:
    client = caller.client
    key = episode.key
    context = client.context_tokens
    duration = effective_duration(transcript)
    duration = duration if duration > 0 else 1.0
    segments = transcript.segments

    def map_request(chunk: Chunk, index: int, count: int) -> LLMRequest:
        return LLMRequest(
            system=MAP_SYSTEM_PROMPT,
            user=build_map_message(
                episode,
                duration_s=duration,
                chunk_text=chunk.to_text(),
                index=index,
                count=count,
                start_s=chunk.start_s,
                end_s=chunk.end_s,
                prompt=prompt,
            ),
            schema=ANALYSIS_OUTPUT_SCHEMA,
            schema_name=SCHEMA_NAME,
            max_tokens=client.output_tokens,
        )

    # -- plan ---------------------------------------------------------------------------
    probe = LLMRequest(
        system=MAP_SYSTEM_PROMPT,
        user=build_map_message(
            episode,
            duration_s=duration,
            chunk_text="",
            index=999,
            count=999,
            start_s=_WIDEST_SECONDS,
            end_s=_WIDEST_SECONDS,
            prompt=prompt,
        ),
        schema=ANALYSIS_OUTPUT_SCHEMA,
        schema_name=SCHEMA_NAME,
        max_tokens=client.output_tokens,
    )
    overhead = caller.count(probe)
    budget = context - client.output_tokens - overhead - LABEL_SLACK_TOKENS
    if budget < MIN_PART_TOKENS:
        raise PermanentError(
            f"EARMARK_LLM_CONTEXT_TOKENS ({context}) is too small to analyze this transcript "
            f"in parts: after the prompt and the {client.output_tokens}-token output cap, "
            f"{max(budget, 0)} tokens are left for each part, and at least {MIN_PART_TOKENS} "
            "are needed; it was not truncated"
        )
    plan = plan_chunks(segments, budget_tokens=budget)

    # -- verify: each proposed part must fit (exact for Anthropic) ------------------------
    pending = list(plan.chunks)
    chunks: list[Chunk] = []
    tokens: list[int] = []
    while pending:
        chunk = pending.pop(0)
        request = map_request(chunk, len(chunks) + 1, len(chunks) + len(pending) + 1)
        counted = caller.count(request)
        if caller.fits(request, counted, slack=LABEL_SLACK_TOKENS):
            chunks.append(chunk)
            tokens.append(counted)
            continue
        halves = split_chunk(chunk, segments)
        if halves is None:
            raise PermanentError(
                f"a part of the transcript at {format_hms(chunk.start_s)} does not fit one "
                f"request within EARMARK_LLM_CONTEXT_TOKENS ({context}); it was not truncated"
            )
        pending[0:0] = halves

    # -- map ----------------------------------------------------------------------------
    outputs: list[AnalysisOutput] = []
    dropped = 0
    i = 0
    while i < len(chunks):
        chunk, n = chunks[i], len(chunks)
        what = f"part {i + 1} of {n}"
        request = map_request(chunk, i + 1, n)
        part_id = _part_id(request)
        output: AnalysisOutput | None = None
        cached = artifacts.read_analysis_part(key, prompt_hash, part_id)
        if cached is not None:
            try:
                output = AnalysisOutput.model_validate_json(cached)
            except ValidationError:
                logger.warning("cached %s of %s is invalid; recomputing it", what, key)
                artifacts.delete_analysis_part(key, prompt_hash, part_id)
        if output is None:
            try:
                response = caller.complete(request, tokens[i], what=what)
            except OutputCapReached:
                halves = split_chunk(chunk, segments)
                if halves is None:
                    raise
                chunks[i : i + 1] = halves
                tokens[i : i + 1] = [tokens[i], tokens[i]]
                continue
            if response.stop == "context_exceeded":
                raise _too_long(what, context)
            output = caller.parse(response, request, what=what)
            artifacts.write_analysis_part(
                key, prompt_hash, part_id, output.model_dump_json().encode()
            )
        output, gone = restrict_timestamps(output, chunk.start_s, chunk.end_s)
        dropped += gone
        outputs.append(output)
        logger.info(
            "analysis of %s: part %d of %d done (%s to %s)",
            key,
            i + 1,
            n,
            format_hms(chunk.start_s),
            format_hms(chunk.end_s),
        )
        i += 1

    # -- reduce -------------------------------------------------------------------------
    count = len(chunks)
    original = [
        PartNote(i + 1, count, c.start_s, c.end_s, out)
        for i, (c, out) in enumerate(zip(chunks, outputs, strict=True))
    ]
    premerged = merged_part_entities(original)

    def reduce_request(notes: list[PartNote]) -> LLMRequest:
        return LLMRequest(
            system=REDUCE_SYSTEM_PROMPT,
            user=build_reduce_message(
                episode,
                duration_s=duration,
                notes=notes,
                entities=merged_part_entities(notes),
                prompt=prompt,
            ),
            schema=ANALYSIS_OUTPUT_SCHEMA,
            schema_name=SCHEMA_NAME,
            max_tokens=client.output_tokens,
        )

    def reduce_once(notes: list[PartNote]) -> AnalysisOutput:
        request = reduce_request(notes)
        counted = caller.count(request)
        response = caller.complete(request, counted, what="the combined notes")
        if response.stop == "context_exceeded":
            raise _too_long("the combined notes", context)
        return caller.parse(response, request, what="the combined notes")

    def fits(notes: list[PartNote]) -> bool:
        request = reduce_request(notes)
        return caller.fits(request, caller.count(request))

    level = original
    while not fits(level):
        groups: list[list[PartNote]] = []
        for note in level:
            if groups and fits([*groups[-1], note]):
                groups[-1].append(note)
            else:
                groups.append([note])
        if len(groups) >= len(level):
            raise PermanentError(
                f"the notes on {len(level)} parts are too long to combine within "
                f"EARMARK_LLM_CONTEXT_TOKENS ({context})"
            )
        merged: list[PartNote] = []
        for g, group in enumerate(groups, start=1):
            if len(group) == 1:
                merged.append(replace(group[0], index=g, count=len(groups)))
                continue
            merged.append(
                PartNote(
                    g,
                    len(groups),
                    group[0].start_s,
                    max(n.end_s for n in group),
                    reduce_once(group),
                )
            )
        level = merged

    final = finalize_entities(reduce_once(level), premerged)

    warnings = [
        f"the transcript was analyzed in {count} parts and combined, because it does not fit "
        f"one {client.model} request (EARMARK_LLM_CONTEXT_TOKENS={context})"
    ]
    if dropped:
        warnings.append(
            f"dropped {dropped} {plural(dropped, 'timestamp')} outside their transcript part"
        )
    if plan.split_segments:
        warnings.append(
            f"split {plan.split_segments} transcript {plural(plan.split_segments, 'segment')} "
            "too long for one request at sentence boundaries"
        )
    return MapReduceResult(output=final, parts=count, warnings=tuple(warnings))
