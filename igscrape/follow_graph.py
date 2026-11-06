"""Read who the logged-in account follows, and follow accounts as it.

Transport-agnostic: BrowserSession and PhoneSession each wrap their page in a
`PageDriver`, and the logic lives here once.

The following list is read with an in-page `fetch` of the web API (outside the
page the same URL answers 200 with an HTML shell). Following someone is a real
click or tap on the profile's Follow button, checked by reloading the profile.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Protocol

from .exceptions import FailedLoginError, RateLimitError
from .logger import logger

BASE_URL = "https://www.instagram.com/"

# Fallbacks for when the page's own app id can't be found in its inline scripts.
DESKTOP_APP_ID = "936619743392459"
MOBILE_WEB_APP_ID = "1217981644879628"

# The web client's own page size for the following dialog.
FOLLOWING_PAGE_SIZE = 12


class PageDriver(Protocol):
    async def goto(self, url: str) -> None: ...

    async def evaluate_json(self, expression: str) -> dict: ...

    async def click(self, x: float, y: float) -> None: ...


_VIEWER_JS = """(() => {
  const cookie = name => {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  };
  let appId = '';
  for (const s of document.querySelectorAll('script')) {
    const m = (s.textContent || '').match(/"(?:X-IG-App-ID|APP_ID|appId|app_id)":"(\\d{6,})"/);
    if (m) { appId = m[1]; break; }
  }
  return JSON.stringify({user_id: cookie('ds_user_id'), app_id: appId});
})()"""

_FOLLOWING_PAGE_JS = """(async (userId, maxId, count, appId) => {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  let url = `/api/v1/friendships/${userId}/following/?count=${count}`;
  if (maxId) url += `&max_id=${encodeURIComponent(maxId)}`;
  const headers = {
    'X-IG-App-ID': appId,
    'X-CSRFToken': m ? m[1] : '',
    'X-Requested-With': 'XMLHttpRequest',
  };
  const claim = sessionStorage.getItem('www-claim-v2');
  if (claim) headers['X-IG-WWW-Claim'] = claim;
  try {
    const r = await fetch(url, {credentials: 'include', headers});
    return JSON.stringify({status: r.status, body: await r.text()});
  } catch (e) {
    return JSON.stringify({status: 0, body: String(e)});
  }
})(%s, %s, %d, %s)"""

# The profile header's follow button and its state. Suggested-account carousels
# further down also carry Follow buttons, hence looking in the header first.
_PROFILE_STATE_JS = """(() => {
  const states = {'follow': 'follow', 'follow back': 'follow',
                  'following': 'following', 'requested': 'requested'};
  const text = (document.body && document.body.innerText) || '';
  const blocked = /try again later|we restrict certain activity/i.test(text);
  const unavailable = /sorry, this page isn.t available|profile isn.t available/i.test(text);
  const scopes = [document.querySelector('main header'), document.querySelector('header'),
                  document.querySelector('main')].filter(Boolean);
  for (const scope of scopes) {
    for (const el of scope.querySelectorAll('button, [role="button"]')) {
      const state = states[(el.innerText || '').trim().toLowerCase()];
      if (!state) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      return JSON.stringify({state, x: r.x + r.width / 2, y: r.y + r.height / 2,
                             blocked, unavailable});
    }
  }
  return JSON.stringify({state: null, blocked, unavailable});
})()"""


async def viewer(driver: PageDriver, fallback_app_id: str) -> tuple[str, str]:
    """(user id, app id) of the logged-in account, read from the open page."""
    info = await driver.evaluate_json(_VIEWER_JS)
    user_id = info.get("user_id") or ""
    if not user_id:
        raise FailedLoginError("no ds_user_id cookie; the page is not logged in")
    app_id = info.get("app_id") or fallback_app_id
    if not info.get("app_id"):
        logger.debug(f"app id not found in page; using {fallback_app_id}")
    return user_id, app_id


def parse_following_page(status: int, body: str) -> tuple[list[dict], str | None]:
    """(users, next_max_id) from one following-page response.

    Raises on throttling or a logged-out answer rather than returning nothing,
    so an empty list always means the account really follows no more.
    """
    if status == 429:
        raise RateLimitError("following list: HTTP 429")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(
            f"following list: HTTP {status} with a non-JSON body: {str(body)[:200]!r}"
        ) from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"following list: unexpected payload {str(payload)[:200]!r}")
    message = str(payload.get("message") or "")
    if payload.get("require_login") or message == "login_required" or status in (401, 403):
        raise FailedLoginError(f"following list: logged out ({status} {message})")
    if "wait a few minutes" in message.lower() or payload.get("spam"):
        raise RateLimitError(f"following list: {message}")
    if payload.get("status") != "ok":
        raise RuntimeError(f"following list: HTTP {status} {message or payload}")
    users = [u for u in payload.get("users") or [] if isinstance(u, dict)]
    next_max_id = payload.get("next_max_id")
    return users, str(next_max_id) if next_max_id else None


async def list_following(
    driver: PageDriver,
    *,
    fallback_app_id: str,
    max_users: int = -1,
    page_size: int = FOLLOWING_PAGE_SIZE,
    pause: tuple[float, float] = (1.0, 3.0),
) -> list[dict]:
    """Every account the logged-in account follows, as Instagram's user records.

    The driver's page must already be on instagram.com and logged in.
    `max_users` < 0 means no cap.
    """
    user_id, app_id = await viewer(driver, fallback_app_id)
    users: list[dict] = []
    seen: set[str] = set()
    max_id: str | None = None
    while True:
        raw = await driver.evaluate_json(
            _FOLLOWING_PAGE_JS
            % (json.dumps(user_id), json.dumps(max_id), page_size, json.dumps(app_id))
        )
        page, max_id = parse_following_page(int(raw.get("status") or 0), raw.get("body"))
        for user in page:
            pk = str(user.get("pk") or user.get("id") or "")
            if pk and pk not in seen:
                seen.add(pk)
                users.append(user)
        logger.info(f"following of {user_id}: +{len(page)} (total {len(users)})")
        if 0 <= max_users <= len(users):
            return users[:max_users]
        if not max_id or not page:
            return users
        await asyncio.sleep(random.uniform(*pause))


async def _profile_state(
    driver: PageDriver, timeout: float, until=lambda s: s.get("state")
) -> dict:
    """Poll the profile's follow-button state until `until(state)` or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    state: dict = {}
    while loop.time() < deadline:
        state = await driver.evaluate_json(_PROFILE_STATE_JS)
        if until(state) or state.get("blocked") or state.get("unavailable"):
            return state
        await asyncio.sleep(1.0)
    return state


async def follow(driver: PageDriver, handle: str, load_timeout: float = 25.0) -> str:
    """Follow `handle` by clicking its profile's Follow button.

    Returns one of: 'followed', 'requested' (a private account), 'already
    following', 'already requested', 'profile is not available', 'no follow
    button' (e.g. your own profile), 'action blocked', 'follow did not stick'.
    """
    handle = handle.lstrip("@").strip()
    url = f"{BASE_URL}{handle}/"
    await driver.goto(url)
    state = await _profile_state(driver, load_timeout)
    if state.get("unavailable"):
        return "profile is not available"
    if state.get("blocked"):
        return "action blocked"
    current = state.get("state")
    if current == "following":
        return "already following"
    if current == "requested":
        return "already requested"
    if current != "follow":
        return "no follow button"

    await asyncio.sleep(random.uniform(0.8, 2.5))
    await driver.click(state["x"], state["y"])
    after = await _profile_state(
        driver, 10.0, until=lambda s: s.get("state") in ("following", "requested")
    )
    if after.get("blocked"):
        return "action blocked"

    # The button flips optimistically, and a blocked follow flips back later,
    # so trust only what a fresh load of the profile shows.
    await asyncio.sleep(random.uniform(3.0, 6.0))
    await driver.goto(url)
    final = await _profile_state(driver, load_timeout)
    if final.get("blocked"):
        return "action blocked"
    if final.get("state") == "following":
        return "followed"
    if final.get("state") == "requested":
        return "requested"
    logger.warning(f"follow @{handle}: button reads {final.get('state')!r} after reload")
    return "follow did not stick"
