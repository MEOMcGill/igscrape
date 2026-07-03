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


def test_graphql_error_fires_when_no_progress():
    assert GraphQLError().evaluate(
        _state(error="boom", new_count=0, end_cursor=None)
    ) == "something went wrong - reload"


def test_assemble_user_timeline_includes_date_and_shape_conditions():
    names = [type(c).__name__ for c in assemble_default_stop_conditions("UserTimeline", {})]
    assert "OldestInBatchBelowStartDate" in names
    assert "ResponseShapeError" in names
    assert "EndOfFeed" in names


def test_assemble_search_omits_date_cutoff():
    names = [type(c).__name__ for c in assemble_default_stop_conditions("Search", {})]
    assert "OldestInBatchBelowStartDate" not in names
    assert "MaxPostsReached" in names
