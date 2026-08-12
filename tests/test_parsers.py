"""Unit tests for igscrape.parsers authorship / date filtering."""

from igscrape.parsers import (
    keep_record,
    owner_ids,
    post_authorship_filterer,
    post_date_filterer,
)


def _profile_node(taken_at=1700000000, owner="2931777286", user=None, coauthors=None):
    """A profile-posts media node. Instagram nulls `user` on this connection and
    carries the author as an `owner_id` object instead."""
    return {
        "__typename": "XDTMediaDict",
        "pk": "1",
        "taken_at": taken_at,
        "user": user,
        "owner_id": {"pk": owner, "id": owner},
        "coauthor_producers": coauthors or [],
    }


def test_keep_record_falls_back_to_owner_id_when_user_is_null():
    node = _profile_node()
    # No id supplied: nothing to match on, so it is dropped.
    assert keep_record(node, "charest_isabelle") is False
    # With the handle's numeric id resolved, it is kept — this is the fix for
    # the authorship filter silently discarding every post of every handle.
    assert keep_record(node, "charest_isabelle", {"2931777286"}) is True


def test_keep_record_id_fallback_rejects_a_different_owner():
    assert keep_record(_profile_node(owner="999"), "charest_isabelle", {"2931777286"}) is False


def test_keep_record_still_prefers_an_explicit_username():
    # An explicit, mismatched author loses even when the id matches — that is
    # what keeps foreign posts picked up from other XHRs out of the file.
    assert keep_record(
        _profile_node(user={"username": "SomeoneElse"}), "charest_isabelle", {"2931777286"}
    ) is False
    assert keep_record(
        _profile_node(user={"username": "Charest_Isabelle"}), "charest_isabelle"
    ) is True


def test_keep_record_coauthor_still_matches():
    node = _profile_node(user={"username": "other"}, coauthors=[{"username": "charest_isabelle"}])
    assert keep_record(node, "charest_isabelle") is True


def test_keep_record_survives_malformed_nodes():
    assert keep_record({}, "x") is False
    assert keep_record({"user": "not a dict"}, "x") is False
    assert keep_record({"coauthor_producers": [None]}, "x") is False


def test_authorship_filterer_uses_ids():
    records = [_profile_node(owner="2931777286"), _profile_node(owner="777")]
    assert len(post_authorship_filterer("charest_isabelle", records, {"2931777286"})) == 1


def test_owner_ids_accepts_object_and_scalar():
    assert owner_ids({"owner_id": {"pk": "1", "id": "1"}}) == {"1"}
    assert owner_ids({"owner_id": "42"}) == {"42"}
    assert owner_ids({}) == set()


def test_date_filter_keeps_inclusive_range():
    import datetime

    mid = int(datetime.datetime(2026, 6, 5, 12, 0).timestamp())
    kept = post_date_filterer([_profile_node(taken_at=mid)], "2026-06-01", "2026-06-09")
    assert len(kept) == 1
    assert post_date_filterer([_profile_node(taken_at=mid)], "2026-07-01", "2026-07-09") == []
