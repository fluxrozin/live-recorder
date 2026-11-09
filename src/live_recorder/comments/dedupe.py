from __future__ import annotations

import hashlib
from datetime import datetime


def dedupe_key(
    *,
    platform_comment_id: str | None,
    platform_user_id: str | None,
    message: str,
    posted_at: datetime | None,
    received_at: datetime,
) -> str:
    if platform_comment_id:
        raw = f"id:{platform_comment_id}"
    elif posted_at is not None:
        raw = f"posted:{platform_user_id or ''}|{posted_at.isoformat()}|{message}"
    else:
        second = received_at.replace(microsecond=0).isoformat()
        raw = f"recv:{platform_user_id or ''}|{second}|{message}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
