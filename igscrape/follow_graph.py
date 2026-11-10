"""Read who the logged-in account follows, and follow or unfollow accounts as it.

Transport-agnostic: BrowserSession and PhoneSession each pass in their own
`goto`, `evaluate_json` and `click`, and the logic lives here once.

The following list is read with an in-page `fetch` of the web API (outside the
page the same URL answers 200 with an HTML shell). Following someone is a real
click or tap on the profile's Follow button, checked by reloading the profile;
unfollowing is the same, through the menu the Following button opens.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .exceptions import FailedLoginError, RateLimitError
from .logger import logger
from .models import Query, ScrapingResult

BASE_URL = "https://www.instagram.com/"

# Fallbacks for when the page's own app id can't be found in its inline scripts.
DESKTOP_APP_ID = "936619743392459"
MOBILE_WEB_APP_ID = "1217981644879628"

# The web client's own page size for the following dialog.
FOLLOWING_PAGE_SIZE = 12


# Evaluates a JS expression that returns a JSON string, and parses it.
EvaluateJson = Callable[[str], Awaitable[dict]]


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


async def viewer(evaluate_json: EvaluateJson, fallback_app_id: str) -> tuple[str, str]:
    """(user id, app id) of the logged-in account, read from the open page."""
    info = await evaluate_json(_VIEWER_JS)
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
    evaluate_json: EvaluateJson,
    *,
    fallback_app_id: str,
    max_users: int = -1,
    page_size: int = FOLLOWING_PAGE_SIZE,
    pause: tuple[float, float] = (1.0, 3.0),
) -> list[dict]:
    """Every account the logged-in account follows, as Instagram's user records.

    The page must already be on instagram.com and logged in.
    `max_users` < 0 means no cap.
    """
    user_id, app_id = await viewer(evaluate_json, fallback_app_id)
    users: list[dict] = []
    seen: set[str] = set()
    max_id: str | None = None
    while True:
        raw = await evaluate_json(
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


# The Unfollow item in the menu (or, for a pending request, the confirmation)
# that the header's Following / Requested button opens.
_UNFOLLOW_ITEM_JS = """(() => {
  const scopes = [...document.querySelectorAll('[role="dialog"]'), document];
  for (const scope of scopes) {
    for (const el of scope.querySelectorAll('button, [role="button"]')) {
      if ((el.innerText || '').trim().toLowerCase() !== 'unfollow') continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
    }
  }
  return JSON.stringify({});
})()"""


async def _poll(evaluate_json: EvaluateJson, expression: str, timeout: float, until) -> dict:
    """Evaluate `expression` once a second until `until(result)` or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    result: dict = {}
    while loop.time() < deadline:
        result = await evaluate_json(expression)
        if until(result):
            return result
        await asyncio.sleep(1.0)
    return result


async def _profile_state(
    evaluate_json: EvaluateJson, timeout: float, until=lambda s: s.get("state")
) -> dict:
    """Poll the profile's follow-button state until `until(state)` or timeout."""
    return await _poll(
        evaluate_json, _PROFILE_STATE_JS, timeout,
        lambda s: until(s) or s.get("blocked") or s.get("unavailable"),
    )


async def _open_profile(
    handle: str, goto: Callable[[str], Awaitable[None]], evaluate_json: EvaluateJson,
    load_timeout: float, action: str,
) -> tuple[str, dict]:
    """Load `handle`'s profile and return (url, follow-button state)."""
    url = f"{BASE_URL}{handle}/"
    await goto(url)
    state = await _profile_state(evaluate_json, load_timeout)
    if state.get("blocked"):
        raise RateLimitError(f"{action} @{handle}: action blocked")
    return url, state


async def _state_after_reload(
    url: str, handle: str, goto: Callable[[str], Awaitable[None]],
    evaluate_json: EvaluateJson, load_timeout: float, action: str,
) -> str | None:
    """The follow-button state on a fresh load of the profile.

    The button flips optimistically, and a blocked action flips back later,
    so only a reload says whether it stuck.
    """
    await asyncio.sleep(random.uniform(3.0, 6.0))
    await goto(url)
    final = await _profile_state(evaluate_json, load_timeout)
    if final.get("blocked"):
        raise RateLimitError(f"{action} @{handle}: action blocked")
    return final.get("state")


async def follow(
    handle: str,
    *,
    goto: Callable[[str], Awaitable[None]],
    evaluate_json: EvaluateJson,
    click: Callable[[float, float], Awaitable[None]],
    load_timeout: float = 25.0,
) -> str:
    """Follow `handle` by clicking its profile's Follow button at page
    coordinates with `click`.

    Returns one of: 'followed', 'requested' (a private account), 'already
    following', 'already requested', 'profile is not available', 'no follow
    button' (e.g. your own profile). Raises RateLimitError when Instagram
    blocks the follow, whether openly or by reverting it.
    """
    handle = handle.lstrip("@").strip()
    url, state = await _open_profile(handle, goto, evaluate_json, load_timeout, "follow")
    if state.get("unavailable"):
        return "profile is not available"
    current = state.get("state")
    if current == "following":
        return "already following"
    if current == "requested":
        return "already requested"
    if current != "follow":
        return "no follow button"

    await asyncio.sleep(random.uniform(0.8, 2.5))
    await click(state["x"], state["y"])
    after = await _profile_state(
        evaluate_json, 10.0, until=lambda s: s.get("state") in ("following", "requested")
    )
    if after.get("blocked"):
        raise RateLimitError(f"follow @{handle}: action blocked")

    final = await _state_after_reload(url, handle, goto, evaluate_json, load_timeout, "follow")
    if final == "following":
        return "followed"
    if final == "requested":
        return "requested"
    raise RateLimitError(f"follow @{handle}: reverted to {final!r} after reload")


async def unfollow(
    handle: str,
    *,
    goto: Callable[[str], Awaitable[None]],
    evaluate_json: EvaluateJson,
    click: Callable[[float, float], Awaitable[None]],
    load_timeout: float = 25.0,
) -> str:
    """Unfollow `handle`, or withdraw a pending follow request: click the
    profile's Following / Requested button, then Unfollow in what it opens.

    Returns one of: 'unfollowed', 'request withdrawn', 'not following',
    'profile is not available', 'no follow button', 'no unfollow option' (the
    menu opened without one). Raises RateLimitError when Instagram blocks the
    unfollow, whether openly or by reverting it.
    """
    handle = handle.lstrip("@").strip()
    url, state = await _open_profile(handle, goto, evaluate_json, load_timeout, "unfollow")
    if state.get("unavailable"):
        return "profile is not available"
    current = state.get("state")
    if current == "follow":
        return "not following"
    if current not in ("following", "requested"):
        return "no follow button"

    await asyncio.sleep(random.uniform(0.8, 2.5))
    await click(state["x"], state["y"])
    item = await _poll(evaluate_json, _UNFOLLOW_ITEM_JS, 10.0, until=lambda s: "x" in s)
    if "x" not in item:
        return "no unfollow option"
    await asyncio.sleep(random.uniform(0.5, 1.5))
    await click(item["x"], item["y"])
    after = await _profile_state(evaluate_json, 10.0, until=lambda s: s.get("state") == "follow")
    if after.get("blocked"):
        raise RateLimitError(f"unfollow @{handle}: action blocked")

    final = await _state_after_reload(url, handle, goto, evaluate_json, load_timeout, "unfollow")
    if final == "follow":
        return "request withdrawn" if current == "requested" else "unfollowed"
    raise RateLimitError(f"unfollow @{handle}: reverted to {final!r} after reload")


async def following_result(
    evaluate_json: EvaluateJson,
    *,
    query: dict,
    fallback_app_id: str,
    max_users: int = -1,
) -> ScrapingResult:
    """list_following as a Following ScrapingResult, the accounts in `.users`."""
    start = datetime.now(timezone.utc)
    users = await list_following(
        evaluate_json, fallback_app_id=fallback_app_id, max_users=max_users
    )
    return ScrapingResult(
        query=Query(endpoint="Following", query={**query, "max_users": max_users}, params={}),
        result="success",
        users=users,
        time_started=start,
        time_taken=datetime.now(timezone.utc) - start,
    )


async def follow_result(
    handle: str,
    *,
    query: dict,
    goto: Callable[[str], Awaitable[None]],
    evaluate_json: EvaluateJson,
    click: Callable[[float, float], Awaitable[None]],
) -> ScrapingResult:
    """follow() as a Follow ScrapingResult, its status in `.result`."""
    start = datetime.now(timezone.utc)
    code = await follow(handle, goto=goto, evaluate_json=evaluate_json, click=click)
    return ScrapingResult(
        query=Query(endpoint="Follow", query={**query, "handle": handle}, params={}),
        result=code,
        time_started=start,
        time_taken=datetime.now(timezone.utc) - start,
    )


async def unfollow_result(
    handle: str,
    *,
    query: dict,
    goto: Callable[[str], Awaitable[None]],
    evaluate_json: EvaluateJson,
    click: Callable[[float, float], Awaitable[None]],
) -> ScrapingResult:
    """unfollow() as an Unfollow ScrapingResult, its status in `.result`."""
    start = datetime.now(timezone.utc)
    code = await unfollow(handle, goto=goto, evaluate_json=evaluate_json, click=click)
    return ScrapingResult(
        query=Query(endpoint="Unfollow", query={**query, "handle": handle}, params={}),
        result=code,
        time_started=start,
        time_taken=datetime.now(timezone.utc) - start,
    )
