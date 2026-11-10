from types import SimpleNamespace

import pytest

from live_recorder.db.runtime import SettingsValidationError, validate_runtime
from live_recorder.speech.engine import transcribe
from live_recorder.speech.quality import SpeechCue, keep_stable
from live_recorder.speech.queue import (
    assign_speech_queue,
    interpret_speech,
    requeue_speech,
    speech_result_path,
)
from live_recorder.speech.srt import (
    format_srt_time,
    parse_speech_srt,
    speech_export_name,
    write_speech_srt,
)
from live_recorder.speech.words import SpeechWordsError, normalize_speech_words


def _cue(text: str, **kwargs) -> SpeechCue:
    values = {"start": 0.0, "end": 1.0, "text": text, "compression_ratio": 1.0, "avg_logprob": -0.2}
    values.update(kwargs)
    return SpeechCue(**values)


def test_speech_words_collapse_separators_and_reject_long_lists():
    assert normalize_speech_words("  さくら\nApex  さくら\tゲーム名 ") == "さくら Apex ゲーム名"
    assert normalize_speech_words(" \n ") == ""
    with pytest.raises(SpeechWordsError):
        normalize_speech_words("あ" * 201)


def test_transcribe_sends_hotwords_only_when_present(tmp_path):
    class _Model:
        def __init__(self):
            self.kwargs = None

        def transcribe(self, _path, **kwargs):
            self.kwargs = kwargs
            return [], None

    audio = tmp_path / "audio.wav"
    model = _Model()
    transcribe(model, audio, language="ja", hotwords=" さくら Apex ")
    assert model.kwargs["hotwords"] == "さくら Apex"
    assert model.kwargs["condition_on_previous_text"] is False
    bare = _Model()
    transcribe(bare, audio, language="ja", hotwords="  ")
    assert "hotwords" not in bare.kwargs


def test_keep_stable_drops_reference_failures_and_extra_noise():
    long = "あいうえおかきくけこさし"
    kept = keep_stable(
        [
            _cue("  こんにちは  "),
            _cue(""),
            _cue("無音", end=0.0),
            _cue("圧縮", compression_ratio=2.41),
            _cue("境界", compression_ratio=2.4),
            _cue("低信頼", avg_logprob=-1.01),
            _cue("境界信頼", avg_logprob=-1.0),
            _cue("♪♪"),
            _cue("ご視聴ありがとうございました！"),
            _cue("今日はご視聴ありがとうございました、楽しかった"),
            _cue("Thanks for watching."),
            _cue("そうですね。そうですね。そうですね。"),
            _cue("そうですねそうですねそうですね"),
            _cue("うん"),
            _cue("うん"),
            _cue(long),
            _cue(long),
            _cue("123"),
        ]
    )
    texts = [cue.text for cue in kept]
    assert texts == [
        "こんにちは",
        "境界",
        "境界信頼",
        "今日はご視聴ありがとうございました、楽しかった",
        "うん",
        "うん",
        long,
        "123",
    ]


def test_keep_stable_removes_copied_hotword_runs_and_keeps_a_single_word():
    words = "ハートミー 人気アップ おつきゅ コンカフェ おーか 斎藤 ドゥー 才蔵"
    echo = "おつきゅ コンカフェ おーか 斎藤 ドゥー 才蔵"
    kept = keep_stable(
        [
            _cue(f"あくのごまちゃん 素晴らしいにゃん ありがとう あくのごまちゃん {echo}"),
            _cue("おつきゅ ありがとう"),
            _cue(f"{echo}。"),
            _cue("プープー言ってる猫ちゃんの話?"),
            _cue("おつきゅ コンカフェ"),
        ],
        hotwords=words,
    )
    assert [cue.text for cue in kept] == [
        "あくのごまちゃん 素晴らしいにゃん ありがとう あくのごまちゃん",
        "おつきゅ ありがとう",
        "プープー言ってる猫ちゃんの話?",
        "おつきゅ コンカフェ",
    ]


def test_write_speech_srt_replaces_atomically_and_removes_empty(tmp_path):
    target = tmp_path / "recording_Speech.srt"
    count = write_speech_srt([SpeechCue(0, 1.5, "こんにちは")], target)
    assert count == 1
    assert format_srt_time(3661.5) == "01:01:01,500"
    assert not (tmp_path / ".recording_Speech.srt.tmp").exists()
    assert target.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:01,500\nこんにちは\n"
    assert write_speech_srt([SpeechCue(0, 1, "   ")], target) == 0
    assert not target.exists()


def test_parse_speech_srt_reads_written_cues(tmp_path):
    target = tmp_path / "recording_Speech.srt"
    write_speech_srt(
        [SpeechCue(0, 1.5, "こんにちは"), SpeechCue(2, 3.25, "次の行\n続き")],
        target,
    )
    assert parse_speech_srt(target.read_text(encoding="utf-8")) == [
        (0, 1500, "こんにちは"),
        (2000, 3250, "次の行\n続き"),
    ]
    assert parse_speech_srt("壊れた塊\n\n2\nnot-a-time\n本文\n") == []


def test_speech_name_stays_within_filename_limit():
    name = speech_export_name("a" * 300 + ".mp4")
    assert name.endswith("_Speech.srt")
    assert len(name.encode("utf-8")) <= 255


def test_queue_does_not_change_recording_status():
    segment = SimpleNamespace(
        file_removed=False,
        speech_status=None,
        speech_error="old",
        speech_srt_path="old.srt",
        recording_status="COMPLETED",
    )
    assign_speech_queue(segment, enabled=True, has_media=True)
    assert segment.speech_status == "pending"
    assert segment.speech_error is None
    assert segment.speech_srt_path is None
    assert segment.recording_status == "COMPLETED"
    assign_speech_queue(segment, enabled=False, has_media=True)
    assert segment.speech_status == "skipped"
    assert segment.recording_status == "COMPLETED"
    assign_speech_queue(segment, enabled=True, has_media=False)
    assert segment.speech_status == "skipped"


def test_one_speech_failure_does_not_stop_the_next():
    jobs = [
        {"id": 1, "recording_status": "COMPLETED"},
        {"id": 2, "recording_status": "PARTIAL"},
    ]

    def run(job):
        if job["id"] == 1:
            raise RuntimeError("gpu failed")
        return "segment_Speech.srt"

    outcomes = []
    for job in jobs:
        status, produced, error = interpret_speech(lambda job=job: run(job))
        outcomes.append((job["id"], status, produced, error, job["recording_status"]))
    assert outcomes[0][1] == "failed"
    assert "gpu failed" in outcomes[0][3]
    assert outcomes[0][4] == "COMPLETED"
    assert outcomes[1] == (2, "completed", "segment_Speech.srt", None, "PARTIAL")


def test_requeue_keeps_existing_srt_until_a_new_result_replaces_it():
    segment = SimpleNamespace(
        file_removed=False,
        speech_status="failed",
        speech_error="gpu failed",
        speech_srt_path="keep.srt",
    )
    assert requeue_speech(segment) == "queued"
    assert segment.speech_status == "pending"
    assert segment.speech_error is None
    assert segment.speech_srt_path == "keep.srt"
    assert requeue_speech(segment) == "pending"
    segment.speech_status = "running"
    assert requeue_speech(segment) == "running"
    assert segment.speech_srt_path == "keep.srt"
    segment.file_removed = True
    assert requeue_speech(segment) == "removed"

    assert speech_result_path(status="failed", produced=None, previous="keep.srt") == "keep.srt"
    assert speech_result_path(status="completed", produced="new.srt", previous="keep.srt") == "new.srt"
    assert speech_result_path(status="completed", produced=None, previous="keep.srt") is None


def test_speech_settings_accept_known_model_and_language():
    settings = validate_runtime({"speech_language": "JA", "speech_model": "large-v3-turbo"})
    assert settings.speech_language == "ja"
    assert settings.speech_model == "large-v3-turbo"
    with pytest.raises(SettingsValidationError):
        validate_runtime({"speech_model": "tiny"})
    with pytest.raises(SettingsValidationError):
        validate_runtime({"speech_language": ""})
