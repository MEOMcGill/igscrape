"""High-level Instagram scraping API.

Usage:
    async with InstagramScraper(db="accounts.db", headless=True) as scraper:
        result = await scraper.user_timeline("natgeo", "2024-01-01", "2024-01-07")
        # or in parallel:
        async for result in gather(
            scraper.user_timeline(h, "2024-01-01", "2024-01-07")
            for h in handles
        ):
            ...
"""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from .accounts_pool import AccountsPool
from .logger import logger
from .models import Query, ScrapingResult
from .worker_pool import WorkerPool


class InstagramScraper:
    def __init__(
        self,
        db: str | AccountsPool = "accounts.db",
        max_browser_sessions: int = 5,
        handles_per_rest: int = 100,
        headless: bool = False,
        mobile: bool = False,
    ):
        self.pool = db if isinstance(db, AccountsPool) else AccountsPool(db)
        self.max_browser_sessions = max_browser_sessions
        self.handles_per_rest = handles_per_rest
        self.headless = headless
        self.mobile = mobile
        self.worker_pool: WorkerPool | None = None
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self):
        async with self._init_lock:
            if self.worker_pool is None:
                self.worker_pool = WorkerPool(
                    pool=self.pool,
                    max_workers=self.max_browser_sessions,
                    handles_per_rest=self.handles_per_rest,
                    headless=self.headless,
                    mobile=self.mobile,
                )

    async def _submit(self, query: Query) -> ScrapingResult:
        await self._ensure_initialized()
        future = await self.worker_pool.submit_task(query)
        return await future

    async def user_timeline(
        self,
        handle: str,
        start_date: str,
        end_date: str,
        on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None,
        download_videos: bool = False,
        video_dir: str | Path | None = None,
        jsonl_path: str | Path | None = None,
    ) -> ScrapingResult:
        """Scrape a user's timeline.

        By default just returns a ScrapingResult. Optionally, as each replayed
        page arrives (these compose — any combination runs):
          - jsonl_path: append each raw post node as one JSON line to this file
            (opt-in streaming sink; the returned ScrapingResult is unchanged).
          - download_videos=True + video_dir: download every mp4 to video_dir.
          - on_new_posts(batch): called with each batch of newly-collected raw
            post nodes (not flattened).
        """
        return await self._submit(
            Query(
                endpoint="UserTimeline",
                query={
                    "handle": handle,
                    "start_date": start_date,
                    "end_date": end_date,
                },
                params={},
                runtime_options={
                    "on_new_posts": on_new_posts,
                    "download_videos": download_videos,
                    "video_dir": video_dir,
                    "jsonl_path": jsonl_path,
                },
            )
        )

    async def user_profile(self, handle: str) -> ScrapingResult:
        return await self._submit(
            Query(endpoint="UserProfile", query={"handle": handle}, params={})
        )

    async def post_by_shortcode(self, shortcode: str) -> ScrapingResult:
        return await self._submit(
            Query(
                endpoint="PostByShortcode",
                query={"shortcode": shortcode},
                params={},
            )
        )

    async def chaining(self, handle: str) -> ScrapingResult:
        return await self._submit(
            Query(endpoint="Chaining", query={"handle": handle}, params={})
        )

    async def search(
        self,
        keyword: str,
        max_posts: int = -1,
        on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None,
        download_videos: bool = False,
        video_dir: str | Path | None = None,
        jsonl_path: str | Path | None = None,
    ) -> ScrapingResult:
        """Collect posts from Instagram's keyword search (the SERP at
        /explore/search/keyword/?q=<keyword>).

        Replays the SERP request until `max_posts` posts are collected or the
        results stop yielding anything new. Search results are not reliably
        chronological, so there is no date cutoff. Streaming options (jsonl_path
        / on_new_posts / download_videos + video_dir) behave exactly as in
        user_timeline.
        """
        return await self._submit(
            Query(
                endpoint="Search",
                query={"keyword": keyword, "max_posts": max_posts},
                params={},
                runtime_options={
                    "on_new_posts": on_new_posts,
                    "download_videos": download_videos,
                    "video_dir": video_dir,
                    "jsonl_path": jsonl_path,
                },
            )
        )

    async def close(self):
        if self.worker_pool:
            await self.worker_pool.close()
            self.worker_pool = None

    async def restart(self):
        """Tear down the worker pool and its browser sessions so the next task
        lazily rebuilds a fresh pool.

        Used to recover a wedged browser session (e.g. after repeated timeouts)
        without aborting the run. worker.close() releases the account, so the
        rebuilt pool re-acquires it cleanly. A wedged session could make the
        graceful close() hang, so bound it and force re-init on timeout.
        """
        logger.info("Restarting scraper: tearing down worker pool to recover session")
        try:
            await asyncio.wait_for(self.close(), timeout=120)
        except asyncio.TimeoutError:
            logger.warning("worker pool did not close within 120s; forcing re-init")
            self.worker_pool = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False
