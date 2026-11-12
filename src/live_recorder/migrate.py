"""データベースマイグレーション。"""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config

from live_recorder.config import load_settings

# worker 824201、文字起こし 824202 と重ならない。
_MIGRATION_LOCK = 824203


def alembic_config() -> Config:
    settings = load_settings()
    root = settings.app_home
    ini = root / "alembic.ini"
    if not ini.exists():
        ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    # URL は alembic/env.py が読む。ConfigParser は % を展開してしまう。
    return cfg


def _sync_psycopg_url() -> str:
    return load_settings().sync_database_url().replace("postgresql+psycopg://", "postgresql://", 1)


def upgrade() -> None:
    with psycopg.connect(_sync_psycopg_url(), autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            command.upgrade(alembic_config(), "head")
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))


def main() -> None:
    upgrade()


if __name__ == "__main__":
    main()
