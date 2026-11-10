from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Probe:
    ok: bool
    container: str | None
    duration_ms: int | None
    size: int
    has_media: bool
    video_codec: str | None


def container_name(format_name: str | None) -> str | None:
    """ffprobe の format_name を画面に出せる短い容器名にする。"""
    if not format_name:
        return None
    names = {part.strip() for part in format_name.split(",") if part.strip()}
    if "mp4" in names or "mov" in names:
        return "mp4"
    if "mpegts" in names:
        return "mpegts"
    if "flv" in names or "live_flv" in names:
        return "flv"
    if "matroska" in names:
        return "mkv"
    if "webm" in names:
        return "webm"
    return format_name.split(",")[0].strip() or None


def _video_codec(streams: object) -> str | None:
    if not isinstance(streams, list):
        return None
    for item in streams:
        if not isinstance(item, dict) or item.get("codec_type") != "video":
            continue
        name = item.get("codec_name")
        if isinstance(name, str) and name:
            return name
    return None


async def probe_file(ffprobe: str, path: Path) -> Probe:
    size = path.stat().st_size if path.is_file() else 0
    if size <= 0:
        return Probe(False, None, None, size, False, None)
    full = await _ffprobe_json(ffprobe, path)
    if not full:
        return Probe(False, None, None, size, False, None)
    try:
        payload = json.loads(full)
    except json.JSONDecodeError:
        return Probe(False, None, None, size, False, None)
    streams = payload.get("streams") or []
    has_media = any(item.get("codec_type") in {"video", "audio"} for item in streams if isinstance(item, dict))
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    raw_container = fmt.get("format_name")
    container = container_name(str(raw_container) if raw_container else None)
    duration_ms = None
    duration = fmt.get("duration")
    if duration not in (None, "N/A"):
        try:
            duration_ms = int(float(duration) * 1000)
        except (TypeError, ValueError):
            duration_ms = None
    return Probe(
        has_media and size > 1024,
        container,
        duration_ms,
        size,
        has_media,
        _video_codec(streams),
    )


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


def remux_args(ffmpeg: str, source: Path, target: Path, video_codec: str | None) -> list[str]:
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-c",
        "copy",
    ]
    # TikTok の FLV は HEVC。MP4 の既定タグ hev1 だと再生できないプレーヤーがある。
    if video_codec == "hevc":
        args.extend(["-tag:v", "hvc1"])
    args.extend(["-movflags", "+faststart", str(target)])
    return args


async def remux_to_mp4(ffmpeg: str, source: Path, target: Path, video_codec: str | None = None) -> bool:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        *remux_args(ffmpeg, source, target, video_codec),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    ok = proc.returncode == 0 and target.is_file() and target.stat().st_size > 0
    if not ok:
        target.unlink(missing_ok=True)
        detail = err.decode("utf-8", errors="replace").strip().replace("\n", " ")[:300]
        log.info("mp4 remux failed for %s: %s", source.name, detail or f"exit {proc.returncode}")
    return ok


@dataclass(frozen=True)
class FinalFile:
    path: Path
    container: str | None
    duration_ms: int | None
    size: int
    validation: str
    has_media: bool


async def finalize_recording(ffmpeg: str, ffprobe: str, source: Path) -> FinalFile:
    probed = await probe_file(ffprobe, source)
    if not probed.has_media:
        status = "partial" if probed.size > 0 else "invalid"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status, False)
    if source.suffix.lower() == ".mp4" and probed.ok:
        return FinalFile(source, probed.container or "mp4", probed.duration_ms, probed.size, "ok", True)
    target = _mp4_target(source)
    remuxed = await remux_to_mp4(ffmpeg, source, target, probed.video_codec)
    if not remuxed:
        status = "ok" if probed.ok else "partial"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status, True)
    checked = await probe_file(ffprobe, target)
    if checked.ok:
        source.unlink(missing_ok=True)
        return FinalFile(target, checked.container or "mp4", checked.duration_ms, checked.size, "ok", True)
    target.unlink(missing_ok=True)
    status = "ok" if probed.ok else "partial"
    return FinalFile(source, probed.container, probed.duration_ms, probed.size, status, True)


def _mp4_target(source: Path) -> Path:
    target = source.with_suffix(".mp4")
    if target.is_file() and target.stat().st_size == 0:
        target.unlink()
    if target.exists():
        target = source.with_name(f"{source.stem}_remux.mp4")
        if target.is_file() and target.stat().st_size == 0:
            target.unlink()
    return target


def disk_free(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return int(usage.free)


def disk_total(path: Path) -> int:
    return int(shutil.disk_usage(path).total)


def allow_new_recording(free_bytes: int, minimum: int) -> bool:
    return free_bytes >= minimum
