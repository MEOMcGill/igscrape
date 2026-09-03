"""Scrape user timelines for multiple handles in parallel."""

import asyncio
import datetime
import os

from igscrape import InstagramScraper, ScrapingResult, gather
from igscrape.logger import set_log_level
from igscrape.utils import get_home_dir_path

set_log_level("INFO")

headless: bool = False
mobile: bool = False
max_browser_sessions: int = 2

handles: list[str] = ["natgeo", "nasa"]

start_date: str = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
end_date: str = datetime.date.today().strftime("%Y-%m-%d")


async def main():
    async with InstagramScraper(
        headless=headless, mobile=mobile, max_browser_sessions=max_browser_sessions
    ) as scraper:
        async for result in gather(
            scraper.user_timeline(handle=h, start_date=start_date, end_date=end_date)
            for h in handles
        ):
            data: ScrapingResult = result
            handle = data.query.query["handle"]
            print(
                f"{handle}: {data.result} "
                f"({len(data.posts)} posts, {len(data.users)} users, {data.time_taken})"
            )
            out = os.path.join(
                get_home_dir_path(),
                "data",
                f"{start_date}_{end_date}",
            )
            os.makedirs(out, exist_ok=True)
            data.save(
                os.path.join(
                    out,
                    f"{handle.replace('.', '_')}_UserTimeline_{start_date}_{end_date}.json",
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
