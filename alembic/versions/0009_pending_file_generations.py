"""削除待ちへ対象ファイルの世代を追加する。"""

from alembic import op

revision = "0009_pending_file_generations"
down_revision = "0008_file_purge_done"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE pending_file_deletions
        ADD COLUMN IF NOT EXISTS recording_segment_id BIGINT,
        ADD COLUMN IF NOT EXISTS expected_device BIGINT,
        ADD COLUMN IF NOT EXISTS expected_inode BIGINT,
        ADD COLUMN IF NOT EXISTS expected_size BIGINT,
        ADD COLUMN IF NOT EXISTS expected_mtime_ns BIGINT,
        ADD COLUMN IF NOT EXISTS expected_ctime_ns BIGINT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE pending_file_deletions
        DROP COLUMN IF EXISTS expected_ctime_ns,
        DROP COLUMN IF EXISTS expected_mtime_ns,
        DROP COLUMN IF EXISTS expected_size,
        DROP COLUMN IF EXISTS expected_inode,
        DROP COLUMN IF EXISTS expected_device,
        DROP COLUMN IF EXISTS recording_segment_id
        """
    )
