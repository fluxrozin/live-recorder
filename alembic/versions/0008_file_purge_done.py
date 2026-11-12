"""削除済みセグメントの物理削除完了を記録する。"""

from alembic import op

revision = "0008_file_purge_done"
down_revision = "0007_pending_file_deletions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE recording_segments
        ADD COLUMN IF NOT EXISTS file_purge_done BOOLEAN NOT NULL DEFAULT false
        """
    )
def downgrade() -> None:
    op.execute("ALTER TABLE recording_segments DROP COLUMN IF EXISTS file_purge_done")
