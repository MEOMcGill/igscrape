"""Step-0 live spike: confirm Instagram's capture-replay contract.

Runs the NEW capture-replay path against a real handle and dumps what was
captured so we can verify, before merging, that:
  1. a replayable template is captured for the feed request,
  2. the selected cursor strategy + variable shape are correct,
  3. replay actually advances the cursor and pages past page 1.

This needs a populated accounts DB (same as the other examples). Run headful
the first time to watch it work:

    python examples/capture_replay_spike.py natgeo

The plan (docs/CAPTURE_REPLAY_PLAN.md §3) calls for recording the findings —
doc_id, variable keys, which token headers IG demands — back into that doc.
"""

import asyncio
import datetime
import sys

from igscrape import InstagramScraper
from igscrape.logger import set_log_level
from igscrape.pagination import select_cursor_strategy

set_log_level("DEBUG")


async def main(handle: str):
    start_date = (datetime.date.today() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
    end_date = datetime.date.today().strftime("%Y-%m-%d")

    async with InstagramScraper(headless=False, max_browser_sessions=1) as scraper:
        # Reach into the worker's session after the run to inspect the template.
        result = await scraper.user_timeline(handle=handle, start_date=start_date, end_date=end_date)

    print("\n==== capture-replay spike ====")
    print(f"handle:      {handle}")
    print(f"result code: {result.result}")
    print(f"posts:       {len(result.posts)}")
    print(f"window:      {start_date} .. {end_date}")

    # The template lives on the (now-closed) session's interceptor; re-running
    # with a custom on_new_posts hook is the supported way to observe streaming.
    # For the contract details, the DEBUG logs above print the captured doc_id
    # and the selected strategy. Print one post's keys as a shape sanity-check.
    if result.posts:
        sample = result.posts[0]
        print(f"sample post keys: {sorted(sample.keys())[:15]}")
        print(f"sample taken_at:  {sample.get('taken_at') or sample.get('media', {}).get('taken_at')}")

    # Demonstrate strategy selection on a synthetic GraphQL-shaped template so
    # the chosen branch is visible even if capture failed.
    demo = {"variables": {"after": None, "first": 12}, "form": {}, "headers": {}}
    print(f"strategy for graphql-shaped template: {select_cursor_strategy(demo).name}")
    print("==============================\n")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "natgeo"
    asyncio.run(main(target))
