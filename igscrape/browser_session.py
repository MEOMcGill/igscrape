"""Camoufox-backed Playwright session with Instagram login + scrape methods.

Encapsulates the scraping behavior: selectors, timings, result codes, and
termination conditions.
"""

import asyncio
import inspect
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
from .exceptions import FailedLoginError, RateLimitError
from .exporter import append_jsonl
from .logger import logger
from .models import Query, ScrapingResult
from .pagination import (
    DEFAULT_PAGE_COUNT,
    build_replay_body,
    errors_indicate_rate_limit,
    merge_header_tokens,
    parse_response,
    select_cursor_strategy,
)
from .parsers import get_post_timestamp, post_flattener
from .response import InstagramResponseInterceptor
from .stop_conditions import StopState, assemble_default_stop_conditions
from .utils import get_device_os

BASE_URL = "https://www.instagram.com/"

# Replay tuning. FINGERPRINT_EVERY: emit a small organic scroll burst every N
# replays so the session still produces human-like page activity.
REPLAY_TIMEOUT_MS = 30000
FINGERPRINT_EVERY = 50

# Post/reel anchor detection for the grid-render poll (_wait_for_first_post).
# Matches /<user>/p/<code>/ and /<user>/reel/<code>/ (main's f9b8f5b).
_POST_HREF_RE = re.compile(r"^/[A-Za-z0-9_.-]+/(?:p|reel)/[A-Za-z0-9_.-]+/?$")


def _is_post_href(href: str | None) -> bool:
    if not href:
        return False
    if href.startswith("/p/") or href.startswith("/reel/"):
        return True
    return _POST_HREF_RE.match(href) is not None


def _combine_post_hooks(hooks: list) -> Callable | None:
    """Fold several per-batch sinks (JSONL writer, video downloader, caller's
    on_new_posts) into one callback fired for each batch of new posts. Returns
    None if no sinks are enabled. Sync or async sinks both work."""
    hooks = [h for h in hooks if h is not None]
    if not hooks:
        return None
    if len(hooks) == 1:
        return hooks[0]

    async def _multi(batch: list[dict]):
        for hook in hooks:
            res = hook(batch)
            if inspect.isawaitable(res):
                await res

    return _multi


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

    def _build_stream_hook(
        self,
        on_new_posts: Callable | None,
        download_videos: bool,
        video_dir: str | Path | None,
        jsonl_path: str | Path | None,
    ) -> Callable | None:
        """Compose the per-batch streaming sinks shared by user_timeline/search:
        an optional JSONL writer, an optional video downloader, and the caller's
        own callback. Any combination runs."""
        jsonl_cb = None
        if jsonl_path is not None:

            def _jsonl_cb(batch: list[dict], _path=str(jsonl_path)):
                append_jsonl(batch, _path)

            jsonl_cb = _jsonl_cb

        video_cb = None
        if download_videos:
            if video_dir is None:
                raise ValueError("download_videos=True requires video_dir")

            async def _video_cb(batch: list[dict]):
                # Flatten internally — extractors read top-level media fields
                # that live under node['media'] for XDTFeedItem nodes.
                await download_videos_from_posts(post_flattener(batch), video_dir)

            video_cb = _video_cb

        return _combine_post_hooks([jsonl_cb, video_cb, on_new_posts])

    # ==================== Capture-replay primitives ====================

    async def _wait_for_template(
        self, label: str, timeout: float = 30.0, require_cursor: bool = True
    ) -> dict | None:
        """Scroll to provoke, and wait for, the request that yields `label`'s
        replayable template.

        Instagram paginates with a *different* query than the first page (e.g.
        profile: PolarisProfilePostsQuery for page 1 vs
        PolarisProfilePostsTabContentQuery_connection for pagination — only the
        latter carries the `after` cursor + the doc_id we must replay), so by
        default we keep scrolling until a cursor-bearing template is captured.
        Falls back to whatever was captured (the initial-page template) if the
        paginating request never fires before `timeout`."""
        elapsed = 0.0
        while elapsed < timeout:
            template = self.response_interceptor.templates.get(label)
            if template is not None and (template.get("_has_cursor") or not require_cursor):
                return template
            await self._provoke_scroll()
            await asyncio.sleep(1.0)
            elapsed += 1.0
        return self.response_interceptor.templates.get(label)

    async def _provoke_scroll(self):
        """Nudge the page so its infinite-scroll fires the next-page request.

        A fixed `mouse.wheel` delta doesn't reliably reach a profile's posts
        grid, so scroll to the current document bottom via JS (as more loads,
        scrollHeight grows and the next call goes further) and add a wheel nudge
        to trip wheel-based observers. Works for both the profile grid and the
        search SERP."""
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        try:
            await self.page.mouse.wheel(0, 3000)
        except Exception:
            pass

    async def _fingerprint_scroll_burst(self, n_min: int = 2, n_max: int = 5):
        """A short burst of real scrolls to keep the session looking human
        while the bulk of collection happens via direct replay."""
        try:
            for _ in range(random.randint(n_min, n_max)):
                await self.page.mouse.wheel(0, random.randint(2000, 5000))
                await asyncio.sleep(random.uniform(0.3, 1.0))
        except Exception as e:
            logger.debug(f"fingerprint scroll failed: {e}")

    async def _send_replay(
        self, template: dict, body: str, headers: dict, timeout_ms: int = REPLAY_TIMEOUT_MS
    ) -> tuple[str | None, str | None]:
        """POST a replay request via the page's request context (shares cookies,
        bypasses the page response listener so it never self-pollutes).

        Returns (text, error_str). Raises RateLimitError / FailedLoginError on
        throttle / auth responses so the worker can lock + rotate the account.
        """
        for attempt in range(3):
            try:
                resp = await self.page.request.post(
                    template["url"], headers=headers, data=body, timeout=timeout_ms
                )
            except Exception as e:
                return None, f"request error: {e}"

            status = resp.status
            if status == 200:
                text = await resp.text()
                if text.lstrip().startswith("<"):
                    # An HTML body on a GraphQL endpoint means we were bounced
                    # to a login / checkpoint wall.
                    raise FailedLoginError("replay returned HTML (login bounce)")
                return text, None
            if status in (401, 403):
                raise FailedLoginError(f"replay status {status}")
            if status == 429:
                raise RateLimitError("replay status 429")
            if 500 <= status < 600:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None, f"replay status {status}"
        return None, "replay failed after retries (5xx)"

    async def _replay_pagination_loop(
        self,
        *,
        label: str,
        template: dict,
        stop_conditions: list,
        strategy,
        start_unix: int | None,
        on_new_posts: Callable | None,
        params: dict,
    ) -> str:
        """The shared collection engine. Replays the captured request with an
        advancing cursor until a stop condition fires. Endpoint-specific
        behavior is supplied entirely via `stop_conditions` + `strategy`."""
        interceptor = self.response_interceptor
        cursor = strategy.initial_cursor(template)
        count = params.get("page_count", DEFAULT_PAGE_COUNT)
        iter_index = 0
        no_progress_streak = 0

        while True:
            body = build_replay_body(
                template, cursor, count, strategy,
                latest_form=interceptor.latest_request_form,
            )
            headers = merge_header_tokens(
                template["headers"], interceptor.latest_request_headers
            )
            text, err = await self._send_replay(template, body, headers)

            payloads: list[dict] = []
            errors: list[dict] = []
            if text is not None:
                payloads, errors = parse_response(text)
            if errors and errors_indicate_rate_limit(errors):
                raise RateLimitError("; ".join(str(e.get("message")) for e in errors))

            error_str = err
            if errors and not error_str:
                error_str = "; ".join(str(e.get("message") or e) for e in errors)

            new_posts = interceptor.ingest_payloads(payloads)
            end_cursor, has_next = strategy.extract(payloads)

            ts_list: list[int] = []
            for post in new_posts:
                dt = get_post_timestamp(post)
                if dt is not None:
                    ts_list.append(int(dt.timestamp()))

            no_progress_streak = no_progress_streak + 1 if not new_posts else 0

            if new_posts and on_new_posts is not None:
                try:
                    res = on_new_posts(new_posts)
                    if inspect.isawaitable(res):
                        await res
                except Exception as e:
                    logger.warning(f"on_new_posts hook raised: {e}")

            state = StopState(
                iter_index=iter_index,
                cursor_sent=cursor,
                end_cursor=end_cursor,
                has_next_page=has_next,
                new_count=len(new_posts),
                all_count=len(interceptor.post_metadata_list),
                oldest_in_batch_unix=min(ts_list) if ts_list else None,
                timestamped_count=len(ts_list),
                error=error_str,
                start_unix=start_unix,
                no_progress_streak=no_progress_streak,
            )

            logger.info(
                f"{label}: replay #{iter_index} +{len(new_posts)} "
                f"(total {state.all_count}), next={'yes' if has_next else 'no'}"
            )

            for cond in stop_conditions:
                code = cond.evaluate(state)
                if code is not None:
                    return code

            cursor = end_cursor
            iter_index += 1
            await self.pool.update_scroll_count(self.account.username, self.endpoint, 1)
            if iter_index % FINGERPRINT_EVERY == 0:
                await self._fingerprint_scroll_burst()
            await asyncio.sleep(random.uniform(0.5, 1.5))

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

    async def _wait_for_first_post(self, timeout: float = 25.0, poll: float = 1.0) -> None:
        """Wait for the profile's post grid to render before snapshotting it.

        Instagram populates the grid asynchronously after ``domcontentloaded``,
        so a fixed short sleep followed by a single ``_find_lowest_post()``
        snapshot spuriously returns "timeout error" whenever the grid is slow to
        load (common over a VPN or on heavy profiles). Poll until a post anchor
        appears, a terminal empty/error state is shown, or ``timeout`` elapses —
        then let the caller's normal logic take its snapshot.
        """
        empty_state_texts = (
            "Profile isn't available",
            "Sorry, this page isn't available",
            "No Posts Yet",
            "This account is private",
        )
        for _ in range(max(1, int(timeout / poll))):
            for text in empty_state_texts:
                if await self.page.get_by_text(text).count() > 0:
                    return
            try:
                if await self._find_lowest_post() is not None:
                    return
            except Exception:
                pass
            await asyncio.sleep(poll)
        logger.warning(f"no post anchor rendered within {timeout:.0f}s")

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

    def _intercepted_is_private(self, handle: str) -> bool:
        """Whether the intercepted profile response marks this handle private.

        The profile GraphQL response (data['user']) carries the authoritative
        is_private flag. Only consulted in the no-posts-visible / load-failure
        branches below, so a private account we *follow* (which returns posts
        normally and never reaches those branches) isn't affected — no need to
        also check viewer-follow state.
        """
        for user in self.response_interceptor.user_metadata_list:
            if user.get("username", "").lower() == handle.lower() and user.get(
                "is_private"
            ):
                return True
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
        jsonl_path: str | Path | None = None,
    ) -> ScrapingResult:
        """Collect a user's timeline via capture-once-then-replay.

        Navigates to the profile, captures the feed GraphQL request once, then
        replays it with an advancing cursor (no continuous scrolling). Stops on
        the start date, end-of-feed, or a safety cap (see stop_conditions).
        Result codes follow the result-code taxonomy.

        Streaming options (fired with each batch of newly-collected raw post
        nodes as each replayed page arrives; they compose — any combination of
        the three runs):
          - jsonl_path: append each post as one JSON line to this file as pages
            arrive (opt-in; the default ScrapingResult.save() still applies too).
          - download_videos + video_dir: download every mp4 to video_dir.
          - on_new_posts: a sync or async callback of your own.
        """
        self.endpoint = "UserTimeline"
        self.response_interceptor.flush()
        effective_cb = self._build_stream_hook(
            on_new_posts, download_videos, video_dir, jsonl_path
        )
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

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        start_unix = int(start_dt.timestamp())

        target_url = f"{BASE_URL}{handle}/"
        try:
            await self._goto(target_url)
        except PWTimeoutError:
            return _result("timeout error")
        # Poll for the profile grid to render rather than a fixed wait (main's
        # f9b8f5b); the replay path still needs the first page loaded to harvest
        # the feed template.
        await self._wait_for_first_post()

        # Availability / access gates before we commit to replaying.
        if await self.page.get_by_text("Profile isn't available").count() > 0:
            return _result("profile is not available")
        if await self.page.get_by_text("Sorry, this page isn't available").count() > 0:
            return _result("no posts")
        if (
            await self.page.get_by_text("This account is private").count() > 0
            or await self.page.get_by_text("account is private").count() > 0
        ):
            return _result("account is private")
        if await self._failed_to_load_gate(handle):
            # A private account we don't follow can render a Retry / "Failed to
            # Load" tile (and modern IG omits the "This account is private"
            # text), so classify it authoritatively before treating the gate as
            # a rate-limit (restores beea38b — regressed by the reset).
            if self._intercepted_is_private(handle):
                return _result("account is private")
            return _result("failed to load")

        # Capture the feed request template (a bootstrap scroll provokes it),
        # then collect by replaying it directly — no further scrolling.
        template = await self._wait_for_template("user_timeline", timeout=30.0)
        if template is None:
            # A private account we don't follow renders no grid and fires no
            # feed request, so template capture times out. Classify it
            # authoritatively (main's beea38b) before reporting a generic error.
            if self._intercepted_is_private(handle):
                return _result("account is private")
            if await self.page.get_by_text("No Posts Yet").count() > 0:
                return _result("no posts")
            return _result("timeout error")

        params = {
            "max_posts": -1,
            "max_paginations": 5000,
            "max_no_progress_streak": 5,
        }
        strategy = select_cursor_strategy(template)
        conditions = assemble_default_stop_conditions("UserTimeline", params)

        try:
            code = await self._replay_pagination_loop(
                label=f"@{handle}",
                template=template,
                stop_conditions=conditions,
                strategy=strategy,
                start_unix=start_unix,
                on_new_posts=effective_cb,
                params=params,
            )
        except PWTimeoutError:
            return _result("timeout error")
        # RateLimitError / FailedLoginError propagate to the worker, which locks
        # + rotates the account; no need to translate them to a result code here.
        return _result(code)

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
        jsonl_path: str | Path | None = None,
    ) -> ScrapingResult:
        """Collect the keyword-search SERP via capture-once-then-replay.

        Captures the SERP GraphQL request once, then replays it with an
        advancing cursor. Search results aren't reliably chronological, so there
        is no date cutoff — collection stops on `max_posts`, end-of-feed, or a
        no-progress streak (see stop_conditions).

        Streaming options (jsonl_path / on_new_posts / download_videos +
        video_dir) behave exactly as in user_timeline.
        """
        self.endpoint = "Search"
        self.response_interceptor.flush()
        effective_cb = self._build_stream_hook(
            on_new_posts, download_videos, video_dir, jsonl_path
        )
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
        try:
            await self._goto(target_url)
        except PWTimeoutError:
            return _result("timeout error")
        await asyncio.sleep(5)
        if not await self._reauth_if_bounced(target_url):
            return _result("logged out while scraping")

        # Capture the SERP request template, then replay it directly.
        template = await self._wait_for_template("search", timeout=30.0)
        if template is None:
            return _result("no posts")

        params = {
            "max_posts": max_posts,
            "max_paginations": 2000,
            "max_no_progress_streak": 5,
        }
        strategy = select_cursor_strategy(template)
        conditions = assemble_default_stop_conditions("Search", params)

        try:
            code = await self._replay_pagination_loop(
                label=f"search:{keyword}",
                template=template,
                stop_conditions=conditions,
                strategy=strategy,
                start_unix=None,
                on_new_posts=effective_cb,
                params=params,
            )
        except PWTimeoutError:
            return _result("timeout error")
        return _result(code)

