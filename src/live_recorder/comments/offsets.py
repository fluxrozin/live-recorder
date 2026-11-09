from __future__ import annotations

from datetime import datetime


def offsets(
    *,
    posted_at: datetime | None,
    received_at: datetime,
    stream_started_at: datetime | None,
    recording_started_at: datetime | None,
) -> tuple[int | None, int | None]:
    anchor = posted_at or received_at
    stream_offset = None
    if stream_started_at is not None:
        stream_offset = int((anchor - stream_started_at).total_seconds() * 1000)
    recording_offset = None
    if recording_started_at is not None:
        recording_offset = int((anchor - recording_started_at).total_seconds() * 1000)
    return stream_offset, recording_offset
