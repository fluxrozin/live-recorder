from types import SimpleNamespace

import pytest

from live_recorder.db.runtime import SettingsValidationError, validate_runtime
from live_recorder.speech.engine import transcribe
from live_recorder.speech.quality import SpeechCue, SpeechWord, keep_stable, refine_cue_times
from live_recorder.speech.queue import (
    assign_speech_queue,
    elapsed_ms,
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


def _cue(text: str, **kwargs) -> SpeechCue:
    values = {"start": 0.0, "end": 1.0, "text": text, "compression_ratio": 1.0, "avg_logprob": -0.2}
    values.update(kwargs)
    return SpeechCue(**values)


def test_transcribe_keeps_segment_text_and_uses_words_inside_it(tmp_path):
    class _Model:
        def __init__(self):
            self.calls = []

        def transcribe(self, _path, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("word_timestamps"):
                return [
                    SimpleNamespace(
                        words=[
                            SimpleNamespace(word="弟", start=605.27, end=605.55),
                            SimpleNamespace(word="に連絡つかないのって", start=618.40, end=620.40),
                            SimpleNamespace(word="やばいね", start=620.40, end=621.10),
                            SimpleNamespace(word="自己紹介欄のURLから", start=9.90, end=13.20),
                        ]
                    )
                ], None
            return [
                SimpleNamespace(
                    start=618.17,
                    end=621.41,
                    text="弟に連絡つかないのってやばいね",
                    compression_ratio=1.1,
                    avg_logprob=-0.3,
                ),
                SimpleNamespace(
                    start=9.68,
                    end=14.0,
                    text="自己紹介欄のURLから飛んでもらえると やってることいろいろわかるから",
                    compression_ratio=1.1,
                    avg_logprob=-0.3,
                ),
                SimpleNamespace(
                    start=30.0,
                    end=31.0,
                    text="聞き取り不能",
                    compression_ratio=1.0,
                    avg_logprob=-1.4,
                ),
            ], None

    model = _Model()
    cues = transcribe(model, tmp_path / "audio.wav", language="ja")
    base = {
        "language": "ja",
        "vad_filter": True,
        "temperature": 0.0,
        "condition_on_previous_text": False,
    }
    assert model.calls[0] == base
    assert model.calls[1] == {**base, "word_timestamps": True}
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (618.17, 621.41, "弟に連絡つかないのってやばいね"),
        (9.68, 14.0, "自己紹介欄のURLから飛んでもらえると やってることいろいろわかるから"),
    ]


def test_refine_cue_times_keeps_the_segment_when_words_do_not_cover_the_text():
    cue = _cue("弟に連絡つかないのってやばいね", start=618.17, end=621.41)
    words = (
        SpeechWord("弟", 618.20, 618.50),
        SpeechWord("に連絡つかないのってやばいね", 620.00, 621.19),
        SpeechWord("はみ出す", 621.20, 622.00),
    )
    refined = refine_cue_times([cue], words)
    assert [(item.start, item.end, item.text) for item in refined] == [
        (618.17, 621.41, "弟に連絡つかないのってやばいね"),
    ]
    outside = _cue("ハートミーありがとう", start=18.0, end=18.98)
    spilling = (SpeechWord("ハートミーありがとう", 18.10, 20.40),)
    assert refine_cue_times([outside], spilling) == [outside]


def test_refine_cue_times_snaps_when_one_run_covers_the_text():
    cue = _cue("弟に連絡つかないのってやばいね", start=618.17, end=621.41)
    words = (
        SpeechWord("弟", 618.20, 618.50),
        SpeechWord("に連絡つかないのって", 618.55, 620.40),
        SpeechWord("やばいね", 620.40, 621.10),
    )
    refined = refine_cue_times([cue], words)
    assert [(item.start, item.end, item.text) for item in refined] == [
        (618.20, 621.10, "弟に連絡つかないのってやばいね"),
    ]
    unrelated = _cue("こんにちは", start=1.0, end=20.0)
    assert refine_cue_times([unrelated], (SpeechWord("別の話", 18.0, 19.0),)) == [unrelated]


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
    korean = keep_stable([_cue("안녕하세요여러분")])
    assert [cue.text for cue in korean] == ["안녕하세요여러분"]
    distant = keep_stable([_cue(long, start=0, end=1), _cue(long, start=100, end=101)])
    assert [cue.text for cue in distant] == [long, long]


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
    prefix = "tiktok_@user_20261110_010756_" + ("あ" * 80)
    first = speech_export_name(prefix + "_seg01.ts")
    second = speech_export_name(prefix + "_seg02.ts")
    assert first.endswith("_seg01_Speech.srt")
    assert second.endswith("_seg02_Speech.srt")
    assert first != second


def test_elapsed_ms_counts_generation_and_never_goes_negative():
    assert elapsed_ms(10.0, 22.5) == 12_500
    assert elapsed_ms(10.0, 9.0) == 0


def test_queue_does_not_change_recording_status():
    segment = SimpleNamespace(
        file_removed=False,
        speech_status=None,
        speech_error="old",
        speech_srt_path="old.srt",
        speech_started_at="started",
        speech_duration_ms=12_000,
        recording_status="COMPLETED",
    )
    assign_speech_queue(segment, enabled=True, has_media=True)
    assert segment.speech_status == "pending"
    assert segment.speech_error is None
    assert segment.speech_srt_path is None
    assert segment.speech_started_at is None
    assert segment.speech_duration_ms is None
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
        speech_started_at="started",
        speech_duration_ms=12_000,
    )
    assert requeue_speech(segment) == "queued"
    assert segment.speech_started_at is None
    assert segment.speech_duration_ms is None
    unfinished = SimpleNamespace(
        file_removed=False,
        finished_at=None,
        speech_status="failed",
        speech_error="gpu failed",
        speech_srt_path="keep.srt",
    )
    assert requeue_speech(unfinished) == "unfinished"
    assert unfinished.speech_status == "failed"
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


def _wav_seconds(path):
    import wave

    with wave.open(str(path)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        return handle.getnframes() / handle.getframerate()


def _run_ffmpeg(*args: str) -> None:
    import subprocess

    completed = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
    )
    if completed.returncode != 0:
        pytest.skip("ffmpeg test source is unavailable")


def test_extract_audio_keeps_the_video_timeline(tmp_path):
    import shutil

    from live_recorder.speech.audio import extract_audio

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg test source is unavailable")
    lead = tmp_path / "lead.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-filter_complex",
        "[1:a]asetpts=PTS+2/TB[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-t",
        "5",
        str(lead),
    )
    late = tmp_path / "late.ts"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-filter_complex",
        "[1:a]asetpts=PTS+2/TB[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        "-output_ts_offset",
        "1000",
        str(late),
    )
    part1 = tmp_path / "part1.ts"
    part2 = tmp_path / "part2.ts"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        str(part1),
    )
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        "-output_ts_offset",
        "5",
        str(part2),
    )
    gap = tmp_path / "gap.ts"
    gap.write_bytes(part1.read_bytes() + part2.read_bytes())
    lead_wav = tmp_path / "lead.wav"
    late_wav = tmp_path / "late.wav"
    gap_wav = tmp_path / "gap.wav"
    extract_audio("ffmpeg", lead, lead_wav)
    extract_audio("ffmpeg", late, late_wav)
    extract_audio("ffmpeg", gap, gap_wav)
    assert 2.8 < _wav_seconds(lead_wav) < 3.4
    assert 2.8 < _wav_seconds(late_wav) < 3.4
    assert 6.5 < _wav_seconds(gap_wav) < 7.2
    jump_a = tmp_path / "jump_a.ts"
    jump_b = tmp_path / "jump_b.ts"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        str(jump_a),
    )
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        "-output_ts_offset",
        "12.7",
        str(jump_b),
    )
    jump = tmp_path / "jump.ts"
    jump.write_bytes(jump_a.read_bytes() + jump_b.read_bytes())
    jump_wav = tmp_path / "jump.wav"
    extract_audio("ffmpeg", jump, jump_wav)
    assert 12.0 < _wav_seconds(jump_wav) < 16.0
    mkv = tmp_path / "late.mkv"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=6",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-filter_complex",
        "[1:a]asetpts=PTS+5/TB[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-t",
        "6",
        str(mkv),
    )
    mkv_wav = tmp_path / "late-mkv.wav"
    extract_audio("ffmpeg", mkv, mkv_wav)
    assert 5.5 < _wav_seconds(mkv_wav) < 6.5


def test_speech_settings_accept_known_model_and_language():
    settings = validate_runtime({"speech_language": "JA", "speech_model": "large-v3-turbo"})
    assert settings.speech_language == "ja"
    assert settings.speech_model == "large-v3-turbo"
    with pytest.raises(SettingsValidationError):
        validate_runtime({"speech_model": "tiny"})
    with pytest.raises(SettingsValidationError):
        validate_runtime({"speech_language": ""})
