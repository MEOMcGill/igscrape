"""Download images and videos from a scraped JSON file."""

import asyncio
import json
import sys

from igscrape.downloaders import (
    download_images_from_posts,
    download_videos_from_posts,
)


async def main(scraped_json: str, out_dir: str):
    with open(scraped_json) as f:
        payload = json.load(f)
    posts = payload.get("posts", [])
    users = payload.get("users", [])
    print(f"Loaded {len(posts)} posts, {len(users)} users from {scraped_json}")

    image_paths = await download_images_from_posts(
        posts, f"{out_dir}/images", include_profile_pics=True, users=users
    )
    video_paths = await download_videos_from_posts(posts, f"{out_dir}/videos")
    print(f"Downloaded {len(image_paths)} images, {len(video_paths)} videos to {out_dir}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python test_downloaders.py <scraped.json> <out_dir>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
