"""Unit tests for rebuilding a browser session Playwright can no longer reach.

A session that goes away mid-run is held by the Worker and handed to every task
still queued behind the one that hit it, so each of those failed instantly with
"Target page, context or browser has been closed". These tests pin down that a
dead session is dropped and rebuilt, and that a live session's Playwright errors
still reach the caller.
"""

import asyncio

import pytest
from playwright.async_api import Error as PWError

from igscrape.models import Query, ScrapingResult
from igscrape.worker import Worker


def _query(keyword="kw"):
    return Query(endpoint="Search", query={"keyword": keyword}, params={})


class FakeSession:
    """Stands in for BrowserSession: alive until killed, then raises like Playwright."""

    def __init__(self):
        self.alive = True
        self.searches = 0
        self.closed = 0
        self.die_after = None

    def is_alive(self) -> bool:
        return self.alive

    async def search(self, keyword, **kwargs):
        self.searches += 1
        if not self.alive:
            raise PWError("Page.goto: Target page, context or browser has been closed")
        if self.die_after is not None and self.searches >= self.die_after:
            self.alive = False
            raise PWError("Page.goto: Target page, context or browser has been closed")
        return ScrapingResult(
            query=_query(keyword),
            result="scraped until first ever post was reached",
        )

    async def close(self):
        self.closed += 1


class FakeAccount:
    username = "acct"


class FakePool:
    """Only what execute_task touches: it renews the account lease per task."""

    def __init__(self):
        self.renewals = 0

    async def renew_lease(self, username):
        self.renewals += 1


def _worker(sessions):
    """A Worker whose _ensure_session hands out `sessions` in order."""
    worker = Worker.__new__(Worker)      # bypass __init__'s AccountsPool
    worker.id = "worker-0"
    worker.current_account = FakeAccount()
    worker.pool = FakePool()
    worker.handles_scraped = 0
    worker.handles_per_rest = 100
    worker.session = None
    worker.built = 0

    async def _ensure_session():
        if worker.session is None:
            worker.session = sessions[worker.built]
            worker.built += 1
        return worker.session

    worker._ensure_session = _ensure_session
    return worker


async def _body_test_a_dead_session_is_rebuilt_and_the_task_succeeds():
    dead, fresh = FakeSession(), FakeSession()
    dead.die_after = 1
    worker = _worker([dead, fresh])

    result = await worker.execute_task(_query())

    assert result.result == "scraped until first ever post was reached"
    assert worker.built == 2, "the dead session was not replaced"
    assert dead.closed == 1, "the dead session was not closed"
    assert fresh.searches == 1


async def _body_test_later_tasks_are_not_poisoned_by_a_dead_session():
    """The regression: one closed browser failed every keyword behind it."""
    dead, fresh = FakeSession(), FakeSession()
    dead.die_after = 1
    worker = _worker([dead, fresh])

    for keyword in ("first", "second", "third"):
        assert (await worker.execute_task(_query(keyword))).posts == []

    assert worker.built == 2, "each task rebuilt its own session"
    assert fresh.searches == 3, "later tasks did not run on the fresh session"


async def _body_test_a_live_session_still_raises_playwright_errors():
    """A timeout on a healthy session is the caller's to see, not ours to retry."""
    session = FakeSession()

    async def _boom(keyword, **kwargs):
        session.searches += 1
        raise PWError("Timeout 30000ms exceeded")

    session.search = _boom
    worker = _worker([session])

    with pytest.raises(PWError, match="Timeout"):
        await worker.execute_task(_query())

    assert session.closed == 0, "a live session must not be dropped"
    assert session.searches == 1, "the error was retried instead of raised"


async def _body_test_a_session_that_never_comes_back_gives_up():
    sessions = [FakeSession() for _ in range(3)]
    for session in sessions:
        session.alive = False
    worker = _worker(sessions)

    with pytest.raises(RuntimeError, match="after 3 retries"):
        await worker.execute_task(_query())

    assert worker.built == 3
    assert worker.session is None, "the next task must start with no session"


def test_a_dead_session_is_rebuilt_and_the_task_succeeds():
    asyncio.run(_body_test_a_dead_session_is_rebuilt_and_the_task_succeeds())

def test_later_tasks_are_not_poisoned_by_a_dead_session():
    asyncio.run(_body_test_later_tasks_are_not_poisoned_by_a_dead_session())

def test_a_live_session_still_raises_playwright_errors():
    asyncio.run(_body_test_a_live_session_still_raises_playwright_errors())

def test_a_session_that_never_comes_back_gives_up():
    asyncio.run(_body_test_a_session_that_never_comes_back_gives_up())
