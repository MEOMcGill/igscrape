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

    def __init__(self, pages=(), states=(), user_id="42", menu=None):
        self.pages = list(pages)
        self.states = list(states)
        self.menu = {"x": 30, "y": 40} if menu is None else menu
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
        if expression == fg._UNFOLLOW_ITEM_JS:
            return self.menu
        self.requests.append(expression)
        return self.pages.pop(0)

    async def click(self, x, y):
        self.clicks.append((x, y))


def test_following_pages_until_no_cursor_and_dedupes():
    driver = FakeDriver([_page(["1", "2"], "c1"), _page(["2", "3"])])
    users = asyncio.run(fg.list_following(driver.evaluate_json, fallback_app_id="app"))
    assert [u["pk"] for u in users] == ["1", "2", "3"]
    assert '"c1"' in driver.requests[1] and "null" in driver.requests[0]


def test_following_stops_at_max_users():
    driver = FakeDriver([_page(["1", "2"], "c1"), _page(["3"])])
    users = asyncio.run(fg.list_following(driver.evaluate_json, fallback_app_id="app", max_users=2))
    assert len(users) == 2 and len(driver.requests) == 1


def test_following_needs_a_logged_in_page():
    with pytest.raises(FailedLoginError):
        asyncio.run(fg.list_following(FakeDriver(user_id="").evaluate_json, fallback_app_id="app"))


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


def _follow(driver, handle, **kw):
    return fg.follow(
        handle, goto=driver.goto, evaluate_json=driver.evaluate_json, click=driver.click, **kw
    )


def _state(state, **kw):
    return {"state": state, "x": 10, "y": 20, **kw}


def test_follow_clicks_then_confirms_on_reload():
    driver = FakeDriver(states=[_state("follow"), _state("following"), _state("following")])
    assert asyncio.run(_follow(driver, "@someone")) == "followed"
    assert driver.clicks == [(10, 20)]
    assert driver.visits == ["https://www.instagram.com/someone/"] * 2


def test_follow_private_account_is_requested():
    driver = FakeDriver(states=[_state("follow"), _state("requested"), _state("requested")])
    assert asyncio.run(_follow(driver, "someone")) == "requested"


def test_follow_that_reverts_on_reload_is_a_block():
    driver = FakeDriver(states=[_state("follow"), _state("following"), _state("follow")])
    with pytest.raises(RateLimitError):
        asyncio.run(_follow(driver, "someone"))


@pytest.mark.parametrize(
    "state, code",
    [
        (_state("following"), "already following"),
        (_state("requested"), "already requested"),
        ({"state": None, "unavailable": True}, "profile is not available"),
    ],
)
def test_follow_does_not_click_when_nothing_to_do(state, code):
    driver = FakeDriver(states=[state])
    assert asyncio.run(_follow(driver, "someone")) == code
    assert driver.clicks == []


def test_follow_without_a_button_gives_up():
    driver = FakeDriver(states=[{"state": None}])
    assert asyncio.run(_follow(driver, "someone", load_timeout=0)) == "no follow button"


@pytest.mark.parametrize(
    "states",
    [
        [{"state": None, "blocked": True}],
        [_state("follow"), _state("follow", blocked=True)],
    ],
)
def test_follow_action_block_raises(states):
    with pytest.raises(RateLimitError):
        asyncio.run(_follow(FakeDriver(states=states), "someone"))


def test_following_result_carries_users_and_account():
    driver = FakeDriver([_page(["1", "2"])])
    result = asyncio.run(
        fg.following_result(
            driver.evaluate_json, query={"account": "me"}, fallback_app_id="app"
        )
    )
    assert result.result == "success"
    assert [u["pk"] for u in result.users] == ["1", "2"]
    assert result.query.endpoint == "Following"
    assert result.query.query == {"account": "me", "max_users": -1}


def test_follow_result_carries_the_status():
    driver = FakeDriver(states=[_state("follow"), _state("following"), _state("following")])
    result = asyncio.run(
        fg.follow_result(
            "someone",
            query={"account": "me"},
            goto=driver.goto,
            evaluate_json=driver.evaluate_json,
            click=driver.click,
        )
    )
    assert result.result == "followed"
    assert result.query.query == {"account": "me", "handle": "someone"}


def _unfollow(driver, handle, **kw):
    return fg.unfollow(
        handle, goto=driver.goto, evaluate_json=driver.evaluate_json, click=driver.click, **kw
    )


def test_unfollow_clicks_button_then_menu_item_and_confirms_on_reload():
    driver = FakeDriver(states=[_state("following"), _state("follow"), _state("follow")])
    assert asyncio.run(_unfollow(driver, "@someone")) == "unfollowed"
    assert driver.clicks == [(10, 20), (30, 40)]
    assert driver.visits == ["https://www.instagram.com/someone/"] * 2


def test_unfollow_withdraws_a_pending_request():
    driver = FakeDriver(states=[_state("requested"), _state("follow"), _state("follow")])
    assert asyncio.run(_unfollow(driver, "someone")) == "request withdrawn"


def test_unfollow_that_reverts_on_reload_is_a_block():
    driver = FakeDriver(states=[_state("following"), _state("follow"), _state("following")])
    with pytest.raises(RateLimitError):
        asyncio.run(_unfollow(driver, "someone"))


@pytest.mark.parametrize(
    "state, code",
    [
        (_state("follow"), "not following"),
        ({"state": None, "unavailable": True}, "profile is not available"),
    ],
)
def test_unfollow_does_not_click_when_nothing_to_do(state, code):
    driver = FakeDriver(states=[state])
    assert asyncio.run(_unfollow(driver, "someone")) == code
    assert driver.clicks == []


def test_unfollow_without_menu_item_stops_after_one_click():
    driver = FakeDriver(states=[_state("following")], menu={})
    assert asyncio.run(_unfollow(driver, "someone")) == "no unfollow option"
    assert driver.clicks == [(10, 20)]


@pytest.mark.parametrize(
    "states",
    [
        [{"state": None, "blocked": True}],
        [_state("following"), _state("following", blocked=True)],
    ],
)
def test_unfollow_action_block_raises(states):
    with pytest.raises(RateLimitError):
        asyncio.run(_unfollow(FakeDriver(states=states), "someone"))


def test_unfollow_result_carries_the_status():
    driver = FakeDriver(states=[_state("following"), _state("follow"), _state("follow")])
    result = asyncio.run(
        fg.unfollow_result(
            "someone",
            query={"account": "me"},
            goto=driver.goto,
            evaluate_json=driver.evaluate_json,
            click=driver.click,
        )
    )
    assert result.result == "unfollowed"
    assert result.query.endpoint == "Unfollow"
    assert result.query.query == {"account": "me", "handle": "someone"}


def test_worker_routes_following_but_not_follow_or_unfollow():
    from igscrape.worker import Worker

    assert Worker.ENDPOINT_METHODS["Following"] == "following"
    assert "Follow" not in Worker.ENDPOINT_METHODS
    assert "Unfollow" not in Worker.ENDPOINT_METHODS
