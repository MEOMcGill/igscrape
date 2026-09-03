"""Unit tests for the profile availability classifier and the rich-record
enrichment replay.

`_classify_profile` is a pure mapping from a profile `user` record to an
availability result code (or None when the timeline is scrapeable). It reads no
instance state, so we exercise it as an unbound method with a dummy self — no
browser required, and the same trick serves `_enrich_profile` with a stub.
"""

import asyncio
import json
import types

from igscrape.browser_session import BrowserSession


def _classify(user: dict):
    return BrowserSession._classify_profile(None, user)


def test_public_with_posts_is_scrapeable():
    user = {"is_private": False, "edge_owner_to_timeline_media": {"count": 5}}
    assert _classify(user) is None


def test_private_not_followed_is_account_private():
    user = {
        "is_private": True,
        "followed_by_viewer": False,
        "edge_owner_to_timeline_media": {"count": 5},
    }
    assert _classify(user) == "account is private"


def test_private_but_followed_is_scrapeable():
    user = {
        "is_private": True,
        "followed_by_viewer": True,
        "edge_owner_to_timeline_media": {"count": 5},
    }
    assert _classify(user) is None


def test_zero_posts_is_no_posts():
    user = {"is_private": False, "edge_owner_to_timeline_media": {"count": 0}}
    assert _classify(user) == "no posts"


# ---- owner resolution when IG nulls each node's `user` ----


def _conn(edges):
    return [{"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": edges}}]


def _null_user_node(owner="2931777286"):
    """The real shape of a profile-posts edge: `user` nulled, author in owner_id."""
    return {"node": {"__typename": "XDTMediaDict", "pk": "1", "user": None,
                     "owner_id": {"pk": owner, "id": owner}}}


def test_owner_falls_back_to_owner_id_when_user_is_null():
    owner = BrowserSession._owner_from_payloads(_conn([_null_user_node()]), "charest_isabelle")
    # Without this the whole branch reported every public account as private.
    assert owner is not None
    assert owner["pk"] == "2931777286"
    assert owner["username"] == "charest_isabelle"
    # ...and it must survive the availability classifier as scrapeable.
    assert _classify(owner) is None


def test_owner_prefers_a_real_user_record_when_present():
    node = {"node": {"user": {"username": "someone", "pk": "5", "is_private": False}}}
    owner = BrowserSession._owner_from_payloads(_conn([node]), "charest_isabelle")
    assert owner["username"] == "someone"


def test_owner_is_none_when_connection_has_no_edges():
    # Private-and-not-followed: connection present but empty. Must stay None so
    # _resolve_profile can report "account is private".
    assert BrowserSession._owner_from_payloads(_conn([]), "x") is None


def test_owner_is_none_when_no_connection():
    assert BrowserSession._owner_from_payloads([{"xdt_viewer": {}}], "x") is None


def test_owner_without_handle_omits_username():
    owner = BrowserSession._owner_from_payloads(_conn([_null_user_node()]))
    assert owner["pk"] == "2931777286"
    assert "username" not in owner


# ---- the rich profile-info record shape (PolarisProfilePageContentQuery) ----


def test_rich_record_private_and_not_following_is_account_private():
    user = {
        "is_private": True,
        "friendship_status": {"following": False},
        "media_count": 12,
        "follower_count": 300,
    }
    assert _classify(user) == "account is private"


def test_rich_record_private_but_following_is_scrapeable():
    user = {
        "is_private": True,
        "friendship_status": {"following": True},
        "media_count": 12,
    }
    assert _classify(user) is None


def test_rich_record_zero_media_count_is_no_posts():
    assert _classify({"is_private": False, "media_count": 0}) == "no posts"


def test_rich_record_public_with_posts_is_scrapeable():
    assert _classify({"is_private": False, "media_count": 12}) is None


# ---- _enrich_profile: replaying the id-keyed profile-info query ----


def _stub_session(reply, seed=True):
    """A BrowserSession stand-in exposing only what _enrich_profile touches."""
    sent = {}

    async def _send_replay(template, body, headers):
        sent["template"] = template
        return reply, None

    return types.SimpleNamespace(
        _profile_seed={
            "template": {
                "url": "https://www.instagram.com/api/graphql",
                "headers": {"x-csrftoken": "t"},
                "form": {"doc_id": "1", "variables": json.dumps({"id": "787132"})},
                "variables": {"id": "787132"},
                "_has_cursor": False,
            },
            "seed_username": "natgeo",
            "seed_user_id": "787132",
        }
        if seed
        else None,
        response_interceptor=types.SimpleNamespace(
            latest_request_form=None, latest_request_headers=None
        ),
        _send_replay=_send_replay,
    ), sent


def _enrich(session, handle, uid):
    return asyncio.run(BrowserSession._enrich_profile(session, handle, uid))


def test_enrich_swaps_the_numeric_id_and_returns_the_rich_record():
    reply = json.dumps({"data": {"user": {"username": "nasa", "follower_count": 104409347}}})
    session, sent = _stub_session(reply)
    rich = _enrich(session, "nasa", "528817151")
    assert rich["follower_count"] == 104409347
    # The query is keyed by the id, so that is what must have been re-pointed.
    assert sent["template"]["variables"]["id"] == "528817151"


def test_enrich_rejects_a_record_for_a_different_handle():
    # Never return another account's data under this handle.
    reply = json.dumps({"data": {"user": {"username": "bbcstrictly", "follower_count": 1}}})
    session, _ = _stub_session(reply)
    assert _enrich(session, "bbc", "2244940797") is None


def test_enrich_is_a_noop_without_a_captured_template():
    session, _ = _stub_session("{}", seed=False)
    assert _enrich(session, "nasa", "528817151") is None


def test_enrich_is_a_noop_without_a_resolved_id():
    session, _ = _stub_session("{}")
    assert _enrich(session, "nasa", "") is None


def test_enrich_returns_none_when_the_reply_carries_no_user():
    session, _ = _stub_session(json.dumps({"data": {"viewer": {}}}))
    assert _enrich(session, "nasa", "528817151") is None
