"""Load the recorded Apple fixtures, serve them through a MockTransport, and derive variants.

The fixture files are never edited: tests that need a variant (an item without an
enclosure, a changed GUID) derive it at run time with the mutators below.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from earmark.fetch import MIB
from earmark.resolve.apple import canonical_episode_url
from earmark.resolve.resolver import AppleResolver
from tests.http_helpers import mock_fetcher

FIXTURES = Path(__file__).parent / "fixtures" / "apple"

DAILY = canonical_episode_url("us", "1200361736", "1000802907981")
BW = canonical_episode_url("us", "1335814741", "1000799743520")
PM_OLD = canonical_episode_url("us", "290783428", "1000655720058")
NO_FEED = canonical_episode_url("us", "1656859371", "1000587846633")
UNKNOWN = canonical_episode_url("us", "999999999999", "1000000000001")

DAILY_GUID = "7704ac9b-bb66-4e07-a943-d08a0be0c9fb"
DAILY_EPISODE = "1000802907981"
BW_GUID_APPLE = "63f0a3d4-76bb-4236-a39e-10d1b235b3bd"
BW_EPISODE = "1000799743520"
PM_OLD_GUID = "e3153a19-8617-46a1-a9ef-989fe009911c"

LOOKUP_TYPE = "text/javascript; charset=utf-8"

_ITEM = re.compile(rb"<item\b.*?</item>", re.DOTALL)
_ENCLOSURE = re.compile(rb"<enclosure\b[^>]*?/>|<enclosure\b[^>]*>.*?</enclosure>", re.DOTALL)
_SERVER_DATA = re.compile(
    rb'(<script type="application/json" id="serialized-server-data">)(.*?)(</script>)', re.DOTALL
)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    data = json.loads(fixture_bytes(name))
    assert isinstance(data, dict)
    return data


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html"
    headers: dict[str, str] | None = None
    raises: Exception | None = None


def _ok(body: bytes, content_type: str) -> Reply:
    return Reply(200, body, content_type)


class FixtureServer:
    """A MockTransport handler serving the fixtures under their real hosts and paths.

    ``lookups`` is keyed by the ``id`` param, ``feeds`` by ``host + path``, ``pages`` by the
    episode id (the ``i`` param). Replace entries to change what a request gets. An unknown
    route fails the test.
    """

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.lookups: dict[str, Reply] = {}
        self.feeds: dict[str, Reply] = {}
        self.pages: dict[str, Reply] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "itunes.apple.com" and url.path == "/lookup":
            reply = self._route(self.lookups, url.params.get("id", ""), request)
        elif url.host == "podcasts.apple.com":
            return self._page(request)
        else:
            reply = self._route(self.feeds, url.host + url.path, request)
        return _response(reply, request)

    @staticmethod
    def _route(table: dict[str, Reply], key: str, request: httpx2.Request) -> Reply:
        if key not in table:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return table[key]

    def _page(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        episode = url.params.get("i", "")
        segments = [s for s in url.path.split("/") if s]
        reply = self._route(self.pages, episode, request)
        if reply.raises is not None or reply.status != 200:
            return _response(reply, request)
        # Real behaviour: the canonical path answers 301 to the slugged path, then 200.
        if len(segments) == 3:
            country, _, show = segments
            slug = f"/{country}/podcast/some-slug/{show}?i={episode}"
            return httpx2.Response(301, headers={"Location": slug}, request=request)
        return _response(reply, request)

    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.requests]


def _response(reply: Reply, request: httpx2.Request) -> httpx2.Response:
    if reply.raises is not None:
        raise reply.raises
    headers = {"content-type": reply.content_type, **(reply.headers or {})}
    return httpx2.Response(reply.status, content=reply.body, headers=headers, request=request)


def default_server() -> FixtureServer:
    server = FixtureServer()
    for name, show in (
        ("lookup_daily.json", "1200361736"),
        ("lookup_business_wars.json", "1335814741"),
        ("lookup_planet_money.json", "290783428"),
        ("lookup_no_feed.json", "1656859371"),
        ("lookup_empty.json", "999999999999"),
    ):
        server.lookups[show] = _ok(fixture_bytes(name), LOOKUP_TYPE)
    server.feeds["feeds.simplecast.com/Sl5CSM3S"] = _ok(
        fixture_bytes("feed_daily.xml"), "application/xml"
    )
    server.feeds["rss.art19.com/business-wars"] = _ok(
        fixture_bytes("feed_business_wars.xml"), "application/xml"
    )
    server.feeds["feeds.npr.org/510289/podcast.xml"] = _ok(
        fixture_bytes("feed_planet_money.xml"), "application/xml"
    )
    server.pages["1000655720058"] = _ok(
        fixture_bytes("page_planet_money_1000655720058.html"), "text/html"
    )
    return server


def resolver_for(server: FixtureServer) -> AppleResolver:
    return AppleResolver(mock_fetcher(server), max_bytes=20 * MIB)


def lookup_reply(body: bytes) -> Reply:
    return _ok(body, LOOKUP_TYPE)


def feed_reply(body: bytes) -> Reply:
    return _ok(body, "application/xml")


def page_reply(body: bytes) -> Reply:
    return _ok(body, "text/html")


# -- mutators ------------------------------------------------------------------------------------


def _item_with_guid(feed: bytes, guid: str) -> re.Match[bytes]:
    for match in _ITEM.finditer(feed):
        if guid.encode() in match.group(0):
            return match
    raise AssertionError(f"no feed item with GUID {guid!r}")


def drop_item(feed: bytes, guid: str) -> bytes:
    match = _item_with_guid(feed, guid)
    return feed[: match.start()] + feed[match.end() :]


def strip_enclosure(feed: bytes, guid: str) -> bytes:
    match = _item_with_guid(feed, guid)
    stripped, count = _ENCLOSURE.subn(b"", match.group(0))
    assert count >= 1, "item has no enclosure"
    return feed[: match.start()] + stripped + feed[match.end() :]


def with_episode_fields(lookup: dict[str, Any], track_id: str, **changes: object) -> bytes:
    """Set (or, with ``None``, delete) keys in one episode record and re-serialize."""
    data = json.loads(json.dumps(lookup))
    for record in data["results"]:
        if str(record.get("trackId")) == track_id and record.get("wrapperType") == "podcastEpisode":
            for key, value in changes.items():
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
            return json.dumps(data, ensure_ascii=False, indent=1).encode()
    raise AssertionError(f"no episode {track_id} in the lookup")


def without_show_record(lookup: dict[str, Any]) -> bytes:
    data = json.loads(json.dumps(lookup))
    data["results"] = [r for r in data["results"] if r.get("wrapperType") != "track"]
    data["resultCount"] = len(data["results"])
    return json.dumps(data).encode()


def rewrite_server_data(page: bytes, fn: Callable[[Any], Any]) -> bytes:
    match = _SERVER_DATA.search(page)
    assert match is not None, "page has no serialized-server-data"
    data = fn(json.loads(match.group(2)))
    text = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return page[: match.start(2)] + text.encode() + page[match.end(2) :]


def drop_script(page: bytes, script_id: str) -> bytes:
    pattern = re.compile(
        rb'<script\b[^>]*\bid="?' + re.escape(script_id.encode()) + rb'"?[^>]*>.*?</script>\n?',
        re.DOTALL,
    )
    result, count = pattern.subn(b"", page)
    assert count == 1, f"expected one script {script_id!r}"
    return result


def enclosure_urls(feed: bytes) -> list[str]:
    return [m.decode() for m in re.findall(rb'<enclosure\b[^>]*?\burl="([^"]*)"', feed)]


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""
