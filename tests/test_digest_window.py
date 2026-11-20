from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from earmark.digest.window import (
    WindowError,
    last_complete_week,
    local_midnight,
    parse_since,
    parse_week,
    select_window,
    since_window,
    week_window,
)

BERLIN = ZoneInfo("Europe/Berlin")
LA = ZoneInfo("America/Los_Angeles")


def test_week_bounds_are_local_midnights() -> None:
    w = week_window(2026, 47, BERLIN)
    assert w.start == datetime(2026, 11, 15, 23, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert (w.first_day, w.last_day) == (date(2026, 11, 16), date(2026, 11, 22))
    assert (w.digest_id, w.label, w.file_stem, w.weekly) == ("2026-W47",) * 3 + (True,)


def test_share_at_local_monday_midnight_is_in_new_week() -> None:
    new = week_window(2026, 47, BERLIN)
    old = week_window(2026, 46, BERLIN)
    moment = datetime(2026, 11, 16, 0, 0, tzinfo=BERLIN)
    assert new.contains(moment)
    assert not old.contains(moment)


def test_share_one_microsecond_before_is_in_previous_week() -> None:
    moment = datetime(2026, 11, 16, 0, 0, tzinfo=BERLIN) - timedelta(microseconds=1)
    assert week_window(2026, 46, BERLIN).contains(moment)
    assert not week_window(2026, 47, BERLIN).contains(moment)


def test_utc_vs_local_day() -> None:
    moment = datetime(2026, 11, 23, 7, 30, tzinfo=UTC)  # Sunday 23:30 in Los Angeles
    assert week_window(2026, 47, LA).contains(moment)
    assert not week_window(2026, 48, LA).contains(moment)
    assert week_window(2026, 48, UTC).contains(moment)


def test_dst_spring_week_is_167_hours() -> None:
    w = week_window(2026, 13, BERLIN)  # DST starts Sunday 29 March
    assert w.end - w.start == timedelta(hours=167)
    inside = datetime(2026, 3, 29, 3, 30, tzinfo=BERLIN)
    assert w.contains(inside)
    assert w.contains(datetime(2026, 3, 29, 23, 59, tzinfo=BERLIN))
    assert not w.contains(datetime(2026, 3, 30, 0, 0, tzinfo=BERLIN))


def test_dst_autumn_week_is_169_hours() -> None:
    w = week_window(2026, 43, BERLIN)  # DST ends Sunday 25 October
    assert w.end - w.start == timedelta(hours=169)
    for fold in (0, 1):
        assert w.contains(datetime(2026, 10, 25, 2, 30, fold=fold, tzinfo=BERLIN))
    assert not w.contains(datetime(2026, 10, 26, 0, 0, tzinfo=BERLIN))
    assert not w.contains(datetime(2026, 10, 18, 23, 59, tzinfo=BERLIN))


def test_iso_year_boundary() -> None:
    w = week_window(2026, 53, UTC)
    assert w.file_stem == "2026-W53"
    assert w.contains(datetime(2027, 1, 2, 12, tzinfo=UTC))
    assert (w.first_day, w.last_day) == (date(2026, 12, 28), date(2027, 1, 3))
    assert not week_window(2027, 1, UTC).contains(datetime(2027, 1, 2, 12, tzinfo=UTC))


def test_week_53_rejected_when_missing() -> None:
    with pytest.raises(WindowError, match="2025 has no ISO week 53"):
        parse_week("2025-W53")
    assert parse_week("2026-W53") == (2026, 53)


def test_since_window_includes_today() -> None:
    now = datetime(2026, 11, 20, 15, 0, tzinfo=BERLIN)
    w = since_window(7, now=now, tz=BERLIN)
    assert (w.first_day, w.last_day) == (date(2026, 11, 14), date(2026, 11, 20))
    assert w.start == local_midnight(date(2026, 11, 14), BERLIN)
    assert w.end == local_midnight(date(2026, 11, 21), BERLIN)
    assert w.digest_id == "2026-11-14..2026-11-20"
    assert w.file_stem == w.label == "2026-11-14 to 2026-11-20"
    assert not w.weekly
    # Two runs on the same day are the same window.
    later = since_window(7, now=now + timedelta(hours=5), tz=BERLIN)
    assert later == w


def test_since_window_start_in_midnight_gap() -> None:
    santiago = ZoneInfo("America/Santiago")  # 2026-09-06 has no 00:00
    now = datetime(2026, 9, 12, 12, 0, tzinfo=santiago)
    w = since_window(7, now=now, tz=santiago)
    assert w.first_day == date(2026, 9, 6)
    assert w.start == datetime(2026, 9, 6, 4, 0, tzinfo=UTC)


def test_last_complete_week_on_monday_and_sunday() -> None:
    monday = datetime(2026, 11, 23, 7, 0, tzinfo=BERLIN)
    assert last_complete_week(now=monday, tz=BERLIN).digest_id == "2026-W47"
    sunday = datetime(2026, 11, 22, 21, 0, tzinfo=BERLIN)
    assert last_complete_week(now=sunday, tz=BERLIN).digest_id == "2026-W46"
    # The local date decides, not the UTC date.
    assert last_complete_week(
        now=datetime(2026, 11, 22, 23, 30, tzinfo=UTC), tz=BERLIN
    ).digest_id == ("2026-W47")


def test_select_window_rejects_both() -> None:
    with pytest.raises(WindowError, match="either --week or --since"):
        select_window(week="2026-W47", since="7d", now=datetime.now(UTC), tz=None)


def test_select_window_dispatch() -> None:
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    assert select_window(week="2026-w47", since=None, now=now, tz=UTC).digest_id == "2026-W47"
    assert select_window(week=None, since=" 3d ", now=now, tz=UTC).first_day == date(2026, 11, 18)
    assert select_window(week=None, since=None, now=now, tz=UTC).digest_id == "2026-W46"
    with pytest.raises(ValueError, match="aware"):
        select_window(week=None, since=None, now=datetime(2026, 11, 20, tzinfo=None), tz=UTC)  # noqa: DTZ001


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("2026-47", "--week must look like 2026-W47"),
        ("26-W47", "--week must look like 2026-W47"),
        ("2026-W00", "--week must look like 2026-W47"),
        ("2026-W54", "--week must look like 2026-W47"),
        ("1969-W50", "the year must be between 1970 and 9998"),
        ("2025-W53", "2025 has no ISO week 53"),
    ],
)
def test_parse_week_errors(text: str, message: str) -> None:
    with pytest.raises(WindowError, match=message):
        parse_week(text)


@pytest.mark.parametrize("text", ["0d", "367d", "7", "d", "-1d", "7days", "1.5d", ""])
def test_parse_since_errors(text: str) -> None:
    with pytest.raises(WindowError, match="--since must be a number of days"):
        parse_since(text)


def test_parse_since_bounds() -> None:
    assert parse_since("1d") == 1
    assert parse_since("366d") == 366
