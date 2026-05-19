#!/usr/bin/env python3
"""
Schedule or post carousel posts to Instagram via Blotato.

Usage:
  # List connected accounts
  python3 skill/scripts/schedule_post.py --list-accounts

  # Schedule a carousel (auto-finds next open slot)
  python3 skill/scripts/schedule_post.py \
    --brand island-splash \
    --carousel-ads ad1.png,ad2.png,ad3.png,ad4.png,ad5.png \
    --caption "Your caption here" \
    --hashtags "#Tag1 #Tag2"

  # Schedule for a specific date + slot
  python3 skill/scripts/schedule_post.py \
    --brand island-splash \
    --carousel-ads ad1.png,ad2.png,ad3.png,ad4.png,ad5.png \
    --caption "Caption" \
    --hashtags "#Tags" \
    --date 2026-04-25 \
    --slot 9am

  # Show scheduled posts
  python3 skill/scripts/schedule_post.py --show-scheduled --brand island-splash

  # Cancel a scheduled post
  python3 skill/scripts/schedule_post.py --cancel --brand island-splash --post-id post_123
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]

# Use persistent volumes when on Railway
REFS_VOLUME = os.environ.get("REFS_VOLUME", "")
DATA_DIR = os.environ.get("DATA_DIR", "")
if DATA_DIR:
    OUTPUT_DIR = Path(DATA_DIR) / "output"
else:
    OUTPUT_DIR = REPO_ROOT / "output"


# Blotato API config
BLOTATO_BASE = "https://backend.blotato.com/v2"
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_api_key() -> str:
    """Load Blotato API key from environment."""
    key = os.environ.get("BLOTATO_API_KEY")
    if key:
        return key
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("BLOTATO_API_KEY="):
                    return line.split("=", 1)[1].strip()
    raise RuntimeError("BLOTATO_API_KEY not found. Set it in dremes-agent/.env")


def blotato_headers() -> dict:
    return {
        "blotato-api-key": load_api_key(),
        "Content-Type": "application/json"
    }


def list_accounts() -> list:
    response = requests.get(f"{BLOTATO_BASE}/users/me/accounts", headers=blotato_headers())
    if response.status_code != 200:
        raise RuntimeError(f"Failed to list accounts: {response.status_code} {response.text}")
    return response.json().get("accounts", [])


def show_accounts():
    accounts = list_accounts()
    print("\n=== Connected Social Accounts ===\n")
    if not accounts:
        print("No accounts connected. Go to https://blotato.com → Settings → Connected Accounts.\n")
        return []
    for acc in accounts:
        print(f"  ID: {acc.get('id')}")
        print(f"  Platform: {acc.get('platform')}")
        print(f"  Name: {acc.get('name', acc.get('username', 'N/A'))}")
        print()
    return accounts


def load_brand(brand_slug: str) -> dict:
    config_path = REPO_ROOT / "brands" / f"{brand_slug}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Brand not found: {config_path}")
    with open(config_path) as f:
        return json.load(f)


def load_ad(brand_slug: str, ad_filename: str) -> dict:
    """Load ad data from brand's JSON file (tries volume first, then git-tracked)."""
    # Try volume path first (new format: {"pending": [...], "approved": [...]})
    ads_path = OUTPUT_DIR / "ad-approval" / f"{brand_slug}.json"
    if ads_path.exists():
        with open(ads_path) as f:
            approval = json.load(f)
        for tab in ("pending", "approved"):
            for entry in approval.get(tab, []):
                if isinstance(entry, str):
                    if entry == ad_filename:
                        return {"filename": entry}
                elif entry.get("filename", "") == ad_filename:
                    return {"filename": entry.get("filename", ad_filename)}
    # Fall back to git-tracked
    ads_path = REPO_ROOT / "website" / "public" / "data" / f"{brand_slug}.json"
    if not ads_path.exists():
        raise ValueError(f"Ads file not found: {ads_path}")
    with open(ads_path) as f:
        ads = json.load(f)
    for ad in ads:
        if ad.get("filename") == ad_filename or ad.get("id") == ad_filename:
            return ad
    raise ValueError(f"Ad not found: {ad_filename}")


def get_image_path(brand_slug: str, ad_filename: str) -> Path:
    """Find the image file on disk."""
    path = REPO_ROOT / "website" / "public" / "images" / "ads" / brand_slug / ad_filename
    if path.exists():
        return path
    # Try output dir
    path = OUTPUT_DIR / ad_filename
    if path.exists():
        return path
    raise FileNotFoundError(f"Image not found for {ad_filename}")


def upload_image(file_path: Path) -> str:
    """Upload image to Blotato, return public URL."""
    content_type = "image/png" if file_path.suffix.lower() == ".png" else "image/jpeg"
    resp = requests.post(
        f"{BLOTATO_BASE}/media/uploads",
        headers=blotato_headers(),
        json={"filename": file_path.name, "contentType": content_type}
    )
    if resp.status_code not in [200, 201]:
        raise RuntimeError(f"Failed to get upload URL: {resp.status_code} {resp.text}")
    data = resp.json()
    presigned_url = data.get("presignedUrl")
    if not presigned_url:
        raise RuntimeError(f"No presigned URL: {data}")
    with open(file_path, "rb") as f:
        upload_resp = requests.put(presigned_url, data=f, headers={"Content-Type": content_type})
    if upload_resp.status_code not in [200, 201]:
        raise RuntimeError(f"Upload failed: {upload_resp.status_code}")
    return data.get("publicUrl", f"https://media.blotato.com/{data.get('mediaId')}")


def upload_carousel_images(brand_slug: str, ad_filenames: list[str]) -> list[str]:
    """Upload all carousel images, return list of public URLs."""
    urls = []
    for i, fname in enumerate(ad_filenames):
        path = get_image_path(brand_slug, fname)
        url = upload_image(path)
        urls.append(url)
        print(f"  [{i+1}/{len(ad_filenames)}] Uploaded: {fname} → {url}")
    return urls


def resolve_account_id(brand_slug: str, platform: str, brand: dict) -> str:
    scheduling = brand.get("scheduling", {})
    configured = scheduling.get(f"{platform}_account_id", "")
    if configured and not configured.startswith("TODO"):
        return configured

    env_key = f"{brand_slug.upper().replace('-', '_')}_{platform.upper()}_ACCOUNT_ID"
    env_value = os.environ.get(env_key, "")
    if env_value:
        return env_value

    display = (brand.get("display_name") or brand_slug).lower()
    candidates = [brand_slug.replace("-", ""), brand_slug.replace("-", " "), display]
    for account in list_accounts():
        if (account.get("platform") or "").lower() != platform:
            continue
        name = (account.get("name") or account.get("username") or "").lower()
        compact = name.replace(" ", "").replace("-", "")
        if any(c.replace(" ", "").replace("-", "") in compact for c in candidates):
            return account.get("id", "")
    return ""


def post_carousel_to_platform(
    platform: str,
    account_id: str,
    media_urls: list[str],
    caption: str,
    hashtags: str = "",
    scheduled_time: str = None,
) -> dict:
    """Post a carousel to a Blotato platform (optionally scheduled)."""
    full_caption = caption
    if hashtags:
        full_caption = f"{caption}\n\n{hashtags}"

    payload = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": full_caption,
                "mediaUrls": media_urls,
                "platform": platform
            },
            "target": {
                "targetType": platform
            }
        }
    }

    # Add scheduling if provided (top-level, NOT inside post)
    if scheduled_time:
        payload["scheduledTime"] = scheduled_time

    resp = requests.post(f"{BLOTATO_BASE}/posts", headers=blotato_headers(), json=payload)
    if resp.status_code not in [200, 201]:
        raise RuntimeError(f"Failed to create post: {resp.status_code} {resp.text}")
    return resp.json()


def post_carousel_to_instagram(
    account_id: str,
    media_urls: list[str],
    caption: str,
    hashtags: str = "",
    scheduled_time: str = None,
) -> dict:
    return post_carousel_to_platform("instagram", account_id, media_urls, caption, hashtags, scheduled_time)


def get_scheduled_posts_from_blotato() -> list[dict]:
    """Fetch all scheduled posts from Blotato."""
    resp = requests.get(f"{BLOTATO_BASE}/schedules?limit=50", headers=blotato_headers())
    if resp.status_code != 200:
        return []
    return resp.json().get("items", [])


def find_open_slot(brand_slug: str, preferred_slot: str = None, preferred_date: str = None) -> str:
    """Find the next open 9am or 5pm slot.

    Returns ISO datetime string like "2026-04-25T09:00:00Z"
    """
    brand = load_brand(brand_slug)
    time_slots = brand.get("scheduling", {}).get("time_slots", ["09:00", "17:00"])
    account_id = brand.get("scheduling", {}).get("instagram_account_id")

    # Fetch what's already booked on Blotato for this account only.
    blotato_schedules = get_scheduled_posts_from_blotato()
    booked_times = {
        s["scheduledAt"]
        for s in blotato_schedules
        if s.get("scheduledAt") and s.get("account", {}).get("id") == account_id
    }

    # Start from today
    today = datetime.now().date()

    # If preferred date given, start there
    if preferred_date:
        try:
            today = datetime.strptime(preferred_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    # Check up to 30 days ahead
    for day_offset in range(0, 30):
        day = today + timedelta(days=day_offset)

        for slot_str in time_slots:
            hour_str, minute_str = slot_str.split(":")
            hour = int(hour_str)
            # Use UTC to match the Z-suffix in the ISO format
            slot_dt = datetime.utcnow().replace(year=day.year, month=day.month, day=day.day, hour=hour, minute=0, second=0, microsecond=0)
            iso_time = slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Skip if already booked
            if iso_time in booked_times:
                continue

            # If preferred_slot given, only consider that slot for today
            if preferred_slot and day_offset == 0:
                desired_hour = 9 if preferred_slot == "9am" else 17
                if hour != desired_hour:
                    continue

            # Don't schedule in the past (compare as UTC)
            now_utc = datetime.utcnow()
            if slot_dt <= now_utc:
                continue

            return iso_time

    raise RuntimeError("No open slots found in the next 30 days")


def add_to_scheduled(
    brand_slug: str,
    blotato_ids: dict,
    ad_filenames: list[str],
    caption: str,
    hashtags: str,
    scheduled_at: str,
    slot: str,
) -> None:
    """Append a scheduled post to the local scheduled JSON."""
    scheduled_path = REPO_ROOT / "website" / "public" / "data" / "scheduled" / f"{brand_slug}.json"
    scheduled_path.parent.mkdir(parents=True, exist_ok=True)

    posts = []
    if scheduled_path.exists():
        with open(scheduled_path) as f:
            posts = json.load(f)

    post_entry = {
        "id": f"local_{int(time.time() * 1000)}",
        "blotato_id": blotato_ids.get("instagram", ""),
        "blotato_ids": blotato_ids,
        "ad_ids": ad_filenames,
        "caption": caption,
        "hashtags": hashtags,
        "scheduled_at": scheduled_at,
        "slot": slot,
        "platform": ",".join(blotato_ids.keys()),
        "status": "pending",
    }

    posts.append(post_entry)

    with open(scheduled_path, "w") as f:
        json.dump(posts, f, indent=2)

    print(f"  Saved to {scheduled_path}")


def schedule_carousel(
    brand_slug: str,
    ad_filenames: list[str],
    caption: str,
    hashtags: str,
    preferred_slot: str = None,
    preferred_date: str = None,
    platforms_override: list[str] = None,
) -> int:
    """Full carousel scheduling flow."""
    print(f"\n=== Scheduling Carousel for {brand_slug} ===\n")

    # Load brand config
    brand = load_brand(brand_slug)
    platforms = platforms_override or brand.get("scheduling", {}).get("platforms", ["instagram"])
    accounts = {}
    for platform in platforms:
        account_id = resolve_account_id(brand_slug, platform, brand)
        if not account_id:
            print(f"❌ {platform.title()} account not configured/resolved for {brand_slug}.")
            print("   Run --list-accounts to see available accounts, or set the account id in the brand config/env.\n")
            return 1
        accounts[platform] = account_id

    # Find next open slot
    if preferred_date and preferred_slot:
        scheduled_time = find_open_slot(brand_slug, preferred_slot, preferred_date)
    elif preferred_slot:
        scheduled_time = find_open_slot(brand_slug, preferred_slot)
    else:
        scheduled_time = find_open_slot(brand_slug)

    slot_label = "9am" if scheduled_time[11:13] == "09" else "5pm"
    print(f"📅 Scheduling for: {scheduled_time} ({slot_label})")

    # Upload all carousel images
    print(f"📤 Uploading {len(ad_filenames)} carousel images...")
    media_urls = upload_carousel_images(brand_slug, ad_filenames)

    blotato_ids = {}
    for platform, account_id in accounts.items():
        print(f"📤 Submitting carousel to {platform.title()} (account: {account_id})...")
        result = post_carousel_to_platform(platform, account_id, media_urls, caption, hashtags, scheduled_time)
        blotato_id = result.get("postSubmissionId", result.get("id", ""))
        blotato_ids[platform] = blotato_id
        print(f"   {platform.title()} submission ID: {blotato_id}")

    # Save locally
    add_to_scheduled(brand_slug, blotato_ids, ad_filenames, caption, hashtags, scheduled_time, slot_label)

    print(f"\n✅ Carousel scheduled for {scheduled_time}")
    print(f"   View on dashboard: http://localhost:3000/{brand_slug}\n")
    return 0


def schedule_from_composed(brand_slug: str) -> int:
    """Read the latest composed post file and schedule it to Blotato."""
    posts_dir = OUTPUT_DIR / "posts"
    if not posts_dir.exists():
        print(f"\n❌ No composed posts found for {brand_slug}\n")
        return 1

    # Find latest composed post file for this brand
    post_files = sorted(posts_dir.glob(f"{brand_slug}_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not post_files:
        print(f"\n❌ No composed posts found for {brand_slug}\n")
        return 1

    latest = post_files[0]
    with open(latest) as f:
        data = json.load(f)

    posts = data.get("posts", [])
    if not posts:
        print(f"\n❌ Composed post file is empty: {latest.name}\n")
        return 1

    print(f"\n=== Scheduling from: {latest.name} ({len(posts)} posts) ===\n")

    scheduled_count = 0
    for post in posts:
        ad_ids = post.get("ad_filenames", [])
        caption = post.get("caption", "Check out our latest!")
        hashtags = post.get("hashtags", "")
        post_id = post.get("post_id", "unknown")
        if post.get("status") != "approved" or not post.get("approved_at"):
            print(f"  Skipping {post_id}: approval required")
            continue
        platform_status = post.get("platform_status", {})
        if any(v.get("external_post_id") or v.get("status") in ("scheduled", "posting", "posted") for v in platform_status.values()):
            print(f"  Skipping {post_id}: already scheduled/posted")
            continue

        if not ad_ids:
            print(f"  Skipping {post_id}: no ad filenames")
            continue

        # Skip if already scheduled (check local scheduled file)
        scheduled_path = REPO_ROOT / "website" / "public" / "data" / "scheduled" / f"{brand_slug}.json"
        already_done = False
        if scheduled_path.exists():
            with open(scheduled_path) as f:
                existing = json.load(f)
            for ep in existing:
                if ep.get("caption") == caption and set(ep.get("ad_ids", [])) == set(ad_ids):
                    already_done = True
                    print(f"  Skipping {post_id}: already scheduled")
                    break

        if already_done:
            continue

        try:
            slot = None  # auto-select next open slot
            result = schedule_carousel(brand_slug, ad_ids, caption, hashtags, slot, None, post.get("platforms"))
            if result == 0:
                scheduled_count += 1
        except Exception as e:
            print(f"  ❌ Failed to schedule {post_id}: {e}")

    print(f"\n✅ Scheduled {scheduled_count}/{len(posts)} posts from {latest.name}\n")
    return 0


def show_scheduled(brand_slug: str):
    scheduled_path = REPO_ROOT / "website" / "public" / "data" / "scheduled" / f"{brand_slug}.json"
    print(f"\n=== Scheduled Posts: {brand_slug} ===\n")
    if not scheduled_path.exists():
        print("No scheduled posts.\n")
        return
    with open(scheduled_path) as f:
        posts = json.load(f)
    if not posts:
        print("No scheduled posts.\n")
        return
    for post in posts:
        emoji = {"posted": "✅", "approved": "✅", "pending": "⏳", "rejected": "❌", "failed": "❌"}.get(post.get("status", ""), "⏳")
        print(f"  {emoji} [{post.get('slot')}] {post.get('scheduled_at')}")
        print(f"     Caption: {post.get('caption', '')[:60]}...")
        print(f"     Ads: {', '.join(post.get('ad_ids', []))}")
        print()
    return posts


def cancel_scheduled(brand_slug: str, blotato_id: str) -> int:
    scheduled_path = REPO_ROOT / "website" / "public" / "data" / "scheduled" / f"{brand_slug}.json"
    if not scheduled_path.exists():
        print(f"\n❌ No scheduled posts found\n")
        return 1

    with open(scheduled_path) as f:
        posts = json.load(f)

    original = len(posts)
    posts = [p for p in posts if p.get("blotato_id") != blotato_id]

    if len(posts) == original:
        print(f"\n❌ Post not found: {blotato_id}\n")
        return 1

    with open(scheduled_path, "w") as f:
        json.dump(posts, f, indent=2)

    # Also delete from Blotato
    try:
        requests.delete(f"{BLOTATO_BASE}/schedules/{blotato_id}", headers=blotato_headers())
    except Exception as e:
        print(f"   (Blotato delete warning: {e})")

    print(f"\n✅ Cancelled: {blotato_id}\n")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Schedule or post carousel posts to Instagram")
    parser.add_argument("--list-accounts", action="store_true", help="List connected accounts")
    parser.add_argument("--show-scheduled", action="store_true", help="Show scheduled posts")
    parser.add_argument("--cancel", action="store_true", help="Cancel a scheduled post")
    parser.add_argument("--brand", help="Brand slug (e.g. island-splash)")
    parser.add_argument("--carousel-ads", help="Comma-separated list of ad filenames (e.g. ad1.png,ad2.png,ad3.png)")
    parser.add_argument("--caption", help="Post caption")
    parser.add_argument("--hashtags", help="Hashtags (space-separated, include the #)")
    parser.add_argument("--slot", help="Preferred slot: 9am or 5pm")
    parser.add_argument("--date", help="Preferred date: YYYY-MM-DD")
    parser.add_argument("--post-id", help="Blotato post ID to cancel")
    parser.add_argument("--at", help="ISO datetime override (e.g. 2026-04-25T09:00:00Z)")
    parser.add_argument("--from-composed", action="store_true", help="Schedule from latest composed post file")
    parser.add_argument("--platforms", help="Comma-separated platforms override (e.g. instagram,facebook)")

    args = parser.parse_args()

    if args.list_accounts:
        show_accounts()
        return 0

    if args.show_scheduled:
        if not args.brand:
            print("\n❌ --brand required\n")
            return 1
        show_scheduled(args.brand)
        return 0

    if args.cancel:
        if not args.brand or not args.post_id:
            print("\n❌ --brand and --post-id required\n")
            return 1
        return cancel_scheduled(args.brand, args.post_id)

    if args.from_composed:
        if not args.brand:
            print("\n❌ --brand required\n")
            return 1
        return schedule_from_composed(args.brand)

    # Schedule carousel mode
    if args.carousel_ads:
        if not args.brand:
            print("\n❌ --brand required\n")
            return 1
        ad_filenames = [f.strip() for f in args.carousel_ads.split(",")]
        if len(ad_filenames) < 1:
            print("\n❌ At least 1 ad required for carousel\n")
            return 1
        if len(ad_filenames) > 10:
            print("\n❌ Instagram max 10 slides per carousel\n")
            return 1
        caption = args.caption or "Check out our latest! 🌴"
        hashtags = args.hashtags or ""
        slot = args.slot or None
        date = args.date or None
        platforms = [p.strip() for p in args.platforms.split(",") if p.strip()] if args.platforms else None
        return schedule_carousel(args.brand, ad_filenames, caption, hashtags, slot, date, platforms)

    # No action
    print("\n=== Asset Ads - Schedule/Post Tool ===\n")
    print("Commands:")
    print("  --list-accounts          List connected accounts")
    print("  --show-scheduled         Show scheduled posts")
    print("  --carousel-ads           Schedule a carousel post")
    print("  --cancel                 Cancel a scheduled post")
    print("\nExamples:")
    print("  python3 skill/scripts/schedule_post.py --list-accounts")
    print("  python3 skill/scripts/schedule_post.py \\")
    print("    --brand island-splash \\")
    print("    --carousel-ads ad1.png,ad2.png,ad3.png,ad4.png,ad5.png \\")
    print("    --caption 'Tropical vibes only 🌴' \\")
    print("    --hashtags '#IslandSplash #TropicalFlavors' \\")
    print("    --slot 9am --date 2026-04-25\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
