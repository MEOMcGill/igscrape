"""Instagram mobile web on a physical Android phone, built on `phone_transport`.

The phone is logged in by seeding the pool account's cookies into its Chrome;
there is no automated login here. Responses are captured off the CDP wire and
fed into the same `InstagramResponseInterceptor` the desktop session uses, so
results come out in the same shape.

    async with PhoneSession("SERIAL0001", account, pool, ssh_host="phone-host") as phone:
        result = await phone.home_feed(max_posts=100)
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from .account import Account
from .accounts_pool import AccountsPool
from .browser_session import BASE_URL, BrowserSession
from .home_feed import (
    EMBEDDED_FEED_JS,
    HOME_FEED_MAX_SCROLLS,
    HOME_FEED_NO_PROGRESS_STREAK,
    parse_embedded_feed_result,
    scroll_feed,
)
from .logger import logger
from .models import Query, ScrapingResult
from .phone_transport import (
    DEFAULT_CDP_PORT,
    CapturedResponse,
    CdpSession,
    CdpTunnel,
    PhoneError,
    adb_from_env,
    ensure_chrome,
    sleep_phone,
    wake,
)
from .response import InstagramResponseInterceptor

# Mobile web posts feed queries to /graphql/query as well as /api/graphql.
GRAPHQL_URL_FILTER = ("instagram.com/api/graphql", "instagram.com/graphql/query")

# sessionid is httpOnly, so page JS can only see ds_user_id.
_LOGIN_STATE_JS = """JSON.stringify({
  cookie: document.cookie.includes('ds_user_id='),
  password: !!document.querySelector('input[type="password"], input[name="pass"]'),
  w: window.innerWidth, h: window.innerHeight,
})"""

# Centre of the first visible button whose text is one of the given labels.
_FIND_BUTTON_JS = """(labels => {
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    const t = (el.innerText || '').trim().toLowerCase();
    if (!labels.includes(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width && r.height) return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
  }
  return '';
})(%s)"""

_DISMISS_LABELS = ["not now", "cancel"]


def parse_graphql_body(body: str | None) -> dict | None:
    """The `data` dict of a captured GraphQL body, or None."""
    if not body:
        return None
    text = body.lstrip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def instagram_cookies(cookies: list[dict]) -> list[dict]:
    """The Instagram cookies out of a whole-browser cookie jar."""
    return [c for c in cookies if (c.get("domain") or "").endswith("instagram.com")]


class PhoneSession:
    """One handset's Chrome, logged in as one pool account.

    The caller claims `account` from the pool and releases it afterwards, as
    with BrowserSession. Give concurrent handsets different `cdp_port`s.
    """

    def __init__(
        self,
        serial: str,
        account: Account,
        pool: AccountsPool,
        *,
        ssh_host: str | None = None,
        adb: str | None = None,
        cdp_port: int = DEFAULT_CDP_PORT,
        sleep_on_exit: bool = False,
    ):
        self.serial = serial
        self.account = account
        self.pool = pool
        self.cdp_port = cdp_port
        self.sleep_on_exit = sleep_on_exit
        self._adb = adb_from_env(adb, ssh_host)
        self._tunnel: CdpTunnel | None = None
        self._cdp: CdpSession | None = None
        self._target_id: str | None = None
        self._session_id: str | None = None
        self._viewport = (400, 800)
        self.response_interceptor = InstagramResponseInterceptor()

    async def __aenter__(self) -> PhoneSession:
        await wake(self._adb, self.serial)
        await ensure_chrome(self._adb, self.serial)
        self._tunnel = CdpTunnel(self._adb, self.serial, self.cdp_port)
        await self._tunnel.__aenter__()
        # __aexit__ does not run when __aenter__ raises, and the adb forward
        # would outlive us, so undo by hand.
        try:
            version = await self._tunnel.get_json("/json/version")
            ws_url = version["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
            logger.info(f"[phone {self.serial}] driving {version.get('Browser')}")
            self._cdp = CdpSession(ws_url, url_filter=GRAPHQL_URL_FILTER)
            await self._cdp.__aenter__()
            self._cdp.on_body = self._on_body
            n = await self._cdp.set_cookies(self.account.cookies)
            logger.info(f"[phone {self.serial}] seeded {n} cookies for {self.account.username}")
        except Exception:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._cdp is not None:
            if self._target_id:
                await self._cdp.close_tab(self._target_id, self._session_id)
            await self._cdp.__aexit__(*exc)
            self._cdp = None
        if self._tunnel is not None:
            await self._tunnel.__aexit__(*exc)
            self._tunnel = None
        if self.sleep_on_exit:
            await sleep_phone(self._adb, self.serial)

    # ---- CDP helpers ----

    async def _on_body(self, cap: CapturedResponse) -> None:
        if cap.session_id != self._session_id:
            return
        data = parse_graphql_body(cap.body)
        if data is None:
            return
        self.response_interceptor.graphql_request_count += 1
        await self.response_interceptor.ingest_and_notify([data])

    async def _evaluate_json(self, expression: str) -> dict:
        return await self._cdp.evaluate_json(expression, self._session_id)

    async def _tap(self, x: float, y: float) -> None:
        point = [{"x": x, "y": y}]
        for kind, points in (("touchStart", point), ("touchEnd", [])):
            await self._cdp.call(
                "Input.dispatchTouchEvent",
                {"type": kind, "touchPoints": points},
                session_id=self._session_id,
            )
            await asyncio.sleep(random.uniform(0.05, 0.15))

    async def _dismiss_popups(self) -> None:
        """Tap away 'Not now' sheets (notifications, open-in-app) if present."""
        for _ in range(3):
            pos = await self._evaluate_json(_FIND_BUTTON_JS % json.dumps(_DISMISS_LABELS))
            if not pos:
                return
            logger.info(f"[phone {self.serial}] dismissing a popup")
            await self._tap(pos["x"], pos["y"])
            await asyncio.sleep(1.5)

    async def _scroll_feed_once(self) -> None:
        """One touch swipe up, most of a screen long."""
        w, h = self._viewport
        await self._cdp.call(
            "Input.synthesizeScrollGesture",
            {
                "x": int(w * random.uniform(0.35, 0.65)),
                "y": int(h * random.uniform(0.65, 0.8)),
                "yDistance": -int(h * random.uniform(0.55, 0.85)),
                "speed": random.randint(700, 1400),
                "gestureSourceType": "touch",
            },
            session_id=self._session_id,
            timeout=30,
        )

    async def _open_home(self, timeout: float = 45.0) -> bool:
        """Open a tab on the home feed. True once it reads as logged in."""
        if self._target_id is None:
            self._target_id, self._session_id = await self._cdp.open_tab()
        await self._cdp.call(
            "Page.navigate", {"url": BASE_URL}, session_id=self._session_id, timeout=90
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(2.0)
            state = await self._evaluate_json(_LOGIN_STATE_JS)
            if state.get("w") and state.get("h"):
                self._viewport = (state["w"], state["h"])
            if state.get("password"):
                return False
            if state.get("cookie"):
                return True
        return False

    async def save_cookies(self) -> int:
        """Write the phone's current Instagram cookies back to the pool."""
        cookies = instagram_cookies(await self._cdp.get_cookies())
        if cookies:
            await self.pool.update_cookies(self.account.username, cookies)
        return len(cookies)

    # ---- endpoints ----

    async def home_feed(
        self,
        max_posts: int = -1,
        max_scrolls: int = HOME_FEED_MAX_SCROLLS,
        max_no_progress_streak: int = HOME_FEED_NO_PROGRESS_STREAK,
        on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None,
        download_videos: bool = False,
        video_dir: str | Path | None = None,
        jsonl_path: str | Path | None = None,
    ) -> ScrapingResult:
        """Scroll the account's mobile home feed; see BrowserSession.home_feed."""
        if self._cdp is None:
            raise PhoneError("PhoneSession used outside its `async with` block")
        interceptor = self.response_interceptor
        interceptor.flush("HomeFeed")
        interceptor.on_new_posts = BrowserSession._build_stream_hook(
            on_new_posts, download_videos, video_dir, jsonl_path
        )

        start_time = datetime.now(timezone.utc)
        query = Query(
            endpoint="HomeFeed",
            query={"account": self.account.username, "max_posts": max_posts,
                   "device": self.serial},
            params={},
        )

        def _result(code: str) -> ScrapingResult:
            return ScrapingResult(
                query=query,
                result=code,
                posts=list(interceptor.post_metadata_list),
                users=list(interceptor.user_metadata_list),
                time_started=start_time,
                time_taken=datetime.now(timezone.utc) - start_time,
            )

        if not await self._open_home():
            return _result("logged out while scraping")
        await self._dismiss_popups()

        raw = await self._cdp.evaluate(EMBEDDED_FEED_JS, self._session_id)
        await interceptor.ingest_and_notify(parse_embedded_feed_result(raw))

        scrolls = 0

        async def _between_scrolls():
            nonlocal scrolls
            scrolls += 1
            await self.pool.update_scroll_count(self.account.username, "HomeFeed", 1)
            if scrolls % 25 == 0:
                await self.pool.renew_lease(self.account.username)
            # Sheets like "Save your login info?" can open at any point and
            # swallow every swipe until closed.
            await self._dismiss_popups()

        code = await scroll_feed(
            scroll_once=self._scroll_feed_once,
            interceptor=interceptor,
            max_posts=max_posts,
            max_scrolls=max_scrolls,
            max_no_progress_streak=max_no_progress_streak,
            label=f"home_feed:{self.account.username}@{self.serial}",
            on_scroll=_between_scrolls,
        )
        await interceptor.drain_hooks()
        await self.save_cookies()
        if not interceptor.post_metadata_list:
            return _result("no posts")
        return _result(code)
