from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from live_recorder.recording.files import stem_taken

log = logging.getLogger(__name__)
_SOURCE_TAG = "live-recorder-source="


@dataclass(frozen=True)
class Probe:
    ok: bool
    container: str | None
    duration_ms: int | None
    size: int
    has_media: bool
    video_codec: str | None
    source_tag: str | None = None


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
    tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    comment = next((value for key, value in tags.items() if str(key).lower() == "comment"), None)
    return Probe(
        has_media and size > 1024,
        container,
        duration_ms,
        size,
        has_media,
        _video_codec(streams),
        comment if isinstance(comment, str) else None,
    )


def source_marker(source: Path) -> str:
    """詰め直した MP4 に残す、元の TS の名前と大きさ。"""
    return f"{_SOURCE_TAG}{source.name}:{source.stat().st_size}"


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


def remux_args(
    ffmpeg: str,
    source: Path,
    target: Path,
    video_codec: str | None,
    marker: str | None = None,
) -> list[str]:
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
    if marker:
        args.extend(["-metadata", f"comment={marker}"])
    args.extend(["-movflags", "+faststart", "-f", "mp4", str(target)])
    return args


async def remux_to_mp4(
    ffmpeg: str,
    source: Path,
    target: Path,
    video_codec: str | None = None,
    marker: str | None = None,
) -> bool:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        *remux_args(ffmpeg, source, target, video_codec, marker),
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
    obsolete: Path | None = None


_NAME_MAX = 255


def _mp4_name(stem: str, counter: int | None) -> str:
    """255 バイト以内の MP4 名。長い幹は拡張子を残して切る。"""
    suffix = ".mp4" if counter is None else f"_{counter}.mp4"
    room = _NAME_MAX - len(suffix.encode("utf-8"))
    raw = stem.encode("utf-8")
    if len(raw) > room:
        stem = raw[:room].decode("utf-8", errors="ignore").rstrip("._") or "segment"
    return f"{stem}{suffix}"


def remux_partial_path(source: Path) -> Path:
    """同じディレクトリの短い一時名。長いタイトルでも 255 バイトを超えない。"""
    token = uuid.uuid4().hex[:12]
    return source.with_name(f".remux-{token}.mp4.partial")


def _written_after(source: Path, target: Path) -> bool:
    """target が source より新しいとき、このセグメントの詰め直し結果とみなす。"""
    try:
        return target.stat().st_size > 0 and target.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def _free_mp4(source: Path) -> Path:
    """他の録画の TS や MP4 と幹が重ならない別名。"""
    counter = 2
    while counter < 1000:
        candidate = source.with_name(_mp4_name(source.stem, counter))
        if not stem_taken(candidate):
            return candidate
        counter += 1
    raise OSError(f"空きの MP4 名がありません: {source.name}")


def _claim(partial: Path, target: Path, source: Path) -> Path:
    """詰め直した結果を、既存のファイルを上書きせずに置く。"""
    for _ in range(1000):
        try:
            os.link(partial, target)
        except FileExistsError:
            target = _free_mp4(source)
            continue
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}:
                raise
            try:
                reserved = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                target = _free_mp4(source)
                continue
            os.close(reserved)
            temporary = remux_partial_path(source)
            try:
                os.replace(partial, temporary)
                temporary.replace(target)
            except OSError:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            return target
        partial.unlink(missing_ok=True)
        return target
    raise OSError(f"空きの MP4 名がありません: {source.name}")


def discard_obsolete(path: Path | None) -> None:
    if path is not None and path.is_file():
        path.unlink()


async def finalize_recording(ffmpeg: str, ffprobe: str, source: Path) -> FinalFile:
    probed = await probe_file(ffprobe, source)
    if not probed.has_media:
        status = "partial" if probed.size > 0 else "invalid"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status, False)
    if source.suffix.lower() == ".mp4" and probed.ok:
        return FinalFile(source, probed.container or "mp4", probed.duration_ms, probed.size, "ok", True)
    preferred = source.with_name(_mp4_name(source.stem, None))
    if preferred.is_file() and preferred.stat().st_size == 0 and preferred.resolve() != source.resolve():
        preferred.unlink()
    marker = source_marker(source)
    # この TS から作った印のある MP4 だけを採用する。印の無い同名 MP4 は別の録画なので残す。
    if preferred.is_file() and preferred.resolve() != source.resolve() and _written_after(source, preferred):
        checked = await probe_file(ffprobe, preferred)
        if checked.ok and checked.source_tag == marker:
            return FinalFile(
                preferred, checked.container or "mp4", checked.duration_ms, checked.size, "ok", True, source
            )
    target = preferred if not preferred.exists() else _free_mp4(source)
    partial = remux_partial_path(source)
    partial.unlink(missing_ok=True)
    remuxed = await remux_to_mp4(ffmpeg, source, partial, probed.video_codec, marker)
    if not remuxed:
        partial.unlink(missing_ok=True)
        status = "ok" if probed.ok else "partial"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status, True)
    checked = await probe_file(ffprobe, partial)
    if not checked.ok:
        partial.unlink(missing_ok=True)
        status = "ok" if probed.ok else "partial"
        return FinalFile(source, probed.container, probed.duration_ms, probed.size, status, True)
    try:
        target = _claim(partial, target, source)
    except OSError:
        partial.unlink(missing_ok=True)
        raise
    obsolete = None if target.resolve() == source.resolve() else source
    return FinalFile(target, checked.container or "mp4", checked.duration_ms, checked.size, "ok", True, obsolete)


def disk_free(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return int(usage.free)


def disk_total(path: Path) -> int:
    return int(shutil.disk_usage(path).total)


def allow_new_recording(free_bytes: int, minimum: int) -> bool:
    return free_bytes >= minimum
