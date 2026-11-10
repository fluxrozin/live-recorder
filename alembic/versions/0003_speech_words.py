"""配信者ごとに字幕のヒント単語を保存する。"""

from alembic import op

revision = "0003_speech_words"
down_revision = "0002_speech"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE streamers ADD COLUMN IF NOT EXISTS speech_words TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE streamers DROP COLUMN IF EXISTS speech_words")
