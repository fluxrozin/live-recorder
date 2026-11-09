from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class PathEscape(ValueError):
    pass


def safe_token(value: str, *, fallback: str = "unknown") -> str:
    cleaned = _UNSAFE.sub("_", value).strip("._")
    cleaned = cleaned[:80]
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned


def assert_under(root: Path, path: Path) -> Path:
    root_resolved = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise PathEscape("recordings root の外です")
    return resolved


def relative_to_root(root: Path, path: Path) -> str:
    resolved = assert_under(root, path)
    return resolved.relative_to(root.resolve()).as_posix()


def build_segment_path(
    *,
    root: Path,
    platform: str,
    user_id: str,
    when_stamp: str,
    day: str,
    stream_id: str | None,
    segment_number: int,
    suffix: str = ".ts",
) -> Path:
    directory = root / safe_token(platform) / safe_token(user_id) / safe_token(day, fallback="day")
    directory.mkdir(parents=True, exist_ok=True)
    stem = "_".join(
        [
            safe_token(platform),
            safe_token(user_id),
            safe_token(stream_id or "nostream"),
            safe_token(when_stamp, fallback="time"),
            f"seg{segment_number:02d}",
        ]
    )
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return assert_under(root, candidate)


def resolve_stored(root: Path, relative: str) -> Path:
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise PathEscape("不正な保存パスです")
    path = (root / relative).resolve()
    return assert_under(root, path)
