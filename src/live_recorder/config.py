from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import URL, make_url

PLACEHOLDER_SECRETS = {
    "replace-with-a-random-string-of-at-least-32-chars",
    "change-me",
    "change-me-to-a-long-random-string",
}


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    recordings_dir: Path
    config_dir: Path
    web_host: str
    web_port: int
    display_timezone: str
    log_level: str
    cookie_secure: bool
    twitcasting_client_id: str
    twitcasting_client_secret: str
    twitcasting_access_token: str
    sign_api_key: str
    ffmpeg_path: str
    ffprobe_path: str
    streamlink_path: str
    ytdlp_path: str
    shutdown_timeout_seconds: float
    app_home: Path

    def sync_database_url(self) -> str:
        url = make_url(self.database_url)
        return _render_url(url.set(drivername="postgresql+psycopg"))

    @property
    def twitcasting_configured(self) -> bool:
        if self.twitcasting_access_token:
            return True
        return bool(self.twitcasting_client_id and self.twitcasting_client_secret)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "").strip()
    name = os.environ.get("POSTGRES_DB", "").strip()
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    if not all((user, password, host, name)):
        raise SettingsError(
            "DATABASE_URL か POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_HOST / POSTGRES_DB が必要です"
        )
    return _render_url(
        URL.create(
            "postgresql+asyncpg",
            username=user,
            password=password,
            host=host,
            port=port,
            database=name,
        )
    )


def _render_url(url: URL) -> str:
    username = quote(url.username or "", safe="")
    password = quote(str(url.password or ""), safe="")
    host = url.host or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{url.port}" if url.port else ""
    database = quote(url.database or "", safe="")
    return f"{url.drivername}://{username}:{password}@{host}{port}/{database}"


def load_settings() -> Settings:
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if len(secret) < 32 or secret in PLACEHOLDER_SECRETS:
        raise SettingsError("SESSION_SECRET は32文字以上の無作為な値にしてください")
    recordings = Path(os.environ.get("RECORDINGS_DIR", "/data/recordings")).expanduser()
    config_dir = Path(os.environ.get("CONFIG_DIR", "/data/config")).expanduser()
    recordings.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise SettingsError("LOG_LEVEL は DEBUG / INFO / WARNING / ERROR のいずれかです")
    port = int(os.environ.get("WEB_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise SettingsError("WEB_PORT が不正です")
    return Settings(
        database_url=_database_url(),
        session_secret=secret,
        recordings_dir=recordings,
        config_dir=config_dir,
        web_host=os.environ.get("WEB_HOST", "0.0.0.0"),
        web_port=port,
        display_timezone=os.environ.get("DISPLAY_TIMEZONE", "Asia/Tokyo"),
        log_level=level,
        cookie_secure=_bool("COOKIE_SECURE", False),
        twitcasting_client_id=os.environ.get("TWITCASTING_CLIENT_ID", "").strip(),
        twitcasting_client_secret=os.environ.get("TWITCASTING_CLIENT_SECRET", "").strip(),
        twitcasting_access_token=os.environ.get("TWITCASTING_ACCESS_TOKEN", "").strip(),
        sign_api_key=os.environ.get("SIGN_API_KEY", "").strip(),
        ffmpeg_path=os.environ.get("FFMPEG_PATH", "ffmpeg"),
        ffprobe_path=os.environ.get("FFPROBE_PATH", "ffprobe"),
        streamlink_path=os.environ.get("STREAMLINK_PATH", "streamlink"),
        ytdlp_path=os.environ.get("YTDLP_PATH", "yt-dlp"),
        shutdown_timeout_seconds=float(os.environ.get("SHUTDOWN_TIMEOUT_SECONDS", "20")),
        app_home=Path(os.environ.get("APP_HOME", Path.cwd())),
    )
