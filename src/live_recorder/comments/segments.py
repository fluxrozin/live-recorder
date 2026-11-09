from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from live_recorder.timeutil import ensure_aware

EXPORT_SUFFIXES = (".jsonl", ".csv", ".txt")


@dataclass(frozen=True)
class ExportSegment:
    id: int
    number: int
    file_name: str
    started_at: datetime


def comment_export_name(video_name: str, kind: str) -> str:
    """録画ファイルと同じ幹の名前にする。拡張子だけ出力形式に変える。"""
    stem = Path(video_name).name
    stem = Path(stem).stem.replace('"', "").replace("\r", "").replace("\n", "").strip()
    if not stem or stem in {".", ".."} or "/" in stem or "\\" in stem:
        stem = "comments"
    suffix = kind if kind.startswith(".") else f".{kind}"
    return f"{stem}{suffix}"


def comment_sidecar_relatives(video_relative: str) -> list[str]:
    path = Path(video_relative)
    return [path.with_suffix(suffix).as_posix() for suffix in EXPORT_SUFFIXES]


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
