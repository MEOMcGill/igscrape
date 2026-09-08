"""Unit tests for the pool-state split WorkerPool logs before it starts workers.

`initialize` used to log only `max` and `active`, so a pool whose accounts were
all held read as healthy. `_unavailable` is the pure split behind the extra
numbers; it has to agree with get_available, so an `in_use` account whose holder
is gone counts as free rather than held.
"""

from datetime import timedelta

from igscrape.account import Account
from igscrape.accounts_pool import LEASE_MINUTES
from igscrape.utils import utc
from igscrape.worker_pool import WorkerPool


def _account(username, in_use=False, held_for=None, rest_for=None, idle_for=None):
    locks = {}
    if held_for is not None:
        locks["held_until"] = utc.now() + held_for
    if rest_for is not None:
        locks["locked_until"] = utc.now() + rest_for
    last_used = utc.now() - idle_for if idle_for is not None else None
    # password is required by the model and unused here.
    return Account(
        username=username, password="unused", in_use=in_use, locks=locks, last_used=last_used
    )


def test_free_pool_reports_nothing_unavailable():
    active = [_account("a"), _account("b")]
    assert WorkerPool._unavailable(active) == ([], [])


def test_live_claim_is_reported_as_held():
    active = [_account("a", in_use=True, held_for=timedelta(minutes=30)), _account("b")]
    assert WorkerPool._unavailable(active) == (["a"], [])


def test_lapsed_claim_is_not_held_because_get_available_would_take_it():
    active = [_account("a", in_use=True, held_for=timedelta(minutes=-1))]
    held, rested = WorkerPool._unavailable(active)
    assert (held, rested) == ([], [])
    assert len(active) - len(held) - len(rested) == 1


def test_unexpired_rest_lock_is_reported_as_rested():
    active = [_account("a", rest_for=timedelta(minutes=10)), _account("b")]
    assert WorkerPool._unavailable(active) == ([], ["a"])


def test_expired_rest_lock_is_not_reported():
    active = [_account("a", rest_for=timedelta(minutes=-10))]
    assert WorkerPool._unavailable(active) == ([], [])


def test_held_wins_over_a_rest_lock_so_the_account_is_counted_once():
    # Counted twice, `free` would go negative on a fully held pool.
    active = [
        _account("a", in_use=True, held_for=timedelta(minutes=30), rest_for=timedelta(minutes=30))
    ]
    held, rested = WorkerPool._unavailable(active)
    assert (held, rested) == (["a"], [])
    assert len(active) - len(held) - len(rested) == 0


def test_fully_held_pool_reports_zero_free():
    active = [
        _account("a", in_use=True, held_for=timedelta(minutes=30)),
        _account("b", in_use=True, held_for=timedelta(minutes=30)),
    ]
    held, rested = WorkerPool._unavailable(active)
    assert held == ["a", "b"]
    assert len(active) - len(held) - len(rested) == 0


def test_names_are_sorted_so_the_log_line_is_stable():
    active = [
        _account("z", in_use=True, held_for=timedelta(minutes=30)),
        _account("a", in_use=True, held_for=timedelta(minutes=30)),
    ]
    assert WorkerPool._unavailable(active)[0] == ["a", "z"]


# ---- claims made before leases existed ----


def test_leaseless_claim_still_working_counts_as_held():
    active = [_account("a", in_use=True, idle_for=timedelta(minutes=1))]
    assert WorkerPool._unavailable(active) == (["a"], [])


def test_leaseless_claim_gone_idle_does_not_count_as_held():
    active = [_account("a", in_use=True, idle_for=timedelta(minutes=LEASE_MINUTES + 5))]
    assert WorkerPool._unavailable(active) == ([], [])


def test_leaseless_claim_that_never_ran_does_not_count_as_held():
    # last_used NULL is reclaimable in get_available, so it must not read as held.
    active = [_account("a", in_use=True)]
    assert WorkerPool._unavailable(active) == ([], [])
