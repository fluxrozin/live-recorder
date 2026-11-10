from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from live_recorder.db.models import SystemSetting
from live_recorder.timeutil import utcnow

ENGINE_NAMES = ("streamlink", "yt-dlp", "tiktoklive")
QUALITIES = ("best", "high", "medium")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SPEECH_MODELS = ("large-v3", "large-v3-turbo")
_SPEECH_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


@dataclass
class RuntimeSettings:
    monitor_interval_seconds: int = 60
    max_concurrent_recordings: int = 3
    recording_quality: str = "best"
    min_free_bytes: int = 5 * 1024**3
    retention_days: int = 0
    auto_delete_enabled: bool = False
    tiktok_engine_order: list[str] | None = None
    comments_globally_enabled: bool = True
    log_level: str = "INFO"
    display_timezone: str = "Asia/Tokyo"
    comment_retention_days: int = 0
    auto_export_on_complete: bool = False
    reconnect_max_attempts: int = 5
    comment_poll_seconds: int = 10
    speech_subtitles_enabled: bool = True
    speech_model: str = "large-v3"
    speech_language: str = "ja"

    def __post_init__(self) -> None:
        if not self.tiktok_engine_order:
            self.tiktok_engine_order = list(ENGINE_NAMES)

    def as_json(self) -> dict[str, object]:
        data = asdict(self)
        return data


class SettingsValidationError(ValueError):
    pass


def validate_runtime(raw: dict[str, object]) -> RuntimeSettings:
    current = RuntimeSettings()
    data = current.as_json()
    data.update(raw)
    try:
        interval = int(data["monitor_interval_seconds"])  # type: ignore[arg-type]
        concurrent = int(data["max_concurrent_recordings"])  # type: ignore[arg-type]
        min_free = int(data["min_free_bytes"])  # type: ignore[arg-type]
        retention = int(data["retention_days"])  # type: ignore[arg-type]
        comment_retention = int(data["comment_retention_days"])  # type: ignore[arg-type]
        reconnects = int(data["reconnect_max_attempts"])  # type: ignore[arg-type]
        poll = int(data["comment_poll_seconds"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError("数値の設定が不正です") from exc
    if interval < 30:
        raise SettingsValidationError("監視間隔は30秒以上にしてください")
    if not 1 <= concurrent <= 20:
        raise SettingsValidationError("同時録画数は1から20です")
    if min_free < 0:
        raise SettingsValidationError("最低空き容量は0以上です")
    if retention < 0 or comment_retention < 0:
        raise SettingsValidationError("保存期限は0以上です")
    if not 0 <= reconnects <= 30:
        raise SettingsValidationError("再接続回数は0から30です")
    if not 5 <= poll <= 120:
        raise SettingsValidationError("コメント取得間隔は5から120秒です")
    quality = str(data["recording_quality"])
    if quality not in QUALITIES:
        raise SettingsValidationError("録画品質が不正です")
    level = str(data["log_level"]).upper()
    if level not in LOG_LEVELS:
        raise SettingsValidationError("ログレベルが不正です")
    tz_name = str(data["display_timezone"])
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise SettingsValidationError("タイムゾーンが不正です") from exc
    order = data["tiktok_engine_order"]
    if not isinstance(order, list) or not order:
        raise SettingsValidationError("録画エンジンの順序が不正です")
    names = [str(item) for item in order]
    if len(names) != len(set(names)) or any(name not in ENGINE_NAMES for name in names):
        raise SettingsValidationError("録画エンジンの順序が不正です")
    speech_model = str(data["speech_model"])
    if speech_model not in SPEECH_MODELS:
        raise SettingsValidationError("字幕モデルが不正です")
    speech_language = str(data["speech_language"]).strip().lower()
    if not _SPEECH_LANGUAGE.fullmatch(speech_language):
        raise SettingsValidationError("字幕の言語が不正です")
    return RuntimeSettings(
        monitor_interval_seconds=interval,
        max_concurrent_recordings=concurrent,
        recording_quality=quality,
        min_free_bytes=min_free,
        retention_days=retention,
        auto_delete_enabled=_as_bool(data["auto_delete_enabled"]),
        tiktok_engine_order=names,
        comments_globally_enabled=_as_bool(data["comments_globally_enabled"]),
        log_level=level,
        display_timezone=tz_name,
        comment_retention_days=comment_retention,
        auto_export_on_complete=_as_bool(data["auto_export_on_complete"]),
        reconnect_max_attempts=reconnects,
        comment_poll_seconds=poll,
        speech_subtitles_enabled=_as_bool(data["speech_subtitles_enabled"]),
        speech_model=speech_model,
        speech_language=speech_language,
    )


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


IMMEDIATE_KEYS = {field.name for field in fields(RuntimeSettings)}


async def load_runtime(session: AsyncSession) -> RuntimeSettings:
    rows = (await session.execute(select(SystemSetting))).scalars().all()
    raw: dict[str, object] = {}
    for row in rows:
        if row.key not in IMMEDIATE_KEYS:
            continue
        raw[row.key] = json.loads(row.value)
    if not raw:
        return RuntimeSettings()
    return validate_runtime(raw)


async def ensure_runtime(session: AsyncSession) -> RuntimeSettings:
    defaults = RuntimeSettings().as_json()
    existing = {
        row.key: row
        for row in (await session.execute(select(SystemSetting))).scalars().all()
    }
    for key, value in defaults.items():
        if key not in existing:
            session.add(SystemSetting(key=key, value=json.dumps(value, ensure_ascii=False), updated_at=utcnow()))
    await session.flush()
    return await load_runtime(session)


async def save_runtime(session: AsyncSession, settings: RuntimeSettings) -> None:
    now = utcnow()
    for key, value in settings.as_json().items():
        row = await session.get(SystemSetting, key)
        payload = json.dumps(value, ensure_ascii=False)
        if row is None:
            session.add(SystemSetting(key=key, value=payload, updated_at=now))
        else:
            row.value = payload
            row.updated_at = now
