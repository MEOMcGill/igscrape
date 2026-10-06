"""Login-state detection in BrowserSession.

The page is faked as a set of visible controls, so these run without a browser.
The case that matters is Instagram's email/SMS verification screen: no password
field, no "Log in" control, no Home control, and a pre-auth cookie jar. It must
not read as logged in anywhere.
"""

import asyncio

import pytest
from playwright.async_api import TimeoutError as PWTimeoutError

from igscrape.account import Account
from igscrape.browser_session import BrowserSession, has_session_cookies
from igscrape.exceptions import FailedLoginError

PRE_AUTH = ["csrftoken", "datr", "ig_did", "mid", "ps_l", "ps_n", "wd"]
SESSION = PRE_AUTH + ["sessionid", "ds_user_id"]

VERIFICATION = set()
LOGIN_FORM = {"password", "login_button"}
HOME = {"home"}


def _jar(names):
    return [{"name": n, "value": "x", "domain": ".instagram.com"} for n in names]


class FakeLocator:
    def __init__(self, visible: bool):
        self.visible = visible

    @property
    def first(self):
        return self

    def or_(self, other):
        return FakeLocator(self.visible or other.visible)

    async def count(self):
        return int(self.visible)

    async def is_visible(self):
        return self.visible

    async def wait_for(self, state="visible", timeout=None):
        if not self.visible:
            raise PWTimeoutError("not visible")


class FakePage:
    def __init__(self, screen: set[str], url="https://www.instagram.com/"):
        self.screen = screen
        self.url = url

    def locator(self, selector):
        is_password = selector in ('input[name="pass"]', 'input[type="password"]')
        return FakeLocator(is_password and "password" in self.screen)

    def get_by_label(self, label, exact=False):
        if label == "Home":
            return FakeLocator("home" in self.screen)
        if label in ("Log in", "Log In"):
            return FakeLocator("login_button" in self.screen)
        return FakeLocator(False)

    def get_by_role(self, role, name=None, exact=False):
        if (role, name) == ("img", "Home"):
            return FakeLocator("home" in self.screen)
        if (role, name) == ("textbox", "Password"):
            return FakeLocator("password" in self.screen)
        return FakeLocator(False)


class FakeContext:
    def __init__(self, cookies):
        self.cookies = cookies

    async def storage_state(self):
        return {"cookies": self.cookies, "origins": []}


class FakePool:
    def __init__(self):
        self.saved = None

    async def update_cookies(self, username, cookies):
        self.saved = cookies


def _session(screen, cookies, url="https://www.instagram.com/"):
    pool = FakePool()
    session = BrowserSession(Account(username="acct", password="pw"), pool)
    session.page = FakePage(screen, url)
    session._context = FakeContext(_jar(cookies))
    session.login_calls = 0

    async def fake_login():
        session.login_calls += 1
        return True

    session.login = fake_login
    return session, pool


def test_has_session_cookies():
    assert not has_session_cookies(_jar(PRE_AUTH))
    assert has_session_cookies(_jar(SESSION))
    # An expired/cleared cookie comes back with an empty value.
    cleared = _jar(PRE_AUTH) + [
        {"name": "sessionid", "value": ""},
        {"name": "ds_user_id", "value": "1"},
    ]
    assert not has_session_cookies(cleared)


def test_verification_screen_is_not_logged_in():
    session, _ = _session(VERIFICATION, PRE_AUTH)
    # The old negative check is fooled: no form, so "no need to log in"...
    assert asyncio.run(session._need_to_log_in()) is False
    # ...the positive check is not.
    assert asyncio.run(session._is_logged_in()) is False


def test_home_without_session_cookies_is_not_logged_in():
    session, _ = _session(HOME, PRE_AUTH)
    assert asyncio.run(session._is_logged_in()) is False


def test_session_cookies_without_home_is_not_logged_in():
    session, _ = _session(VERIFICATION, SESSION)
    assert asyncio.run(session._is_logged_in(timeout=0.01)) is False


def test_home_with_session_cookies_is_logged_in():
    session, _ = _session(HOME, SESSION)
    assert asyncio.run(session._is_logged_in()) is True


def test_manual_wait_does_not_return_on_verification_screen():
    session, _ = _session(VERIFICATION, PRE_AUTH)
    assert asyncio.run(session.wait_until_logged_in(timeout=0.03, poll=0.01)) is False


def test_manual_wait_returns_once_verification_completes():
    session, _ = _session(VERIFICATION, PRE_AUTH)

    async def main():
        async def finish_verification():
            await asyncio.sleep(0.02)
            session.page.screen = HOME
            session._context.cookies = _jar(SESSION)

        task = asyncio.create_task(finish_verification())
        ok = await session.wait_until_logged_in(timeout=1.0, poll=0.01)
        await task
        return ok

    assert asyncio.run(main()) is True


def test_save_cookies_refuses_pre_auth_jar():
    session, pool = _session(VERIFICATION, PRE_AUTH)
    with pytest.raises(FailedLoginError):
        asyncio.run(session.save_cookies())
    assert pool.saved is None


def test_save_cookies_writes_a_session_jar():
    session, pool = _session(HOME, SESSION)
    assert asyncio.run(session.save_cookies()) == len(SESSION)
    assert pool.saved is not None


def test_ensure_logged_in_fails_on_verification_screen():
    session, _ = _session(VERIFICATION, PRE_AUTH)
    with pytest.raises(FailedLoginError, match="verification or challenge"):
        asyncio.run(session._ensure_logged_in())
    assert session.login_calls == 0


def test_ensure_logged_in_fills_the_form_when_shown():
    session, _ = _session(LOGIN_FORM, PRE_AUTH)
    asyncio.run(session._ensure_logged_in())
    assert session.login_calls == 1


def test_ensure_logged_in_skips_login_when_already_in():
    session, _ = _session(HOME, SESSION)
    asyncio.run(session._ensure_logged_in())
    assert session.login_calls == 0


def test_reauth_bounce_onto_verification_screen_raises(monkeypatch):
    session, _ = _session(
        VERIFICATION, PRE_AUTH, url="https://www.instagram.com/accounts/login/"
    )

    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(FailedLoginError):
        asyncio.run(session._reauth_if_bounced("https://www.instagram.com/explore/"))
    assert session.login_calls == 0
