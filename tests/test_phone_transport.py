"""Unit tests for the phone backend's adb + CDP transport.

Everything here runs without a handset: the adb calls go through a fake shell and the CDP
event handling is driven by feeding in the protocol messages Chrome would send.
"""

import asyncio

import pytest

from igscrape.phone_transport import AsyncAdb, CapturedResponse, CdpSession


class _FakeAdb(AsyncAdb):
    """AsyncAdb with `_run` stubbed out, so command construction is still exercised."""

    def __init__(self, output: str = "", **kw):
        super().__init__(**kw)
        self.output = output
        self.commands: list[str] = []

    async def _run(self, cmd: str):
        self.commands.append(cmd)
        return 0, self.output, ""


def test_devices_parses_serial_state_pairs():
    adb = _FakeAdb(output=(
        "List of devices attached\n"
        "SERIAL0001\tdevice product:a16xcs model:SM_A166W\n"
        "SERIAL0002\tunauthorized\n"
        "\n"))
    pairs = asyncio.run(adb.devices())
    assert pairs == [("SERIAL0001", "device"), ("SERIAL0002", "unauthorized")]


def test_devices_ignores_the_daemon_notice():
    """`adb devices` prefixes '* daemon started *' lines when it spawns a server."""
    adb = _FakeAdb(output=("* daemon not running; starting now at tcp:5037\n"
                           "* daemon started successfully\n"
                           "List of devices attached\n"
                           "SERIAL0003\tdevice\n"))
    assert asyncio.run(adb.devices()) == [("SERIAL0003", "device")]


def test_shell_double_quotes_the_script_for_cmd_exe():
    """The script must arrive as one quoted argument or `;`-chains and `&` break on a
    Windows ssh host."""
    adb = _FakeAdb(adb="C:/platform-tools/adb.exe")
    asyncio.run(adb.shell("SERIAL", "svc power stayon true; input keyevent 26"))
    assert adb.commands == [
        'C:/platform-tools/adb.exe -s SERIAL shell "svc power stayon true; input keyevent 26"'
    ]


# ── cookie seeding ────────────────────────────────────────────────────────────


def _session_with_recorded_calls():
    session = CdpSession.__new__(CdpSession)
    recorded: list[tuple[str, dict]] = []

    async def fake_call(method, params=None, **kw):
        recorded.append((method, params or {}))
        return {}

    session.call = fake_call
    return session, recorded


def test_set_cookies_drops_playwright_session_expiry():
    """Playwright marks a session cookie `expires: -1`, which CDP would read as long
    expired — so it has to be sent with no expiry at all."""
    session, recorded = _session_with_recorded_calls()
    count = asyncio.run(CdpSession.set_cookies(session, [
        {"name": "sessionid", "value": "abc", "domain": ".instagram.com",
         "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"},
    ]))
    assert count == 1
    method, params = recorded[0]
    assert method == "Storage.setCookies"
    assert "expires" not in params["cookies"][0]
    assert params["cookies"][0]["sameSite"] == "None"


def test_set_cookies_keeps_a_real_expiry_and_defaults_the_domain():
    session, recorded = _session_with_recorded_calls()
    asyncio.run(CdpSession.set_cookies(session, [
        {"name": "ds_user_id", "value": "42", "expires": 1900000000.0},
    ]))
    cookie = recorded[0][1]["cookies"][0]
    assert cookie["expires"] == 1900000000.0
    assert cookie["domain"] == ".instagram.com"
    assert cookie["path"] == "/"


def test_set_cookies_skips_incomplete_entries_and_bad_samesite():
    session, recorded = _session_with_recorded_calls()
    count = asyncio.run(CdpSession.set_cookies(session, [
        {"value": "no-name"},
        {"name": "no-value"},
        # Playwright can emit sameSite values CDP rejects; those are dropped, not sent.
        {"name": "mid", "value": "m", "sameSite": "Unspecified"},
    ]))
    assert count == 1
    cookie = recorded[0][1]["cookies"][0]
    assert cookie["name"] == "mid"
    assert "sameSite" not in cookie


def test_set_cookies_makes_no_call_when_there_is_nothing_to_send():
    session, recorded = _session_with_recorded_calls()
    assert asyncio.run(CdpSession.set_cookies(session, [])) == 0
    assert recorded == []


def test_get_cookies_round_trips_into_storage_state_shape():
    session = CdpSession.__new__(CdpSession)

    async def fake_call(method, params=None, **kw):
        return {"cookies": [
            {"name": "sessionid", "value": "abc", "domain": ".instagram.com",
             "path": "/", "secure": True, "httpOnly": True, "sameSite": "None",
             "expires": -1},
        ]}

    session.call = fake_call
    cookies = asyncio.run(CdpSession.get_cookies(session))
    assert cookies == [{
        "name": "sessionid", "value": "abc", "domain": ".instagram.com", "path": "/",
        "secure": True, "httpOnly": True, "expires": -1, "sameSite": "None",
    }]


# ── CDP event handling ────────────────────────────────────────────────────────


def _capture_session(url_filter=("/api/graphql",)):
    session = CdpSession.__new__(CdpSession)
    session._filter = url_filter
    session.responses = []
    session._by_request = {}
    session._grabs = set()
    session.on_body = None
    return session


def _request_event(request_id, url, session_id="S1", post_data="doc_id=1"):
    return ("Network.requestWillBeSent",
            {"requestId": request_id,
             "request": {"url": url, "method": "POST", "postData": post_data,
                         "headers": {"x-fb-friendly-name": "PolarisProfilePostsQuery"}}},
            session_id)


def test_only_matching_urls_are_captured():
    session = _capture_session()
    session._on_event(*_request_event("1", "https://www.instagram.com/api/graphql"))
    session._on_event(*_request_event("2", "https://static.cdninstagram.com/rsrc.php/x.js"))
    assert [c.request_id for c in session.responses] == ["1"]
    captured = session.responses[0]
    assert captured.post_data == "doc_id=1"
    assert captured.headers["x-fb-friendly-name"] == "PolarisProfilePostsQuery"


def test_response_received_attaches_the_status():
    session = _capture_session()
    session._on_event(*_request_event("1", "https://www.instagram.com/api/graphql"))
    session._on_event("Network.responseReceived",
                      {"requestId": "1", "response": {"status": 429}}, "S1")
    assert session.responses[0].status == 429


def test_loading_finished_grabs_the_body_eagerly():
    """Android Chrome evicts bodies from the inspector cache within seconds, so the body
    must be fetched when the transfer finishes rather than read back later."""
    session = _capture_session()
    fetched: list[str] = []
    delivered: list[CapturedResponse] = []

    async def fake_call(method, params=None, **kw):
        fetched.append(params["requestId"])
        return {"body": '{"data":{"ok":true}}'}

    session.call = fake_call
    session.on_body = delivered.append

    async def drive():
        session._on_event(*_request_event("1", "https://www.instagram.com/api/graphql"))
        session._on_event("Network.loadingFinished", {"requestId": "1"}, "S1")
        await asyncio.gather(*session._grabs)

    asyncio.run(drive())
    assert fetched == ["1"]
    assert session.responses[0].body == '{"data":{"ok":true}}'
    assert delivered == [session.responses[0]]


def test_an_unavailable_body_becomes_empty_not_an_error():
    """An empty body is one of Instagram's ways of refusing a request, and an evicted one
    is routine — neither should propagate as a transport failure."""
    session = _capture_session()

    async def fake_call(method, params=None, **kw):
        raise RuntimeError("Request content was evicted from inspector cache")

    session.call = fake_call

    async def drive():
        session._on_event(*_request_event("1", "https://www.instagram.com/api/graphql"))
        session._on_event("Network.loadingFinished", {"requestId": "1"}, "S1")
        await asyncio.gather(*session._grabs)

    asyncio.run(drive())
    assert session.responses[0].body == ""


def test_closing_a_tab_drops_only_that_tabs_responses():
    """One instance scrapes many targets in a row, so a closed tab's traffic must not
    leak into the next target's results — nor take another tab's with it."""
    session = _capture_session()
    session._on_event(*_request_event("1", "https://www.instagram.com/api/graphql", "S1"))
    session._on_event(*_request_event("2", "https://www.instagram.com/api/graphql", "S2"))

    async def fake_call(method, params=None, **kw):
        return {}

    session.call = fake_call
    asyncio.run(CdpSession.close_tab(session, "target-1", "S1"))
    assert [c.request_id for c in session.responses] == ["2"]
    assert set(session._by_request) == {"2"}


if __name__ == "__main__":
    pytest.main([__file__])
