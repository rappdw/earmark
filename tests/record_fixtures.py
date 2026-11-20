"""Re-record the trimmed Apple and feed fixtures in ``tests/fixtures/apple/``.

Run by hand, never by pytest: ``uv run python -m tests.record_fixtures [--out DIR]``.
Talks to the real services through ``GuardedFetcher``; when local DNS answers private
addresses (the dev sandbox) the address guard is turned off, as the live tests do.
The README in the output directory is written by hand and never touched here.
"""

import argparse
import html
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from earmark.fetch import MIB, GuardedFetcher
from earmark.models import EpisodeRef
from earmark.resolve.apple import canonical_episode_url
from earmark.resolve.itunes import LOOKUP_URL, lookup_params
from earmark.resolve.rss import parse_feed
from tests.http_helpers import dns_intercepted

logger = logging.getLogger("record_fixtures")

DEFAULT_OUT = Path(__file__).parent / "fixtures" / "apple"
MAX_BYTES = 20 * MIB

DAILY_SHOW = "1200361736"
DAILY_EPISODES = ["1000802907981", "1000802680076", "1000802062822", "1000801908793"]
BW_SHOW = "1335814741"
BW_EPISODES = ["1000799743520", "1000799743409", "1000799743555", "1000802882841"]
PM_SHOW = "290783428"
PM_OLD_EPISODE = "1000655720058"
PM_OLD_GUID = "e3153a19-8617-46a1-a9ef-989fe009911c"
NO_FEED_SHOW = "1656859371"
EMPTY_SHOW = "999999999999"

_ITEM = re.compile(rb"<item\b.*?</item>", re.DOTALL)
_GUID = re.compile(rb"<guid\b[^>]*>(.*?)</guid>", re.DOTALL)
_CDATA = re.compile(rb"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_ENCLOSURE_URL = re.compile(
    rb"""<enclosure\b[^>]*?\burl\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL
)
_PAGE_SCRIPT = re.compile(
    rb'<script\b[^>]*\bid="?(?:schema:episode|serialized-server-data)"?[^>]*>.*?</script>',
    re.DOTALL,
)


class RecordingError(Exception):
    pass


def _lookup(fetcher: GuardedFetcher, show_id: str) -> bytes:
    ref = EpisodeRef(
        source="apple_podcasts", show_id=show_id, episode_id="1", country="us", url="-"
    )
    return fetcher.get(
        LOOKUP_URL, params=lookup_params(ref), max_bytes=MAX_BYTES, purpose="lookup"
    ).content


def _episodes(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in parsed["results"] if r.get("wrapperType") == "podcastEpisode"]


def _dump(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def trim_lookup(data: bytes, keep: list[str] | None) -> tuple[bytes, list[dict[str, Any]]]:
    """Keep the show record and the episodes with these track ids (``None``: the newest one)."""
    parsed = json.loads(data)
    shows = [r for r in parsed["results"] if r.get("wrapperType") == "track"]
    episodes = _episodes(parsed)
    if keep is None:
        wanted = episodes[:1]
    else:
        by_id = {str(e["trackId"]): e for e in episodes}
        missing = [k for k in keep if k not in by_id]
        if missing:
            raise RecordingError(f"episodes {missing} are no longer in the lookup result")
        wanted = [by_id[k] for k in keep]
    if len(shows) != 1 or not wanted:
        raise RecordingError("lookup result has no single show record or no episodes")
    parsed["results"] = [shows[0], *wanted]
    parsed["resultCount"] = len(parsed["results"])
    return _dump(parsed), wanted


def _item_guid(item: bytes) -> str | None:
    match = _GUID.search(item)
    if match is None:
        return None
    text = match.group(1).strip()
    cdata = _CDATA.fullmatch(text)
    return html.unescape((cdata.group(1) if cdata else text).decode("utf-8")).strip()


def _host_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.path


def _item_enclosure_key(item: bytes) -> tuple[str, str] | None:
    match = _ENCLOSURE_URL.search(item)
    if match is None:
        return None
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    return _host_path(html.unescape(raw.decode("utf-8")))


def trim_feed(data: bytes, wanted: list[tuple[str | None, str | None]]) -> bytes:
    """Keep the header, the items matching the (guid, episodeUrl) pairs, and the footer."""
    spans = list(_ITEM.finditer(data))
    if not spans:
        raise RecordingError("feed has no items")
    chosen: list[re.Match[bytes]] = []
    for guid, episode_url in wanted:
        key = _host_path(episode_url) if episode_url else None
        hits = [
            m
            for m in spans
            if (guid is not None and _item_guid(m.group(0)) == guid)
            or (key is not None and _item_enclosure_key(m.group(0)) == key)
        ]
        if not hits:
            raise RecordingError("a wanted episode is no longer in the feed")
        for hit in hits:
            if hit not in chosen:
                chosen.append(hit)
    if len(chosen) != len(wanted):
        raise RecordingError(f"expected {len(wanted)} feed items, found {len(chosen)}")
    chosen.sort(key=lambda m: m.start())
    out = data[: spans[0].start()]
    for match in chosen:
        out += match.group(0) + b"\n"
    out += data[spans[-1].end() :]
    parse_feed(out, host="recorder")  # self-check
    return out


def trim_page(data: bytes) -> bytes:
    scripts = [m.group(0) for m in _PAGE_SCRIPT.finditer(data)]
    if len(scripts) != 2:
        raise RecordingError(f"expected 2 JSON script blocks on the page, found {len(scripts)}")
    return (
        b"<!DOCTYPE html>\n<html><head>\n"
        + b"\n".join(scripts)
        + b"\n</head><body></body></html>\n"
    )


def _feed_url(lookup_bytes: bytes) -> str:
    for record in json.loads(lookup_bytes)["results"]:
        if record.get("wrapperType") == "track":
            return str(record["feedUrl"])
    raise RecordingError("no show record")


def _guid_url(episodes: list[dict[str, Any]]) -> list[tuple[str | None, str | None]]:
    return [(e.get("episodeGuid"), e.get("episodeUrl")) for e in episodes]


def record(fetcher: GuardedFetcher, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, data: bytes) -> None:
        (out / name).write_bytes(data)
        logger.info("wrote %s (%d bytes)", name, len(data))

    def fetch(url: str, purpose: str) -> bytes:
        return fetcher.get(url, max_bytes=MAX_BYTES, purpose=purpose).content

    full = _lookup(fetcher, DAILY_SHOW)
    trimmed, wanted = trim_lookup(full, DAILY_EPISODES)
    write("lookup_daily.json", trimmed)
    write("feed_daily.xml", trim_feed(fetch(_feed_url(full), "feed"), _guid_url(wanted)))

    full = _lookup(fetcher, BW_SHOW)
    trimmed, wanted = trim_lookup(full, BW_EPISODES)
    write("lookup_business_wars.json", trimmed)
    write("feed_business_wars.xml", trim_feed(fetch(_feed_url(full), "feed"), _guid_url(wanted)))

    full = _lookup(fetcher, PM_SHOW)
    if any(str(e["trackId"]) == PM_OLD_EPISODE for e in _episodes(json.loads(full))):
        raise RecordingError("the out-of-window Planet Money episode is now inside the window")
    trimmed, wanted = trim_lookup(full, None)
    write("lookup_planet_money.json", trimmed)
    pairs = [*_guid_url(wanted), (PM_OLD_GUID, None)]
    write("feed_planet_money.xml", trim_feed(fetch(_feed_url(full), "feed"), pairs))

    page = fetch(canonical_episode_url("us", PM_SHOW, PM_OLD_EPISODE), PAGE_PURPOSE)
    write("page_planet_money_1000655720058.html", trim_page(page))

    write("lookup_no_feed.json", _lookup(fetcher, NO_FEED_SHOW))
    write("lookup_empty.json", _lookup(fetcher, EMPTY_SHOW))


PAGE_PURPOSE = "episode page"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    allow = dns_intercepted()
    if allow:
        logger.info("local DNS answers private addresses; the address guard is off")
    with GuardedFetcher(allow_private=allow) as fetcher:
        record(fetcher, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
