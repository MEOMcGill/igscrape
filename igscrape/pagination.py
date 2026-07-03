"""Cursor pagination strategies + replay-request construction.

Pure functions and small strategy classes — no Playwright, no I/O — so the
capture-replay loop's logic is unit-testable without a live browser.

Instagram does not paginate uniformly: the web GraphQL feed/search connections
use an `after` cursor with `page_info {end_cursor, has_next_page}`, while some
`/api/v1/...` endpoints use `max_id` / `next_max_id` + `more_available`. A
`CursorStrategy` encapsulates that difference so the loop in
`browser_session.py` stays endpoint-agnostic (see
docs/CAPTURE_REPLAY_PLAN.md §"Design principle").
"""

import copy
import json
from urllib.parse import parse_qsl, urlencode

from .logger import logger

# Form fields that rotate per-request; replaying a stale value can 400 / bounce
# the session to a login wall. Refreshed from the freshest captured request on
# every replay (only those actually present in the template are touched).
TOKEN_REFRESH_FORM_KEYS = (
    "fb_dtsg",
    "jazoest",
    "lsd",
    "__csr",
    "__dyn",
    "__hsi",
    "__spin_r",
    "__spin_t",
    "__spin_b",
    "__rev",
    "__s",
    "__a",
    "__req",
    "__ccg",
    "av",
    "__user",
)
# Header tokens refreshed the same way.
TOKEN_REFRESH_HEADER_KEYS = (
    "x-csrftoken",
    "x-ig-www-claim",
    "x-fb-lsd",
    "x-asbd-id",
    "x-ig-app-id",
)
# Hop-by-hop / auto-managed headers that must NOT be replayed. Playwright's
# request context sets host/content-length/cookie/accept-encoding itself.
HEADER_DROP = {
    "host",
    "content-length",
    "accept-encoding",
    "cookie",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "upgrade",
    "proxy-connection",
}

DEFAULT_PAGE_COUNT = 12


def parse_form(post_data: str) -> dict:
    """Parse a urlencoded request body into a flat dict (blanks kept)."""
    return dict(parse_qsl(post_data or "", keep_blank_values=True))


def _lower(d: dict | None) -> dict:
    return {k.lower(): v for k, v in (d or {}).items()}


def clean_headers(headers: dict) -> dict:
    """Lowercase keys and drop hop-by-hop / auto-managed headers."""
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in HEADER_DROP or lk.startswith(":"):
            continue
        out[lk] = v
    return out


def merge_header_tokens(headers: dict, latest_headers: dict | None) -> dict:
    """Clean `headers` and overwrite volatile token headers with the freshest
    values seen on the wire (if any)."""
    out = clean_headers(headers)
    lh = _lower(latest_headers)
    for k in TOKEN_REFRESH_HEADER_KEYS:
        if k in lh:
            out[k] = lh[k]
    return out


def build_replay_body(
    template: dict,
    cursor: str | None,
    count: int,
    strategy: "CursorStrategy",
    latest_form: dict | None = None,
) -> str:
    """Construct the urlencoded body for the next replay.

    Starts from the captured form, lets the strategy splice in the cursor/count
    (into `variables` for GraphQL, into a form field for v1), then refreshes the
    volatile auth tokens from the freshest captured request.
    """
    form = dict(template.get("form") or {})
    variables = dict(template.get("variables") or {})
    strategy.apply_cursor(form, variables, cursor, count)
    if variables or "variables" in form:
        form["variables"] = json.dumps(variables, separators=(",", ":"))
    lf = latest_form or {}
    for k in TOKEN_REFRESH_FORM_KEYS:
        if lf.get(k):
            form[k] = lf[k]
    return urlencode(form)


def _swap_in_place(obj, replacements: dict[str, str]) -> None:
    """Recursively replace any string leaf found in `replacements` (old -> new)
    with its mapped value, mutating `obj` in place."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v in replacements:
                obj[k] = replacements[v]
            else:
                _swap_in_place(v, replacements)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and v in replacements:
                obj[i] = replacements[v]
            else:
                _swap_in_place(v, replacements)


def substitute_identity(
    template: dict,
    seed_username: str,
    seed_user_id: str | int,
    new_username: str,
    new_user_id: str | int,
) -> dict:
    """Re-target a captured timeline template at a different handle.

    The captured request bakes the seed handle's identity into its `variables`
    (Instagram keys the profile-posts connection off either the username or the
    numeric user id, depending on the query), so we deep-copy the template and
    swap the seed handle's username *and* id for the new handle's wherever they
    appear. This is the "substitute" half of lazy-seed-plus-substitute: harvest a
    real signed template once, then re-point it per handle without a page load.

    We also reset the Relay cursor: the seed template is the *paginating* request
    (`_has_cursor=True`), so its `after` is the seed's mid-feed cursor — a fresh
    handle must start at page 1. `form["variables"]` is re-dumped so a caller
    reading the raw form (rather than the parsed `variables`) stays consistent.
    """
    new = copy.deepcopy(template)
    replacements = {
        str(seed_username): str(new_username),
        str(seed_user_id): str(new_user_id),
    }
    variables = new.get("variables") or {}
    _swap_in_place(variables, replacements)
    # Start at page 1 — drop any cursor baked into the paginating template.
    variables["after"] = None
    variables.pop("before", None)
    variables.pop("last", None)
    new["variables"] = variables
    if isinstance(new.get("form"), dict) and "variables" in new["form"]:
        new["form"]["variables"] = json.dumps(variables, separators=(",", ":"))
    new["_has_cursor"] = False
    return new


def parse_response(text: str) -> tuple[list[dict], list[dict]]:
    """Parse a GraphQL response (JSON or JSONL) into (data_payloads, errors).

    Returns the list of top-level `data` dicts and any `errors` entries (plus a
    synthetic error for `{"status":"fail",...}` bodies).
    """
    text = (text or "").strip()
    if not text:
        return [], []
    objs: list = []
    try:
        objs = [json.loads(text)]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    payloads: list[dict] = []
    errors: list[dict] = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("data"), dict):
            payloads.append(obj["data"])
        if obj.get("errors"):
            errors.extend(e for e in obj["errors"] if isinstance(e, dict))
        if obj.get("status") == "fail":
            errors.append({"message": obj.get("message") or "status: fail"})
    return payloads, errors


def errors_indicate_rate_limit(errors: list[dict]) -> bool:
    """Heuristic: does an errors[] payload mean we are being throttled?"""
    needles = ("wait a few minutes", "try again later", "rate limit", "please wait")
    for e in errors:
        msg = str(e.get("message") or "").lower()
        if any(n in msg for n in needles):
            return True
    return False


def find_page_info(payloads: list[dict]) -> tuple[str | None, bool]:
    """Return (end_cursor, has_next_page) from the shallowest `page_info`.

    Instagram nests pagination info for sub-streams (Reels, clips, suggested
    rows); the page-level cursor is always the shallowest one, so we pick by
    minimum depth — mirrors fbscrape's shortest-path cursor selection.
    """
    best: tuple[int, str | None, bool] | None = None

    def walk(obj, depth: int):
        nonlocal best
        if isinstance(obj, dict):
            pi = obj.get("page_info")
            if isinstance(pi, dict) and ("end_cursor" in pi or "has_next_page" in pi):
                if best is None or depth < best[0]:
                    best = (depth, pi.get("end_cursor"), bool(pi.get("has_next_page")))
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, depth + 1)

    for data in payloads:
        walk(data, 0)
    if best is None:
        return None, False
    return best[1], best[2]


def _deep_find_first(payloads: list[dict], key: str):
    """Return the first value for `key` found anywhere in the payloads."""
    stack = list(payloads)
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)
    return None


class CursorStrategy:
    """Interface: how to seed, inject, and read a cursor for one endpoint."""

    name = "base"

    def initial_cursor(self, template: dict) -> str | None:
        return None

    def apply_cursor(self, form: dict, variables: dict, cursor: str | None, count: int) -> None:
        raise NotImplementedError

    def extract(self, payloads: list[dict]) -> tuple[str | None, bool]:
        raise NotImplementedError


class GraphQLCursorStrategy(CursorStrategy):
    """`variables.after` cursor + `page_info` extraction (web GraphQL feeds)."""

    name = "graphql"

    def initial_cursor(self, template: dict) -> str | None:
        return (template.get("variables") or {}).get("after")

    def apply_cursor(self, form, variables, cursor, count):
        variables["after"] = cursor
        # `count` lives under different keys across IG queries; set whichever
        # the captured request actually used, leave the rest untouched.
        if variables.get("first"):
            variables["first"] = count
        data = variables.get("data")
        if isinstance(data, dict) and "count" in data:
            data["count"] = count

    def extract(self, payloads):
        return find_page_info(payloads)


class V1MaxIdStrategy(CursorStrategy):
    """`max_id` form field + `next_max_id` / `more_available` (v1 endpoints)."""

    name = "v1_max_id"

    def initial_cursor(self, template: dict) -> str | None:
        return (template.get("form") or {}).get("max_id")

    def apply_cursor(self, form, variables, cursor, count):
        if cursor:
            form["max_id"] = cursor
        else:
            form.pop("max_id", None)

    def extract(self, payloads):
        next_max_id = _deep_find_first(payloads, "next_max_id")
        more = _deep_find_first(payloads, "more_available")
        has_next = bool(more) if more is not None else bool(next_max_id)
        return next_max_id, has_next


def select_cursor_strategy(template: dict) -> CursorStrategy:
    """Pick a strategy from the captured template's shape.

    A GraphQL request carries a `variables` JSON blob; a v1 request carries a
    `max_id` form field. Default to GraphQL (the web feed's usual shape).
    """
    if template.get("variables"):
        strat = GraphQLCursorStrategy()
    elif (template.get("form") or {}).get("max_id") is not None:
        strat = V1MaxIdStrategy()
    else:
        strat = GraphQLCursorStrategy()
    logger.debug(f"selected cursor strategy: {strat.name}")
    return strat
