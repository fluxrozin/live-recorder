from __future__ import annotations

from pathlib import Path


def load_whisper(model_name: str, download_root: Path):
    """CUDA float16 で faster-whisper を載せる。呼び出し側だけがこの依存を使う。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper がありません。transcriber イメージは speech 追加依存付きでビルドしてください"
        ) from exc
    download_root.mkdir(parents=True, exist_ok=True)
    return WhisperModel(
        model_name,
        device="cuda",
        compute_type="float16",
        download_root=str(download_root),
    )


def transcribe(model, audio_path: Path, *, language: str) -> list:
    """セグメントの文章を残し、区間の内側にある語の時刻へ開始と終了を寄せる。"""
    from live_recorder.speech.quality import (
        SpeechCue,
        SpeechWord,
        collect_speech_words,
        keep_stable,
        refine_cue_times,
    )

    options = {
        "language": language,
        "vad_filter": True,
        "temperature": 0.0,
        "condition_on_previous_text": False,
    }
    segments, _info = model.transcribe(str(audio_path), **options)
    cues = keep_stable(
        [
            SpeechCue(
                start=segment.start,
                end=segment.end,
                text=segment.text,
                compression_ratio=float(segment.compression_ratio or 0.0),
                avg_logprob=float(segment.avg_logprob or 0.0),
            )
            for segment in segments
        ]
    )
    timed, _timed_info = model.transcribe(str(audio_path), **options, word_timestamps=True)
    words: list[SpeechWord] = []
    for segment in timed:
        words.extend(collect_speech_words(getattr(segment, "words", None)))
    return refine_cue_times(cues, tuple(words))
