from __future__ import annotations

import asyncio
import base64
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from live_recorder.domain.enums import LiveStatus
from live_recorder.platforms.normalize import normalize_twitcasting
from live_recorder.platforms.types import CommentDraft, LiveObservation
from live_recorder.timeutil import utcnow

log = logging.getLogger(__name__)

API_BASE = "https://apiv2.twitcasting.tv"
STREAM_SERVER = "https://twitcasting.tv/streamserver.php"
TWITCASTING_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TWITCASTING_REFERER = "https://twitcasting.tv/"
_TC_HLS_BEST = ("high", "medium", "low")
_TC_HLS_MEDIUM = ("medium", "low")


class RateLimiter:
    def __init__(self, limit: int, period_seconds: float) -> None:
        self.limit = limit
        self.period = period_seconds
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                while self._times and now - self._times[0] >= self.period:
                    self._times.popleft()
                if len(self._times) < self.limit:
                    self._times.append(now)
                    return
                wait = self.period - (now - self._times[0])
            await asyncio.sleep(max(0.05, wait))


def validate_hls_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return None
    if host != "twitcasting.tv" and not host.endswith(".twitcasting.tv"):
        return None
    return url


def pick_tc_hls_url(streams: dict[str, Any], quality: str) -> str | None:
    """配信プレイヤーと同じ tc-hls から、画質設定に合う HTTPS のプレイリストを選ぶ。"""
    order = _TC_HLS_MEDIUM if quality == "medium" else _TC_HLS_BEST
    for name in order:
        raw = streams.get(name)
        chosen = validate_hls_url(raw if isinstance(raw, str) else None)
        if chosen:
            return chosen
    return None


def comment_from_api(item: dict[str, Any], received_at: datetime | None = None) -> CommentDraft:
    author = item.get("from_user") if isinstance(item.get("from_user"), dict) else {}
    screen = author.get("screen_id")
    numeric = author.get("id")
    user_id = str(screen) if screen else (str(numeric) if numeric is not None else None)
    name = author.get("name")
    created = item.get("created")
    posted = None
    if isinstance(created, int) and created > 0:
        posted = datetime.fromtimestamp(created, tz=timezone.utc)
    raw_id = item.get("id")
    message = item.get("message")
    return CommentDraft(
        platform_comment_id=str(raw_id) if raw_id is not None else None,
        platform_user_id=user_id,
        nickname=str(name) if name else None,
        message=message if isinstance(message, str) else "",
        posted_at=posted,
        received_at=received_at or utcnow(),
    )


class TwitCastingClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        limiter: RateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.limiter = limiter or RateLimiter(50, 60)
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.access_token or (self.client_id and self.client_secret))

    def headers(self) -> dict[str, str]:
        headers = {"X-Api-Version": "2.0", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        elif self.client_id and self.client_secret:
            token = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        else:
            raise RuntimeError("twitcasting credentials are not configured")
        return headers

    async def __aenter__(self) -> TwitCastingClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20)
            self._owns_client = True
        return self._client

    async def _request(self, method: str, path: str, params: dict[str, object] | None = None) -> httpx.Response:
        await self.limiter.acquire()
        return await self._ensure_client().request(method, f"{API_BASE}{path}", headers=self.headers(), params=params)

    async def _player_get(self, url: str, *, params: dict[str, str] | None = None, accept: str) -> httpx.Response:
        headers = {
            "User-Agent": TWITCASTING_USER_AGENT,
            "Referer": TWITCASTING_REFERER,
            "Accept": accept,
        }
        return await self._ensure_client().get(url, params=params, headers=headers, follow_redirects=True)

    async def open_live_hls(self, user_id: str, quality: str) -> tuple[str | None, str | None]:
        """開けた tc-hls の URL を返す。失敗時は URL なしと理由。合言葉と会員配信は拒否する。"""
        observation = await self.current_live(user_id)
        if observation.restricted:
            return None, "access_denied"
        normalized = normalize_twitcasting(user_id)
        try:
            response = await self._player_get(
                STREAM_SERVER,
                params={"target": normalized, "mode": "client", "player": "pc_web"},
                accept="application/json",
            )
        except httpx.HTTPError as exc:
            log.info("twitcasting streamserver failed: %s", type(exc).__name__)
            return None, type(exc).__name__
        if response.status_code != 200:
            return None, f"streamserver http_{response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return None, "invalid_json"
        block = body.get("tc-hls") if isinstance(body, dict) else None
        streams = block.get("streams") if isinstance(block, dict) else None
        chosen = pick_tc_hls_url(streams if isinstance(streams, dict) else {}, quality)
        if not chosen:
            return None, "tc-hls missing"
        try:
            playlist = await self._player_get(chosen, accept="*/*")
        except httpx.HTTPError as exc:
            log.info("twitcasting playlist failed: %s", type(exc).__name__)
            return None, type(exc).__name__
        if playlist.status_code != 200:
            return None, f"playlist http_{playlist.status_code}"
        opened = validate_hls_url(str(playlist.url))
        if opened is None:
            return None, "playlist host rejected"
        text = playlist.text[:200].lstrip("\ufeff \t\r\n")
        if not text.startswith("#EXTM3U"):
            return None, "playlist is not hls"
        return opened, None

    async def user_exists(self, user_id: str) -> LiveObservation | None:
        normalized = normalize_twitcasting(user_id)
        response = await self._request("GET", f"/users/{quote(normalized, safe='')}")
        if response.status_code == 404:
            return LiveObservation(status=LiveStatus.ERROR, error="user_not_found")
        if response.status_code in {401, 403}:
            return LiveObservation(status=LiveStatus.ERROR, error=f"auth_{response.status_code}")
        if response.status_code != 200:
            return LiveObservation(status=LiveStatus.ERROR, error=f"http_{response.status_code}")
        return None

    async def current_live(self, user_id: str) -> LiveObservation:
        if not self.configured:
            return LiveObservation(status=LiveStatus.ERROR, error="credentials_missing")
        try:
            missing = await self.user_exists(user_id)
            if missing is not None:
                return missing
            normalized = normalize_twitcasting(user_id)
            response = await self._request("GET", f"/users/{quote(normalized, safe='')}/current_live")
        except httpx.HTTPError as exc:
            log.info("twitcasting request failed: %s", type(exc).__name__)
            return LiveObservation(status=LiveStatus.ERROR, error=type(exc).__name__)
        if response.status_code == 404:
            return LiveObservation(status=LiveStatus.OFFLINE)
        if response.status_code in {401, 403}:
            return LiveObservation(status=LiveStatus.ERROR, error=f"auth_{response.status_code}")
        if response.status_code == 429:
            return LiveObservation(status=LiveStatus.ERROR, error="rate_limited")
        if response.status_code != 200:
            return LiveObservation(status=LiveStatus.ERROR, error=f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            return LiveObservation(status=LiveStatus.ERROR, error="invalid_json")
        movie = body.get("movie") if isinstance(body, dict) else None
        if not isinstance(movie, dict) or not movie.get("is_live"):
            return LiveObservation(status=LiveStatus.OFFLINE)
        restricted = bool(movie.get("is_protected")) or bool(movie.get("is_membership"))
        started = movie.get("created")
        started_at = None
        if isinstance(started, int) and started > 0:
            started_at = datetime.fromtimestamp(started, tz=timezone.utc)
        title = movie.get("title")
        raw_id = movie.get("id")
        hls = None if restricted else validate_hls_url(movie.get("hls_url") if isinstance(movie.get("hls_url"), str) else None)
        return LiveObservation(
            status=LiveStatus.LIVE,
            platform_stream_id=str(raw_id) if raw_id is not None else None,
            title=str(title) if isinstance(title, str) else None,
            platform_started_at=started_at,
            restricted=restricted,
            hls_url=hls,
        )

    async def fetch_comments(
        self, movie_id: str, *, slice_id: str | None = None, limit: int = 50
    ) -> tuple[list[CommentDraft], int | None, str | None]:
        if not movie_id.isdigit():
            return [], None, "invalid_movie_id"
        params: dict[str, object] = {"limit": max(1, min(limit, 50))}
        if slice_id:
            params["slice_id"] = slice_id
        try:
            response = await self._request("GET", f"/movies/{movie_id}/comments", params=params)
        except httpx.HTTPError as exc:
            return [], None, type(exc).__name__
        if response.status_code == 404:
            return [], None, "not_found"
        if response.status_code in {401, 403}:
            return [], None, f"auth_{response.status_code}"
        if response.status_code == 429:
            return [], None, "rate_limited"
        if response.status_code != 200:
            return [], None, f"http_{response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return [], None, "invalid_json"
        raw_comments = body.get("comments") if isinstance(body, dict) else None
        all_count = body.get("all_count") if isinstance(body, dict) else None
        drafts = [comment_from_api(item) for item in raw_comments or [] if isinstance(item, dict)]
        total = int(all_count) if isinstance(all_count, int) else None
        return drafts, total, None
