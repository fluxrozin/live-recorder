from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from live_recorder.comments.ass import refresh_combined_ass
from live_recorder.comments.export import encode_export, export_row
from live_recorder.comments.segments import (
    ExportSegment,
    comment_export_name,
    partition_by_segment,
    segment_end,
    segment_offset_ms,
)
from live_recorder.config import Settings
from live_recorder.db.repository import Repository
from live_recorder.db.runtime import load_runtime
from live_recorder.db.session import Database
from live_recorder.domain.enums import CommentJobStatus, LiveStatus, RecordingStatus
from live_recorder.monitor.planner import backoff_seconds
from live_recorder.platforms.normalize import tiktok_live_url
from live_recorder.platforms.tiktok import (
    TikTokChecks,
    comment_from_event,
    lookup_room,
    pauses_tiktok_polling,
    room_fields,
)
from live_recorder.platforms.twitcasting import (
    TWITCASTING_REFERER,
    TWITCASTING_USER_AGENT,
    TwitCastingClient,
    fetch_comments_since,
)
from live_recorder.platforms.types import CommentDraft
from live_recorder.recording.commands import (
    ffmpeg_hls_args,
    pick_tiktok_pull_url,
    streamlink_tiktok_args,
    streamlink_twitcasting_args,
    ytdlp_args,
)
from live_recorder.recording.files import (
    PathEscape,
    build_segment_path,
    relative_to_root,
    resolve_stored,
)
from live_recorder.recording.finalize import discard_obsolete, finalize_recording
from live_recorder.recording.policy import (
    ACCESS_DENIED,
    INTERRUPTED,
    OFFLINE,
    classify_output,
    leave_recording_open,
    next_action,
    recording_result,
)
from live_recorder.recording.process import run_process
from live_recorder.recording.retitle import (
    StagedRetitle,
    publish_retitle,
    rewrite_stored_path,
    rollback_retitle,
    stage_retitle,
)
from live_recorder.speech.queue import assign_speech_queue
from live_recorder.speech.srt import stored_speech_srt
from live_recorder.timeutil import utcnow

log = logging.getLogger(__name__)
SENTINEL = object()


def comment_job_result(
    *,
    outcome: str,
    stored_count: int,
    has_gaps: bool,
    writer_failed: bool,
    dropped: int = 0,
) -> tuple[CommentJobStatus, str | None]:
    """収集の終わり方から終端状態を決める。書き込み失敗や取りこぼしは成功扱いにしない。"""
    missed = has_gaps or dropped > 0
    if writer_failed and outcome in {"stopped", "ended", "offline"}:
        outcome = "comment_writer_failed"
    if outcome == "access_denied":
        return CommentJobStatus.FAILED, "restricted or age restricted"
    if stored_count == 0 and not missed and outcome not in {"stopped", "ended", "offline"}:
        return CommentJobStatus.FAILED, outcome[:2000]
    if missed or outcome not in {"stopped", "ended", "offline"}:
        status = CommentJobStatus.PARTIAL if stored_count or missed else CommentJobStatus.FAILED
        error = outcome[:2000] if outcome not in {"stopped", "ended", "offline"} else None
        return status, error
    return CommentJobStatus.COMPLETED, None


def comment_gaps_for_job(gaps, job) -> list:
    """この収集の期間と重なるコメント欠損を見る。開始がジョブより前でも、終わりが後なら対象にする。"""
    started = getattr(job, "started_at", None)
    chosen = []
    for gap in gaps:
        if getattr(gap, "job_type", None) != "comments":
            continue
        if started is None:
            chosen.append(gap)
            continue
        ended = getattr(gap, "ended_at", None)
        if ended is None or ended >= started:
            chosen.append(gap)
    return chosen


def next_comment_wait(attempt: int, error: str | None, block_remaining: float) -> float:
    """確認失敗の待ち時間。出口の拒否は休止が明けるまで待つ。"""
    delay = min(120.0, backoff_seconds(attempt))
    if pauses_tiktok_polling(error):
        return max(delay, block_remaining)
    return delay


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
        self.writer_failed = False

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


async def _resolve_official_hls(
    tc: TwitCastingClient, user_id: str, quality: str
) -> tuple[str | None, EngineRun | None]:
    """プレイリストが開いてから録画を始める。開けないときはセグメントを作らない。"""
    url, reason = await tc.open_live_hls(user_id, quality)
    if url:
        return url, None
    if reason == "access_denied":
        return None, EngineRun(ACCESS_DENIED, 0, "this live is protected")
    return None, EngineRun(INTERRUPTED, 0, reason or "tc-hls missing")


async def run_engine(
    *,
    settings: Settings,
    name: str,
    platform: str,
    user_id: str,
    quality: str,
    output: Path,
    stop: asyncio.Event,
    playlist: str | None = None,
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
            if not playlist:
                return EngineRun(INTERRUPTED, 0, "tc-hls missing")
            args = ffmpeg_hls_args(
                settings.ffmpeg_path,
                playlist,
                str(output),
                user_agent=TWITCASTING_USER_AGENT,
                referer=TWITCASTING_REFERER,
            )
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
            if job is None or stream is None or job.cancel_requested or stream.status == "ENDED":
                stop.set()
                return
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except TimeoutError:
            continue


async def run_recording_job(settings: Settings, db: Database, tc: TwitCastingClient, job_id: int, stop: asyncio.Event) -> None:
    watcher = asyncio.create_task(_watch_stop(db, stop, job_id, await _session_id(db, job_id)))
    saw_failure = ""
    open_retitles: list[StagedRetitle] = []
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
            path: Path | None = None
            segment_id = 0
            playlist: str | None = None
            early: EngineRun | None = None
            async with db.session() as session:
                repo = Repository(session)
                job = await repo.get_recording_job(job_id)
                runtime = await load_runtime(session)
                if job is None or job.cancel_requested:
                    break
                if job.status == RecordingStatus.RECONNECTING.value:
                    repo.move_recording(job, RecordingStatus.STARTING)
                current = await repo.get_session(session_id)
                title = current.title if current and current.title else None
                need_title = platform == "tiktok" and not title and not title_lookup_done
                quality = runtime.recording_quality
                timezone_name = runtime.display_timezone
                await session.commit()
            if need_title:
                title_lookup_done = True
                if settings.sign_api_key:
                    os.environ["SIGN_API_KEY"] = settings.sign_api_key
                info = await lookup_room(user_id)
                if info is not None:
                    matched = await _remember_room(db, session_id, None, info)
                    if not matched:
                        saw_failure = "room_changed"
                        last_kind = INTERRUPTED
                        break
                continue
            if engine == "official-hls" and platform == "twitcasting":
                playlist, early = await _resolve_official_hls(tc, user_id, quality)
            if early is None:
                async with db.session() as session:
                    repo = Repository(session)
                    locked = await repo.lock_session(session_id)
                    if locked is None or locked.status == "ENDED":
                        break
                    job = await repo.get_recording_job(job_id)
                    if job is None or job.cancel_requested:
                        break
                    number = await repo.next_segment_number(job_id)
                    now = utcnow()
                    from zoneinfo import ZoneInfo

                    local = now.astimezone(ZoneInfo(timezone_name))
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
                    await repo.close_open_gaps(session_id, "recording", now)
                    segment_id = segment.id
                    repo.move_recording(job, RecordingStatus.RECORDING)
                    job.recorder_engine = engine
                    await session.commit()
                    opened = True
                if not opened or path is None:
                    break
                outcome = await run_engine(
                    settings=settings,
                    name=engine,
                    platform=platform,
                    user_id=user_id,
                    quality=quality,
                    output=path,
                    stop=stop,
                    playlist=playlist,
                )
            else:
                outcome = early
            last_kind = outcome.kind
            saw_failure = outcome.error
            final_path = path
            validation = "invalid"
            container = None
            duration = None
            size = outcome.size
            has_media = False
            obsolete = None
            if path is not None and path.is_file() and path.stat().st_size > 0:
                try:
                    final = await finalize_recording(settings.ffmpeg_path, settings.ffprobe_path, path)
                except Exception:
                    log.exception("finalize failed for %s", path)
                    final_path = path
                    validation = "invalid"
                    has_media = False
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = outcome.size
                else:
                    final_path = final.path
                    validation = final.validation
                    container = final.container
                    duration = final.duration_ms
                    size = final.size
                    has_media = final.has_media
                    obsolete = final.obsolete
            drop_segment = opened and size == 0
            async with db.session() as session:
                repo = Repository(session)
                job = await repo.get_recording_job(job_id)
                # 配信行を先にロックしてからセグメントを更新する。削除と逆順にならないようにする。
                current = await repo.lock_session(session_id)
                if opened and not drop_segment and final_path is not None:
                    segment = await session.get(
                        __import__("live_recorder.db.models", fromlist=["RecordingSegment"]).RecordingSegment,
                        segment_id,
                    )
                    if segment is not None:
                        segment.finished_at = utcnow()
                        segment.file_path = relative_to_root(settings.recordings_dir, final_path)
                        segment.file_size = size
                        segment.duration_ms = duration
                        segment.container_format = container
                        segment.validation_status = validation
                        speech_runtime = await load_runtime(session)
                        assign_speech_queue(
                            segment,
                            enabled=speech_runtime.speech_subtitles_enabled,
                            has_media=has_media,
                        )
                    if current is not None and current.title:
                        await _retitle_finished_segments(
                            repo, settings.recordings_dir, session_id, job_id, current.title, open_retitles
                        )
                elif drop_segment:
                    segment = await session.get(
                        __import__("live_recorder.db.models", fromlist=["RecordingSegment"]).RecordingSegment,
                        segment_id,
                    )
                    if segment is not None:
                        await session.delete(segment)
                if job is not None:
                    if outcome.kind == ACCESS_DENIED or (outcome.kind == INTERRUPTED and size < 64 * 1024):
                        await repo.mark_engine(engine, ok=False, reason=outcome.error or outcome.kind)
                    elif size >= 64 * 1024:
                        await repo.mark_engine(engine, ok=True)
                await _commit_and_publish(session, open_retitles)
            if drop_segment and path is not None:
                path.unlink(missing_ok=True)
            discard_obsolete(obsolete)
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
        stamp_recording = False
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_recording_job(job_id)
            if job is None:
                return
            stream = await repo.lock_session(session_id)
            if stream is not None and stream.title:
                await _retitle_finished_segments(
                    repo, settings.recordings_dir, session_id, job_id, stream.title, open_retitles
                )
            if leave_recording_open(
                stopped=stop.is_set(),
                cancel_requested=job.cancel_requested,
                stream_status=stream.status if stream is not None else None,
            ):
                if job.status in {RecordingStatus.STARTING.value, RecordingStatus.RECORDING.value}:
                    repo.move_recording(job, RecordingStatus.RECONNECTING)
                await _commit_and_publish(session, open_retitles)
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
            await repo.close_open_gaps(session_id, "recording", utcnow())
            repo.finish_recording(job, target, None if target == RecordingStatus.COMPLETED else saw_failure)
            stamp_recording = stream is not None and has_media
            severity = "info" if target in {RecordingStatus.COMPLETED, RecordingStatus.CANCELLED, RecordingStatus.PARTIAL} else "error"
            event = "recording_finished" if target != RecordingStatus.FAILED else "recording_failed"
            await repo.add_event(event, f"録画を {target.value} で終了", severity=severity, session_id=session_id)
            await _commit_and_publish(session, open_retitles)
        if stamp_recording:
            try:
                await _stamp_last_recording(db, session_id)
            except Exception:
                log.exception("last recording stamp failed for session %s", session_id)
    except Exception:
        log.exception("recording job %s crashed", job_id)
        await _reconcile_retitles(db, settings.recordings_dir, open_retitles)
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_recording_job(job_id)
            if job is not None and job.status not in {
                RecordingStatus.COMPLETED.value,
                RecordingStatus.PARTIAL.value,
                RecordingStatus.CANCELLED.value,
                RecordingStatus.FAILED.value,
            }:
                repo.finish_recording(job, RecordingStatus.FAILED, "runner crashed")
                await repo.close_open_gaps(job.stream_session_id, "recording", utcnow())
                await repo.add_event(
                    "recording_failed",
                    "録画処理が異常終了しました",
                    severity="error",
                    session_id=job.stream_session_id,
                )
                await session.commit()
    finally:
        await _reconcile_retitles(db, settings.recordings_dir, open_retitles)
        watcher.cancel()


async def _session_id(db: Database, job_id: int) -> int:
    async with db.session() as session:
        job = await Repository(session).get_recording_job(job_id)
        if job is None:
            return 0
        return job.stream_session_id


async def _flush_comments(db: Database, session_id: int, job_id: int, drafts: list[CommentDraft], dropped: int) -> bool:
    if not drafts and dropped == 0:
        return False
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
        duplicates = 0
        failed = False
        try:
            async with session.begin_nested():
                stored = await repo.insert_comments(rows)
                duplicates = max(0, len(rows) - stored)
        except Exception:
            log.exception("comment insert failed")
            dropped += len(drafts)
            stored = 0
            duplicates = 0
            failed = True
        job = await repo.get_comment_job(job_id)
        if job is not None:
            job.received_count += len(drafts)
            job.stored_count += stored
            job.duplicate_count += duplicates
            job.error_count += dropped
            if drafts:
                job.last_received_at = drafts[-1].received_at
        await session.commit()
        return failed


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
            try:
                sink.writer_failed = await _flush_comments(db, session_id, job_id, batch, dropped) or sink.writer_failed
            except Exception:
                log.exception("comment batch failed")
                sink.writer_failed = True
            batch = []
    if batch or sink.dropped:
        try:
            sink.writer_failed = (
                await _flush_comments(db, session_id, job_id, batch, sink.dropped) or sink.writer_failed
            )
        except Exception:
            log.exception("comment batch failed")
            sink.writer_failed = True
        sink.dropped = 0


async def _remember_room(db: Database, session_id: int, room_id: str | None, info: dict | None) -> bool:
    """空の部屋 ID は埋める。別の配信の ID は書かず、混ぜない。"""
    stream_id, title, started = room_fields(info)
    chosen = room_id or stream_id
    async with db.session() as session:
        stream = await Repository(session).get_session(session_id)
        if stream is None:
            return False
        if chosen and stream.platform_stream_id and stream.platform_stream_id != chosen:
            return False
        if chosen and not stream.platform_stream_id:
            stream.platform_stream_id = chosen
        if title and not stream.title:
            stream.title = title
        if started and not stream.platform_started_at:
            stream.platform_started_at = started
        await session.commit()
    return True


async def _collect_tiktok(
    user_id: str,
    stop: asyncio.Event,
    sink: CommentSink,
    sign_api_key: str,
    on_room,
    checks: TikTokChecks,
    on_unverified=None,
) -> str:
    if sign_api_key:
        os.environ["SIGN_API_KEY"] = sign_api_key
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent

    client = TikTokLiveClient(unique_id=user_id)
    gate = asyncio.Lock()
    room_open = False
    attempt = 0
    held: list[CommentDraft] = []

    @client.on(ConnectEvent)
    async def on_connect(event: object) -> None:
        nonlocal room_open
        started = attempt
        room_id = getattr(event, "room_id", None)
        info = getattr(client, "room_info", None)
        try:
            matched = await on_room(str(room_id) if room_id else None, info if isinstance(info, dict) else None)
        except Exception:
            log.exception("tiktok room check failed for %s", user_id)
            matched = None
        disconnect = False
        async with gate:
            if started != attempt:
                return
            if matched is True:
                room_open = True
                pending = list(held)
                held.clear()
                for draft in pending:
                    await sink.add(draft)
                return
            # 不一致は打ち切り。照合不能はゲートを開けず再接続する（未確認コメントは入れない）。
            held.clear()
            if matched is False:
                stop.set()
                return
            sink.reconnects += 1
            disconnect = True
        if disconnect:
            if on_unverified is not None:
                try:
                    await on_unverified("room_check_failed")
                except Exception:
                    log.exception("tiktok room check gap failed for %s", user_id)
            try:
                await client.disconnect()
            except Exception:
                pass

    @client.on(CommentEvent)
    async def on_comment(event: object) -> None:
        draft = comment_from_event(event)
        if draft is None:
            return
        async with gate:
            if not room_open:
                if len(held) < 500:
                    held.append(draft)
                return
        await sink.add(draft)

    while not stop.is_set():
        observation = await checks.check(user_id)
        if observation.status == LiveStatus.OFFLINE:
            return "offline"
        if observation.status != LiveStatus.LIVE:
            sink.reconnects += 1
            detail = observation.error or "check_failed"
            if sink.reconnects > sink.max_reconnects:
                return detail
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    next_comment_wait(sink.reconnects, observation.error, checks.block_remaining()),
                )
            except TimeoutError:
                continue
            continue
        async with gate:
            attempt += 1
            room_open = False
            held.clear()
        connect_task = asyncio.create_task(client.connect(fetch_room_info=True))
        stop_task = asyncio.create_task(stop.wait())
        await asyncio.wait({connect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            connect_task.cancel()
            try:
                await client.disconnect()
            except Exception:
                pass
            try:
                await connect_task
            except (asyncio.CancelledError, Exception):
                pass
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
    on_gap,
) -> str:
    last_id: str | None = None
    last_total: int | None = None
    first = True
    while not stop.is_set():
        drafts, total, error, truncated = await fetch_comments_since(
            tc, movie_id, last_id, last_total=last_total
        )
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
            await on_gap("comments_before_collector")
        first = False
        if truncated:
            await on_gap("comments_page_truncated")
        numeric = [
            int(item.platform_comment_id)
            for item in drafts
            if item.platform_comment_id and item.platform_comment_id.isdigit()
        ]
        if numeric:
            last_id = str(max(numeric))
        if total is not None:
            last_total = total
        for draft in drafts:
            await sink.add(draft)
        try:
            await asyncio.wait_for(stop.wait(), poll_seconds)
        except TimeoutError:
            continue
    return "stopped"


async def run_comment_job(
    settings: Settings,
    db: Database,
    tc: TwitCastingClient,
    job_id: int,
    stop: asyncio.Event,
    checks: TikTokChecks | None = None,
) -> None:
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
    writer_failed = False
    tiktok_checks = checks if checks is not None else TikTokChecks()
    try:
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_comment_job(job_id)
            if job is not None:
                repo.move_comments(job, CommentJobStatus.COLLECTING)
                await repo.close_open_gaps(session_id, "comments", utcnow())
                await session.commit()
        async def mark_gap(reason: str) -> None:
            async with db.session() as session:
                repo = Repository(session)
                stream = await repo.get_session(session_id)
                started = stream.detected_at if stream else utcnow()
                await repo.add_gap(session_id, "comments", started, utcnow(), reason)
                await session.commit()

        if platform == "tiktok":
            outcome = await _collect_tiktok(
                user_id,
                stop,
                sink,
                settings.sign_api_key,
                lambda room_id, info: _remember_room(db, session_id, room_id, info),
                tiktok_checks,
                on_unverified=mark_gap,
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
                outcome = await _collect_twitcasting(
                    tc, movie_id, stop, sink, runtime.comment_poll_seconds, mark_gap
                )
    except Exception as exc:
        log.exception("comment job %s crashed", job_id)
        outcome = type(exc).__name__
    finally:
        await queue.put(SENTINEL)
        try:
            await writer
        except Exception:
            log.exception("comment writer failed")
            writer_failed = True
        watcher.cancel()
    async with db.session() as session:
        repo = Repository(session)
        await repo.lock_session(session_id)
        job = await repo.get_comment_job(job_id)
        if job is None:
            return
        job.reconnect_count = sink.reconnects
        gaps = comment_gaps_for_job(await repo.gaps_for_session(session_id), job)
        target, error = comment_job_result(
            outcome=outcome,
            stored_count=job.stored_count,
            has_gaps=bool(gaps),
            writer_failed=writer_failed or sink.writer_failed,
            dropped=job.error_count,
        )
        if error:
            job.last_error = error
        try:
            repo.move_comments(job, target)
        except Exception:
            job.status = target.value
        job.finished_at = utcnow()
        await repo.close_open_gaps(session_id, "comments", utcnow())
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
    body, _media = encode_export(rows)
    path.write_bytes(body)


async def _refresh_ass(settings: Settings, video: Path, speech_relative: str | None) -> None:
    try:
        await asyncio.to_thread(
            refresh_combined_ass,
            video,
            ffprobe=settings.ffprobe_path,
            speech_srt=stored_speech_srt(settings.recordings_dir, speech_relative),
        )
    except Exception:
        log.exception("combined ass failed")


async def _auto_export(settings: Settings, repo: Repository, session_id: int, job, tz_name: str) -> None:
    """配信行をロックしてから書き出す。削除と同時に字幕を書き戻さない。"""
    try:
        stream = await repo.lock_session(session_id)
        if stream is None:
            return
        stored = [segment for segment in await repo.segments_for_session(session_id) if not segment.file_removed]
        windows = [
            ExportSegment(
                id=segment.id,
                number=segment.segment_number,
                file_name=Path(segment.file_path).name,
                started_at=segment.started_at,
                ended_at=segment_end(segment.started_at, segment.duration_ms, segment.finished_at),
            )
            for segment in stored
        ]
        written: list[Path] = []
        if windows:
            comments = await repo.export_comments_for_segments(session_id, windows)
            groups = partition_by_segment(
                comments,
                windows,
                lambda comment: comment.posted_at or comment.received_at,
            )
            by_id = {segment.id: segment for segment in stored}
            for window, grouped in groups:
                segment = by_id[window.id]
                video = resolve_stored(settings.recordings_dir, segment.file_path)
                target = video.with_name(comment_export_name(window.file_name))
                if not grouped:
                    target.unlink(missing_ok=True)
                    await _refresh_ass(settings, video, segment.speech_srt_path)
                    continue
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
                await _refresh_ass(settings, video, segment.speech_srt_path)
                written.append(target)
        else:
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
            rows = [
                _comment_row(comment, tz_name, stream, comment.recording_offset_ms) for comment in comments
            ]
            target = settings.recordings_dir / "exports" / f"session-{session_id}_Comments.srt"
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


def _release_retitles(staged: list[StagedRetitle], *, publish: bool) -> None:
    action = publish_retitle if publish else rollback_retitle
    for item in staged:
        action(item)
    staged.clear()


async def _reconcile_retitles(db: Database, root: Path, staged: list[StagedRetitle]) -> None:
    """コミット成否が不明なとき、DB の参照先を見て公開か取り消しを決める。照合不能なら両方残す。"""
    if not staged:
        return
    pending = list(staged)
    staged.clear()
    try:
        async with db.session() as session:
            refs = await Repository(session).recorded_path_refs()
    except Exception:
        log.exception("retitle reconcile lookup failed; keeping both names")
        return
    for item in pending:
        try:
            dest_rel = relative_to_root(root, item.dest)
            source_rel = relative_to_root(root, item.source)
        except PathEscape:
            log.exception("retitle reconcile path failed for %s", item.dest)
            continue
        if dest_rel in refs:
            publish_retitle(item)
        elif source_rel in refs:
            rollback_retitle(item)
        else:
            log.warning("retitle ambiguous for %s / %s; keeping both", source_rel, dest_rel)


async def _commit_and_publish(session, staged: list[StagedRetitle]) -> None:
    """DB が新しいパスを指したら、すぐ古い名前を消す。コミット後に取り消さない。"""
    await session.commit()
    # commit が返った時点で DB は新パス。取り消し対象から外してから古い名前を消す。
    pending = list(staged)
    staged.clear()
    for item in pending:
        publish_retitle(item)


async def _stamp_last_recording(db: Database, session_id: int) -> None:
    """配信行ロックと別に最終録画時刻を残す。監視の streamer 更新とデッドロックしない。"""
    async with db.session() as session:
        stream = await Repository(session).get_session(session_id)
        if stream is None or stream.streamer is None:
            return
        stream.streamer.last_recording_at = utcnow()
        await session.commit()


async def _stage_retitle(path: Path, title: str | None, sink: list[StagedRetitle]) -> StagedRetitle | None:
    """付け直しを別スレッドで行う。キャンセルされても作成物を sink に残す。"""
    box: list[StagedRetitle | None] = [None]
    finished = threading.Event()

    def run() -> StagedRetitle | None:
        try:
            staged = stage_retitle(path, title)
            box[0] = staged
            if staged is not None:
                sink.append(staged)
            return staged
        finally:
            finished.set()

    try:
        return await asyncio.to_thread(run)
    except asyncio.CancelledError:
        # 3.11 以降はキャンセル中の await がすぐ再送出するので、いったん解除してスレッドを待つ。
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        await asyncio.to_thread(finished.wait)
        raise


async def _point_exports_at(repo: Repository, session_id: int, staged: StagedRetitle) -> None:
    for comment_job in await repo.comment_jobs_for_session(session_id):
        comment_job.export_path = rewrite_stored_path(staged, comment_job.export_path)


async def _retitle_finished_segments(
    repo: Repository,
    root: Path,
    session_id: int,
    job_id: int,
    title: str,
    sink: list[StagedRetitle],
) -> None:
    """閉じたセグメントの「無題」を配信タイトルへ付け直す。文字起こし中のファイルは動かさない。

    呼び出し側は配信行をロックしておく。セグメント行もロックするので、コミットまで
    文字起こしが古いパスを取得することはない。作った名前はすぐ sink に載せる。
    """
    for segment in await repo.lock_segments_for_job(job_id):
        if segment.file_removed or segment.speech_status == "running" or segment.finished_at is None:
            continue
        try:
            path = resolve_stored(root, segment.file_path)
        except ValueError:
            continue
        staged = await _stage_retitle(path, title, sink)
        if staged is None:
            continue
        segment.file_path = rewrite_stored_path(staged, segment.file_path) or segment.file_path
        segment.speech_srt_path = rewrite_stored_path(staged, segment.speech_srt_path)
        await _point_exports_at(repo, session_id, staged)


async def close_recording_if_idle(
    settings: Settings,
    db: Database,
    job_id: int,
    target: RecordingStatus,
    *,
    finish: bool = True,
    reason: str | None = None,
    from_media: bool = False,
) -> None:
    obsolete: list[Path] = []
    open_retitles: list[StagedRetitle] = []
    try:
        async with db.session() as session:
            repo = Repository(session)
            job = await repo.get_recording_job(job_id)
            if job is None:
                return
            # 配信行を先にロックしてからセグメントを更新する。履歴削除とデッドロックしない。
            stream = await repo.lock_session(job.stream_session_id)
            if stream is None:
                return
            speech_enabled = (await load_runtime(session)).speech_subtitles_enabled
            title = stream.title
            for segment in await repo.lock_segments_for_job(job_id):
                if segment.finished_at is not None or segment.file_removed:
                    continue
                has_media = False
                try:
                    path = resolve_stored(settings.recordings_dir, segment.file_path)
                except ValueError:
                    segment.finished_at = utcnow()
                    segment.validation_status = "invalid"
                    assign_speech_queue(segment, enabled=speech_enabled, has_media=False)
                    continue
                if path.is_file() and path.stat().st_size > 0:
                    try:
                        final = await finalize_recording(settings.ffmpeg_path, settings.ffprobe_path, path)
                    except Exception:
                        log.exception("finalize failed for %s", segment.file_path)
                        segment.finished_at = utcnow()
                        segment.validation_status = "invalid"
                        assign_speech_queue(segment, enabled=speech_enabled, has_media=False)
                        continue
                    segment.file_path = relative_to_root(settings.recordings_dir, final.path)
                    segment.file_size = final.size
                    segment.duration_ms = final.duration_ms
                    segment.container_format = final.container
                    segment.validation_status = final.validation
                    has_media = final.has_media
                    if final.obsolete is not None:
                        obsolete.append(final.obsolete)
                else:
                    segment.validation_status = "invalid"
                    segment.file_size = 0
                segment.finished_at = utcnow()
                assign_speech_queue(segment, enabled=speech_enabled, has_media=has_media)
            if title:
                await _retitle_finished_segments(
                    repo, settings.recordings_dir, job.stream_session_id, job_id, title, open_retitles
                )
            if finish and job.status not in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
                chosen = target
                if from_media:
                    has_media = await repo.any_valid_segment(job_id)
                    chosen = RecordingStatus.PARTIAL if has_media else RecordingStatus.FAILED
                if reason is None:
                    reason = None if chosen == RecordingStatus.COMPLETED else "worker restarted"
                repo.finish_recording(job, chosen, reason)
                await repo.close_open_gaps(job.stream_session_id, "recording", utcnow())
            await _commit_and_publish(session, open_retitles)
    except BaseException:
        await _reconcile_retitles(db, settings.recordings_dir, open_retitles)
        raise
    for path in obsolete:
        discard_obsolete(path)
