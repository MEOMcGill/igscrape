"""Single-shot user profile lookup."""

import asyncio

from igscrape import InstagramScraper
from igscrape.logger import set_log_level

set_log_level("INFO")


async def main():
    async with InstagramScraper(headless=False, max_browser_sessions=1) as scraper:
        result = await scraper.user_profile("natgeo")
        print(f"result={result.result}, users={len(result.users)}")
        if result.users:
            u = result.users[0]
            # web_profile_info nests the follower count under edge_followed_by.
            followers = (u.get("edge_followed_by") or {}).get("count")
            print(
                f"  {u.get('username')}: "
                f"{u.get('full_name')} | {followers} followers"
            )


if __name__ == "__main__":
    asyncio.run(main())
