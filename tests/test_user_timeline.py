"""Live smoke test for the capture-replay UserTimeline path (handle: markjcarney).

NOT a pytest unit test — it hits real Instagram, needs a populated accounts DB,
and launches a browser. It defines no `test_*` functions, so `pytest tests/`
ignores it. Run it directly:

    python tests/test_user_timeline.py

What to watch for in the DEBUG logs (confirms the new API-replay path):
  - captured 'user_timeline' template (doc_id=..., has_cursor=True)
  - selected cursor strategy: graphql
  - @markjcarney: replay #N +X (total Y), next=yes   (browser sits still between
    occasional fingerprint scroll bursts)
Then a result of "scraped until user-specified starting date was reached"
(or "...first ever post...") with a deduped post count.
"""

import asyncio
import datetime
from pathlib import Path

from igscrape import InstagramScraper, ScrapingResult
from igscrape.logger import set_log_level

set_log_level("DEBUG")

# --- configuration -----------------------------------------------------------

HANDLE = "markjcarney"

# Collection window. A ~1-year span so the replay loop pages several times and
# the start-date stop condition is actually exercised.
START_DATE = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
END_DATE = datetime.date.today().strftime("%Y-%m-%d")

# Account pool igscrape logs in / rotates through.
DB_PATH = "/Users/mikad/MEOMcGill/igscrape/db/accounts.db"

# Keep the browser visible for the first run so login / any checkpoint is
# observable.
HEADLESS = False
MOBILE = False
MAX_BROWSER_SESSIONS = 1

# Output dir, anchored at the igscrape repo root (tests/ is one level down).
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "user_timeline"


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = OUTPUT_DIR / f"{HANDLE}_UserTimeline"
    jsonl_path = stem.with_suffix(".jsonl")
    # The streaming sink appends, so clear any prior run first.
    jsonl_path.unlink(missing_ok=True)

    async with InstagramScraper(
        db=DB_PATH,
        headless=HEADLESS,
        mobile=MOBILE,
        max_browser_sessions=MAX_BROWSER_SESSIONS,
    ) as scraper:
        result: ScrapingResult = await scraper.user_timeline(
            handle=HANDLE,
            start_date=START_DATE,
            end_date=END_DATE,
            jsonl_path=str(jsonl_path),  # crash-safe incremental .jsonl
        )

    print(
        f"\nHandle:     {HANDLE!r}\n"
        f"Window:     {START_DATE} .. {END_DATE}\n"
        f"Outcome:    {result.result}\n"
        f"Posts:      {len(result.posts)}\n"
        f"Time taken: {result.time_taken}"
    )

    # save_all rewrites the clean final .json + .jsonl from memory.
    json_path, jsonl_out = result.save_all(str(stem))
    print(f"Saved -> {json_path}\n      -> {jsonl_out}")


if __name__ == "__main__":
    asyncio.run(main())
