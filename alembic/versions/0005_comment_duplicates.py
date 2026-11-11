"""コメントの重複受信件数を保存する。"""

from alembic import op

revision = "0005_comment_duplicates"
down_revision = "0004_streamer_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE comment_collection_jobs ADD COLUMN IF NOT EXISTS duplicate_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        """
        UPDATE comment_collection_jobs
        SET duplicate_count = GREATEST(received_count - stored_count - error_count, 0)
        WHERE duplicate_count = 0
          AND received_count > stored_count
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE comment_collection_jobs DROP COLUMN IF EXISTS duplicate_count")
