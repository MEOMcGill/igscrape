"""Intercept Instagram XHR responses and accumulate posts/users.

Two jobs:
  1. Parse intercepted GraphQL responses into posts/users (the passive path,
     still used by the single-shot endpoints).
  2. Capture a *replayable request template* and the freshest auth tokens for
     each paginated endpoint, so the capture-replay loop can issue requests
     directly without scrolling (see igscrape/pagination.py and
     docs/CAPTURE_REPLAY_PLAN.md).

The replay loop feeds responses back in via `ingest_payloads()`, which reuses
the same `_dispatch` branching as the live listener.
"""

import inspect
import json
import traceback
from typing import Awaitable, Callable

from playwright.async_api import Page, Request, Response

from .logger import logger

# Match without trailing slash: the live GraphQL endpoint is posted to as
# `/api/graphql` (no trailing slash), so prefixes must not require one.
API_DIRECTORIES = (
    "https://www.instagram.com/api/graphql",
    "https://www.instagram.com/api/v1",
    "https://www.instagram.com/graphql",
)

# Response `data` keys -> the friendly template name we replay under. The URL
# (/api/graphql/) is shared across queries, so we identify the endpoint by the
# response it produced and stash the request that produced it.
FEED_DATA_KEYS = (
    "xdt_api__v1__feed__timeline__connection",
    "xdt_api__v1__feed__user_timeline_graphql_connection",
)
SEARCH_DATA_KEYS = ("xdt_fbsearch__top_serp_graphql",)


def _post_id(node: dict) -> str | None:
    """Stable id for a post node (XDTMediaDict flat, XDTFeedItem nested)."""
    pid = node.get("pk") or node.get("id")
    if pid is not None:
        return str(pid)
    media = node.get("media") or {}
    pid = media.get("pk") or media.get("id")
    return str(pid) if pid is not None else None


class InstagramResponseInterceptor:
    """Collect posts/users and capture replay templates from IG API traffic."""

    def __init__(self):
        self.post_metadata_list: list[dict] = []
        self.user_metadata_list: list[dict] = []
        self.graphql_request_count: int = 0
        self.page: Page | None = None
        # Optional streaming hook fired with each batch of newly-intercepted
        # posts (raw XHR nodes, not flattened). May be sync or async.
        self.on_new_posts: Callable[[list[dict]], None | Awaitable[None]] | None = None

        # Replayable request templates keyed by friendly name ("user_timeline",
        # "search"). Cleared per scrape (the target id is baked into variables).
        self.templates: dict[str, dict] = {}
        # Freshest auth tokens (raw form + headers) from the most recent
        # qualifying request. Session-level — survives flush().
        self.latest_request_form: dict | None = None
        self.latest_request_headers: dict | None = None

        # Global dedup set so overlapping pages never double-count a post.
        self._seen_post_ids: set[str] = set()

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
        self._seen_post_ids = set()
        # The streaming hook is per-scrape config; clear it so a reused session
        # never fires a previous task's callback on a later scrape.
        self.on_new_posts = None
        # Templates are per-handle (target id is baked into the captured
        # variables), so drop them. Tokens are account/session-level — keep.
        self.templates = {}

    def has_graphql_activity(self) -> bool:
        return self.graphql_request_count > 0

    def get_posts(self) -> list[dict]:
        return self.post_metadata_list

    def get_users(self) -> list[dict]:
        return self.user_metadata_list

    def _add_post(self, node: dict) -> bool:
        """Append a post node unless we've already seen its id. Returns True if
        it was newly added."""
        pid = _post_id(node)
        if pid is not None:
            if pid in self._seen_post_ids:
                return False
            self._seen_post_ids.add(pid)
        self.post_metadata_list.append(node)
        return True

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

        # Capture the replayable template + freshest tokens from this request.
        try:
            await self._capture_request(response.request, data)
        except Exception as e:
            logger.debug(f"template capture skipped: {e}")

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

    async def _capture_request(self, request: Request, data: dict):
        """Stash a replayable template (keyed by endpoint) and refresh tokens
        from any qualifying GraphQL POST."""
        if request.method != "POST":
            return
        post_data = request.post_data
        if not post_data:
            return

        # Local import avoids a module-load cycle (pagination imports nothing
        # from here, but keep the dependency direction explicit).
        from .pagination import parse_form

        form = parse_form(post_data)
        try:
            headers = await request.all_headers()
        except Exception:
            headers = dict(request.headers)

        # Refresh session-level tokens from every qualifying POST.
        self.latest_request_form = form
        self.latest_request_headers = headers

        keys = data.keys()
        if any(k in keys for k in FEED_DATA_KEYS):
            label = "user_timeline"
        elif any(k in keys for k in SEARCH_DATA_KEYS):
            label = "search"
        else:
            return

        variables: dict = {}
        if "variables" in form:
            try:
                variables = json.loads(form["variables"])
            except Exception:
                variables = {}

        # Instagram uses a SEPARATE query for pagination than for the first page
        # (e.g. PolarisKeywordSearchExplorePageRelayQuery vs ...PaginationQuery):
        # only the paginating one carries a cursor (`after` / `max_id`) and the
        # doc_id we must replay. Prefer the cursor-bearing template; never let an
        # initial-page request clobber a paginating one already captured.
        has_cursor = bool(variables.get("after")) or bool(form.get("max_id"))
        existing = self.templates.get(label)
        if existing is not None and existing.get("_has_cursor") and not has_cursor:
            return

        self.templates[label] = {
            "url": request.url,
            "method": request.method,
            "headers": headers,
            "form": form,
            "variables": variables,
            "doc_id": form.get("doc_id"),
            "friendly_name": headers.get("x-fb-friendly-name"),
            "_has_cursor": has_cursor,
        }
        logger.debug(
            f"captured '{label}' template (doc_id={form.get('doc_id')}, "
            f"has_cursor={has_cursor})"
        )

    def ingest_payloads(self, payloads: list[dict]) -> list[dict]:
        """Dispatch already-parsed replay `data` payloads into the accumulators
        and return the newly-added (deduped) posts.

        Unlike the live listener, this does NOT fire `on_new_posts` — the replay
        loop owns that callback so it controls batching.
        """
        before = len(self.post_metadata_list)
        for data in payloads:
            if not isinstance(data, dict):
                continue
            try:
                self._dispatch(data)
            except Exception as e:
                logger.warning(f"Error dispatching replay payload: {e}")
                logger.debug(traceback.format_exc())
        return self.post_metadata_list[before:]

    def _dispatch(self, data: dict):
        """Route a response payload's `data` dict to the right accumulator."""
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
                self._add_post(item)
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

        Posts live in `XDTTopSerpMediaGridUnit.items`, each a full XDTMediaDict
        (same shape as feed media). Other units carry no posts. Dedup is handled
        centrally by `_add_post`.
        """
        for edge in serp.get("edges") or []:
            node = edge.get("node") or {}
            if node.get("__typename") != "XDTTopSerpMediaGridUnit":
                continue
            for item in node.get("items") or []:
                self._add_post(item)

    def _parse_feed(self, feed: dict):
        for edge in feed.get("edges") or []:
            node = edge.get("node")
            if not node:
                continue
            typename = node.get("__typename")
            if typename == "XDTFeedItem":
                if node.get("media") is not None:
                    self._add_post(node)
            elif typename == "XDTMediaDict":
                self._add_post(node)
            else:
                logger.debug(f"Unexpected feed __typename: {typename}")
