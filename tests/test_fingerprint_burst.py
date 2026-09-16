"""Unit tests for the fingerprint scroll burst's timeout.

The burst is decorative — a few real scrolls so the session still produces page
activity while collection runs on direct replay. But `page.mouse.wheel` takes no
timeout of its own, so a wedged renderer stopped the burst returning and the whole
collection parked there: no exception for the `except` to catch, and no progress
line, so the run looked alive while doing nothing. These pin down that a stalled
burst is abandoned and replay continues.
"""

import asyncio

from igscrape.browser_session import BrowserSession


class FakeMouse:
    def __init__(self, hang=False):
        self.hang = hang
        self.scrolls = 0

    async def wheel(self, dx, dy):
        self.scrolls += 1
        if self.hang:
            await asyncio.Event().wait()      # never returns, and never raises


class FakePage:
    def __init__(self, hang=False):
        self.mouse = FakeMouse(hang)


def _session(hang=False):
    session = BrowserSession.__new__(BrowserSession)   # bypass __init__'s browser setup
    session.page = FakePage(hang)
    return session


def test_burst_scrolls_when_the_page_responds():
    session = _session()
    asyncio.run(session._fingerprint_scroll_burst(n_min=2, n_max=2))
    assert session.page.mouse.scrolls == 2


def test_a_hung_scroll_is_abandoned_rather_than_waited_on():
    """The regression: one wedged wheel call parked the collection indefinitely."""
    session = _session(hang=True)

    async def body():
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            session._fingerprint_scroll_burst(n_min=1, n_max=1), timeout=5
        )
        return asyncio.get_running_loop().time() - started

    from igscrape import browser_session as bs
    original = bs.FINGERPRINT_BURST_TIMEOUT
    bs.FINGERPRINT_BURST_TIMEOUT = 0.05
    try:
        elapsed = asyncio.run(body())
    finally:
        bs.FINGERPRINT_BURST_TIMEOUT = original

    assert session.page.mouse.scrolls == 1, "the burst never reached the wheel"
    assert elapsed < 3, f"the burst was waited on for {elapsed:.2f}s instead of abandoned"


def test_a_raising_scroll_does_not_propagate():
    session = _session()

    async def boom(dx, dy):
        raise RuntimeError("target closed")

    session.page.mouse.wheel = boom
    asyncio.run(session._fingerprint_scroll_burst(n_min=1, n_max=1))
