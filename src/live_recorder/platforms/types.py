from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from live_recorder.domain.enums import LiveStatus


@dataclass(frozen=True)
class LiveObservation:
    status: LiveStatus
    platform_stream_id: str | None = None
    title: str | None = None
    platform_started_at: datetime | None = None
    error: str | None = None
    restricted: bool = False
    hls_url: str | None = None


@dataclass(frozen=True)
class CommentDraft:
    platform_comment_id: str | None
    platform_user_id: str | None
    nickname: str | None
    message: str
    posted_at: datetime | None
    received_at: datetime
