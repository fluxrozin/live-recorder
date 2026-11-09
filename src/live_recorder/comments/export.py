from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator
from datetime import datetime

from live_recorder.timeutil import format_local, format_offset

_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA):
        return "'" + text
    return text


def escape_log_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def iter_jsonl(rows: Iterable[dict[str, object]]) -> Iterator[str]:
    for row in rows:
        yield json.dumps(row, ensure_ascii=False, default=_json_default) + "\n"


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def render_csv(rows: Iterable[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["時刻", "投稿者ID", "表示名", "コメント本文", "プラットフォーム", "配信ID", "録画開始からの経過時間"])
    for row in rows:
        writer.writerow(
            [
                csv_safe(row.get("time", "")),
                csv_safe(row.get("user_id", "")),
                csv_safe(row.get("nickname", "")),
                csv_safe(row.get("message", "")),
                csv_safe(row.get("platform", "")),
                csv_safe(row.get("stream_id", "")),
                csv_safe(row.get("recording_offset", "")),
            ]
        )
    return buffer.getvalue()


def render_txt(rows: Iterable[dict[str, object]]) -> str:
    lines: list[str] = []
    for row in rows:
        offset = row.get("recording_offset_ms")
        stamp = format_offset(offset if isinstance(offset, int) else None)
        user = escape_log_text(str(row.get("user_id") or row.get("nickname") or "unknown"))
        message = escape_log_text(str(row.get("message") or ""))
        lines.append(f"[{stamp}] {user}: {message}")
    return "\n".join(lines) + ("\n" if lines else "")


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
        "time": format_local(shown, timezone_name),
        "posted_at": posted_at.isoformat() if posted_at else None,
        "received_at": received_at.isoformat(),
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
