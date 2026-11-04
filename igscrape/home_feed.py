"""Scroll a logged-in account's home feed and record what it serves.

Transport-agnostic: the desktop BrowserSession and the phone PhoneSession each
supply their own `scroll_once` and feed responses into the same interceptor, so
the stop logic and the embedded-first-page parsing live here once.

Posts are kept in the order they arrived, which is their order in the feed.
"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable

from .logger import logger
from .response import HOME_FEED_DATA_KEY, InstagramResponseInterceptor
from .stop_conditions import MaxPaginations, MaxPostsReached, NoNewPostsStreak, StopState

# A feed page is ~12 posts spread over several screens, so many scrolls add
# nothing; this counts scrolls, not pages.
HOME_FEED_NO_PROGRESS_STREAK = 20
HOME_FEED_MAX_SCROLLS = 500

# The first feed page is server-rendered into the HTML rather than fetched by
# XHR. Returns the inline JSON scripts that carry it, as a JSON array string.
EMBEDDED_FEED_JS = (
    "JSON.stringify(Array.from(document.querySelectorAll("
    "'script[type=\"application/json\"]'))"
    f".map(s => s.textContent).filter(t => t.includes('{HOME_FEED_DATA_KEY}')))"
)


def embedded_feed_payloads(script_texts: list[str]) -> list[dict]:
    """`data`-shaped payloads ({HOME_FEED_DATA_KEY: connection}) found anywhere
    inside the given inline-script JSON texts."""
    found: list[dict] = []

    def walk(obj, depth: int):
        if depth > 60:
            return
        if isinstance(obj, dict):
            conn = obj.get(HOME_FEED_DATA_KEY)
            if isinstance(conn, dict):
                found.append({HOME_FEED_DATA_KEY: conn})
                return
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, depth + 1)

    for text in script_texts:
        try:
            walk(json.loads(text), 0)
        except (json.JSONDecodeError, TypeError):
            continue
    return found


def parse_embedded_feed_result(raw) -> list[dict]:
    """Parse the string EMBEDDED_FEED_JS evaluates to."""
    try:
        texts = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return embedded_feed_payloads(texts if isinstance(texts, list) else [])


def assemble_home_feed_stop_conditions(
    max_posts: int, max_scrolls: int, max_no_progress_streak: int
) -> list:
    return [
        NoNewPostsStreak(max_no_progress_streak),
        MaxPostsReached(max_posts),
        MaxPaginations(max_scrolls),
    ]


async def scroll_feed(
    *,
    scroll_once: Callable[[], Awaitable[None]],
    interceptor: InstagramResponseInterceptor,
    max_posts: int = -1,
    max_scrolls: int = HOME_FEED_MAX_SCROLLS,
    max_no_progress_streak: int = HOME_FEED_NO_PROGRESS_STREAK,
    dwell: tuple[float, float] = (1.5, 4.0),
    label: str = "home_feed",
    on_scroll: Callable[[], Awaitable[None]] | None = None,
) -> str:
    """Scroll until a stop condition fires and return its result code.

    New posts are counted off `interceptor`, which the caller's transport keeps
    fed while this scrolls. `dwell` is the pause after each scroll, in seconds.
    """
    conditions = assemble_home_feed_stop_conditions(
        max_posts, max_scrolls, max_no_progress_streak
    )
    seen = len(interceptor.post_metadata_list)
    streak = 0
    scroll_index = 0
    while True:
        await scroll_once()
        await asyncio.sleep(random.uniform(*dwell))

        total = len(interceptor.post_metadata_list)
        new = total - seen
        seen = total
        streak = streak + 1 if new == 0 else 0
        if new:
            logger.info(f"{label}: scroll #{scroll_index} +{new} (total {total})")

        state = StopState(
            iter_index=scroll_index + 1,  # scrolls done, so max_scrolls is exact
            cursor_sent=None,
            end_cursor=None,
            has_next_page=True,
            new_count=new,
            all_count=total,
            oldest_in_batch_unix=None,
            timestamped_count=new,
            error=None,
            start_unix=None,
            no_progress_streak=streak,
        )
        for cond in conditions:
            code = cond.evaluate(state)
            if code is not None:
                return code

        scroll_index += 1
        if on_scroll is not None:
            await on_scroll()
