"""Camoufox-backed Playwright session with Instagram login + scrape methods.

Encapsulates the scraping behavior: selectors, timings, result codes, and
termination conditions.
"""

import asyncio
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from camoufox.async_api import AsyncNewBrowser
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    async_playwright,
    expect,
)

from .account import Account
from .accounts_pool import AccountsPool
from .downloaders import download_videos_from_posts
from .exceptions import FailedLoginError
from .logger import logger
from .models import Query, ScrapingResult
from .parsers import get_post_timestamp, post_flattener
from .response import InstagramResponseInterceptor
from .utils import get_device_os, internet_good

BASE_URL = "https://www.instagram.com/"


_POST_HREF_RE = re.compile(r"^/[A-Za-z0-9_.-]+/(?:p|reel)/[A-Za-z0-9_.-]+/?$")


def _is_post_href(href: str | None) -> bool:
    if not href:
        return False
    if href.startswith("/p/") or href.startswith("/reel/"):
        return True
    return _POST_HREF_RE.match(href) is not None


class BrowserSession:
    """Manages a single Instagram browser session for one scraping task."""

    # ==================== Initialization & lifecycle ====================

    def __init__(
        self,
        account: Account,
        pool: AccountsPool,
        headless: bool = False,
        mobile: bool = False,
    ):
        self.account = account
        self.pool = pool
        self.headless = headless
        self.mobile = mobile
        self.endpoint: str = ""

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.response_interceptor: Optional[InstagramResponseInterceptor] = None

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def initialize(self):
        logger.debug(
            f"BrowserSession.initialize() for {self.account.username}, headless={self.headless}"
        )
        self._pw = await async_playwright().start()

        proxy_settings = self._get_proxy_dict()

        self._browser = await AsyncNewBrowser(
            playwright=self._pw,
            humanize=True,
            headless="virtual" if self.headless else self.headless,
            proxy=proxy_settings,
            geoip=True if proxy_settings else False,
            os=get_device_os(),
            firefox_user_prefs={
                "browser.aboutwelcome.enabled": False,
                "browser.startup.firstrunSkipsHomepage": True,
                "browser.shell.checkDefaultBrowser": False,
                "datareporting.policy.dataSubmissionEnabled": False,
                "browser.cache.disk.enable": False,
                "browser.cache.memory.capacity": 0,
                "browser.sessionhistory.max_entries": 2,
                "browser.sessionhistory.max_total_viewers": 0,
                "dom.ipc.processCount.webIsolated": 1,
            },
        )

        self._context = await self._browser.new_context()
        self.page = await self._context.new_page()

        # Workaround for camoufox br/zstd decompression issue
        await self.page.set_extra_http_headers({"Accept-Encoding": "gzip, deflate"})

        self.response_interceptor = InstagramResponseInterceptor()
        self.response_interceptor.setup_interception(self.page)

        if self.account.cookies:
            try:
                await self._context.add_cookies(self.account.cookies)
                logger.info(
                    f"Injected {len(self.account.cookies)} cookies for {self.account.username}"
                )
            except Exception as e:
                logger.warning(f"Failed to inject cookies: {e}")

        # Always land on instagram.com and decide whether to log in.
        await self.page.goto(BASE_URL, wait_until="domcontentloaded")
        # Wait 10s after the initial goto to let the page settle
        await asyncio.sleep(10)

        await self._handle_continue_reauth()

        if await self._need_to_log_in():
            ok = await self.login()
            if not ok:
                raise FailedLoginError(f"Login failed for {self.account.username}")

        logger.info(f"Browser session ready for {self.account.username}")

    async def _handle_continue_reauth(self):
        """Click through the 'Continue' reauth screen (passkey / saved-session
        one-click login). Instagram renders this two ways:
          - the passkey variant: <div role="button"> wrapping an inner element
            with aria-label="Continue" (post_scraper.py:283-301), and
          - the 'Continue as <user>' saved-session screen on /accounts/login/,
            whose button has no aria-label and is matched by its accessible
            name instead.
        We try each candidate locator in turn and click the first visible one."""
        candidates = (
            self.page.locator('[role="button"]:has([aria-label="Continue"])'),
            self.page.get_by_role("button", name="Continue"),
        )
        for continue_button in candidates:
            try:
                if (
                    await continue_button.count() > 0
                    and await continue_button.first.is_visible()
                ):
                    logger.info("'Continue' reauth screen detected — clicking")
                    await continue_button.first.click()
                    await asyncio.sleep(10)
                    await self._save_cookies()
                    return
            except Exception as e:
                logger.debug(f"Continue reauth candidate skipped: {e}")

    async def close(self):
        if self.response_interceptor:
            self.response_interceptor.stop_interception()
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        logger.info(f"Browser session closed for {self.account.username}")

    # ==================== Auth ====================

    async def _find_username_field(self) -> Locator | None:
        """Find the username input, checking aria-labels then name attribute
        (post_scraper.py:833-842)."""
        for label in (
            "Phone number, username, or email",
            "Mobile number, username, or email",
        ):
            field = self.page.get_by_label(label)
            if await field.count() > 0:
                return field
        field = self.page.locator('input[name="email"]')
        if await field.count() > 0:
            return field
        return None

    async def _find_password_field(self) -> Locator | None:
        """Find the password input, checking name attribute first to avoid
        strict-mode collisions with dialogs that get_by_label('Password') also
        matches (post_scraper.py:844-857)."""
        field = self.page.locator('input[name="pass"]')
        if await field.count() > 0:
            return field
        field = self.page.locator('input[type="password"]')
        if await field.count() > 0:
            return field
        field = self.page.get_by_role("textbox", name="Password")
        if await field.count() > 0:
            return field
        return None

    async def _need_to_log_in(self) -> bool:
        """Detect whether a login is required (post_scraper.py:239-261).

        The password-only reauth screen (after clicking 'Continue') has no
        username field, so we only require the password field to be present;
        we also treat the logged-out landing page as needing login.
        """
        password_field = await self._find_password_field()
        if password_field is not None:
            try:
                if await password_field.is_visible():
                    logger.info("Login/reauth password field visible — need to log in")
                    return True
            except Exception:
                pass

        for label in ("Log in", "Log In"):
            button = self.page.get_by_label(label)
            try:
                if await button.count() > 0 and await button.first.is_visible():
                    logger.info("Logged-out landing page detected — need to log in")
                    return True
            except Exception:
                pass
        return False

    async def login(self) -> bool:
        """Replicates InstagramSession.log_in_to_instagram + the post-login
        popup/Home handling (post_scraper.py:264-328, 999-1021), with the
        @sleep_before(10)/@sleep_after(10) timings preserved.
        """
        logger.info(f"Logging in as {self.account.username}")
        await asyncio.sleep(10)
        try:
            if self.mobile:
                await self.page.get_by_role("button", name="Log in").click()
                await asyncio.sleep(5)

            # Username field may be absent on the password-only reauth screen.
            username_field = await self._find_username_field()
            if username_field is not None and await username_field.is_visible():
                await username_field.fill(self.account.username)
                await asyncio.sleep(1)

            password_field = await self._find_password_field()
            if password_field is None:
                raise FailedLoginError("could not find password field on login page")
            await password_field.fill(self.account.password)
            await asyncio.sleep(1)

            login_button = self.page.get_by_role("button", name="Log in")
            if await login_button.count() > 0:
                await login_button.nth(0).click()
            else:
                await self.page.locator('input[type="submit"]').click()

            await asyncio.sleep(10)
            await self._dismiss_popups_and_wait_for_home()

            # Persist cookies and mark account active
            await self._save_cookies()
            await self.pool.set_active(self.account.username, True, None)
            await self.pool.update_last_used(self.account.username)
            return True
        except Exception as e:
            logger.error(f"Login error for {self.account.username}: {e}")
            await self.pool.set_active(
                self.account.username, False, f"Login error: {e}"
            )
            return False

    async def _dismiss_popups_and_wait_for_home(self):
        """Dismiss post-login popups ('Save login info?', notifications) that
        may appear sequentially, then wait for the Home control. The 120s
        timeout leaves room for manual 2FA / challenge screens
        (post_scraper.py:307-327)."""
        # exact=True so post images whose alt text merely contains "home"
        # (e.g. "#coffeeathome") don't match — otherwise a login that redirects
        # to a content page rather than the feed (e.g. reauth from a gated
        # ?next=... URL) makes this locator resolve to many elements.
        home = self.page.get_by_label("Home", exact=True).or_(
            self.page.get_by_role("img", name="Home", exact=True)
        )
        for _ in range(6):
            try:
                if await home.count() > 0 and await home.first.is_visible():
                    break
            except Exception:
                pass
            not_now = self.page.get_by_role("button", name="Not Now")
            try:
                if await not_now.count() > 0 and await not_now.first.is_visible():
                    logger.info("post-login popup detected — clicking 'Not Now'")
                    await not_now.first.click()
                    await asyncio.sleep(5)
                    continue
            except Exception:
                pass
            await asyncio.sleep(5)

        await expect(home.first).to_be_visible(timeout=120000)
        logger.info("home page detected")

    async def _save_cookies(self):
        storage = await self._context.storage_state()
        await self.pool.update_cookies(self.account.username, storage["cookies"])

    # ==================== Helpers ====================

    def _get_proxy_dict(self) -> dict | None:
        if self.account.proxy_server:
            if self.account.proxy_username and self.account.proxy_password:
                return {
                    "server": self.account.proxy_server,
                    "username": self.account.proxy_username,
                    "password": self.account.proxy_password,
                }
            logger.warning("Proxy server set without username/password, skipping proxy")
        return None

    async def _goto(self, url: str, timeout: int = 30000):
        logger.debug(f"goto({url})")
        await self.page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    async def _find_lowest_post(self) -> Locator | None:
        """Return the bottom-most post/reel anchor on the page,
        or None if none visible. Ports post_scraper.py:429-454."""
        anchors = self.page.locator("a")
        count = await anchors.count()
        for i in range(count):
            j = count - 1 - i
            elt = anchors.nth(j)
            try:
                href = await elt.get_attribute("href")
            except Exception:
                continue
            if _is_post_href(href):
                return elt
        return None

    async def _failed_to_load_gate(self, handle: str) -> bool:
        """The 'Failed to Load / Retry' popup (post_scraper.py:173-187)."""
        return await self._failed_to_load_gate_url(f"{BASE_URL}{handle}/")

    async def _failed_to_load_gate_url(self, url: str) -> bool:
        """As _failed_to_load_gate, but gated on an arbitrary target URL."""
        try:
            if await self.page.get_by_role("button", name="Retry").count() > 0:
                if await self.page.get_by_text("Failed to Load").count() > 0:
                    # Only treat as gate if we're actually on the target page —
                    # retry popups from previous pages can linger.
                    return self.page.url == url
        except Exception:
            pass
        return False

    # ==================== Scraping: user_timeline ====================

    async def user_timeline(
        self,
        handle: str,
        start_date: str,
        end_date: str,
        on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None,
        download_videos: bool = False,
        video_dir: str | Path | None = None,
    ) -> ScrapingResult:
        """Scroll a user's profile, collect XHR-intercepted posts, stop at start_date
        or when the same post has been the lowest visible post too many times.

        Scrapes a user's home page; result codes follow the result-code taxonomy.

        Streaming options (fired with each batch of newly-intercepted raw post
        nodes as they arrive during scrolling):
          - on_new_posts: a sync or async callback; overrides the built-in hook.
          - download_videos + video_dir: download every mp4 to video_dir as
            posts stream in (the built-in hook; ignored when on_new_posts set).
        """
        self.endpoint = "UserTimeline"
        self.response_interceptor.flush()

        effective_cb = on_new_posts
        if effective_cb is None and download_videos:
            if video_dir is None:
                raise ValueError("download_videos=True requires video_dir")

            async def _builtin_video_download(batch: list[dict]):
                # Flatten internally — extractors read top-level media fields
                # that live under node['media'] for XDTFeedItem nodes.
                await download_videos_from_posts(post_flattener(batch), video_dir)

            effective_cb = _builtin_video_download

        self.response_interceptor.on_new_posts = effective_cb

        start_time = datetime.now(timezone.utc)
        query = Query(
            endpoint="UserTimeline",
            query={
                "handle": handle,
                "start_date": start_date,
                "end_date": end_date,
            },
            params={},
            start_date=datetime.strptime(start_date, "%Y-%m-%d"),
            end_date=datetime.strptime(end_date, "%Y-%m-%d"),
        )

        def _result(code: str) -> ScrapingResult:
            return ScrapingResult(
                query=query,
                result=code,
                posts=list(self.response_interceptor.post_metadata_list),
                users=list(self.response_interceptor.user_metadata_list),
                time_started=start_time,
                time_taken=datetime.now(timezone.utc) - start_time,
            )

        target_url = f"{BASE_URL}{handle}/"
        await self._goto(target_url)
        await asyncio.sleep(5)

        total_scrolls = 0
        prev_post_url: str | None = None
        repeated_post_count = 0
        num_retries_lowest_post = 0
        internet_bad_count = 0
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")

        while True:
            # Outer-loop rate-limit check (post_scraper.py:482)
            if await self._failed_to_load_gate(handle):
                return _result("failed to load")

            try:
                # Re-navigate if we got pushed off the target page
                if self.page.url != target_url:
                    await self._goto(target_url)
                    await asyncio.sleep(5)
                    if self.page.url != target_url:
                        return _result("logged out while scraping")

                # Inner scroll loop: find the lowest post (with retries)
                while True:
                    if await self.page.get_by_role("button", name="Retry").count() > 0:
                        if (
                            await self.page.get_by_text("account is private").count()
                            > 0
                        ):
                            return _result("account is private")
                        if await self.page.get_by_text("Failed to Load").count() > 0:
                            return _result("failed to load")

                    if (
                        await self.page.get_by_role(
                            "button", name="Reload page"
                        ).count()
                        > 0
                    ):
                        if (
                            await self.page.get_by_text(
                                "Something went wrong"
                            ).count()
                            > 0
                        ):
                            return _result("something went wrong - reload")

                    if (
                        await self.page.get_by_text("Profile isn't available").count()
                        > 0
                    ):
                        return _result("profile is not available")

                    try:
                        lowest_post = await self._find_lowest_post()
                        num_retries_lowest_post = 0
                        break
                    except Exception as e:
                        msg = str(e)
                        logger.warning(f"find_lowest_post error: {msg}")
                        if msg == "Target crashed":
                            return _result("target crashed")
                        num_retries_lowest_post += 1
                        if num_retries_lowest_post > 5:
                            raise
                        await asyncio.sleep(5)

                if lowest_post is None:
                    if (
                        await self.page.get_by_text(
                            "Sorry, this page isn't available"
                        ).count()
                        > 0
                    ):
                        return _result("no posts")
                    if await self.page.get_by_text("No Posts Yet").count() > 0:
                        return _result("no posts")
                    if (
                        await self.page.get_by_text("This account is private").count()
                        > 0
                    ):
                        return _result("no posts")
                    return _result("timeout error")

                try:
                    lowest_post_url = await lowest_post.get_attribute("href")
                except Exception as e:
                    logger.error(f"Failed to read lowest post href: {e}")
                    raise

                try:
                    await lowest_post.scroll_into_view_if_needed()
                except Exception as e:
                    logger.warning(f"scroll_into_view failed: {e}")
                total_scrolls += 1
                await self.pool.update_scroll_count(
                    self.account.username, self.endpoint, 1
                )

                # Date check on lowest intercepted post
                lowest_ts = self._get_lowest_post_datetime_utc()
                if lowest_ts is None:
                    return _result("timeout error")

                logger.info(
                    f"@{handle}: lowest post {lowest_ts}, target start {start_date}, "
                    f"scroll #{total_scrolls}, posts={len(self.response_interceptor.post_metadata_list)}"
                )

                if lowest_ts < start_dt:
                    return _result(
                        "scraped until user-specified starting date was reached"
                    )

                # Repetition detection
                if lowest_post_url == prev_post_url:
                    repeated_post_count += 1
                    if repeated_post_count > 15:
                        if not internet_good():
                            internet_bad_count += 1
                            repeated_post_count = 0
                            if internet_bad_count > 10:
                                return _result("bad internet")
                    if repeated_post_count > 20:
                        return _result("scraped until first ever post was reached")
                else:
                    repeated_post_count = 0

                prev_post_url = lowest_post_url
                await asyncio.sleep(1)

            except PWTimeoutError:
                return _result("timeout error")
            except Exception as e:
                msg = str(e)
                logger.error(f"Unexpected error scraping @{handle}: {e}")
                if msg == "Target crashed":
                    return _result("target crashed")
                raise

    def _get_lowest_post_datetime_utc(self) -> datetime | None:
        posts = self.response_interceptor.post_metadata_list
        if not posts:
            return None
        return get_post_timestamp(posts[-1])

    # ==================== Scraping: user_profile ====================

    async def user_profile(self, handle: str) -> ScrapingResult:
        """Single-shot profile lookup — capture the first `user` XHR response."""
        self.endpoint = "UserProfile"
        self.response_interceptor.flush()
        start_time = datetime.now(timezone.utc)
        query = Query(endpoint="UserProfile", query={"handle": handle}, params={})

        def _result(code: str) -> ScrapingResult:
            return ScrapingResult(
                query=query,
                result=code,
                posts=list(self.response_interceptor.post_metadata_list),
                users=list(self.response_interceptor.user_metadata_list),
                time_started=start_time,
                time_taken=datetime.now(timezone.utc) - start_time,
            )

        target = f"{BASE_URL}{handle}/"
        try:
            await self._goto(target)
        except PWTimeoutError:
            return _result("timeout error")
        await asyncio.sleep(5)

        if self.page.url != target:
            return _result("logged out while scraping")

        if await self.page.get_by_text("Profile isn't available").count() > 0:
            return _result("profile is not available")

        # Wait a bit for user XHR to arrive
        for _ in range(20):
            if self.response_interceptor.user_metadata_list:
                return _result("success")
            await asyncio.sleep(0.5)

        return _result("timeout error")

    # ==================== Scraping: post_by_shortcode ====================

    async def post_by_shortcode(self, shortcode: str) -> ScrapingResult:
        """Navigate to /p/<shortcode>/ and capture the shortcode XHR."""
        self.endpoint = "PostByShortcode"
        self.response_interceptor.flush()
        start_time = datetime.now(timezone.utc)
        query = Query(
            endpoint="PostByShortcode", query={"shortcode": shortcode}, params={}
        )

        def _result(code: str) -> ScrapingResult:
            return ScrapingResult(
                query=query,
                result=code,
                posts=list(self.response_interceptor.post_metadata_list),
                users=list(self.response_interceptor.user_metadata_list),
                time_started=start_time,
                time_taken=datetime.now(timezone.utc) - start_time,
            )

        target = f"{BASE_URL}p/{shortcode}/"
        try:
            await self._goto(target)
        except PWTimeoutError:
            return _result("timeout error")
        await asyncio.sleep(5)

        if (
            await self.page.get_by_text(
                "Sorry, this page isn't available"
            ).count()
            > 0
        ):
            return _result("profile is not available")

        for _ in range(20):
            if self.response_interceptor.post_metadata_list:
                return _result("success")
            await asyncio.sleep(0.5)

        return _result("timeout error")

    # ==================== Scraping: chaining ====================

    async def chaining(self, handle: str) -> ScrapingResult:
        """Visit the profile, trigger 'Suggested for you' chaining XHR.

        Instagram exposes the chaining endpoint when the user clicks the
        caret/expand button next to Follow. We try that interaction and then
        fall back to clicking anything labelled 'See all' / 'Suggested'.
        """
        self.endpoint = "Chaining"
        self.response_interceptor.flush()
        start_time = datetime.now(timezone.utc)
        query = Query(endpoint="Chaining", query={"handle": handle}, params={})

        def _result(code: str) -> ScrapingResult:
            return ScrapingResult(
                query=query,
                result=code,
                posts=list(self.response_interceptor.post_metadata_list),
                users=list(self.response_interceptor.user_metadata_list),
                time_started=start_time,
                time_taken=datetime.now(timezone.utc) - start_time,
            )

        target = f"{BASE_URL}{handle}/"
        try:
            await self._goto(target)
        except PWTimeoutError:
            return _result("timeout error")
        await asyncio.sleep(5)

        if await self.page.get_by_text("Profile isn't available").count() > 0:
            return _result("profile is not available")

        users_before = len(self.response_interceptor.user_metadata_list)

        # Best-effort: click the chevron beside the Follow/Following button
        # to trigger the "Suggested for you" panel.
        for label in ("Similar accounts", "Suggested for you", "See all"):
            try:
                btn = self.page.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3000)
                    await asyncio.sleep(3)
                    break
            except Exception:
                continue

        for _ in range(20):
            if len(self.response_interceptor.user_metadata_list) > users_before:
                return _result("success")
            await asyncio.sleep(0.5)

        # Still return success if the profile at least loaded — chaining
        # XHR does not always fire.
        return _result("success" if self.response_interceptor.user_metadata_list else "timeout error")

    # ==================== Scraping: search ====================

    async def _reauth_if_bounced(self, target_url: str) -> bool:
        """Gated pages (the search SERP) bounce a soft-logged-in session to the
        '/accounts/login/' Continue wall. The reauth chain only runs on the
        BASE_URL landing in initialize(), so re-run it here and re-navigate.

        Returns True if we end up back on `target_url`, False otherwise."""
        if "accounts/login" not in self.page.url:
            return self.page.url == target_url
        logger.info(f"bounced to login wall ({self.page.url}); reauthenticating")
        await self._handle_continue_reauth()
        await asyncio.sleep(5)
        if await self._need_to_log_in():
            ok = await self.login()
            if not ok:
                raise FailedLoginError(f"reauth failed for {self.account.username}")
        await self._goto(target_url)
        await asyncio.sleep(5)
        return self.page.url == target_url

    async def search(
        self,
        keyword: str,
        max_posts: int = -1,
        on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None,
        download_videos: bool = False,
        video_dir: str | Path | None = None,
    ) -> ScrapingResult:
        """Scroll the keyword-search SERP, collecting XHR-intercepted posts.

        Mirrors user_timeline's scroll/repetition machinery but stops on a
        `max_posts` cap (search results aren't reliably chronological, so there
        is no date cutoff). The repeated-lowest-post counter is the only other
        stop condition — the safety net against endless scrolling.

        Streaming options (on_new_posts / download_videos + video_dir) behave
        exactly as in user_timeline.
        """
        self.endpoint = "Search"
        self.response_interceptor.flush()

        effective_cb = on_new_posts
        if effective_cb is None and download_videos:
            if video_dir is None:
                raise ValueError("download_videos=True requires video_dir")

            async def _builtin_video_download(batch: list[dict]):
                await download_videos_from_posts(post_flattener(batch), video_dir)

            effective_cb = _builtin_video_download

        self.response_interceptor.on_new_posts = effective_cb

        start_time = datetime.now(timezone.utc)
        query = Query(
            endpoint="Search",
            query={"keyword": keyword, "max_posts": max_posts},
            params={},
        )

        def _result(code: str) -> ScrapingResult:
            return ScrapingResult(
                query=query,
                result=code,
                posts=list(self.response_interceptor.post_metadata_list),
                users=list(self.response_interceptor.user_metadata_list),
                time_started=start_time,
                time_taken=datetime.now(timezone.utc) - start_time,
            )

        target_url = f"{BASE_URL}explore/search/keyword/?q={quote(keyword)}"
        await self._goto(target_url)
        await asyncio.sleep(5)
        if not await self._reauth_if_bounced(target_url):
            return _result("logged out while scraping")

        prev_post_url: str | None = None
        repeated_post_count = 0
        num_retries_lowest_post = 0

        while True:
            if await self._failed_to_load_gate_url(target_url):
                return _result("failed to load")

            try:
                if self.page.url != target_url and "accounts/login" not in self.page.url:
                    await self._goto(target_url)
                    await asyncio.sleep(5)
                if not await self._reauth_if_bounced(target_url):
                    return _result("logged out while scraping")

                # Post-count cap (primary stop condition)
                if (max_posts > 0) and (len(self.response_interceptor.post_metadata_list) >= max_posts):
                    return _result("success")

                # Inner scroll loop: find the lowest post (with retries)
                while True:
                    if await self.page.get_by_role("button", name="Retry").count() > 0:
                        if await self.page.get_by_text("Failed to Load").count() > 0:
                            return _result("failed to load")
                    if (
                        await self.page.get_by_role(
                            "button", name="Reload page"
                        ).count()
                        > 0
                        and await self.page.get_by_text("Something went wrong").count()
                        > 0
                    ):
                        return _result("something went wrong - reload")

                    try:
                        lowest_post = await self._find_lowest_post()
                        num_retries_lowest_post = 0
                        break
                    except Exception as e:
                        msg = str(e)
                        logger.warning(f"find_lowest_post error: {msg}")
                        if msg == "Target crashed":
                            return _result("target crashed")
                        num_retries_lowest_post += 1
                        if num_retries_lowest_post > 5:
                            raise
                        await asyncio.sleep(5)

                if lowest_post is None:
                    # No post anchors yet: keep scrolling a few times, then give
                    # up. If we already have posts, treat it as the end.
                    if self.response_interceptor.post_metadata_list:
                        return _result("success")
                    await self.page.mouse.wheel(0, 4000)
                    await asyncio.sleep(2)
                    repeated_post_count += 1
                    if repeated_post_count > 10:
                        return _result("no posts")
                    continue

                try:
                    lowest_post_url = await lowest_post.get_attribute("href")
                except Exception as e:
                    logger.error(f"Failed to read lowest post href: {e}")
                    raise

                try:
                    await lowest_post.scroll_into_view_if_needed()
                except Exception as e:
                    logger.warning(f"scroll_into_view failed: {e}")
                await self.pool.update_scroll_count(
                    self.account.username, self.endpoint, 1
                )

                logger.info(
                    f"search '{keyword}': posts="
                    f"{len(self.response_interceptor.post_metadata_list)}/{max_posts if max_posts > 0 else '∞'}, "
                    f"repeated={repeated_post_count}"
                )

                # Repetition detection — the SERP stopped yielding new posts.
                if lowest_post_url == prev_post_url:
                    repeated_post_count += 1
                    if repeated_post_count > 20:
                        return _result("scraped until first ever post was reached")
                else:
                    repeated_post_count = 0

                prev_post_url = lowest_post_url
                await asyncio.sleep(1)

            except PWTimeoutError:
                return _result("timeout error")
            except Exception as e:
                msg = str(e)
                logger.error(f"Unexpected error searching '{keyword}': {e}")
                if msg == "Target crashed":
                    return _result("target crashed")
                raise

