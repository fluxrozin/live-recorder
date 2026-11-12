"""録画セグメントに音声字幕の生成時間を残す。"""

from alembic import op

revision = "0010_speech_duration"
down_revision = "0009_pending_file_generations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE recording_segments
        ADD COLUMN IF NOT EXISTS speech_started_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS speech_duration_ms BIGINT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE recording_segments
        DROP COLUMN IF EXISTS speech_duration_ms,
        DROP COLUMN IF EXISTS speech_started_at
        """
    )
