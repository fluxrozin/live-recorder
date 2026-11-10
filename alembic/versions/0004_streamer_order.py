"""配信者の表示順を保存する。"""

from alembic import op

revision = "0004_streamer_order"
down_revision = "0003_speech_words"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE streamers ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0")
    op.execute(
        """
        UPDATE streamers AS streamer
        SET sort_order = ranked.ord
        FROM (
            SELECT id, (row_number() OVER (ORDER BY platform, display_name, id) - 1) AS ord
            FROM streamers
        ) AS ranked
        WHERE streamer.id = ranked.id
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE streamers DROP COLUMN IF EXISTS sort_order")
