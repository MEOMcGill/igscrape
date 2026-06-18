"""Instagram post/user parsing helpers.

Parsing helpers for post and user payloads.
"""

from datetime import datetime

from bs4 import BeautifulSoup

from .logger import logger


def post_flattener(posts: list[dict]) -> list[dict]:
    """Resolve XDTFeedItem → its nested media; pass XDTMediaDict through.

    From post_scraper.py:1061-1073.
    """
    new_posts: list[dict] = []
    for post in posts:
        typename = post.get("__typename")
        if typename == "XDTMediaDict":
            new_posts.append(post)
        elif typename == "XDTFeedItem":
            media = post.get("media")
            if media is not None:
                new_posts.append(media)
            else:
                logger.debug("XDTFeedItem with media=None, skipping")
        else:
            logger.warning(f"Unknown post __typename: {typename}, skipping")
    return new_posts


def get_post_timestamp(post: dict) -> datetime | None:
    """Return the post's taken_at as a naive UTC datetime.

    Tries caption.created_at first, then taken_at, then media.taken_at.
    """
    try:
        ts = post["caption"]["created_at"]
    except (KeyError, TypeError):
        ts = post.get("taken_at") or post.get("media", {}).get("taken_at")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts))
    except Exception:
        return None


def post_date_filterer(
    posts: list[dict], start_date: str, end_date: str
) -> list[dict]:
    """Filter posts to [start_date, end_date] inclusive, using taken_at.

    From post_scraper.py:1076-1087.
    """
    start_dt = datetime.strptime(start_date + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S")
    filtered = []
    for p in posts:
        if "taken_at" not in p:
            continue
        dt = datetime.fromtimestamp(p["taken_at"])
        if start_dt <= dt <= end_dt:
            filtered.append(p)
    logger.debug(
        f"{len(posts)} posts before date filter, {len(filtered)} after "
        f"(start={start_date}, end={end_date})"
    )
    return filtered


def keep_record(record: dict, handle: str) -> bool:
    """Keep a post if its author or any coauthor matches handle.

    From post_scraper.py:1129-1147.
    """
    try:
        author = record["user"]["username"]
    except (KeyError, TypeError):
        return False

    if handle.lower() == author.lower():
        return True

    for coauthor in record.get("coauthor_producers") or []:
        if coauthor.get("username", "").lower() == handle.lower():
            return True
    return False


def post_authorship_filterer(handle: str, records: list[dict]) -> list[dict]:
    """Keep only posts authored or coauthored by `handle`."""
    filtered = [r for r in records if keep_record(r, handle)]
    logger.debug(
        f"{len(records)} records before authorship filter, {len(filtered)} after"
    )
    return filtered


def parse_video_urls(video_xml: str | None) -> list[str]:
    """Extract unique <BaseURL> entries from a DASH manifest.

    From post_scraper.py:1111-1117.
    """
    if not video_xml:
        return []
    soup = BeautifulSoup(video_xml, "xml")
    urls = [tag.contents[0] for tag in soup.find_all("BaseURL") if tag.contents]
    return list(set(urls))


def parse_video_manifest(video_xml: str | None) -> dict:
    """Parse a DASH manifest into best-quality video + audio URLs.

    Instagram manifests typically contain 2 video <Representation>s (two
    bitrates of the same content) and 1 audio <Representation>. Returns
    {'video': <highest-bw url>, 'audio': <audio url>}.
    """
    if not video_xml:
        return {}
    soup = BeautifulSoup(video_xml, "xml")

    video_candidates: list[tuple[int, str]] = []
    audio_url: str | None = None

    for repr_tag in soup.find_all("Representation"):
        mime = repr_tag.get("mimeType") or (
            repr_tag.parent.get("mimeType") if repr_tag.parent else ""
        )
        base = repr_tag.find("BaseURL")
        if not base or not base.contents:
            continue
        url = base.contents[0]
        try:
            bw = int(repr_tag.get("bandwidth") or 0)
        except ValueError:
            bw = 0

        if mime and mime.startswith("video/"):
            video_candidates.append((bw, url))
        elif mime and mime.startswith("audio/"):
            audio_url = url

    out: dict = {}
    if video_candidates:
        video_candidates.sort(reverse=True)
        out["video"] = video_candidates[0][1]
    if audio_url:
        out["audio"] = audio_url
    return out


def extract_video_audio_pairs_from_post(post: dict) -> list[dict]:
    """One dict per video manifest: {post_id, video_url, audio_url, idx}.

    Covers top-level and carousel-slide videos. `idx` lets the downloader
    write unique filenames when a carousel has multiple video slides.
    """
    post_id = post["id"]
    manifests: list[str] = []
    if post.get("video_dash_manifest"):
        manifests.append(post["video_dash_manifest"])
    for item in post.get("carousel_media") or []:
        if item.get("video_dash_manifest"):
            manifests.append(item["video_dash_manifest"])

    pairs: list[dict] = []
    for idx, xml in enumerate(manifests):
        parsed = parse_video_manifest(xml)
        video = parsed.get("video")
        if not video:
            continue
        pairs.append(
            {
                "post_id": post_id,
                "idx": idx,
                "video_url": video,
                "audio_url": parsed.get("audio"),
            }
        )
    return pairs


def extract_image_url(image_versions2: dict) -> str:
    """Return the highest-resolution image URL (first candidate)."""
    candidates = image_versions2["candidates"]
    if not isinstance(candidates, list) or len(candidates) == 0:
        raise ValueError("image_versions2.candidates must be a non-empty list")
    return candidates[0]["url"]


def extract_videos_from_post(post: dict) -> list[dict]:
    """From post_scraper.py:960-982."""
    post_id = post["id"]
    video_urls: list[str] = []

    if post.get("video_dash_manifest"):
        video_urls += parse_video_urls(post["video_dash_manifest"])

    for item in post.get("carousel_media") or []:
        if item.get("video_dash_manifest"):
            video_urls += parse_video_urls(item["video_dash_manifest"])

    return [{"post_id": post_id, "video_url": url} for url in video_urls]


def extract_images_from_post(post: dict) -> list[dict]:
    """From post_scraper.py:984-1005."""
    post_id = post["id"]
    image_urls: list[str] = []

    if post.get("image_versions2") is not None:
        try:
            image_urls.append(extract_image_url(post["image_versions2"]))
        except ValueError:
            pass

    for item in post.get("carousel_media") or []:
        iv2 = item.get("image_versions2")
        if iv2 is not None:
            try:
                image_urls.append(extract_image_url(iv2))
            except ValueError:
                continue

    return [{"post_id": post_id, "image_url": url} for url in image_urls]


def extract_profile_pics_from_post(post: dict) -> list[dict]:
    """From post_scraper.py:1007-1039, dedup by handle."""
    seen: set[str] = set()
    profile_pics: list[dict] = []

    def _add(handle: str | None, url: str | None):
        if not handle or not url or handle in seen:
            return
        seen.add(handle)
        profile_pics.append({"handle": handle, "profile_pic_url": url})

    user = post.get("user") or {}
    hd = (user.get("hd_profile_pic_url_info") or {}).get("url")
    _add(user.get("username"), hd)

    for liker in post.get("facepile_top_likers") or []:
        _add(
            liker.get("username") or liker.get("id"),
            liker.get("profile_pic_url"),
        )

    owner = post.get("owner")
    if owner:
        _add(owner.get("username"), owner.get("profile_pic_url"))

    return profile_pics


def extract_profile_pics_from_users(users: list[dict]) -> list[dict]:
    """From post_scraper.py:1044-1058, dedup by handle."""
    seen: set[str] = set()
    out: list[dict] = []
    for u in users:
        url = u.get("profile_pic_url")
        handle = u.get("username")
        if url and handle and handle not in seen:
            seen.add(handle)
            out.append({"handle": handle, "profile_pic_url": url})
    return out


def extract_assets_from_post(post: dict) -> tuple[list[dict], list[dict], list[dict]]:
    return (
        extract_videos_from_post(post),
        extract_images_from_post(post),
        extract_profile_pics_from_post(post),
    )


def extract_assets_from_posts(
    posts: list[dict], users: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    videos: list[dict] = []
    images: list[dict] = []
    profile_pics: list[dict] = []
    for post in posts:
        v, i, p = extract_assets_from_post(post)
        videos += v
        images += i
        profile_pics += p

    profile_pics += extract_profile_pics_from_users(users)

    seen: set[str] = set()
    deduped: list[dict] = []
    for p in profile_pics:
        if p["handle"] not in seen:
            seen.add(p["handle"])
            deduped.append(p)

    return videos, images, deduped
