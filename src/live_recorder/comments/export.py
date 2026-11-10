from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from live_recorder.timeutil import format_export_time, format_offset

CUE_DURATION_MS = 5_000
_BLANK_LINES = re.compile(r"\n{2,}")


def _srt_timestamp(ms: int) -> str:
    if ms < 0:
        ms = 0
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _cue_text(row: dict[str, object]) -> str:
    nickname = str(row.get("nickname") or "").strip()
    user_id = str(row.get("user_id") or "").strip()
    author = nickname or user_id or "unknown"
    author = " ".join(author.replace("\r", " ").replace("\n", " ").split()) or "unknown"
    message = str(row.get("message") or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in _BLANK_LINES.sub("\n", message).split("\n")]
    text = "\n".join(line for line in lines if line)
    if text:
        return f"{author}: {text}"
    return f"{author}:"


def render_srt(rows: Iterable[dict[str, object]], *, duration_ms: int = CUE_DURATION_MS) -> str:
    """セグメント先頭からの経過時間で字幕にする。重なっても開始時刻は動かさない。"""
    ordered = sorted(
        rows,
        key=lambda row: row.get("recording_offset_ms") if isinstance(row.get("recording_offset_ms"), int) else 0,
    )
    blocks: list[str] = []
    for index, row in enumerate(ordered, start=1):
        start = row.get("recording_offset_ms")
        start_ms = start if isinstance(start, int) and start > 0 else 0
        end_ms = start_ms + duration_ms
        blocks.append(f"{index}\n{_srt_timestamp(start_ms)} --> {_srt_timestamp(end_ms)}\n{_cue_text(row)}\n")
    return "\n".join(blocks)


def encode_export(rows: Iterable[dict[str, object]]) -> tuple[bytes, str]:
    return render_srt(rows).encode("utf-8"), "application/x-subrip; charset=utf-8"


def export_row(
    *,
    posted_at: datetime | None,
    received_at: datetime,
    timezone_name: str,
    user_id: str | None,
    nickname: str | None,
    message: str,
    platform: str,
    stream_id: str | None,
    recording_offset_ms: int | None,
    stream_offset_ms: int | None,
    platform_comment_id: str | None,
) -> dict[str, object]:
    shown = posted_at or received_at
    return {
        "time": format_export_time(shown, timezone_name) or "",
        "posted_at": format_export_time(posted_at, timezone_name),
        "received_at": format_export_time(received_at, timezone_name),
        "user_id": user_id or "",
        "nickname": nickname or "",
        "message": message,
        "platform": platform,
        "stream_id": stream_id or "",
        "recording_offset": format_offset(recording_offset_ms) if recording_offset_ms is not None else "",
        "recording_offset_ms": recording_offset_ms,
        "stream_offset_ms": stream_offset_ms,
        "platform_comment_id": platform_comment_id,
    }
