# igscrape

Standalone Instagram scraper.

Architecture mirrors [`dt-facebook-scraper`](../dt-facebook-scraper/) (`fbscrape`): async Python API, SQLite-backed account pool, asyncio WorkerPool of Camoufox browser sessions, Click CLI for account management. The actual Instagram scraping primitives — login flow, XHR interception targets, scroll termination conditions, result-code taxonomy, account rotation — are ported from the production-tested [`instagram-scraper`](../instagram-scraper/) repo so behavior is identical to what already works in prod.

## Install

```bash
pip install -e .
playwright install firefox
```

## Quick start (Python API)

```python
import asyncio
from igscrape import InstagramScraper, gather

async def main():
    async with InstagramScraper(headless=True, max_browser_sessions=2) as scraper:
        async for result in gather(
            scraper.user_timeline(handle=h, start_date="2024-06-01", end_date="2024-06-30")
            for h in ["natgeo", "nasa"]
        ):
            handle = result.query.query["handle"]
            print(f"{handle}: {result.result}, {len(result.posts)} posts")
            result.save(f"data/{handle}.json")

asyncio.run(main())
```

Other endpoints:

```python
await scraper.user_profile("natgeo")
await scraper.post_by_shortcode("CwV9sKXOk-A")
await scraper.chaining("natgeo")
await scraper.search("coffee", max_posts=100)   # keyword-search SERP posts
```

## Accounts

Before scraping you need at least one Instagram account in the pool:

```bash
igscrape add --username myuser --password mypass
igscrape list
igscrape activate myuser           # usually auto-activated after first login
```

Full CLI reference:

```
Account management:
  igscrape add --username U --password P [--email ...] [--proxy ...] [--cookies ...]
  igscrape add-from-file accounts.txt --format username:password
  igscrape delete <username>...     [--all] [--inactive]
  igscrape list                     [--active] [--inactive] [-v]
  igscrape info <username>
  igscrape stats
  igscrape activate <username>...   [--all]
  igscrape deactivate <username>... [--all] [--error MSG]
  igscrape unlock <username>...     [--all]
  igscrape release <username>...    [--all]
  igscrape set <username> <field> <value>
  igscrape fields
  igscrape reset-scrolls <username>... [--all] [--endpoint X]
  igscrape set-cookies <username> <file>
  igscrape export-cookies <username> <file>

Scraping:
  igscrape scrape user-timeline <handle>... --start-date YYYY-MM-DD --end-date YYYY-MM-DD
  igscrape scrape user-profile <handle>...
  igscrape scrape post <shortcode>...
  igscrape scrape chaining <handle>...
  igscrape scrape search <keyword>... [--max-posts N]

Asset downloading:
  igscrape download-images <scraped.json> --out-dir ./images [--include-profile-pics] [--concurrency N]
  igscrape download-videos <scraped.json> --out-dir ./videos [--merge] [--keep-streams] [--concurrency N]

Export:
  igscrape export-posts <scraped.json> -o posts.csv       # or .parquet
```

### Keyword search

`scrape search` / `scraper.search(keyword, max_posts=...)` collects posts from
Instagram's search SERP (`/explore/search/keyword/?q=<keyword>`). It scrolls
until `max_posts` posts are gathered or the results stop yielding anything new
(there is no date cutoff — search results aren't reliably chronological).
Results are deduplicated by `pk` across the overlapping scroll responses, and
the posts come back as standard media records, so `export-posts`,
`download-images`, and `download-videos` all work on the output unchanged.

### Video downloads

By default `download-videos` writes two files per post: `{post_id}_video.mp4` and `{post_id}_audio.mp4`. Instagram serves video and audio as separate DASH streams; the parser picks the highest-bitrate video `Representation` + the single audio `Representation` per manifest (dropping the duplicate lower-bitrate video rep that `instagram-scraper` used to save too).

With `--merge`, each pair is ffmpeg-muxed (stream-copied, no re-encode) into a single playable `{post_id}.mp4`, and the raw streams are deleted unless you pass `--keep-streams`. Silent reels (no audio track in the manifest) stay as `{post_id}_video.mp4`. Requires `ffmpeg` on `PATH` — `brew install ffmpeg`.

### Post export

`export-posts` flattens a scraped `ScrapingResult` JSON into a 44-column CSV or Parquet (one row per post). Format is inferred from the output extension. Parquet requires `polars` (`pip install polars`). Columns cover identity (`id`/`pk`/`code`/`url`/`media_type`/`product_type`), timing (`taken_at` + ISO), caption, engagement (`like_count`/`comment_count`/`view_count`/...), media shape (`num_images`/`num_videos`/`carousel_media_count`/`has_audio`/`original_width`/`original_height`), author (`user_*`), relationships (`owner_id`/`coauthor_usernames`/`tagged_usernames`), location, `audio_label`, and `is_paid_partnership`.

## Configuration

Environment variables:

- `IG_LOG_LEVEL` — `TRACE`/`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (default `INFO`)
- `IG_RAISE_WHEN_NO_ACCOUNT` — raise `NoAccountError` immediately instead of waiting

Default DB path: `~/db/accounts.db` relative to the repo root. Override with `igscrape --db /path/to/accounts.db ...` or pass `db=` to `InstagramScraper`.

## Result codes

From `igscrape.worker`, sourced verbatim from `instagram-scraper`:

| Code                                                          | Category |
|---------------------------------------------------------------|----------|
| `success`                                                     | success  |
| `scraped until user-specified starting date was reached`      | success  |
| `scraped until first ever post was reached`                   | success  |
| `no posts`                                                     | success  |
| `account is private`                                           | success  |
| `profile is not available`                                     | success  |
| `bad internet` / `timeout error` / `something went wrong - reload` / `failed to load` | retry |
| `target crashed` / `logged out while scraping`                 | crash    |

Retryable results trigger up to 3 attempts with account rotation (accounts that hit `failed to load` are locked for 15 minutes, per instagram-scraper's `RETRY_MINUTES`). After `HANDLES_PER_REST` (100) successful handles on one account, the worker rotates — same threshold instagram-scraper uses in `consume_post_scraper.py`.

## Project layout

```
igscrape/
  __init__.py
  account.py              # Account dataclass (username-keyed)
  accounts_pool.py        # SQLite pool with locking
  browser_session.py      # Camoufox + IG login + per-endpoint scrapers
  cli.py                  # Click CLI
  db.py                   # aiosqlite wrapper + migrations
  downloaders.py          # image/video downloaders (ported from consumers)
  exceptions.py
  logger.py               # loguru wrapper
  models.py               # Query, ScrapingResult
  parsers.py              # post_flattener, date/authorship filters, asset extractors
  response.py             # XHR interceptor
  scraper.py              # InstagramScraper high-level API
  utils.py
  worker.py               # handles result-code taxonomy + rotation
  worker_pool.py          # asyncio producer-consumer pool
examples/
  test_user_timeline.py
  test_user_profile.py
  test_post_by_shortcode.py
  test_downloaders.py
```
