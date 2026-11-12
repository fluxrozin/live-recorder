import os

import pytest

from live_recorder.config import SettingsError, load_settings
from live_recorder.redact import redact


def test_redact_hides_urls_and_signatures():
    text = "failed https://cdn.example/live.flv?sign=abcd&expire=10 Authorization: Bearer secret-token"
    cleaned = redact(text)
    assert "abcd" not in cleaned
    assert "https://" not in cleaned
    assert "secret-token" not in cleaned


def test_settings_reject_placeholder_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "replace-with-a-random-string-of-at-least-32-chars")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://recorder:recorder@localhost/live")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "rec"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(SettingsError):
        load_settings()


def test_settings_build_url_from_parts(tmp_path, monkeypatch):
    for key in ("DATABASE_URL",):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("POSTGRES_USER", "recorder")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss word")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_DB", "live")
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "rec"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))
    settings = load_settings()
    assert "p%40ss+word" in settings.database_url or "p%40ss%20word" in settings.database_url
    assert settings.sync_database_url().startswith("postgresql+psycopg://")
    os.environ.get("POSTGRES_PASSWORD")


def test_sync_url_keeps_the_query_string():
    from sqlalchemy.engine import make_url

    from live_recorder.config import _render_url

    rendered = _render_url(make_url("postgresql+psycopg://user:p%40ss@localhost/live?sslmode=require"))
    assert "p%40ss" in rendered
    assert "sslmode=require" in rendered
    assert "immutabledict" not in rendered
