"""Unit tests for the interceptor's replay-ingest + dedup path."""

from igscrape.response import InstagramResponseInterceptor


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
