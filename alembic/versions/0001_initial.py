"""初期スキーマ。モデル定義が正本です。"""

from alembic import op

from live_recorder.db.base import Base
from live_recorder.db import models as _models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
