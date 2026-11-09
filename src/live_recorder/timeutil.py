from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def format_local(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "—"
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Tokyo")
    return ensure_aware(value).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")


def format_offset(ms: int | None) -> str:
    if ms is None:
        return "--:--:--"
    sign = "-" if ms < 0 else ""
    total = abs(int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_platform_time(value: object) -> datetime | None:
    """数値の桁数から秒・ミリ秒・マイクロ秒を判別する。値が無いときは None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10**14:
        seconds = number / 1_000_000
    elif number > 10**11:
        seconds = number / 1_000
    else:
        seconds = float(number)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def start_of_local_day(tz_name: str, now: datetime | None = None) -> datetime:
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Tokyo")
    current = ensure_aware(now or utcnow()).astimezone(zone)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)
