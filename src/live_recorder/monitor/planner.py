from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from live_recorder.domain.enums import LiveStatus
from live_recorder.platforms.types import LiveObservation


@dataclass(frozen=True)
class Plan:
    live_status: str
    session: str
    start_recording: bool
    start_comments: bool
    stop_recording: bool
    stop_comments: bool
    note: str | None = None


def plan_observation(
    *,
    monitoring_enabled: bool,
    recording_enabled: bool,
    comments_enabled: bool,
    comments_global: bool,
    observation: LiveObservation,
    has_open_session: bool,
    open_stream_id: str | None,
    recording_active: bool,
    comments_active: bool,
    recording_slots_left: int,
    disk_ok: bool,
    recording_open: bool = False,
    comments_open: bool = False,
) -> Plan:
    if not monitoring_enabled:
        return Plan(
            live_status=LiveStatus.DISABLED,
            session="end" if has_open_session else "none",
            start_recording=False,
            start_comments=False,
            stop_recording=has_open_session,
            stop_comments=has_open_session,
        )
    if observation.status in {LiveStatus.ERROR, LiveStatus.UNKNOWN}:
        # 通信の失敗では配信を終わらせない。止めたスイッチだけは止める。
        return Plan(
            live_status=observation.status.value,
            session="keep" if has_open_session else "none",
            start_recording=False,
            start_comments=False,
            stop_recording=bool(recording_open and not recording_enabled),
            stop_comments=bool(comments_open and (not comments_enabled or not comments_global)),
            note=observation.error,
        )
    if observation.status == LiveStatus.OFFLINE:
        return Plan(
            live_status=LiveStatus.OFFLINE,
            session="end" if has_open_session else "none",
            start_recording=False,
            start_comments=False,
            stop_recording=has_open_session,
            stop_comments=has_open_session,
        )
    replace = bool(
        has_open_session
        and observation.platform_stream_id
        and open_stream_id
        and observation.platform_stream_id != open_stream_id
    )
    session = "replace" if replace else ("keep" if has_open_session else "open")
    slots = recording_slots_left
    if replace and recording_active:
        slots += 1
    blocked = observation.restricted or not disk_ok or slots <= 0
    start_recording = recording_enabled and not blocked and (replace or not recording_active)
    start_comments = (
        comments_enabled
        and comments_global
        and not observation.restricted
        and (replace or not comments_active)
    )
    note = None
    if observation.restricted:
        note = "restricted"
    elif not disk_ok:
        note = "disk_low"
    elif recording_enabled and slots <= 0 and not recording_active:
        note = "recording_limit"
    stop_recording = (replace and recording_active) or (not recording_enabled and recording_open)
    stop_comments = (replace and comments_active) or (
        (not comments_enabled or not comments_global) and comments_open
    )
    return Plan(
        live_status=LiveStatus.LIVE,
        session=session,
        start_recording=start_recording,
        start_comments=start_comments,
        stop_recording=stop_recording,
        stop_comments=stop_comments,
        note=note,
    )


def effective_interval(configured: int, twitcasting_count: int) -> tuple[int, str | None]:
    interval = max(30, configured)
    if twitcasting_count <= 0:
        return interval, None
    # ユーザー確認と current_live の2リクエスト。予算は毎分50。
    minimum = math.ceil(twitcasting_count * 2 / 50 * 60)
    minimum = max(30, minimum)
    if interval < minimum:
        return minimum, f"Twitcasting のレート制限のため間隔を{minimum}秒に広げました"
    return interval, None


def backoff_seconds(attempt: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return delay + delay * 0.2 * random.random()


def recovery_action(*, still_live: bool | None, has_media: bool) -> str:
    if still_live is True:
        return "resume"
    if still_live is False:
        return "partial" if has_media else "failed"
    return "wait"


def restart_gap_start(
    segment_started_at: datetime | None, duration_ms: int | None, now: datetime
) -> datetime:
    """再起動の欠損は、最後に撮れた時刻から始める。長さが無いときは再起動時刻にする。"""
    if segment_started_at is not None and duration_ms:
        start = segment_started_at + timedelta(milliseconds=duration_ms)
        if start > now:
            return now
        return start
    return now
