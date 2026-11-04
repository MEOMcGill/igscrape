"""Scroll a pool account's home feed, on desktop (Camoufox) or on a farm phone."""

import asyncio
import os
from datetime import datetime

from igscrape.accounts_pool import AccountsPool
from igscrape.browser_session import BrowserSession
from igscrape.logger import set_log_level
from igscrape.parsers import post_flattener
from igscrape.utils import get_home_dir_path

set_log_level("INFO")

account_name: str = "your_pool_account"
max_posts: int = 100
# Set to a handset's adb serial to use the phone farm instead of a desktop browser.
phone_serial: str | None = None
phone_ssh_host: str | None = None  # SSH host the phones hang off, if not local


async def main():
    pool = AccountsPool(os.getenv("IGSCRAPE_DB", "accounts.db"))
    account = await pool.get_available(account_name)
    if account is None:
        raise SystemExit(f"{account_name} is not available")
    try:
        if phone_serial:
            from igscrape.phone_session import PhoneSession

            async with PhoneSession(
                phone_serial, account, pool, ssh_host=phone_ssh_host
            ) as phone:
                result = await phone.home_feed(max_posts=max_posts)
        else:
            async with BrowserSession(account, pool, headless=False) as session:
                result = await session.home_feed(max_posts=max_posts)
                await session.response_interceptor.drain_hooks()
    finally:
        await pool.release_account(account_name)

    result.posts = post_flattener(result.posts)
    print(f"{account_name}: {result.result} ({len(result.posts)} posts, {result.time_taken})")
    out = os.path.join(get_home_dir_path(), "data", "HomeFeed")
    os.makedirs(out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    result.save(os.path.join(out, f"{account_name}_HomeFeed_{stamp}.json"))


if __name__ == "__main__":
    asyncio.run(main())
