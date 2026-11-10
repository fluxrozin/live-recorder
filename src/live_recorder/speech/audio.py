from __future__ import annotations

import subprocess
from pathlib import Path


def extract_audio(ffmpeg: str, video: Path, wav: Path) -> None:
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
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg が見つかりません") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")[:300]
        raise RuntimeError(detail or f"ffmpeg が終了コード {exc.returncode} で失敗しました") from exc
