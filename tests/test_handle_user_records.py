"""Unit tests for worker._handle_user_records.

Instagram's profile-posts connection nulls `user` on every post node and
carries the author only as `owner_id`. `result.users` still holds the handle's
own full profile object (captured from the profile page load), so this
resolves that record by id -- the id lets parsers.keep_record match a node to
the handle it was scraped for; the full record lets parsers.enrich_owner
restore real metadata onto the node instead of just proving an id.
"""

from igscrape.worker import _handle_user_records


def _profile(username, pk, **extra):
    return {"pk": pk, "id": pk, "username": username, **extra}


def test_matches_the_scraped_handle_case_insensitively():
    users = [_profile("Charest_Isabelle", "2931777286", full_name="Isabelle Charest")]
    by_id = _handle_user_records("charest_isabelle", users)
    assert by_id == {"2931777286": users[0]}


def test_ignores_profiles_belonging_to_someone_else():
    users = [_profile("other_account", "111")]
    assert _handle_user_records("charest_isabelle", users) == {}


def test_indexes_by_both_id_and_pk_when_they_differ():
    # Some payload shapes carry a numeric `id` distinct from `pk`; either one
    # can show up as a post node's owner_id, so both must resolve.
    user = _profile("charest_isabelle", "2931777286")
    user["id"] = "999999"
    by_id = _handle_user_records("charest_isabelle", [user])
    assert by_id == {"2931777286": user, "999999": user}


def test_tolerates_missing_or_malformed_entries():
    users = [None, "not a dict", {"username": "charest_isabelle"}]  # no id/pk at all
    assert _handle_user_records("charest_isabelle", users) == {}


def test_empty_users_list_returns_empty_mapping():
    assert _handle_user_records("charest_isabelle", None) == {}
    assert _handle_user_records("charest_isabelle", []) == {}
