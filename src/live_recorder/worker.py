from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy import text

from live_recorder.config import load_settings
from live_recorder.db.session import Database
from live_recorder.logging import setup_logging
from live_recorder.migrate import upgrade
from live_recorder.monitor.supervisor import Supervisor
from live_recorder.platforms.twitcasting import RateLimiter, TwitCastingClient

log = logging.getLogger(__name__)
LOCK_KEY = 824201


async def _lock(db: Database):
    connection = await db.engine.connect()
    for _ in range(30):
        result = await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        await connection.commit()
        if result.scalar():
            return connection
        await asyncio.sleep(1)
    await connection.close()
    raise SystemExit("別のワーカーが稼働中です")


async def _run() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    if settings.sign_api_key:
        import os

        os.environ["SIGN_API_KEY"] = settings.sign_api_key
    upgrade()
    db = Database(settings)
    connection = await _lock(db)
    tc = TwitCastingClient(
        client_id=settings.twitcasting_client_id,
        client_secret=settings.twitcasting_client_secret,
        access_token=settings.twitcasting_access_token,
        limiter=RateLimiter(50, 60),
    )
    supervisor = Supervisor(settings, db, tc)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, supervisor.request_stop)
    try:
        await supervisor.run()
    finally:
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
        await connection.commit()
        await connection.close()
        await tc.__aexit__(None, None, None)
        await db.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
