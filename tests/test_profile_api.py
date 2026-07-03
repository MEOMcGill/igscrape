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
