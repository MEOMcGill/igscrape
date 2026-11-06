"""Unit tests for follow_graph with a scripted page; no browser or handset."""

import asyncio
import json

import pytest

import igscrape.follow_graph as fg
from igscrape.exceptions import FailedLoginError, RateLimitError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(fg.asyncio, "sleep", instant)


def _page(users, next_max_id=None, status=200):
    body = {"users": [{"pk": u, "username": f"u{u}"} for u in users], "status": "ok"}
    if next_max_id:
        body["next_max_id"] = next_max_id
    return {"status": status, "body": json.dumps(body)}


class FakeDriver:
    """Answers the viewer probe, then serves following pages or profile states."""

    def __init__(self, pages=(), states=(), user_id="42"):
        self.pages = list(pages)
        self.states = list(states)
        self.user_id = user_id
        self.requests = []
        self.visits = []
        self.clicks = []

    async def goto(self, url):
        self.visits.append(url)

    async def evaluate_json(self, expression):
        if expression == fg._VIEWER_JS:
            return {"user_id": self.user_id, "app_id": ""}
        if expression == fg._PROFILE_STATE_JS:
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        self.requests.append(expression)
        return self.pages.pop(0)

    async def click(self, x, y):
        self.clicks.append((x, y))


def test_following_pages_until_no_cursor_and_dedupes():
    driver = FakeDriver([_page(["1", "2"], "c1"), _page(["2", "3"])])
    users = asyncio.run(fg.list_following(driver, fallback_app_id="app"))
    assert [u["pk"] for u in users] == ["1", "2", "3"]
    assert '"c1"' in driver.requests[1] and "null" in driver.requests[0]


def test_following_stops_at_max_users():
    driver = FakeDriver([_page(["1", "2"], "c1"), _page(["3"])])
    users = asyncio.run(fg.list_following(driver, fallback_app_id="app", max_users=2))
    assert len(users) == 2 and len(driver.requests) == 1


def test_following_needs_a_logged_in_page():
    with pytest.raises(FailedLoginError):
        asyncio.run(fg.list_following(FakeDriver(user_id=""), fallback_app_id="app"))


@pytest.mark.parametrize(
    "status, body, exc",
    [
        (429, "", RateLimitError),
        (200, json.dumps({"message": "Please wait a few minutes", "status": "fail"}),
         RateLimitError),
        (403, json.dumps({"message": "login_required", "status": "fail"}), FailedLoginError),
        (200, "<!DOCTYPE html><html>", RuntimeError),
    ],
)
def test_following_page_failures_raise(status, body, exc):
    with pytest.raises(exc):
        fg.parse_following_page(status, body)


def _state(state, **kw):
    return {"state": state, "x": 10, "y": 20, **kw}


def test_follow_clicks_then_confirms_on_reload():
    driver = FakeDriver(states=[_state("follow"), _state("following"), _state("following")])
    assert asyncio.run(fg.follow(driver, "@someone")) == "followed"
    assert driver.clicks == [(10, 20)]
    assert driver.visits == ["https://www.instagram.com/someone/"] * 2


def test_follow_private_account_is_requested():
    driver = FakeDriver(states=[_state("follow"), _state("requested"), _state("requested")])
    assert asyncio.run(fg.follow(driver, "someone")) == "requested"


def test_follow_that_reverts_on_reload_did_not_stick():
    driver = FakeDriver(states=[_state("follow"), _state("following"), _state("follow")])
    assert asyncio.run(fg.follow(driver, "someone")) == "follow did not stick"


@pytest.mark.parametrize(
    "state, code",
    [
        (_state("following"), "already following"),
        (_state("requested"), "already requested"),
        ({"state": None, "unavailable": True}, "profile is not available"),
        ({"state": None, "blocked": True}, "action blocked"),
    ],
)
def test_follow_does_not_click_when_nothing_to_do(state, code):
    driver = FakeDriver(states=[state])
    assert asyncio.run(fg.follow(driver, "someone")) == code
    assert driver.clicks == []


def test_follow_without_a_button_gives_up():
    driver = FakeDriver(states=[{"state": None}])
    assert asyncio.run(fg.follow(driver, "someone", load_timeout=0)) == "no follow button"


def test_follow_blocked_after_click():
    driver = FakeDriver(states=[_state("follow"), _state("follow", blocked=True)])
    assert asyncio.run(fg.follow(driver, "someone")) == "action blocked"
