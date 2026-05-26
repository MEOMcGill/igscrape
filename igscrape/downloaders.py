"""Async image and video downloaders for scraped Instagram posts.

Ported from instagram-scraper/instagram_scraper/insta_image_consumer.py and
insta_video_consumer.py (the fetch/save logic, not the RabbitMQ/S3 plumbing).
"""

import asyncio
import os
import shutil
from pathlib import Path

import aiohttp

from .logger import logger
from .parsers import (
    extract_images_from_post,
    extract_profile_pics_from_post,
    extract_profile_pics_from_users,
    extract_video_audio_pairs_from_post,
)

IMAGE_EXTENSIONS = (".jpg", ".png", ".heic", ".webp")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.4 Safari/605.1.15"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate",
}


def _image_name(post_id: str, image_url: str) -> str:
    """Same naming convention as instagram-scraper: <post_id>_<slug>.png."""
    tail = image_url[image_url.rfind("/") + 1 :]
    for ext in IMAGE_EXTENSIONS:
        if ext in tail:
            stem = tail[: tail.find(ext)]
            return f"{post_id}_{stem}.png"
    return f"{post_id}_{tail}.png"


def _video_pair_names(post_id: str, idx: int) -> tuple[str, str, str]:
    """Return (video_name, audio_name, merged_name) for a pair."""
    suffix = f"_{idx}" if idx > 0 else ""
    return (
        f"{post_id}{suffix}_video.mp4",
        f"{post_id}{suffix}_audio.mp4",
        f"{post_id}{suffix}.mp4",
    )


async def _fetch_bytes(
    session: aiohttp.ClientSession,
    url: str,
    max_retries: int = 2,
) -> bytes | None:
    backoff = 5
    for attempt in range(max_retries + 1):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"fetch {url} returned status {resp.status}")
        except Exception as e:
            logger.warning(f"fetch {url} failed: {e}")
        if attempt < max_retries:
            await asyncio.sleep(backoff)
            backoff *= 2
    return None


def _save_bytes(data: bytes, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with open(path, "wb") as f:
        f.write(data)


async def download_images_from_posts(
    posts: list[dict],
    out_dir: str | Path,
    include_profile_pics: bool = False,
    users: list[dict] | None = None,
    concurrency: int = 4,
) -> list[Path]:
    """Download every image+carousel image referenced by `posts`.

    If include_profile_pics is True, also download profile pics from the post
    author, facepile, owner, and (optionally) the intercepted users list.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    image_meta: list[dict] = []
    for post in posts:
        image_meta += extract_images_from_post(post)

    profile_meta: list[dict] = []
    if include_profile_pics:
        for post in posts:
            profile_meta += extract_profile_pics_from_post(post)
        if users:
            profile_meta += extract_profile_pics_from_users(users)

    sem = asyncio.Semaphore(concurrency)
    saved: list[Path] = []

    async with aiohttp.ClientSession() as session:

        async def _one(meta: dict, name: str):
            async with sem:
                url = meta.get("image_url") or meta.get("profile_pic_url")
                if not url:
                    return
                data = await _fetch_bytes(session, url)
                if data is None:
                    return
                path = out / name
                _save_bytes(data, path)
                logger.info(f"saved {path}")
                saved.append(path)

        tasks = []
        for m in image_meta:
            tasks.append(_one(m, _image_name(m["post_id"], m["image_url"])))
        for m in profile_meta:
            tasks.append(_one(m, f"{m['handle']}.png"))

        if tasks:
            await asyncio.gather(*tasks)

    return saved


async def _ffmpeg_merge(video_path: Path, audio_path: Path, out_path: Path) -> bool:
    """Mux video + audio with stream copy. Returns True on success."""
    if shutil.which("ffmpeg") is None:
        logger.error("ffmpeg not found on PATH; cannot merge")
        return False
    if out_path.exists():
        out_path.unlink()
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(out_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(f"ffmpeg merge failed for {out_path.name}: {err.decode().strip()}")
        return False
    return True


async def download_videos_from_posts(
    posts: list[dict],
    out_dir: str | Path,
    concurrency: int = 2,
    merge: bool = False,
    keep_streams: bool = False,
) -> list[Path]:
    """Download the best-quality video + its audio track for each post.

    Instagram DASH manifests typically contain two near-duplicate video
    bitrates plus an audio track; we pick the highest-bandwidth video and
    the single audio representation per manifest.

    Args:
        posts: list of post dicts (e.g. ScrapingResult.posts)
        out_dir: directory to write files to
        concurrency: concurrent pair downloads
        merge: if True, ffmpeg-mux video+audio into a single {post_id}.mp4
        keep_streams: when merge=True, keep the raw _video.mp4 / _audio.mp4
            alongside the merged file (default: delete them after merge)

    Returns the list of saved file paths.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    pairs: list[dict] = []
    for post in posts:
        pairs += extract_video_audio_pairs_from_post(post)

    if merge and shutil.which("ffmpeg") is None:
        logger.error(
            "merge=True but ffmpeg is not on PATH; falling back to separate streams"
        )
        merge = False

    sem = asyncio.Semaphore(concurrency)
    saved: list[Path] = []

    async with aiohttp.ClientSession() as session:

        async def _one(pair: dict):
            async with sem:
                video_name, audio_name, merged_name = _video_pair_names(
                    pair["post_id"], pair["idx"]
                )
                video_path = out / video_name
                audio_path = out / audio_name
                merged_path = out / merged_name

                video_bytes = await _fetch_bytes(session, pair["video_url"])
                if video_bytes is None:
                    return
                _save_bytes(video_bytes, video_path)
                logger.info(f"saved {video_path} ({os.path.getsize(video_path)} bytes)")

                audio_bytes = None
                if pair.get("audio_url"):
                    audio_bytes = await _fetch_bytes(session, pair["audio_url"])
                    if audio_bytes is not None:
                        _save_bytes(audio_bytes, audio_path)
                        logger.info(
                            f"saved {audio_path} ({os.path.getsize(audio_path)} bytes)"
                        )

                if merge and audio_bytes is not None:
                    ok = await _ffmpeg_merge(video_path, audio_path, merged_path)
                    if ok:
                        saved.append(merged_path)
                        if not keep_streams:
                            video_path.unlink(missing_ok=True)
                            audio_path.unlink(missing_ok=True)
                        else:
                            saved.extend([video_path, audio_path])
                        return

                # Not merging (or merge failed / no audio): keep raw streams
                saved.append(video_path)
                if audio_bytes is not None:
                    saved.append(audio_path)

        if pairs:
            await asyncio.gather(*[_one(p) for p in pairs])

    return saved
