"""InstagramScraper.search rejects a keyword that is an @handle before any work is queued."""

import asyncio

import pytest

from igscrape.scraper import InstagramScraper


@pytest.mark.parametrize("keyword", ["@someaccount", " @someaccount"])
def test_search_rejects_a_handle(keyword):
    scraper = InstagramScraper.__new__(InstagramScraper)
    with pytest.raises(ValueError, match="starts with '@'"):
        asyncio.run(scraper.search(keyword=keyword))
