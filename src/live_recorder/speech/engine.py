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


def transcribe(model, audio_path: Path, *, language: str, hotwords: str | None = None) -> list:
    from live_recorder.speech.quality import SpeechCue, keep_stable

    options: dict = {
        "language": language,
        "vad_filter": True,
        "temperature": 0.0,
        "condition_on_previous_text": False,
    }
    cleaned = (hotwords or "").strip()
    if cleaned:
        options["hotwords"] = cleaned
    segments, _info = model.transcribe(str(audio_path), **options)
    cues = [
        SpeechCue(
            start=segment.start,
            end=segment.end,
            text=segment.text,
            compression_ratio=float(segment.compression_ratio or 0.0),
            avg_logprob=float(segment.avg_logprob or 0.0),
        )
        for segment in segments
    ]
    return keep_stable(cues)
