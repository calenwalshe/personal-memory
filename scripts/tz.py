"""
tz.py — Shared timezone helpers for the vault pipeline.

All internal storage uses UTC.  Display-facing code calls to_local() to
convert to the user's configured timezone (default: America/Los_Angeles).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def to_local(utc_str: str | None, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Convert a UTC ISO-8601 timestamp string to local time for display.

    Handles common formats: '2026-04-17T00:51:33Z', '2026-04-17T00:51:33.928Z',
    '2026-04-17T00:51:33+00:00'.  Returns the original string if parsing fails.
    """
    if not utc_str:
        return "?"
    try:
        s = utc_str.replace("+00:00", "Z").rstrip("Z")
        # Strip sub-second precision for fromisoformat compatibility
        if "." in s:
            s = s[: s.index(".")]
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).strftime(fmt)
    except (ValueError, TypeError):
        return utc_str


def to_local_short(utc_str: str | None) -> str:
    """Compact display: 'Apr 16 5:32 PM PDT'."""
    return to_local(utc_str, fmt="%b %d %-I:%M %p %Z")


def to_local_date(utc_str: str | None) -> str:
    """Date only: '2026-04-16'."""
    return to_local(utc_str, fmt="%Y-%m-%d")


def now_local(fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Current time in local timezone."""
    return datetime.now(LOCAL_TZ).strftime(fmt)
