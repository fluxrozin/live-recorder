import re
from pathlib import Path

from live_recorder.comments.ass import (
    combined_ass_path,
    display_size_from_probe,
    escape_ass,
    format_ass_time,
    refresh_combined_ass,
    render_ass,
)
from live_recorder.comments.segments import comment_export_name
from live_recorder.speech.srt import format_srt_time, speech_srt_path


def _write_srt(path: Path, cues: list[tuple[int, int, str]]) -> None:
    blocks = []
    for index, (start, end, text) in enumerate(cues, start=1):
        stamp = f"{format_srt_time(start / 1000)} --> {format_srt_time(end / 1000)}"
        blocks.append(f"{index}\n{stamp}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def test_ass_time_is_centiseconds():
    assert format_ass_time(1500) == "0:00:01.50"
    assert format_ass_time(3_661_000) == "1:01:01.00"


def test_ass_escapes_overrides_and_keeps_newlines():
    assert escape_ass("a{b}\\c") == r"a\{b\}\\c"
    assert escape_ass("上\n下") == r"上\N下"


def test_styles_leave_the_font_blank_and_stack_comments():
    speech = [(1_500, 5_000, "今日は{配信}する")]
    comments = [
        (2_000, 7_000, "ユーザーA：何時まで？"),
        (2_000, 7_000, "ユーザーB：かわいい\n二行目\n三行目"),
        (2_000, 7_000, "ユーザーC：次"),
    ]
    body = render_ass(speech, comments, width=1080, height=1920)
    assert body.count("\nStyle: ") == 2
    assert "Style: Speech,,48,&H00FFFFFF," in body
    assert "Style: Comment,,48,&H0000FFFF," in body
    assert "Yu Gothic" not in body
    assert r"今日は\{配信\}する" in body
    assert r"\N二行目" in body
    assert "三行目" not in body
    assert "Dialogue: 0,0:00:01.50,0:00:05.00,Speech," in body
    margins = [int(match) for match in re.findall(r",Comment,,0,0,(\d+),,", body)]
    assert margins == [146, 146 + 106, 146 + 106 * 3]
    assert all(margin > 40 for margin in margins)
    speech_line = next(line for line in body.splitlines() if line.startswith("Dialogue: 0,"))
    assert r"{\an2}" in speech_line
    assert ",Speech,,0,0,0,," in speech_line
    assert r"\pos(" not in body
    comment_line = next(line for line in body.splitlines() if ",Comment,,0,0,146,," in line)
    assert r"{\an2}" in comment_line


def test_rotated_probe_swaps_width_and_height():
    payload = {
        "streams": [
            {"codec_type": "audio"},
            {"codec_type": "video", "width": 1920, "height": 1080, "tags": {"rotate": "-90"}},
        ]
    }
    assert display_size_from_probe(payload) == (1080, 1920)
    upright = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080, "side_data_list": [{"rotation": 180}]}]}
    assert display_size_from_probe(upright) == (1920, 1080)


def test_speech_keeps_its_end_when_the_next_line_follows():
    body = render_ass(
        [(0, 8_000, "見てたら"), (8_000, 9_200, "こんな時間になってた")],
        [],
        width=1080,
        height=1920,
    )
    assert "Dialogue: 0,0:00:00.00,0:00:08.00,Speech," in body
    assert "Dialogue: 0,0:00:08.00,0:00:09.20,Speech," in body


def test_speech_hides_a_long_tail_before_silence():
    body = render_ass(
        [(0, 30_000, "見てたら"), (40_000, 42_000, "続き")],
        [],
        width=1080,
        height=1920,
    )
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,Speech," in body
    assert "Dialogue: 0,0:00:40.00,0:00:42.00,Speech," in body


def test_long_speech_does_not_stay_up_and_emoji_are_removed():
    body = render_ass(
        [(0, 70_000, "あんよが")],
        [(0, 5_000, "輝元🐶: おはよ")],
        width=1080,
        height=1920,
    )
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,Speech," in body
    assert "🐶" not in body
    assert "輝元: おはよ" in body


def test_missing_comment_srt_uses_legacy_jsonl(tmp_path: Path):
    video = tmp_path / "live.mp4"
    video.write_bytes(b"not a video")
    video.with_suffix(".jsonl").write_text(
        '{"nickname":"みるお","user_id":"@miruo841397","message":"やっほー","recording_offset_ms":2000}\n',
        encoding="utf-8",
    )
    written = refresh_combined_ass(video, play_size=(1080, 1920))
    assert written is not None
    body = written.read_text(encoding="utf-8-sig")
    assert "Dialogue: 1,0:00:02.00,0:00:07.00,Comment," in body
    assert "やっほー (みるお | @miruo841397)" in body


def test_refresh_writes_bom_and_removes_empty_file(tmp_path: Path):
    video = tmp_path / "live.mp4"
    video.write_bytes(b"not a video")
    _write_srt(speech_srt_path(video), [(0, 1_000, "発話")])
    _write_srt(video.with_name(comment_export_name(video.name)), [(0, 5_000, "視聴者: こんにちは")])
    written = refresh_combined_ass(video, play_size=(1080, 1920))
    assert written == combined_ass_path(video)
    raw = written.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "PlayResX: 1080" in raw.decode("utf-8-sig")
    speech_srt_path(video).unlink()
    video.with_name(comment_export_name(video.name)).unlink()
    assert refresh_combined_ass(video, play_size=(1080, 1920)) is None
    assert not written.exists()


def test_refresh_reads_the_stored_speech_srt_instead_of_the_video_name(tmp_path: Path):
    video = tmp_path / "live.mp4"
    video.write_bytes(b"not a video")
    _write_srt(speech_srt_path(video), [(0, 1_000, "古い名前の字幕")])
    stored = tmp_path / "stored_Speech.srt"
    _write_srt(stored, [(0, 1_000, "DBの字幕")])
    body = refresh_combined_ass(video, play_size=(1080, 1920), speech_srt=stored).read_text(
        encoding="utf-8-sig"
    )
    assert "DBの字幕" in body and "古い名前の字幕" not in body
    _write_srt(video.with_name(comment_export_name(video.name)), [(0, 5_000, "視聴者: こんにちは")])
    body = refresh_combined_ass(video, play_size=(1080, 1920), speech_srt=None).read_text(
        encoding="utf-8-sig"
    )
    assert "古い名前の字幕" not in body and "こんにちは" in body
