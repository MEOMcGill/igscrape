"""Collect posts from Instagram keyword search for multiple queries in parallel."""

import asyncio
import os
import re

from igscrape import InstagramScraper, ScrapingResult, gather
from igscrape.logger import set_log_level
from igscrape.utils import get_home_dir_path

set_log_level("INFO")

headless: bool = False
mobile: bool = False
max_browser_sessions: int = 2

keywords: list[str] = ["coffee", "latte art"]
max_posts: int = 100


async def main():
    async with InstagramScraper(
        headless=headless, mobile=mobile, max_browser_sessions=max_browser_sessions
    ) as scraper:
        async for result in gather(
            scraper.search(keyword=k, max_posts=max_posts) for k in keywords
        ):
            data: ScrapingResult = result
            keyword = data.query.query["keyword"]
            print(
                f"{keyword!r}: {data.result} "
                f"({len(data.posts)} posts, {data.time_taken})"
            )
            out = os.path.join(get_home_dir_path(), "data", "Search")
            os.makedirs(out, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", keyword).strip("_")
            data.save(os.path.join(out, f"{safe}_Search.json"))


if __name__ == "__main__":
    asyncio.run(main())
