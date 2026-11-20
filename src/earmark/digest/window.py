"""Digest windows: ``--week`` and ``--since`` parsing and local-midnight boundaries. Pure,
standard library only. Both bounds are instants (UTC) of local midnights, so DST changes need
no special casing."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Final

MAX_SINCE_DAYS: Final = 366
_WEEK = re.compile(r"(\d{4})-[Ww](\d{2})")
_SINCE = re.compile(r"([1-9][0-9]{0,2})d")


class WindowError(ValueError):
    """A bad --week or --since value; the message is safe to print."""


@dataclass(frozen=True, slots=True)
class DigestWindow:
    digest_id: str  # "2026-W47" or "2026-11-14..2026-11-20"
    label: str  # "2026-W47" or "2026-11-14 to 2026-11-20"
    file_stem: str  # the note's file name without .md; equals label
    first_day: date  # local, inclusive
    last_day: date  # local, inclusive
    start: datetime  # aware, UTC: local midnight of first_day (inclusive)
    end: datetime  # aware, UTC: local midnight of last_day + 1 (exclusive)
    weekly: bool

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def local_midnight(day: date, tz: tzinfo | None) -> datetime:
    """The first instant of the local ``day`` (``fold=0``), as an aware UTC datetime."""
    if tz is None:
        return datetime.combine(day, time()).astimezone(UTC)
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def parse_week(text: str) -> tuple[int, int]:
    m = _WEEK.fullmatch(text.strip())
    if m is None:
        raise WindowError("--week must look like 2026-W47")
    year, week = int(m.group(1)), int(m.group(2))
    if not 1970 <= year <= 9998:
        raise WindowError("--week: the year must be between 1970 and 9998")
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError:
        if week == 53:
            raise WindowError(f"{year} has no ISO week 53") from None
        raise WindowError("--week must look like 2026-W47") from None
    return year, week


def parse_since(text: str) -> int:
    m = _SINCE.fullmatch(text.strip())
    if m is None or int(m.group(1)) > MAX_SINCE_DAYS:
        raise WindowError(f"--since must be a number of days such as 7d (1 to {MAX_SINCE_DAYS})")
    return int(m.group(1))


def week_window(year: int, week: int, tz: tzinfo | None) -> DigestWindow:
    first = date.fromisocalendar(year, week, 1)
    ident = f"{year:04d}-W{week:02d}"
    return DigestWindow(
        digest_id=ident,
        label=ident,
        file_stem=ident,
        first_day=first,
        last_day=first + timedelta(days=6),
        start=local_midnight(first, tz),
        end=local_midnight(first + timedelta(days=7), tz),
        weekly=True,
    )


def _today(now: datetime, tz: tzinfo | None) -> date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(tz).date()


def since_window(days: int, *, now: datetime, tz: tzinfo | None) -> DigestWindow:
    today = _today(now, tz)
    first = today - timedelta(days=days - 1)
    return DigestWindow(
        digest_id=f"{first.isoformat()}..{today.isoformat()}",
        label=f"{first.isoformat()} to {today.isoformat()}",
        file_stem=f"{first.isoformat()} to {today.isoformat()}",
        first_day=first,
        last_day=today,
        start=local_midnight(first, tz),
        end=local_midnight(today + timedelta(days=1), tz),
        weekly=False,
    )


def last_complete_week(*, now: datetime, tz: tzinfo | None) -> DigestWindow:
    year, week, _ = (_today(now, tz) - timedelta(days=7)).isocalendar()
    return week_window(year, week, tz)


def select_window(
    *, week: str | None, since: str | None, now: datetime, tz: tzinfo | None
) -> DigestWindow:
    if week is not None and since is not None:
        raise WindowError("use either --week or --since, not both")
    if week is not None:
        year, number = parse_week(week)
        return week_window(year, number, tz)
    if since is not None:
        return since_window(parse_since(since), now=now, tz=tz)
    return last_complete_week(now=now, tz=tz)
