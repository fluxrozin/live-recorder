from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from live_recorder.comments.dedupe import dedupe_key
from live_recorder.comments.segments import ordered_export_segments, segment_comment_bounds
from live_recorder.db.models import (
    CollectionGap,
    Comment,
    CommentCollectionJob,
    PendingFileDeletion,
    RecorderEngine,
    RecordingJob,
    RecordingSegment,
    Streamer,
    StreamSession,
    SystemEvent,
    User,
    WorkerHeartbeat,
)
from live_recorder.domain.enums import (
    COMMENT_ACTIVE,
    RECORDING_ACTIVE,
    CommentJobStatus,
    RecordingStatus,
    recording_terminal_path,
    transition_comments,
    transition_recording,
)
from live_recorder.speech.queue import requeue_speech
from live_recorder.timeutil import utcnow

REC_ACTIVE = tuple(status.value for status in RECORDING_ACTIVE)
COM_ACTIVE = tuple(status.value for status in COMMENT_ACTIVE)


class RepositoryError(ValueError):
    pass


def assign_sort_order(streamers: list[Streamer], ordered_ids: list[int]) -> None:
    """配信者の表示順を ordered_ids にする。現在の配信者と過不足や重複があれば拒否する。"""
    if len(ordered_ids) != len(set(ordered_ids)) or {item.id for item in streamers} != set(ordered_ids):
        raise RepositoryError("並び順が現在の配信者と一致しません")
    by_id = {item.id: item for item in streamers}
    for index, streamer_id in enumerate(ordered_ids):
        by_id[streamer_id].sort_order = index


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_users(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(User)) or 0)

    async def user_by_name(self, username: str) -> User | None:
        return await self.session.scalar(select(User).where(User.username == username))

    async def user_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def create_user(self, username: str, password_hash: str) -> User:
        user = User(username=username, password_hash=password_hash, is_active=True)
        self.session.add(user)
        await self.session.flush()
        return user

    async def list_streamers(self, *, custom: bool = False) -> list[Streamer]:
        ordering = (
            (Streamer.sort_order, Streamer.id)
            if custom
            else (Streamer.platform, Streamer.display_name, Streamer.id)
        )
        result = await self.session.scalars(select(Streamer).order_by(*ordering))
        return list(result)

    async def next_sort_order(self) -> int:
        current = await self.session.scalar(select(func.max(Streamer.sort_order)))
        return 0 if current is None else int(current) + 1

    async def monitored_streamers(self) -> list[Streamer]:
        """監視中に加え、切ったあとも開いている配信は終了処理まで巡回する。"""
        open_ids = select(StreamSession.streamer_id).where(StreamSession.status.in_(("LIVE", "ENDING")))
        result = await self.session.scalars(
            select(Streamer)
            .where(or_(Streamer.monitoring_enabled.is_(True), Streamer.id.in_(open_ids)))
            .order_by(Streamer.id)
        )
        return list(result)

    async def get_streamer(self, streamer_id: int) -> Streamer | None:
        return await self.session.get(Streamer, streamer_id)

    async def find_streamer(self, platform: str, user_id: str) -> Streamer | None:
        return await self.session.scalar(
            select(Streamer).where(Streamer.platform == platform, Streamer.platform_user_id == user_id)
        )

    async def add_streamer(self, streamer: Streamer) -> Streamer:
        self.session.add(streamer)
        await self.session.flush()
        return streamer

    async def delete_streamer(self, streamer_id: int) -> None:
        count = await self.session.scalar(
            select(func.count()).select_from(StreamSession).where(StreamSession.streamer_id == streamer_id)
        )
        if count:
            raise RepositoryError("録画履歴がある配信者は削除できません。先に履歴を削除してください")
        streamer = await self.get_streamer(streamer_id)
        if streamer is None:
            raise RepositoryError("配信者が見つかりません")
        await self.session.delete(streamer)

    async def open_session(self, streamer_id: int) -> StreamSession | None:
        return await self.session.scalar(
            select(StreamSession)
            .where(StreamSession.streamer_id == streamer_id, StreamSession.status.in_(("LIVE", "ENDING")))
            .order_by(StreamSession.id.desc())
        )

    async def create_session(self, **kwargs: object) -> StreamSession:
        session = StreamSession(**kwargs)  # type: ignore[arg-type]
        self.session.add(session)
        await self.session.flush()
        return session

    async def get_session(self, session_id: int) -> StreamSession | None:
        return await self.session.scalar(
            select(StreamSession)
            .options(selectinload(StreamSession.streamer))
            .where(StreamSession.id == session_id)
        )

    async def lock_session(self, session_id: int) -> StreamSession | None:
        """削除・ジョブ作成・字幕の公開がすれ違わないよう、配信行をロックする。

        FOR NO KEY UPDATE なので、同じ配信へのコメント INSERT（外部キー確認）は待たせない。
        """
        return await self.session.scalar(
            select(StreamSession)
            .options(selectinload(StreamSession.streamer))
            .where(StreamSession.id == session_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

    async def fresh_segment(self, segment_id: int) -> RecordingSegment | None:
        """読み込み済みの古い値を使わず、DB の今の行を読む。"""
        return await self.session.scalar(
            select(RecordingSegment)
            .where(RecordingSegment.id == segment_id)
            .execution_options(populate_existing=True)
        )

    async def deletion_blocked(self, session_id: int) -> bool:
        """録画中かコメント収集中なら True。行ロックは呼び出し側のトランザクションが終わるまで持つ。"""
        stream = await self.lock_session(session_id)
        if stream is None:
            return False
        if await self.active_recording(session_id) is not None:
            return True
        return await self.active_comments(session_id) is not None

    async def active_recording(self, session_id: int) -> RecordingJob | None:
        return await self.session.scalar(
            select(RecordingJob)
            .where(RecordingJob.stream_session_id == session_id, RecordingJob.status.in_(REC_ACTIVE))
            .order_by(RecordingJob.id.desc())
        )

    async def active_comments(self, session_id: int) -> CommentCollectionJob | None:
        return await self.session.scalar(
            select(CommentCollectionJob)
            .where(
                CommentCollectionJob.stream_session_id == session_id,
                CommentCollectionJob.status.in_(COM_ACTIVE),
            )
            .order_by(CommentCollectionJob.id.desc())
        )

    async def count_active_recordings(self) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(RecordingJob).where(RecordingJob.status.in_(REC_ACTIVE))
            )
            or 0
        )

    async def create_recording_job(self, session_id: int) -> RecordingJob:
        job = RecordingJob(stream_session_id=session_id, status=RecordingStatus.PENDING.value, retry_count=0)
        self.session.add(job)
        await self.session.flush()
        return job

    async def create_comment_job(self, session_id: int) -> CommentCollectionJob:
        job = CommentCollectionJob(stream_session_id=session_id, status=CommentJobStatus.PENDING.value)
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_recording_job(self, job_id: int) -> RecordingJob | None:
        return await self.session.get(RecordingJob, job_id)

    async def get_comment_job(self, job_id: int) -> CommentCollectionJob | None:
        return await self.session.get(CommentCollectionJob, job_id)

    def move_recording(self, job: RecordingJob, status: RecordingStatus) -> None:
        job.status = transition_recording(RecordingStatus(job.status), status).value
        job.updated_at = utcnow()

    def move_comments(self, job: CommentCollectionJob, status: CommentJobStatus) -> None:
        job.status = transition_comments(CommentJobStatus(job.status), status).value
        job.updated_at = utcnow()

    def finish_recording(self, job: RecordingJob, target: RecordingStatus, error: str | None = None) -> None:
        for step in recording_terminal_path(RecordingStatus(job.status), target):
            self.move_recording(job, step)
        job.finished_at = utcnow()
        if error:
            job.last_error = error[:2000]

    async def next_segment_number(self, job_id: int) -> int:
        current = await self.session.scalar(
            select(func.max(RecordingSegment.segment_number)).where(RecordingSegment.recording_job_id == job_id)
        )
        return int(current or 0) + 1

    async def add_segment(self, job_id: int, number: int, relative: str, started: datetime) -> RecordingSegment:
        segment = RecordingSegment(
            recording_job_id=job_id,
            segment_number=number,
            started_at=started,
            file_path=relative,
            validation_status="pending",
        )
        self.session.add(segment)
        await self.session.flush()
        return segment

    async def segments_for_job(self, job_id: int) -> list[RecordingSegment]:
        result = await self.session.scalars(
            select(RecordingSegment)
            .where(RecordingSegment.recording_job_id == job_id)
            .order_by(RecordingSegment.segment_number)
        )
        return list(result)

    async def lock_segments_for_job(self, job_id: int) -> list[RecordingSegment]:
        """文字起こしの取得と重ならないよう、セグメント行をロックして今の値を読む。"""
        result = await self.session.scalars(
            select(RecordingSegment)
            .where(RecordingSegment.recording_job_id == job_id)
            .order_by(RecordingSegment.segment_number)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(result)

    async def release_running_speech(self) -> None:
        rows = await self.session.scalars(
            select(RecordingSegment).where(RecordingSegment.speech_status == "running")
        )
        for segment in rows:
            segment.speech_status = "pending"

    async def claim_next_speech(self) -> RecordingSegment | None:
        return await self.session.scalar(
            select(RecordingSegment)
            .where(
                RecordingSegment.speech_status == "pending",
                RecordingSegment.file_removed.is_(False),
                RecordingSegment.finished_at.is_not(None),
            )
            .order_by(RecordingSegment.id)
            .limit(1)
            .options(
                selectinload(RecordingSegment.job)
                .selectinload(RecordingJob.session)
                .selectinload(StreamSession.streamer)
            )
            .with_for_update(skip_locked=True)
        )

    async def finish_speech(
        self,
        segment_id: int,
        *,
        status: str,
        srt_path: str | None,
        error: str | None,
        keep_path: bool = False,
    ) -> None:
        segment = await self.session.get(RecordingSegment, segment_id)
        if segment is None:
            return
        segment.speech_status = status
        segment.speech_error = error
        if not keep_path:
            segment.speech_srt_path = srt_path

    async def speech_counts(self) -> dict[str, int]:
        counts = {key: 0 for key in ("pending", "running", "completed", "failed", "skipped", "none")}
        rows = await self.session.execute(
            select(RecordingSegment.speech_status, func.count())
            .where(RecordingSegment.file_removed.is_(False), RecordingSegment.finished_at.is_not(None))
            .group_by(RecordingSegment.speech_status)
        )
        for status, count in rows:
            key = status if status in counts else "none"
            counts[key] += int(count)
        return counts

    def _speech_filters(self, stmt: Select, *, status: str | None, session_id: int | None) -> Select:
        stmt = stmt.where(RecordingSegment.file_removed.is_(False), RecordingSegment.finished_at.is_not(None))
        if session_id is not None:
            stmt = stmt.where(RecordingJob.stream_session_id == session_id)
        if status == "none":
            stmt = stmt.where(RecordingSegment.speech_status.is_(None))
        elif status:
            stmt = stmt.where(RecordingSegment.speech_status == status)
        return stmt

    async def search_speech(
        self,
        *,
        status: str | None,
        session_id: int | None,
        page: int,
        page_size: int,
    ) -> tuple[list[RecordingSegment], int]:
        joined = select(RecordingSegment).join(RecordingJob).join(StreamSession)
        filtered = self._speech_filters(joined, status=status, session_id=session_id)
        total = int(
            await self.session.scalar(
                self._speech_filters(
                    select(func.count()).select_from(RecordingSegment).join(RecordingJob).join(StreamSession),
                    status=status,
                    session_id=session_id,
                )
            )
            or 0
        )
        rank = case(
            (RecordingSegment.speech_status == "running", 0),
            (RecordingSegment.speech_status == "pending", 1),
            (RecordingSegment.speech_status == "failed", 2),
            else_=3,
        )
        rows = list(
            await self.session.scalars(
                filtered.options(
                    selectinload(RecordingSegment.job)
                    .selectinload(RecordingJob.session)
                    .selectinload(StreamSession.streamer)
                )
                .order_by(rank, RecordingSegment.finished_at.desc(), RecordingSegment.id.desc())
                .offset(max(0, page - 1) * page_size)
                .limit(page_size)
            )
        )
        return rows, total

    async def completed_speech_segments(
        self, *, session_id: int | None, segment_id: int | None
    ) -> list[RecordingSegment]:
        stmt = (
            select(RecordingSegment)
            .join(RecordingJob)
            .join(StreamSession)
            .where(
                RecordingSegment.file_removed.is_(False),
                RecordingSegment.speech_srt_path.is_not(None),
            )
            .options(
                selectinload(RecordingSegment.job)
                .selectinload(RecordingJob.session)
                .selectinload(StreamSession.streamer)
            )
            .order_by(RecordingSegment.started_at, RecordingSegment.id)
        )
        if session_id is not None:
            stmt = stmt.where(RecordingJob.stream_session_id == session_id)
        if segment_id is not None:
            stmt = stmt.where(RecordingSegment.id == segment_id)
        return list(await self.session.scalars(stmt))

    async def requeue_failed_speech(self) -> int:
        rows = await self.session.scalars(
            select(RecordingSegment).where(
                RecordingSegment.speech_status == "failed",
                RecordingSegment.file_removed.is_(False),
            )
        )
        queued = 0
        for segment in rows:
            if requeue_speech(segment) == "queued":
                queued += 1
        return queued

    async def unfinished_segments(self) -> list[RecordingSegment]:
        result = await self.session.scalars(
            select(RecordingSegment).where(RecordingSegment.finished_at.is_(None), RecordingSegment.file_removed.is_(False))
        )
        return list(result)

    async def unfinished_jobs(self) -> list[RecordingJob]:
        result = await self.session.scalars(select(RecordingJob).where(RecordingJob.status.in_(REC_ACTIVE)))
        return list(result)

    async def unfinished_comment_jobs(self) -> list[CommentCollectionJob]:
        result = await self.session.scalars(
            select(CommentCollectionJob).where(CommentCollectionJob.status.in_(COM_ACTIVE))
        )
        return list(result)

    async def add_gap(
        self, session_id: int, job_type: str, started: datetime, ended: datetime | None, reason: str
    ) -> None:
        self.session.add(
            CollectionGap(
                stream_session_id=session_id,
                job_type=job_type,
                started_at=started,
                ended_at=ended,
                reason=reason[:200],
            )
        )

    async def close_open_gaps(self, session_id: int, job_type: str, ended: datetime) -> None:
        rows = await self.session.scalars(
            select(CollectionGap).where(
                CollectionGap.stream_session_id == session_id,
                CollectionGap.job_type == job_type,
                CollectionGap.ended_at.is_(None),
            )
        )
        for gap in rows:
            gap.ended_at = ended if gap.started_at <= ended else gap.started_at

    async def latest_segment(self, job_id: int) -> RecordingSegment | None:
        return await self.session.scalar(
            select(RecordingSegment)
            .where(RecordingSegment.recording_job_id == job_id, RecordingSegment.file_removed.is_(False))
            .order_by(RecordingSegment.segment_number.desc())
            .limit(1)
        )

    async def add_event(
        self,
        event_type: str,
        message: str,
        *,
        severity: str = "info",
        session_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        self.session.add(
            SystemEvent(
                event_type=event_type,
                severity=severity,
                stream_session_id=session_id,
                message=message[:2000],
                details=details or {},
            )
        )

    async def insert_comments(self, rows: list[dict[str, object]]) -> int:
        if not rows:
            return 0
        stmt = (
            pg_insert(Comment)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_comments_session_dedupe")
            .returning(Comment.id)
        )
        result = await self.session.execute(stmt)
        return len(result.scalars().all())

    async def comment_rows(
        self,
        *,
        session_id: int,
        recording_started: datetime | None,
        stream_started: datetime | None,
        drafts: list[object],
    ) -> list[dict[str, object]]:
        from live_recorder.comments.offsets import offsets
        from live_recorder.platforms.types import CommentDraft

        rows: list[dict[str, object]] = []
        for draft in drafts:
            if not isinstance(draft, CommentDraft):
                continue
            stream_offset, recording_offset = offsets(
                posted_at=draft.posted_at,
                received_at=draft.received_at,
                stream_started_at=stream_started,
                recording_started_at=recording_started,
            )
            rows.append(
                {
                    "stream_session_id": session_id,
                    "platform_comment_id": draft.platform_comment_id,
                    "platform_user_id": draft.platform_user_id,
                    "nickname": draft.nickname,
                    "message": draft.message,
                    "posted_at": draft.posted_at,
                    "received_at": draft.received_at,
                    "stream_offset_ms": stream_offset,
                    "recording_offset_ms": recording_offset,
                    "dedupe_key": dedupe_key(
                        platform_comment_id=draft.platform_comment_id,
                        platform_user_id=draft.platform_user_id,
                        message=draft.message,
                        posted_at=draft.posted_at,
                        received_at=draft.received_at,
                    ),
                }
            )
        return rows

    async def heartbeat(self, worker_id: str, status: str, detail: str | None) -> None:
        existing = await self.session.get(WorkerHeartbeat, worker_id)
        now = utcnow()
        if existing is None:
            self.session.add(WorkerHeartbeat(worker_id=worker_id, last_seen_at=now, status=status, detail=detail))
        else:
            existing.last_seen_at = now
            existing.status = status
            existing.detail = detail

    async def latest_heartbeat(self) -> WorkerHeartbeat | None:
        return await self.session.scalar(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()))

    async def list_engines(self) -> list[RecorderEngine]:
        result = await self.session.scalars(select(RecorderEngine).order_by(RecorderEngine.name))
        return list(result)

    async def upsert_engine(self, name: str, *, version: str | None, available: bool) -> None:
        row = await self.session.get(RecorderEngine, name)
        now = utcnow()
        if row is None:
            self.session.add(RecorderEngine(name=name, version=version, available=available, updated_at=now))
        else:
            row.version = version
            row.available = available
            row.updated_at = now

    async def mark_engine(self, name: str, *, ok: bool, reason: str | None = None) -> None:
        row = await self.session.get(RecorderEngine, name)
        now = utcnow()
        if row is None:
            row = RecorderEngine(name=name, available=True, updated_at=now)
            self.session.add(row)
        if ok:
            row.last_success_at = now
            row.failure_reason = None
        else:
            row.last_failure_at = now
            row.failure_reason = (reason or "")[:2000]
            row.retry_count = (row.retry_count or 0) + 1
        row.updated_at = now

    async def recent_errors(self, limit: int = 8) -> list[SystemEvent]:
        result = await self.session.scalars(
            select(SystemEvent)
            .where(SystemEvent.severity.in_(("error", "warning")))
            .order_by(SystemEvent.created_at.desc())
            .limit(limit)
        )
        return list(result)

    async def recent_events(self, limit: int = 40) -> list[SystemEvent]:
        result = await self.session.scalars(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit))
        return list(result)

    def session_query(self) -> Select[tuple[StreamSession]]:
        return select(StreamSession).options(selectinload(StreamSession.streamer)).order_by(StreamSession.detected_at.desc())

    async def search_sessions(
        self,
        *,
        streamer_id: int | None,
        platform: str | None,
        status: str | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        page_size: int,
    ) -> tuple[list[StreamSession], int]:
        stmt = self.session_query()
        count_stmt = select(func.count()).select_from(StreamSession).join(Streamer)
        if streamer_id:
            stmt = stmt.where(StreamSession.streamer_id == streamer_id)
            count_stmt = count_stmt.where(StreamSession.streamer_id == streamer_id)
        if platform:
            stmt = stmt.join(Streamer).where(Streamer.platform == platform)
            count_stmt = count_stmt.where(Streamer.platform == platform)
        if start:
            stmt = stmt.where(StreamSession.detected_at >= start)
            count_stmt = count_stmt.where(StreamSession.detected_at >= start)
        if end:
            stmt = stmt.where(StreamSession.detected_at <= end)
            count_stmt = count_stmt.where(StreamSession.detected_at <= end)
        if status:
            job_ids = select(RecordingJob.stream_session_id).where(RecordingJob.status == status)
            stmt = stmt.where(StreamSession.id.in_(job_ids))
            count_stmt = count_stmt.where(StreamSession.id.in_(job_ids))
        total = int(await self.session.scalar(count_stmt) or 0)
        rows = list(
            await self.session.scalars(stmt.offset(max(0, page - 1) * page_size).limit(page_size))
        )
        return rows, total

    async def latest_recording(self, session_id: int) -> RecordingJob | None:
        return await self.session.scalar(
            select(RecordingJob).where(RecordingJob.stream_session_id == session_id).order_by(RecordingJob.id.desc())
        )

    async def latest_comment_job(self, session_id: int) -> CommentCollectionJob | None:
        return await self.session.scalar(
            select(CommentCollectionJob)
            .where(CommentCollectionJob.stream_session_id == session_id)
            .order_by(CommentCollectionJob.id.desc())
        )

    async def jobs_for_session(self, session_id: int) -> list[RecordingJob]:
        result = await self.session.scalars(
            select(RecordingJob).where(RecordingJob.stream_session_id == session_id).order_by(RecordingJob.id)
        )
        return list(result)

    async def segments_for_session(self, session_id: int) -> list[RecordingSegment]:
        result = await self.session.scalars(
            select(RecordingSegment)
            .join(RecordingJob)
            .where(RecordingJob.stream_session_id == session_id)
            .order_by(RecordingSegment.started_at)
            .execution_options(populate_existing=True)
        )
        return list(result)

    async def get_segment(self, segment_id: int) -> RecordingSegment | None:
        return await self.session.get(RecordingSegment, segment_id)

    async def segment_with_session(self, segment_id: int) -> tuple[RecordingSegment, int] | None:
        row = (
            await self.session.execute(
                select(RecordingSegment, RecordingJob.stream_session_id)
                .join(RecordingJob, RecordingSegment.recording_job_id == RecordingJob.id)
                .where(RecordingSegment.id == segment_id)
            )
        ).one_or_none()
        if row is None:
            return None
        segment, session_id = row
        return segment, int(session_id)

    async def search_commented_sessions(
        self,
        *,
        streamer_id: int | None,
        platform: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[StreamSession, int]], int]:
        """コメントが1件以上ある録画を、新しい順で返す。"""
        counted = func.count(Comment.id)
        grouped = (
            select(StreamSession.id.label("session_id"), counted.label("comments"))
            .join(Streamer, StreamSession.streamer_id == Streamer.id)
            .join(Comment, Comment.stream_session_id == StreamSession.id)
            .group_by(StreamSession.id)
        )
        if streamer_id:
            grouped = grouped.where(StreamSession.streamer_id == streamer_id)
        if platform:
            grouped = grouped.where(Streamer.platform == platform)
        grouped = grouped.subquery()
        total = int(await self.session.scalar(select(func.count()).select_from(grouped)) or 0)
        result = await self.session.execute(
            select(StreamSession, grouped.c.comments)
            .join(grouped, grouped.c.session_id == StreamSession.id)
            .options(selectinload(StreamSession.streamer))
            .order_by(StreamSession.detected_at.desc(), StreamSession.id.desc())
            .offset(max(0, page - 1) * page_size)
            .limit(page_size)
        )
        return [(row, int(count)) for row, count in result], total

    async def comment_count(self, session_id: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(Comment).where(Comment.stream_session_id == session_id)
            )
            or 0
        )

    async def gaps_for_session(self, session_id: int) -> list[CollectionGap]:
        result = await self.session.scalars(
            select(CollectionGap).where(CollectionGap.stream_session_id == session_id).order_by(CollectionGap.started_at)
        )
        return list(result)

    async def search_comments(
        self,
        *,
        text: str | None,
        user_id: str | None,
        nickname: str | None,
        streamer_id: int | None,
        platform: str | None,
        session_id: int | None,
        start: datetime | None,
        end: datetime | None,
        page: int,
        page_size: int,
        newest_first: bool = False,
    ) -> tuple[list[Comment], int]:
        stmt = select(Comment).join(StreamSession).join(Streamer)
        filters = []
        if text:
            filters.append(Comment.message.ilike(self._like(text)))
        if user_id:
            filters.append(Comment.platform_user_id == user_id)
        if nickname:
            filters.append(Comment.nickname.ilike(self._like(nickname)))
        if streamer_id:
            filters.append(StreamSession.streamer_id == streamer_id)
        if platform:
            filters.append(Streamer.platform == platform)
        if session_id:
            filters.append(Comment.stream_session_id == session_id)
        if start:
            filters.append(Comment.received_at >= start)
        if end:
            filters.append(Comment.received_at <= end)
        if filters:
            stmt = stmt.where(*filters)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(await self.session.scalar(count_stmt) or 0)
        stmt = stmt.options(selectinload(Comment.session).selectinload(StreamSession.streamer))
        if newest_first:
            order = (Comment.received_at.desc(), Comment.id.desc())
        else:
            order = (Comment.received_at.asc(), Comment.id.asc())
        rows = list(
            await self.session.scalars(
                stmt.order_by(*order).offset(max(0, page - 1) * page_size).limit(page_size)
            )
        )
        return rows, total

    async def export_comments(self, **filters: object) -> list[Comment]:
        page = 1
        found: list[Comment] = []
        while True:
            rows, total = await self.search_comments(page=page, page_size=500, **filters)  # type: ignore[arg-type]
            found.extend(rows)
            if len(found) >= total or not rows:
                break
            page += 1
            if page > 200:
                break
        return found

    async def export_comments_between(
        self,
        session_id: int,
        *,
        start: datetime | None,
        end_inclusive: datetime | None,
        end_exclusive: datetime | None,
        text: str | None = None,
        user_id: str | None = None,
        nickname: str | None = None,
        received_start: datetime | None = None,
        received_end: datetime | None = None,
        limit: int = 100_000,
    ) -> list[Comment]:
        """セグメントの時間幅の中だけを上限まで取る。全体を先に切らない。"""
        anchor = func.coalesce(Comment.posted_at, Comment.received_at)
        stmt = select(Comment).where(Comment.stream_session_id == session_id)
        if start is not None:
            stmt = stmt.where(anchor >= start)
        if end_inclusive is not None:
            stmt = stmt.where(anchor <= end_inclusive)
        if end_exclusive is not None:
            stmt = stmt.where(anchor < end_exclusive)
        if received_start is not None:
            stmt = stmt.where(Comment.received_at >= received_start)
        if received_end is not None:
            stmt = stmt.where(Comment.received_at <= received_end)
        if text:
            stmt = stmt.where(Comment.message.ilike(self._like(text)))
        if user_id:
            stmt = stmt.where(Comment.platform_user_id == user_id)
        if nickname:
            stmt = stmt.where(Comment.nickname.ilike(self._like(nickname)))
        stmt = (
            stmt.options(selectinload(Comment.session).selectinload(StreamSession.streamer))
            .order_by(anchor.asc(), Comment.id.asc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def export_comments_for_segments(
        self,
        session_id: int,
        windows,
        *,
        only_id: int | None = None,
        text: str | None = None,
        user_id: str | None = None,
        nickname: str | None = None,
        received_start: datetime | None = None,
        received_end: datetime | None = None,
    ) -> list[Comment]:
        ordered = ordered_export_segments(windows)
        found: list[Comment] = []
        for index, window in enumerate(ordered):
            if only_id is not None and window.id != only_id:
                continue
            start, end_inclusive, end_exclusive = segment_comment_bounds(ordered, index)
            found.extend(
                await self.export_comments_between(
                    session_id,
                    start=start,
                    end_inclusive=end_inclusive,
                    end_exclusive=end_exclusive,
                    text=text,
                    user_id=user_id,
                    nickname=nickname,
                    received_start=received_start,
                    received_end=received_end,
                )
            )
        return found

    async def delete_session(self, session_id: int) -> StreamSession | None:
        row = await self.session.get(StreamSession, session_id)
        if row is None:
            return None
        await self.session.delete(row)
        return row

    async def enqueue_file_deletion(
        self,
        file_path: str,
        speech_srt_path: str | None = None,
        *,
        kind: str = "file",
        recording_segment_id: int | None = None,
        expected_device: int | None = None,
        expected_inode: int | None = None,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
        expected_ctime_ns: int | None = None,
    ) -> None:
        """同じパスの操作は最新世代へ置き換える。"""
        stmt = pg_insert(PendingFileDeletion).values(
            file_path=file_path,
            speech_srt_path=speech_srt_path,
            kind=kind,
            recording_segment_id=recording_segment_id,
            expected_device=expected_device,
            expected_inode=expected_inode,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
            expected_ctime_ns=expected_ctime_ns,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_pending_file_deletions_path",
            set_={
                "speech_srt_path": stmt.excluded.speech_srt_path,
                "kind": stmt.excluded.kind,
                "recording_segment_id": stmt.excluded.recording_segment_id,
                "expected_device": stmt.excluded.expected_device,
                "expected_inode": stmt.excluded.expected_inode,
                "expected_size": stmt.excluded.expected_size,
                "expected_mtime_ns": stmt.excluded.expected_mtime_ns,
                "expected_ctime_ns": stmt.excluded.expected_ctime_ns,
            },
        )
        await self.session.execute(stmt)

    async def pending_file_deletions(self, *, kind: str | None = None) -> list[PendingFileDeletion]:
        stmt = select(PendingFileDeletion)
        if kind is not None:
            stmt = stmt.where(PendingFileDeletion.kind == kind)
        result = await self.session.scalars(stmt.order_by(PendingFileDeletion.id))
        return list(result)

    async def get_pending_file_deletion(self, deletion_id: int) -> PendingFileDeletion | None:
        return await self.session.get(PendingFileDeletion, deletion_id)

    async def clear_pending_file_deletion(self, deletion_id: int) -> None:
        row = await self.session.get(PendingFileDeletion, deletion_id)
        if row is not None:
            await self.session.delete(row)

    async def clear_pending_file_path(self, file_path: str, *, kind: str | None = None) -> None:
        stmt = delete(PendingFileDeletion).where(PendingFileDeletion.file_path == file_path)
        if kind is not None:
            stmt = stmt.where(PendingFileDeletion.kind == kind)
        await self.session.execute(stmt)

    async def lock_recording_paths(self) -> None:
        """録画名の予約と削除要求作成を直列化する。"""
        await self.session.execute(select(func.pg_advisory_xact_lock(0x4C5250415448)))

    async def reserved_recording_paths(self) -> set[str]:
        paths = {
            value
            for value in await self.session.scalars(select(RecordingSegment.file_path))
            if value
        }
        paths.update(
            value
            for value in await self.session.scalars(select(PendingFileDeletion.file_path))
            if value
        )
        return paths

    async def segments_needing_purge(self) -> list[RecordingSegment]:
        result = await self.session.scalars(
            select(RecordingSegment).where(
                RecordingSegment.file_removed.is_(True),
                RecordingSegment.file_purge_done.is_(False),
            )
        )
        return list(result)

    async def path_has_live_segment(self, file_path: str, *, excluding_id: int | None = None) -> bool:
        stmt = select(RecordingSegment.id).where(
            RecordingSegment.file_path == file_path,
            RecordingSegment.file_removed.is_(False),
        )
        if excluding_id is not None:
            stmt = stmt.where(RecordingSegment.id != excluding_id)
        return await self.session.scalar(stmt.limit(1)) is not None

    async def finish_segment_purge_if_ready(self, segment_id: int) -> None:
        pending = await self.session.scalar(
            select(PendingFileDeletion.id)
            .where(
                PendingFileDeletion.recording_segment_id == segment_id,
                PendingFileDeletion.kind == "file",
            )
            .limit(1)
        )
        if pending is not None:
            return
        segment = await self.session.get(RecordingSegment, segment_id)
        if segment is not None and segment.file_removed:
            segment.file_purge_done = True

    async def mark_file_purge_done(self, file_path: str) -> None:
        """旧キューの移行用。パスだけで削除せず、削除済み行を完了へ送る。"""
        segments = await self.session.scalars(
            select(RecordingSegment).where(
                RecordingSegment.file_path == file_path,
                RecordingSegment.file_removed.is_(True),
            )
        )
        for segment in segments:
            segment.file_purge_done = True

    def mark_segment_removed(self, segment: RecordingSegment) -> None:
        """履歴は残してファイル削除を始める。完了フラグは付け直す。"""
        segment.file_removed = True
        segment.file_purge_done = False

    async def recorded_path_refs(self) -> set[str]:
        """改名の照合用。DB が指している録画・字幕・書き出しパス。"""
        paths: set[str] = set()
        for value in await self.session.scalars(select(RecordingSegment.file_path)):
            if value:
                paths.add(value)
        for value in await self.session.scalars(
            select(RecordingSegment.speech_srt_path).where(RecordingSegment.speech_srt_path.is_not(None))
        ):
            if value:
                paths.add(value)
        for value in await self.session.scalars(
            select(CommentCollectionJob.export_path).where(CommentCollectionJob.export_path.is_not(None))
        ):
            if value:
                paths.add(value)
        return paths

    async def stored_files(self) -> list[RecordingSegment]:
        result = await self.session.scalars(select(RecordingSegment).where(RecordingSegment.file_removed.is_(False)))
        return list(result)

    async def expired_segments(self, older_than: datetime) -> list[RecordingSegment]:
        result = await self.session.scalars(
            select(RecordingSegment).where(
                RecordingSegment.file_removed.is_(False),
                RecordingSegment.finished_at.is_not(None),
                RecordingSegment.finished_at < older_than,
            )
        )
        return list(result)

    async def expired_session_ids(self, older_than: datetime) -> list[int]:
        """保存期限を過ぎたファイルを持つ配信。ロックの順を揃えるため ID 順に返す。"""
        result = await self.session.scalars(
            select(RecordingJob.stream_session_id)
            .join(RecordingSegment, RecordingSegment.recording_job_id == RecordingJob.id)
            .where(
                RecordingSegment.file_removed.is_(False),
                RecordingSegment.finished_at.is_not(None),
                RecordingSegment.finished_at < older_than,
            )
            .distinct()
            .order_by(RecordingJob.stream_session_id)
        )
        return list(result)

    async def sessions_with_comments_before(self, older_than: datetime) -> list[int]:
        """古いコメントを持つ配信。ロックの順を揃えるため ID 順に返す。"""
        result = await self.session.scalars(
            select(Comment.stream_session_id)
            .where(Comment.created_at < older_than)
            .distinct()
            .order_by(Comment.stream_session_id)
        )
        return list(result)

    async def delete_comments_before(self, older_than: datetime) -> tuple[int, set[int]]:
        """古いコメントを消す。呼び出し側は先に対象配信を lock_session しておく。"""
        result = await self.session.execute(
            delete(Comment).where(Comment.created_at < older_than).returning(Comment.stream_session_id)
        )
        session_ids = list(result.scalars().all())
        return len(session_ids), set(session_ids)

    async def comment_jobs_for_session(self, session_id: int) -> list[CommentCollectionJob]:
        result = await self.session.scalars(
            select(CommentCollectionJob).where(CommentCollectionJob.stream_session_id == session_id)
        )
        return list(result)

    async def dashboard(self, day_start: datetime) -> dict[str, int]:
        streamers = int(await self.session.scalar(select(func.count()).select_from(Streamer)) or 0)
        live = int(
            await self.session.scalar(
                select(func.count()).select_from(Streamer).where(Streamer.live_status == "LIVE", Streamer.monitoring_enabled.is_(True))
            )
            or 0
        )
        recording = await self.count_active_recordings()
        collecting = int(
            await self.session.scalar(
                select(func.count()).select_from(CommentCollectionJob).where(CommentCollectionJob.status.in_(COM_ACTIVE))
            )
            or 0
        )
        speech = {"pending": 0, "running": 0}
        speech_rows = await self.session.execute(
            select(RecordingSegment.speech_status, func.count())
            .where(
                RecordingSegment.file_removed.is_(False),
                RecordingSegment.finished_at.is_not(None),
                RecordingSegment.speech_status.in_(("pending", "running")),
            )
            .group_by(RecordingSegment.speech_status)
        )
        for status, count in speech_rows:
            if status in speech:
                speech[status] = int(count)
        sessions_today = int(
            await self.session.scalar(
                select(func.count()).select_from(StreamSession).where(StreamSession.detected_at >= day_start)
            )
            or 0
        )
        comments_today = int(
            await self.session.scalar(select(func.count()).select_from(Comment).where(Comment.received_at >= day_start))
            or 0
        )
        total_bytes = int(
            await self.session.scalar(
                select(func.coalesce(func.sum(RecordingSegment.file_size), 0)).where(RecordingSegment.file_removed.is_(False))
            )
            or 0
        )
        return {
            "streamers": streamers,
            "live": live,
            "recording": recording,
            "collecting": collecting,
            "speech_pending": speech["pending"],
            "speech_running": speech["running"],
            "sessions_today": sessions_today,
            "comments_today": comments_today,
            "total_bytes": total_bytes,
        }

    async def recent_sessions(self, limit: int = 8) -> list[StreamSession]:
        result = await self.session.scalars(self.session_query().limit(limit))
        return list(result)

    async def bytes_by_streamer(self) -> list[tuple[str, str, int]]:
        stmt = (
            select(Streamer.platform, Streamer.display_name, func.coalesce(func.sum(RecordingSegment.file_size), 0))
            .join(StreamSession, StreamSession.streamer_id == Streamer.id)
            .join(RecordingJob, RecordingJob.stream_session_id == StreamSession.id)
            .join(RecordingSegment, RecordingSegment.recording_job_id == RecordingJob.id)
            .where(RecordingSegment.file_removed.is_(False))
            .group_by(Streamer.id)
            .order_by(func.sum(RecordingSegment.file_size).desc())
        )
        rows = await self.session.execute(stmt)
        return [(platform, name, int(size)) for platform, name, size in rows]

    @staticmethod
    def _like(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    async def recording_started_at(self, session_id: int) -> datetime | None:
        return await self.session.scalar(
            select(func.min(RecordingJob.started_at)).where(RecordingJob.stream_session_id == session_id)
        )

    async def any_valid_segment(self, job_id: int) -> bool:
        count = await self.session.scalar(
            select(func.count())
            .select_from(RecordingSegment)
            .where(
                RecordingSegment.recording_job_id == job_id,
                RecordingSegment.file_removed.is_(False),
                RecordingSegment.file_size.is_not(None),
                RecordingSegment.file_size > 0,
                RecordingSegment.validation_status == "ok",
            )
        )
        return bool(count)
