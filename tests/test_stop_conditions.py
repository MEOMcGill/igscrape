"""Unit tests for igscrape.stop_conditions."""

from igscrape.stop_conditions import (
    EndOfFeed,
    GraphQLError,
    MaxPaginations,
    MaxPostsReached,
    NoNewPostsStreak,
    OldestInBatchBelowStartDate,
    ResponseShapeError,
    StopState,
    assemble_default_stop_conditions,
)


def _state(**overrides):
    base = dict(
        iter_index=1,
        cursor_sent="CUR",
        end_cursor="CUR2",
        has_next_page=True,
        new_count=5,
        all_count=10,
        oldest_in_batch_unix=1700000000,
        timestamped_count=5,
        error=None,
        start_unix=1600000000,
        no_progress_streak=0,
    )
    base.update(overrides)
    return StopState(**base)


def test_end_of_feed_on_no_next_page():
    assert EndOfFeed().evaluate(_state(has_next_page=False)) == (
        "scraped until first ever post was reached"
    )


def test_end_of_feed_on_null_cursor():
    assert EndOfFeed().evaluate(_state(end_cursor=None)) == (
        "scraped until first ever post was reached"
    )


def test_end_of_feed_continues_when_more_available():
    assert EndOfFeed().evaluate(_state()) is None


def test_oldest_below_start_date_skips_bootstrap():
    # cursor_sent=None marks the bootstrap iteration — must not trip early.
    assert OldestInBatchBelowStartDate().evaluate(
        _state(cursor_sent=None, oldest_in_batch_unix=1500000000)
    ) is None


def test_oldest_below_start_date_fires():
    assert OldestInBatchBelowStartDate().evaluate(
        _state(oldest_in_batch_unix=1500000000)
    ) == "scraped until user-specified starting date was reached"


def test_oldest_below_start_date_continues_when_above():
    assert OldestInBatchBelowStartDate().evaluate(_state()) is None


def test_max_posts_reached():
    assert MaxPostsReached(10).evaluate(_state(all_count=10)) == "success"
    assert MaxPostsReached(10).evaluate(_state(all_count=9)) is None
    assert MaxPostsReached(-1).evaluate(_state(all_count=9999)) is None


def test_max_paginations():
    assert MaxPaginations(5).evaluate(_state(iter_index=5)) == "max_paginations_reached"
    assert MaxPaginations(5).evaluate(_state(iter_index=4)) is None
    assert MaxPaginations(-1).evaluate(_state(iter_index=10**6)) is None


def test_no_new_posts_streak():
    assert NoNewPostsStreak(3).evaluate(_state(no_progress_streak=3)) == (
        "scraped until first ever post was reached"
    )
    assert NoNewPostsStreak(3).evaluate(_state(no_progress_streak=2)) is None


def test_response_shape_error():
    assert ResponseShapeError().evaluate(
        _state(new_count=4, timestamped_count=0)
    ) == "response_shape_error"
    # No false positive on a pure-overlap page (nothing new at all).
    assert ResponseShapeError().evaluate(_state(new_count=0, timestamped_count=0)) is None
    assert ResponseShapeError().evaluate(_state(new_count=4, timestamped_count=4)) is None


def test_graphql_error_tolerated_with_posts_and_cursor():
    # Side-fragment error but page still produced posts + a live cursor.
    assert GraphQLError().evaluate(_state(error="boom", new_count=5, end_cursor="C")) is None


NULLABILITY = (
    "A server error field_type_nullability_mismatch occured. "
    "Check server logs for details."
)


def test_graphql_error_tolerated_on_bootstrap_refetch():
    """The regression that aborted every handle at replay #0.

    The bootstrap replay re-sends the captured template's own cursor, so it
    re-fetches the page the live listener already ingested: new_count == 0 with a
    full connection and a live cursor in hand. Instagram attaches a permanent
    non-fatal nullability error to that response. It must not end the scrape.
    """
    assert GraphQLError().evaluate(
        _state(
            iter_index=0,
            cursor_sent="CUR",
            error=NULLABILITY,
            new_count=0,
            no_progress_streak=1,
            end_cursor="CUR2",
            has_next_page=True,
            connection_present=True,
        )
    ) is None


def test_graphql_error_fires_when_connection_missing():
    # Nulled / absent connection: the server never resolved the field, so
    # nothing in this response is trustworthy.
    assert GraphQLError().evaluate(
        _state(error="boom", new_count=0, connection_present=False)
    ) == "something went wrong - reload"


def test_graphql_error_tolerated_while_cursor_advances_despite_dedup():
    """A fully-deduped page is not stagnation.

    The page-load listener keeps ingesting while the replay loop runs, so a
    replayed page can arrive entirely duplicate (new_count == 0) two iterations
    running while the feed is really advancing — observed live on @madwanika.
    Only the cursor failing to move means stuck.
    """
    assert GraphQLError().evaluate(
        _state(
            error=NULLABILITY,
            new_count=0,
            no_progress_streak=2,
            cursor_sent="CUR",
            end_cursor="CUR2",
            has_next_page=True,
        )
    ) is None


def test_graphql_error_fires_when_cursor_does_not_advance():
    # Instagram claims a next page but returned the cursor we just sent: an
    # error with no way forward.
    assert GraphQLError().evaluate(
        _state(error=NULLABILITY, cursor_sent="CUR", end_cursor="CUR", has_next_page=True)
    ) == "something went wrong - reload"


def test_graphql_error_fires_when_next_page_promised_but_no_cursor():
    assert GraphQLError().evaluate(
        _state(error=NULLABILITY, cursor_sent="CUR", end_cursor=None, has_next_page=True)
    ) == "something went wrong - reload"


def test_graphql_error_tolerated_on_final_page():
    """End of feed with the error still attached must stay a success path.

    GraphQLError runs before EndOfFeed, so tolerating here is what lets a
    low-volume account terminate as "reached first ever post" instead of failing
    after everything was already collected.
    """
    state = _state(
        error=NULLABILITY, new_count=4, end_cursor=None, has_next_page=False
    )
    assert GraphQLError().evaluate(state) is None
    assert EndOfFeed().evaluate(state) == "scraped until first ever post was reached"


def test_no_error_is_never_flagged():
    assert GraphQLError().evaluate(_state(error=None, connection_present=False)) is None


def test_assemble_user_timeline_includes_date_and_shape_conditions():
    names = [type(c).__name__ for c in assemble_default_stop_conditions("UserTimeline", {})]
    assert "OldestInBatchBelowStartDate" in names
    assert "ResponseShapeError" in names
    assert "EndOfFeed" in names


def test_assemble_search_omits_date_cutoff():
    names = [type(c).__name__ for c in assemble_default_stop_conditions("Search", {})]
    assert "OldestInBatchBelowStartDate" not in names
    assert "MaxPostsReached" in names


def test_assemble_search_honours_configured_no_progress_streak():
    """The streak cap is what actually ends most keyword searches, so a caller
    raising it must reach the assembled condition rather than the default."""
    conditions = assemble_default_stop_conditions(
        "Search", {"max_no_progress_streak": 25}
    )
    streak = next(c for c in conditions if isinstance(c, NoNewPostsStreak))
    assert streak.max_streak == 25
    # A tail that goes quiet for longer than the old default no longer stops.
    assert streak.evaluate(_state(no_progress_streak=5)) is None
    assert streak.evaluate(_state(no_progress_streak=25)) is not None
