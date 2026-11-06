"""Unit tests for home-feed collection: the shared scroll loop, embedded first
page, endpoint scoping, and the phone body parsing. No browser or handset."""

import asyncio
import json
import tempfile
from pathlib import Path

from igscrape.accounts_pool import AccountsPool
from igscrape.home_feed import embedded_feed_payloads, parse_embedded_feed_result, scroll_feed
from igscrape.models import Query
from igscrape.parsers import post_flattener
from igscrape.phone_session import instagram_cookies, parse_graphql_body
from igscrape.response import HOME_FEED_DATA_KEY, InstagramResponseInterceptor


def _home(*pks):
    return {
        HOME_FEED_DATA_KEY: {
            "edges": [
                {"node": {"__typename": "XDTFeedItem", "media": {"pk": pk, "taken_at": 1}}}
                for pk in pks
            ]
        }
    }


def test_embedded_payloads_found_deep_in_relay_json():
    script = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {
        "result": {"data": _home("1", "2")}}}]]]}
    payloads = embedded_feed_payloads([json.dumps(script), "not json"])
    assert len(payloads) == 1
    assert len(payloads[0][HOME_FEED_DATA_KEY]["edges"]) == 2


def test_parse_embedded_result_tolerates_empty_and_garbage():
    assert parse_embedded_feed_result(None) == []
    assert parse_embedded_feed_result("{oops") == []
    assert parse_embedded_feed_result(json.dumps([json.dumps({"data": _home("9")})]))


def test_home_feed_scope_accepts_home_and_rejects_other_surfaces():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    interceptor.ingest_payloads([
        _home("1"),
        {"xdt_fbsearch__top_serp_graphql": {"edges": []}},
    ])
    assert [p["media"]["pk"] for p in interceptor.post_metadata_list] == ["1"]


def test_other_endpoints_still_ignore_the_home_feed():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("UserTimeline")
    interceptor.ingest_payloads([_home("1")])
    assert interceptor.post_metadata_list == []


def test_ingest_and_notify_streams_new_posts_once():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    batches = []
    interceptor.on_new_posts = batches.append

    async def run():
        await interceptor.ingest_and_notify([_home("1", "2")])
        await interceptor.ingest_and_notify([_home("2")])

    asyncio.run(run())
    assert [len(b) for b in batches] == [2]


def _scroll(interceptor, pages, **kw):
    """Run scroll_feed where each scroll delivers the next page of pks."""
    pages = list(pages)
    scrolls = []

    async def scroll_once():
        scrolls.append(1)
        if pages:
            interceptor.ingest_payloads([_home(*pages.pop(0))])

    code = asyncio.run(scroll_feed(
        scroll_once=scroll_once, interceptor=interceptor, dwell=(0, 0), **kw
    ))
    return code, len(scrolls)


def test_scroll_stops_on_no_progress_streak():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    code, scrolls = _scroll(interceptor, [["1"], [], ["2"]], max_no_progress_streak=3)
    assert code == "scraped until first ever post was reached"
    assert scrolls == 6
    assert len(interceptor.post_metadata_list) == 2


def test_scroll_stops_at_max_posts():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    code, _ = _scroll(interceptor, [["1", "2"], ["3", "4"], ["5"]], max_posts=3)
    assert code == "success"
    assert len(interceptor.post_metadata_list) == 4


def test_scroll_stops_at_max_scrolls():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    code, scrolls = _scroll(interceptor, [[str(i)] for i in range(100)], max_scrolls=5)
    assert code == "max_paginations_reached"
    assert scrolls == 5


def test_scroll_that_hangs_ends_with_timeout_error():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")

    async def hang():
        await asyncio.sleep(10)

    code = asyncio.run(scroll_feed(
        scroll_once=hang, interceptor=interceptor, dwell=(0, 0), scroll_timeout=0.05
    ))
    assert code == "timeout error"


def test_breather_pauses_every_n_scrolls(monkeypatch):
    pauses = []
    real_sleep = asyncio.sleep

    async def record(seconds):
        pauses.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", record)
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    _scroll(interceptor, [[str(i)] for i in range(100)], max_scrolls=7,
            breather_every=3, breather=(9.5, 9.5))
    assert pauses.count(9.5) == 2


def test_home_feed_query_needs_no_fields():
    Query(endpoint="HomeFeed", query={}, params={})


def test_parse_graphql_body_strips_bloks_prefix():
    assert parse_graphql_body('for (;;);{"data": {"a": 1}}') == {"a": 1}
    assert parse_graphql_body('{"errors": []}') is None
    assert parse_graphql_body("") is None
    assert parse_graphql_body("<html>") is None


def test_instagram_cookies_drops_other_sites():
    jar = [{"name": "a", "domain": ".instagram.com"}, {"name": "b", "domain": ".tiktok.com"}]
    assert [c["name"] for c in instagram_cookies(jar)] == ["a"]


def test_get_available_by_username_claims_only_that_account():
    async def run():
        pool = AccountsPool(db_file=str(Path(tempfile.mkdtemp()) / "accounts.db"))
        for u in ("alice", "bob"):
            await pool.add_account(u, "unused")
            await pool.set_active(u, True, None)
        got = await pool.get_available("bob")
        again = await pool.get_available("bob")
        other = await pool.get_available()
        return got, again, other

    got, again, other = asyncio.run(run())
    assert got.username == "bob"
    assert again is None
    assert other.username == "alice"


def _suggested(pk):
    return {"__typename": "XDTFeedItem", "explore_story": {
        "id": f"s{pk}", "inventory_source": "explore_story",
        "media": {"pk": pk, "taken_at": 1}}}


def test_suggested_posts_are_collected_and_deduped():
    interceptor = InstagramResponseInterceptor()
    interceptor.flush("HomeFeed")
    interceptor.ingest_payloads([{HOME_FEED_DATA_KEY: {"edges": [
        {"node": _suggested("1")},
        {"node": _suggested("1")},
        {"node": {"__typename": "XDTFeedItem", "end_of_feed_demarcator": {}}},
    ]}}])
    assert len(interceptor.post_metadata_list) == 1


def test_flattener_marks_suggested_posts_only():
    followed = {"__typename": "XDTFeedItem", "media": {"pk": "2"}}
    out = post_flattener([_suggested("1"), followed])
    assert out[0]["pk"] == "1"
    assert out[0]["feed_inventory_source"] == "explore_story"
    assert "feed_inventory_source" not in out[1]
