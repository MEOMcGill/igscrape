"""Data models for Instagram scraping results."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar


@dataclass
class JSONTrait:
    """Mixin for JSON serialization of dataclasses."""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class Query:
    """A scraping task with endpoint-specific validation."""

    ENDPOINT_REQUIRED_FIELDS: ClassVar[dict[str, list[str]]] = {
        "UserTimeline": ["handle", "start_date", "end_date"],
        "UserProfile": ["handle"],
        "PostByShortcode": ["shortcode"],
        "Chaining": ["handle"],
        "Search": ["keyword"],
    }

    endpoint: str
    query: dict
    params: dict
    start_date: datetime | None = None
    end_date: datetime | None = None
    # Non-serializable per-call options (e.g. streaming callbacks). Excluded
    # from to_dict/to_json and from equality so the Query stays JSON-safe.
    runtime_options: dict | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        self._validate_endpoint()
        self._validate_query_fields()

    def _validate_endpoint(self):
        if self.endpoint not in self.ENDPOINT_REQUIRED_FIELDS:
            raise ValueError(
                f"Unsupported endpoint: '{self.endpoint}'. "
                f"Supported endpoints: {list(self.ENDPOINT_REQUIRED_FIELDS.keys())}"
            )

    def _validate_query_fields(self):
        required = self.ENDPOINT_REQUIRED_FIELDS[self.endpoint]
        missing = [f for f in required if f not in self.query]
        if missing:
            raise ValueError(
                f"Query for endpoint '{self.endpoint}' missing required fields: {missing}. "
                f"Required: {required}"
            )

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "query": self.query,
            "params": self.params,
            "start_date": str(self.start_date) if self.start_date else None,
            "end_date": str(self.end_date) if self.end_date else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class ScrapingResult:
    """Result of an Instagram scraping operation.

    The `result` string comes from the result-code taxonomy:
        - 'scraped until user-specified starting date was reached'
        - 'scraped until first ever post was reached'
        - 'no posts'
        - 'account is private'
        - 'profile is not available'
        - 'failed to load'
        - 'timeout error'
        - 'something went wrong - reload'
        - 'bad internet'
        - 'target crashed'
        - 'logged out while scraping'
        - 'success' (for single-shot endpoints like UserProfile / PostByShortcode / Chaining)
    """

    query: Query
    result: str
    posts: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)
    time_started: datetime | None = None
    time_taken: timedelta | None = None

    def to_dict(self) -> dict:
        return {
            "query": self.query.to_dict(),
            "result": self.result,
            "posts": self.posts,
            "users": self.users,
            "time_started": str(self.time_started) if self.time_started else None,
            "time_taken": str(self.time_taken) if self.time_taken else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def save(self, path: str, jsonl: bool | None = None):
        """Persist the result.

        Default: one pretty-printed JSON object (full result + metadata). If
        `path` ends in .jsonl/.ndjson (or `jsonl=True`), write the raw post nodes
        one JSON object per line instead — the same shape the streaming
        `jsonl_path` sink produces, but written in one shot from memory.
        """
        p = str(path)
        if jsonl is None:
            jsonl = p.endswith((".jsonl", ".ndjson"))
        with open(p, "w", encoding="utf-8") as f:
            if jsonl:
                for post in self.posts:
                    f.write(json.dumps(post, default=str, ensure_ascii=False) + "\n")
            else:
                json.dump(self.to_dict(), f, indent=2, default=str)

    def save_all(self, base: str) -> tuple[str, str]:
        """Write BOTH `<base>.json` (full result) and `<base>.jsonl` (one raw
        post per line). `base` may include or omit an extension. Returns the two
        paths written, as (json_path, jsonl_path)."""
        stem = str(Path(base).with_suffix(""))
        json_path, jsonl_path = stem + ".json", stem + ".jsonl"
        self.save(json_path)
        self.save(jsonl_path)
        return json_path, jsonl_path

    def add_post(self, post: dict):
        self.posts.append(post)
