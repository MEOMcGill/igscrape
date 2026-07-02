"""Flatten scraped Instagram posts into a tidy row-per-post representation.

Produces a consistent, analysis-friendly schema out of the ~90-key raw posts.
Writes CSV (stdlib), JSONL (stdlib), or Parquet (via polars, if installed).
"""

import csv
import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://www.instagram.com/"

# Input file extensions the flattener understands (single file or a directory
# of these). Covers both the ScrapingResult .json save and the streamed .jsonl.
INPUT_EXTS = (".json", ".json.gz", ".jsonl", ".jsonl.gz", ".ndjson", ".ndjson.gz")
_FMT_SUFFIX = {"csv": ".csv", "jsonl": ".jsonl", "parquet": ".parquet"}


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

    Returns the number of rows written. Kept for backwards compatibility — new
    callers can use the more capable `flatten_paths()`.
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


# ==================== Batch flatten engine (fbscrape-style) ====================


def _open_maybe_gz(path: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def load_posts(path: str | Path) -> list[dict]:
    """Load raw post nodes from a scrape file, format inferred from extension.

    Accepts:
      - .json / .json.gz : a ScrapingResult dict ({"posts": [...]}), a bare list
        of posts, or a single post dict.
      - .jsonl / .ndjson (.gz) : one raw post node per line (the streamed sink
        format written by jsonl_store / ScrapingResult.save to .jsonl).
    """
    p = str(path)
    is_jsonl = any(p.endswith(e) for e in (".jsonl", ".jsonl.gz", ".ndjson", ".ndjson.gz"))
    with _open_maybe_gz(p) as f:
        if is_jsonl:
            out = []
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
            return out
        payload = json.load(f)
    if isinstance(payload, dict):
        if "posts" in payload:
            return payload.get("posts") or []
        return [payload]  # a single bare post dict
    if isinstance(payload, list):
        return payload
    return []


def write_jsonl(rows: list[dict], path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")


def _write_rows(rows: list[dict], path: str | Path, fmt: str):
    if fmt == "csv":
        write_csv(rows, path)
    elif fmt == "jsonl":
        write_jsonl(rows, path)
    elif fmt == "parquet":
        write_parquet(rows, path)
    else:
        raise ValueError(f"Unknown format: {fmt}")


def _input_stem(path: str) -> str:
    """File name minus a (possibly double) input extension: foo.json.gz -> foo."""
    name = Path(path).name
    for ext in sorted(INPUT_EXTS, key=len, reverse=True):
        if name.endswith(ext):
            return name[: -len(ext)]
    return Path(path).stem


def _looks_like_file(output: str) -> bool:
    return Path(output).suffix.lower() in (".csv", ".jsonl", ".parquet")


def flatten_paths(
    input_path: str | Path,
    output: str | Path | None = None,
    fmt: str = "csv",
    concat: bool = False,
) -> int:
    """Flatten one file OR a directory of scrape files into CSV / JSONL / Parquet.

    A backwards-compatible superset of `export_posts()`: adds directory input,
    `.jsonl`/`.gz` inputs, JSONL output, the `"all"` format, and `--concat`.
    Returns the total number of rows written (summed across formats for "all").

    Output resolution:
      - concat: merge all inputs into the single `output` file (per format).
      - single file + file-like `output`: write there (suffix adjusted per format).
      - otherwise: per-file outputs land in the `output` folder (or the input
        dir / the input file's parent when `output` is omitted).
    """
    input_path = str(input_path)
    formats = ["csv", "jsonl", "parquet"] if fmt == "all" else [fmt]
    is_dir = os.path.isdir(input_path)

    if is_dir:
        files = sorted(
            os.path.join(input_path, f)
            for f in os.listdir(input_path)
            if any(f.endswith(e) for e in INPUT_EXTS)
        )
        if not files:
            raise ValueError(f"No .json/.jsonl scrape files found in {input_path}")
    else:
        files = [input_path]

    # --- concat: everything into one output file per format ---
    if concat:
        if output is None or not _looks_like_file(str(output)):
            raise ValueError("--concat requires --output to be a file path")
        rows = flatten_posts([p for fp in files for p in load_posts(fp)])
        if not rows:
            raise ValueError("No posts found across inputs")
        for fm in formats:
            _write_rows(rows, Path(output).with_suffix(_FMT_SUFFIX[fm]), fm)
        return len(rows) * len(formats)

    # --- single file -> single named output ---
    if (
        len(files) == 1
        and output is not None
        and _looks_like_file(str(output))
        and not (os.path.isdir(str(output)) or str(output).endswith(os.sep))
    ):
        rows = flatten_posts(load_posts(files[0]))
        base = Path(output)
        for fm in formats:
            out = (
                base
                if (fmt != "all" and base.suffix.lower() == _FMT_SUFFIX[fm])
                else base.with_suffix(_FMT_SUFFIX[fm])
            )
            _write_rows(rows, out, fm)
        return len(rows) * len(formats)

    # --- per-file outputs into a folder ---
    if len(files) > 1 and output is not None and _looks_like_file(str(output)):
        raise ValueError(
            "Multiple input files: --output must be a folder (or use --concat)"
        )
    out_dir = (
        Path(output)
        if output is not None
        else (Path(input_path) if is_dir else Path(input_path).parent)
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for fp in files:
        rows = flatten_posts(load_posts(fp))
        if not rows:
            continue  # skip empty scrapes (e.g. a "no posts" result)
        for fm in formats:
            _write_rows(rows, out_dir / f"{_input_stem(fp)}{_FMT_SUFFIX[fm]}", fm)
            total += len(rows)
    return total
