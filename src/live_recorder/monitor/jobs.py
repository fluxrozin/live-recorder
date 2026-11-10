from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from live_recorder.comments.export import export_row, iter_jsonl
from live_recorder.comments.segments import (
    ExportSegment,
    comment_export_name,
    partition_by_segment,
    segment_offset_ms,
)
from live_recorder.config import Settings
from live_recorder.db.repository import Repository
from live_recorder.db.runtime import load_runtime
from live_recorder.db.session import Database
from live_recorder.domain.enums import CommentJobStatus, RecordingStatus
from live_recorder.monitor.planner import backoff_seconds
from live_recorder.platforms.normalize import tiktok_live_url
from live_recorder.platforms.tiktok import comment_from_event, lookup_room, room_fields
from live_recorder.platforms.twitcasting import TwitCastingClient
from live_recorder.platforms.types import CommentDraft
from live_recorder.recording.commands import (
    ffmpeg_hls_args,
    pick_tiktok_pull_url,
    streamlink_tiktok_args,
    streamlink_twitcasting_args,
    ytdlp_args,
)
from live_recorder.recording.files import build_segment_path, relative_to_root, resolve_stored
from live_recorder.recording.finalize import finalize_recording
from live_recorder.recording.policy import (
    ACCESS_DENIED,
    INTERRUPTED,
    OFFLINE,
    classify_output,
    next_action,
    recording_result,
)
from live_recorder.recording.process import run_process
from live_recorder.timeutil import utcnow

log = logging.getLogger(__name__)
SENTINEL = object()


@dataclass
class EngineRun:
    kind: str
    size: int
    error: str


class CommentSink:
    def __init__(self, queue: asyncio.Queue, max_reconnects: int) -> None:
        self.queue = queue
        self.max_reconnects = max_reconnects
        self.dropped = 0
        self.reconnects = 0

    async def add(self, draft: CommentDraft) -> None:
        try:
            await asyncio.wait_for(self.queue.put(draft), 2)
        except TimeoutError:
            self.dropped += 1


async def _resolve_tiktok_url(user_id: str, quality: str, sign_api_key: str) -> str:
    if sign_api_key:
        os.environ["SIGN_API_KEY"] = sign_api_key
    from TikTokLive import TikTokLiveClient

    client = TikTokLiveClient(unique_id=user_id)
    await client.start(fetch_room_info=True)
    try:
        for _ in range(20):
            info = getattr(client, "room_info", None)
            if isinstance(info, dict):
                return pick_tiktok_pull_url(info, quality)
            await asyncio.sleep(0.25)
        raise RuntimeError("room_info missing")
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.info("tiktoklive disconnect failed", exc_info=True)


async def run_engine(
    *,
    settings: Settings,
    name: str,
    platform: str,
    user_id: str,
    quality: str,
    output: Path,
    stop: asyncio.Event,
    tc: TwitCastingClient,
) -> EngineRun:
    try:
        if name == "streamlink" and platform == "tiktok":
            args = streamlink_tiktok_args(settings.streamlink_path, user_id, str(output), quality)
        elif name == "yt-dlp" and platform == "tiktok":
            args = ytdlp_args(settings.ytdlp_path, tiktok_live_url(user_id), str(output))
        elif name == "tiktoklive" and platform == "tiktok":
            url = await _resolve_tiktok_url(user_id, quality, settings.sign_api_key)
            args = ffmpeg_hls_args(settings.ffmpeg_path, url, str(output))
        elif name == "official-hls" and platform == "twitcasting":
            obs = await tc.current_live(user_id)
            if obs.restricted:
                return EngineRun(ACCESS_DENIED, 0, "this live is protected")
            if not obs.hls_url:
                return EngineRun(INTERRUPTED, 0, "hls_url missing")
            args = ffmpeg_hls_args(settings.ffmpeg_path, obs.hls_url, str(output))
        elif name == "streamlink-hls" and platform == "twitcasting":
            args = streamlink_twitcasting_args(settings.streamlink_path, user_id, str(output), quality)
        else:
            return EngineRun(INTERRUPTED, 0, "unknown engine")
        result = await run_process(args, stop)
    except Exception as exc:
        name_ = type(exc).__name__
        if "AgeRestricted" in name_:
            return EngineRun(ACCESS_DENIED, 0, "age restricted")
        if "Offline" in name_:
            return EngineRun(OFFLINE, 0, "offline")
        log.info("engine %s failed before process: %s", name, name_)
        return EngineRun(INTERRUPTED, 0, name_)
    size = output.stat().st_size if output.is_file() else 0
    kind = classify_output(stderr=result.stderr, returncode=result.returncode, stopped=result.stopped)
    return EngineRun(kind, size, result.stderr[-800:])


async def _watch_stop(db: Database, stop: asyncio.Event, job_id: int, session_id: int, comment: bool = False) -> None:
    while not stop.is_set():
        async with db.session() as session:
            repo = Repository(session)
            if comment:
                job = await repo.get_comment_job(job_id)
            else:
                job = await repo.get_recording_job(job_id)
            stream = await session.get(
                __import__("live_recorder.db.models", fromlist=["StreamSession"]).StreamSession, session_id
            )
            if job is not None and job.cancel_requested:
                stop.set()
                return
            if stream is not None and stream.status == "ENDED":
                stop.set()
                return
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except TimeoutError:
            continue


async def run_recording_job(settings: Settings, db: Database, tc: TwitCastingClient, job_id: int, stop: asyncio.Event) -> None:
    watcher = asyncio.create_task(_watch_stop(db, stop, job_id, await _session_id(db, job_id)))
    saw_failure = ""
    try:
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_recording_job(job_id)
            if job is None:
                return
            stream = await repo.get_session(job.stream_session_id)
            if stream is None:
                return
            runtime = await load_runtime(session)
            streamer = stream.streamer
            session_id = stream.id
            platform = streamer.platform
            user_id = streamer.platform_user_id
            if job.started_at is None:
                job.started_at = utcnow()
            if job.status == RecordingStatus.PENDING.value:
                repo.move_recording(job, RecordingStatus.STARTING)
            await repo.add_event("recording_started", f"{platform}:{user_id} の録画を開始", session_id=session_id)
            await session.commit()
        order = list(runtime.tiktok_engine_order or []) if platform == "tiktok" else ["official-hls", "streamlink-hls"]
        attempt = 0
        engine_index = 0
        last_kind = INTERRUPTED
        title_lookup_done = False
        while not stop.is_set() and order:
            from live_recorder.recording.finalize import allow_new_recording, disk_free

            if not allow_new_recording(disk_free(settings.recordings_dir), runtime.min_free_bytes):
                saw_failure = "disk_low"
                last_kind = INTERRUPTED
                break
            engine = order[min(engine_index, len(order) - 1)]
            need_title = False
            opened = False
            async with db.session() as session:
                repo = Repository(session)
                job = await repo.get_recording_job(job_id)
                runtime = await load_runtime(session)
                if job is None or job.cancel_requested:
                    break
                if job.status == RecordingStatus.STARTING.value:
                    pass
                elif job.status == RecordingStatus.RECONNECTING.value:
                    repo.move_recording(job, RecordingStatus.STARTING)
                current = await repo.get_session(session_id)
                title = current.title if current and current.title else None
                need_title = platform == "tiktok" and not title and not title_lookup_done
                if need_title:
                    await session.commit()
                else:
                    number = await repo.next_segment_number(job_id)
                    now = utcnow()
                    from zoneinfo import ZoneInfo

                    local = now.astimezone(ZoneInfo(runtime.display_timezone))
                    path = build_segment_path(
                        root=settings.recordings_dir,
                        platform=platform,
                        user_id=user_id,
                        when_stamp=local.strftime("%Y%m%d_%H%M%S"),
                        day=local.strftime("%Y-%m-%d"),
                        title=title,
                        segment_number=number,
                    )
                    relative = relative_to_root(settings.recordings_dir, path)
                    segment = await repo.add_segment(job_id, number, relative, now)
                    segment_id = segment.id
                    repo.move_recording(job, RecordingStatus.RECORDING)
                    job.recorder_engine = engine
                    await session.commit()
                    opened = True
            if need_title:
                title_lookup_done = True
                if settings.sign_api_key:
                    os.environ["SIGN_API_KEY"] = settings.sign_api_key
                info = await lookup_room(user_id)
                if info is not None:
                    await _remember_room(db, session_id, None, info)
                continue
            if not opened:
                break
            outcome = await run_engine(
                settings=settings,
                name=engine,
                platform=platform,
                user_id=user_id,
                quality=runtime.recording_quality,
                output=path,
                stop=stop,
                tc=tc,
            )
            last_kind = outcome.kind
            saw_failure = outcome.error
            final_path = path
            validation = "invalid"
            container = None
            duration = None
            size = outcome.size
            if path.is_file() and path.stat().st_size > 0:
                final = await finalize_recording(settings.ffmpeg_path, settings.ffprobe_path, path)
                final_path = final.path
                validation = final.validation
                container = final.container
                duration = final.duration_ms
                size = final.size
            async with db.session() as session:
                repo = Repository(session)
                segment = await session.get(
                    __import__("live_recorder.db.models", fromlist=["RecordingSegment"]).RecordingSegment,
                    segment_id,
                )
                job = await repo.get_recording_job(job_id)
                if segment is not None:
                    segment.finished_at = utcnow()
                    segment.file_path = relative_to_root(settings.recordings_dir, final_path)
                    segment.file_size = size
                    segment.duration_ms = duration
                    segment.container_format = container
                    segment.validation_status = validation
                if job is not None:
                    if outcome.kind == ACCESS_DENIED or (outcome.kind == INTERRUPTED and size < 64 * 1024):
                        await repo.mark_engine(engine, ok=False, reason=outcome.error or outcome.kind)
                    elif size >= 64 * 1024:
                        await repo.mark_engine(engine, ok=True)
                await session.commit()
            action = next_action(
                kind=outcome.kind,
                bytes_written=size,
                attempt=attempt,
                max_attempts=runtime.reconnect_max_attempts,
                engine_index=engine_index,
                engine_count=len(order),
            )
            if action == "switch":
                engine_index += 1
                attempt = 0
                async with db.session() as session:
                    repo = Repository(session)
                    await repo.add_event(
                        "engine_switched",
                        f"{engine} から切り替えます",
                        severity="warning",
                        session_id=session_id,
                        details={"from": engine, "reason": outcome.kind},
                    )
                    await session.commit()
                continue
            if action == "same" and not stop.is_set():
                attempt += 1
                async with db.session() as session:
                    repo = Repository(session)
                    job = await repo.get_recording_job(job_id)
                    if job is not None and job.status == RecordingStatus.RECORDING.value:
                        repo.move_recording(job, RecordingStatus.RECONNECTING)
                        job.retry_count = attempt
                    await repo.add_gap(session_id, "recording", utcnow(), None, "reconnect")
                    await repo.add_event("reconnect", f"{engine} を再接続します", severity="warning", session_id=session_id)
                    await session.commit()
                try:
                    await asyncio.wait_for(stop.wait(), backoff_seconds(attempt))
                except TimeoutError:
                    pass
                continue
            break
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_recording_job(job_id)
            stream = await repo.get_session(session_id)
            if job is None:
                return
            has_media = await repo.any_valid_segment(job_id)
            gaps = await repo.gaps_for_session(session_id)
            recording_gaps = [
                gap
                for gap in gaps
                if gap.job_type == "recording" and (job.started_at is None or gap.started_at >= job.started_at)
            ]
            target = RecordingStatus(
                recording_result(
                    cancel_requested=job.cancel_requested,
                    kind=last_kind,
                    has_media=has_media,
                    has_gaps=bool(recording_gaps),
                )
            )
            repo.finish_recording(job, target, None if target == RecordingStatus.COMPLETED else saw_failure)
            if stream is not None and has_media:
                stream.streamer.last_recording_at = utcnow()
            severity = "info" if target in {RecordingStatus.COMPLETED, RecordingStatus.CANCELLED, RecordingStatus.PARTIAL} else "error"
            event = "recording_finished" if target != RecordingStatus.FAILED else "recording_failed"
            await repo.add_event(event, f"録画を {target.value} で終了", severity=severity, session_id=session_id)
            await session.commit()
    except Exception:
        log.exception("recording job %s crashed", job_id)
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_recording_job(job_id)
            if job is not None:
                repo.finish_recording(job, RecordingStatus.FAILED, "runner crashed")
                await repo.add_event("recording_failed", "録画処理が異常終了しました", severity="error", session_id=job.stream_session_id)
                await session.commit()
    finally:
        watcher.cancel()


async def _session_id(db: Database, job_id: int) -> int:
    async with db.session() as session:
        job = await Repository(session).get_recording_job(job_id)
        if job is None:
            return 0
        return job.stream_session_id


async def _flush_comments(db: Database, session_id: int, job_id: int, drafts: list[CommentDraft], dropped: int) -> None:
    if not drafts and dropped == 0:
        return
    async with db.session() as session:
        repo = Repository(session)
        stream = await repo.get_session(session_id)
        started = await repo.recording_started_at(session_id)
        rows = await repo.comment_rows(
            session_id=session_id,
            recording_started=started,
            stream_started=stream.platform_started_at if stream else None,
            drafts=drafts,
        )
        stored = 0
        try:
            stored = await repo.insert_comments(rows)
        except Exception:
            log.exception("comment insert failed")
            dropped += len(drafts)
        job = await repo.get_comment_job(job_id)
        if job is not None:
            job.received_count += len(drafts)
            job.stored_count += stored
            job.error_count += dropped
            if drafts:
                job.last_received_at = drafts[-1].received_at
        await session.commit()


async def _batch_writer(db: Database, session_id: int, job_id: int, queue: asyncio.Queue, sink: CommentSink) -> None:
    batch: list[CommentDraft] = []
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), 1)
        except TimeoutError:
            item = None
        if item is SENTINEL:
            break
        if isinstance(item, CommentDraft):
            batch.append(item)
        if len(batch) >= 50 or (batch and item is None):
            dropped = sink.dropped
            sink.dropped = 0
            await _flush_comments(db, session_id, job_id, batch, dropped)
            batch = []
    if batch or sink.dropped:
        await _flush_comments(db, session_id, job_id, batch, sink.dropped)
        sink.dropped = 0


async def _remember_room(db: Database, session_id: int, room_id: str | None, info: dict | None) -> None:
    stream_id, title, started = room_fields(info)
    chosen = room_id or stream_id
    async with db.session() as session:
        stream = await Repository(session).get_session(session_id)
        if stream is None:
            return
        if chosen and not stream.platform_stream_id:
            stream.platform_stream_id = chosen
        if title and not stream.title:
            stream.title = title
        if started and not stream.platform_started_at:
            stream.platform_started_at = started
        await session.commit()


async def _collect_tiktok(user_id: str, stop: asyncio.Event, sink: CommentSink, sign_api_key: str, on_room) -> str:
    if sign_api_key:
        os.environ["SIGN_API_KEY"] = sign_api_key
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent

    client = TikTokLiveClient(unique_id=user_id)

    @client.on(ConnectEvent)
    async def on_connect(event: object) -> None:
        room_id = getattr(event, "room_id", None)
        info = getattr(client, "room_info", None)
        await on_room(str(room_id) if room_id else None, info if isinstance(info, dict) else None)

    @client.on(CommentEvent)
    async def on_comment(event: object) -> None:
        draft = comment_from_event(event)
        if draft is not None:
            await sink.add(draft)

    while not stop.is_set():
        try:
            live = await asyncio.wait_for(client.is_live(), 30)
        except Exception as exc:
            return type(exc).__name__
        if not live:
            return "offline"
        connect_task = asyncio.create_task(client.connect(fetch_room_info=True))
        stop_task = asyncio.create_task(stop.wait())
        await asyncio.wait({connect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            try:
                await client.disconnect()
            except Exception:
                connect_task.cancel()
            return "stopped"
        stop_task.cancel()
        error = connect_task.exception() if connect_task.done() else None
        if error is None:
            return "ended"
        name = type(error).__name__
        if "Offline" in name:
            return "offline"
        if "AgeRestricted" in name:
            return "access_denied"
        sink.reconnects += 1
        if sink.reconnects > sink.max_reconnects:
            return name
        delay = float(getattr(error, "retry_after", 0) or backoff_seconds(sink.reconnects))
        try:
            await asyncio.wait_for(stop.wait(), min(delay, 120))
        except TimeoutError:
            continue
    return "stopped"


async def _collect_twitcasting(
    tc: TwitCastingClient,
    movie_id: str,
    stop: asyncio.Event,
    sink: CommentSink,
    poll_seconds: int,
    on_initial_gap,
) -> str:
    last_id: str | None = None
    first = True
    while not stop.is_set():
        drafts, total, error = await tc.fetch_comments(movie_id, slice_id=last_id)
        if error:
            sink.reconnects += 1
            if sink.reconnects > sink.max_reconnects:
                return error
            try:
                await asyncio.wait_for(stop.wait(), backoff_seconds(sink.reconnects))
            except TimeoutError:
                continue
            continue
        if first and total is not None and total > len(drafts):
            await on_initial_gap()
        first = False
        numeric = [int(item.platform_comment_id) for item in drafts if item.platform_comment_id and item.platform_comment_id.isdigit()]
        if numeric:
            last_id = str(max(numeric))
        for draft in sorted(drafts, key=lambda item: int(item.platform_comment_id or 0)):
            await sink.add(draft)
        if len(drafts) >= 50:
            continue
        try:
            await asyncio.wait_for(stop.wait(), poll_seconds)
        except TimeoutError:
            continue
    return "stopped"


async def run_comment_job(settings: Settings, db: Database, tc: TwitCastingClient, job_id: int, stop: asyncio.Event) -> None:
    async with db.session() as session:
        repo = Repository(session)
        job = await repo.get_comment_job(job_id)
        if job is None:
            return
        stream = await repo.get_session(job.stream_session_id)
        if stream is None:
            return
        runtime = await load_runtime(session)
        session_id = stream.id
        platform = stream.streamer.platform
        user_id = stream.streamer.platform_user_id
        movie_id = stream.platform_stream_id
        if not runtime.comments_globally_enabled or not stream.streamer.comments_enabled:
            repo.move_comments(job, CommentJobStatus.DISABLED)
            await session.commit()
            return
        job.started_at = job.started_at or utcnow()
        repo.move_comments(job, CommentJobStatus.CONNECTING)
        await repo.add_event("comments_started", f"{platform}:{user_id} のコメント収集を開始", session_id=session_id)
        await session.commit()
    watcher = asyncio.create_task(_watch_stop(db, stop, job_id, session_id, comment=True))
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    sink = CommentSink(queue, runtime.reconnect_max_attempts)
    writer = asyncio.create_task(_batch_writer(db, session_id, job_id, queue, sink))
    outcome = "failed"
    try:
        async with db.session() as session:
            job = await Repository(session).get_comment_job(job_id)
            if job is not None:
                Repository(session).move_comments(job, CommentJobStatus.COLLECTING)
                await session.commit()
        if platform == "tiktok":
            outcome = await _collect_tiktok(
                user_id,
                stop,
                sink,
                settings.sign_api_key,
                lambda room_id, info: _remember_room(db, session_id, room_id, info),
            )
        else:
            if not movie_id:
                obs = await tc.current_live(user_id)
                movie_id = obs.platform_stream_id
                if movie_id:
                    await _remember_room(db, session_id, movie_id, None)
            if not movie_id:
                outcome = "movie_id_missing"
            elif obs_restricted(await tc.current_live(user_id)):
                outcome = "access_denied"
            else:
                async def mark_gap() -> None:
                    async with db.session() as session:
                        repo = Repository(session)
                        stream = await repo.get_session(session_id)
                        started = stream.detected_at if stream else utcnow()
                        await repo.add_gap(session_id, "comments", started, utcnow(), "comments_before_collector")
                        await session.commit()

                outcome = await _collect_twitcasting(
                    tc, movie_id, stop, sink, runtime.comment_poll_seconds, mark_gap
                )
    except Exception as exc:
        log.exception("comment job %s crashed", job_id)
        outcome = type(exc).__name__
    finally:
        await queue.put(SENTINEL)
        await writer
        watcher.cancel()
    async with db.session() as session:
        repo = Repository(session)
        job = await repo.get_comment_job(job_id)
        if job is None:
            return
        job.reconnect_count = sink.reconnects
        gaps = [gap for gap in await repo.gaps_for_session(session_id) if gap.job_type == "comments"]
        if outcome == "access_denied":
            target = CommentJobStatus.FAILED
            job.last_error = "restricted or age restricted"
        elif job.stored_count == 0 and outcome not in {"stopped", "ended", "offline"}:
            target = CommentJobStatus.FAILED
            job.last_error = outcome[:2000]
        elif gaps or outcome not in {"stopped", "ended", "offline"}:
            target = CommentJobStatus.PARTIAL if job.stored_count or gaps else CommentJobStatus.FAILED
            if outcome not in {"stopped", "ended", "offline"}:
                job.last_error = outcome[:2000]
        else:
            target = CommentJobStatus.COMPLETED
        if job.status == CommentJobStatus.CONNECTING.value and target == CommentJobStatus.COLLECTING:
            pass
        try:
            repo.move_comments(job, target)
        except Exception:
            job.status = target.value
        job.finished_at = utcnow()
        runtime = await load_runtime(session)
        if runtime.auto_export_on_complete and target in {CommentJobStatus.COMPLETED, CommentJobStatus.PARTIAL}:
            await _auto_export(settings, repo, session_id, job, runtime.display_timezone)
        event = "comments_finished" if target != CommentJobStatus.FAILED else "comments_failed"
        await repo.add_event(
            event,
            f"コメント収集を {target.value} で終了",
            severity="error" if target == CommentJobStatus.FAILED else "info",
            session_id=session_id,
        )
        await session.commit()


def obs_restricted(observation) -> bool:
    return bool(getattr(observation, "restricted", False))


def _comment_row(comment, tz_name: str, stream, recording_offset_ms: int | None):
    return export_row(
        posted_at=comment.posted_at,
        received_at=comment.received_at,
        timezone_name=tz_name,
        user_id=comment.platform_user_id,
        nickname=comment.nickname,
        message=comment.message,
        platform=stream.streamer.platform if stream and stream.streamer else "",
        stream_id=stream.platform_stream_id if stream else None,
        recording_offset_ms=recording_offset_ms,
        stream_offset_ms=comment.stream_offset_ms,
        platform_comment_id=comment.platform_comment_id,
    )


def _write_comment_file(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(iter_jsonl(rows)), encoding="utf-8")


async def _auto_export(settings: Settings, repo: Repository, session_id: int, job, tz_name: str) -> None:
    try:
        stream = await repo.get_session(session_id)
        comments = await repo.export_comments(
            session_id=session_id,
            text=None,
            user_id=None,
            nickname=None,
            streamer_id=None,
            platform=None,
            start=None,
            end=None,
        )
        stored = [segment for segment in await repo.segments_for_session(session_id) if not segment.file_removed]
        windows = [
            ExportSegment(
                id=segment.id,
                number=segment.segment_number,
                file_name=Path(segment.file_path).name,
                started_at=segment.started_at,
            )
            for segment in stored
        ]
        written: list[Path] = []
        if windows:
            groups = partition_by_segment(
                comments,
                windows,
                lambda comment: comment.posted_at or comment.received_at,
            )
            by_id = {segment.id: segment for segment in stored}
            for window, grouped in groups:
                if not grouped:
                    continue
                segment = by_id[window.id]
                video = resolve_stored(settings.recordings_dir, segment.file_path)
                target = video.with_name(comment_export_name(window.file_name, "jsonl"))
                rows = [
                    _comment_row(
                        comment,
                        tz_name,
                        stream,
                        segment_offset_ms(comment.posted_at or comment.received_at, window.started_at),
                    )
                    for comment in grouped
                ]
                _write_comment_file(target, rows)
                written.append(target)
        else:
            rows = [
                _comment_row(comment, tz_name, stream, comment.recording_offset_ms) for comment in comments
            ]
            target = settings.recordings_dir / "exports" / f"session-{session_id}.jsonl"
            _write_comment_file(target, rows)
            written.append(target)
        if written:
            job.export_path = relative_to_root(settings.recordings_dir, written[0])
    except Exception:
        log.exception("auto export failed")
        await repo.add_event(
            "comments_failed",
            "自動エクスポートに失敗しました。コメント本体は残しています",
            severity="warning",
            session_id=session_id,
        )


async def close_recording_if_idle(
    settings: Settings, db: Database, job_id: int, target: RecordingStatus, *, finish: bool = True
) -> None:
    async with db.session() as session:
        repo = Repository(session)
        job = await repo.get_recording_job(job_id)
        if job is None:
            return
        for segment in await repo.segments_for_job(job_id):
            if segment.finished_at is not None or segment.file_removed:
                continue
            try:
                path = resolve_stored(settings.recordings_dir, segment.file_path)
            except ValueError:
                segment.finished_at = utcnow()
                segment.validation_status = "invalid"
                continue
            if path.is_file() and path.stat().st_size > 0:
                final = await finalize_recording(settings.ffmpeg_path, settings.ffprobe_path, path)
                segment.file_path = relative_to_root(settings.recordings_dir, final.path)
                segment.file_size = final.size
                segment.duration_ms = final.duration_ms
                segment.container_format = final.container
                segment.validation_status = final.validation
            else:
                segment.validation_status = "invalid"
                segment.file_size = 0
            segment.finished_at = utcnow()
        if finish and job.status not in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
            repo.finish_recording(job, target, "worker restarted" if target != RecordingStatus.COMPLETED else None)
        await session.commit()
