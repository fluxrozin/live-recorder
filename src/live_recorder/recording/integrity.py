from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from live_recorder.recording.files import PathEscape, assert_under

MEDIA_SUFFIXES = {".mp4", ".ts", ".flv", ".mkv", ".m4v"}


@dataclass
class ScanReport:
    missing: list[str] = field(default_factory=list)
    size_mismatch: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    temporary: list[str] = field(default_factory=list)
    suspect: list[str] = field(default_factory=list)
    checked: int = 0
    ok: int = 0


def scan_storage(root: Path, segments: list[dict[str, object]], *, limit: int = 200) -> ScanReport:
    report = ScanReport()
    known: dict[Path, dict[str, object]] = {}
    root_resolved = root.resolve()
    for segment in segments:
        relative = str(segment.get("file_path") or "")
        removed = bool(segment.get("file_removed"))
        try:
            path = assert_under(root, root / relative)
        except (PathEscape, OSError):
            _add(report.missing, relative, limit)
            if not removed:
                report.checked += 1
            continue
        known[path] = segment
        if removed:
            continue
        report.checked += 1
        if not path.is_file():
            _add(report.missing, relative, limit)
            continue
        bad = False
        finished = segment.get("finished")
        expected = segment.get("file_size")
        if finished and isinstance(expected, int) and path.stat().st_size != expected:
            _add(report.size_mismatch, relative, limit)
            bad = True
        if segment.get("validation") == "invalid":
            _add(report.suspect, relative, limit)
            bad = True
        if not bad:
            report.ok += 1
    if not root_resolved.exists():
        return report
    for path in root_resolved.rglob("*"):
        if not path.is_file():
            continue
        try:
            assert_under(root, path)
        except PathEscape:
            continue
        name = path.name
        if _temporary_name(name):
            _add(report.temporary, path.relative_to(root_resolved).as_posix(), limit)
            continue
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if path.resolve() not in known:
            _add(report.unregistered, path.relative_to(root_resolved).as_posix(), limit)
    return report


def _temporary_name(name: str) -> bool:
    return (
        name.endswith(".part")
        or name.endswith(".tmp")
        or name.endswith(".mp4.partial")
        or (name.startswith(".remux-") and name.endswith(".partial"))
    )


def _add(bucket: list[str], value: str, limit: int) -> None:
    if len(bucket) < limit:
        bucket.append(value)
