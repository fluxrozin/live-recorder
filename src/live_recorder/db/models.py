from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from live_recorder.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Streamer(Base):
    __tablename__ = "streamers"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_streamers_platform_user"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32))
    platform_user_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(100))
    profile_url: Mapped[str] = mapped_column(String(300))
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    comments_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    live_status: Mapped[str] = mapped_column(String(16), default="OFFLINE", server_default="OFFLINE")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recording_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sessions: Mapped[list[StreamSession]] = relationship(back_populates="streamer")


class StreamSession(Base):
    __tablename__ = "stream_sessions"
    __table_args__ = (
        Index(
            "uq_open_session_per_streamer",
            "streamer_id",
            unique=True,
            postgresql_where=text("status IN ('LIVE', 'ENDING')"),
        ),
        Index("ix_stream_sessions_streamer_status", "streamer_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id", ondelete="RESTRICT"))
    platform_stream_id: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    platform_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    platform_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="LIVE", server_default="LIVE")
    restricted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    streamer: Mapped[Streamer] = relationship(back_populates="sessions")
    recording_jobs: Mapped[list[RecordingJob]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
    comment_jobs: Mapped[list[CommentCollectionJob]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )


class RecordingJob(Base):
    __tablename__ = "recording_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stream_session_id: Mapped[int] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    recorder_engine: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    session: Mapped[StreamSession] = relationship(back_populates="recording_jobs")
    segments: Mapped[list[RecordingSegment]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class RecordingSegment(Base):
    __tablename__ = "recording_segments"
    __table_args__ = (Index("ix_recording_segments_speech_status", "speech_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recording_job_id: Mapped[int] = mapped_column(
        ForeignKey("recording_jobs.id", ondelete="CASCADE"), index=True
    )
    segment_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_path: Mapped[str] = mapped_column(String(500))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    container_format: Mapped[str | None] = mapped_column(String(32))
    validation_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    file_removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    file_purge_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    speech_status: Mapped[str | None] = mapped_column(String(16))
    speech_srt_path: Mapped[str | None] = mapped_column(String(500))
    speech_error: Mapped[str | None] = mapped_column(Text)
    speech_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    speech_duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[RecordingJob] = relationship(back_populates="segments")


class CommentCollectionJob(Base):
    __tablename__ = "comment_collection_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stream_session_id: Mapped[int] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stored_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reconnect_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    export_path: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    session: Mapped[StreamSession] = relationship(back_populates="comment_jobs")


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("stream_session_id", "dedupe_key", name="uq_comments_session_dedupe"),
        Index("ix_comments_session_received", "stream_session_id", "received_at"),
        Index("ix_comments_platform_user", "platform_user_id"),
        Index(
            "ix_comments_platform_user_trgm",
            "platform_user_id",
            postgresql_using="gin",
            postgresql_ops={"platform_user_id": "gin_trgm_ops"},
        ),
        Index(
            "ix_comments_message_trgm",
            "message",
            postgresql_using="gin",
            postgresql_ops={"message": "gin_trgm_ops"},
        ),
        Index(
            "ix_comments_nickname_trgm",
            "nickname",
            postgresql_using="gin",
            postgresql_ops={"nickname": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id", ondelete="CASCADE"))
    platform_comment_id: Mapped[str | None] = mapped_column(String(64))
    platform_user_id: Mapped[str | None] = mapped_column(String(128))
    nickname: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stream_offset_ms: Mapped[int | None] = mapped_column(BigInteger)
    recording_offset_ms: Mapped[int | None] = mapped_column(BigInteger)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[StreamSession] = relationship(back_populates="comments")


class CollectionGap(Base):
    __tablename__ = "collection_gaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stream_session_id: Mapped[int] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="CASCADE"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    stream_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="SET NULL")
    )
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecorderEngine(Base):
    __tablename__ = "recorder_engines"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str | None] = mapped_column(String(200))
    available: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)


class PendingFileDeletion(Base):
    """DB 確定後のファイル操作。削除対象の世代も保持する。"""

    __tablename__ = "pending_file_deletions"
    __table_args__ = (UniqueConstraint("file_path", name="uq_pending_file_deletions_path"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    file_path: Mapped[str] = mapped_column(String(500))
    speech_srt_path: Mapped[str | None] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(16), default="video", server_default="video")
    recording_segment_id: Mapped[int | None] = mapped_column(BigInteger)
    expected_device: Mapped[int | None] = mapped_column(BigInteger)
    expected_inode: Mapped[int | None] = mapped_column(BigInteger)
    expected_size: Mapped[int | None] = mapped_column(BigInteger)
    expected_mtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    expected_ctime_ns: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
