from datetime import datetime, timezone

from live_recorder.comments.dedupe import dedupe_key
from live_recorder.comments.export import export_row, render_srt
from live_recorder.comments.offsets import offsets
from live_recorder.comments.segments import (
    ExportSegment,
    assign_segment,
    comment_export_name,
    comment_sidecar_relatives,
    ordered_export_segments,
    partition_by_segment,
    segment_comment_bounds,
    segment_offset_ms,
)
from live_recorder.timeutil import parse_platform_time


def test_dedupe_keeps_repeated_text_at_another_time():
    posted = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 9, 1, 1, tzinfo=timezone.utc)
    received = datetime(2026, 11, 9, 1, 0, 2, tzinfo=timezone.utc)
    first = dedupe_key(
        platform_comment_id=None,
        platform_user_id="a",
        message="同じ",
        posted_at=posted,
        received_at=received,
    )
    second = dedupe_key(
        platform_comment_id=None,
        platform_user_id="a",
        message="同じ",
        posted_at=later,
        received_at=received,
    )
    assert first != second


def test_dedupe_uses_platform_id_when_present():
    received = datetime(2026, 11, 9, tzinfo=timezone.utc)
    key = dedupe_key(
        platform_comment_id="abc",
        platform_user_id="a",
        message="本文",
        posted_at=None,
        received_at=received,
    )
    again = dedupe_key(
        platform_comment_id="abc",
        platform_user_id="other",
        message="別の本文",
        posted_at=received,
        received_at=received,
    )
    assert key == again


def test_offsets_allow_negative_and_null():
    start = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    before = datetime(2026, 11, 9, 0, 59, tzinfo=timezone.utc)
    received = datetime(2026, 11, 9, 1, 0, 5, tzinfo=timezone.utc)
    stream_offset, recording_offset = offsets(
        posted_at=before,
        received_at=received,
        stream_started_at=start,
        recording_started_at=start,
    )
    assert stream_offset == -60_000
    assert recording_offset == -60_000
    stream_offset, recording_offset = offsets(
        posted_at=None,
        received_at=received,
        stream_started_at=start,
        recording_started_at=None,
    )
    assert stream_offset == 5_000
    assert recording_offset is None


def test_missing_platform_time_is_not_invented():
    assert parse_platform_time(None) is None
    assert parse_platform_time(0) is None
    assert parse_platform_time("nope") is None


def test_srt_uses_segment_offset_and_keeps_overlaps():
    text = render_srt(
        [
            {"nickname": "名", "user_id": "u", "message": "こんにちは", "recording_offset_ms": 83_450},
            {"nickname": "", "user_id": "late", "message": "次", "recording_offset_ms": 1_000},
            {"nickname": "早", "user_id": "u", "message": "前", "recording_offset_ms": -500},
        ]
    )
    assert text.index("前 (早 | @u)") < text.index("次 (@late)") < text.index("こんにちは (名 | @u)")
    assert "1\n00:00:00,000 --> 00:00:05,000\n前 (早 | @u)\n" in text
    assert "2\n00:00:01,000 --> 00:00:06,000\n次 (@late)\n" in text
    assert "3\n00:01:23,450 --> 00:01:28,450\nこんにちは (名 | @u)\n" in text


def test_srt_shows_name_and_id_together():
    text = render_srt(
        [
            {"nickname": "みるお", "user_id": "miruo841397", "message": "やっほー", "recording_offset_ms": 0},
            {"nickname": "みるお", "user_id": "", "message": "名前だけ", "recording_offset_ms": 0},
            {"nickname": "", "user_id": "", "message": "", "recording_offset_ms": 0},
        ]
    )
    assert "やっほー (みるお | @miruo841397)" in text
    assert "名前だけ (みるお)" in text
    assert "(unknown)" in text


def test_srt_collapses_blank_lines_and_falls_back_to_user_id():
    text = render_srt(
        [
            {"nickname": "", "user_id": "", "message": "本文", "recording_offset_ms": None},
            {"nickname": "名", "user_id": "u", "message": "一行目\n\n二行目", "recording_offset_ms": 0},
        ]
    )
    assert text == (
        "1\n00:00:00,000 --> 00:00:05,000\n本文 (unknown)\n"
        "\n"
        "2\n00:00:00,000 --> 00:00:05,000\n一行目\n二行目 (名 | @u)\n"
    )


def test_export_times_follow_display_timezone():
    posted = datetime(2026, 11, 9, 1, 2, 3, tzinfo=timezone.utc)
    received = datetime(2026, 11, 9, 1, 2, 5, tzinfo=timezone.utc)
    row = export_row(
        posted_at=posted,
        received_at=received,
        timezone_name="Asia/Tokyo",
        user_id="u",
        nickname="n",
        message="m",
        platform="tiktok",
        stream_id="s",
        recording_offset_ms=1000,
        stream_offset_ms=2000,
        platform_comment_id="c",
    )
    assert row["posted_at"] == "2026-11-09T10:02:03+09:00"
    assert row["received_at"] == "2026-11-09T10:02:05+09:00"
    assert row["time"] == "2026-11-09T10:02:03+09:00"
    assert "+00:00" not in str(row["posted_at"]) + str(row["received_at"]) + str(row["time"])
    missing = export_row(
        posted_at=None,
        received_at=datetime(2026, 11, 9, 15, 0),
        timezone_name="Asia/Tokyo",
        user_id="u",
        nickname="n",
        message="m",
        platform="tiktok",
        stream_id="s",
        recording_offset_ms=None,
        stream_offset_ms=None,
        platform_comment_id=None,
    )
    assert missing["posted_at"] is None
    assert missing["time"] == missing["received_at"] == "2026-11-10T00:00:00+09:00"


def test_comment_export_uses_recording_stem():
    video = "tiktok_@user_20261109_200621_タイトル_seg01.ts"
    assert comment_export_name(video) == "tiktok_@user_20261109_200621_タイトル_seg01_Comments.srt"
    assert comment_export_name("../comments.mp4") == "comments_Comments.srt"
    long_name = comment_export_name(("あ" * 200) + ".ts")
    assert long_name.endswith("_Comments.srt")
    assert len(long_name.encode("utf-8")) <= 255
    prefix = "tiktok_@user_20261110_010756_" + ("あ" * 80)
    first = comment_export_name(prefix + "_seg01.ts")
    second = comment_export_name(prefix + "_seg02.ts")
    assert first.endswith("_seg01_Comments.srt")
    assert second.endswith("_seg02_Comments.srt")
    assert first != second
    assert len(first.encode("utf-8")) <= 255
    relatives = comment_sidecar_relatives("tiktok/user/day/clip.ts")
    assert relatives[0] == "tiktok/user/day/clip_Comments.srt"
    assert relatives[1:] == [
        "tiktok/user/day/clip.ass",
        "tiktok/user/day/clip.jsonl",
        "tiktok/user/day/clip.csv",
        "tiktok/user/day/clip.txt",
    ]


def test_comments_split_across_segments_and_offsets():
    start = datetime(2026, 11, 9, 11, 6, 21, tzinfo=timezone.utc)
    later = datetime(2026, 11, 9, 11, 20, 0, tzinfo=timezone.utc)
    first = ExportSegment(1, 1, "seg01.mp4", start)
    second = ExportSegment(2, 2, "seg02.mp4", later)
    early = datetime(2026, 11, 9, 11, 6, 0, tzinfo=timezone.utc)
    during_first = datetime(2026, 11, 9, 11, 10, 0, tzinfo=timezone.utc)
    during_second = datetime(2026, 11, 9, 11, 21, 0, tzinfo=timezone.utc)
    assert assign_segment(early, [second, first]).id == 1
    assert assign_segment(during_second, [first, second]).id == 2
    grouped = partition_by_segment(
        [("a", early), ("b", during_first), ("c", during_second)],
        [second, first],
        lambda item: item[1],
    )
    assert [len(items) for _segment, items in grouped] == [2, 1]
    assert segment_offset_ms(during_first, start) == 219_000


def test_comments_during_a_gap_stay_out_of_the_video():
    start = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    first_end = datetime(2026, 11, 11, 12, 10, tzinfo=timezone.utc)
    second_start = datetime(2026, 11, 11, 12, 30, tzinfo=timezone.utc)
    first = ExportSegment(1, 1, "seg01.mp4", start, first_end)
    second = ExportSegment(2, 2, "seg02.mp4", second_start, None)
    during_gap = datetime(2026, 11, 11, 12, 20, tzinfo=timezone.utc)
    during_first = datetime(2026, 11, 11, 12, 5, tzinfo=timezone.utc)
    before = datetime(2026, 11, 11, 11, 50, tzinfo=timezone.utc)
    assert assign_segment(during_gap, [first, second]) is None
    assert assign_segment(during_first, [first, second]).id == 1
    assert assign_segment(before, [first, second]).id == 1


def test_comment_retention_does_not_list_speech_or_video():
    from live_recorder.comments.segments import comment_text_relatives

    relatives = comment_text_relatives("tiktok/user/day/clip.ts")
    assert "tiktok/user/day/clip.ts" not in relatives
    assert "tiktok/user/day/clip_Speech.srt" not in relatives
    assert "tiktok/user/day/clip.ass" not in relatives
    assert "tiktok/user/day/clip_Comments.srt" in relatives


def test_comment_bounds_follow_each_segment():
    start = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    second_start = datetime(2026, 11, 11, 12, 30, tzinfo=timezone.utc)
    first = ExportSegment(1, 1, "seg01.mp4", start, datetime(2026, 11, 11, 12, 10, tzinfo=timezone.utc))
    second = ExportSegment(2, 2, "seg02.mp4", second_start, None)
    ordered = ordered_export_segments([second, first])
    assert segment_comment_bounds(ordered, 0) == (None, first.ended_at, second_start)
    assert segment_comment_bounds(ordered, 1) == (second_start, None, None)
