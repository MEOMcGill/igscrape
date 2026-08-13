# igscrape — async Instagram scraper for Python

Scrape Instagram user timelines, profiles, posts, and keyword-search results from
Python or the command line. `igscrape` drives real logged-in browser sessions
(Camoufox, a fingerprint-hardened Firefox), intercepts Instagram's own XHR
responses instead of parsing HTML, and rotates through a pool of accounts so long
collection runs survive rate limits, challenges, and crashes.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Built and maintained at the [Media Ecosystem Observatory](https://www.mediaecosystemobservatory.com/)
for social-media research. Sibling project: [pytok](https://github.com/networkdynamics/pytok),
the same approach for TikTok.

## Why igscrape

- **Raw API records, not scraped HTML.** Every post arrives as the JSON node
  Instagram's own front end received, so fields like `pk`, `taken_at`,
  `like_count`, `coauthor_producers`, and DASH video manifests are all intact.
- **Built for long runs.** A SQLite account pool with row locking, automatic
  rotation, per-endpoint scroll accounting, a retry taxonomy, and recovery from
  wedged browser sessions.
- **Date-bounded timeline collection.** Ask for a window and the scroll stops
  when it reaches it — no downloading a whole profile to keep one month.
- **Concurrent by default.** An asyncio `WorkerPool` of browser sessions
  processes many handles at once from one `async with` block.
- **Streaming output.** Write posts to JSONL as they arrive, or hand each batch
  to your own callback, so a run that dies at hour six keeps its first five.
- **Batteries for analysis.** Flatten to a 44-column CSV/Parquet, and download
  images or (ffmpeg-muxed) videos straight from a result file.

## What it collects

| Endpoint | Python | CLI |
|---|---|---|
| Posts from a handle, date-bounded | `scraper.user_timeline(handle, start_date, end_date)` | `igscrape scrape user-timeline <handle>...` |
| Profile metadata for a handle | `scraper.user_profile(handle)` | `igscrape scrape user-profile <handle>...` |
| A single post by shortcode | `scraper.post_by_shortcode(shortcode)` | `igscrape scrape post <shortcode>...` |
| Accounts recommended alongside a handle | `scraper.chaining(handle)` | `igscrape scrape chaining <handle>...` |
| Posts from keyword search | `scraper.search(keyword, max_posts=N)` | `igscrape scrape search <keyword>...` |

Every call returns a `ScrapingResult` with `.posts`, `.users`, `.result` (a
result code, see below), `.time_started`, and `.time_taken`.

## Install

```bash
pip install -e .
playwright install firefox
```

Optional: `ffmpeg` on `PATH` for merged video downloads, `polars` for Parquet
export (already a dependency).

## Quick start (Python)

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

`InstagramScraper(db="accounts.db", max_browser_sessions=5, handles_per_rest=100,
headless=False, mobile=False)` — `db` takes a path or an existing `AccountsPool`.

## Accounts

Before scraping you need at least one Instagram account in the pool:

```bash
igscrape add --username myuser --password mypass
igscrape list
igscrape activate myuser           # usually auto-activated after first login
```

Have a human at the screen for a first login — Instagram commonly asks for 2FA or
shows a challenge, which is why the browser is non-headless by default.

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

## Streaming results

`user_timeline` and `search` accept three optional sinks that fire as each page
of results arrives, rather than only at the end. They compose — any combination
runs together:

```python
await scraper.user_timeline(
    handle="natgeo",
    start_date="2024-01-01",
    end_date="2024-12-31",
    jsonl_path="data/natgeo.jsonl",       # append each raw post node as one JSON line
    on_new_posts=my_callback,             # called with each batch of new raw nodes
    download_videos=True,                 # fetch every mp4 as it is discovered
    video_dir="data/videos",
)
```

The returned `ScrapingResult` is unchanged by any of these. On the result itself,
`result.save(path)` writes JSON (or JSONL when the path ends in `.jsonl`/
`.ndjson`), and `result.save_all(base)` writes both `<base>.json` and
`<base>.jsonl`.

### Keyword search

`scrape search` / `scraper.search(keyword, max_posts=...)` collects posts from
Instagram's search SERP (`/explore/search/keyword/?q=<keyword>`). It scrolls
until `max_posts` posts are gathered or the results stop yielding anything new
(there is no date cutoff — search results aren't reliably chronological).
Results are deduplicated by `pk` across the overlapping scroll responses, and
the posts come back as standard media records, so `export-posts`,
`download-images`, and `download-videos` all work on the output unchanged.

### Video downloads

By default `download-videos` writes two files per post: `{post_id}_video.mp4` and `{post_id}_audio.mp4`. Instagram serves video and audio as separate DASH streams; the parser picks the highest-bitrate video `Representation` + the single audio `Representation` per manifest (dropping the duplicate lower-bitrate video rep).

With `--merge`, each pair is ffmpeg-muxed (stream-copied, no re-encode) into a single playable `{post_id}.mp4`, and the raw streams are deleted unless you pass `--keep-streams`. Silent reels (no audio track in the manifest) stay as `{post_id}_video.mp4`. Requires `ffmpeg` on `PATH` — `brew install ffmpeg`.

### Post export

`export-posts` flattens a scraped `ScrapingResult` JSON into a 44-column CSV or Parquet (one row per post). Format is inferred from the output extension. Parquet requires `polars` (`pip install polars`). Columns cover identity (`id`/`pk`/`code`/`url`/`media_type`/`product_type`), timing (`taken_at` + ISO), caption, engagement (`like_count`/`comment_count`/`view_count`/...), media shape (`num_images`/`num_videos`/`carousel_media_count`/`has_audio`/`original_width`/`original_height`), author (`user_*`), relationships (`owner_id`/`coauthor_usernames`/`tagged_usernames`), location, `audio_label`, and `is_paid_partnership`.

## Configuration

Environment variables:

- `IG_LOG_LEVEL` — `TRACE`/`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (default `INFO`)
- `IG_RAISE_WHEN_NO_ACCOUNT` — raise `NoAccountError` immediately instead of waiting

Account database location: the CLI defaults to `db/accounts.db` under the
repository root, while `InstagramScraper` defaults to `accounts.db` in the
working directory. Override with `igscrape --db /path/to/accounts.db ...` or by
passing `db=` to `InstagramScraper`.

## Result codes

From `igscrape.worker`:

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

Retryable results trigger up to 3 attempts with account rotation (accounts that hit `failed to load` are locked for 15 minutes, set by `RETRY_MINUTES`). After `HANDLES_PER_REST` (100) successful handles on one account, the worker rotates.

## Project layout

```
igscrape/
  __init__.py
  account.py              # Account dataclass (username-keyed)
  accounts_pool.py        # SQLite pool with locking
  browser_session.py      # Camoufox + IG login + per-endpoint scrapers
  cli.py                  # Click CLI
  db.py                   # aiosqlite wrapper + migrations
  downloaders.py          # image/video downloaders
  exceptions.py
  exporter.py             # CSV/Parquet post export
  logger.py               # loguru wrapper
  models.py               # Query, ScrapingResult
  pagination.py           # cursor strategies + replay-request construction
  parsers.py              # post_flattener, date/authorship filters, asset extractors
  response.py             # XHR interceptor
  scraper.py              # InstagramScraper high-level API
  stop_conditions.py      # scroll termination rules
  utils.py
  worker.py               # result-code taxonomy + rotation
  worker_pool.py          # asyncio producer-consumer pool
examples/                 # runnable scripts per endpoint
tests/                    # pytest suite (pytest -q)
```

## Responsible use

This is a research tool. Collecting data from Instagram with it is your
responsibility: check Instagram's terms, your institution's ethics requirements,
and applicable law before you run it. Prefer public accounts, collect the
minimum you need, and don't redistribute personal data you shouldn't.

## License

MIT — see [LICENSE](LICENSE).
