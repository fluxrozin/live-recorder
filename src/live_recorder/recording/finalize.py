from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Probe:
    ok: bool
    container: str | None
    duration_ms: int | None
    size: int
    has_media: bool


async def probe_file(ffprobe: str, path: Path) -> Probe:
    size = path.stat().st_size if path.is_file() else 0
    if size <= 0:
        return Probe(False, None, None, size, False)
    full = await _ffprobe_json(ffprobe, path)
    if not full:
        return Probe(False, None, None, size, False)
    try:
        payload = json.loads(full)
    except json.JSONDecodeError:
        return Probe(False, None, None, size, False)
    streams = payload.get("streams") or []
    has_media = any(item.get("codec_type") in {"video", "audio"} for item in streams if isinstance(item, dict))
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    container = fmt.get("format_name")
    duration_ms = None
    duration = fmt.get("duration")
    if duration not in (None, "N/A"):
        try:
            duration_ms = int(float(duration) * 1000)
        except (TypeError, ValueError):
            duration_ms = None
    return Probe(has_media and size > 1024, str(container) if container else None, duration_ms, size, has_media)


async def _ffprobe_json(ffprobe: str, path: Path) -> str:
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return ""
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", errors="replace")


async def remux_to_mp4(ffmpeg: str, source: Path, target: Path) -> bool:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0 and target.is_file() and target.stat().st_size > 0


@dataclass(frozen=True)
class FinalFile:
    path: Path
    container: str | None
    duration_ms: int | None
    size: int
    validation: str


async def finalize_recording(ffmpeg: str, ffprobe: str, source: Path) -> FinalFile:
    probed = await probe_file(ffprobe, source)
    if not probed.has_media:
        status = "partial" if probed.size > 0 else "invalid"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status)
    if source.suffix.lower() == ".mp4" and probed.ok:
        return FinalFile(source, probed.container or "mp4", probed.duration_ms, probed.size, "ok")
    target = source.with_suffix(".mp4")
    if target.exists():
        target = source.with_name(f"{source.stem}_remux.mp4")
    remuxed = await remux_to_mp4(ffmpeg, source, target)
    if not remuxed:
        status = "ok" if probed.ok else "partial"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status)
    checked = await probe_file(ffprobe, target)
    if checked.ok:
        source.unlink(missing_ok=True)
        return FinalFile(target, checked.container or "mp4", checked.duration_ms, checked.size, "ok")
    target.unlink(missing_ok=True)
    status = "ok" if probed.ok else "partial"
    return FinalFile(source, probed.container, probed.duration_ms, probed.size, status)


def disk_free(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return int(usage.free)


def disk_total(path: Path) -> int:
    return int(shutil.disk_usage(path).total)


def allow_new_recording(free_bytes: int, minimum: int) -> bool:
    return free_bytes >= minimum
