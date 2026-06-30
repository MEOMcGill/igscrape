"""Pluggable stop conditions for the capture-replay pagination loop.

Ported from fbscrape's `stop_conditions.py` and adapted to igscrape's
result-code taxonomy (see models.ScrapingResult). Each condition is a small
object with `evaluate(state) -> str | None`; the loop evaluates them in order
and the first non-None result string terminates the scrape.

The returned strings map onto the worker's result-code sets (worker.py):
SUCCESS_CASES, RETRY_CASES, and the explicitly-handled codes.
"""

from dataclasses import dataclass

from .logger import logger


@dataclass
class StopState:
    """Snapshot of one pagination iteration, passed to every condition."""

    iter_index: int
    cursor_sent: str | None  # cursor used for THIS request; None on bootstrap
    end_cursor: str | None  # cursor returned by THIS response
    has_next_page: bool
    new_count: int  # newly-added (deduped) posts this iteration
    all_count: int  # total unique posts collected so far
    oldest_in_batch_unix: int | None  # min taken_at among this batch's posts
    timestamped_count: int  # how many new posts had a readable timestamp
    error: str | None  # in-body GraphQL error message, if any
    start_unix: int | None  # user-requested start date (UserTimeline only)
    no_progress_streak: int  # consecutive iterations with new_count == 0


class StopCondition:
    def evaluate(self, state: StopState) -> str | None:  # pragma: no cover
        return None


class GraphQLError(StopCondition):
    """Bail on an in-body error — but tolerate side-fragment errors when the
    page still parsed posts and handed us a live cursor (mirrors fbscrape)."""

    def evaluate(self, state):
        if state.error and (state.new_count == 0 or not state.end_cursor):
            logger.warning(f"graphql error response: {state.error}")
            return "something went wrong - reload"
        return None


class EndOfFeed(StopCondition):
    """Stop when Instagram signals no further pages (null cursor / no next)."""

    def evaluate(self, state):
        if not state.has_next_page or not state.end_cursor:
            logger.info(f"end of feed after {state.iter_index} page(s)")
            return "scraped until first ever post was reached"
        return None


class OldestInBatchBelowStartDate(StopCondition):
    """Stop once the batch dips below the requested start date.

    Skipped on the bootstrap iteration (cursor_sent is None): Instagram surfaces
    pinned / out-of-order posts on the first page that would trip this early.
    """

    def evaluate(self, state):
        if state.cursor_sent is None:
            return None
        if state.start_unix is None or state.oldest_in_batch_unix is None:
            return None
        if state.oldest_in_batch_unix < state.start_unix:
            logger.info("oldest post in batch older than start_date")
            return "scraped until user-specified starting date was reached"
        return None


class NoNewPostsStreak(StopCondition):
    """Stop after N consecutive iterations yield no new (deduped) posts."""

    def __init__(self, max_streak: int):
        self.max_streak = max_streak

    def evaluate(self, state):
        if self.max_streak and state.no_progress_streak >= self.max_streak:
            logger.info(f"no new posts for {state.no_progress_streak} page(s)")
            return "scraped until first ever post was reached"
        return None


class MaxPostsReached(StopCondition):
    """Stop once the post-count cap is hit (-1 disables)."""

    def __init__(self, max_posts: int):
        self.max_posts = max_posts

    def evaluate(self, state):
        if self.max_posts and self.max_posts > 0 and state.all_count >= self.max_posts:
            logger.info(f"max_posts cap reached ({self.max_posts})")
            return "success"
        return None


class MaxPaginations(StopCondition):
    """Safety cap on the number of replay iterations (-1 disables)."""

    def __init__(self, max_paginations: int):
        self.max_paginations = max_paginations

    def evaluate(self, state):
        if (
            self.max_paginations
            and self.max_paginations > 0
            and state.iter_index >= self.max_paginations
        ):
            logger.warning(f"hit max_paginations cap ({self.max_paginations})")
            return "max_paginations_reached"
        return None


class ResponseShapeError(StopCondition):
    """New posts parsed but none carried a timestamp → the metadata shape
    changed under us. Non-retryable; surfaces a partial result."""

    def evaluate(self, state):
        if state.new_count > 0 and state.timestamped_count == 0:
            logger.error("posts parsed but none had a timestamp — response shape error")
            return "response_shape_error"
        return None


def assemble_default_stop_conditions(endpoint: str, params: dict) -> list[StopCondition]:
    """Build the canonical condition list for an endpoint.

    UserTimeline is chronological (date cutoff + shape check apply). Search is
    not reliably chronological, so it is count-bounded only.
    """
    max_posts = params.get("max_posts", -1)
    max_paginations = params.get("max_paginations", -1)
    streak = params.get("max_no_progress_streak", 5)

    conditions: list[StopCondition] = [GraphQLError(), EndOfFeed()]
    if endpoint == "UserTimeline":
        conditions.append(OldestInBatchBelowStartDate())
    conditions += [NoNewPostsStreak(streak), MaxPostsReached(max_posts)]
    if endpoint == "UserTimeline":
        conditions.append(ResponseShapeError())
    conditions.append(MaxPaginations(max_paginations))
    return conditions
