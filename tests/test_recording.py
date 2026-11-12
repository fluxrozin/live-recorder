import os
from pathlib import Path

import pytest

from live_recorder.recording.commands import (
    ffmpeg_hls_args,
    pick_tiktok_pull_url,
    streamlink_tiktok_args,
)
from live_recorder.recording.files import PathEscape, build_segment_path, resolve_stored, safe_token
from live_recorder.recording.integrity import scan_storage
from live_recorder.recording.policy import (
    ACCESS_DENIED,
    INTERRUPTED,
    OFFLINE,
    STOPPED,
    leave_recording_open,
    next_action,
    recording_result,
)


def test_hls_url_stays_on_twitcasting():
    from live_recorder.platforms.twitcasting import validate_hls_url

    assert validate_hls_url("https://twitcasting.tv/user/metastream.m3u8/?video=1")
    assert validate_hls_url("https://edge.twitcasting.tv/live.m3u8")
    assert validate_hls_url("http://twitcasting.tv/user/metastream.m3u8") is None
    assert validate_hls_url("https://evil.example/live.m3u8") is None


def test_filename_uses_platform_user_time_and_title(tmp_path: Path):
    path = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="../etc",
        when_stamp="20261109_210000",
        day="2026-11-09",
        title="ゲスト出勤/../秘密",
        segment_number=2,
    )
    assert path.is_relative_to(tmp_path)
    assert ".." not in path.parts
    assert path.name == "tiktok_@etc_20261109_210000_ゲスト出勤..秘密_seg02.ts"
    untitled = build_segment_path(
        root=tmp_path,
        platform="twitcasting",
        user_id="caster",
        when_stamp="20261110_010756",
        day="2026-11-10",
        title=None,
        segment_number=1,
    )
    assert untitled.name == "twitcasting_@caster_20261110_010756_無題_seg01.ts"
    long_name = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="9ri_pi",
        when_stamp="20261110_010756",
        day="2026-11-10",
        title="あ" * 200,
        segment_number=1,
    )
    assert len(long_name.name.encode("utf-8")) <= 255
    assert long_name.name.startswith("tiktok_@9ri_pi_20261110_010756_")
    assert long_name.name.endswith("_seg01.ts")
    assert safe_token("../../x") == "x"
    occupied = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="guest",
        when_stamp="20261109_210000",
        day="2026-11-09",
        title="同じ秒",
        segment_number=1,
    )
    occupied.with_suffix(".mp4").write_bytes(b"old")
    again = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="guest",
        when_stamp="20261109_210000",
        day="2026-11-09",
        title="同じ秒",
        segment_number=1,
    )
    assert again.name == occupied.name.removesuffix(".ts") + "_2.ts"
    assert occupied.with_suffix(".mp4").read_bytes() == b"old"
    reserved = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="reserved",
        when_stamp="20261109_210000",
        day="2026-11-09",
        title="削除待ち",
        segment_number=1,
        reserved_paths={
            "tiktok/reserved/2026-11-09/"
            "tiktok_@reserved_20261109_210000_削除待ち_seg01.mp4"
        },
    )
    assert reserved.name == "tiktok_@reserved_20261109_210000_削除待ち_seg01_2.ts"


def test_closed_untitled_recording_takes_the_session_title(tmp_path: Path):
    from live_recorder.recording.retitle import (
        retitle_closed_file,
        rewrite_stored_path,
        rollback_retitle,
        stage_retitle,
    )

    video = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01.mp4"
    video.write_bytes(b"video")
    speech = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01_Speech.srt"
    speech.write_text("speech", encoding="utf-8")
    comments = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01_Comments.srt"
    comments.write_text("comments", encoding="utf-8")
    ass = video.with_suffix(".ass")
    ass.write_text("ass", encoding="utf-8")
    assert stage_retitle(video, None) is None
    staged = stage_retitle(video, "ゲスト出勤")
    assert staged is not None
    renamed = staged.dest
    assert renamed.name == "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01.mp4"
    assert video.exists() and video.samefile(renamed)
    relative = "tiktok/9ri_pi/2026-11-12/" + video.name
    assert rewrite_stored_path(staged, relative) == "tiktok/9ri_pi/2026-11-12/" + renamed.name
    assert rewrite_stored_path(staged, relative.removesuffix(".mp4") + "_Comments.srt").endswith(
        "_ゲスト出勤_seg01_Comments.srt"
    )
    rollback_retitle(staged)
    assert video.is_file() and not renamed.exists()
    assert speech.is_file()
    renamed = retitle_closed_file(video, "ゲスト出勤")
    assert renamed.name == "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01.mp4"
    assert renamed.read_bytes() == b"video"
    assert not video.exists()
    assert (tmp_path / "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01_Speech.srt").read_text(encoding="utf-8") == "speech"
    assert (tmp_path / "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01_Comments.srt").read_text(encoding="utf-8") == "comments"
    assert renamed.with_suffix(".ass").read_text(encoding="utf-8") == "ass"
    second = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01_2.mp4"
    second.write_bytes(b"next")
    collided = retitle_closed_file(second, "ゲスト出勤")
    assert collided.name == "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01_2.mp4"
    assert renamed.read_bytes() == b"video"
    titled = tmp_path / "tiktok_@9ri_pi_20261111_044027_ゲスト出勤_seg01.mp4"
    titled.write_bytes(b"kept")
    assert retitle_closed_file(titled, "別の題") == titled


def test_retitle_keeps_a_sidecar_when_the_destination_is_another_file(tmp_path: Path):
    from live_recorder.recording.retitle import rewrite_stored_path, stage_retitle

    video = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01.mp4"
    video.write_bytes(b"video")
    speech = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01_Speech.srt"
    speech.write_text("ours", encoding="utf-8")
    other = tmp_path / "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01_Speech.srt"
    other.write_text("theirs", encoding="utf-8")
    staged = stage_retitle(video, "ゲスト出勤")
    assert staged is not None
    stored = "day/" + speech.name
    assert rewrite_stored_path(staged, stored) == stored
    assert speech.read_text(encoding="utf-8") == "ours"
    assert other.read_text(encoding="utf-8") == "theirs"


async def test_stage_retitle_keeps_work_when_the_waiter_is_cancelled(tmp_path: Path, monkeypatch):
    import asyncio
    import time

    import live_recorder.monitor.jobs as jobs
    from live_recorder.monitor.jobs import _release_retitles, _stage_retitle

    video = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01.mp4"
    video.write_bytes(b"video")
    sink = []
    started = asyncio.Event()
    real = jobs.stage_retitle

    def slow(path, title):
        started.set()
        time.sleep(0.2)
        return real(path, title)

    monkeypatch.setattr(jobs, "stage_retitle", slow)
    task = asyncio.create_task(_stage_retitle(video, "ゲスト出勤", sink))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sink) == 1
    assert sink[0].dest.is_file() and video.is_file()
    _release_retitles(sink, publish=False)
    assert video.is_file()
    assert not (tmp_path / "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01.mp4").exists()


def test_retitle_never_overwrites_a_name_taken_after_the_check(tmp_path: Path, monkeypatch):
    import live_recorder.recording.retitle as retitle

    video = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01.mp4"
    video.write_bytes(b"ours")
    dest = tmp_path / "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01.mp4"
    real_link = os.link

    def racing_link(src, dst):
        if Path(dst) == dest:
            Path(dst).write_bytes(b"theirs")
        return real_link(src, dst)

    monkeypatch.setattr(retitle.os, "link", racing_link)
    assert retitle.stage_retitle(video, "ゲスト出勤") is None
    assert dest.read_bytes() == b"theirs"
    assert video.read_bytes() == b"ours"


def test_retitle_copy_fallback_tracks_a_failed_copy(tmp_path: Path, monkeypatch):
    import errno

    import live_recorder.recording.retitle as retitle

    video = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01.mp4"
    video.write_bytes(b"video")
    speech = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01_Speech.srt"
    speech.write_text("speech", encoding="utf-8")

    def no_link(src, dst):
        raise OSError(errno.EXDEV, "cross-device")

    def broken_copy(src, dst, **_kwargs):
        Path(dst).write_bytes(b"half")
        if Path(src) == video:
            raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(retitle.os, "link", no_link)
    monkeypatch.setattr(retitle.shutil, "copy2", broken_copy)
    assert retitle.stage_retitle(video, "ゲスト出勤") is None
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([video.name, speech.name])

    monkeypatch.setattr(retitle.shutil, "copy2", lambda src, dst, **_kw: Path(dst).write_bytes(Path(src).read_bytes()))
    staged = retitle.stage_retitle(video, "ゲスト出勤")
    assert staged is not None and staged.dest.read_bytes() == b"video"
    retitle.publish_retitle(staged)
    assert not video.exists() and not speech.exists()


def test_retitle_does_not_share_a_stem_with_an_open_ts(tmp_path: Path):
    from live_recorder.recording.retitle import retitle_closed_file

    video = tmp_path / "tiktok_@9ri_pi_20261112_080415_無題_seg01.mp4"
    video.write_bytes(b"mp4")
    ts = tmp_path / "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01.ts"
    ts.write_bytes(b"ts-long")
    renamed = retitle_closed_file(video, "ゲスト出勤")
    assert renamed.name == "tiktok_@9ri_pi_20261112_080415_ゲスト出勤_seg01_2.mp4"
    assert ts.read_bytes() == b"ts-long"


def test_long_title_does_not_stat_an_overlong_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import errno

    real_exists = Path.exists

    def guarded(self: Path) -> bool:
        if len(self.name.encode("utf-8")) > 255:
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", guarded)
    path = build_segment_path(
        root=tmp_path,
        platform="tiktok",
        user_id="9ri_pi",
        when_stamp="20261110_010756",
        day="2026-11-10",
        title="あ" * 200,
        segment_number=1,
    )
    assert len(path.name.encode("utf-8")) <= 255
    assert path.name.endswith("_seg01.ts")


def test_resolve_rejects_escape(tmp_path: Path):
    with pytest.raises(PathEscape):
        resolve_stored(tmp_path, "../secret.mp4")


def test_tc_hls_prefers_high_and_skips_other_hosts():
    from live_recorder.platforms.twitcasting import pick_tc_hls_url

    streams = {
        "high": "https://evil.example/live.m3u8",
        "medium": "https://edge.twitcasting.tv/tc.livehls/medium.m3u8",
        "low": "https://edge.twitcasting.tv/tc.livehls/low.m3u8",
    }
    assert pick_tc_hls_url(streams, "best") == "https://edge.twitcasting.tv/tc.livehls/medium.m3u8"
    assert pick_tc_hls_url(streams, "high") == "https://edge.twitcasting.tv/tc.livehls/medium.m3u8"
    assert pick_tc_hls_url(streams, "medium") == "https://edge.twitcasting.tv/tc.livehls/medium.m3u8"
    assert pick_tc_hls_url({"high": streams["medium"]}, "medium") is None
    assert pick_tc_hls_url({"low": streams["low"]}, "best") == streams["low"]


def test_twitcasting_ffmpeg_sends_player_headers_only_when_asked():
    args = ffmpeg_hls_args(
        "ffmpeg",
        "https://edge.twitcasting.tv/live.m3u8",
        "out.ts",
        user_agent="Mozilla/5.0",
        referer="https://twitcasting.tv/",
    )
    assert args[args.index("-user_agent") + 1] == "Mozilla/5.0"
    assert args[args.index("-headers") + 1] == "Referer: https://twitcasting.tv/\r\n"
    assert args.index("-headers") < args.index("-i")
    plain = ffmpeg_hls_args("ffmpeg", "https://cdn.example/live.m3u8", "out.ts")
    assert "-user_agent" not in plain
    assert "-headers" not in plain


def test_streamlink_command_uses_normalized_url():
    args = streamlink_tiktok_args("streamlink", "@user.name", "/tmp/out.ts", "best")
    assert args[0] == "streamlink"
    assert "https://www.tiktok.com/@user.name/live" in args
    assert "--no-config" in args


def test_stream_end_with_media_is_completed():
    assert recording_result(cancel_requested=False, kind=STOPPED, has_media=True, has_gaps=False) == "COMPLETED"
    assert recording_result(cancel_requested=False, kind=OFFLINE, has_media=True, has_gaps=False) == "COMPLETED"
    assert recording_result(cancel_requested=False, kind=STOPPED, has_media=True, has_gaps=True) == "PARTIAL"
    assert recording_result(cancel_requested=False, kind=STOPPED, has_media=False, has_gaps=False) == "FAILED"


def test_monitoring_stop_stays_cancelled():
    assert recording_result(cancel_requested=True, kind=STOPPED, has_media=True, has_gaps=False) == "CANCELLED"


def test_access_denied_does_not_switch_engine():
    assert next_action(kind=ACCESS_DENIED, bytes_written=0, attempt=0, max_attempts=5, engine_index=0, engine_count=3) == "stop"


def test_extract_failure_switches_then_reconnects_after_data():
    assert next_action(kind=INTERRUPTED, bytes_written=0, attempt=0, max_attempts=5, engine_index=0, engine_count=3) == "switch"
    assert next_action(kind=INTERRUPTED, bytes_written=100_000, attempt=1, max_attempts=5, engine_index=1, engine_count=3) == "same"
    assert next_action(kind=INTERRUPTED, bytes_written=100_000, attempt=5, max_attempts=5, engine_index=1, engine_count=3) == "stop"


def test_pick_only_https_rendition_that_exists():
    room = {
        "stream_url": {
            "live_core_sdk_data": {
                "pull_data": {
                    "stream_data": {
                        "data": {
                            "hd": {"main": {"flv": "https://cdn.example/live.flv"}},
                        }
                    }
                }
            }
        }
    }
    assert pick_tiktok_pull_url(room, "best") == "https://cdn.example/live.flv"
    room["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"]["data"]["ld"] = {
        "main": {"flv": "http://cdn.example/insecure.flv"}
    }
    with pytest.raises(KeyError):
        pick_tiktok_pull_url({"stream_url": {"live_core_sdk_data": {"pull_data": {"stream_data": {"data": {}}}}}}, "medium")


def test_scan_finds_missing_unregistered_and_temp(tmp_path: Path):
    kept = tmp_path / "tiktok" / "a"
    kept.mkdir(parents=True)
    registered = kept / "clip.mp4"
    registered.write_bytes(b"1234")
    (kept / "extra.mp4").write_bytes(b"abcd")
    (kept / "partial.part").write_bytes(b"x")
    report = scan_storage(
        tmp_path,
        [
            {"file_path": "tiktok/a/clip.mp4", "file_size": 4, "validation": "ok", "finished": True, "file_removed": False},
            {"file_path": "tiktok/a/missing.mp4", "file_size": 4, "validation": "ok", "finished": True, "file_removed": False},
            {"file_path": "tiktok/a/clip.mp4", "file_size": 9, "validation": "invalid", "finished": True, "file_removed": False},
        ],
    )
    assert "tiktok/a/missing.mp4" in report.missing
    assert any(item.endswith("extra.mp4") for item in report.unregistered)
    assert any(item.endswith(".part") for item in report.temporary)
    assert any(item.endswith("clip.mp4") for item in report.suspect)
    (kept / "clip.mp4.partial").write_bytes(b"p")
    (kept / ".remux-abc.mp4.partial").write_bytes(b"r")
    again = scan_storage(
        tmp_path,
        [
            {"file_path": "tiktok/a/clip.mp4", "file_size": 4, "validation": "ok", "finished": True, "file_removed": False},
        ],
    )
    assert any(item.endswith(".mp4.partial") for item in again.temporary)
    assert again.ok == 1
    assert again.checked == 1


def test_comment_failure_still_has_a_terminal_state():
    from live_recorder.domain.enums import CommentJobStatus
    from live_recorder.monitor.jobs import comment_job_result, next_comment_wait

    failed, error = comment_job_result(outcome="stopped", stored_count=3, has_gaps=False, writer_failed=True)
    assert failed == CommentJobStatus.PARTIAL
    assert error == "comment_writer_failed"
    empty, empty_error = comment_job_result(outcome="stopped", stored_count=0, has_gaps=False, writer_failed=True)
    assert empty == CommentJobStatus.FAILED
    assert empty_error == "comment_writer_failed"
    done, clean = comment_job_result(outcome="stopped", stored_count=3, has_gaps=False, writer_failed=False)
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from live_recorder.monitor.jobs import comment_gaps_for_job

    job = SimpleNamespace(started_at=datetime(2026, 11, 12, 1, tzinfo=timezone.utc))
    overlap = SimpleNamespace(
        job_type="comments",
        started_at=datetime(2026, 11, 12, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 11, 12, 1, 5, tzinfo=timezone.utc),
    )
    earlier = SimpleNamespace(
        job_type="comments",
        started_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        ended_at=datetime(2026, 11, 11, 2, tzinfo=timezone.utc),
    )
    assert comment_gaps_for_job([overlap, earlier], job) == [overlap]
    assert done == CommentJobStatus.COMPLETED
    assert clean is None
    dropped, _dropped_error = comment_job_result(
        outcome="stopped", stored_count=3, has_gaps=False, writer_failed=False, dropped=2
    )
    assert dropped == CommentJobStatus.PARTIAL
    assert next_comment_wait(1, "http_403", 300) >= 300
    assert next_comment_wait(1, "timeout", 0) <= 120
    assert leave_recording_open(stopped=True, cancel_requested=False, stream_status="LIVE")
    assert not leave_recording_open(stopped=True, cancel_requested=False, stream_status="ENDED")
    assert not leave_recording_open(stopped=True, cancel_requested=True, stream_status="LIVE")


def test_scan_ok_count_ignores_the_display_cap(tmp_path: Path):
    segments = [
        {
            "file_path": f"missing-{index}.mp4",
            "file_size": 1,
            "validation": "ok",
            "finished": True,
            "file_removed": False,
        }
        for index in range(250)
    ]
    report = scan_storage(tmp_path, segments)
    assert report.checked == 250
    assert report.ok == 0
    assert len(report.missing) == 200


async def test_empty_live_body_is_not_offline():
    import httpx

    from live_recorder.domain.enums import LiveStatus
    from live_recorder.platforms.twitcasting import TwitCastingClient

    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    def ended(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"movie": {"id": "1", "is_live": False}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(empty)) as http:
        client = TwitCastingClient(client_id="id", client_secret="secret", access_token="", client=http)
        observation = await client.current_live("caster")
    assert observation.status == LiveStatus.ERROR
    assert observation.error == "invalid_response"
    async with httpx.AsyncClient(transport=httpx.MockTransport(ended)) as http:
        client = TwitCastingClient(client_id="id", client_secret="secret", access_token="", client=http)
        observation = await client.current_live("caster")
    assert observation.status == LiveStatus.OFFLINE


async def test_open_live_hls_uses_streamserver_not_api_hls_url():
    import httpx

    from live_recorder.platforms.twitcasting import TwitCastingClient

    seen: list[str] = []
    high = "https://203-0-113-10.twitcasting.tv/tc.livehls/v1/streams/1/hls/high/media.m3u8"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path == "/users/caster":
            return httpx.Response(200, json={"user": {"id": "1"}})
        if path == "/users/caster/current_live":
            return httpx.Response(
                200,
                json={
                    "movie": {
                        "id": "1",
                        "is_live": True,
                        "is_protected": False,
                        "is_membership": False,
                        "hls_url": "https://twitcasting.tv/caster/metastream.m3u8/?video=1",
                    }
                },
            )
        if path == "/streamserver.php":
            assert request.url.params["target"] == "caster"
            assert request.headers["referer"] == "https://twitcasting.tv/"
            return httpx.Response(
                200,
                json={
                    "movie": {"id": 1, "live": True},
                    "tc-hls": {"streams": {"high": high, "low": "https://evil.example/live.m3u8"}},
                },
            )
        if path.endswith("/media.m3u8"):
            assert request.headers["user-agent"].startswith("Mozilla/5.0")
            assert request.headers["referer"] == "https://twitcasting.tv/"
            return httpx.Response(200, text="#EXTM3U\n#EXT-X-VERSION:3\n")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = TwitCastingClient(client_id="id", client_secret="secret", access_token="", client=http)
        url, reason = await client.open_live_hls("caster", "best")
    assert reason is None
    assert url == high
    assert not any("metastream.m3u8" in item for item in seen)


async def test_open_live_hls_rejects_a_closed_playlist_and_protected_live():
    import httpx

    from live_recorder.platforms.twitcasting import TwitCastingClient

    def closed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/caster":
            return httpx.Response(200, json={})
        if request.url.path == "/users/caster/current_live":
            return httpx.Response(200, json={"movie": {"id": "1", "is_live": True}})
        if request.url.path == "/streamserver.php":
            return httpx.Response(
                200,
                json={"tc-hls": {"streams": {"high": "https://edge.twitcasting.tv/live.m3u8"}}},
            )
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(closed)) as http:
        client = TwitCastingClient(client_id="id", client_secret="secret", access_token="", client=http)
        url, reason = await client.open_live_hls("caster", "high")
    assert url is None
    assert reason == "playlist http_400"

    def protected(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/caster":
            return httpx.Response(200, json={})
        if request.url.path == "/users/caster/current_live":
            return httpx.Response(200, json={"movie": {"id": "1", "is_live": True, "is_membership": True}})
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(protected)) as http:
        client = TwitCastingClient(client_id="id", client_secret="secret", access_token="", client=http)
        url, reason = await client.open_live_hls("caster", "best")
    assert url is None
    assert reason == "access_denied"


def test_cancel_stops_the_recording_process():
    import asyncio

    from live_recorder.recording.process import run_process

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_process(["sleep", "30"], stop))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            return
        raise AssertionError("cancel was swallowed")

    asyncio.run(run())


def _comment(number: int):
    from datetime import datetime, timezone

    from live_recorder.platforms.types import CommentDraft

    return CommentDraft(str(number), "u", "n", f"m{number}", None, datetime.now(timezone.utc))


class _CommentPages:
    def __init__(self):
        self.calls = []
        self.all_ids = list(range(180, 0, -1))

    async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
        self.calls.append((slice_id, offset))
        if slice_id is not None:
            newer = [item for item in self.all_ids if item > int(slice_id)]
            return [_comment(item) for item in newer[:limit]], 180, None
        start = 0 if offset is None else offset
        page = self.all_ids[start : start + limit]
        return [_comment(item) for item in page], 180, None


async def test_comments_since_backfills_when_a_page_is_full():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    pages = _CommentPages()
    drafts, total, error, truncated = await fetch_comments_since(pages, "55", "100")
    assert error is None and truncated is False and total == 180
    assert [int(item.platform_comment_id) for item in drafts] == list(range(101, 181))
    assert pages.calls == [("100", None), (None, 0), (None, 50)]


async def test_comments_since_walks_a_short_page_when_the_count_says_more():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    class Short:
        def __init__(self):
            self.calls = []

        async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
            self.calls.append((slice_id, offset))
            if slice_id is not None:
                return [_comment(item) for item in range(180, 139, -1)], 180, None
            start = 0 if offset is None else offset
            page = list(range(180, 0, -1))[start : start + limit]
            return [_comment(item) for item in page], 180, None

    pages = Short()
    drafts, total, error, truncated = await fetch_comments_since(pages, "55", "100", last_total=100)
    assert error is None and truncated is False and total == 180
    assert [int(item.platform_comment_id) for item in drafts] == list(range(101, 181))
    assert pages.calls == [("100", None), (None, 0), (None, 50)]


class _SparsePages:
    """ID は全配信で共有され、1コメントごとに数百〜数千飛ぶ。"""

    def __init__(self, count: int):
        self.calls = []
        self.all_ids = [1_000_000 + 997 * index for index in range(count, 0, -1)]

    async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
        self.calls.append((slice_id, offset))
        total = len(self.all_ids)
        if slice_id is not None:
            newer = [item for item in self.all_ids if item > int(slice_id)]
            return [_comment(item) for item in newer[:limit]], total, None
        start = 0 if offset is None else offset
        return [_comment(item) for item in self.all_ids[start : start + limit]], total, None


@pytest.mark.parametrize("new", [5, 49])
async def test_sparse_ids_finish_in_one_request_when_the_count_matches(new: int):
    from live_recorder.platforms.twitcasting import fetch_comments_since

    pages = _SparsePages(200)
    cursor = pages.all_ids[new]
    drafts, total, error, truncated = await fetch_comments_since(
        pages, "55", str(cursor), last_total=200 - new
    )
    assert error is None and truncated is False and total == 200
    assert [int(item.platform_comment_id) for item in drafts] == sorted(pages.all_ids[:new])
    assert pages.calls == [(str(cursor), None)]


async def test_sparse_ids_walk_when_a_full_page_matches_the_count():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    pages = _SparsePages(200)
    cursor = pages.all_ids[50]
    drafts, total, error, truncated = await fetch_comments_since(
        pages, "55", str(cursor), last_total=150
    )
    assert error is None and truncated is False and total == 200
    assert [int(item.platform_comment_id) for item in drafts] == sorted(pages.all_ids[:50])
    assert pages.calls[0] == (str(cursor), None)
    assert (None, 0) in pages.calls


async def test_sparse_ids_backfill_every_new_comment_without_a_gap():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    pages = _SparsePages(200)
    cursor = pages.all_ids[80]
    drafts, total, error, truncated = await fetch_comments_since(pages, "55", str(cursor), last_total=120)
    assert error is None and truncated is False and total == 200
    assert [int(item.platform_comment_id) for item in drafts] == sorted(pages.all_ids[:80])
    assert pages.calls == [(str(cursor), None), (None, 0), (None, 50)]


async def test_empty_first_poll_then_burst_backfills_without_a_silent_gap():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    class Burst:
        def __init__(self):
            self.n = 0
            self.calls = []

        async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
            self.calls.append((slice_id, offset))
            self.n += 1
            if self.n == 1:
                return [], 0, None
            ids = list(range(80, 0, -1))
            if slice_id is not None:
                newer = [item for item in ids if item > int(slice_id)]
                return [_comment(item) for item in newer[:limit]], 80, None
            start = 0 if offset is None else offset
            return [_comment(item) for item in ids[start : start + limit]], 80, None

    pages = Burst()
    first, total, error, truncated = await fetch_comments_since(pages, "55", None, last_total=None)
    assert first == [] and total == 0 and truncated is False and error is None
    drafts, total, error, truncated = await fetch_comments_since(pages, "55", None, last_total=0)
    assert error is None and truncated is False and total == 80
    assert [int(item.platform_comment_id) for item in drafts] == list(range(1, 81))
    assert pages.calls == [(None, None), (None, None), (None, 50)]


async def test_count_drop_from_deletions_still_walks_a_full_page():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    class Pages:
        def __init__(self):
            self.calls = []

        async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
            self.calls.append((slice_id, offset))
            # 削除で件数差は 49 だが、新着は 52。満杯ページは必ずさかのぼる。
            if slice_id is not None:
                return [_comment(item) for item in range(152, 102, -1)], 149, None
            start = 0 if offset is None else offset
            page = list(range(152, 100, -1))[start : start + limit]
            return [_comment(item) for item in page], 149, None

    drafts, total, error, truncated = await fetch_comments_since(Pages(), "55", "100", last_total=100)
    assert error is None and truncated is False and total == 149
    ids = [int(item.platform_comment_id) for item in drafts]
    assert 101 in ids and 102 in ids and 152 in ids


async def test_comments_since_keeps_a_short_page_without_walking_history():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    pages = _CommentPages()
    drafts, _, error, truncated = await fetch_comments_since(pages, "55", "170")
    assert error is None and truncated is False
    assert [int(item.platform_comment_id) for item in drafts] == list(range(171, 181))
    assert pages.calls == [("170", None)]
    first, total, error, truncated = await fetch_comments_since(pages, "55", None)
    assert error is None and truncated is False and total == 180
    assert len(first) == 50
    assert pages.calls[-1] == (None, None)


async def test_comments_since_marks_a_gap_when_the_walk_stops_early():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    pages = _CommentPages()
    drafts, _, error, truncated = await fetch_comments_since(pages, "55", "100", max_pages=1)
    assert error is None and truncated is True
    assert [int(item.platform_comment_id) for item in drafts] == list(range(131, 181))


async def test_comments_since_drops_nothing_into_the_cursor_when_backfill_fails():
    from live_recorder.platforms.twitcasting import fetch_comments_since

    class Broken:
        async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
            if slice_id is not None:
                return [_comment(item) for item in range(180, 130, -1)], 180, None
            return [], None, "rate_limited"

    drafts, _, error, truncated = await fetch_comments_since(Broken(), "55", "100")
    assert drafts == [] and error == "rate_limited" and truncated is False


async def test_truncated_comment_walk_moves_on_and_records_one_gap():
    import asyncio

    from live_recorder.monitor.jobs import _collect_twitcasting

    stop = asyncio.Event()

    class Sink:
        def __init__(self):
            self.reconnects = 0
            self.max_reconnects = 2

        async def add(self, _draft):
            return None

    pages = _SparsePages(3000)

    class Fake:
        def __init__(self):
            self.slices: list[str | None] = []

        async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
            self.slices.append(slice_id)
            if slice_id is None and offset is None:
                return [_comment(pages.all_ids[-1])], 1, None
            if slice_id is not None and slice_id != str(pages.all_ids[-1]):
                stop.set()
                return [], len(pages.all_ids), None
            return await pages.fetch_comments(movie_id, slice_id=slice_id, limit=limit, offset=offset)

    fake = Fake()
    gaps: list[str] = []

    async def on_gap(reason: str) -> None:
        gaps.append(reason)

    outcome = await asyncio.wait_for(_collect_twitcasting(fake, "55", stop, Sink(), 0, on_gap), 2)
    assert outcome == "stopped"
    slices = [item for item in fake.slices if item is not None]
    assert slices == [str(pages.all_ids[-1]), str(pages.all_ids[0])]
    assert gaps == ["comments_page_truncated"]


class _TikTokSink:
    def __init__(self):
        self.items: list[str] = []
        self.reconnects = 0
        self.max_reconnects = 5
        self.dropped = 0
        self.integrity_missed = False

    async def add(self, draft):
        self.items.append(draft.message)


class _LiveChecks:
    async def check(self, _user_id):
        from types import SimpleNamespace

        from live_recorder.platforms.types import LiveStatus

        return SimpleNamespace(status=LiveStatus.LIVE, error=None)

    def block_remaining(self) -> float:
        return 0.0


def _fake_tiktok(monkeypatch, rooms: list[str]):
    """接続ごとに、部屋の確認が終わる前にコメントを1件流す TikTokLive の代わり。"""
    import asyncio
    from types import SimpleNamespace

    from TikTokLive.events import CommentEvent, ConnectEvent

    class Client:
        def __init__(self, unique_id):
            self.handlers = {}
            self.room_info = None
            self.attempt = 0

        def on(self, event_type):
            def register(handler):
                self.handlers[event_type] = handler
                return handler

            return register

        async def connect(self, fetch_room_info=True):
            room = rooms[self.attempt]
            self.attempt += 1
            checking = asyncio.create_task(self.handlers[ConnectEvent](SimpleNamespace(room_id=room)))
            await asyncio.sleep(0)
            await self.handlers[CommentEvent](SimpleNamespace(content=f"{room}-early", user=None, common=None))
            await checking
            await self.handlers[CommentEvent](SimpleNamespace(content=f"{room}-late", user=None, common=None))
            error = ConnectionError("dropped")
            error.retry_after = 0.01
            raise error

        async def disconnect(self):
            return None

    monkeypatch.setattr("TikTokLive.TikTokLiveClient", Client)


async def test_tiktok_holds_comments_until_each_connection_is_checked(monkeypatch):
    import asyncio

    from live_recorder.monitor.jobs import _collect_tiktok

    _fake_tiktok(monkeypatch, ["r1", "r2", "r2"])
    stop = asyncio.Event()
    sink = _TikTokSink()

    async def on_room(room_id, _info):
        await asyncio.sleep(0.01)
        return room_id == "r1"

    outcome = await asyncio.wait_for(
        _collect_tiktok("someone", stop, sink, "", on_room, _LiveChecks()), 2
    )
    assert outcome == "stopped"
    assert sink.items == ["r1-early", "r1-late"]


async def test_tiktok_room_check_failure_keeps_unverified_comments_out(monkeypatch):
    import asyncio

    from live_recorder.monitor.jobs import _collect_tiktok

    _fake_tiktok(monkeypatch, ["r1", "r1"])
    stop = asyncio.Event()
    sink = _TikTokSink()
    sink.max_reconnects = 0
    gaps: list[str] = []

    async def on_room(_room_id, _info):
        raise RuntimeError("db down")

    async def on_unverified(reason: str) -> None:
        gaps.append(reason)

    outcome = await asyncio.wait_for(
        _collect_tiktok("someone", stop, sink, "", on_room, _LiveChecks(), on_unverified),
        2,
    )
    assert outcome == "room_check_failed"
    assert sink.items == []
    assert gaps == ["room_check_failed"]


async def test_tiktok_room_check_failure_retries_until_match(monkeypatch):
    import asyncio

    from live_recorder.monitor.jobs import _collect_tiktok

    _fake_tiktok(monkeypatch, ["r1", "r1"])
    stop = asyncio.Event()
    sink = _TikTokSink()
    checks = 0

    async def on_room(_room_id, _info):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("db down")
        stop.set()
        return True

    gaps: list[str] = []

    async def on_unverified(reason: str) -> None:
        gaps.append(reason)

    outcome = await asyncio.wait_for(
        _collect_tiktok("someone", stop, sink, "", on_room, _LiveChecks(), on_unverified),
        2,
    )
    assert outcome == "stopped"
    assert checks == 2
    assert gaps == ["room_check_failed"]
    assert "r1-early" in sink.items


async def test_tiktok_held_overflow_counts_as_dropped(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from TikTokLive.events import CommentEvent, ConnectEvent

    from live_recorder.monitor.jobs import _collect_tiktok

    class Client:
        def __init__(self, unique_id):
            self.handlers = {}
            self.room_info = None

        def on(self, event_type):
            def register(handler):
                self.handlers[event_type] = handler
                return handler

            return register

        async def connect(self, fetch_room_info=True):
            checking = asyncio.create_task(self.handlers[ConnectEvent](SimpleNamespace(room_id="r1")))
            await asyncio.sleep(0)
            for index in range(550):
                await self.handlers[CommentEvent](
                    SimpleNamespace(content=f"c{index}", user=None, common=None)
                )
            await checking
            stop.set()
            raise ConnectionError("dropped")

        async def disconnect(self):
            return None

    monkeypatch.setattr("TikTokLive.TikTokLiveClient", Client)
    stop = asyncio.Event()
    sink = _TikTokSink()

    async def on_room(_room_id, _info):
        await asyncio.sleep(0.05)
        return True

    await asyncio.wait_for(_collect_tiktok("someone", stop, sink, "", on_room, _LiveChecks()), 2)
    assert len(sink.items) == 500
    assert sink.dropped == 50


async def test_collect_twitcasting_backfills_without_a_tight_loop():
    import asyncio

    from live_recorder.monitor.jobs import _collect_twitcasting

    stop = asyncio.Event()

    class Sink:
        def __init__(self):
            self.items: list[int] = []
            self.reconnects = 0
            self.max_reconnects = 2

        async def add(self, draft):
            self.items.append(int(draft.platform_comment_id))
            if 101 in self.items and 180 in self.items:
                stop.set()

    class Fake:
        def __init__(self):
            self.calls = []
            self.pages = _CommentPages()

        async def fetch_comments(self, movie_id, *, slice_id=None, limit=50, offset=None):
            self.calls.append((slice_id, offset))
            if slice_id is None and offset is None:
                return [_comment(100)], 1, None
            return await self.pages.fetch_comments(movie_id, slice_id=slice_id, limit=limit, offset=offset)

    async def on_gap(_reason: str) -> None:
        return None

    fake = Fake()
    sink = Sink()
    outcome = await asyncio.wait_for(_collect_twitcasting(fake, "55", stop, sink, 0, on_gap), 2)
    assert outcome == "stopped"
    assert sink.items == [100, *range(101, 181)]
    assert fake.calls == [(None, None), ("100", None), (None, 0), (None, 50)]
