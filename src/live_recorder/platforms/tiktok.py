from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from live_recorder.domain.enums import LiveStatus
from live_recorder.platforms.normalize import normalize_tiktok
from live_recorder.platforms.types import CommentDraft, LiveObservation
from live_recorder.timeutil import parse_platform_time, utcnow

log = logging.getLogger(__name__)


async def check_tiktok(user_id: str, *, client: Any | None = None, timeout: float = 30) -> LiveObservation:
    normalized = normalize_tiktok(user_id)
    if client is None:
        from TikTokLive import TikTokLiveClient

        client = TikTokLiveClient(unique_id=normalized)
    try:
        live = await asyncio.wait_for(client.is_live(), timeout)
    except TimeoutError:
        return LiveObservation(status=LiveStatus.ERROR, error="is_live timed out")
    except Exception as exc:
        log.info("tiktok is_live failed for %s: %s", normalized, type(exc).__name__)
        return LiveObservation(status=LiveStatus.ERROR, error=type(exc).__name__)
    if live:
        return LiveObservation(status=LiveStatus.LIVE)
    return LiveObservation(status=LiveStatus.OFFLINE)


def comment_from_event(event: Any, received_at: datetime | None = None) -> CommentDraft | None:
    message = getattr(event, "content", None)
    if message is None:
        message = getattr(event, "comment", None)
    if not isinstance(message, str):
        return None
    user = getattr(event, "user", None)
    common = getattr(event, "common", None)
    platform_user_id = None
    nickname = None
    if user is not None:
        unique = getattr(user, "unique_id", None)
        numeric = getattr(user, "id", None)
        if unique:
            platform_user_id = str(unique)
        elif numeric:
            platform_user_id = str(numeric)
        nick = getattr(user, "nickname", None)
        if nick:
            nickname = str(nick)
    msg_id = getattr(common, "msg_id", None) if common is not None else None
    create_time = getattr(common, "create_time", None) if common is not None else None
    comment_id = str(msg_id) if msg_id else None
    return CommentDraft(
        platform_comment_id=comment_id,
        platform_user_id=platform_user_id,
        nickname=nickname,
        message=message,
        posted_at=parse_platform_time(create_time),
        received_at=received_at or utcnow(),
    )


def room_fields(room_info: dict[str, Any] | None) -> tuple[str | None, str | None, datetime | None]:
    if not isinstance(room_info, dict):
        return None, None, None
    raw_id = room_info.get("id") or room_info.get("room_id")
    stream_id = str(raw_id) if raw_id else None
    title = room_info.get("title")
    title_text = str(title) if isinstance(title, str) and title else None
    started = parse_platform_time(room_info.get("create_time"))
    return stream_id, title_text, started
