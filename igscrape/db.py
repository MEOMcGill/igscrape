import asyncio
import os.path
import random
import sqlite3
from collections import defaultdict

import aiosqlite

from .logger import logger
from .utils import get_home_dir_path

_lock = asyncio.Lock()


def lock_retry(max_retries=10):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    async with _lock:
                        return await func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if i == max_retries - 1 or "database is locked" not in str(e):
                        raise e
                    await asyncio.sleep(random.uniform(0.5, 1.0))

        return wrapper

    return decorator


async def migrate(db: aiosqlite.Connection):
    async with db.execute("PRAGMA user_version") as cur:
        rs = await cur.fetchone()
        current_version = rs[0] if rs else 0

    MIGRATIONS = [
        (1, migrate_v1),
    ]

    for version, migration_fn in MIGRATIONS:
        if current_version < version:
            logger.info(f"Running migration to v{version}")
            await migration_fn(db)
            await db.execute(f"PRAGMA user_version = {version}")
            await db.commit()


async def migrate_v1(db: aiosqlite.Connection):
    """Initial schema. Instagram accounts use `username` as the primary identifier."""
    qs = """
    CREATE TABLE IF NOT EXISTS accounts (
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password TEXT NOT NULL,
        email TEXT DEFAULT NULL COLLATE NOCASE,
        email_password TEXT DEFAULT NULL,
        phone_number TEXT DEFAULT NULL COLLATE NOCASE,
        active BOOLEAN DEFAULT FALSE NOT NULL,
        locks TEXT DEFAULT '{}' NOT NULL,
        scroll_count_per_endpoint_total TEXT DEFAULT '{}' NOT NULL,
        cookies TEXT DEFAULT '[]' NOT NULL,
        twofa_id TEXT DEFAULT NULL,
        proxy_server TEXT DEFAULT NULL,
        proxy_username TEXT DEFAULT NULL,
        proxy_password TEXT DEFAULT NULL,
        fingerprint TEXT DEFAULT NULL,
        os TEXT DEFAULT 'macos',
        error_msg TEXT DEFAULT NULL,
        last_used TEXT DEFAULT NULL,
        in_use BOOLEAN DEFAULT FALSE NOT NULL,
        handles_scraped_since_rest INTEGER DEFAULT 0 NOT NULL,
        scroll_count_overall_24h INTEGER DEFAULT 0 NOT NULL,
        _tx TEXT DEFAULT NULL
    );"""
    await db.execute(qs)

    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email) WHERE email IS NOT NULL"
    )


class DB:
    _init_once: defaultdict[str, bool] = defaultdict(bool)

    def __init__(self, db_path):
        self.db_path: str = str(os.path.join(get_home_dir_path(), "db", db_path))
        self.conn = None

    async def __aenter__(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row

        if not self._init_once[self.db_path]:
            await migrate(db)
            self._init_once[self.db_path] = True

        self.conn = db
        return db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            await self.conn.commit()
            await self.conn.close()


@lock_retry()
async def execute(db_path: str, qs: str, params: dict | None = None):
    async with DB(db_path) as db:
        await db.execute(qs, params)


@lock_retry()
async def fetchone(db_path: str, qs: str, params: dict | None = None):
    async with DB(db_path) as db:
        async with db.execute(qs, params) as cur:
            row = await cur.fetchone()
            return row


@lock_retry()
async def fetchall(db_path: str, qs: str, params: dict | None = None):
    async with DB(db_path) as db:
        async with db.execute(qs, params) as cur:
            rows = await cur.fetchall()
            return rows


@lock_retry()
async def executemany(db_path: str, qs: str, params: list[dict]):
    async with DB(db_path) as db:
        await db.executemany(qs, params)
