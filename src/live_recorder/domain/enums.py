from enum import StrEnum


class Platform(StrEnum):
    TIKTOK = "tiktok"
    TWITCASTING = "twitcasting"


class LiveStatus(StrEnum):
    OFFLINE = "OFFLINE"
    LIVE = "LIVE"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class SessionStatus(StrEnum):
    LIVE = "LIVE"
    ENDING = "ENDING"
    ENDED = "ENDED"


class RecordingStatus(StrEnum):
    PENDING = "PENDING"
    STARTING = "STARTING"
    RECORDING = "RECORDING"
    RECONNECTING = "RECONNECTING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CommentJobStatus(StrEnum):
    PENDING = "PENDING"
    CONNECTING = "CONNECTING"
    COLLECTING = "COLLECTING"
    RECONNECTING = "RECONNECTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class JobKind(StrEnum):
    RECORDING = "recording"
    COMMENTS = "comments"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    INVALID = "invalid"
    PARTIAL = "partial"


RECORDING_ACTIVE = frozenset(
    {
        RecordingStatus.PENDING,
        RecordingStatus.STARTING,
        RecordingStatus.RECORDING,
        RecordingStatus.RECONNECTING,
        RecordingStatus.FINALIZING,
    }
)

COMMENT_ACTIVE = frozenset(
    {
        CommentJobStatus.PENDING,
        CommentJobStatus.CONNECTING,
        CommentJobStatus.COLLECTING,
        CommentJobStatus.RECONNECTING,
    }
)

RECORDING_TRANSITIONS: dict[RecordingStatus, frozenset[RecordingStatus]] = {
    RecordingStatus.PENDING: frozenset(
        {RecordingStatus.STARTING, RecordingStatus.CANCELLED, RecordingStatus.FAILED}
    ),
    RecordingStatus.STARTING: frozenset(
        {
            RecordingStatus.RECORDING,
            RecordingStatus.RECONNECTING,
            RecordingStatus.FAILED,
            RecordingStatus.CANCELLED,
            RecordingStatus.FINALIZING,
        }
    ),
    RecordingStatus.RECORDING: frozenset(
        {
            RecordingStatus.RECONNECTING,
            RecordingStatus.FINALIZING,
            RecordingStatus.FAILED,
            RecordingStatus.CANCELLED,
        }
    ),
    RecordingStatus.RECONNECTING: frozenset(
        {
            RecordingStatus.STARTING,
            RecordingStatus.RECORDING,
            RecordingStatus.FINALIZING,
            RecordingStatus.FAILED,
            RecordingStatus.CANCELLED,
        }
    ),
    RecordingStatus.FINALIZING: frozenset(
        {RecordingStatus.COMPLETED, RecordingStatus.PARTIAL, RecordingStatus.FAILED}
    ),
    RecordingStatus.COMPLETED: frozenset(),
    RecordingStatus.PARTIAL: frozenset(),
    RecordingStatus.FAILED: frozenset(),
    RecordingStatus.CANCELLED: frozenset(),
}

COMMENT_TRANSITIONS: dict[CommentJobStatus, frozenset[CommentJobStatus]] = {
    CommentJobStatus.PENDING: frozenset(
        {
            CommentJobStatus.CONNECTING,
            CommentJobStatus.DISABLED,
            CommentJobStatus.FAILED,
            CommentJobStatus.COMPLETED,
            CommentJobStatus.PARTIAL,
        }
    ),
    CommentJobStatus.CONNECTING: frozenset(
        {
            CommentJobStatus.COLLECTING,
            CommentJobStatus.RECONNECTING,
            CommentJobStatus.FAILED,
            CommentJobStatus.COMPLETED,
            CommentJobStatus.PARTIAL,
        }
    ),
    CommentJobStatus.COLLECTING: frozenset(
        {
            CommentJobStatus.RECONNECTING,
            CommentJobStatus.COMPLETED,
            CommentJobStatus.PARTIAL,
            CommentJobStatus.FAILED,
        }
    ),
    CommentJobStatus.RECONNECTING: frozenset(
        {
            CommentJobStatus.CONNECTING,
            CommentJobStatus.COLLECTING,
            CommentJobStatus.PARTIAL,
            CommentJobStatus.FAILED,
            CommentJobStatus.COMPLETED,
        }
    ),
    CommentJobStatus.COMPLETED: frozenset(),
    CommentJobStatus.PARTIAL: frozenset(),
    CommentJobStatus.FAILED: frozenset(),
    CommentJobStatus.DISABLED: frozenset(),
}


class TransitionError(ValueError):
    pass


def transition_recording(current: RecordingStatus, new: RecordingStatus) -> RecordingStatus:
    if new == current:
        return current
    if new not in RECORDING_TRANSITIONS[current]:
        raise TransitionError(f"recording {current} -> {new} is not allowed")
    return new


def transition_comments(current: CommentJobStatus, new: CommentJobStatus) -> CommentJobStatus:
    if new == current:
        return current
    if new not in COMMENT_TRANSITIONS[current]:
        raise TransitionError(f"comments {current} -> {new} is not allowed")
    return new


def recording_terminal_path(current: RecordingStatus, target: RecordingStatus) -> list[RecordingStatus]:
    if current == target or current not in RECORDING_ACTIVE:
        return []
    if target in RECORDING_TRANSITIONS[current]:
        return [target]
    middle = RecordingStatus.FINALIZING
    if middle in RECORDING_TRANSITIONS[current] and target in RECORDING_TRANSITIONS[middle]:
        return [middle, target]
    if RecordingStatus.FAILED in RECORDING_TRANSITIONS[current]:
        return [RecordingStatus.FAILED]
    return []
