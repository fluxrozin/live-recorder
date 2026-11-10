from __future__ import annotations

import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_TITLE_DROPPED = re.compile(r"[\x00-\x1f\x7f\\/:*?\"<>|]+")


class PathEscape(ValueError):
    pass


def safe_token(value: str, *, fallback: str = "unknown") -> str:
    cleaned = _UNSAFE.sub("_", value).strip("._")
    cleaned = cleaned[:80]
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned


def file_label(value: str | None, *, fallback: str, max_bytes: int) -> str:
    """ファイル名に入れる表示名。区切り文字と制御文字を除き、バイト長で切る。"""
    text = _TITLE_DROPPED.sub("", value or "")
    text = re.sub(r"\s+", "_", text).strip("._")
    if not text or text in {".", ".."}:
        text = fallback
    limit = max(1, max_bytes)
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        text = encoded[:limit].decode("utf-8", errors="ignore").rstrip("._")
    if not text or text in {".", ".."}:
        text = fallback.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip("._")
    return text or "title"


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
    title: str | None,
    segment_number: int,
    suffix: str = ".ts",
) -> Path:
    directory = root / safe_token(platform) / safe_token(user_id) / safe_token(day, fallback="day")
    directory.mkdir(parents=True, exist_ok=True)
    platform_token = safe_token(platform)
    user_token = safe_token(user_id)
    stamp = safe_token(when_stamp, fallback="time")
    segment = f"seg{segment_number:02d}"
    prefix = f"{platform_token}_@{user_token}_{stamp}_"
    # ファイル名は 255 バイトまで。拡張子と重複時の _99 を残す。
    reserved = len(suffix.encode("utf-8")) + len("_99") + len(f"_{segment}".encode("utf-8"))
    room = 255 - reserved - len(prefix.encode("utf-8"))
    label = file_label(title, fallback="無題", max_bytes=room)
    stem = f"{prefix}{label}_{segment}"
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
