"""テンプレート向けの表示ヘルパー（状態表示・URL 組み立て・時刻表記）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.requests import Request

from live_recorder.domain.labels import (
    COMMENT_STATUS_JA,
    LIVE_STATUS_JA,
    PLATFORM_JA,
    RECORDING_STATUS_JA,
    SESSION_STATUS_JA,
    SPEECH_STATUS_JA,
)
from live_recorder.timeutil import ensure_aware, utcnow

STATIC_DIR = Path(__file__).resolve().parent / "static"

# tone: live / success / info / warning / danger / neutral
_LIVE = {
    "LIVE": ("live", "dot", LIVE_STATUS_JA["LIVE"]),
    "OFFLINE": ("neutral", "circle", LIVE_STATUS_JA["OFFLINE"]),
    "UNKNOWN": ("warning", "circle-help", LIVE_STATUS_JA["UNKNOWN"]),
    "ERROR": ("danger", "circle-x", LIVE_STATUS_JA["ERROR"]),
    "DISABLED": ("neutral", "circle-pause", "監視停止"),
    "CHECKING": ("info", "clock", "確認待ち"),
}
_REC = {
    "PENDING": ("info", "clock"),
    "STARTING": ("info", "loader-circle"),
    "RECORDING": ("success", "dot"),
    "RECONNECTING": ("warning", "refresh-cw"),
    "FINALIZING": ("info", "loader-circle"),
    "COMPLETED": ("success", "circle-check"),
    "PARTIAL": ("warning", "triangle-alert"),
    "FAILED": ("danger", "circle-x"),
    "CANCELLED": ("neutral", "minus"),
    "NONE": ("neutral", "minus"),
}
_COM = {
    "PENDING": ("info", "clock"),
    "CONNECTING": ("info", "loader-circle"),
    "COLLECTING": ("success", "dot"),
    "RECONNECTING": ("warning", "refresh-cw"),
    "COMPLETED": ("success", "circle-check"),
    "PARTIAL": ("warning", "triangle-alert"),
    "FAILED": ("danger", "circle-x"),
    "DISABLED": ("neutral", "circle-pause"),
    "NONE": ("neutral", "minus"),
}
_SESSION = {
    "LIVE": ("live", "dot"),
    "ENDING": ("info", "loader-circle"),
    "ENDED": ("neutral", "circle"),
}
_SEVERITY = {
    "error": ("danger", "circle-x", "エラー"),
    "warning": ("warning", "triangle-alert", "警告"),
    "info": ("info", "info", "情報"),
    "debug": ("neutral", "circle", "デバッグ"),
}
_HEALTH = {
    "ok": ("success", "circle-check", "正常"),
    "warning": ("warning", "triangle-alert", "警告"),
    "error": ("danger", "circle-x", "異常"),
    "unknown": ("neutral", "circle-help", "不明"),
}
_SPEECH = {
    "pending": ("info", "clock"),
    "running": ("info", "loader-circle"),
    "completed": ("success", "circle-check"),
    "failed": ("danger", "circle-x"),
    "skipped": ("neutral", "minus"),
    "NONE": ("neutral", "minus"),
}
_SPEECH_SUMMARY = {
    "running": ("info", "loader-circle", "字幕生成中"),
    "pending": ("info", "clock", "字幕待機"),
    "failed": ("danger", "circle-x", "字幕失敗"),
    "completed": ("success", "circle-check", "字幕完了"),
}
_VALIDATION = {
    "ok": ("success", "circle-check", "検証済み"),
    "pending": ("info", "clock", "検証待ち"),
    "partial": ("warning", "triangle-alert", "一部有効"),
    "invalid": ("danger", "circle-x", "破損の可能性"),
}


def speech_summary(statuses: list[str | None]) -> str | None:
    """録画1件の字幕状態。生成中、待機、失敗、完了の順で一つにまとめる。"""
    active = [status for status in statuses if status not in {None, "skipped"}]
    if not active:
        return None
    if "running" in active:
        return "running"
    if "pending" in active or None in statuses:
        return "pending"
    if "failed" in active:
        return "failed"
    if "completed" in active:
        return "completed"
    return None


def status_info(kind: str, code: object) -> dict[str, object]:
    """状態コードを表示用の tone / icon / label に変換する。未知のコードは中立表示にする。"""
    raw = str(code or "")
    tone, icon, label = "neutral", "circle-help", raw or "—"
    if kind == "live":
        tone, icon, label = _LIVE.get(raw, (tone, icon, label))
    elif kind == "rec":
        key = raw or "NONE"
        if key in _REC:
            tone, icon = _REC[key]
            label = RECORDING_STATUS_JA.get(key, key)
    elif kind == "com":
        key = raw or "NONE"
        if key in _COM:
            tone, icon = _COM[key]
            label = COMMENT_STATUS_JA.get(key, key)
    elif kind == "session":
        if raw in _SESSION:
            tone, icon = _SESSION[raw]
            label = SESSION_STATUS_JA.get(raw, raw)
    elif kind == "severity":
        tone, icon, label = _SEVERITY.get(raw.lower(), (tone, icon, label))
    elif kind == "health":
        tone, icon, label = _HEALTH.get(raw, (tone, icon, label))
    elif kind == "speech":
        key = raw or "NONE"
        if key in _SPEECH:
            tone, icon = _SPEECH[key]
            label = SPEECH_STATUS_JA.get(key, key)
    elif kind == "speech_summary":
        if raw in _SPEECH_SUMMARY:
            tone, icon, label = _SPEECH_SUMMARY[raw]
    elif kind == "validation":
        tone, icon, label = _VALIDATION.get(raw, (tone, icon, label))
    return {"tone": tone, "icon": icon, "label": label, "code": raw, "pulse": icon == "dot"}


def streamer_live_code(row: dict) -> str:
    """一度も確認されていない監視対象は「オフライン」ではなく「確認待ち」として扱う。"""
    status = str(row.get("live_status") or "")
    if status == "OFFLINE" and row.get("monitoring_enabled") and not row.get("last_checked_at"):
        return "CHECKING"
    return status


def url_with(request: Request, drop: list[str] | tuple[str, ...] = (), **updates: object) -> str:
    """現在のクエリを保ったまま一部を差し替えた URL を返す。空の値は出力しない。"""
    removed = set(drop) | set(updates)
    items = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in removed and value != ""
    ]
    items += [(key, str(value)) for key, value in updates.items() if value not in (None, "")]
    query = urlencode(items)
    return f"{request.url.path}?{query}" if query else request.url.path


def export_url(request: Request, fmt: str, **extra: object) -> str:
    items = [("format", fmt)]
    items += [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"page", "format"} and key not in extra and value != ""
    ]
    items += [(key, str(value)) for key, value in extra.items() if value not in (None, "")]
    return "/comments/export?" + urlencode(items)


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Tokyo")


def iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return ensure_aware(value).isoformat()


def short_time(
    value: datetime | None, tz_name: str, now: datetime | None = None, *, seconds: bool = False
) -> str:
    """同年は月日と時刻、それ以外は年月日を表示する。seconds のときは秒まで出す。"""
    if value is None:
        return "—"
    zone = _zone(tz_name)
    local = ensure_aware(value).astimezone(zone)
    current = ensure_aware(now or utcnow()).astimezone(zone)
    clock = "%H:%M:%S" if seconds else "%H:%M"
    if local.year == current.year:
        return local.strftime(f"%m/%d {clock}")
    return local.strftime(f"%Y/%m/%d {clock}")


def clock(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "—"
    return ensure_aware(value).astimezone(_zone(tz_name)).strftime("%H:%M:%S")


def ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "—"
    seconds = int(((now or utcnow()) - ensure_aware(value)).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}秒前"
    if seconds < 3600:
        return f"{seconds // 60}分前"
    if seconds < 86400:
        return f"{seconds // 3600}時間前"
    return f"{seconds // 86400}日前"


def duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def percent(part: object, whole: object) -> int | None:
    try:
        numerator = float(part)  # type: ignore[arg-type]
        denominator = float(whole)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100)


_static_versions: dict[str, str] = {}


def static_url(path: str) -> str:
    """ファイル更新時刻をクエリに付け、デプロイ後に古いキャッシュが使われないようにする。"""
    version = _static_versions.get(path)
    if version is None:
        try:
            version = format(int((STATIC_DIR / path).stat().st_mtime), "x")
        except OSError:
            version = "0"
        _static_versions[path] = version
    return f"/static/{path}?v={version}"


def register(templates, version: str) -> None:
    env = templates.env
    env.globals.update(
        status_info=status_info,
        streamer_live_code=streamer_live_code,
        url_with=url_with,
        export_url=export_url,
        static_url=static_url,
        percent=percent,
        platform_names=PLATFORM_JA,
        app_version=version,
    )
    env.filters.update(
        iso=iso,
        shorttime=short_time,
        clock=clock,
        ago=ago,
        duration=duration,
    )
