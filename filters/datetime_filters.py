"""
filters/datetime_filters.py — Egress-side date/time value adapters.

Every function here is a pure, stateless transform: scalar in, scalar
out, zero I/O, zero knowledge of any specific contract or tenant. These
exist because RENDER_ARTIFACT (Jinja2) needs value-SHAPE adapters that
JMESPath/jsonschema cannot express -- that is presentation-layer
plumbing, not business logic (Directive II.1 is unaffected: nothing
here branches on a domain value).

RELATIVE DATE RANGES (shift_days / in_timezone / start_of_day /
end_of_day / to_date_str): compose these against the engine's own
`current_timestamp` (auto-injected into every RENDER_ARTIFACT context
as UTC-now, see executor.py) to build "last N days" / "yesterday
23:59:59" style boundaries for outbound reporting-API calls, e.g.:

    {{ current_timestamp | shift_days(-7) | to_date_str }}   -> "2026-08-20"
    {{ current_timestamp | shift_days(-1) | in_timezone('Asia/Ho_Chi_Minh')
                          | end_of_day }}                     -> yesterday 23:59:59 ICT, as ISO

CONSTRAINT: these are Jinja filters, usable in RENDER_ARTIFACT output
and in HTTP_DISPATCH's `url` / `headers` (both Jinja2-rendered) -- NOT
in HTTP_DISPATCH's `body_mapping`, which is resolved via JMESPath, a
query language with no date-arithmetic functions and no way to call an
arbitrary Python filter. A computed date range needed inside a POST
body currently has no path into the DAG; see the accompanying note for
how to close that gap if/when a contract needs it.

See EXPORTS at the bottom for the names these are registered under in
jinja_env.filters.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


def _parse_iso(value: Any) -> datetime:
    """Shared tolerant ISO-8601 parser -- accepts a bare date
    ('2026-01-15'), a full datetime, or a trailing 'Z' in place of
    '+00:00'. A timezone-naive result is assumed UTC, matching
    iso_to_epoch_ms's existing convention."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected a non-empty ISO-8601 string, got {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"could not parse '{value}' as ISO-8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_to_epoch_ms(value: Any) -> Optional[int]:
    """ISO-8601 date/datetime string -> epoch milliseconds (int). Many
    destination systems' date-type fields expect epoch-ms, not ISO
    strings. Passes None through unchanged so
    `field | default(none) | iso_to_epoch_ms` on an absent optional
    field still renders null instead of crashing the render."""
    if value is None:
        return None
    dt = _parse_iso(value)
    return int(dt.timestamp() * 1000)


def shift_days(value: Any, days: int) -> Optional[str]:
    """ISO-8601 string -> ISO-8601 string shifted by `days` (negative
    for the past, e.g. -7 for "7 days ago"). Pure calendar arithmetic
    in the VALUE's own timezone/offset -- chain `in_timezone` first if
    the shift must respect a specific timezone's calendar rather than
    whatever offset the input already carries."""
    if value is None:
        return None
    dt = _parse_iso(value)
    return (dt + timedelta(days=days)).isoformat()


def in_timezone(value: Any, tz_name: str) -> Optional[str]:
    """Re-expresses an ISO-8601 instant in a named IANA timezone (e.g.
    'Asia/Ho_Chi_Minh', 'America/Los_Angeles') -- same instant, new
    wall-clock/offset. Needed before start_of_day/end_of_day whenever
    "day" must mean a calendar day in a specific timezone (e.g. an ad
    account's reporting timezone) rather than UTC's."""
    if value is None:
        return None
    dt = _parse_iso(value)
    return dt.astimezone(ZoneInfo(tz_name)).isoformat()


def start_of_day(value: Any) -> Optional[str]:
    """ISO-8601 string -> that same calendar day's 00:00:00, same
    timezone/offset as the input. Chain after in_timezone() if the day
    boundary must be computed in a specific timezone."""
    if value is None:
        return None
    dt = _parse_iso(value)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def end_of_day(value: Any) -> Optional[str]:
    """ISO-8601 string -> that same calendar day's 23:59:59, same
    timezone/offset as the input -- the Meta Marketing API convention
    for a time_range.until boundary (inclusive end of day, not the
    start of the next day)."""
    if value is None:
        return None
    dt = _parse_iso(value)
    return dt.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()


def to_date_str(value: Any, fmt: str = "%Y-%m-%d") -> Optional[str]:
    """ISO-8601 string -> plain date string (default YYYY-MM-DD, the
    shape most reporting-API date-range params want -- Google Ads'
    date_range.start_date/end_date, Meta's time_range.since/until,
    GA4's explicit dateRanges -- as opposed to a full ISO datetime)."""
    if value is None:
        return None
    dt = _parse_iso(value)
    return dt.strftime(fmt)


EXPORTS = {
    "iso_to_epoch_ms": iso_to_epoch_ms,
    "shift_days": shift_days,
    "in_timezone": in_timezone,
    "start_of_day": start_of_day,
    "end_of_day": end_of_day,
    "to_date_str": to_date_str,
}

