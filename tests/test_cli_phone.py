"""`--phone` / `--cdp-port` routing for the CLI commands that open one account's session.

PhoneSession and the pool are stubbed, so these check which session each command opens
and with what, without a handset or Instagram.
"""

import pytest
from click.testing import CliRunner

import igscrape.cli as cli_mod
import igscrape.phone_session as ps
from igscrape.cli import cli
from igscrape.models import Query, ScrapingResult


class FakePool:
    def __init__(self):
        self.released = []

    async def get_available(self, username):
        return type("A", (), {"username": username})()

    async def release_account(self, username):
        self.released.append(username)


class FakePhoneSession:
    opened: list[dict] = []

    def __init__(self, serial, account, pool, *, ssh_host=None, cdp_port=9222, **_):
        FakePhoneSession.opened.append(
            {"serial": serial, "account": account.username, "ssh_host": ssh_host,
             "cdp_port": cdp_port}
        )
        self.response_interceptor = type("I", (), {"drain_hooks": staticmethod(_noop)})()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def home_feed(self, max_posts=-1, max_scrolls=500):
        return ScrapingResult(query=Query(endpoint="HomeFeed", query={}, params={}),
                              result="success", posts=[])

    async def following(self, max_users=-1):
        return ScrapingResult(query=Query(endpoint="Following", query={}, params={}),
                              result="success", users=[{"username": "nasa"}])


async def _noop():
    return None


@pytest.fixture
def pool(monkeypatch):
    FakePhoneSession.opened = []
    pool = FakePool()
    monkeypatch.setattr(cli_mod, "AccountsPool", lambda *_a, **_k: pool)
    monkeypatch.setattr(ps, "PhoneSession", FakePhoneSession)
    return pool


def run(*args):
    return CliRunner().invoke(cli, ["--db", "/tmp/ignored.db", *args])


def test_home_feed_on_a_phone(pool, tmp_path):
    r = run("scrape", "home-feed", "dave", "--phone", "SER1", "--cdp-port", "9233",
            "--output-dir", str(tmp_path))
    assert r.exit_code == 0, r.output
    assert FakePhoneSession.opened == [
        {"serial": "SER1", "account": "dave", "ssh_host": None, "cdp_port": 9233}
    ]
    assert "dave: success (0 posts)" in r.output
    assert list(tmp_path.glob("dave_HomeFeed_*.json"))
    assert pool.released == ["dave"]


def test_following_cdp_port_defaults_to_9222(pool):
    r = run("following", "dave", "--phone", "SER1")
    assert r.exit_code == 0, r.output
    assert FakePhoneSession.opened[0]["cdp_port"] == 9222
    assert "dave follows 1 accounts" in r.output
