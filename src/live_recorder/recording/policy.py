from __future__ import annotations

ACCESS_DENIED = "access_denied"
OFFLINE = "offline"
ENDED = "ended"
STOPPED = "stopped"
INTERRUPTED = "interrupted"

_ACCESS = (
    "the stream is inaccessible",
    "login required",
    "age-restricted",
    "age restricted",
    "video is private",
    "private video",
    "members-only",
    "this live is protected",
    "requested content is not available",
)
_OFFLINE = (
    "not currently live",
    "channel is not currently live",
    "no live stream available",
    "user is offline",
    "useroffline",
)


def classify_output(*, stderr: str, returncode: int | None, stopped: bool) -> str:
    if stopped:
        return STOPPED
    text = stderr.lower()
    if any(marker in text for marker in _ACCESS):
        return ACCESS_DENIED
    if any(marker in text for marker in _OFFLINE):
        return OFFLINE
    if returncode == 0:
        return ENDED
    return INTERRUPTED


def next_action(*, kind: str, bytes_written: int, attempt: int, max_attempts: int, engine_index: int, engine_count: int) -> str:
    """same / switch / stop のいずれかを返す。"""
    if kind in {ACCESS_DENIED, ENDED, STOPPED, OFFLINE}:
        return "stop"
    useful = bytes_written >= 64 * 1024
    if kind == INTERRUPTED and useful:
        if attempt >= max_attempts:
            return "stop"
        return "same"
    if not useful and engine_index + 1 < engine_count:
        return "switch"
    if kind == INTERRUPTED and attempt < max_attempts:
        return "same"
    return "stop"
