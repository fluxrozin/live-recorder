from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import time
from datetime import timedelta
from pathlib import Path

from live_recorder.comments.segments import comment_sidecar_relatives
from live_recorder.config import Settings
from live_recorder.db.repository import Repository
from live_recorder.db.runtime import ensure_runtime, load_runtime
from live_recorder.db.session import Database
from live_recorder.domain.enums import CommentJobStatus, LiveStatus, RecordingStatus
from live_recorder.monitor.jobs import close_recording_if_idle, run_comment_job, run_recording_job
from live_recorder.monitor.planner import effective_interval, plan_observation
from live_recorder.platforms.tiktok import check_tiktok
from live_recorder.platforms.twitcasting import TwitCastingClient
from live_recorder.platforms.types import LiveObservation
from live_recorder.recording.files import resolve_stored
from live_recorder.recording.finalize import allow_new_recording, disk_free
from live_recorder.recording.process import command_output
from live_recorder.timeutil import utcnow
from live_recorder.versions import tiktoklive_status, tool_version

log = logging.getLogger(__name__)
HEARTBEAT_FILE = Path("/tmp/live-recorder-worker-heartbeat")


def worker_start_notice(recordings: int, comments: int) -> tuple[str, str]:
    """起動そのものは情報。未完了の収集が残っているときだけ警告にする。"""
    parts: list[str] = []
    if recordings:
        parts.append(f"録画 {recordings} 件")
    if comments:
        parts.append(f"コメント {comments} 件")
    if not parts:
        return "ワーカーを起動しました", "info"
    return f"未完了の{'、'.join(parts)}がある状態でワーカーを起動しました", "warning"


class Supervisor:
    def __init__(self, settings: Settings, db: Database, tc: TwitCastingClient) -> None:
        self.settings = settings
        self.db = db
        self.tc = tc
        self.stop = asyncio.Event()
        self.tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.events: dict[tuple[str, int], asyncio.Event] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.next_ok: dict[int, float] = {}
        self.failures: dict[int, int] = {}
        self.pollers: dict[str, object] = {}
        self.disk_low = False
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.warning = ""

    def request_stop(self) -> None:
        self.stop.set()

    async def run(self) -> None:
        await self.recover()
        await self.refresh_engines()
        background = [
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self.monitor_loop(), name="monitor"),
            asyncio.create_task(self.maintenance_loop(), name="maintenance"),
        ]
        await self.stop.wait()
        for task in background:
            task.cancel()
        for event in self.events.values():
            event.set()
        if self.tasks:
            _done, pending = await asyncio.wait(set(self.tasks.values()), timeout=self.settings.shutdown_timeout_seconds)
            for task in pending:
                task.cancel()
        await self._heartbeat("stopped")
        log.info("worker stopped")

    async def recover(self) -> None:
        now = utcnow()
        async with self.db.session() as session:
            repo = Repository(session)
            await ensure_runtime(session)
            recordings = await repo.unfinished_jobs()
            comments = await repo.unfinished_comment_jobs()
            message, severity = worker_start_notice(len(recordings), len(comments))
            await repo.add_event("worker_restart", message, severity=severity)
            for job in recordings:
                await repo.add_gap(job.stream_session_id, "recording", job.started_at or now, now, "worker_restart")
                if job.status != RecordingStatus.FINALIZING.value:
                    try:
                        repo.move_recording(job, RecordingStatus.RECONNECTING)
                    except Exception:
                        log.info("could not mark recording %s reconnecting", job.id, exc_info=True)
            for job in comments:
                await repo.add_gap(job.stream_session_id, "comments", job.started_at or now, now, "worker_restart")
                if job.status != CommentJobStatus.PENDING.value:
                    try:
                        repo.move_comments(job, CommentJobStatus.RECONNECTING)
                    except Exception:
                        log.info("could not mark comments %s reconnecting", job.id, exc_info=True)
            await session.commit()
        async with self.db.session() as session:
            segments = await Repository(session).unfinished_segments()
            job_ids = {segment.recording_job_id for segment in segments}
        for job_id in job_ids:
            await close_recording_if_idle(
                self.settings, self.db, job_id, RecordingStatus.PARTIAL, finish=False
            )

    async def refresh_engines(self) -> None:
        streamlink = await command_output([self.settings.streamlink_path, "--version"])
        ytdlp = await command_output([self.settings.ytdlp_path, "--version"])
        ffmpeg = tool_version(await command_output([self.settings.ffmpeg_path, "-version"]))
        ffprobe = tool_version(await command_output([self.settings.ffprobe_path, "-version"]))
        tiktok_version, tiktok_ok = tiktoklive_status()
        async with self.db.session() as session:
            repo = Repository(session)
            await repo.upsert_engine("streamlink", version=streamlink, available=bool(streamlink))
            await repo.upsert_engine("yt-dlp", version=ytdlp, available=bool(ytdlp))
            await repo.upsert_engine("tiktoklive", version=tiktok_version, available=tiktok_ok)
            await repo.upsert_engine("ffmpeg", version=ffmpeg, available=bool(ffmpeg))
            await repo.upsert_engine("ffprobe", version=ffprobe, available=bool(ffprobe))
            await session.commit()

    async def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            await self._heartbeat("running")
            try:
                await asyncio.wait_for(self.stop.wait(), 15)
            except TimeoutError:
                continue

    async def _heartbeat(self, status: str) -> None:
        try:
            HEARTBEAT_FILE.write_text(utcnow().isoformat(), encoding="utf-8")
        except OSError:
            log.info("heartbeat file was not written")
        try:
            async with self.db.session() as session:
                await Repository(session).heartbeat(self.worker_id, status, self.warning or None)
                await session.commit()
        except Exception:
            log.exception("heartbeat failed")

    async def monitor_loop(self) -> None:
        await self._heartbeat("running")
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                async with self.db.session() as session:
                    runtime = await load_runtime(session)
                    streamers = await Repository(session).monitored_streamers()
                logging.getLogger().setLevel(runtime.log_level)
                tc_count = sum(1 for streamer in streamers if streamer.platform == "twitcasting")
                interval, warning = effective_interval(runtime.monitor_interval_seconds, tc_count)
                self.warning = warning or ""
                results = await asyncio.gather(
                    *(self.check_one(streamer.id) for streamer in streamers),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        log.error("monitor task failed", exc_info=result)
            except Exception:
                log.exception("monitor loop failed")
                interval = 60
            elapsed = time.monotonic() - started
            pause = max(1.0, interval - elapsed) + random.uniform(0, min(5.0, interval * 0.1))
            try:
                await asyncio.wait_for(self.stop.wait(), pause)
            except TimeoutError:
                continue

    async def check_one(self, streamer_id: int) -> None:
        lock = self.locks.setdefault(streamer_id, asyncio.Lock())
        async with lock:
            if self.next_ok.get(streamer_id, 0) > time.monotonic():
                return
            async with self.db.session() as session:
                repo = Repository(session)
                streamer = await repo.get_streamer(streamer_id)
                if streamer is None:
                    return
                runtime = await load_runtime(session)
                open_session = await repo.open_session(streamer_id)
                active_count = await repo.count_active_recordings()
                rec_job = await repo.active_recording(open_session.id) if open_session else None
                com_job = await repo.active_comments(open_session.id) if open_session else None
                platform = streamer.platform
                user_id = streamer.platform_user_id
                monitoring = streamer.monitoring_enabled
                recording_enabled = streamer.recording_enabled
                comments_enabled = streamer.comments_enabled
                open_id = open_session.platform_stream_id if open_session else None
                has_open = open_session is not None
                rec_id = rec_job.id if rec_job else None
                com_id = com_job.id if com_job else None
            try:
                observation = await self.observe(platform, user_id)
            except Exception as exc:
                log.info("observation failed for %s: %s", user_id, type(exc).__name__)
                observation = LiveObservation(status=LiveStatus.ERROR, error=type(exc).__name__)
            if observation.status in {LiveStatus.ERROR, LiveStatus.UNKNOWN}:
                self.failures[streamer_id] = self.failures.get(streamer_id, 0) + 1
                from live_recorder.monitor.planner import backoff_seconds

                self.next_ok[streamer_id] = time.monotonic() + backoff_seconds(self.failures[streamer_id])
            else:
                self.failures[streamer_id] = 0
            free = disk_free(self.settings.recordings_dir)
            disk_ok = allow_new_recording(free, runtime.min_free_bytes)
            if not disk_ok and not self.disk_low:
                await self._event("disk_low", "空き容量が下限を下回っています", severity="warning")
            self.disk_low = not disk_ok
            slots_left = runtime.max_concurrent_recordings - active_count
            if rec_id and ("rec", rec_id) not in self.tasks:
                slots_left += 1
            plan = plan_observation(
                monitoring_enabled=monitoring,
                recording_enabled=recording_enabled,
                comments_enabled=comments_enabled,
                comments_global=runtime.comments_globally_enabled,
                observation=observation,
                has_open_session=has_open,
                open_stream_id=open_id,
                recording_active=("rec", rec_id) in self.tasks if rec_id else False,
                comments_active=("com", com_id) in self.tasks if com_id else False,
                recording_slots_left=slots_left,
                disk_ok=disk_ok,
            )
            await self.apply(streamer_id, observation, plan)

    async def observe(self, platform: str, user_id: str) -> LiveObservation:
        if platform == "tiktok":
            client = self.pollers.get(user_id)
            if client is None:
                from TikTokLive import TikTokLiveClient

                client = TikTokLiveClient(unique_id=user_id)
                self.pollers[user_id] = client
            return await check_tiktok(user_id, client=client)
        return await self.tc.current_live(user_id)

    async def apply(self, streamer_id: int, observation: LiveObservation, plan) -> None:
        start_recording = False
        start_comments = False
        session_id: int | None = None
        async with self.db.session() as session:
            repo = Repository(session)
            streamer = await repo.get_streamer(streamer_id)
            if streamer is None:
                return
            streamer.live_status = plan.live_status
            streamer.last_checked_at = utcnow()
            streamer.last_error = observation.error
            if observation.status == LiveStatus.LIVE:
                streamer.last_live_at = utcnow()
            open_session = await repo.open_session(streamer_id)
            if plan.session in {"end", "replace"} and open_session is not None:
                await self._end_session(repo, open_session.id)
                open_session = None if plan.session == "replace" else await repo.get_session(open_session.id)
            if plan.session in {"open", "replace"} and observation.status == LiveStatus.LIVE:
                created = await repo.create_session(
                    streamer_id=streamer_id,
                    platform_stream_id=observation.platform_stream_id,
                    title=observation.title,
                    platform_started_at=observation.platform_started_at,
                    detected_at=utcnow(),
                    last_seen_live_at=utcnow(),
                    status="LIVE",
                    restricted=observation.restricted,
                )
                await repo.add_event(
                    "live_detected",
                    f"{streamer.platform}:{streamer.platform_user_id} の配信を検知",
                    session_id=created.id,
                )
                open_session = created
            elif plan.session == "keep" and open_session is not None and observation.status == LiveStatus.LIVE:
                open_session.last_seen_live_at = utcnow()
                if observation.platform_stream_id and not open_session.platform_stream_id:
                    open_session.platform_stream_id = observation.platform_stream_id
                if observation.title and not open_session.title:
                    open_session.title = observation.title
                if observation.platform_started_at and not open_session.platform_started_at:
                    open_session.platform_started_at = observation.platform_started_at
                open_session.restricted = observation.restricted
            if plan.note == "restricted" and open_session is not None:
                await repo.add_event(
                    "live_detected",
                    "合言葉またはメンバーシップ配信のため録画とコメント収集を行いません",
                    severity="warning",
                    session_id=open_session.id,
                )
            await session.commit()
            if open_session is not None and open_session.status == "LIVE":
                session_id = open_session.id
                start_recording = plan.start_recording
                start_comments = plan.start_comments
        if session_id and start_recording:
            await self._ensure_recording(session_id)
        if session_id and start_comments:
            await self._ensure_comments(session_id)

    async def _end_session(self, repo: Repository, session_id: int) -> None:
        stream = await repo.get_session(session_id)
        if stream is None or stream.status == "ENDED":
            return
        rec = await repo.active_recording(session_id)
        com = await repo.active_comments(session_id)
        stream.status = "ENDED"
        stream.ended_detected_at = utcnow()
        if rec is not None:
            rec.cancel_requested = True
        if com is not None:
            com.cancel_requested = True
        await repo.add_event("live_ended", "配信終了を検知しました", session_id=session_id)
        if rec is not None:
            event = self.events.get(("rec", rec.id))
            if event is not None:
                event.set()
            elif rec.status not in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
                target = RecordingStatus.PARTIAL if await repo.any_valid_segment(rec.id) else RecordingStatus.FAILED
                repo.finish_recording(rec, target, "stream ended")
        if com is not None:
            event = self.events.get(("com", com.id))
            if event is not None:
                event.set()
            elif com.status not in {"COMPLETED", "PARTIAL", "FAILED", "DISABLED"}:
                target = CommentJobStatus.PARTIAL if com.stored_count else CommentJobStatus.COMPLETED
                try:
                    repo.move_comments(com, target)
                except Exception:
                    com.status = target.value
                com.finished_at = utcnow()

    async def _ensure_recording(self, session_id: int) -> None:
        async with self.db.session() as session:
            repo = Repository(session)
            existing = await repo.active_recording(session_id)
            if existing is not None and ("rec", existing.id) in self.tasks:
                return
            if existing is not None and existing.status == RecordingStatus.FINALIZING.value:
                job_id = existing.id
                await session.commit()
                await close_recording_if_idle(self.settings, self.db, job_id, RecordingStatus.PARTIAL)
                return
            if existing is None:
                runtime = await load_runtime(session)
                if await repo.count_active_recordings() >= runtime.max_concurrent_recordings:
                    return
                if not allow_new_recording(disk_free(self.settings.recordings_dir), runtime.min_free_bytes):
                    await repo.add_event("disk_low", "容量不足のため録画を開始しません", severity="warning", session_id=session_id)
                    await session.commit()
                    return
                existing = await repo.create_recording_job(session_id)
            job_id = existing.id
            await session.commit()
        self._spawn("rec", job_id, run_recording_job)

    async def _ensure_comments(self, session_id: int) -> None:
        async with self.db.session() as session:
            repo = Repository(session)
            existing = await repo.active_comments(session_id)
            if existing is not None and ("com", existing.id) in self.tasks:
                return
            if existing is None:
                existing = await repo.create_comment_job(session_id)
            job_id = existing.id
            await session.commit()
        self._spawn("com", job_id, run_comment_job)

    def _spawn(self, kind: str, job_id: int, runner) -> None:
        key = (kind, job_id)
        current = self.tasks.get(key)
        if current is not None and not current.done():
            return
        event = self.events.setdefault(key, asyncio.Event())
        task = asyncio.create_task(runner(self.settings, self.db, self.tc, job_id, event))
        self.tasks[key] = task

        def _done(done: asyncio.Task, key: tuple[str, int] = key) -> None:
            self.tasks.pop(key, None)
            self.events.pop(key, None)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                log.error("job %s failed", key, exc_info=error)

        task.add_done_callback(_done)

    async def maintenance_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), 3600)
                return
            except TimeoutError:
                pass
            try:
                await self.maintain()
            except Exception:
                log.exception("maintenance failed")

    async def maintain(self) -> None:
        async with self.db.session() as session:
            repo = Repository(session)
            runtime = await load_runtime(session)
            now = utcnow()
            if runtime.auto_delete_enabled and runtime.retention_days > 0:
                cutoff = now - timedelta(days=runtime.retention_days)
                for segment in await repo.expired_segments(cutoff):
                    self._unlink_recorded(segment.file_path)
                    segment.file_removed = True
                await repo.add_event("maintenance", "保存期限を過ぎた録画ファイルを削除しました")
            if runtime.comment_retention_days > 0:
                from sqlalchemy import select

                from live_recorder.db.models import CommentCollectionJob

                cutoff = now - timedelta(days=runtime.comment_retention_days)
                removed = await repo.delete_comments_before(cutoff)
                if removed:
                    jobs = await session.scalars(
                        select(CommentCollectionJob).where(CommentCollectionJob.export_path.is_not(None))
                    )
                    for job in jobs:
                        if job.export_path:
                            self._unlink_recorded(job.export_path)
                            job.export_path = None
                        for segment in await repo.segments_for_session(job.stream_session_id):
                            for relative in comment_sidecar_relatives(segment.file_path):
                                self._unlink_recorded(relative)
                    await repo.add_event("maintenance", f"期限を過ぎたコメント {removed} 件と自動エクスポートを削除しました")
            await session.commit()

    def _unlink_recorded(self, relative: str) -> None:
        try:
            path = resolve_stored(self.settings.recordings_dir, relative)
        except ValueError:
            return
        if path.is_file():
            path.unlink()

    async def _event(self, event_type: str, message: str, *, severity: str) -> None:
        async with self.db.session() as session:
            await Repository(session).add_event(event_type, message, severity=severity)
            await session.commit()
