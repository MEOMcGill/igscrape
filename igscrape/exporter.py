"""Flatten scraped Instagram posts into a tidy row-per-post representation.

Produces a consistent, analysis-friendly schema out of the ~90-key raw posts.
Writes CSV (stdlib) or Parquet (via polars, if installed).
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://www.instagram.com/"


def _iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _post_url(post: dict) -> str | None:
    code = post.get("code")
    if not code:
        return None
    if post.get("product_type") == "clips":
        return f"{BASE_URL}reel/{code}/"
    return f"{BASE_URL}p/{code}/"


def _audio_label(clips_metadata: dict | None) -> str | None:
    if not clips_metadata:
        return None
    orig = clips_metadata.get("original_sound_info") or {}
    if orig.get("original_audio_title"):
        return orig["original_audio_title"]
    music = clips_metadata.get("music_info") or {}
    asset = (music.get("music_asset_info") or {}) if isinstance(music, dict) else {}
    if asset.get("title"):
        artist = asset.get("display_artist") or ""
        return f"{asset['title']}{' — ' + artist if artist else ''}"
    return None


def _count_images_videos(post: dict) -> tuple[int, int]:
    num_images = 1 if post.get("image_versions2") else 0
    num_videos = 1 if post.get("video_dash_manifest") else 0
    for item in post.get("carousel_media") or []:
        if item.get("image_versions2"):
            num_images += 1
        if item.get("video_dash_manifest"):
            num_videos += 1
    return num_images, num_videos


def _semi_join(values) -> str | None:
    vals = [v for v in values if v]
    return ";".join(vals) if vals else None


def flatten_post(post: dict) -> dict:
    user = post.get("user") or {}
    owner = post.get("owner") or {}
    caption = post.get("caption") or {}
    location = post.get("location") or {}
    coauthors = post.get("coauthor_producers") or []
    tagged = [t.get("user", {}).get("username") for t in (post.get("usertags") or {}).get("in") or []]
    num_images, num_videos = _count_images_videos(post)

    media_type_map = {1: "photo", 2: "video", 8: "carousel"}
    mt = post.get("media_type")
    media_type_name = media_type_map.get(mt, str(mt) if mt is not None else None)

    return {
        # identity
        "id": post.get("id"),
        "pk": post.get("pk"),
        "code": post.get("code"),
        "url": _post_url(post),
        "typename": post.get("__typename"),
        "media_type": media_type_name,
        "product_type": post.get("product_type"),
        # timing
        "taken_at": post.get("taken_at"),
        "taken_at_iso": _iso(post.get("taken_at")),
        # content
        "caption_text": caption.get("text"),
        "caption_created_at_iso": _iso(caption.get("created_at")),
        "caption_is_edited": post.get("caption_is_edited"),
        "title": post.get("title"),
        "headline": post.get("headline"),
        "accessibility_caption": post.get("accessibility_caption"),
        # engagement
        "like_count": post.get("like_count"),
        "comment_count": post.get("comment_count"),
        "view_count": post.get("view_count"),
        "play_count": post.get("play_count"),
        "fb_like_count": post.get("fb_like_count"),
        "media_repost_count": post.get("media_repost_count"),
        "comments_disabled": post.get("comments_disabled"),
        "like_and_view_counts_disabled": post.get("like_and_view_counts_disabled"),
        # media shape
        "num_images": num_images,
        "num_videos": num_videos,
        "carousel_media_count": post.get("carousel_media_count"),
        "has_audio": post.get("has_audio"),
        "is_dash_eligible": post.get("is_dash_eligible"),
        "original_width": post.get("original_width"),
        "original_height": post.get("original_height"),
        # author
        "user_username": user.get("username"),
        "user_pk": user.get("pk"),
        "user_full_name": user.get("full_name"),
        "user_is_verified": user.get("is_verified"),
        "user_is_private": user.get("is_private"),
        "owner_id": owner.get("id") if owner.get("id") != user.get("pk") else None,
        "coauthor_usernames": _semi_join(c.get("username") for c in coauthors),
        "tagged_usernames": _semi_join(tagged),
        # context
        "location_name": location.get("name") if isinstance(location, dict) else None,
        "location_pk": location.get("pk") if isinstance(location, dict) else None,
        "location_lat": location.get("lat") if isinstance(location, dict) else None,
        "location_lng": location.get("lng") if isinstance(location, dict) else None,
        "audio_label": _audio_label(post.get("clips_metadata")),
        "is_paid_partnership": post.get("is_paid_partnership"),
    }


def flatten_posts(posts: list[dict]) -> list[dict]:
    return [flatten_post(p) for p in posts]


def write_csv(rows: list[dict], path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("No rows to write")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_parquet(rows: list[dict], path: str | Path):
    try:
        import polars as pl
    except ImportError as e:
        raise RuntimeError(
            "Parquet output requires polars. `pip install polars`"
        ) from e
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)


def export_posts(input_path: str | Path, output_path: str | Path) -> int:
    """Read a scraped JSON file, flatten posts, write to CSV or Parquet based on ext.

    Returns the number of rows written.
    """
    with open(input_path) as f:
        payload = json.load(f)
    posts = payload.get("posts", [])
    rows = flatten_posts(posts)

    out = Path(output_path)
    if out.suffix.lower() == ".parquet":
        write_parquet(rows, out)
    else:
        write_csv(rows, out)
    return len(rows)
