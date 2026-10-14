"""Unit tests for a worker that loses its account with nothing to rotate onto.

rotate_account raises NoAccountError and leaves the worker with no account. The
loop used to carry on regardless, so every task it dequeued afterwards failed in
milliseconds and the queue drained with nothing scraped. A worker in that state
now retires: its task goes to the workers still running, and only when none are
left do the waiting callers get the NoAccountError.
"""

import asyncio

import pytest

from igscrape.exceptions import NoAccountError
from igscrape.worker_pool import WorkerPool


class FakeQuery:
    def __init__(self, name):
        self.endpoint = "UserTimeline"
        self.query = {"handle": name}
        self.name = name


class FakeWorker:
    """Runs tasks until `fail_after` have started, then raises NoAccountError for
    every task from then on, as a worker with no account would."""

    def __init__(self, id, fail_after=None):
        self.id = id
        self.fail_after = fail_after
        self.started = 0
        self.ran: list[str] = []

    async def execute_task(self, query):
        self.started += 1
        await asyncio.sleep(0.01)
        if self.fail_after is not None and self.started > self.fail_after:
            raise NoAccountError(f"Worker {self.id}: no account available for rotation")
        self.ran.append(query.name)
        return f"result:{query.name}"

    async def drop_session(self):
        pass


def _pool(workers):
    pool = WorkerPool.__new__(WorkerPool)     # bypass __init__'s AccountsPool
    pool.task_queue = asyncio.Queue()
    pool._shutdown = False
    pool._initialized = True
    pool.workers = list(workers)
    pool.worker_tasks = []
    pool._retired = set()
    pool._init_lock = asyncio.Lock()
    return pool


async def _start(pool):
    tasks = [asyncio.create_task(pool._worker_loop(w)) for w in pool.workers]
    await asyncio.sleep(0)
    return tasks


async def _stop(pool, tasks):
    pool._shutdown = True
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)


async def _body_test_survivor_runs_the_retired_workers_tasks():
    dead = FakeWorker("worker-0", fail_after=0)
    alive = FakeWorker("worker-1")
    pool = _pool([dead, alive])
    tasks = await _start(pool)

    futures = [await pool.submit_task(FakeQuery(f"q{i}")) for i in range(6)]
    results = await asyncio.wait_for(asyncio.gather(*futures), timeout=3)

    assert results == [f"result:q{i}" for i in range(6)]
    assert dead.started == 1, "a worker with no account must stop taking tasks"
    assert sorted(alive.ran) == [f"q{i}" for i in range(6)]
    await _stop(pool, tasks)


async def _body_test_last_worker_retiring_fails_every_waiting_caller():
    worker = FakeWorker("worker-0", fail_after=1)
    pool = _pool([worker])
    tasks = await _start(pool)

    futures = [await pool.submit_task(FakeQuery(f"q{i}")) for i in range(50)]
    results = await asyncio.wait_for(
        asyncio.gather(*futures, return_exceptions=True), timeout=3
    )

    assert results[0] == "result:q0"
    assert all(isinstance(r, NoAccountError) for r in results[1:])
    assert worker.started == 2, "the queue must be failed, not walked task by task"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)


async def _body_test_submit_after_every_worker_retired_raises():
    worker = FakeWorker("worker-0", fail_after=0)
    pool = _pool([worker])
    tasks = await _start(pool)
    with pytest.raises(NoAccountError):
        await asyncio.wait_for(await pool.submit_task(FakeQuery("q0")), timeout=3)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    with pytest.raises(NoAccountError):
        await pool.submit_task(FakeQuery("q1"))


def test_survivor_runs_the_retired_workers_tasks():
    asyncio.run(_body_test_survivor_runs_the_retired_workers_tasks())

def test_last_worker_retiring_fails_every_waiting_caller():
    asyncio.run(_body_test_last_worker_retiring_fails_every_waiting_caller())

def test_submit_after_every_worker_retired_raises():
    asyncio.run(_body_test_submit_after_every_worker_retired_raises())
