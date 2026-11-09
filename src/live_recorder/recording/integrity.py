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


def scan_storage(root: Path, segments: list[dict[str, object]], *, limit: int = 200) -> ScanReport:
    report = ScanReport()
    known: dict[Path, dict[str, object]] = {}
    root_resolved = root.resolve()
    for segment in segments:
        relative = str(segment.get("file_path") or "")
        try:
            path = assert_under(root, root / relative)
        except (PathEscape, OSError):
            report.missing.append(relative)
            continue
        known[path] = segment
        if segment.get("file_removed"):
            continue
        if not path.is_file():
            _add(report.missing, relative, limit)
            continue
        finished = segment.get("finished")
        expected = segment.get("file_size")
        if finished and isinstance(expected, int) and path.stat().st_size != expected:
            _add(report.size_mismatch, relative, limit)
        if segment.get("validation") == "invalid":
            _add(report.suspect, relative, limit)
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
        if name.endswith(".part") or name.endswith(".tmp"):
            _add(report.temporary, path.relative_to(root_resolved).as_posix(), limit)
            continue
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if path.resolve() not in known:
            _add(report.unregistered, path.relative_to(root_resolved).as_posix(), limit)
    return report


def _add(bucket: list[str], value: str, limit: int) -> None:
    if len(bucket) < limit:
        bucket.append(value)
