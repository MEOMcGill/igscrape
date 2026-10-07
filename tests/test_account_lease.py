"""Unit tests for the lease that makes an abandoned in_use claim expire.

in_use used to be a plain boolean cleared only by release_account, so a holder
killed mid-run stranded its account until someone edited the DB by hand. A claim
now carries `locks.held_until`, renewed while the holder works, and get_available
takes an account back once that lapses. These run against a real SQLite file so
the SQL is what is under test, not a re-implementation of it.
"""

import asyncio
import tempfile
from pathlib import Path

from igscrape.accounts_pool import LEASE_MINUTES, AccountsPool
from igscrape.db import execute, fetchone


async def _pool(*usernames):
    tmp = Path(tempfile.mkdtemp()) / "accounts.db"
    pool = AccountsPool(db_file=str(tmp))
    for u in usernames:
        await pool.add_account(u, "unused")
        await pool.set_active(u, True, None)
    return pool


async def _locks(pool, username):
    rs = await fetchone(
        pool._db_file, f"SELECT locks FROM accounts WHERE username = '{username}'"
    )
    return rs["locks"]


async def _held_until(pool, username):
    rs = await fetchone(
        pool._db_file,
        "SELECT json_extract(locks, '$.held_until') AS h FROM accounts "
        f"WHERE username = '{username}'",
    )
    return rs["h"]


async def _set_held_until(pool, username, expr):
    await execute(
        pool._db_file,
        f"UPDATE accounts SET locks = json_set(locks, '$.held_until', {expr}) "
        f"WHERE username = '{username}'",
    )


# ---- acquiring claims a lease ----


async def _body_acquire_sets_a_future_lease():
    pool = await _pool("a")
    got = await pool.get_available()
    assert got is not None and got.username == "a"
    rs = await fetchone(
        pool._db_file,
        "SELECT json_extract(locks, '$.held_until') > datetime('now') AS future "
        "FROM accounts WHERE username = 'a'",
    )
    assert rs["future"] == 1


def test_acquire_sets_a_future_lease():
    asyncio.run(_body_acquire_sets_a_future_lease())


async def _body_a_live_claim_is_not_handed_out_twice():
    pool = await _pool("a")
    assert await pool.get_available() is not None
    # Same pool, second caller: the claim is live, so there is nothing free.
    assert await pool.get_available() is None


def test_a_live_claim_is_not_handed_out_twice():
    asyncio.run(_body_a_live_claim_is_not_handed_out_twice())


# ---- the point of the change ----


async def _body_lapsed_claim_is_reclaimed():
    pool = await _pool("a")
    await pool.get_available()
    # The holder died: it stopped renewing and the lease fell into the past.
    await _set_held_until(pool, "a", "datetime('now', '-1 minutes')")
    got = await pool.get_available()
    assert got is not None and got.username == "a", "a dead holder must not strand the account"


def test_lapsed_claim_is_reclaimed():
    asyncio.run(_body_lapsed_claim_is_reclaimed())


async def _body_reclaiming_installs_a_fresh_lease():
    pool = await _pool("a")
    await pool.get_available()
    await _set_held_until(pool, "a", "datetime('now', '-1 minutes')")
    await pool.get_available()
    rs = await fetchone(
        pool._db_file,
        "SELECT json_extract(locks, '$.held_until') > datetime('now') AS future "
        "FROM accounts WHERE username = 'a'",
    )
    assert rs["future"] == 1


def test_reclaiming_installs_a_fresh_lease():
    asyncio.run(_body_reclaiming_installs_a_fresh_lease())


# ---- renewal ----


async def _body_renew_pushes_the_lease_out():
    pool = await _pool("a")
    await pool.get_available()
    await _set_held_until(pool, "a", "datetime('now', '+1 minutes')")
    before = await _held_until(pool, "a")
    await pool.renew_lease("a")
    after = await _held_until(pool, "a")
    assert after > before
    # ...and far enough out that it is a full window, not a nudge.
    rs = await fetchone(
        pool._db_file,
        "SELECT json_extract(locks, '$.held_until') > datetime('now', "
        f"'+{LEASE_MINUTES - 1} minutes') AS ok FROM accounts WHERE username = 'a'",
    )
    assert rs["ok"] == 1


def test_renew_pushes_the_lease_out():
    asyncio.run(_body_renew_pushes_the_lease_out())


async def _body_renew_cannot_steal_back_a_reclaimed_account():
    pool = await _pool("a")
    await pool.get_available()
    await pool.release_account("a")          # whoever took it over released it
    await pool.renew_lease("a")              # the old holder wakes up and renews
    got = await pool.get_available()
    assert got is not None, "a renewal from a released holder must not re-claim it"


def test_renew_cannot_steal_back_a_reclaimed_account():
    asyncio.run(_body_renew_cannot_steal_back_a_reclaimed_account())


# ---- release ----


async def _body_release_clears_the_lease():
    pool = await _pool("a")
    await pool.get_available()
    await pool.release_account("a")
    assert await _held_until(pool, "a") is None
    assert "held_until" not in await _locks(pool, "a")


def test_release_clears_the_lease():
    asyncio.run(_body_release_clears_the_lease())


# ---- claims made before leases existed ----


async def _body_legacy_claim_with_stale_activity_is_reclaimed():
    pool = await _pool("a")
    await pool.get_available()
    await execute(
        pool._db_file,
        "UPDATE accounts SET locks = json_remove(locks, '$.held_until'), "
        f"last_used = datetime('now', '-{LEASE_MINUTES + 5} minutes') WHERE username = 'a'",
    )
    got = await pool.get_available()
    assert got is not None, "an idle pre-lease claim must not strand the account"


def test_legacy_claim_with_stale_activity_is_reclaimed():
    asyncio.run(_body_legacy_claim_with_stale_activity_is_reclaimed())


async def _body_legacy_claim_still_working_is_left_alone():
    pool = await _pool("a")
    await pool.get_available()
    # No lease, but active recently: a live holder on older code during a rollout.
    await execute(
        pool._db_file,
        "UPDATE accounts SET locks = json_remove(locks, '$.held_until'), "
        "last_used = datetime('now', '-1 minutes') WHERE username = 'a'",
    )
    assert (
        await pool.get_available() is None
    ), "must not take an account out from under a live holder"


def test_legacy_claim_still_working_is_left_alone():
    asyncio.run(_body_legacy_claim_still_working_is_left_alone())


# ---- the resting lock is untouched by all this ----


async def _body_rested_account_is_still_withheld():
    pool = await _pool("a")
    await pool.get_available()
    await pool.release_account("a")
    await pool.lock_until("a", "datetime('now', '+15 minutes')")
    assert await pool.get_available() is None


def test_rested_account_is_still_withheld():
    asyncio.run(_body_rested_account_is_still_withheld())


async def _body_free_account_is_preferred_over_a_lapsed_one():
    pool = await _pool("a", "b")
    first = await pool.get_available()
    second = await pool.get_available()
    assert {first.username, second.username} == {"a", "b"}
    assert await pool.get_available() is None


def test_free_account_is_preferred_over_a_lapsed_one():
    asyncio.run(_body_free_account_is_preferred_over_a_lapsed_one())
