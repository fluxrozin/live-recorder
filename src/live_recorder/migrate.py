"""データベースマイグレーション。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from live_recorder.config import load_settings


def alembic_config() -> Config:
    settings = load_settings()
    root = settings.app_home
    ini = root / "alembic.ini"
    if not ini.exists():
        ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.sync_database_url())
    return cfg


def upgrade() -> None:
    command.upgrade(alembic_config(), "head")


def main() -> None:
    upgrade()


if __name__ == "__main__":
    main()
