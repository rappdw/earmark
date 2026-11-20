"""Builders for the digest tests: stored episodes with share times and analyses."""

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

from earmark.analyze.chunking import estimate_request_tokens
from earmark.analyze.llm.base import LLMRequest, LLMResponse
from earmark.artifacts import ArtifactStore
from earmark.models import Episode, ResearchLead, prompt_hash
from tests.factories import T0, make_analysis, make_episode, make_transcript
from tests.llm_fakes import ScriptedLLMClient

type Lead = ResearchLead | tuple[str, str, float | None] | str


def lead(item: Lead) -> ResearchLead:
    if isinstance(item, ResearchLead):
        return item
    if isinstance(item, str):
        return ResearchLead(text=item, rationale="because", timestamp_s=1.0)
    text, rationale, stamp = item
    return ResearchLead(text=text, rationale=rationale, timestamp_s=stamp)


def put_episode(
    artifacts: ArtifactStore,
    guid: str,
    shared_at: datetime,
    *,
    title: str | None = None,
    show: str = "Show A",
    analyses: Sequence[Sequence[Lead]] = (),
    transcript: bool = True,
) -> Episode:
    """Write the episode, its share time, a transcript, and one analysis per lead list."""
    episode = make_episode(guid=guid, title=title or f"Episode {guid}").model_copy(
        update={"show_title": show}
    )
    artifacts.write_episode(episode)
    artifacts.record_share(episode.key, shared_at)
    if transcript:
        artifacts.write_transcript(make_transcript(guid=guid))
    for i, leads in enumerate(analyses):
        prompt = None if i == 0 else f"prompt {i}"
        base = make_analysis(guid=guid, prompt=prompt, model="fake-llm")
        artifacts.write_analysis(
            base.model_copy(
                update={
                    "prompt_hash": prompt_hash(prompt, "fake-llm"),
                    "created_at": T0 + timedelta(minutes=i),
                    "research_leads": tuple(lead(x) for x in leads),
                }
            )
        )
    return episode


def themes_answer(*themes: tuple[str, str, list[str]]) -> LLMResponse:
    body = {"themes": [{"title": t, "summary": s, "items": ids} for t, s, ids in themes]}
    return LLMResponse(text=json.dumps(body), stop="end")


_WIKILINK = re.compile(r"(?<!\\)\[\[([^\]|]+)")
_MD_LINK = re.compile(r"(?<!\\)\]\(([^)\s]+)\)")


def assert_digest_links_resolve(vault: Path, note_text: str) -> int:
    """Every unescaped wikilink target and Markdown link destination is a file in the vault,
    and there is at least one. Returns how many were checked."""
    targets = [t + ".md" for t in _WIKILINK.findall(note_text)]
    targets += [unquote(t) for t in _MD_LINK.findall(note_text)]
    assert targets, "the digest has no links"
    for target in targets:
        assert (vault / target).is_file(), f"dangling link: {target}"
    return len(targets)


_IDS = re.compile(r'<item id="([^"]+)"')


def ids_of(request: LLMRequest) -> list[str]:
    return _IDS.findall(request.user)


def pairs(request: LLMRequest) -> LLMResponse:
    """Answer by putting the items two by two into themes."""
    ids = ids_of(request)
    themes = [(f"Theme {i}", "s", ids[i : i + 2]) for i in range(0, len(ids), 2)]
    return themes_answer(*themes)


def small_client(**kw: object) -> ScriptedLLMClient:
    params: dict[str, object] = {
        "context_tokens": 3500,
        "output_tokens": 500,
        "max_output_tokens": 500,
        "token_count": estimate_request_tokens,
        "respond": pairs,
    }
    params.update(kw)
    return ScriptedLLMClient(**params)  # type: ignore[arg-type]
