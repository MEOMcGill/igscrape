"""Unit tests for WorkerPool cancellation propagation.

The pool hands the caller a future and runs the scrape in its own task, so a
caller that gives up (asyncio.wait_for) used to stop waiting while the scrape
kept running — holding the browser, so every later task started on an occupied
session and collected nothing. These tests pin down that abandoning the future
stops the work and drops the session, and that the normal paths are unaffected.
"""

import asyncio

import pytest

from igscrape.worker_pool import WorkerPool


class FakeQuery:
    """Minimal stand-in for models.Query — the loop logs .endpoint and .query."""

    def __init__(self, name):
        self.endpoint = "UserTimeline"
        self.query = {"handle": name}
        self.name = name

    def __str__(self):
        return self.name


class FakeWorker:
    """Stands in for Worker: records what ran, what was cancelled, and whether
    the session was dropped."""

    def __init__(self, delay=0.01, exc=None):
        self.id = "worker-0"
        self.delay = delay
        self.exc = exc
        self.started = 0
        self.completed = 0
        self.cancelled = 0
        self.dropped = 0

    async def execute_task(self, query):
        self.started += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.exc is not None:
            raise self.exc
        self.completed += 1
        return f"result:{query.name}"

    async def drop_session(self):
        self.dropped += 1


def _pool(worker):
    pool = WorkerPool.__new__(WorkerPool)     # bypass __init__'s AccountsPool
    pool.task_queue = asyncio.Queue()
    pool._shutdown = False
    pool._initialized = True
    pool.workers = [worker]
    pool.worker_tasks = []
    pool._init_lock = asyncio.Lock()
    return pool


async def _run_loop(pool, worker):
    task = asyncio.create_task(pool._worker_loop(worker))
    await asyncio.sleep(0)      # let the loop reach the queue
    return task


async def _stop(pool, loop_task):
    pool._shutdown = True
    try:
        await asyncio.wait_for(loop_task, timeout=3)
    except asyncio.TimeoutError:
        loop_task.cancel()


async def _body_test_result_is_delivered_normally():
    worker = FakeWorker()
    pool = _pool(worker)
    loop_task = await _run_loop(pool, worker)
    future = await pool.submit_task(FakeQuery("q1"))
    assert await asyncio.wait_for(future, timeout=3) == "result:q1"
    assert (worker.completed, worker.dropped) == (1, 0)
    await _stop(pool, loop_task)


async def _body_test_caller_timeout_cancels_the_scrape_and_drops_the_session():
    """The regression: the scrape used to keep running after the caller gave up."""
    worker = FakeWorker(delay=5)
    pool = _pool(worker)
    loop_task = await _run_loop(pool, worker)
    future = await pool.submit_task(FakeQuery("slow"))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(future, timeout=0.1)

    await asyncio.sleep(0.1)          # let the cancellation land
    assert worker.cancelled == 1, "the scrape was not cancelled"
    assert worker.completed == 0, "the scrape ran to completion despite the timeout"
    assert worker.dropped == 1, "the session must be dropped — it is mid-scrape"
    await _stop(pool, loop_task)


async def _body_test_next_task_still_runs_after_an_abandoned_one():
    """The consequence that mattered: one timeout used to poison every handle
    after it, which showed up as later handles collecting 0 posts."""
    worker = FakeWorker(delay=5)
    pool = _pool(worker)
    loop_task = await _run_loop(pool, worker)

    slow = await pool.submit_task(FakeQuery("slow"))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(slow, timeout=0.1)
    await asyncio.sleep(0.1)

    worker.delay = 0.01
    nxt = await pool.submit_task(FakeQuery("next"))
    assert await asyncio.wait_for(nxt, timeout=3) == "result:next"
    assert worker.completed == 1
    await _stop(pool, loop_task)


async def _body_test_future_cancelled_before_pickup_is_not_started():
    worker = FakeWorker()
    pool = _pool(worker)
    future = await pool.submit_task(FakeQuery("q"))
    future.cancel()                    # caller gone before the loop dequeues it
    loop_task = await _run_loop(pool, worker)
    await asyncio.sleep(0.1)
    assert worker.started == 0, "an abandoned task should not be started at all"
    await _stop(pool, loop_task)


async def _body_test_exception_still_reaches_the_caller():
    worker = FakeWorker(exc=RuntimeError("boom"))
    pool = _pool(worker)
    loop_task = await _run_loop(pool, worker)
    future = await pool.submit_task(FakeQuery("q"))
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(future, timeout=3)
    assert worker.dropped == 0, "an ordinary failure must not drop the session"
    await _stop(pool, loop_task)


def test_result_is_delivered_normally():
    asyncio.run(_body_test_result_is_delivered_normally())

def test_caller_timeout_cancels_the_scrape_and_drops_the_session():
    asyncio.run(_body_test_caller_timeout_cancels_the_scrape_and_drops_the_session())

def test_next_task_still_runs_after_an_abandoned_one():
    asyncio.run(_body_test_next_task_still_runs_after_an_abandoned_one())

def test_future_cancelled_before_pickup_is_not_started():
    asyncio.run(_body_test_future_cancelled_before_pickup_is_not_started())

def test_exception_still_reaches_the_caller():
    asyncio.run(_body_test_exception_still_reaches_the_caller())
