"""Live check: resolve profiles by replaying the profile-info GraphQL query.

Tests the hypothesis that the bare web_profile_info REST endpoint 429s, but a
*replayed* profile-info GraphQL request (reusing the seed navigation's signed
headers/tokens) is accepted like ordinary web-app traffic.

Flow: the FIRST handle seeds the query template via one profile navigation; every
later handle is resolved by replaying that template with the username swapped —
no page load. If the later handles come back "success" (not a RateLimitError),
the hypothesis holds.

    python examples/test_profile_graphql_replay.py natgeo nasa bbc

Needs a populated accounts DB, same as the other examples. Run headful the first
time to watch the single seed navigation.
"""

import asyncio
import os
import sys

from igscrape import InstagramScraper
from igscrape.cli import get_default_db
from igscrape.exceptions import RateLimitError
from igscrape.logger import set_log_level

set_log_level("DEBUG")  # shows "captured 'profile' template" + replay logs

# Accounts DB: IGSCRAPE_DB env var wins, else the standard <repo>/db/accounts.db.
DB = os.environ.get("IGSCRAPE_DB") or get_default_db()


async def main(handles: list[str]):
    print(f"using accounts db: {DB}")
    async with InstagramScraper(db=DB, headless=False, max_browser_sessions=1) as scraper:
        for i, handle in enumerate(handles):
            path = "seed (navigation)" if i == 0 else "replay (no page load)"
            try:
                result = await scraper.user_profile(handle)
            except RateLimitError as e:
                print(f"[{path}] @{handle}: RATE LIMITED — {e}")
                continue
            u = result.users[0] if result.users else {}
            print(
                f"[{path}] @{handle}: result={result.result} "
                f"id={u.get('id')} is_private={u.get('is_private')} "
                f"full_name={u.get('full_name')!r}"
            )


if __name__ == "__main__":
    targets = sys.argv[1:] or ["natgeo", "nasa", "bbc"]
    asyncio.run(main(targets))
