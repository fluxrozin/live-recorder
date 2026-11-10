from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable

from live_recorder.domain.enums import LiveStatus
from live_recorder.platforms.normalize import normalize_tiktok
from live_recorder.platforms.types import CommentDraft, LiveObservation
from live_recorder.timeutil import parse_platform_time, utcnow

log = logging.getLogger(__name__)

ROOM_URL = "https://www.tiktok.com/api-live/user/room/"
# 5人を同時に叩くと空の 403 が返る。1人ずつ間を空ける。
TIKTOK_CHECK_GAP_SECONDS = 1.5
# 拒否されたあと同じ出口へすぐ戻すと、また全員が 403 になる。
TIKTOK_BLOCK_SECONDS = 300


async def lookup_room(user_id: str, *, timeout: float = 5) -> dict[str, Any] | None:
    """録画ファイル名に使うタイトルのため、配信開始時だけルーム情報を取る。"""
    from TikTokLive import TikTokLiveClient

    normalized = normalize_tiktok(user_id)
    client = TikTokLiveClient(unique_id=normalized)

    async def _fetch() -> dict[str, Any] | None:
        try:
            await client.start(fetch_room_info=True)
            for _ in range(20):
                info = getattr(client, "room_info", None)
                if isinstance(info, dict) and (info.get("title") or info.get("id") or info.get("room_id")):
                    return info
                await asyncio.sleep(0.25)
            info = getattr(client, "room_info", None)
            return info if isinstance(info, dict) else None
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.info("tiktok room lookup disconnect failed")

    try:
        return await asyncio.wait_for(_fetch(), timeout)
    except Exception as exc:
        log.info("tiktok room lookup failed for %s: %s", normalized, type(exc).__name__)
        return None


def pauses_tiktok_polling(error: str | None) -> bool:
    """空応答や出口の拒否は配信者個人の問題ではない。"""
    if error == "invalid_json":
        return True
    if not error or not error.startswith("http_"):
        return False
    raw = error.removeprefix("http_")
    if not raw.isdigit():
        return False
    code = int(raw)
    return code in {403, 429} or code >= 500


def _room_observation(body: Any) -> LiveObservation:
    if isinstance(body, dict) and body.get("message") == "user_not_found":
        return LiveObservation(status=LiveStatus.ERROR, error="user_not_found")
    data = body.get("data") if isinstance(body, dict) else None
    room = data.get("liveRoom") if isinstance(data, dict) else None
    status = room.get("status") if isinstance(room, dict) else None
    try:
        code = int(status)
    except (TypeError, ValueError):
        return LiveObservation(status=LiveStatus.ERROR, error="invalid_response")
    if code != 4:
        return LiveObservation(status=LiveStatus.LIVE)
    return LiveObservation(status=LiveStatus.OFFLINE)


async def check_tiktok(user_id: str, *, client: Any | None = None, timeout: float = 30) -> LiveObservation:
    """配信有無。is_live() は空の 403 を JSONDecodeError に変えてしまうので、ステータスを先に見る。"""
    normalized = normalize_tiktok(user_id)
    if client is None:
        from TikTokLive import TikTokLiveClient

        client = TikTokLiveClient(unique_id=normalized)
    try:
        response = await asyncio.wait_for(
            client.web.get(
                url=ROOM_URL,
                extra_params={"uniqueId": normalized, "sourceType": 54},
            ),
            timeout,
        )
    except TimeoutError:
        return LiveObservation(status=LiveStatus.ERROR, error="is_live timed out")
    except Exception as exc:
        log.info("tiktok check failed for %s: %s", normalized, type(exc).__name__)
        return LiveObservation(status=LiveStatus.ERROR, error=type(exc).__name__)
    if response.status_code != 200:
        log.info("tiktok check failed for %s: http_%s", normalized, response.status_code)
        return LiveObservation(status=LiveStatus.ERROR, error=f"http_{response.status_code}")
    if not response.content:
        log.info("tiktok check failed for %s: empty body", normalized)
        return LiveObservation(status=LiveStatus.ERROR, error="invalid_json")
    try:
        body = response.json()
    except ValueError:
        log.info("tiktok check failed for %s: invalid json", normalized)
        return LiveObservation(status=LiveStatus.ERROR, error="invalid_json")
    return _room_observation(body)


class TikTokChecks:
    """TikTok の配信確認を1本に並べ、拒否されたら全員分を止める。"""

    def __init__(
        self,
        *,
        gap: float = TIKTOK_CHECK_GAP_SECONDS,
        block_seconds: float = TIKTOK_BLOCK_SECONDS,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.gap = gap
        self.block_seconds = block_seconds
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._clients: dict[str, Any] = {}
        self._not_before = 0.0
        self._blocked_until = 0.0

    def block_remaining(self) -> float:
        return max(0.0, self._blocked_until - time.monotonic())

    def _client(self, user_id: str) -> Any:
        client = self._clients.get(user_id)
        if client is None:
            if self._client_factory is not None:
                client = self._client_factory(user_id)
            else:
                from TikTokLive import TikTokLiveClient

                client = TikTokLiveClient(unique_id=user_id)
            self._clients[user_id] = client
        return client

    async def check(self, user_id: str) -> LiveObservation:
        async with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                return LiveObservation(status=LiveStatus.ERROR, error="http_403")
            wait = self._not_before - now
            if wait > 0:
                await asyncio.sleep(wait)
            observation = await check_tiktok(user_id, client=self._client(user_id))
            self._not_before = time.monotonic() + self.gap
            if pauses_tiktok_polling(observation.error):
                self._blocked_until = time.monotonic() + self.block_seconds
            return observation


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
