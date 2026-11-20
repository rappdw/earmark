"""``LlmThemer``: group research leads into themes on any ``LLMClient``.

One request when everything fits. Otherwise the leads are packed into batches, each batch
is themed, and the themes are merged by further requests, level by level, until one request
holds them. Nothing is truncated: what cannot fit fails permanently."""

import hashlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from earmark.analyze.calls import ModelCaller, OutputCapReached
from earmark.analyze.chunking import estimate_tokens
from earmark.analyze.llm.base import LLMClient, LLMRequest
from earmark.analyze.schema import plural
from earmark.digest.cache import DigestCache
from earmark.digest.model import ThemeEntry, ThemeRecord, ThemeRequest
from earmark.digest.prompts import (
    DIGEST_SCHEMA_VERSION,
    DIGEST_SYSTEM_PROMPT,
    THEMES_SCHEMA,
    THEMES_SCHEMA_NAME,
    build_group_message,
    lead_item,
    theme_item,
)
from earmark.errors import PermanentError

logger = logging.getLogger(__name__)

MIN_BATCH_TOKENS: Final = 1024
SLACK_TOKENS: Final = 16
UNTITLED: Final = "Untitled theme"


class WireTheme(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    summary: str
    items: list[str]


class ThemesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    themes: list[WireTheme]


@dataclass(frozen=True, slots=True)
class Placement:
    themes: tuple[tuple[str, str, tuple[str, ...]], ...]  # (title, summary, item ids)
    unplaced: tuple[str, ...]  # item ids in no theme, in item order
    unknown: int  # ids the model named that were not offered
    repeated: int  # ids placed in more than one theme (kept in the first)


def place(output: ThemesOutput, offered: Sequence[str]) -> Placement:
    order = {item_id: i for i, item_id in enumerate(offered)}
    placed: set[str] = set()
    unknown = repeated = 0
    themes: list[tuple[str, str, tuple[str, ...]]] = []
    for theme in output.themes:
        ids: list[str] = []
        for raw in theme.items:
            item_id = raw.strip()
            if item_id not in order:
                unknown += 1
            elif item_id in placed:
                repeated += 1
            else:
                placed.add(item_id)
                ids.append(item_id)
        if not ids:
            continue
        ids.sort(key=order.__getitem__)
        title = " ".join(theme.title.split()) or UNTITLED
        themes.append((title, " ".join(theme.summary.split()), tuple(ids)))
    unplaced = tuple(i for i in offered if i not in placed)
    return Placement(tuple(themes), unplaced, unknown, repeated)


@dataclass(frozen=True, slots=True)
class _Theme:
    title: str
    summary: str
    members: tuple[str, ...]  # lead ids


@dataclass(frozen=True, slots=True)
class _Item:
    item_id: str
    text: str
    members: tuple[str, ...]  # lead ids
    episode_key: str | None  # set for a lead
    theme: _Theme | None = None  # set for a theme item


@dataclass(slots=True)
class _Tally:
    unplaced: int = 0
    unknown: int = 0
    repeated: int = 0
    batches: int = 0

    def add(self, placement: Placement, *, leads_unplaced: int) -> None:
        self.unplaced += leads_unplaced
        self.unknown += placement.unknown
        self.repeated += placement.repeated


def _too_long_item(item: _Item, context: int) -> PermanentError:
    what = (
        f"a research lead of episode {item.episode_key}"
        if item.episode_key is not None
        else "a theme"
    )
    return PermanentError(
        f"{what} is too long for one request within EARMARK_LLM_CONTEXT_TOKENS ({context}); "
        "it was not truncated"
    )


class LlmThemer:
    """Satisfies ``Themer`` on any ``LLMClient``. Never truncates."""

    def __init__(self, client: LLMClient, cache: DigestCache, *, repair_invalid_json: bool) -> None:
        self._client = client
        self._cache = cache
        self._repair = repair_invalid_json

    @property
    def model(self) -> str:
        return self._client.model

    def close(self) -> None:
        self._client.close()

    # -- helpers --------------------------------------------------------------------------

    def _request(self, items: Sequence[_Item], *, merging: bool) -> LLMRequest:
        return LLMRequest(
            system=DIGEST_SYSTEM_PROMPT,
            user=build_group_message([i.text for i in items], merging=merging),
            schema=THEMES_SCHEMA,
            schema_name=THEMES_SCHEMA_NAME,
            max_tokens=self._client.output_tokens,
        )

    def _too_long(self, what: str) -> PermanentError:
        context = self._client.context_tokens
        return PermanentError(
            f"the model rejected {what} as too long although EARMARK_LLM_CONTEXT_TOKENS "
            f"({context}) says they fit; set it to the server's context window (see "
            "docs/llm.md); it was not truncated"
        )

    def _call_id(self, request: LLMRequest) -> str:
        raw = f"{self._client.model}\0{request.system}\0{request.user}\0{request.schema_name}"
        return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:16]

    def _read_cached(self, input_hash: str, call_id: str, what: str) -> ThemesOutput | None:
        data = self._cache.read_call(input_hash, call_id)
        if data is None:
            return None
        try:
            return ThemesOutput.model_validate_json(data)
        except ValidationError:
            logger.warning(
                "cached answer for %s of digest %s is invalid; recomputing", what, input_hash
            )
            self._cache.delete_call(input_hash, call_id)
            return None

    # -- the algorithm --------------------------------------------------------------------

    def themes(self, request: ThemeRequest, /) -> ThemeRecord:
        client = self._client
        context = client.context_tokens
        input_hash = request.input_hash
        caller = ModelCaller(
            client,
            repair=self._repair,
            episode_key=f"digest {input_hash}",
            subject="the research leads",
            schema_label="digest themes",
        )
        lead_index = {lead.lead_id: i for i, lead in enumerate(request.leads)}
        tally = _Tally()

        items: list[_Item] = [
            _Item(
                lead.lead_id,
                lead_item(lead, request.episodes[lead.episode_key]),
                (lead.lead_id,),
                lead.episode_key,
            )
            for lead in request.leads
        ]
        level = 1
        final: list[_Theme] = []

        def members_of(chosen: Sequence[_Item], ids: Sequence[str]) -> tuple[str, ...]:
            by_id = {i.item_id: i for i in chosen}
            found = {m for i in ids for m in by_id[i].members}
            return tuple(sorted(found, key=lead_index.__getitem__))

        def themes_of(chosen: Sequence[_Item], placement: Placement) -> list[_Theme]:
            return [
                _Theme(title, summary, members_of(chosen, ids))
                for title, summary, ids in placement.themes
            ]

        def carried(chosen: Sequence[_Item], placement: Placement) -> list[_Theme]:
            by_id = {i.item_id: i for i in chosen}
            return [t for u in placement.unplaced if (t := by_id[u].theme) is not None]

        def leads_unplaced(chosen: Sequence[_Item], placement: Placement) -> int:
            by_id = {i.item_id: i for i in chosen}
            return sum(1 for u in placement.unplaced if by_id[u].theme is None)

        while True:
            merging = level > 1
            what = "the combined themes" if merging else "the research leads"
            whole = self._request(items, merging=merging)
            counted = caller.count(whole)
            done = False
            if caller.fits(whole, counted):
                try:
                    response = caller.complete(whole, counted, what=what)
                except OutputCapReached:
                    if len(items) == 1:
                        raise
                else:
                    if response.stop == "context_exceeded":
                        if len(items) == 1:
                            raise self._too_long(what)
                    else:
                        output = caller.parse_as(ThemesOutput, response, whole, what=what)
                        placement = place(output, [i.item_id for i in items])
                        tally.add(placement, leads_unplaced=leads_unplaced(items, placement))
                        final = themes_of(items, placement) + carried(items, placement)
                        done = True
            if done:
                break

            pending = self._plan_batches(caller, items, merging=merging)
            if level == 1:
                tally.batches = len(pending)
            placed_themes: list[_Theme] = []
            carry: list[_Theme] = []
            i = 0
            while i < len(pending):
                batch, tokens = pending[i]
                batch_request = self._request(batch, merging=merging)
                call_id = self._call_id(batch_request)
                batch_what = f"batch {i + 1} of {len(pending)}"
                output_opt = self._read_cached(input_hash, call_id, batch_what)
                if output_opt is None:
                    try:
                        response = caller.complete(batch_request, tokens, what=batch_what)
                    except OutputCapReached:
                        if len(batch) == 1:
                            raise
                        half = len(batch) // 2
                        pending[i : i + 1] = [(batch[:half], tokens), (batch[half:], tokens)]
                        continue
                    if response.stop == "context_exceeded":
                        raise self._too_long(batch_what)
                    output_opt = caller.parse_as(
                        ThemesOutput, response, batch_request, what=batch_what
                    )
                    self._cache.write_call(
                        input_hash, call_id, output_opt.model_dump_json().encode()
                    )
                offered = [b.item_id for b in batch]
                placement = place(output_opt, offered)
                tally.add(placement, leads_unplaced=leads_unplaced(batch, placement))
                placed_themes += themes_of(batch, placement)
                carry += carried(batch, placement)
                i += 1

            next_themes = placed_themes + carry
            if len(next_themes) >= len(items):
                raise PermanentError(
                    "the model did not combine the research leads into fewer themes; they are "
                    f"too many to group within EARMARK_LLM_CONTEXT_TOKENS ({context})"
                )
            level += 1
            if not next_themes:
                final = []
                break
            items = [
                _Item(
                    f"T{n}",
                    theme_item(f"T{n}", t.title, t.summary, len(t.members)),
                    t.members,
                    None,
                    t,
                )
                for n, t in enumerate(next_themes, start=1)
            ]

        warnings: list[str] = []
        if tally.unplaced:
            warnings.append(
                f"the model placed {tally.unplaced} research "
                f"{plural(tally.unplaced, 'lead')} in no theme; they are listed under Other leads"
            )
        if tally.unknown:
            warnings.append(
                f"the model named {tally.unknown} {plural(tally.unknown, 'id')} that "
                "were not in the request; they were ignored"
            )
        if tally.repeated:
            warnings.append(
                f"{tally.repeated} research {plural(tally.repeated, 'lead')} were placed in "
                "more than one theme; each is kept in the first"
            )
        if tally.batches:
            warnings.append(
                f"the research leads were grouped in {tally.batches} batches and combined, "
                f"because they do not fit one {client.model} request "
                f"(EARMARK_LLM_CONTEXT_TOKENS={context})"
            )
        warnings.extend(caller.stats.warnings)
        if caller.stats.repairs:
            n = caller.stats.repairs
            warnings.append(
                f"{n} model {plural(n, 'answer')} did not match the digest themes schema "
                "and were repaired"
            )
        return ThemeRecord(
            version=DIGEST_SCHEMA_VERSION,
            input_hash=input_hash,
            model=client.model,
            themes=tuple(
                ThemeEntry(title=t.title, summary=t.summary, leads=t.members) for t in final
            ),
            requests=caller.stats.completions,
            warnings=tuple(warnings),
        )

    def _plan_batches(
        self, caller: ModelCaller, items: Sequence[_Item], *, merging: bool
    ) -> list[tuple[list[_Item], int]]:
        """Batches that each fit one request, with their counted tokens."""
        client = self._client
        context = client.context_tokens
        overhead = caller.count(self._request([], merging=merging))
        budget = context - client.output_tokens - overhead - SLACK_TOKENS
        if budget < MIN_BATCH_TOKENS:
            raise PermanentError(
                f"EARMARK_LLM_CONTEXT_TOKENS ({context}) is too small to group the research "
                f"leads: after the prompt and the {client.output_tokens}-token output cap, "
                f"{max(budget, 0)} tokens are left for each batch, and at least "
                f"{MIN_BATCH_TOKENS} are needed; nothing was truncated"
            )
        costs = [estimate_tokens(i.text + "\n") for i in items]
        total = sum(costs)
        k = max(1, math.ceil(total / budget))
        target = math.ceil(total / k)
        proposed: list[list[_Item]] = []
        current: list[_Item] = []
        used = 0
        for item, cost in zip(items, costs, strict=True):
            if current and (used + cost > budget or used >= target):
                proposed.append(current)
                current, used = [], 0
            current.append(item)
            used += cost
        if current:
            proposed.append(current)

        todo = proposed
        planned: list[tuple[list[_Item], int]] = []
        while todo:
            batch = todo.pop(0)
            request = self._request(batch, merging=merging)
            counted = caller.count(request)
            if caller.fits(request, counted, slack=SLACK_TOKENS):
                planned.append((batch, counted))
                continue
            if len(batch) == 1:
                raise _too_long_item(batch[0], context)
            half = len(batch) // 2
            todo[0:0] = [batch[:half], batch[half:]]
        return planned
