"""Custom exceptions for igscrape.

These map loosely onto instagram-scraper's result-code taxonomy from
consume_post_scraper.py (retry_cases / success_cases / crash_cases).
"""


class InstagramScraperError(Exception):
    """Base exception for igscrape."""


class NoAccountError(InstagramScraperError):
    """No accounts available in pool."""


class FailedLoginError(InstagramScraperError):
    """Login attempt failed, or session was logged out mid-scrape."""


class AccountBannedError(InstagramScraperError):
    """Account has been banned or challenged."""


class RateLimitError(InstagramScraperError):
    """Scraper tripped an Instagram rate-limit / 'Failed to Load' gate."""


class TargetCrashedError(InstagramScraperError):
    """Browser tab crashed (maps to the 'target crashed' result code)."""
