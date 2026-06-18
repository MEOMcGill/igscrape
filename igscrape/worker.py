"""Worker: acquires an account, runs a scraping task, routes the result.

Retry/rotate/crash behavior follows the result-code taxonomy
(retry_cases / success_cases / crash_cases).
"""

import asyncio
from typing import Callable, Optional

from .account import Account
from .accounts_pool import AccountsPool
from .browser_session import BrowserSession
from .exceptions import (
    AccountBannedError,
    FailedLoginError,
    NoAccountError,
    RateLimitError,
    TargetCrashedError,
)
from .logger import logger
from .models import Query, ScrapingResult
from .parsers import (
    post_authorship_filterer,
    post_date_filterer,
    post_flattener,
)

# Result-code taxonomy: retry / success / crash cases
RETRY_CASES = {
    "bad internet",
    "timeout error",
    "something went wrong - reload",
    "failed to load",
}
SUCCESS_CASES = {
    "success",
    "scraped until user-specified starting date was reached",
    "scraped until first ever post was reached",
    "no posts",
    "account is private",
    "profile is not available",
}
CRASH_CASES = {
    "target crashed",
    "logged out while scraping",
}

# Rotation policy: rest after 100 handles
HANDLES_PER_REST = 100
REST_SECONDS = 300
# consume_post_scraper.py:42
RETRY_MINUTES = 15


class Worker:
    """Runs tasks for one acquired account until rotation or shutdown."""

    ENDPOINT_METHODS = {
        "UserTimeline": "user_timeline",
        "UserProfile": "user_profile",
        "PostByShortcode": "post_by_shortcode",
        "Chaining": "chaining",
        "Search": "search",
    }

    def __init__(
        self,
        id: str,
        pool: AccountsPool,
        handles_per_rest: int = HANDLES_PER_REST,
        headless: bool = False,
        mobile: bool = False,
    ):
        self.id = id
        self.pool = pool
        self.handles_per_rest = handles_per_rest
        self.headless = headless
        self.mobile = mobile

        self.current_account: Optional[Account] = None
        self.handles_scraped: int = 0
        self._initialized = False
        # Persistent browser session, reused across tasks for the lifetime of
        # the current account. Recreated on rotation / crash / logout. Callers
        # who want a fresh browser per handle should instead recreate the
        # InstagramScraper per handle (one worker => one session here).
        self.session: Optional[BrowserSession] = None

    @classmethod
    async def create(
        cls,
        id: str,
        pool: AccountsPool,
        handles_per_rest: int = HANDLES_PER_REST,
        headless: bool = False,
        mobile: bool = False,
    ) -> "Worker":
        instance = cls(
            id=id,
            pool=pool,
            handles_per_rest=handles_per_rest,
            headless=headless,
            mobile=mobile,
        )
        if not await instance.initialize():
            raise NoAccountError(f"Worker {id}: no account available")
        return instance

    async def __aenter__(self):
        if not self._initialized:
            if not await self.initialize():
                raise NoAccountError(f"Worker {self.id}: no account available")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def initialize(self) -> bool:
        account = await self.pool.get_available()
        if not account:
            return False
        self.current_account = account
        self.handles_scraped = 0
        self._initialized = True
        logger.info(f"Worker {self.id} acquired account {account.username}")
        return True

    async def close(self):
        await self._close_session()
        if self.current_account:
            await self.pool.release_account(self.current_account.username)
            self.current_account = None
        self.handles_scraped = 0
        self._initialized = False

    async def _ensure_session(self) -> BrowserSession:
        """Lazily create + initialize the persistent session for the current
        account, reusing it across tasks."""
        if self.session is None:
            self.session = BrowserSession(
                account=self.current_account,
                pool=self.pool,
                headless=self.headless,
                mobile=self.mobile,
            )
            await self.session.initialize()
        return self.session

    async def _close_session(self):
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None

    async def execute_task(self, task: Query) -> ScrapingResult:
        """Run one task. Applies the IG result-code taxonomy to decide what
        to do on failure — retry (same or rotated account), rotate, or raise.
        """
        # batch_size=100 rotation rule
        if self.handles_scraped >= self.handles_per_rest:
            logger.info(
                f"Worker {self.id}: scraped {self.handles_scraped} handles, "
                f"rotating account {self.current_account.username}"
            )
            await self.rotate_account()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                session = await self._ensure_session()
                method = self._get_scraping_method(session, task.endpoint)
                result: ScrapingResult = await method(
                    **task.query, **(task.runtime_options or {})
                )

                # Successful single-shot or timeline scrape
                if result.result in SUCCESS_CASES:
                    # Post-process UserTimeline results
                    if task.endpoint == "UserTimeline" and result.result not in (
                        "no posts",
                        "account is private",
                        "profile is not available",
                    ):
                        handle = task.query["handle"]
                        start = task.query["start_date"]
                        end = task.query["end_date"]
                        result.posts = post_authorship_filterer(
                            handle,
                            post_date_filterer(
                                post_flattener(result.posts), start, end
                            ),
                        )
                    # Search results aren't from one author, so only flatten —
                    # no authorship filter. Posts are already XDTMediaDict, so
                    # post_flattener passes them through unchanged.
                    elif task.endpoint == "Search":
                        result.posts = post_flattener(result.posts)
                    self.handles_scraped += 1
                    return result

                if result.result in RETRY_CASES:
                    logger.warning(
                        f"Worker {self.id}: retryable result '{result.result}' "
                        f"for {task.query}, attempt {attempt + 1}/{max_retries}"
                    )
                    # 'failed to load' means this account is rate-limited;
                    # lock it out and rotate before the next attempt.
                    if result.result == "failed to load":
                        await self.pool.lock_until(
                            self.current_account.username,
                            f"datetime('now', '+{RETRY_MINUTES} minutes')",
                        )
                        await self.rotate_account()
                    await asyncio.sleep(2)
                    continue

                if result.result in CRASH_CASES:
                    logger.error(
                        f"Worker {self.id}: crash result '{result.result}' for "
                        f"{task.query} on account {self.current_account.username}"
                    )
                    if result.result == "logged out while scraping":
                        await self.pool.mark_inactive(
                            self.current_account.username,
                            "Logged out mid-scrape",
                        )
                        await self.rotate_account()
                        continue
                    # target crashed / anything else — the tab is dead, so drop
                    # the session (recreated on the next task) and return the
                    # result so the caller sees the failure.
                    await self._close_session()
                    return result

                # Unknown result code — return as-is
                logger.warning(f"Unknown result code: {result.result}")
                return result

            except FailedLoginError as e:
                logger.warning(
                    f"Worker {self.id}: login failed for "
                    f"{self.current_account.username}: {e}"
                )
                await self.pool.mark_inactive(
                    self.current_account.username, f"Login failed: {e}"
                )
                await self.rotate_account()
            except AccountBannedError as e:
                logger.warning(
                    f"Worker {self.id}: account banned {self.current_account.username}: {e}"
                )
                await self.pool.mark_inactive(
                    self.current_account.username, f"Banned: {e}"
                )
                await self.rotate_account()
            except RateLimitError as e:
                logger.warning(
                    f"Worker {self.id}: rate limited {self.current_account.username}: {e}"
                )
                await self.pool.lock_until(
                    self.current_account.username,
                    f"datetime('now', '+{RETRY_MINUTES} minutes')",
                )
                await self.rotate_account()
            except TargetCrashedError as e:
                logger.error(
                    f"Worker {self.id}: target crashed for "
                    f"{self.current_account.username}: {e}"
                )
                await self.rotate_account()

        raise RuntimeError(
            f"Worker {self.id}: failed to execute task after {max_retries} retries"
        )

    async def rotate_account(self):
        """Release current account with a 5-minute cooldown, then acquire the
        next available one.

        With a single-account pool there is nothing else to switch to, so we
        wait for the cooldown to expire and re-acquire the same account — this
        gives the periodic-rest behavior the production scraper relied on
        (rest 5 min every HANDLES_PER_REST handles) instead of crashing.
        """
        await self._close_session()
        if self.current_account:
            await self.pool.lock_until(
                self.current_account.username,
                "datetime('now', '+5 minutes')",
            )
            await self.pool.release_account(self.current_account.username)
            logger.info(
                f"Worker {self.id} released {self.current_account.username} "
                f"(5-minute cooldown)"
            )
            self.current_account = None

        self.handles_scraped = 0
        self._initialized = False

        account = await self.pool.get_available_or_wait()
        if not account:
            raise NoAccountError(
                f"Worker {self.id}: no account available for rotation"
            )
        self.current_account = account
        self._initialized = True
        logger.info(f"Worker {self.id} acquired account {account.username}")

    def _get_scraping_method(
        self, session: BrowserSession, endpoint: str
    ) -> Callable:
        if endpoint not in self.ENDPOINT_METHODS:
            raise ValueError(
                f"Unsupported endpoint: {endpoint}. "
                f"Supported: {list(self.ENDPOINT_METHODS.keys())}"
            )
        return getattr(session, self.ENDPOINT_METHODS[endpoint])
