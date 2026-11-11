"""配信者の字幕ヒント列を削除する。"""

from alembic import op

revision = "0006_drop_speech_words"
down_revision = "0005_comment_duplicates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE streamers DROP COLUMN IF EXISTS speech_words")


def downgrade() -> None:
    op.execute("ALTER TABLE streamers ADD COLUMN IF NOT EXISTS speech_words TEXT")
