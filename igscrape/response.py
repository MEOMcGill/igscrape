"""Intercept Instagram XHR responses and accumulate posts/users.

Ports the branching logic from instagram-scraper's
InstagramSession.intercept_response (post_scraper.py:761-840) into an
async Playwright interceptor.
"""

import inspect
import json
import traceback
from typing import Awaitable, Callable

from playwright.async_api import Page, Response

from .logger import logger

API_DIRECTORIES = (
    "https://www.instagram.com/api/graphql/",
    "https://www.instagram.com/api/v1",
    "https://www.instagram.com/graphql/",
)


class InstagramResponseInterceptor:
    """Collect posts and users from intercepted Instagram API responses."""

    def __init__(self):
        self.post_metadata_list: list[dict] = []
        self.user_metadata_list: list[dict] = []
        self.graphql_request_count: int = 0
        self.page: Page | None = None
        # Optional streaming hook fired with each batch of newly-intercepted
        # posts (raw XHR nodes, not flattened) as they arrive during a scrape.
        # May be sync or async.
        self.on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None

    def setup_interception(self, page: Page):
        self.page = page
        self.page.on("response", self._on_response)
        logger.info("Instagram response interception enabled")

    def stop_interception(self):
        if self.page:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self.page = None

    def flush(self):
        self.post_metadata_list = []
        self.user_metadata_list = []
        self.graphql_request_count = 0
        # The streaming hook is per-scrape config; clear it so a reused session
        # never fires a previous task's callback on a later scrape.
        self.on_new_posts = None

    def has_graphql_activity(self) -> bool:
        return self.graphql_request_count > 0

    def get_posts(self) -> list[dict]:
        return self.post_metadata_list

    def get_users(self) -> list[dict]:
        return self.user_metadata_list

    async def _on_response(self, response: Response):
        if response.request.resource_type != "xhr":
            return

        url = response.url
        if not any(url.startswith(d) for d in API_DIRECTORIES):
            return

        try:
            body = await response.body()
        except Exception:
            return

        self.graphql_request_count += 1

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return

        data = payload.get("data")
        if not isinstance(data, dict):
            return

        before = len(self.post_metadata_list)
        try:
            self._dispatch(data)
        except Exception as e:
            logger.warning(f"Error handling intercepted response: {e}")
            logger.debug(traceback.format_exc())

        new_posts = self.post_metadata_list[before:]
        if new_posts and self.on_new_posts is not None:
            try:
                res = self.on_new_posts(new_posts)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.warning(f"on_new_posts hook raised: {e}")
                logger.debug(traceback.format_exc())

    def _dispatch(self, data: dict):
        """Route a response payload's `data` dict to the right accumulator.

        Mirrors the if/elif chain in post_scraper.py:793-826.
        """
        keys = data.keys()

        if "xdt_notification_badge" in keys or "lightspeed_web_request_for_igd" in keys:
            return

        if "xdt_api__v1__feed__timeline__connection" in keys:
            self._parse_feed(data["xdt_api__v1__feed__timeline__connection"])
        elif "xdt_api__v1__feed__user_timeline_graphql_connection" in keys:
            self._parse_feed(data["xdt_api__v1__feed__user_timeline_graphql_connection"])
        elif "xdt_api__v1__media__shortcode__web_info" in keys:
            shortcode_data = data["xdt_api__v1__media__shortcode__web_info"]
            for item in shortcode_data.get("items") or []:
                self.post_metadata_list.append(item)
        elif "xdt_api__v1__discover__chaining" in keys:
            chaining = data["xdt_api__v1__discover__chaining"] or {}
            self.user_metadata_list += chaining.get("users") or []
        elif "user" in keys:
            self.user_metadata_list.append(data["user"])
        elif "highlights" in keys or "xdt_get_inbox_tray_items" in keys:
            return
        else:
            logger.debug(f"Ignoring unhandled data keys: {list(keys)}")

    def _parse_feed(self, feed: dict):
        """From post_scraper.py:728-749."""
        for edge in feed.get("edges") or []:
            node = edge.get("node")
            if not node:
                continue
            typename = node.get("__typename")
            if typename == "XDTFeedItem":
                if node.get("media") is not None:
                    self.post_metadata_list.append(node)
            elif typename == "XDTMediaDict":
                self.post_metadata_list.append(node)
            else:
                logger.debug(f"Unexpected feed __typename: {typename}")
