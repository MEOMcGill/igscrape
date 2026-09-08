"""Unit tests for the headful window size handed to Camoufox.

Camoufox sizes a headful window from the generated fingerprint, which can be
larger than the display and push the login form off-screen. get_window_size()
caps it; these cover the env override, monitor detection and the fallbacks.
"""

import pytest

from igscrape.utils import MAX_WINDOW_SIZE, get_window_size


class FakeMonitor:
    def __init__(self, width, height):
        self.width = width
        self.height = height


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    monkeypatch.delenv("IGSCRAPE_WINDOW_SIZE", raising=False)


def fake_monitors(monkeypatch, monitors):
    import screeninfo

    monkeypatch.setattr(screeninfo, "get_monitors", lambda: monitors)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("IGSCRAPE_WINDOW_SIZE", "1280x720")
    fake_monitors(monkeypatch, [FakeMonitor(3840, 2160)])
    assert get_window_size() == (1280, 720)


def test_unparseable_override_falls_back(monkeypatch):
    monkeypatch.setenv("IGSCRAPE_WINDOW_SIZE", "huge")
    fake_monitors(monkeypatch, [FakeMonitor(3840, 2160)])
    assert get_window_size() == MAX_WINDOW_SIZE


def test_large_monitor_capped(monkeypatch):
    fake_monitors(monkeypatch, [FakeMonitor(3840, 2160)])
    assert get_window_size() == MAX_WINDOW_SIZE


def test_small_monitor_shrinks_window(monkeypatch):
    fake_monitors(monkeypatch, [FakeMonitor(1024, 768)])
    w, h = get_window_size()
    assert (w, h) == (944, 648)
    assert w < 1024 and h < 768


def test_largest_monitor_used(monkeypatch):
    fake_monitors(monkeypatch, [FakeMonitor(800, 600), FakeMonitor(1024, 768)])
    assert get_window_size() == (944, 648)


def test_no_monitors_falls_back(monkeypatch):
    fake_monitors(monkeypatch, [])
    assert get_window_size() == MAX_WINDOW_SIZE


def test_screeninfo_failure_falls_back(monkeypatch):
    import screeninfo

    def boom():
        raise RuntimeError("no display")

    monkeypatch.setattr(screeninfo, "get_monitors", boom)
    assert get_window_size() == MAX_WINDOW_SIZE
