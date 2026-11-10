"""録画セグメントに音声字幕の状態を足す。"""

from alembic import op

revision = "0002_speech"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS speech_status VARCHAR(16)")
    op.execute("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS speech_srt_path VARCHAR(500)")
    op.execute("ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS speech_error TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_recording_segments_speech_status ON recording_segments (speech_status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_recording_segments_speech_status")
    op.execute("ALTER TABLE recording_segments DROP COLUMN IF EXISTS speech_error")
    op.execute("ALTER TABLE recording_segments DROP COLUMN IF EXISTS speech_srt_path")
    op.execute("ALTER TABLE recording_segments DROP COLUMN IF EXISTS speech_status")
