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
