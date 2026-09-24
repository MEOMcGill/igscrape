"""Unit tests for the interceptor's replay-ingest + dedup path."""

import asyncio
import json
from urllib.parse import urlencode

from igscrape.response import InstagramResponseInterceptor, has_post_connection


class _FakeRequest:
    """Minimal stand-in for a Playwright Request for _capture_request."""

    def __init__(self, url, form: dict, headers: dict, method="POST"):
        self.url = url
        self.method = method
        self.post_data = urlencode(form)
        self._headers = headers

    @property
    def headers(self):
        return self._headers

    async def all_headers(self):
        return self._headers


_SEARCH_DATA = {"xdt_fbsearch__top_serp_graphql": {"edges": []}}


def _capture(interceptor, request):
    asyncio.run(interceptor._capture_request(request, _SEARCH_DATA))


def _feed_data(edges):
    return {
        "xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": edges,
            "page_info": {"end_cursor": "C", "has_next_page": True},
        }
    }


def test_ingest_payloads_returns_new_posts():
    interceptor = InstagramResponseInterceptor()
    data = _feed_data(
        [
            {"node": {"__typename": "XDTMediaDict", "pk": "1", "taken_at": 1}},
            {"node": {"__typename": "XDTFeedItem", "media": {"pk": "2", "taken_at": 2}}},
        ]
    )
    new = interceptor.ingest_payloads([data])
    assert len(new) == 2
    assert len(interceptor.post_metadata_list) == 2


def test_ingest_payloads_dedups_across_pages():
    interceptor = InstagramResponseInterceptor()
    page1 = _feed_data([{"node": {"__typename": "XDTMediaDict", "pk": "1", "taken_at": 1}}])
    # Overlapping page: pk "1" repeats, "2" is new.
    page2 = _feed_data(
        [
            {"node": {"__typename": "XDTMediaDict", "pk": "1", "taken_at": 1}},
            {"node": {"__typename": "XDTMediaDict", "pk": "2", "taken_at": 2}},
        ]
    )
    assert len(interceptor.ingest_payloads([page1])) == 1
    new2 = interceptor.ingest_payloads([page2])
    assert len(new2) == 1
    assert new2[0]["pk"] == "2"
    assert len(interceptor.post_metadata_list) == 2


def test_feed_item_without_media_is_skipped():
    interceptor = InstagramResponseInterceptor()
    data = _feed_data([{"node": {"__typename": "XDTFeedItem", "media": None}}])
    assert interceptor.ingest_payloads([data]) == []


def test_search_serp_dedup():
    interceptor = InstagramResponseInterceptor()
    serp = {
        "xdt_fbsearch__top_serp_graphql": {
            "edges": [
                {
                    "node": {
                        "__typename": "XDTTopSerpMediaGridUnit",
                        "items": [
                            {"pk": "a", "taken_at": 1},
                            {"pk": "b", "taken_at": 2},
                        ],
                    }
                },
                {"node": {"__typename": "XDTTopSerpAccountsUnit", "items": []}},
            ]
        }
    }
    new = interceptor.ingest_payloads([serp])
    assert {p["pk"] for p in new} == {"a", "b"}
    # Re-ingesting the same SERP yields nothing new.
    assert interceptor.ingest_payloads([serp]) == []


def test_capture_prefers_pagination_template_over_initial():
    interceptor = InstagramResponseInterceptor()
    hdrs = {"x-fb-friendly-name": "PolarisKeywordSearchExplorePageRelayQuery"}

    # 1) Initial-page request: variables carry no cursor.
    initial = _FakeRequest(
        "https://www.instagram.com/api/graphql",
        {"doc_id": "111", "variables": json.dumps({"query": "x"})},
        hdrs,
    )
    _capture(interceptor, initial)
    assert interceptor.templates["search"]["doc_id"] == "111"
    assert interceptor.templates["search"]["_has_cursor"] is False

    # 2) Pagination request: carries `after` + a different doc_id. Wins.
    paginating = _FakeRequest(
        "https://www.instagram.com/api/graphql",
        {"doc_id": "222", "variables": json.dumps({"after": "CUR", "first": 24, "query": "x"})},
        {"x-fb-friendly-name": "PolarisKeywordSearchExplorePageRelayPaginationQuery"},
    )
    _capture(interceptor, paginating)
    assert interceptor.templates["search"]["doc_id"] == "222"
    assert interceptor.templates["search"]["_has_cursor"] is True

    # 3) A later initial-page request must NOT clobber the paginating template.
    _capture(interceptor, initial)
    assert interceptor.templates["search"]["doc_id"] == "222"


def test_capture_matches_url_without_trailing_slash():
    interceptor = InstagramResponseInterceptor()
    req = _FakeRequest(
        "https://www.instagram.com/api/graphql",  # no trailing slash
        {"doc_id": "1", "variables": json.dumps({"after": "C"})},
        {},
    )
    _capture(interceptor, req)
    assert "search" in interceptor.templates


def test_flush_clears_posts_and_templates_but_keeps_tokens():
    interceptor = InstagramResponseInterceptor()
    interceptor.ingest_payloads([_feed_data([{"node": {"__typename": "XDTMediaDict", "pk": "1"}}])])
    interceptor.templates["user_timeline"] = {"doc_id": "x"}
    interceptor.latest_request_form = {"fb_dtsg": "tok"}
    interceptor.flush()
    assert interceptor.post_metadata_list == []
    assert interceptor.templates == {}
    assert interceptor._seen_post_ids == set()
    assert interceptor.latest_request_form == {"fb_dtsg": "tok"}  # session-level, kept


# ---- has_post_connection: telling a side-fragment error from a real breakage ----


def test_has_post_connection_true_for_present_connection():
    assert has_post_connection([_feed_data([{"node": {"__typename": "XDTMediaDict", "pk": "1"}}])])


def test_has_post_connection_true_for_empty_but_present_connection():
    # `edges: []` still means the server resolved the field — not a breakage.
    assert has_post_connection([_feed_data([])])


def test_has_post_connection_false_for_nulled_connection():
    # What a real field-resolution failure looks like: the key is there, nulled.
    assert not has_post_connection(
        [{"xdt_api__v1__feed__user_timeline_graphql_connection": None}]
    )


def test_has_post_connection_false_for_unrelated_payload():
    assert not has_post_connection([{"xdt_viewer": {"user": {}}}])
    assert not has_post_connection([])
    assert not has_post_connection([None, "not a dict"])


def test_has_post_connection_true_for_search_serp():
    assert has_post_connection([{"xdt_fbsearch__top_serp_graphql": {"edges": []}}])


# ---- profile-info template capture + chaining gating ----


_PROFILE_DATA = {"user": {"username": "natgeo", "follower_count": 1}, "viewer": {}}


def test_captures_the_profile_info_query_as_its_own_template():
    interceptor = InstagramResponseInterceptor()
    req = _FakeRequest(
        "https://www.instagram.com/api/graphql",
        {"doc_id": "28036671149327607", "variables": json.dumps({"id": "787132"})},
        {"x-fb-friendly-name": "PolarisProfilePageContentQuery"},
    )
    asyncio.run(interceptor._capture_request(req, _PROFILE_DATA))
    assert interceptor.templates["profile_info"]["doc_id"] == "28036671149327607"
    assert interceptor.templates["profile_info"]["variables"]["id"] == "787132"


def test_profile_info_capture_needs_an_id_to_retarget():
    interceptor = InstagramResponseInterceptor()
    req = _FakeRequest(
        "https://www.instagram.com/api/graphql",
        {"doc_id": "1", "variables": json.dumps({"username": "natgeo"})},
        {},
    )
    asyncio.run(interceptor._capture_request(req, _PROFILE_DATA))
    assert "profile_info" not in interceptor.templates


def _chaining(usernames):
    return {
        "xdt_api__v1__discover__chaining": {
            "users": [{"username": u, "pk": u} for u in usernames]
        }
    }


def test_suggested_users_are_ignored_outside_the_chaining_endpoint():
    # The carousel fires on any profile page load; its users are not the handle
    # being scraped.
    interceptor = InstagramResponseInterceptor()
    interceptor.ingest_payloads([_chaining(["a", "b"])])
    assert interceptor.user_metadata_list == []


def test_chaining_endpoint_still_collects_suggested_users():
    interceptor = InstagramResponseInterceptor()
    interceptor.collect_chaining_users = True
    interceptor.ingest_payloads([_chaining(["a", "b"])])
    assert [u["username"] for u in interceptor.user_metadata_list] == ["a", "b"]


def test_flush_clears_the_chaining_opt_in():
    interceptor = InstagramResponseInterceptor()
    interceptor.collect_chaining_users = True
    interceptor.flush()
    assert interceptor.collect_chaining_users is False
