"""Unit tests for the interceptor's replay-ingest + dedup path."""

import asyncio
import json
from urllib.parse import urlencode

from igscrape.response import InstagramResponseInterceptor


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
