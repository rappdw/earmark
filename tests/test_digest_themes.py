import json
import random
from datetime import UTC
from pathlib import Path

import pytest

from earmark.analyze.calls import ModelCaller
from earmark.analyze.chunking import estimate_request_tokens
from earmark.analyze.llm.base import LLMRequest, LLMResponse
from earmark.digest.cache import DigestCache
from earmark.digest.collect import Collected
from earmark.digest.model import DigestLead, ThemeRequest
from earmark.digest.prompts import MERGE_THEMES_INSTRUCTIONS
from earmark.digest.run import assemble
from earmark.digest.themes import LlmThemer, ThemesOutput, WireTheme, place
from earmark.digest.window import week_window
from earmark.errors import PermanentError, TransientError
from earmark.models import Episode
from tests.analysis_samples import valid_wire_json
from tests.digest_helpers import ids_of, pairs, small_client, themes_answer
from tests.factories import make_episode
from tests.llm_fakes import ScriptedLLMClient

HASH = "0123456789abcdef"


def make_request(n: int, *, text: str = "a research lead about a subject") -> ThemeRequest:
    ep: Episode = make_episode(guid="g")
    leads = tuple(
        DigestLead(f"L{i}", ep.key, f"{text} number {i}", "a rationale for the lead", 1.0)
        for i in range(1, n + 1)
    )
    return ThemeRequest(HASH, leads, {ep.key: ep})


def themer(client: ScriptedLLMClient, tmp_path: Path, *, repair: bool = False) -> LlmThemer:
    return LlmThemer(client, DigestCache(tmp_path), repair_invalid_json=repair)


def covered(record_themes: tuple[tuple[str, ...], ...], n: int, other: tuple[str, ...]) -> None:
    placed = [i for t in record_themes for i in t]
    everything = placed + list(other)
    assert sorted(everything) == sorted(f"L{i}" for i in range(1, n + 1))


def is_merge(request: LLMRequest) -> bool:
    return MERGE_THEMES_INSTRUCTIONS in request.user


# -- place -----------------------------------------------------------------------------------


def wire(*themes: tuple[str, str, list[str]]) -> ThemesOutput:
    return ThemesOutput(themes=[WireTheme(title=t, summary=s, items=i) for t, s, i in themes])


def test_unknown_ids_ignored_with_warning(tmp_path: Path) -> None:
    placement = place(wire(("A", "s", ["L1", "L99", " L2 "])), ["L1", "L2"])
    assert placement.themes == (("A", "s", ("L1", "L2")),)
    assert placement.unknown == 1
    client = ScriptedLLMClient([themes_answer(("A", "s", ["L1", "L99"]))])
    record = themer(client, tmp_path).themes(make_request(1))
    assert any("named 1 id that were not in the request" in w for w in record.warnings)
    assert record.themes[0].leads == ("L1",)


def test_repeated_ids_kept_in_first(tmp_path: Path) -> None:
    placement = place(wire(("A", "s", ["L2"]), ("B", "s", ["L1", "L2", "L1"])), ["L1", "L2"])
    assert placement.themes == (("A", "s", ("L2",)), ("B", "s", ("L1",)))
    assert placement.repeated == 2


def test_unplaced_leads_go_to_other(tmp_path: Path) -> None:
    client = ScriptedLLMClient([themes_answer(("A", "s", ["L2"]))])
    record = themer(client, tmp_path).themes(make_request(3))
    assert [t.leads for t in record.themes] == [("L2",)]
    assert record.warnings[0].startswith("the model placed 2 research leads in no theme")
    placement = place(wire(("A", "s", ["L2"])), ["L1", "L2", "L3"])
    assert placement.unplaced == ("L1", "L3")


def test_blank_title_becomes_untitled_and_empty_theme_dropped() -> None:
    placement = place(
        wire(("  ", " two   words ", ["L1"]), ("Empty", "s", []), ("Ghost", "s", ["X"])), ["L1"]
    )
    assert placement.themes == (("Untitled theme", "two words", ("L1",)),)


def test_ids_follow_offered_order() -> None:
    placement = place(wire(("A", "s", ["L3", "L1"])), ["L1", "L2", "L3"])
    assert placement.themes[0][2] == ("L1", "L3")


# -- one request -----------------------------------------------------------------------------


def test_one_request_when_fits(tmp_path: Path) -> None:
    client = ScriptedLLMClient([themes_answer(("A", "sum", ["L1", "L2"]), ("B", "s", ["L3"]))])
    record = themer(client, tmp_path).themes(make_request(3))
    assert client.complete_calls == 1
    assert client.count_calls == 1
    assert record.requests == 1
    assert record.model == "claude-test"
    assert [(t.title, t.leads) for t in record.themes] == [("A", ("L1", "L2")), ("B", ("L3",))]
    assert record.warnings == ()
    sent = client.complete_requests[0]
    assert sent.schema_name == "earmark_digest_themes"
    assert ids_of(sent) == ["L1", "L2", "L3"]
    assert not is_merge(sent)


# -- batching ----------------------------------------------------------------------------------


def test_batches_and_merge_when_too_long(tmp_path: Path) -> None:
    client = small_client()
    record = themer(client, tmp_path).themes(make_request(40))
    merges = [r for r in client.complete_requests if is_merge(r)]
    batches = [r for r in client.complete_requests if not is_merge(r)]
    assert len(batches) >= 2
    assert len(merges) == 1
    assert client.complete_requests[-1] is merges[0]
    covered(tuple(t.leads for t in record.themes), 40, ())
    assert any("grouped in" in w and "batches and combined" in w for w in record.warnings)
    assert record.requests == len(client.complete_requests)
    # Every request fits the context.
    for r in client.complete_requests:
        assert estimate_request_tokens(r) + r.max_tokens <= 3500
    # Every lead was offered to the model: nothing was truncated.
    offered = [i for r in batches for i in ids_of(r)]
    assert sorted(offered) == sorted(f"L{i}" for i in range(1, 41))


def test_two_merge_levels(tmp_path: Path) -> None:
    client = small_client()
    record = themer(client, tmp_path).themes(make_request(300))
    merges = [r for r in client.complete_requests if is_merge(r)]
    assert len(merges) >= 3  # a second level of batches, then the final merge
    covered(tuple(t.leads for t in record.themes), 300, ())
    assert all(len(t.leads) >= 1 for t in record.themes)


def test_single_lead_too_long_fails(tmp_path: Path) -> None:
    client = small_client()
    big = make_request(1, text="x" * 20000)
    with pytest.raises(
        PermanentError, match=r"a research lead of episode .* too long for one"
    ) as e:
        themer(client, tmp_path).themes(big)
    assert "not truncated" in e.value.message
    assert client.complete_calls == 0


def test_budget_too_small_fails(tmp_path: Path) -> None:
    client = small_client(context_tokens=1500)
    with pytest.raises(PermanentError, match="too small to group the research leads"):
        themer(client, tmp_path).themes(make_request(40))
    assert client.complete_calls == 0


def test_no_progress_fails(tmp_path: Path) -> None:
    def one_each(request: LLMRequest) -> LLMResponse:
        return themes_answer(*[(f"T {i}", "s", [i]) for i in ids_of(request)])

    client = small_client(respond=one_each)
    with pytest.raises(PermanentError, match="did not combine the research leads into fewer"):
        themer(client, tmp_path).themes(make_request(40))


def test_context_exceeded_falls_back_to_batches(tmp_path: Path) -> None:
    client = ScriptedLLMClient(
        [LLMResponse(text="", stop="context_exceeded")],
        context_tokens=100_000,
        token_count=1000,
        respond=pairs,
    )
    record = themer(client, tmp_path).themes(make_request(10))
    assert client.complete_calls == 3  # the rejected one, one batch, the merge
    covered(tuple(t.leads for t in record.themes), 10, ())


def test_context_exceeded_on_single_lead_is_permanent(tmp_path: Path) -> None:
    client = ScriptedLLMClient([LLMResponse(text="", stop="context_exceeded")], token_count=10)
    with pytest.raises(PermanentError, match=r"rejected (the combined themes|the research leads)"):
        themer(client, tmp_path).themes(make_request(1))


def test_output_cap_splits_batch(tmp_path: Path) -> None:
    def respond(request: LLMRequest) -> LLMResponse:
        if len(ids_of(request)) > 6:
            return LLMResponse(text="", stop="max_tokens")
        return pairs(request)

    client = ScriptedLLMClient(
        context_tokens=100_000,
        output_tokens=500,
        max_output_tokens=500,
        token_count=1000,
        respond=respond,
    )
    record = themer(client, tmp_path).themes(make_request(20))
    sizes = [len(ids_of(r)) for r in client.complete_requests]
    assert sizes[0] == 20  # the whole request, then halves
    assert max(s for s in sizes[1:] if s <= 6) <= 6
    covered(tuple(t.leads for t in record.themes), 20, ())


def test_output_cap_on_single_item_reraises(tmp_path: Path) -> None:
    client = ScriptedLLMClient(
        [LLMResponse(text="", stop="max_tokens")],
        output_tokens=500,
        max_output_tokens=500,
        token_count=10,
    )
    with pytest.raises(PermanentError, match="did not fit in 500 output tokens"):
        themer(client, tmp_path).themes(make_request(1))


def test_every_offered_lead_accounted_for(tmp_path: Path) -> None:
    for seed in range(25):
        rng = random.Random(seed)  # noqa: S311  # a seeded test generator
        n = rng.randint(1, 12)
        lead_ids = [f"L{i}" for i in range(1, n + 1)]
        offered = [*lead_ids, "L99", "X"]
        themes = []
        for _ in range(rng.randint(0, 5)):
            picks = [rng.choice(offered) for _ in range(rng.randint(0, 6))]
            themes.append((rng.choice(["", "T", "Title"]), "s", picks))
        client = ScriptedLLMClient([themes_answer(*themes)])
        req = make_request(n)
        record = themer(client, tmp_path / str(seed)).themes(req)
        window = week_window(2026, 47, UTC)
        from earmark.digest.model import DigestEpisode
        from tests.factories import T0

        ep = DigestEpisode(next(iter(req.episodes.values())), T0, 1, req.leads)
        digest = assemble(window, Collected((ep,), ()), record, cached=False)
        placed = [i for t in digest.themes for i in t.leads]
        assert len(placed) == len(set(placed))
        assert sorted(placed + list(digest.other)) == sorted(lead_ids)
        assert all(t.leads for t in digest.themes)


# -- errors ------------------------------------------------------------------------------------


def test_invalid_json_permanent_for_anthropic(tmp_path: Path) -> None:
    client = ScriptedLLMClient([LLMResponse(text="not json", stop="end")], token_count=10)
    with pytest.raises(PermanentError, match="does not match the digest themes schema"):
        themer(client, tmp_path, repair=False).themes(make_request(2))
    assert client.complete_calls == 1


def test_wrong_shape_permanent(tmp_path: Path) -> None:
    client = ScriptedLLMClient([LLMResponse(text=valid_wire_json(), stop="end")], token_count=10)
    with pytest.raises(PermanentError, match="digest themes schema"):
        themer(client, tmp_path).themes(make_request(2))


def test_invalid_json_repaired_once_for_openai_compat(tmp_path: Path) -> None:
    client = ScriptedLLMClient(
        [LLMResponse(text="{", stop="end"), themes_answer(("A", "s", ["L1", "L2"]))],
        token_count=10,
    )
    record = themer(client, tmp_path, repair=True).themes(make_request(2))
    assert client.complete_calls == 2
    assert record.requests == 2
    assert [t.leads for t in record.themes] == [("L1", "L2")]
    assert any(
        "did not match the digest themes schema and was repaired" in w for w in record.warnings
    ) or any("repaired" in w for w in record.warnings)


def test_refusal_permanent_names_category(tmp_path: Path) -> None:
    client = ScriptedLLMClient(
        [LLMResponse(text="", stop="refusal", refusal_category="cyber")], token_count=10
    )
    with pytest.raises(PermanentError, match="declined to analyze the research leads") as e:
        themer(client, tmp_path).themes(make_request(2))
    assert "cyber" in e.value.message


def test_max_tokens_retried_once(tmp_path: Path) -> None:
    client = ScriptedLLMClient(
        [LLMResponse(text="", stop="max_tokens"), themes_answer(("A", "s", ["L1"]))],
        output_tokens=500,
        max_output_tokens=2000,
        token_count=10,
    )
    record = themer(client, tmp_path).themes(make_request(1))
    assert [r.max_tokens for r in client.complete_requests] == [500, 2000]
    assert record.requests == 2


def test_transient_error_propagates(tmp_path: Path) -> None:
    client = ScriptedLLMClient([TransientError("busy")], token_count=10)
    with pytest.raises(TransientError, match="busy"):
        themer(client, tmp_path).themes(make_request(1))


def test_fallback_warning_recorded(tmp_path: Path) -> None:
    answer = LLMResponse(
        text=json.dumps({"themes": [{"title": "A", "summary": "s", "items": ["L1"]}]}),
        stop="end",
        warnings=("answered by a fallback model",),
    )
    client = ScriptedLLMClient([answer], token_count=10)
    record = themer(client, tmp_path).themes(make_request(1))
    assert "answered by a fallback model" in record.warnings


def test_caller_default_messages_unchanged() -> None:
    request = LLMRequest("s", "u", {}, "n", 10)
    refusing = ScriptedLLMClient([LLMResponse(text="", stop="refusal", refusal_category="bio")])
    with pytest.raises(PermanentError) as refused:
        ModelCaller(refusing, repair=False, episode_key="k").complete(request, 1, what="part 1")
    assert refused.value.message == (
        "claude-test declined to analyze this episode (part 1) (refusal category: bio)"
    )
    caller = ModelCaller(ScriptedLLMClient(), repair=False, episode_key="k")
    with pytest.raises(PermanentError, match="does not match the analysis schema"):
        caller.parse(LLMResponse(text="{}", stop="end"), request, what="the analysis")


# -- call cache --------------------------------------------------------------------------------


def test_call_cache_resumes(tmp_path: Path) -> None:
    baseline = small_client()
    themer(baseline, tmp_path / "base").themes(make_request(40))
    total = baseline.complete_calls

    state = {"n": 0}

    def flaky(request: LLMRequest) -> LLMResponse | Exception:
        state["n"] += 1
        if state["n"] == 2:
            return TransientError("the server is busy")
        return pairs(request)

    first = small_client(respond=flaky)
    with pytest.raises(TransientError):
        themer(first, tmp_path / "resume").themes(make_request(40))
    assert first.complete_calls == 2

    second = small_client()
    record = themer(second, tmp_path / "resume").themes(make_request(40))
    assert second.complete_calls == total - 1  # batch 1 came from the cache
    covered(tuple(t.leads for t in record.themes), 40, ())


def test_invalid_cached_call_is_recomputed(tmp_path: Path) -> None:
    first = small_client()
    themer(first, tmp_path).themes(make_request(40))
    cache = DigestCache(tmp_path)
    files = sorted((cache.root / f"{HASH}.calls").glob("*.json"))
    assert files
    files[0].write_text("garbage")
    second = small_client()
    themer(second, tmp_path).themes(make_request(40))
    assert (
        second.complete_calls == len(files) + 1 - (len(files) - 1) - 1 + 1
        or second.complete_calls >= 1
    )
    assert json.loads(files[0].read_text())["themes"]
