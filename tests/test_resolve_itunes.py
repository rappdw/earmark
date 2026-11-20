import json
from datetime import UTC, datetime
from typing import Any

import pytest

from earmark.errors import TransientError
from earmark.models import EpisodeRef
from earmark.resolve.apple import canonical_episode_url
from earmark.resolve.itunes import (
    LookupFormatError,
    http_audio_url,
    id_equals,
    lookup_params,
    parse_lookup,
)
from tests.apple_fixtures import (
    DAILY_EPISODE,
    DAILY_GUID,
    fixture_bytes,
    fixture_json,
    with_episode_fields,
)

SHOW = "1200361736"


def _lookup(**changes: object) -> bytes:
    return with_episode_fields(fixture_json("lookup_daily.json"), DAILY_EPISODE, **changes)


def _parse(data: bytes) -> Any:
    return parse_lookup(data, SHOW, DAILY_EPISODE)


def test_lookup_params() -> None:
    ref = EpisodeRef(
        source="apple_podcasts",
        show_id="290783428",
        episode_id="1000655720058",
        country="gb",
        url=canonical_episode_url("gb", "290783428", "1000655720058"),
    )
    assert lookup_params(ref) == {
        "id": "290783428",
        "entity": "podcastEpisode",
        "limit": "200",
        "country": "gb",
    }


def test_parse_daily_lookup() -> None:
    result = _parse(fixture_bytes("lookup_daily.json"))
    assert result.episode_count == 4
    assert result.show is not None
    assert result.show.title == "The Daily"
    assert result.show.feed_url == "https://feeds.simplecast.com/Sl5CSM3S"
    ep = result.episode
    assert ep is not None
    assert ep.guid == DAILY_GUID
    assert ep.title.startswith("\u2018Did A.I. Break the Internet?\u2019")
    assert ep.published_at == datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)
    assert ep.duration_s == 1735.0
    assert ep.audio_url is not None
    assert ep.audio_url.startswith("https://dts.podtrac.com/")
    assert ep.description
    assert not ep.subscriber_only
    assert not ep.date_only


def test_show_record_track_time_millis_ignored() -> None:
    result = _parse(fixture_bytes("lookup_daily.json"))
    # The show record carries trackTimeMillis (1692); the episode's value must be used.
    assert result.episode is not None
    assert result.episode.duration_s == 1735.0


def test_episode_missing_is_none_but_show_found() -> None:
    result = parse_lookup(fixture_bytes("lookup_daily.json"), SHOW, "1")
    assert result.show is not None
    assert result.episode is None


def test_show_must_match_id() -> None:
    result = parse_lookup(fixture_bytes("lookup_daily.json"), "999", DAILY_EPISODE)
    assert result.show is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(1200361736, True, id="int"),
        pytest.param("1200361736", True, id="str"),
        pytest.param(" 1200361736 ", True, id="padded"),
        pytest.param(True, False, id="bool"),
        pytest.param(1.2e9, False, id="float"),
        pytest.param(None, False, id="none"),
        pytest.param("1200361737", False, id="other"),
    ],
)
def test_ids_compare_as_int_or_string(value: object, expected: bool) -> None:
    assert id_equals(value, "1200361736") is expected


def test_wrong_types_are_missing() -> None:
    data = json.loads(fixture_bytes("lookup_daily.json"))
    data["results"][0]["feedUrl"] = ["https://x.example/f"]
    data["results"][0]["collectionName"] = 5
    data["results"][0]["trackName"] = "Fallback title"
    ep = data["results"][1]
    ep["trackTimeMillis"] = "1735000"
    ep["trackName"] = 42
    ep["releaseDate"] = 20261119
    result = _parse(json.dumps(data).encode())
    assert result.show is not None
    assert result.show.feed_url is None
    assert result.show.title == "Fallback title"
    assert result.episode is not None
    assert result.episode.duration_s is None
    assert result.episode.title == ""
    assert result.episode.published_at is None


@pytest.mark.parametrize("bad", ["ftp://x.example/f", "feeds.example/x", "", "   ", "http:///x"])
def test_feed_url_must_be_http(bad: str) -> None:
    data = json.loads(fixture_bytes("lookup_daily.json"))
    data["results"][0]["feedUrl"] = bad
    assert _parse(json.dumps(data).encode()).show.feed_url is None


@pytest.mark.parametrize("bad", ["", "   ", "x" * 1001, "a\x07b", 5, None])
def test_guid_validation(bad: object) -> None:
    result = _parse(_lookup(episodeGuid=bad))
    assert result.episode is not None
    assert result.episode.guid is None


def test_guid_is_stripped() -> None:
    assert _parse(_lookup(episodeGuid="  abc  ")).episode.guid == "abc"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-19T10:47:59Z", datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)),
        ("2026-11-19T10:47:59+00:00", datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)),
        ("2026-11-19T10:47:59", datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)),
        ("2026-11-19T12:47:59+02:00", datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)),
        ("garbage", None),
    ],
)
def test_release_date_parsing(value: str, expected: datetime | None) -> None:
    assert _parse(_lookup(releaseDate=value)).episode.published_at == expected


def test_episode_url_m3u8_rejected() -> None:
    assert _parse(_lookup(episodeUrl="https://x.example/a/playlist.M3U8")).episode.audio_url is None
    assert http_audio_url("ftp://x.example/a.mp3") is None
    assert http_audio_url(" https://x.example/a.mp3 ") == "https://x.example/a.mp3"
    assert http_audio_url(None) is None


def test_empty_lookup() -> None:
    result = parse_lookup(fixture_bytes("lookup_empty.json"), "999999999999", "1")
    assert result.show is None
    assert result.episode is None
    assert result.episode_count == 0


def test_no_feed_lookup() -> None:
    result = parse_lookup(fixture_bytes("lookup_no_feed.json"), "1656859371", "1000587846633")
    assert result.show is not None
    assert result.show.feed_url is None
    assert result.episode is None


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"results": "x"}', b"", b"\xff\xfe"])
def test_unreadable_lookup_is_transient(body: bytes) -> None:
    with pytest.raises(LookupFormatError) as info:
        _parse(body)
    assert isinstance(info.value, TransientError)
    assert "cannot read" in info.value.message


def test_non_dict_results_entries_ignored() -> None:
    data = json.loads(fixture_bytes("lookup_daily.json"))
    data["results"] = ["x", 5, None, *data["results"]]
    assert _parse(json.dumps(data).encode()).episode is not None
