from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from live_recorder.timeutil import ensure_aware

COMMENT_EXPORT_SUFFIX = "_Comments.srt"
_LEGACY_EXPORT_SUFFIXES = (".jsonl", ".csv", ".txt")
_MAX_NAME_BYTES = 255


@dataclass(frozen=True)
class ExportSegment:
    id: int
    number: int
    file_name: str
    started_at: datetime


def comment_export_name(video_name: str) -> str:
    """録画ファイルと同じ幹に _Comments.srt を付ける。255 バイトを超えるときは幹を切る。"""
    stem = Path(video_name).name
    stem = Path(stem).stem.replace('"', "").replace("\r", "").replace("\n", "").strip()
    if not stem or stem in {".", ".."} or "/" in stem or "\\" in stem:
        stem = "comments"
    room = _MAX_NAME_BYTES - len(COMMENT_EXPORT_SUFFIX.encode("utf-8"))
    encoded = stem.encode("utf-8")
    if len(encoded) > room:
        stem = encoded[:room].decode("utf-8", errors="ignore").rstrip("._")
    if not stem or stem in {".", ".."}:
        stem = "comments"
    return f"{stem}{COMMENT_EXPORT_SUFFIX}"


def comment_sidecar_relatives(video_relative: str) -> list[str]:
    """自動書き出しの SRT、以前の JSONL / CSV / TXT、統合 ASS を削除対象にする。"""
    path = Path(video_relative)
    parent = path.parent
    names = [comment_export_name(path.name), path.with_suffix(".ass").name]
    names.extend(path.with_suffix(suffix).name for suffix in _LEGACY_EXPORT_SUFFIXES)
    return [(parent / name).as_posix() for name in dict.fromkeys(names)]


def segment_offset_ms(anchor: datetime, started_at: datetime) -> int:
    return int((ensure_aware(anchor) - ensure_aware(started_at)).total_seconds() * 1000)


def assign_segment(anchor: datetime, segments: Sequence[ExportSegment]) -> ExportSegment | None:
    """コメントを、その時刻以前に始まった最後のセグメントへ入れる。最初のセグメントより前も先頭に含める。"""
    if not segments:
        return None
    moment = ensure_aware(anchor)
    ordered = sorted(segments, key=lambda item: (ensure_aware(item.started_at), item.number, item.id))
    chosen = ordered[0]
    for item in ordered:
        if ensure_aware(item.started_at) <= moment:
            chosen = item
        else:
            break
    return chosen


def partition_by_segment[T](
    items: Sequence[T],
    segments: Sequence[ExportSegment],
    anchor_of,
) -> list[tuple[ExportSegment, list[T]]]:
    ordered = tuple(sorted(segments, key=lambda item: (ensure_aware(item.started_at), item.number, item.id)))
    buckets: dict[int, list[T]] = {item.id: [] for item in ordered}
    for item in items:
        chosen = assign_segment(anchor_of(item), ordered)
        if chosen is not None:
            buckets[chosen.id].append(item)
    return [(item, buckets[item.id]) for item in ordered]
