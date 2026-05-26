import asyncio
import json
import sqlite3
import uuid
from datetime import datetime

from .account import Account
from .db import execute, fetchall, fetchone
from .exceptions import NoAccountError
from .logger import logger
from .utils import get_env_bool, parse_cookies, utc


class AccountsPool:
    _order_by: str = "scroll_count_overall_24h ASC"

    def __init__(
        self,
        db_file: str = "accounts.db",
        _raise_when_no_account: bool = get_env_bool("IG_RAISE_WHEN_NO_ACCOUNT"),
    ):
        self._db_file = db_file
        self._raise_when_no_account = _raise_when_no_account

    @staticmethod
    def _id_cond(username: str) -> str:
        return f"username = '{username}'"

    @staticmethod
    def _ids_cond(usernames: list[str]) -> str:
        quoted = ",".join([f"'{x}'" for x in usernames])
        return f"username IN ({quoted})"

    async def add_account(
        self,
        username: str,
        password: str,
        email: str | None = None,
        email_password: str | None = None,
        phone_number: str | None = None,
        cookies: str | dict | list | None = None,
        proxy_server: str | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
        fingerprint: str | None = None,
        os: str = "macos",
        twofa_id: str | None = None,
    ):
        """Add an account, keyed on `username`."""
        if not username:
            raise ValueError("Must provide username")

        qs = f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
        if await fetchone(self._db_file, qs):
            logger.warning(f"Account {username} already exists")
            return

        if isinstance(cookies, str):
            cookies = parse_cookies(cookies)
        elif cookies is None:
            cookies = []

        account = Account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            phone_number=phone_number,
            active=bool(cookies),
            cookies=cookies,
            proxy_server=proxy_server,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
            fingerprint=fingerprint,
            os=os,
            twofa_id=twofa_id,
        )

        await self.save(account)
        logger.info(f"Account {username} added (active={account.active})")

    async def delete_account(self, username: str | list[str]):
        usernames = username if isinstance(username, list) else [username]
        usernames = list(set(usernames))
        if not usernames:
            return
        qs = f"DELETE FROM accounts WHERE {self._ids_cond(usernames)}"
        await execute(self._db_file, qs)
        logger.info(f"Deleted {len(usernames)} account(s)")

    async def get_inactive_accounts(self) -> list[Account]:
        qs = "SELECT * FROM accounts WHERE active = false"
        rs = await fetchall(self._db_file, qs)
        return [Account.from_rs(x) for x in rs]

    async def get_active_accounts(self) -> list[Account]:
        qs = "SELECT * FROM accounts WHERE active = true"
        rs = await fetchall(self._db_file, qs)
        return [Account.from_rs(x) for x in rs]

    async def get(self, username: str | list[str] | None) -> Account | list[Account]:
        if username is None:
            rs = await fetchall(self._db_file, "SELECT * FROM accounts")
            return [Account.from_rs(x) for x in rs]
        elif isinstance(username, list):
            usernames = list(set(username))
            qs = f"SELECT * FROM accounts WHERE {self._ids_cond(usernames)}"
            rs = await fetchall(self._db_file, qs)
            return [Account.from_rs(x) for x in rs]
        else:
            qs = f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
            rs = await fetchone(self._db_file, qs)
            if not rs:
                raise ValueError(f"Account {username} not found")
            return Account.from_rs(rs)

    async def save(self, account: Account):
        data = account.to_rs()
        cols = list(data.keys())
        username = account.username

        existing = await fetchone(
            self._db_file, f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
        )
        if existing:
            set_clause = ",".join([f"{x}=:{x}" for x in cols if x != "username"])
            qs = f"UPDATE accounts SET {set_clause} WHERE {self._id_cond(username)}"
        else:
            qs = (
                f"INSERT INTO accounts ({','.join(cols)}) "
                f"VALUES ({','.join([f':{x}' for x in cols])})"
            )
        await execute(self._db_file, qs, data)

    async def reset_locks(self, username: str | list[str] | None):
        if username is None:
            qs = "UPDATE accounts SET locks = json_object()"
        else:
            usernames = username if isinstance(username, list) else [username]
            qs = f"UPDATE accounts SET locks = json_object() WHERE {self._ids_cond(list(set(usernames)))}"
        await execute(self._db_file, qs)
        logger.info(f"Reset locks for {username if username else 'all accounts'}")

    async def set_active(
        self,
        username: str | list[str] | None,
        active: bool,
        error_message: str | None = None,
    ):
        if username is None:
            qs = "UPDATE accounts SET active = :active, error_msg = :error_msg"
            await execute(
                self._db_file, qs, {"active": active, "error_msg": error_message}
            )
        else:
            usernames = username if isinstance(username, list) else [username]
            qs = (
                f"UPDATE accounts SET active = :active, error_msg = :error_msg "
                f"WHERE {self._ids_cond(list(set(usernames)))}"
            )
            await execute(
                self._db_file, qs, {"active": active, "error_msg": error_message}
            )
        logger.info(
            f"Set active={active} for {username if username else 'all accounts'}"
        )

    async def lock_until(self, username: str | list[str] | None, until: str):
        """Lock account(s) until given SQLite datetime expression (e.g. "datetime('now', '+15 minutes')")."""
        usernames = username if isinstance(username, list) else [username] if username else []
        if not usernames:
            where = "TRUE"
        else:
            where = self._ids_cond(list(set(usernames)))
        qs = f"""
        UPDATE accounts SET
            locks = json_set(locks, '$.locked_until', {until}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {where}
        """
        await execute(self._db_file, qs)

    async def unlock(self, username: str | list[str] | None):
        usernames = username if isinstance(username, list) else [username] if username else []
        if not usernames:
            where = "TRUE"
        else:
            where = self._ids_cond(list(set(usernames)))
        qs = f"""
        UPDATE accounts SET
            locks = json_remove(locks, '$.locked_until'),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {where}
        """
        await execute(self._db_file, qs)

    async def _get_and_mark_in_use(self, subquery: str) -> Account | None:
        if int(sqlite3.sqlite_version_info[1]) >= 35:
            qs = f"""
            UPDATE accounts SET
                last_used = datetime({utc.ts()}, 'unixepoch'),
                in_use = true
            WHERE username = ({subquery})
            RETURNING *
            """
            rs = await fetchone(self._db_file, qs)
        else:
            tx = uuid.uuid4().hex
            qs = f"""
            UPDATE accounts SET
                last_used = datetime({utc.ts()}, 'unixepoch'),
                in_use = true,
                _tx = '{tx}'
            WHERE username = ({subquery})
            """
            await execute(self._db_file, qs)
            rs = await fetchone(
                self._db_file, f"SELECT * FROM accounts WHERE _tx = '{tx}'"
            )
        return Account.from_rs(rs) if rs else None

    async def get_available(self) -> Account | None:
        q = f"""
        SELECT username FROM accounts
        WHERE active = true
          AND in_use = false
          AND (
                locks IS NULL
                OR json_extract(locks, '$.locked_until') IS NULL
                OR json_extract(locks, '$.locked_until') < datetime('now')
          )
        ORDER BY {self._order_by}
        LIMIT 1
        """
        return await self._get_and_mark_in_use(q)

    async def get_available_or_wait(self) -> Account | None:
        msg_shown = False
        while True:
            account = await self.get_available()
            if account:
                if msg_shown:
                    logger.info(f"Continuing with account {account.username}")
                return account

            if self._raise_when_no_account or get_env_bool("IG_RAISE_WHEN_NO_ACCOUNT"):
                raise NoAccountError("No account available")

            if not msg_shown:
                nat = await self.next_available_at()
                if not nat:
                    logger.warning("No active accounts. Stopping...")
                    return None
                logger.info(f"No account available. Next available at {nat}")
                msg_shown = True

            await asyncio.sleep(5)

    async def next_available_at(self):
        qs = """
        SELECT json_extract(locks, '$.locked_until') AS locked_until
        FROM accounts
        WHERE active = true
          AND json_extract(locks, '$.locked_until') IS NOT NULL
          AND json_extract(locks, '$.locked_until') > datetime('now')
        ORDER BY locked_until ASC
        LIMIT 1
        """
        rs = await fetchone(self._db_file, qs)
        if rs and rs["locked_until"]:
            now, trg = utc.now(), utc.from_iso(rs["locked_until"])
            if trg < now:
                return "now"
            at_local = datetime.now() + (trg - now)
            return at_local.strftime("%H:%M:%S")
        return None

    async def release_account(self, username: str | list[str] | None):
        usernames = username if isinstance(username, list) else [username] if username else []
        if not usernames:
            where = "TRUE"
        else:
            where = self._ids_cond(list(set(usernames)))
        qs = f"""
        UPDATE accounts SET
            in_use = false,
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {where}
        """
        await execute(self._db_file, qs)

    async def mark_inactive(self, username: str, error_msg: str | None):
        qs = (
            f"UPDATE accounts SET active = false, error_msg = :error_msg, in_use = false "
            f"WHERE {self._id_cond(username)}"
        )
        await execute(self._db_file, qs, {"error_msg": error_msg})
        logger.warning(f"Marked account {username} inactive: {error_msg}")

    async def update_cookies(self, username: str, cookies: str | dict | list):
        if isinstance(cookies, str):
            cookies = parse_cookies(cookies)
        elif isinstance(cookies, dict):
            if "cookies" in cookies:
                cookies = cookies["cookies"]
            else:
                cookies = parse_cookies(json.dumps(cookies))
        cookies_json = json.dumps(cookies)
        qs = f"UPDATE accounts SET cookies = :cookies WHERE {self._id_cond(username)}"
        await execute(self._db_file, qs, {"cookies": cookies_json})
        logger.info(f"Updated cookies for {username} ({len(cookies)} cookies)")

    async def update_last_used(self, username: str):
        qs = (
            f"UPDATE accounts SET last_used = datetime({utc.ts()}, 'unixepoch') "
            f"WHERE {self._id_cond(username)}"
        )
        await execute(self._db_file, qs)

    async def update_scroll_count(self, username: str, endpoint: str, increment: int = 1):
        qs = f"""
        UPDATE accounts SET
            scroll_count_per_endpoint_total = json_set(
                scroll_count_per_endpoint_total,
                '$.{endpoint}',
                COALESCE(json_extract(scroll_count_per_endpoint_total, '$.{endpoint}'), 0) + :increment
            ),
            scroll_count_overall_24h = scroll_count_overall_24h + :increment,
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {self._id_cond(username)}
        """
        await execute(self._db_file, qs, {"increment": increment})

    async def get_scroll_count(self, username: str, endpoint: str | None = None) -> int:
        if endpoint:
            qs = (
                f"SELECT json_extract(scroll_count_per_endpoint_total, '$.{endpoint}') AS count "
                f"FROM accounts WHERE {self._id_cond(username)}"
            )
        else:
            qs = f"SELECT scroll_count_overall_24h AS count FROM accounts WHERE {self._id_cond(username)}"
        rs = await fetchone(self._db_file, qs)
        return (rs["count"] or 0) if rs else 0

    async def reset_scroll_counts(
        self, username: str | None = None, endpoint: str | None = None
    ):
        if endpoint:
            base = (
                "UPDATE accounts SET "
                f"scroll_count_per_endpoint_total = json_remove(scroll_count_per_endpoint_total, '$.{endpoint}')"
            )
            qs = base if username is None else f"{base} WHERE {self._id_cond(username)}"
        else:
            base = (
                "UPDATE accounts SET "
                "scroll_count_per_endpoint_total = '{}', scroll_count_overall_24h = 0"
            )
            qs = base if username is None else f"{base} WHERE {self._id_cond(username)}"
        await execute(self._db_file, qs)
        logger.info(
            f"Reset scroll counts for {username if username else 'all'}"
            + (f" endpoint={endpoint}" if endpoint else "")
        )

    async def increment_handles_scraped(self, username: str, increment: int = 1):
        qs = (
            f"UPDATE accounts SET handles_scraped_since_rest = handles_scraped_since_rest + :inc "
            f"WHERE {self._id_cond(username)}"
        )
        await execute(self._db_file, qs, {"inc": increment})

    async def reset_handles_scraped(self, username: str):
        qs = (
            f"UPDATE accounts SET handles_scraped_since_rest = 0 "
            f"WHERE {self._id_cond(username)}"
        )
        await execute(self._db_file, qs)

    async def get_handles_scraped(self, username: str) -> int:
        qs = (
            f"SELECT handles_scraped_since_rest AS c FROM accounts "
            f"WHERE {self._id_cond(username)}"
        )
        rs = await fetchone(self._db_file, qs)
        return rs["c"] if rs else 0

    _updatable_fields = {
        "password",
        "email",
        "email_password",
        "phone_number",
        "active",
        "proxy_server",
        "proxy_username",
        "proxy_password",
        "fingerprint",
        "os",
        "error_msg",
        "twofa_id",
    }

    async def update_field(self, username: str, field: str, value):
        if field not in self._updatable_fields:
            raise ValueError(
                f"Field '{field}' is not updatable. "
                f"Allowed: {', '.join(sorted(self._updatable_fields))}"
            )
        existing = await fetchone(
            self._db_file, f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
        )
        if not existing:
            raise ValueError(f"Account {username} not found")
        if field == "active" and isinstance(value, str):
            value = value.lower() in ("true", "1", "yes", "y")
        qs = f"UPDATE accounts SET {field} = :value WHERE {self._id_cond(username)}"
        await execute(self._db_file, qs, {"value": value})
        logger.info(f"Updated {field}={value} for {username}")

    async def stats(self) -> dict:
        config = [
            ("total", "SELECT COUNT(*) FROM accounts"),
            ("active", "SELECT COUNT(*) FROM accounts WHERE active = true"),
            ("inactive", "SELECT COUNT(*) FROM accounts WHERE active = false"),
            ("in_use", "SELECT COUNT(*) FROM accounts WHERE in_use = true"),
            (
                "locked",
                "SELECT COUNT(*) FROM accounts "
                "WHERE json_extract(locks, '$.locked_until') IS NOT NULL "
                "AND json_extract(locks, '$.locked_until') > datetime('now')",
            ),
        ]
        qs = f"SELECT {','.join([f'({q}) as {k}' for k, q in config])}"
        rs = await fetchone(self._db_file, qs)
        return dict(rs) if rs else {}
