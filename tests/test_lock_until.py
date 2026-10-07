"""lock_until only ever extends an account's lock. Runs against a real SQLite file."""

import asyncio
import tempfile
from pathlib import Path

from igscrape.accounts_pool import AccountsPool
from igscrape.db import fetchone


async def _pool(*usernames):
    tmp = Path(tempfile.mkdtemp()) / "accounts.db"
    pool = AccountsPool(db_file=str(tmp))
    for u in usernames:
        await pool.add_account(u, "unused")
        await pool.set_active(u, True, None)
    return pool


async def _locked_until(pool, username):
    rs = await fetchone(
        pool._db_file,
        "SELECT json_extract(locks, '$.locked_until') AS l FROM accounts "
        f"WHERE username = '{username}'",
    )
    return rs["l"]


async def _body_shorter_lock_does_not_cut_a_longer_one():
    pool = await _pool("a")
    await pool.lock_until("a", "datetime('now', '+15 minutes')")
    long_lock = await _locked_until(pool, "a")
    await pool.lock_until("a", "datetime('now', '+5 minutes')")
    assert await _locked_until(pool, "a") == long_lock


def test_shorter_lock_does_not_cut_a_longer_one():
    asyncio.run(_body_shorter_lock_does_not_cut_a_longer_one())


async def _body_longer_lock_extends_a_shorter_one():
    pool = await _pool("a")
    await pool.lock_until("a", "datetime('now', '+5 minutes')")
    short_lock = await _locked_until(pool, "a")
    await pool.lock_until("a", "datetime('now', '+15 minutes')")
    assert await _locked_until(pool, "a") > short_lock


def test_longer_lock_extends_a_shorter_one():
    asyncio.run(_body_longer_lock_extends_a_shorter_one())


async def _body_locked_account_is_withheld():
    pool = await _pool("a")
    await pool.lock_until("a", "datetime('now', '+15 minutes')")
    assert await pool.get_available() is None


def test_locked_account_is_withheld():
    asyncio.run(_body_locked_account_is_withheld())
