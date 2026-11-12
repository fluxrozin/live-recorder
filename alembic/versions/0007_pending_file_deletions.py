"""削除待ちの録画パスを永続化する。"""

from alembic import op

revision = "0007_pending_file_deletions"
down_revision = "0006_drop_speech_words"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_file_deletions (
            id BIGSERIAL PRIMARY KEY,
            file_path VARCHAR(500) NOT NULL,
            speech_srt_path VARCHAR(500),
            kind VARCHAR(16) NOT NULL DEFAULT 'video',
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            CONSTRAINT uq_pending_file_deletions_path UNIQUE (file_path)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pending_file_deletions")
