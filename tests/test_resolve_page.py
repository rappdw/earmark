import json
from datetime import UTC, datetime
from typing import Any

import pytest

from earmark.resolve.apple_page import (
    JSON_LD_ID,
    MAX_NODES,
    SERVER_DATA_ID,
    WrongShowError,
    episode_from_json_ld,
    episode_from_server_data,
    extract_page_json,
    parse_episode_page,
    parse_iso_duration,
)
from tests.apple_fixtures import PM_OLD_GUID, drop_script, fixture_bytes, rewrite_server_data

PM_SHOW = "290783428"
PM_EP = "1000655720058"
PAGE = fixture_bytes("page_planet_money_1000655720058.html")


def _blocks() -> dict[str, object]:
    return extract_page_json(PAGE.decode())


def test_extracts_both_blocks() -> None:
    blocks = _blocks()
    assert set(blocks) == {SERVER_DATA_ID, JSON_LD_ID}
    assert all(isinstance(v, dict) for v in blocks.values())


def test_server_data_episode() -> None:
    ep = episode_from_server_data(_blocks()[SERVER_DATA_ID], PM_SHOW, PM_EP)
    assert ep is not None
    assert ep.guid == PM_OLD_GUID
    assert ep.title == "Why Gold? (Classic)"
    assert ep.published_at == datetime(2024, 5, 15, 21, 56, 13, tzinfo=UTC)
    assert ep.duration_s == 1131.0
    assert ep.audio_url is not None
    assert ep.audio_url.startswith("https://tracking.swap.fm/")
    assert ep.audio_url.endswith("&size=18107751")
    assert "m3u8" not in ep.audio_url
    assert not ep.subscriber_only


def test_server_data_ignores_other_content_ids() -> None:
    assert episode_from_server_data(_blocks()[SERVER_DATA_ID], PM_SHOW, "1000000000001") is None


def test_server_data_wrong_show() -> None:
    with pytest.raises(WrongShowError) as info:
        episode_from_server_data(_blocks()[SERVER_DATA_ID], "1200361736", PM_EP)
    assert info.value.message == (
        f"Apple lists episode {PM_EP} under show {PM_SHOW}, not 1200361736. "
        "Share the episode again from Apple Podcasts."
    )


def _set_price_types(price: str) -> Any:
    def fn(data: Any) -> Any:
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                enclosures = node.get("mediaEnclosures")
                if isinstance(enclosures, list):
                    for entry in enclosures:
                        entry["priceType"] = price
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return data

    return fn


def test_server_data_subscriber_only() -> None:
    page = rewrite_server_data(PAGE, _set_price_types("PSUB"))
    ep = parse_episode_page(page, PM_SHOW, PM_EP)
    assert ep is not None
    assert ep.audio_url is None
    assert ep.subscriber_only


def _delete_key(key: str) -> Any:
    def fn(data: Any) -> Any:
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                node.pop(key, None)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)
        return data

    return fn


def test_server_data_no_enclosures_uses_offer_stream() -> None:
    page = rewrite_server_data(PAGE, _delete_key("mediaEnclosures"))
    ep = parse_episode_page(page, PM_SHOW, PM_EP)
    assert ep is not None
    assert ep.audio_url is not None
    assert ep.audio_url.startswith("https://tracking.swap.fm/")
    assert not ep.subscriber_only


def test_server_data_node_budget() -> None:
    bomb: list[object] = [[] for _ in range(MAX_NODES + 100)]
    assert episode_from_server_data(bomb, PM_SHOW, PM_EP) is None


def test_server_data_deep_nesting_does_not_recurse() -> None:
    node: Any = {"contentId": PM_EP, "guid": "g"}
    for _ in range(5000):
        node = {"x": node}
    ep = episode_from_server_data(node, PM_SHOW, PM_EP)
    assert ep is not None
    assert ep.guid == "g"


def test_json_ld_episode() -> None:
    ep = episode_from_json_ld(_blocks()[JSON_LD_ID], PM_SHOW, PM_EP)
    assert ep is not None
    assert ep.title == "Why Gold? (Classic)"
    assert ep.date_only
    assert ep.published_at == datetime(2024, 5, 15, tzinfo=UTC)
    assert ep.duration_s == 1131.0
    assert ep.guid is None
    assert ep.audio_url is None


def test_json_ld_wrong_episode_or_show() -> None:
    data = _blocks()[JSON_LD_ID]
    assert episode_from_json_ld(data, PM_SHOW, "1000000000001") is None
    with pytest.raises(WrongShowError):
        episode_from_json_ld(data, "1200361736", PM_EP)
    assert episode_from_json_ld("nope", PM_SHOW, PM_EP) is None
    assert episode_from_json_ld({"@type": "Thing"}, PM_SHOW, PM_EP) is None


def test_json_ld_empty_title_is_none() -> None:
    block = _blocks()[JSON_LD_ID]
    assert isinstance(block, dict)
    data = {**block, "name": "  "}
    assert episode_from_json_ld(data, PM_SHOW, PM_EP) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT18M51S", 1131.0),
        ("PT1H", 3600.0),
        ("PT0.5S", 0.5),
        ("P1D", 86400.0),
        ("P1DT2H", 93600.0),
        ("", None),
        ("18:51", None),
        ("P", None),
        ("PT", None),
        (None, None),
        (5, None),
    ],
)
def test_parse_iso_duration(value: object, expected: float | None) -> None:
    assert parse_iso_duration(value) == expected


def test_unparseable_blocks_ignored() -> None:
    html = (
        '<script id="serialized-server-data">{not json</script>'
        '<script id=schema:episode type="application/ld+json">{"a": 1}</script>'
    )
    assert extract_page_json(html) == {JSON_LD_ID: {"a": 1}}


def test_first_occurrence_of_each_block_wins() -> None:
    html = (
        '<script id="schema:episode">{"n": 1}</script><script id="schema:episode">{"n": 2}</script>'
    )
    assert extract_page_json(html) == {JSON_LD_ID: {"n": 1}}


def test_parse_page_falls_back_to_json_ld() -> None:
    ep = parse_episode_page(drop_script(PAGE, SERVER_DATA_ID), PM_SHOW, PM_EP)
    assert ep is not None
    assert ep.guid is None
    assert ep.date_only


def test_parse_page_with_neither_block() -> None:
    page = drop_script(drop_script(PAGE, SERVER_DATA_ID), JSON_LD_ID)
    assert parse_episode_page(page, PM_SHOW, PM_EP) is None


def test_script_text_cannot_close_the_element() -> None:
    def tweak(data: Any) -> Any:
        return {"title": "</script><b>x</b>", "data": data}

    page = rewrite_server_data(PAGE, tweak)
    assert b"</script><b>" not in page
    ep = parse_episode_page(page, PM_SHOW, PM_EP)
    assert ep is not None
    assert json.dumps(extract_page_json(page.decode())[SERVER_DATA_ID])
