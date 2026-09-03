"""Unit tests for `igscrape login`.

The command's job is to get a *named* account its first session, bypassing the
`active = true` gate and the `scroll_count_overall_24h ASC` ordering that
`get_available()` applies. BrowserSession is stubbed out, so these exercise the
wiring — account targeting, auto_login mode, cookie save, activation, the
in_use guard and exit codes — without a browser or Instagram.
"""

import pytest
from click.testing import CliRunner

from igscrape.account import Account
from igscrape.cli import cli
from igscrape.exceptions import FailedLoginError


class FakePool:
    """Stands in for AccountsPool: records set_active calls, serves accounts."""

    def __init__(self, accounts: dict[str, Account]):
        self.accounts = accounts
        self.set_active_calls: list[tuple] = []

    async def get(self, username):
        if username not in self.accounts:
            raise ValueError(f"Account {username} not found")
        return self.accounts[username]

    async def set_active(self, username, active, error_message=None):
        self.set_active_calls.append((username, active, error_message))


class FakeSession:
    """Stands in for BrowserSession. `behavior` decides what initialize() does."""

    instances: list["FakeSession"] = []

    def __init__(self, account, pool, headless=False, auto_login=True):
        self.account = account
        self.pool = pool
        self.headless = headless
        self.auto_login = auto_login
        self.closed = False
        self.saved = False
        self.waited_timeout = None
        FakeSession.instances.append(self)

    # behavior hooks, set per-test
    init_error: Exception | None = None
    logged_in_after_wait: bool = True

    async def initialize(self):
        if type(self).init_error is not None:
            raise type(self).init_error

    async def wait_until_logged_in(self, timeout=300.0, poll=5.0):
        self.waited_timeout = timeout
        return type(self).logged_in_after_wait

    async def save_cookies(self):
        self.saved = True
        return 7

    async def close(self):
        self.closed = True


def _account(username="acct", password="pw", in_use=False, active=False):
    return Account(username=username, password=password, active=active, in_use=in_use)


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSession.instances = []
    FakeSession.init_error = None
    FakeSession.logged_in_after_wait = True
    yield


@pytest.fixture
def patched(monkeypatch):
    """Patch BrowserSession + AccountsPool everywhere `login` resolves them."""
    import igscrape.browser_session as bs
    import igscrape.cli as cli_mod

    pools: list[FakePool] = []

    def make(accounts):
        pool = FakePool(accounts)
        pools.append(pool)
        monkeypatch.setattr(cli_mod, "AccountsPool", lambda *_a, **_k: pool)
        monkeypatch.setattr(bs, "BrowserSession", FakeSession)
        return pool

    return make


def _run(args):
    return CliRunner().invoke(cli, ["--db", "/tmp/ignored.db", "login", *args])


def test_automatic_login_saves_cookies_and_activates(patched):
    pool = patched({"acct": _account()})
    res = _run(["acct"])
    assert res.exit_code == 0, res.output
    assert "saved 7 cookies" in res.output
    # Activated with the error cleared, so get_available() will hand it out.
    assert pool.set_active_calls == [("acct", True, None)]
    session = FakeSession.instances[0]
    assert session.auto_login is True  # automatic => initialize() drives login
    assert session.closed is True


def test_manual_mode_does_not_auto_login_and_waits(patched):
    pool = patched({"acct": _account()})
    res = _run(["acct", "--mode", "manual", "--timeout", "42"])
    assert res.exit_code == 0, res.output
    session = FakeSession.instances[0]
    # The whole point of manual: don't race the human with the form filler.
    assert session.auto_login is False
    assert session.waited_timeout == 42.0
    assert session.saved is True
    assert pool.set_active_calls == [("acct", True, None)]


def test_manual_mode_timeout_saves_nothing(patched):
    pool = patched({"acct": _account()})
    FakeSession.logged_in_after_wait = False
    res = _run(["acct", "--mode", "manual", "--timeout", "1"])
    assert res.exit_code != 0
    assert "nothing saved" in res.output
    assert FakeSession.instances[0].saved is False
    assert pool.set_active_calls == []


def test_targets_the_named_account_not_the_pool_order(patched):
    # get_available() would prefer another account; login must use the named one.
    patched({"wanted": _account("wanted"), "other": _account("other")})
    res = _run(["wanted"])
    assert res.exit_code == 0, res.output
    assert FakeSession.instances[0].account.username == "wanted"


def test_in_use_account_is_refused_without_force(patched):
    pool = patched({"acct": _account(in_use=True)})
    res = _run(["acct"])
    assert res.exit_code != 0
    assert "in_use is set" in res.output
    # No browser opened at all — that's the point of the guard.
    assert FakeSession.instances == []
    assert pool.set_active_calls == []


def test_force_overrides_the_in_use_guard(patched):
    patched({"acct": _account(in_use=True)})
    res = _run(["acct", "--force"])
    assert res.exit_code == 0, res.output
    assert len(FakeSession.instances) == 1


def test_automatic_requires_a_stored_password(patched):
    patched({"acct": _account(password=None)})
    res = _run(["acct"])
    assert res.exit_code != 0
    assert "no stored password" in res.output
    assert FakeSession.instances == []


def test_manual_works_without_a_stored_password(patched):
    patched({"acct": _account(password=None)})
    res = _run(["acct", "--mode", "manual"])
    assert res.exit_code == 0, res.output


def test_missing_account_reports_and_exits_nonzero(patched):
    patched({})
    res = _run(["nope"])
    assert res.exit_code != 0
    assert "Account not found: nope" in res.output


def test_failed_login_is_reported_and_browser_closed(patched):
    pool = patched({"acct": _account()})
    FakeSession.init_error = FailedLoginError("checkpoint")
    res = _run(["acct"])
    assert res.exit_code != 0
    assert "login failed — checkpoint" in res.output
    # login() already recorded active=False; don't overwrite it here.
    assert pool.set_active_calls == []
    assert FakeSession.instances[0].closed is True


def test_one_failure_does_not_abort_the_others(patched):
    patched({"good": _account("good")})
    res = _run(["nope", "good"])
    # 'nope' fails, 'good' still runs, overall exit is non-zero.
    assert res.exit_code != 0
    assert "Account not found: nope" in res.output
    assert [s.account.username for s in FakeSession.instances] == ["good"]
    assert "1 succeeded, 1 failed" in res.output


def test_no_username_is_a_usage_error(patched):
    patched({})
    res = _run([])
    assert res.exit_code != 0
    assert "Provide at least one username" in res.output


def test_headless_flag_is_passed_through(patched):
    patched({"acct": _account()})
    assert _run(["acct"]).exit_code == 0
    assert FakeSession.instances[0].headless is False  # default: a human may be needed
    FakeSession.instances = []
    assert _run(["acct", "--headless"]).exit_code == 0
    assert FakeSession.instances[0].headless is True
