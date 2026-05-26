"""Fetch a single post by shortcode."""

import asyncio

from igscrape import InstagramScraper
from igscrape.logger import set_log_level

set_log_level("INFO")

SHORTCODE = "CwV9sKXOk-A"  # any public instagram.com/p/<shortcode> works


async def main():
    async with InstagramScraper(headless=False, max_browser_sessions=1) as scraper:
        result = await scraper.post_by_shortcode(SHORTCODE)
        print(f"result={result.result}, posts={len(result.posts)}")
        if result.posts:
            p = result.posts[0]
            print(f"  id={p.get('id')} taken_at={p.get('taken_at')}")


if __name__ == "__main__":
    asyncio.run(main())
