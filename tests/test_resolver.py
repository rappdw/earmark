import ast
import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

import earmark
from earmark.errors import PermanentError, SkipEpisode, TransientError
from earmark.fakes import FetchCall, make_fake_backends
from earmark.fetch import MIB, GuardedFetcher
from earmark.models import Episode
from earmark.pipeline.stages import Resolver
from earmark.resolve.apple import UnsupportedUrlError, canonical_episode_url
from earmark.resolve.apple_page import SERVER_DATA_ID, WrongShowError
from earmark.resolve.itunes import LOOKUP_URL, LookupFormatError
from earmark.resolve.resolver import (
    BAD_LOOKUP,
    EPISODE_NOT_FOUND,
    LOOKUP_REFUSED,
    NO_AUDIO_REASON,
    NO_FEED_REASON,
    NOT_IN_FEED,
    UNKNOWN_SHOW,
    AppleResolver,
)
from tests.apple_fixtures import (
    BW,
    BW_EPISODE,
    BW_GUID_APPLE,
    DAILY,
    DAILY_EPISODE,
    DAILY_GUID,
    NO_FEED,
    PM_OLD,
    PM_OLD_GUID,
    UNKNOWN,
    FixtureServer,
    Reply,
    default_server,
    drop_item,
    drop_script,
    enclosure_urls,
    feed_reply,
    fixture_bytes,
    fixture_json,
    lookup_reply,
    page_reply,
    resolver_for,
    rewrite_server_data,
    strip_enclosure,
    with_episode_fields,
)
from tests.factories import new_podcast_job
from tests.http_helpers import dns_intercepted
from tests.pipeline_env import CANON, PipelineEnv, make_env

SRC = Path(earmark.__file__).parent
DAILY_FEED = "feeds.simplecast.com/Sl5CSM3S"
PM_FEED = "feeds.npr.org/510289/podcast.xml"
DAILY_LOOKUP = fixture_json("lookup_daily.json")
BW_LOOKUP = fixture_json("lookup_business_wars.json")


def _daily_record() -> dict[str, Any]:
    [record] = [r for r in DAILY_LOOKUP["results"] if str(r.get("trackId")) == DAILY_EPISODE]
    assert isinstance(record, dict)
    return record


def _serve_daily_lookup(server: FixtureServer, **changes: object) -> None:
    server.lookups["1200361736"] = lookup_reply(
        with_episode_fields(DAILY_LOOKUP, DAILY_EPISODE, **changes)
    )


def _feed_without_item(feed_name: str, guid: str) -> Reply:
    return feed_reply(drop_item(fixture_bytes(feed_name), guid))


# -- A1: in-window resolution --------------------------------------------------------------------


def test_resolves_daily_episode() -> None:
    server = default_server()
    resolution = resolver_for(server).resolve_details(DAILY)
    assert resolution.source == "lookup"
    assert resolution.match == "guid"
    ep = resolution.episode
    assert ep.key == "f2faca69c9811409f6a6"
    assert ep.source == "apple_podcasts"
    assert ep.show_id == "1200361736"
    assert ep.episode_id == DAILY_EPISODE
    assert ep.guid == DAILY_GUID
    assert ep.title == (
        "\u2018Did A.I. Break the Internet?\u2019 and Other Things "
        "We\u2019ve Worried About This Week"
    )
    assert ep.show_title == "The Daily"
    assert ep.published_at == datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)
    assert ep.duration_s == 1735.0
    assert ep.feed_url == "https://feeds.simplecast.com/Sl5CSM3S"
    assert ep.url == DAILY
    assert ep.description
    assert "\x00" not in ep.description
    feed = fixture_bytes("feed_daily.xml")
    [expected] = [u for u in enclosure_urls(feed) if "e4f0c51f-a7f8-409f-8786-bccd54d2e1a1" in u]
    assert ep.audio_url == expected.replace("&amp;", "&")
    assert ep.audio_url.startswith(
        "https://dts.podtrac.com/redirect.mp3/pdst.fm/e/pfx.vpixl.com/6qj4J/"
    )
    assert "episodes/e4f0c51f-a7f8-409f-8786-bccd54d2e1a1/audio/128/default.mp3" in ep.audio_url
    assert server.paths() == ["itunes.apple.com/lookup", DAILY_FEED]
    lookup_request = server.requests[0]
    assert dict(lookup_request.url.params) == {
        "id": "1200361736",
        "entity": "podcastEpisode",
        "limit": "200",
        "country": "us",
    }


def test_resolve_returns_episode() -> None:
    resolver = resolver_for(default_server())
    assert resolver.resolve(DAILY) == resolver.resolve_details(DAILY).episode


def test_resolver_satisfies_protocol() -> None:
    r: Resolver = resolver_for(default_server())
    assert isinstance(r.resolve(DAILY), Episode)


# -- A2: audio url -------------------------------------------------------------------------------


def test_audio_url_comes_from_feed() -> None:
    server = default_server()
    _serve_daily_lookup(server, episodeUrl="https://cdn.example/other.mp3")
    resolution = resolver_for(server).resolve_details(DAILY)
    assert resolution.match == "guid"
    assert "cdn.example" not in resolution.episode.audio_url
    assert resolution.episode.audio_url.startswith("https://dts.podtrac.com/")


def test_feed_without_item_uses_episode_url() -> None:
    server = default_server()
    server.feeds[DAILY_FEED] = _feed_without_item("feed_daily.xml", DAILY_GUID)
    resolution = resolver_for(server).resolve_details(DAILY)
    assert resolution.match is None
    assert resolution.episode.audio_url == _daily_record()["episodeUrl"]


def test_feed_without_item_and_no_episode_url_fails() -> None:
    server = default_server()
    _serve_daily_lookup(server, episodeUrl=None)
    server.feeds[DAILY_FEED] = _feed_without_item("feed_daily.xml", DAILY_GUID)
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(DAILY)
    assert info.value.message == NOT_IN_FEED


# -- A3: matching --------------------------------------------------------------------------------


def test_match_by_guid() -> None:
    resolution = resolver_for(default_server()).resolve_details(DAILY)
    assert resolution.match == "guid"
    assert "e4f0c51f-a7f8-409f-8786-bccd54d2e1a1" in resolution.episode.audio_url


def test_match_by_guid_beats_same_title_earlier_in_feed() -> None:
    # Every other item gets the target's title; the GUID still picks the right item.
    feed = fixture_bytes("feed_daily.xml")
    items = list(re.finditer(rb"<item>.*?</item>", feed, re.DOTALL))
    [target] = [m for m in items if DAILY_GUID.encode() in m.group(0)]
    title = re.search(rb"<title>.*?</title>", target.group(0), re.DOTALL)
    assert title is not None
    out = feed
    for match in reversed(items):
        if match is target:
            continue
        changed = re.sub(rb"<title>.*?</title>", lambda _: title.group(0), match.group(0), count=1)
        out = out[: match.start()] + changed + out[match.end() :]
    server = default_server()
    server.feeds[DAILY_FEED] = feed_reply(out)
    resolution = resolver_for(server).resolve_details(DAILY)
    assert resolution.match == "guid"
    assert "e4f0c51f-a7f8-409f-8786-bccd54d2e1a1" in resolution.episode.audio_url


def test_match_by_title() -> None:
    server = default_server()
    _serve_daily_lookup(server, episodeGuid="not-in-feed", episodeUrl=None)
    resolution = resolver_for(server).resolve_details(DAILY)
    assert resolution.match == "title"
    assert "e4f0c51f-a7f8-409f-8786-bccd54d2e1a1" in resolution.episode.audio_url
    assert resolution.episode.guid == "not-in-feed"


def test_match_by_date_business_wars() -> None:
    server = default_server()
    server.lookups["1335814741"] = lookup_reply(
        with_episode_fields(BW_LOOKUP, BW_EPISODE, episodeUrl=None)
    )
    resolution = resolver_for(server).resolve_details(BW)
    assert resolution.match == "date"
    assert resolution.episode.audio_url.startswith(
        "https://rss.art19.com/episodes/88f2ce7b-0ead-4e39-80f4-fa16a9a4b552.mp3"
    )


def test_match_by_enclosure_business_wars() -> None:
    resolution = resolver_for(default_server()).resolve_details(BW)
    assert resolution.match == "enclosure"
    ep = resolution.episode
    assert ep.guid == BW_GUID_APPLE
    assert ep.key == "bec3f156fed0cb38a63c"
    assert ep.title == "Big Beef: The War on Your Wallet | A Bloody Business"
    assert ep.published_at == datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    assert ep.duration_s == 2491.0
    assert ep.show_title == "Business Wars"
    assert ep.feed_url == "https://rss.art19.com/business-wars"
    assert ep.audio_url.startswith(
        "https://rss.art19.com/episodes/88f2ce7b-0ead-4e39-80f4-fa16a9a4b552.mp3"
    )


# -- A4: skips -----------------------------------------------------------------------------------


def test_show_without_feed_skips() -> None:
    server = default_server()
    with pytest.raises(SkipEpisode) as info:
        resolver_for(server).resolve(NO_FEED)
    assert info.value.reason == NO_FEED_REASON
    assert len(server.requests) == 1


def test_item_without_enclosure_skips() -> None:
    server = default_server()
    server.feeds[DAILY_FEED] = feed_reply(
        strip_enclosure(fixture_bytes("feed_daily.xml"), DAILY_GUID)
    )
    assert _daily_record()["episodeUrl"]
    with pytest.raises(SkipEpisode) as info:
        resolver_for(server).resolve(DAILY)
    assert info.value.reason == NO_AUDIO_REASON


def test_skip_reasons_name_subscribers_and_exclusive() -> None:
    for reason in (NO_FEED_REASON, NO_AUDIO_REASON):
        assert "subscribers" in reason
        assert "exclusive" in reason
        assert len(reason) <= 120


def _all_subscriber(data: object) -> object:
    def walk(node: object) -> None:
        if isinstance(node, dict):
            enclosures = node.get("mediaEnclosures")
            if isinstance(enclosures, list):
                for entry in enclosures:
                    entry["priceType"] = "PSUB"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return data


def test_page_subscriber_only_skips() -> None:
    server = default_server()
    server.pages["1000655720058"] = page_reply(
        rewrite_server_data(fixture_bytes("page_planet_money_1000655720058.html"), _all_subscriber)
    )
    server.feeds[PM_FEED] = _feed_without_item("feed_planet_money.xml", PM_OLD_GUID)
    with pytest.raises(SkipEpisode) as info:
        resolver_for(server).resolve(PM_OLD)
    assert info.value.reason == NO_AUDIO_REASON


# -- A5: out-of-window ---------------------------------------------------------------------------


def test_out_of_window_resolves_from_page() -> None:
    server = default_server()
    resolution = resolver_for(server).resolve_details(PM_OLD)
    assert resolution.source == "page"
    assert resolution.match == "guid"
    ep = resolution.episode
    assert ep.guid == PM_OLD_GUID
    assert ep.key == "06db3587077b5cb1d7a8"
    assert ep.title == "Why Gold? (Classic)"
    assert ep.published_at == datetime(2024, 5, 15, 21, 56, 13, tzinfo=UTC)
    assert ep.duration_s == 1131.0
    assert ep.show_title == "Planet Money"
    assert ep.feed_url == "https://feeds.npr.org/510289/podcast.xml"
    assert ep.audio_url.startswith("https://tracking.swap.fm/")
    assert ep.audio_url.endswith("&size=18107751")
    assert server.paths() == [
        "itunes.apple.com/lookup",
        "podcasts.apple.com/us/podcast/id290783428",
        "podcasts.apple.com/us/podcast/some-slug/id290783428",
        PM_FEED,
    ]


def test_out_of_window_not_in_feed_uses_page_audio() -> None:
    server = default_server()
    server.feeds[PM_FEED] = _feed_without_item("feed_planet_money.xml", PM_OLD_GUID)
    resolution = resolver_for(server).resolve_details(PM_OLD)
    assert resolution.match is None
    assert resolution.episode.audio_url.startswith("https://tracking.swap.fm/")
    assert "play.itunes.apple.com" not in resolution.episode.audio_url
    assert ".m3u8" not in resolution.episode.audio_url


def test_out_of_window_json_ld_only() -> None:
    server = default_server()
    page = drop_script(fixture_bytes("page_planet_money_1000655720058.html"), SERVER_DATA_ID)
    server.pages["1000655720058"] = page_reply(page)
    resolution = resolver_for(server).resolve_details(PM_OLD)
    ep = resolution.episode
    assert resolution.match == "title"
    assert ep.title == "Why Gold? (Classic)"
    assert ep.guid == PM_OLD_GUID  # the feed's, since the JSON-LD has none
    assert ep.published_at == datetime(2024, 5, 15, 21, 56, 13, tzinfo=UTC)
    assert ep.duration_s == 1131.0


def test_unknown_episode_fails() -> None:
    server = default_server()
    ref = canonical_episode_url("us", "290783428", "1000000000001")
    server.pages["1000000000001"] = page_reply(
        fixture_bytes("page_planet_money_1000655720058.html")
    )
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(ref)
    assert info.value.message == EPISODE_NOT_FOUND.format(
        episode_id="1000000000001", show_id="290783428"
    )


@pytest.mark.parametrize("status", [404, 410])
def test_page_gone_is_episode_not_found(status: int) -> None:
    server = default_server()
    server.pages["1000655720058"] = Reply(status, b"gone")
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(PM_OLD)
    assert info.value.message == EPISODE_NOT_FOUND.format(
        episode_id="1000655720058", show_id="290783428"
    )


def test_page_for_other_show_fails() -> None:
    server = default_server()
    url = canonical_episode_url("us", "1200361736", "1000655720058")
    with pytest.raises(WrongShowError) as info:
        resolver_for(server).resolve(url)
    assert "290783428" in info.value.message
    assert "1200361736" in info.value.message


def test_page_with_neither_block_fails() -> None:
    server = default_server()
    page = fixture_bytes("page_planet_money_1000655720058.html")
    page = drop_script(drop_script(page, SERVER_DATA_ID), "schema:episode")
    server.pages["1000655720058"] = page_reply(page)
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(PM_OLD)
    assert info.value.message == EPISODE_NOT_FOUND.format(
        episode_id="1000655720058", show_id="290783428"
    )


def test_page_subscriber_only_resolves_nothing_without_feed_item() -> None:
    server = default_server()
    server.feeds[PM_FEED] = _feed_without_item("feed_planet_money.xml", PM_OLD_GUID)
    ep = resolver_for(server).resolve(PM_OLD)
    assert ep.guid == PM_OLD_GUID


# -- A6: failures --------------------------------------------------------------------------------

FAILURES = [
    pytest.param(Reply(503, b"busy"), id="503"),
    pytest.param(Reply(raises=httpx2.ReadTimeout("slow")), id="read-timeout"),
    pytest.param(Reply(raises=httpx2.ConnectTimeout("slow")), id="connect-timeout"),
]


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(
    ("stage", "host", "count"),
    [
        ("lookup", "itunes.apple.com", 1),
        ("page", "podcasts.apple.com", 2),
        ("feed", "feeds.npr.org", 4),
    ],
)
def test_transient_failures(failure: Reply, stage: str, host: str, count: int) -> None:
    server = default_server()
    if stage == "lookup":
        server.lookups["290783428"] = failure
    elif stage == "page":
        server.pages["1000655720058"] = failure
    else:
        server.feeds[PM_FEED] = failure
    with pytest.raises(TransientError):
        resolver_for(server).resolve(PM_OLD)
    assert len(server.requests) == count
    assert server.requests[-1].url.host == host


def test_lookup_403_is_transient() -> None:
    server = default_server()
    server.lookups["1200361736"] = Reply(403, b"{}")
    with pytest.raises(TransientError) as info:
        resolver_for(server).resolve(DAILY)
    assert info.value.message == LOOKUP_REFUSED


def test_lookup_400_is_permanent() -> None:
    server = default_server()
    server.lookups["1200361736"] = Reply(400, b'{"errorMessage": "Invalid value(s)"}')
    gb = canonical_episode_url("gb", "1200361736", DAILY_EPISODE)
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(gb)
    assert info.value.message == BAD_LOOKUP.format(country="gb")


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"results": "x"}'])
def test_lookup_unreadable_is_transient(body: bytes) -> None:
    server = default_server()
    server.lookups["1200361736"] = lookup_reply(body)
    with pytest.raises(LookupFormatError):
        resolver_for(server).resolve(DAILY)


def test_unknown_show_fails() -> None:
    server = default_server()
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(UNKNOWN)
    assert info.value.message == UNKNOWN_SHOW.format(show_id="999999999999", country="us")
    assert len(server.requests) == 1


def test_lookup_without_show_record_fails() -> None:
    server = default_server()
    data = json.loads(json.dumps(DAILY_LOOKUP))
    data["results"] = [r for r in data["results"] if r["wrapperType"] != "track"]
    server.lookups["1200361736"] = lookup_reply(json.dumps(data).encode())
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(DAILY)
    assert info.value.message == UNKNOWN_SHOW.format(show_id="1200361736", country="us")


def test_feed_404_is_permanent() -> None:
    server = default_server()
    server.feeds[DAILY_FEED] = Reply(404, b"nope")
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(DAILY)
    assert not isinstance(info.value, TransientError)


def test_unsupported_url_is_permanent_and_makes_no_request() -> None:
    server = default_server()
    with pytest.raises(UnsupportedUrlError):
        resolver_for(server).resolve("https://example.com/x")
    assert server.requests == []


# -- A7: country ---------------------------------------------------------------------------------


def test_country_reaches_lookup_and_page() -> None:
    server = default_server()
    url = canonical_episode_url("gb", "290783428", "1000655720058")
    resolver_for(server).resolve(url)
    params = server.requests[0].url.params
    assert (params["country"], params["id"], params["entity"], params["limit"]) == (
        "gb",
        "290783428",
        "podcastEpisode",
        "200",
    )
    assert server.requests[1].url.path.startswith("/gb/podcast/")


# -- A8: hostile feed through the resolver -------------------------------------------------------


def test_resolver_feed_entities_fail_job_permanently() -> None:
    server = default_server()
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b"<rss><channel><title>&b;</title></channel></rss>"
    )
    server.feeds[DAILY_FEED] = feed_reply(bomb)
    with pytest.raises(PermanentError) as info:
        resolver_for(server).resolve(DAILY)
    assert not isinstance(info.value, TransientError)
    assert "entities" in info.value.message


# -- A9: only the injected fetcher ---------------------------------------------------------------


def test_resolver_uses_only_the_injected_fetcher() -> None:
    server = default_server()
    resolver_for(server).resolve(DAILY)
    resolver_for(server).resolve(PM_OLD)
    assert len(server.requests) == 2 + 4
    assert {r.headers["user-agent"] for r in server.requests} == {f"earmark/{earmark.__version__}"}


FORBIDDEN_IMPORTS = {"httpx2", "httpcore2", "urllib.request", "http.client", "socket", "requests"}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_resolve_modules_make_no_direct_requests() -> None:
    modules = sorted((SRC / "resolve").glob("*.py"))
    assert {m.name for m in modules} >= {"resolver.py", "rss.py", "itunes.py", "apple_page.py"}
    for path in modules:
        imports = _imports(path)
        assert not any(
            i.split(".")[0] in FORBIDDEN_IMPORTS or i in FORBIDDEN_IMPORTS for i in imports
        ), path
        uses_fetch = any(i == "earmark.fetch" or i.startswith("earmark.fetch.") for i in imports)
        assert uses_fetch == (path.name == "resolver.py"), path
        xml = {i for i in imports if i.split(".")[0] in {"defusedxml", "xml"}}
        if path.name == "rss.py":
            assert {i for i in xml if i.startswith("xml")} <= {
                "xml.etree.ElementTree",
                "xml.etree.ElementTree.Element",
            }
            assert any(i.startswith("defusedxml") for i in xml)
        else:
            assert not xml, path


# -- B4: in the pipeline -------------------------------------------------------------------------


def _pipeline_env(tmp_path: Path, server: FixtureServer) -> PipelineEnv:
    fakes = make_fake_backends(tmp_path / "data")
    backends = replace(fakes.backends(), resolver=resolver_for(server))
    return make_env(tmp_path, backends=backends, fakes=fakes)


def test_pipeline_with_apple_resolver(tmp_path: Path) -> None:
    env = _pipeline_env(tmp_path, default_server())
    assert CANON == DAILY
    job = env.jobs.enqueue(new_podcast_job(CANON)).job
    env.worker.run_once()
    done = env.jobs.get(job.id)
    assert done is not None
    assert done.status == "done"
    assert done.episode_key == "f2faca69c9811409f6a6"
    stored = env.fakes.artifacts.read_episode("f2faca69c9811409f6a6")
    assert stored == resolver_for(default_server()).resolve(DAILY)
    assert [c.episode.key for c in env.fakes.fetcher.calls if isinstance(c, FetchCall)] == [
        "f2faca69c9811409f6a6"
    ]


def test_pipeline_skip_no_feed(tmp_path: Path) -> None:
    env = _pipeline_env(tmp_path, default_server())
    job = env.jobs.enqueue(new_podcast_job(NO_FEED)).job
    env.worker.run_once()
    done = env.jobs.get(job.id)
    assert done is not None
    assert (done.status, done.error, done.stage) == ("skipped", NO_FEED_REASON, "resolve")


def test_pipeline_unknown_show_fails(tmp_path: Path) -> None:
    env = _pipeline_env(tmp_path, default_server())
    job = env.jobs.enqueue(new_podcast_job(UNKNOWN)).job
    env.worker.run_once()
    done = env.jobs.get(job.id)
    assert done is not None
    assert (done.status, done.stage) == ("failed", "resolve")


def test_pipeline_transient_requeues(tmp_path: Path) -> None:
    server = default_server()
    server.lookups["1200361736"] = Reply(503, b"busy", headers={"Retry-After": "600"})
    env = _pipeline_env(tmp_path, server)
    job = env.jobs.enqueue(new_podcast_job(CANON)).job
    env.worker.run_once()
    done = env.jobs.get(job.id)
    assert done is not None
    assert (done.status, done.attempts) == ("queued", 1)
    assert done.run_after == env.clock.now + timedelta(seconds=600)


# -- B8: messages and logs are safe --------------------------------------------------------------

bomb = b'<!DOCTYPE r [<!ENTITY a "x">]><rss><channel><title>&a;</title></channel></rss>'
UNSAFE = (
    "/Sl5CSM3S",
    "aid=rss_feed",
    "Did A.I.",
    "Why Gold",
    "passwd",
    "The Daily",
    "Planet Money",
)


def _no_audio_anywhere(server: FixtureServer) -> None:
    _serve_daily_lookup(server, episodeUrl=None)
    server.feeds[DAILY_FEED] = _feed_without_item("feed_daily.xml", DAILY_GUID)


def _outcomes() -> list[tuple[FixtureServer, str]]:
    cases: list[tuple[FixtureServer, str]] = []

    def case(url: str, tweak: Callable[[FixtureServer], object] | None = None) -> None:
        server = default_server()
        if tweak is not None:
            tweak(server)
        cases.append((server, url))

    case(DAILY, lambda s: s.lookups.__setitem__("1200361736", Reply(400, b"{}")))
    case(DAILY, lambda s: s.lookups.__setitem__("1200361736", Reply(503, b"busy")))
    case(DAILY, lambda s: s.feeds.__setitem__(DAILY_FEED, feed_reply(bomb)))
    case(
        DAILY,
        lambda s: _no_audio_anywhere(s),
    )
    case(NO_FEED)
    case(UNKNOWN)
    case(DAILY, lambda s: s.lookups.__setitem__("1200361736", Reply(403, b"{}")))
    case(DAILY, lambda s: s.lookups.__setitem__("1200361736", lookup_reply(b"<html>")))
    case(DAILY, lambda s: s.feeds.__setitem__(DAILY_FEED, feed_reply(b"<rss")))
    case(DAILY, lambda s: s.feeds.__setitem__(DAILY_FEED, Reply(404, b"x")))
    case(PM_OLD, lambda s: s.pages.__setitem__("1000655720058", Reply(404, b"x")))
    case(canonical_episode_url("us", "1200361736", "1000655720058"))
    return cases


def _failure_message(server: FixtureServer, url: str) -> str | None:
    try:
        resolver_for(server).resolve(url)
    except (PermanentError, TransientError, SkipEpisode) as exc:
        return exc.message
    return None


def test_messages_are_safe() -> None:
    messages = [_failure_message(server, url) for server, url in _outcomes()]
    assert all(m is not None for m in messages)
    for message in messages:
        assert message is not None
        assert not any(fragment in message for fragment in UNSAFE), message


def test_resolve_logs_hosts_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    resolver_for(default_server()).resolve(DAILY)
    resolver_for(default_server()).resolve(PM_OLD)
    ours = [r for r in caplog.records if r.name.startswith(("earmark.resolve", "earmark.fetch"))]
    assert any("resolved " in r.getMessage() for r in ours)
    for record in ours:
        text = record.getMessage()
        for fragment in ("/Sl5CSM3S", "aid=", "Did A.I.", "Why Gold"):
            assert fragment not in text


def test_resolve_log_line_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="earmark.resolve.resolver")
    resolver_for(default_server()).resolve(DAILY)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("resolved ")]
    assert line == (
        "resolved f2faca69c9811409f6a6 (show 1200361736, episode 1000802907981) "
        "from the lookup; feed match: guid; feed host feeds.simplecast.com"
    )


# -- A10: live ----------------------------------------------------------------------------------


@pytest.mark.network
def test_live_resolves_recent_episode() -> None:
    allow = dns_intercepted()
    with GuardedFetcher(allow_private=allow) as raw:
        doc = raw.get(
            LOOKUP_URL,
            params={"id": "1200361736", "entity": "podcastEpisode", "limit": "5", "country": "us"},
            max_bytes=20 * MIB,
        )
    records = [
        r for r in json.loads(doc.content)["results"] if r["wrapperType"] == "podcastEpisode"
    ]
    newest = records[0]
    url = canonical_episode_url("us", "1200361736", str(newest["trackId"]))
    resolver = AppleResolver(GuardedFetcher(allow_private=allow), max_bytes=20 * MIB)
    try:
        resolution = resolver.resolve_details(url)
    finally:
        resolver.close()
    ep = resolution.episode
    assert resolution.source == "lookup"
    assert resolution.match is not None
    assert ep.guid == str(newest["episodeGuid"]).strip()
    assert ep.title
    assert ep.audio_url.startswith(("https://", "http://"))
    assert ep.published_at is not None
    assert datetime.now(UTC) - ep.published_at < timedelta(days=60)
    assert ep.feed_url


@pytest.mark.network
def test_live_resolves_out_of_window_episode() -> None:
    resolver = AppleResolver(GuardedFetcher(allow_private=dns_intercepted()), max_bytes=20 * MIB)
    try:
        resolution = resolver.resolve_details(PM_OLD)
    finally:
        resolver.close()
    assert resolution.source == "page"
    assert resolution.episode.guid == PM_OLD_GUID
    assert resolution.episode.title == "Why Gold? (Classic)"
