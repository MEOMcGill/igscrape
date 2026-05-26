"""igscrape — standalone Instagram scraper.

Architecture mirrors dt-facebook-scraper (fbscrape). Scraping logic (XHR
interception targets, login flow, scroll termination conditions, result codes,
account rotation) is ported from instagram-scraper, which is production-tested.
"""

from .accounts_pool import AccountsPool
from .browser_session import BrowserSession
from .downloaders import download_images_from_posts, download_videos_from_posts
from .models import Query, ScrapingResult
from .response import InstagramResponseInterceptor
from .scraper import InstagramScraper
from .utils import extract_shortcode, gather, internet_good, is_post_url

__all__ = [
    "AccountsPool",
    "BrowserSession",
    "InstagramResponseInterceptor",
    "InstagramScraper",
    "Query",
    "ScrapingResult",
    "download_images_from_posts",
    "download_videos_from_posts",
    "extract_shortcode",
    "gather",
    "internet_good",
    "is_post_url",
]
