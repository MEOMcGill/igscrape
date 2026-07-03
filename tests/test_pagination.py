"""Unit tests for igscrape.pagination — pure logic, no live Instagram.

These verify the capture-replay request construction and cursor handling
against synthetic IG-shaped payloads. They do NOT prove the real IG contract
(that's the Step-0 live spike in examples/capture_replay_spike.py); they lock in
the behavior the replay loop depends on.
"""

import json
from urllib.parse import parse_qs

from igscrape.pagination import (
    GraphQLCursorStrategy,
    V1MaxIdStrategy,
    build_replay_body,
    clean_headers,
    errors_indicate_rate_limit,
    find_page_info,
    merge_header_tokens,
    parse_form,
    parse_response,
    select_cursor_strategy,
    substitute_identity,
)


def _feed_payload(end_cursor="CUR2", has_next=True, edges=None):
    if edges is None:
        edges = [
            {"node": {"__typename": "XDTMediaDict", "pk": "1", "taken_at": 1700000000}},
        ]
    return {
        "xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": edges,
            "page_info": {"end_cursor": end_cursor, "has_next_page": has_next},
        }
    }


def _graphql_template():
    variables = {"after": None, "first": 12, "id": "999"}
    form = {
        "doc_id": "123456",
        "variables": json.dumps(variables),
        "fb_dtsg": "OLD_DTSG",
        "jazoest": "1",
        "lsd": "OLD_LSD",
        "__user": "0",
    }
    return {
        "url": "https://www.instagram.com/api/graphql/",
        "method": "POST",
        "headers": {"x-csrftoken": "OLD", "content-type": "application/x-www-form-urlencoded"},
        "form": form,
        "variables": variables,
        "doc_id": "123456",
    }


def test_parse_form_roundtrip():
    form = parse_form("a=1&b=hello%20world&empty=")
    assert form == {"a": "1", "b": "hello world", "empty": ""}


def test_clean_headers_drops_hop_by_hop_and_lowercases():
    out = clean_headers({"Host": "x", "Content-Length": "5", "X-Foo": "bar", ":method": "POST"})
    assert "host" not in out and "content-length" not in out and ":method" not in out
    assert out["x-foo"] == "bar"


def test_merge_header_tokens_refreshes_volatile_tokens():
    out = merge_header_tokens(
        {"x-csrftoken": "OLD", "x-keep": "1"},
        {"X-CSRFToken": "NEW", "x-ig-www-claim": "claim123"},
    )
    assert out["x-csrftoken"] == "NEW"
    assert out["x-ig-www-claim"] == "claim123"
    assert out["x-keep"] == "1"


def test_build_replay_body_graphql_sets_cursor_count_and_refreshes_tokens():
    template = _graphql_template()
    strategy = GraphQLCursorStrategy()
    body = build_replay_body(
        template, cursor="CUR_X", count=24, strategy=strategy,
        latest_form={"fb_dtsg": "NEW_DTSG", "lsd": "NEW_LSD"},
    )
    form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
    variables = json.loads(form["variables"])
    assert variables["after"] == "CUR_X"
    assert variables["first"] == 24          # count spliced into the key in use
    assert variables["id"] == "999"          # untouched
    assert form["doc_id"] == "123456"
    assert form["fb_dtsg"] == "NEW_DTSG"     # refreshed
    assert form["lsd"] == "NEW_LSD"


def test_graphql_strategy_count_into_nested_data_key():
    variables = {"after": None, "data": {"count": 12}}
    strat = GraphQLCursorStrategy()
    form = {}
    strat.apply_cursor(form, variables, "C", 30)
    assert variables["after"] == "C"
    assert variables["data"]["count"] == 30


def test_find_page_info_picks_shallowest():
    # A deep (nested sub-stream) cursor must NOT win over the page-level one.
    payload = _feed_payload(end_cursor="PAGE", has_next=True)
    payload["xdt_api__v1__feed__user_timeline_graphql_connection"]["edges"][0]["node"][
        "clips_stream"
    ] = {"page_info": {"end_cursor": "NESTED", "has_next_page": True}}
    end_cursor, has_next = find_page_info([payload])
    assert end_cursor == "PAGE"
    assert has_next is True


def test_find_page_info_none_when_absent():
    assert find_page_info([{"foo": {"bar": 1}}]) == (None, False)


def test_graphql_extract_reads_page_info():
    end_cursor, has_next = GraphQLCursorStrategy().extract([_feed_payload("Z", False)])
    assert end_cursor == "Z"
    assert has_next is False


def test_parse_response_json_and_errors():
    text = json.dumps({"data": _feed_payload(), "errors": [{"message": "side fragment"}]})
    payloads, errors = parse_response(text)
    assert len(payloads) == 1
    assert errors[0]["message"] == "side fragment"


def test_parse_response_jsonl():
    lines = "\n".join(
        [json.dumps({"data": _feed_payload("A")}), json.dumps({"data": _feed_payload("B")})]
    )
    payloads, errors = parse_response(lines)
    assert len(payloads) == 2
    assert errors == []


def test_parse_response_status_fail_becomes_error():
    payloads, errors = parse_response(json.dumps({"status": "fail", "message": "rate limited"}))
    assert payloads == []
    assert errors[0]["message"] == "rate limited"


def test_errors_indicate_rate_limit():
    assert errors_indicate_rate_limit([{"message": "Please wait a few minutes before you try again."}])
    assert not errors_indicate_rate_limit([{"message": "some other error"}])


def test_v1_strategy_max_id():
    strat = V1MaxIdStrategy()
    template = {"form": {"max_id": "10"}, "variables": {}}
    assert strat.initial_cursor(template) == "10"
    form = {"max_id": "10"}
    strat.apply_cursor(form, {}, "20", 12)
    assert form["max_id"] == "20"
    nxt, has_next = strat.extract([{"items": [], "next_max_id": "30", "more_available": True}])
    assert nxt == "30" and has_next is True


def test_select_cursor_strategy():
    assert select_cursor_strategy(_graphql_template()).name == "graphql"
    assert select_cursor_strategy({"form": {"max_id": "5"}, "variables": {}}).name == "v1_max_id"


def test_substitute_identity_username_keyed_resets_cursor_and_form():
    variables = {"after": "SEED_CURSOR", "username": "seed", "first": 12}
    template = {
        "variables": dict(variables),
        "form": {"doc_id": "1", "variables": json.dumps(variables)},
        "_has_cursor": True,
    }
    out = substitute_identity(template, "seed", "111", "target", "222")
    assert out["variables"]["username"] == "target"
    assert out["variables"]["after"] is None          # cursor reset to page 1
    assert out["_has_cursor"] is False
    # form's variables blob re-dumped to match the swapped identity
    form_vars = json.loads(out["form"]["variables"])
    assert form_vars["username"] == "target"
    assert form_vars["after"] is None
    # original template is left untouched (deep copy)
    assert template["variables"]["username"] == "seed"
    assert template["variables"]["after"] == "SEED_CURSOR"


def test_substitute_identity_id_keyed_nested():
    template = {
        "variables": {"after": None, "data": {"count": 12, "user_id": "111"}, "id": "111"},
        "form": {},
    }
    out = substitute_identity(template, "seed", "111", "target", "222")
    assert out["variables"]["id"] == "222"
    assert out["variables"]["data"]["user_id"] == "222"
    assert out["variables"]["data"]["count"] == 12     # non-identity ints untouched


def test_substitute_identity_drops_before_and_last():
    template = {"variables": {"after": "C", "before": "B", "last": 5, "id": "111"}, "form": {}}
    out = substitute_identity(template, "seed", "111", "t", "222")
    assert out["variables"]["after"] is None
    assert "before" not in out["variables"]
    assert "last" not in out["variables"]
