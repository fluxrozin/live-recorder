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


_SEGMENT_TAIL = re.compile(r"(_seg\d+(?:_\d+)?)$")
_NAME_MAX = 255


def fit_export_name(stem: str, suffix: str, *, fallback: str) -> str:
    """255 バイトに収める。セグメント番号は末尾に残す。"""
    room = _NAME_MAX - len(suffix.encode("utf-8"))
    raw = stem.encode("utf-8")
    if len(raw) <= room:
        return f"{stem}{suffix}"
    match = _SEGMENT_TAIL.search(stem)
    tail = match.group(1).encode("utf-8") if match else b""
    if len(tail) >= room:
        trimmed = raw[:room].decode("utf-8", errors="ignore").rstrip("._") or fallback
        return f"{trimmed}{suffix}"
    head = raw[: room - len(tail)].decode("utf-8", errors="ignore").rstrip("._")
    if not head:
        head = fallback
    return f"{head}{tail.decode('utf-8')}{suffix}"


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
    while _stem_taken(candidate):
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
        if counter > 1000 or not _within_name_limit(candidate):
            raise OSError(f"空きの録画名がありません: {stem}{suffix}")
    return assert_under(root, candidate)


_UNTITLED_FILE = re.compile(
    r"^(?P<head>.+_\d{8}_\d{6}_)無題_seg(?P<num>\d+)(?:_\d+)?(?P<suffix>\.[A-Za-z0-9]+)$"
)


def untitled_replacement(path: Path, title: str | None) -> Path | None:
    """無題で確定したファイル名を、配信タイトルの名前に組み直す。対象外なら None。"""
    if not title or not title.strip():
        return None
    match = _UNTITLED_FILE.match(path.name)
    if match is None:
        return None
    head = match.group("head")
    suffix = match.group("suffix")
    segment = f"seg{int(match.group('num')):02d}"
    reserved = len(suffix.encode("utf-8")) + len("_99") + len(f"_{segment}".encode("utf-8"))
    room = 255 - reserved - len(head.encode("utf-8"))
    if room < 1:
        return None
    label = file_label(title, fallback="無題", max_bytes=room)
    if label == "無題":
        return None
    stem = f"{head}{label}_{segment}"
    candidate = path.with_name(f"{stem}{suffix}")
    counter = 2
    while candidate != path and _stem_taken(candidate):
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if len(candidate.name.encode("utf-8")) > 255 or counter > 1000:
            return None
        counter += 1
    if candidate == path:
        return None
    return candidate


def _within_name_limit(path: Path) -> bool:
    return len(path.name.encode("utf-8")) <= _NAME_MAX


def _present(path: Path) -> bool:
    """名前がファイルシステムに収まるときだけ存在を調べる。長すぎる名前は無いものとして扱う。"""
    if not _within_name_limit(path):
        return False
    try:
        return path.exists()
    except OSError:
        return False


def _stem_taken(candidate: Path) -> bool:
    """同じ幹の TS、確定済みの MP4、詰め直し途中のファイルがある名前は使わない。"""
    if not _within_name_limit(candidate):
        return True
    if _present(candidate):
        return True
    for suffix in (".ts", ".mp4", ".mp4.partial"):
        other = candidate.with_suffix(suffix)
        if other != candidate and _present(other):
            return True
    return False


def resolve_stored(root: Path, relative: str) -> Path:
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise PathEscape("不正な保存パスです")
    path = (root / relative).resolve()
    return assert_under(root, path)
