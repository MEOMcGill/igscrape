"""adb + Chrome DevTools Protocol plumbing for driving Chrome on a physical Android phone.

Instagram-agnostic: this module knows how to reach a handset's browser and read traffic
off it, nothing about what is being scraped. `phone_session.py` builds the scraper on top.

The route is:

    wake the phone -> start Chrome -> `adb forward` its DevTools socket -> attach to the
    browser-level websocket -> open our own tab -> capture matching responses off the wire

The transport follows pytok's phone backend, which does the same for TikTok. Reimplemented
here rather than imported because igscrape needs its own response-capture shape (see
`CdpSession`).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
import websockets

from .exceptions import InstagramScraperError
from .logger import logger

CHROME_PKG = "com.android.chrome"
DEFAULT_CDP_PORT = 9222


class PhoneError(InstagramScraperError):
    """adb / CDP transport failure while driving a phone.

    Distinct from the scraping exceptions: it means the *handset* could not be driven,
    not that Instagram refused us.
    """


# ── adb transport (local, or over one SSH hop) ────────────────────────────────


@dataclass
class AsyncAdb:
    """Runs adb, either locally or by prefixing one SSH hop to a remote host."""

    adb: str = "adb"
    ssh_host: str | None = None
    timeout: int = 60

    async def _run(self, cmd: str) -> tuple[int, str, str]:
        if self.ssh_host:
            proc = await asyncio.create_subprocess_exec(
                "ssh", self.ssh_host, cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        else:
            # The platform's own shell: on a Windows host driven locally that is
            # cmd.exe, and every command built here is plain enough to survive both.
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise PhoneError(f"adb command timed out after {self.timeout}s: {cmd}") from None
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def shell(self, serial: str, script: str) -> str:
        """Run `adb -s <serial> shell "<script>"`.

        `script` runs in the phone's own sh, so `;`-chaining works. Wrap URLs in single
        quotes inside it so `&` survives: cmd.exe on a Windows ssh host keeps `&` literal
        because the whole argument is double-quoted.
        """
        _, out, _ = await self._run(f'{self.adb} -s {serial} shell "{script}"')
        return out

    async def raw(self, serial: str, args: str) -> str:
        _, out, _ = await self._run(f"{self.adb} -s {serial} {args}")
        return out

    async def devices(self) -> list[tuple[str, str]]:
        """`adb devices` as (serial, state) pairs, e.g. ("SERIAL0003", "device")."""
        _, out, _ = await self._run(f"{self.adb} devices")
        pairs = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pairs.append((parts[0], parts[1]))
        return pairs


def adb_from_env(adb: str | None = None, ssh_host: str | None = None) -> AsyncAdb:
    """Build an `AsyncAdb` from explicit args, falling back to env then defaults."""
    return AsyncAdb(
        adb=adb or os.getenv("IG_ADB", "adb"),
        ssh_host=ssh_host or os.getenv("IG_PHONE_SSH") or None,
    )


async def list_serials(*, ssh_host: str | None = None, adb: str | None = None) -> list[str]:
    """Serials of the phones that are drivable right now.

    Only handsets in adb state "device" are returned. An "unauthorized" one needs its
    on-screen USB-debugging prompt accepted first, so it stays invisible to a run rather
    than failing one midway.
    """
    pairs = await adb_from_env(adb, ssh_host).devices()
    ready = [s for s, state in pairs if state == "device"]
    if not ready and pairs:
        logger.warning("no drivable phones; adb reports: "
                       + ", ".join(f"{s}={st}" for s, st in pairs))
    return ready


async def wake(adb: AsyncAdb, serial: str) -> None:
    """Wake and unlock the screen, and hold it on for the session."""
    await adb.shell(
        serial, "input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard; svc power stayon true")


async def sleep_phone(adb: AsyncAdb, serial: str) -> None:
    await adb.shell(serial, "svc power stayon false; input keyevent KEYCODE_SLEEP")


async def ensure_chrome(adb: AsyncAdb, serial: str, timeout: float = 45.0) -> None:
    """Start Chrome and wait for its DevTools socket to exist.

    Launching on about:blank rather than straight at a target keeps this step independent
    of what we are about to scrape, so a failure here is unambiguously "the phone would
    not give us a browser".
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    launched = False
    while loop.time() < deadline:
        sockets = await adb.shell(serial, "cat /proc/net/unix | grep chrome_devtools_remote")
        if "chrome_devtools_remote" in sockets:
            return
        if not launched:
            await adb.shell(
                serial,
                f"am start -a android.intent.action.VIEW -d 'about:blank' {CHROME_PKG}")
            launched = True
        await asyncio.sleep(1.0)
    raise PhoneError(
        f"Chrome's DevTools socket never appeared on {serial} after {timeout:.0f}s "
        f"— is Chrome installed, USB debugging authorized, and the screen awake?")


# ── CDP tunnel: adb forward + (optional) SSH -L, kept alive together ──────────


class CdpTunnel:
    """Expose the phone's Chrome DevTools endpoint at http://127.0.0.1:<port>.

    Three non-obvious things this has to get right:

    1. Over SSH the forward is established *inside* the same long-lived process that holds
       the `-L` tunnel open, so one process owns both ends. The forward itself belongs to
       the adb server and outlives that process, so `__aexit__` removes it explicitly.
    2. The tunnel's remote target must be `127.0.0.1`, not `localhost`: the latter can
       resolve to `::1` while adb binds IPv4, and the connection then hangs.
    3. `localabstract:chrome_devtools_remote` only exists *while Chrome runs*, so the
       forward succeeds against a dead Chrome and every connection through it is refused.
       Call `ensure_chrome` before building the tunnel.
    """

    def __init__(self, adb: AsyncAdb, serial: str, port: int = DEFAULT_CDP_PORT):
        self._adb = adb
        self._serial = serial
        self._port = port
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def __aenter__(self) -> CdpTunnel:
        fwd = (f"{self._adb.adb} -s {self._serial} forward tcp:{self._port} "
               f"localabstract:chrome_devtools_remote")
        if self._adb.ssh_host:
            # Windows keepalive, killed when we terminate the ssh process. The forward and
            # the -L tunnel must share one process (gotcha 1 above).
            keepalive = "ping -n 100000 127.0.0.1 >NUL"
            self._proc = await asyncio.create_subprocess_exec(
                "ssh", "-L", f"{self._port}:127.0.0.1:{self._port}",
                self._adb.ssh_host, f"{fwd} && {keepalive}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        else:
            rc, out, err = await self._adb._run(fwd)
            if rc != 0:
                raise PhoneError(f"`adb forward` failed for {self._serial} (rc {rc}): "
                                 f"{(err or out).strip()[:200]}")
        await self._wait_ready()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        # Always remove the forward, including over SSH. It is registered with the *adb
        # server*, a daemon that outlives the client that created it, so killing the ssh
        # session drops the -L tunnel but leaves the forward behind. Left alone they
        # accumulate, and a stale one on a port later reused for a different handset would
        # quietly point at the wrong phone.
        await self._adb.raw(self._serial, f"forward --remove tcp:{self._port}")

    async def _wait_ready(self, timeout: float = 25.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: Any = None
        while loop.time() < deadline:
            try:
                await self.get_json("/json/version")
                return
            except Exception as ex:  # noqa: BLE001 — retry any transport hiccup
                last = ex
                await asyncio.sleep(0.7)
        raise PhoneError(f"CDP endpoint never came up on {self.base}: {last}")

    async def get_json(self, path: str) -> Any:
        """GET a DevTools HTTP endpoint.

        Uses httpx rather than a hand-rolled request because Android Chrome's DevTools
        server keeps the connection open regardless of `Connection: close`, so reading to
        EOF just hangs until the timeout.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(self.base + path)
        resp.raise_for_status()
        return resp.json()


# ── CDP session over the browser-level websocket ──────────────────────────────


class CapturedResponse:
    """One response matched by `url_filter`, with its body fetched eagerly.

    `body` is None until the transfer finishes. `request` carries the outgoing side
    (url / method / post_data / headers) so a replayable template can be built from it.
    """

    __slots__ = ("session_id", "request_id", "url", "status", "method",
                 "post_data", "headers", "body")

    def __init__(self, session_id: str, request_id: str, url: str, method: str,
                 post_data: str | None, headers: dict):
        self.session_id = session_id
        self.request_id = request_id
        self.url = url
        self.status: int | None = None
        self.method = method
        self.post_data = post_data
        self.headers = headers
        self.body: str | None = None


class CdpSession:
    """One websocket to the phone Chrome's browser endpoint, with flat target sessions.

    Attaching at the *browser* level (rather than to a page) is what lets us open our own
    tab, which matters on these handsets: shared phones carry tabs from other work,
    and Chrome on Android freezes backgrounded ones, so reusing an existing tab means
    talking to a renderer that may never answer.

    Response bodies are fetched the moment `Network.loadingFinished` arrives, not on
    demand later. Android Chrome evicts them from the inspector cache within seconds
    ("Request content was evicted from inspector cache"), so a body read at the end of a
    scrape is usually already gone.
    """

    def __init__(self, ws_url: str, url_filter: tuple[str, ...] = ()):
        self._ws_url = ws_url
        self._filter = url_filter
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._grabs: set[asyncio.Task] = set()
        # Matched responses in arrival order, keyed for eager body attachment.
        self.responses: list[CapturedResponse] = []
        self._by_request: dict[str, CapturedResponse] = {}
        # Fired with each CapturedResponse once its body has landed.
        self.on_body: Any = None

    async def __aenter__(self) -> CdpSession:
        # 100 MB: an Instagram profile query came back over 200 KB and search pages run
        # larger; a truncated frame kills the connection.
        self._ws = await websockets.connect(self._ws_url, max_size=100 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        for task in list(self._grabs):
            task.cancel()
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(PhoneError(f"CDP error: {msg['error']}"))
                        else:
                            fut.set_result(msg.get("result", {}))
                    continue
                self._on_event(msg.get("method"), msg.get("params") or {},
                               msg.get("sessionId", ""))
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 — a dead socket must fail every waiter
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PhoneError(f"CDP connection lost: {ex}"))
            self._pending.clear()

    def _on_event(self, method: str | None, params: dict, session_id: str) -> None:
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = request.get("url", "")
            if self._matches(url):
                cap = CapturedResponse(
                    session_id=session_id,
                    request_id=params["requestId"],
                    url=url,
                    method=request.get("method", "GET"),
                    post_data=request.get("postData"),
                    headers=request.get("headers") or {},
                )
                # A redirect reuses the requestId; the latest hop is the one we want.
                self._by_request[cap.request_id] = cap
                self.responses.append(cap)
        elif method == "Network.responseReceived":
            cap = self._by_request.get(params.get("requestId", ""))
            if cap is not None:
                cap.status = (params.get("response") or {}).get("status")
        elif method == "Network.loadingFinished":
            cap = self._by_request.get(params.get("requestId", ""))
            if cap is not None and cap.body is None:
                task = asyncio.create_task(self._grab_body(cap))
                self._grabs.add(task)
                task.add_done_callback(self._grabs.discard)

    def _matches(self, url: str) -> bool:
        return any(needle in url for needle in self._filter)

    async def _grab_body(self, cap: CapturedResponse) -> None:
        """Fetch a response body while Chrome still has it, then notify."""
        try:
            res = await self.call("Network.getResponseBody",
                                  {"requestId": cap.request_id},
                                  session_id=cap.session_id, timeout=30)
            cap.body = res.get("body") or ""
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            # An evicted or refused body is not an error here: an empty body is one of
            # Instagram's ways of declining a request.
            logger.debug(f"response body unavailable for {cap.url[:80]}: {ex}")
            cap.body = ""
        if self.on_body is not None:
            try:
                res = self.on_body(cap)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"on_body hook raised: {ex}")

    async def call(self, method: str, params: dict | None = None,
                   session_id: str | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        payload: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(self._id, None)
            raise PhoneError(f"CDP command {method} timed out after {timeout}s") from None

    async def open_tab(self, url: str = "about:blank") -> tuple[str, str]:
        """Create a tab, attach to it, enable the domains we read, foreground it.

        Returns (target_id, session_id). `Page.bringToFront` is not cosmetic: Android
        Chrome throttles background tabs to the point that navigation never completes.
        `maxPostDataSize` matters just as much — without it `requestWillBeSent` omits the
        form body, which is exactly what a replayable template is built from.
        """
        target_id = (await self.call("Target.createTarget", {"url": url}))["targetId"]
        session_id = (await self.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        for domain in ("Page", "Runtime"):
            await self.call(f"{domain}.enable", {}, session_id=session_id)
        await self.call("Network.enable", {"maxPostDataSize": 2 * 1024 * 1024},
                        session_id=session_id)
        await self.call("Page.bringToFront", {}, session_id=session_id)
        return target_id, session_id

    async def close_tab(self, target_id: str, session_id: str | None = None) -> None:
        """Best effort: a tab we cannot close is untidy, not a failed scrape.

        Drops that tab's captured responses too, so a long run over many targets does not
        accumulate the whole crawl's wire traffic in memory.
        """
        try:
            await self.call("Target.closeTarget", {"targetId": target_id}, timeout=10)
        except Exception as ex:  # noqa: BLE001
            logger.debug(f"could not close phone tab {target_id}: {ex}")
        if session_id:
            self.responses = [r for r in self.responses if r.session_id != session_id]
            self._by_request = {k: v for k, v in self._by_request.items()
                                if v.session_id != session_id}

    async def evaluate(self, expression: str, session_id: str, timeout: float = 30.0) -> Any:
        res = await self.call("Runtime.evaluate",
                              {"expression": expression, "returnByValue": True,
                               "awaitPromise": True},
                              session_id=session_id, timeout=timeout)
        if "exceptionDetails" in res:
            raise PhoneError(f"JS evaluation failed: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    async def evaluate_json(self, expression: str, session_id: str,
                            timeout: float = 30.0) -> dict:
        """Evaluate an expression that returns a JSON string, and parse it.

        One `returnByValue` round trip carries a whole page-state snapshot, which keeps
        per-poll cost down over a USB-attached renderer.
        """
        raw = await self.evaluate(expression, session_id, timeout=timeout)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    async def set_cookies(self, cookies: list[dict]) -> int:
        """Seed cookies into the phone's Chrome, browser-wide.

        Takes Playwright/`storage_state` shaped cookies (what the accounts pool stores)
        and converts them to CDP's `CookieParam`. Playwright marks a session cookie with
        `expires: -1`, which CDP would read as "already expired", so those are sent with
        no expiry at all.
        """
        params = []
        for cookie in cookies or []:
            name, value = cookie.get("name"), cookie.get("value")
            if name is None or value is None:
                continue
            entry = {
                "name": name,
                "value": value,
                "domain": cookie.get("domain") or ".instagram.com",
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure", True)),
                "httpOnly": bool(cookie.get("httpOnly", False)),
            }
            same_site = cookie.get("sameSite")
            if same_site in ("Strict", "Lax", "None"):
                entry["sameSite"] = same_site
            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                entry["expires"] = expires
            params.append(entry)
        if not params:
            return 0
        await self.call("Storage.setCookies", {"cookies": params})
        return len(params)

    async def get_cookies(self) -> list[dict]:
        """Read the phone Chrome's cookies back in `storage_state` shape.

        Used to persist a phone-side login into the accounts pool, so the same session can
        later be replayed onto a handset (or into a desktop browser).
        """
        res = await self.call("Storage.getCookies", {})
        out = []
        for cookie in res.get("cookies") or []:
            entry = {
                "name": cookie.get("name"),
                "value": cookie.get("value"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure", False)),
                "httpOnly": bool(cookie.get("httpOnly", False)),
            }
            expires = cookie.get("expires")
            entry["expires"] = expires if isinstance(expires, (int, float)) and expires > 0 else -1
            same_site = cookie.get("sameSite")
            if same_site in ("Strict", "Lax", "None"):
                entry["sameSite"] = same_site
            out.append(entry)
        return out
