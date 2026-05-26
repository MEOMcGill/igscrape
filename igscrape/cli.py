"""Click-based CLI for igscrape.

Commands:
  Account management:  add, add-from-file, delete, list, info, stats,
                       activate, deactivate, unlock, release, set, fields,
                       reset-scrolls, set-cookies, export-cookies
  Asset downloading:   download-images, download-videos
  Scraping:            scrape user-timeline / user-profile / post / chaining
"""

import asyncio
import json
import os

import click
from tabulate import tabulate

from .accounts_pool import AccountsPool
from .downloaders import download_images_from_posts, download_videos_from_posts
from .exporter import export_posts
from .logger import set_log_level
from .utils import gather, get_home_dir_path


def get_default_db():
    return os.path.join(get_home_dir_path(), "db", "accounts.db")


def run_async(coro):
    return asyncio.run(coro)


@click.group()
@click.option("--db", default=None, help="Path to accounts database")
@click.pass_context
def cli(ctx, db):
    """igscrape — Instagram scraper CLI."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db or get_default_db()


# ============== Account management ==============


@cli.command()
@click.option("--username", required=True, help="Instagram handle (login identifier)")
@click.option("--password", required=True, help="Account password")
@click.option("--email", default=None, help="Optional email on file")
@click.option("--phone", default=None, help="Optional phone on file")
@click.option("--email-password", default=None, help="Email password")
@click.option("--proxy", default=None, help="Proxy server URL")
@click.option("--proxy-user", default=None, help="Proxy username")
@click.option("--proxy-pass", default=None, help="Proxy password")
@click.option("--cookies", default=None, help="Cookies JSON string or file path")
@click.option("--os", "os_type", default="macos", help="OS fingerprint (macos/windows/linux)")
@click.pass_context
def add(
    ctx,
    username,
    password,
    email,
    phone,
    email_password,
    proxy,
    proxy_user,
    proxy_pass,
    cookies,
    os_type,
):
    """Add a new Instagram account."""

    async def _add():
        pool = AccountsPool(ctx.obj["db"])
        cookie_data = cookies
        if cookies and os.path.exists(cookies):
            with open(cookies) as f:
                cookie_data = f.read()
        await pool.add_account(
            username=username,
            password=password,
            email=email,
            phone_number=phone,
            email_password=email_password,
            proxy_server=proxy,
            proxy_username=proxy_user,
            proxy_password=proxy_pass,
            cookies=cookie_data,
            os=os_type,
        )
        click.echo(f"Added account: {username}")

    run_async(_add())


@cli.command("add-from-file")
@click.argument("filepath")
@click.option(
    "--format",
    "fmt",
    default="username:password",
    help="Line format (e.g. 'username:password' or 'username:password:email:email_password')",
)
@click.pass_context
def add_from_file(ctx, filepath, fmt):
    """Add accounts from a file (one per line, colon-separated)."""
    if not os.path.exists(filepath):
        raise click.UsageError(f"File not found: {filepath}")

    fields = fmt.split(":")

    async def _add():
        pool = AccountsPool(ctx.obj["db"])
        added, failed = 0, 0
        with open(filepath) as f:
            for line_num, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) < len(fields):
                    click.echo(f"Line {line_num}: not enough fields, skipping")
                    failed += 1
                    continue
                kwargs: dict = {}
                for i, fld in enumerate(fields):
                    val = parts[i] if i < len(parts) else None
                    if fld == "username":
                        kwargs["username"] = val
                    elif fld == "password":
                        kwargs["password"] = val
                    elif fld == "email":
                        kwargs["email"] = val
                    elif fld == "email_password":
                        kwargs["email_password"] = val
                    elif fld == "phone":
                        kwargs["phone_number"] = val
                if "username" not in kwargs or "password" not in kwargs:
                    click.echo(f"Line {line_num}: missing username or password, skipping")
                    failed += 1
                    continue
                try:
                    await pool.add_account(**kwargs)
                    added += 1
                except Exception as e:
                    click.echo(f"Line {line_num}: {e}")
                    failed += 1
        click.echo(f"Added {added}, failed {failed}")

    run_async(_add())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "delete_all", is_flag=True, help="Delete all accounts")
@click.option("--inactive", is_flag=True, help="Delete only inactive accounts")
@click.pass_context
def delete(ctx, username, delete_all, inactive):
    """Delete account(s)."""
    if not username and not delete_all and not inactive:
        raise click.UsageError("Provide username(s) or use --all/--inactive")

    async def _delete():
        pool = AccountsPool(ctx.obj["db"])
        if delete_all:
            accounts = await pool.get(None)
            if not accounts:
                click.echo("No accounts")
                return
            if not click.confirm(f"Delete ALL {len(accounts)} accounts?"):
                return
            await pool.delete_account([a.username for a in accounts])
            click.echo(f"Deleted {len(accounts)}")
        elif inactive:
            accounts = await pool.get_inactive_accounts()
            if not accounts:
                click.echo("No inactive accounts")
                return
            if not click.confirm(f"Delete {len(accounts)} inactive accounts?"):
                return
            await pool.delete_account([a.username for a in accounts])
            click.echo(f"Deleted {len(accounts)}")
        else:
            await pool.delete_account(list(username))
            click.echo(f"Deleted {len(username)}")

    run_async(_delete())


@cli.command(name="list")
@click.option("--active", is_flag=True, help="Active accounts only")
@click.option("--inactive", is_flag=True, help="Inactive accounts only")
@click.option("--verbose", "-v", is_flag=True, help="Verbose columns")
@click.pass_context
def list_accounts(ctx, active, inactive, verbose):
    """List accounts."""

    async def _list():
        pool = AccountsPool(ctx.obj["db"])
        if active:
            accounts = await pool.get_active_accounts()
        elif inactive:
            accounts = await pool.get_inactive_accounts()
        else:
            accounts = await pool.get(None)
        if not accounts:
            click.echo("No accounts")
            return

        if verbose:
            headers = [
                "Username",
                "Active",
                "In Use",
                "Last Used",
                "Handles/Rest",
                "Scrolls (24h)",
                "Error",
                "Proxy",
            ]
            rows = [
                [
                    a.username,
                    "Y" if a.active else "N",
                    "Y" if a.in_use else "N",
                    str(a.last_used)[:19] if a.last_used else "-",
                    a.handles_scraped_since_rest,
                    a.scroll_count_overall_24h,
                    (a.error_msg[:30] + "...")
                    if a.error_msg and len(a.error_msg) > 30
                    else (a.error_msg or "-"),
                    a.proxy_server or "-",
                ]
                for a in accounts
            ]
        else:
            headers = ["Username", "Active", "In Use", "Last Used", "Scrolls (24h)"]
            rows = [
                [
                    a.username,
                    "Y" if a.active else "N",
                    "Y" if a.in_use else "N",
                    str(a.last_used)[:19] if a.last_used else "-",
                    a.scroll_count_overall_24h,
                ]
                for a in accounts
            ]
        click.echo(tabulate(rows, headers=headers, tablefmt="simple"))
        click.echo(f"\nTotal: {len(accounts)} accounts")

    run_async(_list())


@cli.command()
@click.argument("username")
@click.pass_context
def info(ctx, username):
    """Show account details."""

    async def _info():
        pool = AccountsPool(ctx.obj["db"])
        try:
            a = await pool.get(username)
        except ValueError:
            click.echo(f"Account not found: {username}")
            return
        click.echo(f"Account: {a.username}")
        click.echo(f"  Email:         {a.email or '-'}")
        click.echo(f"  Phone:         {a.phone_number or '-'}")
        click.echo(f"  Active:        {a.active}")
        click.echo(f"  In Use:        {a.in_use}")
        click.echo(f"  Last Used:     {a.last_used or '-'}")
        click.echo(f"  OS:            {a.os}")
        click.echo(f"  Proxy:         {a.proxy_server or '-'}")
        click.echo(f"  Cookies:       {len(a.cookies)} stored")
        click.echo(f"  Handles/Rest:  {a.handles_scraped_since_rest}")
        click.echo(f"  Scrolls (24h): {a.scroll_count_overall_24h}")
        click.echo(f"  Per-endpoint:  {a.scroll_count_per_endpoint_total or '-'}")
        click.echo(f"  Locks:         {a.locks or '-'}")
        click.echo(f"  Error:         {a.error_msg or '-'}")

    run_async(_info())


@cli.command()
@click.pass_context
def stats(ctx):
    """Pool statistics."""

    async def _stats():
        pool = AccountsPool(ctx.obj["db"])
        s = await pool.stats()
        if not s:
            click.echo("No stats")
            return
        click.echo("Account Pool Statistics")
        click.echo("-" * 30)
        for k in ("total", "active", "inactive", "in_use", "locked"):
            click.echo(f"  {k.capitalize():<10} {s.get(k, 0)}")

    run_async(_stats())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "set_all", is_flag=True)
@click.pass_context
def activate(ctx, username, set_all):
    """Activate account(s)."""
    if not username and not set_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _a():
        pool = AccountsPool(ctx.obj["db"])
        await pool.set_active(None if set_all else list(username), True)
        click.echo("Done")

    run_async(_a())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "set_all", is_flag=True)
@click.option("--error", default=None)
@click.pass_context
def deactivate(ctx, username, set_all, error):
    """Deactivate account(s)."""
    if not username and not set_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _d():
        pool = AccountsPool(ctx.obj["db"])
        await pool.set_active(None if set_all else list(username), False, error)
        click.echo("Done")

    run_async(_d())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "reset_all", is_flag=True)
@click.pass_context
def unlock(ctx, username, reset_all):
    """Remove locks from account(s)."""
    if not username and not reset_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _u():
        pool = AccountsPool(ctx.obj["db"])
        await pool.reset_locks(None if reset_all else list(username))
        click.echo("Done")

    run_async(_u())


@cli.command()
@click.argument("username", nargs=-1)
@click.option("--all", "release_all", is_flag=True)
@click.pass_context
def release(ctx, username, release_all):
    """Mark account(s) as not in use."""
    if not username and not release_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _r():
        pool = AccountsPool(ctx.obj["db"])
        await pool.release_account(None if release_all else list(username))
        click.echo("Done")

    run_async(_r())


@cli.command(name="set")
@click.argument("username")
@click.argument("field")
@click.argument("value")
@click.pass_context
def set_field(ctx, username, field, value):
    """Set a field for an account (use 'null' for None)."""

    async def _s():
        pool = AccountsPool(ctx.obj["db"])
        parsed = value
        if value.lower() in ("null", "none"):
            parsed = None
        elif value.lower() in ("true", "false"):
            parsed = value.lower() == "true"
        try:
            await pool.update_field(username, field, parsed)
            click.echo(f"Updated {username}: {field} = {parsed}")
        except ValueError as e:
            raise click.UsageError(str(e))

    run_async(_s())


@cli.command(name="fields")
def list_fields():
    """List updatable fields for `set`."""
    for f in sorted(AccountsPool._updatable_fields):
        click.echo(f"  {f}")


@cli.command("reset-scrolls")
@click.argument("username", nargs=-1)
@click.option("--all", "reset_all", is_flag=True)
@click.option("--endpoint", default=None)
@click.pass_context
def reset_scrolls(ctx, username, reset_all, endpoint):
    """Reset scroll counts."""
    if not username and not reset_all:
        raise click.UsageError("Provide username(s) or --all")

    async def _r():
        pool = AccountsPool(ctx.obj["db"])
        if reset_all:
            await pool.reset_scroll_counts(None, endpoint)
        else:
            for u in username:
                await pool.reset_scroll_counts(u, endpoint)
        click.echo("Done")

    run_async(_r())


@cli.command("set-cookies")
@click.argument("username")
@click.argument("cookies_file")
@click.pass_context
def set_cookies(ctx, username, cookies_file):
    """Set cookies from file."""
    if not os.path.exists(cookies_file):
        raise click.UsageError(f"File not found: {cookies_file}")

    async def _s():
        pool = AccountsPool(ctx.obj["db"])
        with open(cookies_file) as f:
            cookies = f.read()
        await pool.update_cookies(username, cookies)
        click.echo(f"Updated cookies for {username}")

    run_async(_s())


@cli.command("export-cookies")
@click.argument("username")
@click.argument("output_file")
@click.pass_context
def export_cookies(ctx, username, output_file):
    """Export cookies to file."""

    async def _e():
        pool = AccountsPool(ctx.obj["db"])
        try:
            a = await pool.get(username)
        except ValueError:
            click.echo(f"Account not found: {username}")
            return
        with open(output_file, "w") as f:
            json.dump(a.cookies, f, indent=2)
        click.echo(f"Exported {len(a.cookies)} cookies to {output_file}")

    run_async(_e())


# ============== Scraping ==============


@cli.group()
def scrape():
    """Run scraping jobs."""


def _load_result_posts_users(input_path: str) -> tuple[list[dict], list[dict]]:
    """Load posts (and optional users) from either:
    - a scraped JSON produced by ScrapingResult.save (has 'posts'/'users' keys), or
    - a JSONL of post dicts
    """
    with open(input_path) as f:
        first = f.read(1)
        f.seek(0)
        if first == "{":
            payload = json.load(f)
            return payload.get("posts", []), payload.get("users", [])
        posts = [json.loads(l) for l in f if l.strip()]
        return posts, []


def _run_scrape_common(
    ctx, endpoint: str, handles: tuple[str, ...], output_dir: str | None,
    max_sessions: int, headless: bool, mobile: bool, log_level: str,
    extra_query: dict | None = None,
):
    from .scraper import InstagramScraper

    set_log_level(log_level)
    if output_dir is None:
        output_dir = os.path.join(get_home_dir_path(), "data", endpoint)
    os.makedirs(output_dir, exist_ok=True)

    async def _scrape():
        pool = AccountsPool(ctx.obj["db"])
        async with InstagramScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            headless=headless,
            mobile=mobile,
        ) as scraper:
            calls = []
            for h in handles:
                if endpoint == "UserTimeline":
                    calls.append(
                        scraper.user_timeline(
                            handle=h,
                            start_date=extra_query["start_date"],
                            end_date=extra_query["end_date"],
                        )
                    )
                elif endpoint == "UserProfile":
                    calls.append(scraper.user_profile(handle=h))
                elif endpoint == "PostByShortcode":
                    calls.append(scraper.post_by_shortcode(shortcode=h))
                elif endpoint == "Chaining":
                    calls.append(scraper.chaining(handle=h))

            async for result in gather(calls):
                target = result.query.query.get("handle") or result.query.query.get(
                    "shortcode"
                )
                click.echo(
                    f"{target}: {result.result} "
                    f"({len(result.posts)} posts, {len(result.users)} users, "
                    f"{result.time_taken})"
                )
                suffix = (
                    f"_{extra_query['start_date']}_{extra_query['end_date']}"
                    if extra_query
                    else ""
                )
                fname = f"{target.replace('.', '_')}_{endpoint}{suffix}.json"
                result.save(os.path.join(output_dir, fname))

        click.echo(f"\nResults saved to: {output_dir}")

    run_async(_scrape())


@scrape.command("user-timeline")
@click.argument("handles", nargs=-1, required=True)
@click.option("--start-date", required=True, help="Earliest post date YYYY-MM-DD")
@click.option("--end-date", required=True, help="Latest post date YYYY-MM-DD")
@click.option("--output-dir", default=None)
@click.option("--max-sessions", default=2, type=int)
@click.option("--headless", is_flag=True)
@click.option("--mobile", is_flag=True)
@click.option("--log-level", default="INFO")
@click.pass_context
def scrape_user_timeline(
    ctx, handles, start_date, end_date, output_dir, max_sessions, headless, mobile, log_level
):
    """Scrape posts from a user's timeline."""
    _run_scrape_common(
        ctx,
        "UserTimeline",
        handles,
        output_dir,
        max_sessions,
        headless,
        mobile,
        log_level,
        extra_query={"start_date": start_date, "end_date": end_date},
    )


@scrape.command("user-profile")
@click.argument("handles", nargs=-1, required=True)
@click.option("--output-dir", default=None)
@click.option("--max-sessions", default=2, type=int)
@click.option("--headless", is_flag=True)
@click.option("--mobile", is_flag=True)
@click.option("--log-level", default="INFO")
@click.pass_context
def scrape_user_profile(ctx, handles, output_dir, max_sessions, headless, mobile, log_level):
    """Scrape user profile metadata."""
    _run_scrape_common(
        ctx,
        "UserProfile",
        handles,
        output_dir,
        max_sessions,
        headless,
        mobile,
        log_level,
    )


@scrape.command("post")
@click.argument("shortcodes", nargs=-1, required=True)
@click.option("--output-dir", default=None)
@click.option("--max-sessions", default=2, type=int)
@click.option("--headless", is_flag=True)
@click.option("--mobile", is_flag=True)
@click.option("--log-level", default="INFO")
@click.pass_context
def scrape_post(ctx, shortcodes, output_dir, max_sessions, headless, mobile, log_level):
    """Scrape individual posts by shortcode."""
    _run_scrape_common(
        ctx,
        "PostByShortcode",
        shortcodes,
        output_dir,
        max_sessions,
        headless,
        mobile,
        log_level,
    )


@scrape.command("chaining")
@click.argument("handles", nargs=-1, required=True)
@click.option("--output-dir", default=None)
@click.option("--max-sessions", default=2, type=int)
@click.option("--headless", is_flag=True)
@click.option("--mobile", is_flag=True)
@click.option("--log-level", default="INFO")
@click.pass_context
def scrape_chaining(ctx, handles, output_dir, max_sessions, headless, mobile, log_level):
    """Scrape 'suggested users' (chaining) for handles."""
    _run_scrape_common(
        ctx,
        "Chaining",
        handles,
        output_dir,
        max_sessions,
        headless,
        mobile,
        log_level,
    )


# ============== Asset downloading ==============


@cli.command("download-images")
@click.argument("input_file")
@click.option("--out-dir", required=True, help="Directory to save images")
@click.option("--include-profile-pics", is_flag=True, help="Also download profile pics")
@click.option("--concurrency", default=4, type=int)
def download_images(input_file, out_dir, include_profile_pics, concurrency):
    """Download images from a scraped JSON/JSONL file."""
    if not os.path.exists(input_file):
        raise click.UsageError(f"File not found: {input_file}")

    posts, users = _load_result_posts_users(input_file)

    async def _run():
        paths = await download_images_from_posts(
            posts,
            out_dir,
            include_profile_pics=include_profile_pics,
            users=users,
            concurrency=concurrency,
        )
        click.echo(f"Downloaded {len(paths)} images to {out_dir}")

    run_async(_run())


@cli.command("download-videos")
@click.argument("input_file")
@click.option("--out-dir", required=True, help="Directory to save videos")
@click.option("--concurrency", default=2, type=int)
@click.option(
    "--merge",
    is_flag=True,
    help="ffmpeg-mux video+audio into a single {post_id}.mp4 (requires ffmpeg on PATH)",
)
@click.option(
    "--keep-streams",
    is_flag=True,
    help="With --merge, keep the raw _video.mp4 / _audio.mp4 files too",
)
def download_videos(input_file, out_dir, concurrency, merge, keep_streams):
    """Download videos from a scraped JSON/JSONL file.

    By default downloads the highest-bitrate video stream + its audio track
    for each post (two files per video). Use --merge to mux them into a
    single playable .mp4 via ffmpeg.
    """
    if not os.path.exists(input_file):
        raise click.UsageError(f"File not found: {input_file}")

    posts, _ = _load_result_posts_users(input_file)

    async def _run():
        paths = await download_videos_from_posts(
            posts,
            out_dir,
            concurrency=concurrency,
            merge=merge,
            keep_streams=keep_streams,
        )
        click.echo(f"Downloaded {len(paths)} files to {out_dir}")

    run_async(_run())


@cli.command("export-posts")
@click.argument("input_file")
@click.option(
    "--output",
    "-o",
    required=True,
    help="Output file (.csv or .parquet — format inferred from extension)",
)
def export_posts_cmd(input_file, output):
    """Flatten a scraped JSON into a tidy CSV or Parquet of posts."""
    if not os.path.exists(input_file):
        raise click.UsageError(f"File not found: {input_file}")
    n = export_posts(input_file, output)
    click.echo(f"Wrote {n} rows to {output}")


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
