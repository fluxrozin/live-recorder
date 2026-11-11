from __future__ import annotations

import tempfile
from pathlib import Path

from live_recorder.speech.audio import extract_audio
from live_recorder.speech.engine import transcribe
from live_recorder.speech.srt import speech_srt_path, write_speech_srt


def assign_speech_queue(segment, *, enabled: bool, has_media: bool) -> None:
    """確定直後に字幕の待ち状態を決める。録画の状態は変えない。"""
    segment.speech_error = None
    segment.speech_srt_path = None
    if segment.file_removed or not enabled or not has_media:
        segment.speech_status = "skipped"
        return
    segment.speech_status = "pending"


def requeue_speech(segment) -> str:
    """再生成を待ち行列へ戻す。処理中と、すでに待ちのものは変えない。既存の SRT は残す。"""
    if segment.file_removed:
        return "removed"
    if segment.speech_status == "running":
        return "running"
    if segment.speech_status == "pending":
        return "pending"
    segment.speech_status = "pending"
    segment.speech_error = None
    return "queued"


def speech_result_path(*, status: str, produced: str | None, previous: str | None) -> str | None:
    """完了時だけパスを置き換える。失敗した再生成では、今ある SRT を残す。"""
    if status == "completed":
        return produced
    return previous


def clip_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ").strip()[:2000] or exc.__class__.__name__


def interpret_speech(action):
    """1本の成否を返す。例外は呼び出し元のループを止めない。"""
    try:
        produced = action()
    except Exception as exc:
        return "failed", None, clip_error(exc)
    return "completed", produced, None


def transcribe_video(ffmpeg: str, model, video: Path, *, language: str) -> Path | None:
    with tempfile.TemporaryDirectory() as temporary:
        wav = Path(temporary) / "audio.wav"
        extract_audio(ffmpeg, video, wav)
        cues = transcribe(model, wav, language=language)
    target = speech_srt_path(video)
    if write_speech_srt(cues, target) == 0:
        return None
    return target
