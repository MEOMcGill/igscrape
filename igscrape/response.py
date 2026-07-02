"""Intercept Instagram XHR responses and accumulate posts/users.

Implements the response-branching logic as an async Playwright interceptor.
"""

import inspect
import json
import traceback
from typing import Awaitable, Callable

from playwright.async_api import Page, Response

from .logger import logger

# NB: no trailing slash on /api/graphql — the profile-info GraphQL response
# (data['user'], which carries the user record + is_private) is served from
# exactly "https://www.instagram.com/api/graphql" with no trailing slash, so a
# trailing slash here silently dropped every profile/user response (empty
# users output + no is_private to classify private accounts on).
API_DIRECTORIES = (
    "https://www.instagram.com/api/graphql",
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
        elif "xdt_fbsearch__top_serp_graphql" in keys:
            self._parse_search(data["xdt_fbsearch__top_serp_graphql"] or {})
        elif "xdt_api__v1__discover__chaining" in keys:
            chaining = data["xdt_api__v1__discover__chaining"] or {}
            self.user_metadata_list += chaining.get("users") or []
        elif "user" in keys:
            self.user_metadata_list.append(data["user"])
        elif "highlights" in keys or "xdt_get_inbox_tray_items" in keys:
            return
        else:
            logger.debug(f"Ignoring unhandled data keys: {list(keys)}")

    def _parse_search(self, serp: dict):
        """Keyword-search SERP (xdt_fbsearch__top_serp_graphql).

        Each `edges[].node` is one of several `XDTTopSerp*Unit` typenames; the
        posts live in `XDTTopSerpMediaGridUnit.items`, each a full XDTMediaDict
        (same shape as feed media). Other units (header, accounts) carry no
        posts and are skipped. Successive scroll responses overlap, so we dedup
        by `pk`/`id` — the search scrape's stop condition counts this list.
        """
        seen = {p.get("pk") or p.get("id") for p in self.post_metadata_list}
        for edge in serp.get("edges") or []:
            node = edge.get("node") or {}
            if node.get("__typename") != "XDTTopSerpMediaGridUnit":
                continue
            for item in node.get("items") or []:
                key = item.get("pk") or item.get("id")
                if key in seen:
                    continue
                seen.add(key)
                self.post_metadata_list.append(item)

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
