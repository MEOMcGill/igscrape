"""Unit tests for the web_profile_info availability classifier.

`_classify_profile` is a pure mapping from a web_profile_info `user` record to
an availability result code (or None when the timeline is scrapeable). It reads
no instance state, so we exercise it as an unbound method with a dummy self —
no browser required.
"""

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
