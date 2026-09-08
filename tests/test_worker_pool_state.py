"""Unit tests for the pool-state split WorkerPool logs before it starts workers.

`initialize` used to log only `max` and `active`, so a pool whose accounts were
all held read as healthy — active counts an account another run holds, and one
whose lock a killed run never released. `_unavailable` is the pure split behind
the extra numbers, so exercise it directly with real Account records.
"""

from datetime import timedelta

from igscrape.account import Account
from igscrape.utils import utc
from igscrape.worker_pool import WorkerPool


def _account(username, in_use=False, locked_for=None):
    locks = {}
    if locked_for is not None:
        locks["locked_until"] = utc.now() + locked_for
    # password is required by the model and unused here.
    return Account(username=username, password="unused", in_use=in_use, locks=locks)


def test_free_pool_reports_nothing_unavailable():
    active = [_account("a"), _account("b")]
    assert WorkerPool._unavailable(active) == ([], [])


def test_in_use_account_is_reported_as_held():
    active = [_account("a", in_use=True), _account("b")]
    assert WorkerPool._unavailable(active) == (["a"], [])


def test_unexpired_lock_is_reported_as_rested():
    active = [_account("a", locked_for=timedelta(minutes=10)), _account("b")]
    assert WorkerPool._unavailable(active) == ([], ["a"])


def test_expired_lock_is_not_reported():
    # get_available treats a lapsed locked_until as free, so this must agree.
    active = [_account("a", locked_for=timedelta(minutes=-10))]
    assert WorkerPool._unavailable(active) == ([], [])


def test_in_use_wins_over_a_lock_so_the_account_is_counted_once():
    # Counted twice, `free` would go negative on a fully held pool.
    active = [_account("a", in_use=True, locked_for=timedelta(minutes=10))]
    held, rested = WorkerPool._unavailable(active)
    assert (held, rested) == (["a"], [])
    assert len(active) - len(held) - len(rested) == 0


def test_fully_held_pool_reports_zero_free():
    active = [_account("a", in_use=True), _account("b", in_use=True)]
    held, rested = WorkerPool._unavailable(active)
    assert held == ["a", "b"]
    assert len(active) - len(held) - len(rested) == 0


def test_names_are_sorted_so_the_log_line_is_stable():
    active = [_account("z", in_use=True), _account("a", in_use=True)]
    assert WorkerPool._unavailable(active)[0] == ["a", "z"]
