from __future__ import annotations

import json
import subprocess
from pathlib import Path


def extract_audio(ffmpeg: str, video: Path, wav: Path) -> None:
    """映像の再生時刻に合わせた WAV を書く。先頭のずれと途中の欠落は無音として残す。"""
    delay_ms = _leading_delay_ms(*_stream_starts(_ffprobe_path(ffmpeg), video))
    filters = "aresample=async=1:osr=16000:ochl=mono"
    if delay_ms > 0:
        filters = f"{filters},adelay={delay_ms}:all=1"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-af",
        filters,
        "-c:a",
        "pcm_s16le",
        str(wav),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg が見つかりません") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")[:300]
        raise RuntimeError(detail or f"ffmpeg が終了コード {exc.returncode} で失敗しました") from exc


def _ffprobe_path(ffmpeg: str) -> str:
    return str(Path(ffmpeg).with_name("ffprobe"))


def _stream_starts(ffprobe: str, video: Path) -> tuple[float | None, float | None]:
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,start_time",
                "-of",
                "json",
                str(video),
            ],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        return None, None
    if proc.returncode != 0:
        return None, None
    try:
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None, None
    video_start = None
    audio_start = None
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        raw = stream.get("start_time")
        if raw in (None, "N/A"):
            continue
        try:
            start = float(raw)
        except (TypeError, ValueError):
            continue
        kind = stream.get("codec_type")
        if kind == "video" and video_start is None:
            video_start = start
        elif kind == "audio" and audio_start is None:
            audio_start = start
    return video_start, audio_start


def _leading_delay_ms(video_start: float | None, audio_start: float | None) -> int:
    """音声が映像より遅れて始まるミリ秒。共通の大きな PTS は引き算で消える。"""
    if video_start is None or audio_start is None:
        return 0
    delta = audio_start - video_start
    if delta <= 0.0005:
        return 0
    return int(round(delta * 1000))
