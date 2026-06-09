"""Pool of Workers consuming tasks from a shared asyncio.Queue."""

import asyncio

from .accounts_pool import AccountsPool
from .exceptions import NoAccountError
from .logger import logger
from .models import Query
from .worker import HANDLES_PER_REST, Worker


class WorkerPool:
    def __init__(
        self,
        pool: AccountsPool,
        max_workers: int = 5,
        handles_per_rest: int = HANDLES_PER_REST,
        headless: bool = False,
        mobile: bool = False,
    ):
        self.pool = pool
        self.max_workers = max_workers
        self.handles_per_rest = handles_per_rest
        self.headless = headless
        self.mobile = mobile

        self.workers: list[Worker] = []
        self.worker_tasks: list[asyncio.Task] = []
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._initialized = False
        self._shutdown = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> int:
        if self._initialized:
            return len(self.workers)

        active = await self.pool.get_active_accounts()
        if not active:
            raise NoAccountError("No active accounts in pool")

        num = max(1, min(self.max_workers, len(active)))
        logger.info(
            f"WorkerPool initializing {num} workers "
            f"(max={self.max_workers}, active={len(active)})"
        )

        for i in range(num):
            try:
                worker = await Worker.create(
                    id=f"worker-{i}",
                    pool=self.pool,
                    handles_per_rest=self.handles_per_rest,
                    headless=self.headless,
                    mobile=self.mobile,
                )
                self.workers.append(worker)
                self.worker_tasks.append(
                    asyncio.create_task(self._worker_loop(worker))
                )
            except NoAccountError:
                logger.warning(
                    f"WorkerPool: only created {len(self.workers)}/{num} workers"
                )
                break

        if not self.workers:
            raise NoAccountError("Failed to create any workers")

        self._initialized = True
        return len(self.workers)

    async def _worker_loop(self, worker: Worker):
        logger.info(f"{worker.id} loop started")
        while not self._shutdown:
            try:
                query, future = await asyncio.wait_for(
                    self.task_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            logger.info(f"{worker.id} processing {query.endpoint} {query.query}")
            try:
                result = await worker.execute_task(query)
                future.set_result(result)
            except Exception as e:
                logger.error(f"{worker.id} task failed: {e}")
                future.set_exception(e)
            finally:
                self.task_queue.task_done()

        logger.info(f"{worker.id} loop exiting")

    async def submit_task(self, query: Query) -> asyncio.Future:
        async with self._init_lock:
            if not self._initialized:
                await self.initialize()

        future = asyncio.get_running_loop().create_future()
        await self.task_queue.put((query, future))
        return future

    async def close(self):
        if not self._initialized:
            return
        self._shutdown = True
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        for worker in self.workers:
            await worker.close()
        self.workers = []
        self.worker_tasks = []
        self._initialized = False
        self._shutdown = False

    async def force_restart(self):
        """Tear down and reset the pool so the next task rebuilds it fresh.

        Unlike close(), this CANCELS the worker loop tasks instead of awaiting
        them: a worker can be wedged inside a hung browser op in
        execute_task(), and close()'s `await asyncio.gather(worker_tasks)` would
        then block forever. Every step here is time-bounded so a wedged browser
        can't stall recovery, and each account is released up front so a hanging
        browser-close can't strand it as in_use=1 (which would NoAccountError
        the re-initialize). Leaves the pool uninitialized; the next
        submit_task() builds new workers + a fresh BrowserSession.
        """
        self._shutdown = True
        for task in self.worker_tasks:
            task.cancel()
        if self.worker_tasks:
            # Bounded: returns after the timeout even if a task won't unwind.
            await asyncio.wait(self.worker_tasks, timeout=30)
        for worker in self.workers:
            account = getattr(worker, "current_account", None)
            if account is not None:
                try:
                    await asyncio.wait_for(
                        self.pool.release_account(account.username), timeout=10
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(worker.close(), timeout=30)
            except Exception:
                pass
        self.workers = []
        self.worker_tasks = []
        self._initialized = False
        self._shutdown = False
        logger.info("WorkerPool force-restarted; will re-initialize on next task")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False
