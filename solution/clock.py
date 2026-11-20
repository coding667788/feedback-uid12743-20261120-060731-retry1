"""Instant handling.

All instants are integer microseconds since the Unix epoch, UTC.  Input and
output use RFC 3339 with a mandatory ``Z`` suffix.  Leases use half-open
intervals ``[start, end)``: a lease is active at ``t`` iff
``start <= t < end``.  "Exactly expiring" therefore means the first instant at
which a lease is no longer active.
"""

from __future__ import annotations

import re

_EPOCH_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$"
)
_DAYS_BEFORE_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


class InstantError(ValueError):
    """Raised when an instant literal cannot be parsed."""


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap(year) else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 (Howard Hinnant's civil-from-days inverse)."""

    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_instant(text: str) -> int:
    """Parse an RFC 3339 ``...Z`` instant into integer microseconds."""

    if not isinstance(text, str):
        raise InstantError("instant must be a string")
    match = _EPOCH_RE.match(text)
    if match is None:
        raise InstantError(f"malformed instant: {text!r}")
    year, month, day, hour, minute, second, fraction = match.groups()
    year = int(year)
    month = int(month)
    day = int(day)
    hour = int(hour)
    minute = int(minute)
    second = int(second)
    if not (1 <= month <= 12):
        raise InstantError(f"month out of range: {text!r}")
    if not (1 <= day <= _days_in_month(year, month)):
        raise InstantError(f"day out of range: {text!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise InstantError(f"time out of range: {text!r}")
    micros = int(fraction.ljust(6, "0")) if fraction else 0
    seconds = _days_from_civil(year, month, day) * 86400
    seconds += hour * 3600 + minute * 60 + second
    return seconds * 1_000_000 + micros


def _civil_from_days(days: int) -> tuple[int, int, int]:
    z = days + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (1 if m <= 2 else 0), m, d


def format_instant(micros: int) -> str:
    """Render integer microseconds as canonical RFC 3339 with ``Z``.

    Trailing zeros in the fractional part are trimmed; a whole second is
    rendered without a fractional part at all.
    """

    if not isinstance(micros, int):
        raise InstantError("instant value must be an integer microsecond count")
    seconds, fraction = divmod(micros, 1_000_000)
    days, rem = divmod(seconds, 86400)
    year, month, day = _civil_from_days(days)
    hour, rem = divmod(rem, 3600)
    minute, second = divmod(rem, 60)
    text = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
    if fraction:
        text += "." + f"{fraction:06d}".rstrip("0")
    return text + "Z"
