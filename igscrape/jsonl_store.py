"""Opt-in streaming JSONL sink for scraped posts.

The default save path is unchanged — `ScrapingResult.save()` still writes one
pretty-printed JSON object per scrape. This adds an *alternative* that can run
alongside it: pass `jsonl_path` to `scraper.user_timeline()` / `scraper.search()`
and each raw post node is appended as one JSON object per line **as each
replayed page arrives** (via the on_new_posts streaming hook).

Why: flat memory on large scrapes, partial results survive a crash, and the file
is appendable/resumable. One object per line — read it back with a line loop or
`pandas.read_json(path, lines=True)`.
"""

import json
from pathlib import Path


class JsonlWriter:
    """Append raw post nodes to a .jsonl file, one JSON object per line.

    Opened in append mode per batch (no long-lived handle to manage), so it is
    safe across the scrape's early-return paths and leaves a valid file at every
    point. Note: re-running against an existing path appends — choose a fresh
    path per run if you don't want accumulation.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def append_batch(self, posts: list[dict]) -> None:
        if not posts:
            return
        with self.path.open("a", encoding="utf-8") as f:
            for post in posts:
                f.write(json.dumps(post, default=str, ensure_ascii=False) + "\n")
                self.count += 1
