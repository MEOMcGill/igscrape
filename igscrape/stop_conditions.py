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
    # Whether the response carried a post connection object at all — i.e. the
    # server resolved the field we asked for (response.has_post_connection).
    # Defaults True so a caller that cannot determine it gets the pre-existing
    # "judge the error by progress alone" behavior.
    connection_present: bool = True


class StopCondition:
    def evaluate(self, state: StopState) -> str | None:  # pragma: no cover
        return None


class GraphQLError(StopCondition):
    """Bail on an in-body error only when the response was actually unusable.

    Instagram serves the profile-posts query with a non-fatal
    `field_type_nullability_mismatch` error permanently attached: HTTP 200, a
    complete `edges` array, a live `end_cursor`, and an `errors` entry that
    carries no `path` (it belongs to a side fragment, not to the connection).
    An error alone therefore says nothing about whether collection can proceed.

    What it may NOT key off is `new_count`. The bootstrap iteration replays the
    captured template's own cursor, so it necessarily re-fetches the page the
    live page-load listener already ingested and `new_count == 0` is structurally
    guaranteed (see browser_session._replay_pagination_loop). Treating that as
    "error and no progress" aborted every single handle at replay #0 while the
    response in hand held a full page of posts and a live cursor.

    Nor is `no_progress_streak` a usable signal, for the same underlying reason:
    the page-load listener keeps ingesting on its own while the replay loop runs,
    so a replayed page can arrive entirely deduped (`new_count == 0`) while the
    feed is in fact advancing — observed twice running on a live handle. The
    reliable test is whether the *cursor* moved.

    So bail when either:
      - the connection object is missing/nulled — the server did not resolve the
        field, nothing here is trustworthy (this also covers transport errors,
        where there are no payloads at all); or
      - Instagram says there is a next page but did not hand us a new cursor to
        reach it — an error plus no way forward, i.e. genuinely stuck.

    Otherwise let the ordinary conditions (end-of-feed, date cutoff, no-progress)
    decide, exactly as on an error-free response. In particular an error on the
    final page is tolerated so EndOfFeed can report success, rather than failing
    a handle after everything was already collected.
    """

    def evaluate(self, state):
        if not state.error:
            return None
        if not state.connection_present:
            logger.warning(f"graphql error response (no connection): {state.error}")
            return "something went wrong - reload"
        advanced = bool(state.end_cursor) and state.end_cursor != state.cursor_sent
        if state.has_next_page and not advanced:
            logger.warning(
                f"graphql error response and the cursor did not advance: {state.error}"
            )
            return "something went wrong - reload"
        logger.debug(f"tolerating non-fatal graphql error: {state.error}")
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
