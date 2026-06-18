import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .models import JSONTrait
from .utils import utc


@dataclass
class Account(JSONTrait):
    """Instagram account record.

    Instagram accepts username, email, or phone as login identifier — we use
    `username` as the primary key because it is always set and is what
    gets filled into the login form.
    """

    username: str
    password: str
    email: str | None = None
    email_password: str | None = None
    phone_number: str | None = None
    active: bool = False
    locks: dict[str, datetime] = field(default_factory=dict)
    scroll_count_per_endpoint_total: dict[str, int] = field(default_factory=dict)
    cookies: list[dict] = field(default_factory=list)
    twofa_id: str | None = None
    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    fingerprint: str | None = None
    os: str = "macos"
    error_msg: str | None = None
    last_used: datetime | None = None
    in_use: bool = False
    handles_scraped_since_rest: int = 0
    scroll_count_overall_24h: int = 0

    @property
    def identifier(self) -> str:
        return self.username

    @property
    def display_name(self) -> str:
        return self.username

    @staticmethod
    def from_rs(rs: sqlite3.Row) -> "Account":
        doc = dict(rs)
        doc.pop("_tx", None)
        doc["locks"] = {k: utc.from_iso(v) for k, v in json.loads(doc["locks"]).items()}
        doc["scroll_count_per_endpoint_total"] = {
            k: v
            for k, v in json.loads(doc["scroll_count_per_endpoint_total"]).items()
            if isinstance(v, int)
        }
        doc["cookies"] = json.loads(doc["cookies"])
        doc["active"] = bool(doc["active"])
        doc["in_use"] = bool(doc["in_use"])
        doc["last_used"] = utc.from_iso(doc["last_used"]) if doc["last_used"] else None
        return Account(**doc)

    def to_rs(self) -> dict:
        rs = asdict(self)
        rs["locks"] = json.dumps(rs["locks"], default=lambda x: x.isoformat())
        rs["scroll_count_per_endpoint_total"] = json.dumps(rs["scroll_count_per_endpoint_total"])
        rs["cookies"] = json.dumps(rs["cookies"])
        rs["last_used"] = rs["last_used"].isoformat() if rs["last_used"] else None
        return rs
