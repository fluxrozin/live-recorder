from __future__ import annotations

import asyncio
import getpass
import os
import sys

from live_recorder.config import load_settings
from live_recorder.db.repository import Repository
from live_recorder.db.runtime import ensure_runtime
from live_recorder.db.session import Database
from live_recorder.migrate import upgrade


def hash_password(password: str) -> str:
    from pwdlib import PasswordHash

    return PasswordHash.recommended().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    from pwdlib import PasswordHash

    try:
        return bool(PasswordHash.recommended().verify(password, password_hash))
    except Exception:
        return False


async def _create_admin(username: str, password: str, reset: bool) -> None:
    if len(password) < 12:
        raise SystemExit("パスワードは12文字以上にしてください")
    settings = load_settings()
    upgrade()
    db = Database(settings)
    try:
        async with db.session() as session:
            await ensure_runtime(session)
            repo = Repository(session)
            existing = await repo.user_by_name(username)
            if existing and not reset:
                raise SystemExit("その管理者は既にいます。更新するときは ADMIN_RESET_PASSWORD=1 を指定してください")
            hashed = hash_password(password)
            if existing:
                existing.password_hash = hashed
                existing.is_active = True
            else:
                await repo.create_user(username, hashed)
            await session.commit()
    finally:
        await db.dispose()
    print(f"管理者 {username} を保存しました")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "create-admin":
        raise SystemExit("使い方: python -m live_recorder.cli create-admin")
    username = os.environ.get("ADMIN_USERNAME", "admin").strip()
    password = os.environ.get("ADMIN_INITIAL_PASSWORD", "")
    if not password:
        password = getpass.getpass("管理者パスワード: ")
    reset = os.environ.get("ADMIN_RESET_PASSWORD", "").lower() in {"1", "true", "yes"}
    asyncio.run(_create_admin(username, password, reset))


if __name__ == "__main__":
    main()
