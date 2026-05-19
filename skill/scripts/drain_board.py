#!/usr/bin/env python3
"""
Drain images from a Pinterest board and add to product ref pool.
No generation — refs staged only. Run splash/cinco go separately to generate.

Uses pinterest-dl (https://github.com/sean1832/pinterest-dl) for reliable
board scraping via the reverse-engineered Pinterest API.

Usage:
  python3 skill/scripts/drain_board.py --brand island-splash --board-url "https://www.pinterest.com/user/board/XXXXX" --pool drinks
  python3 skill/scripts/drain_board.py --brand island-splash --board-url "https://pin.it/XXXXX" --pool drinks --max-images 50
  python3 skill/scripts/drain_board.py --brand island-splash --board-url "https://pin.it/XXXXX" --pool drinks --dry-run
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dremes_common import slugify, load_brand_config, REFS_PUBLIC_DIR, REFS_DATA_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRANDS_DIR = REPO_ROOT / "brands"
PINTEREST_DL_BIN = shutil.which("pinterest-dl") or "/home/drewp/.local/bin/pinterest-dl"
COOKIES_FILE = REPO_ROOT / "pinterest-cookies.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ── Pinterest scraping ──────────────────────────────────────────────────────────

def is_pinterest_dl_available() -> bool:
    try:
        result = subprocess.run([PINTEREST_DL_BIN, '--version'], capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def resolve_short_url(url: str) -> str:
    """Resolve a pin.it short URL to the full Pinterest board URL."""
    if 'pin.it' not in url:
        return url

    import requests as _requests
    try:
        resp = _requests.head(url, allow_redirects=True, timeout=10,
                             headers={'User-Agent': 'Mozilla/5.0'})
        return resp.url
    except Exception:
        return url


def scrape_pinterest_board(board_url: str, max_images: int = None, output_dir: str = None) -> list:
    """Scrape images from a Pinterest board using pinterest-dl.

    Uses Chromium/Playwright browser-based scraping which is the only
    reliable method as of 2025-2026. Pinterest's reverse-engineered API
    endpoints are blocked for unauthenticated requests.

    If cookies file is available, uses API mode instead (faster).
    """
    if not is_pinterest_dl_available():
        print(f"⚠️  pinterest-dl not found at {PINTEREST_DL_BIN}")
        print(f"   Install with: pip install pinterest-dl")
        return []

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='pinterest_')

    # Resolve short URLs (pin.it/xxx → full board URL)
    resolved_url = resolve_short_url(board_url)
    if resolved_url != board_url:
        print(f"   Resolved short URL: {resolved_url}")

    # Strategy: Chromium/Playwright mode is the most reliable for public boards.
    # API mode only works with authenticated cookies (Pinterest blocks
    # unauthenticated API access as of 2025).
    # If cookies are available, use API mode (faster). Otherwise use Chromium.

    has_cookies = COOKIES_FILE.exists()
    client_mode = 'api' if has_cookies else 'chromium'

    cmd = [
        PINTEREST_DL_BIN, 'scrape',
        '--client', client_mode,
        '-o', output_dir,
        '-n', str(max_images),
    ]

    if has_cookies:
        cmd.extend(['-c', str(COOKIES_FILE)])

    cmd.append(resolved_url)

    mode_label = 'API (with cookies)' if has_cookies else 'Chromium browser'
    print(f"📥 Scraping Pinterest board via {mode_label}...")
    print(f"   URL: {resolved_url}")
    print(f"   Output: {output_dir}")

    try:
        timeout = 300 if has_cookies else 900  # Chromium is slower
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # pinterest-dl exits with code 1 even when most images downloaded successfully
        # (e.g. 4/5 downloaded, 1 failed with 403 on originals).
        # Check actual downloaded files rather than exit code.
        output_path = Path(output_dir)
        downloaded = list(output_path.glob('*'))
        downloaded_files = [f for f in downloaded if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
        # Filter small files (thumbnails)
        downloaded_files = [f for f in downloaded_files if f.stat().st_size > 10000]
        # Sort by filename (stable ordering)
        downloaded_files.sort(key=lambda x: x.name)

        if downloaded_files:
            # Success - we got some images
            if result.returncode != 0:
                for line in result.stderr.split('\n'):
                    if 'Failed to download' in line:
                        print(f"   ⚠️  {line.strip()[:120]}")
                print(f"   ✅ Downloaded {len(downloaded_files)} images (some may have failed)")
            return downloaded_files

        # Nothing downloaded - try fallbacks
        stderr_tail = result.stderr[-500:] if len(result.stderr) > 500 else result.stderr

        # If chromium failed and cookies available, try API fallback
        if client_mode == 'chromium' and has_cookies:
            print(f"⚠️  Chromium scrape failed: {stderr_tail[:200]}")
            print(f"   Retrying with API + cookies...")
            cmd_api = [
                PINTEREST_DL_BIN, 'scrape',
                '--client', 'api',
                '-o', output_dir,
                '-n', str(max_images),
                '-c', str(COOKIES_FILE),
                resolved_url,
            ]
            result = subprocess.run(cmd_api, capture_output=True, text=True, timeout=300)
            downloaded_files = [f for f in output_path.glob('*') if f.is_file() and f.suffix.lower() in IMAGE_EXTS and f.stat().st_size > 10000]
            downloaded_files.sort(key=lambda x: x.name)
            if not downloaded_files:
                print(f"⚠️  API+ cookies also failed: {result.stderr[:300]}")
                return []
            return downloaded_files
        # If chromium failed and no cookies, try API as last resort
        elif client_mode == 'chromium':
            print(f"⚠️  Chromium scrape failed: {stderr_tail[:200]}")
            print(f"   Trying API as last resort...")
            cmd_api = [
                PINTEREST_DL_BIN, 'scrape',
                '--client', 'api',
                '-o', output_dir,
                '-n', str(max_images),
                resolved_url,
            ]
            result = subprocess.run(cmd_api, capture_output=True, text=True, timeout=300)
            downloaded_files = [f for f in output_path.glob('*') if f.is_file() and f.suffix.lower() in IMAGE_EXTS and f.stat().st_size > 10000]
            downloaded_files.sort(key=lambda x: x.name)
            if not downloaded_files:
                print(f"⚠️  API also failed. Board may be private or Pinterest is blocking.")
                return []
            return downloaded_files
        else:
            print(f"⚠️  Scrape failed: {stderr_tail[:300]}")
            return []

    except subprocess.TimeoutExpired:
        print(f"⚠️  Timed out")
        return []
    except FileNotFoundError:
        print(f"⚠️  pinterest-dl not found at {PINTEREST_DL_BIN}")
        return []
    except Exception as e:
        print(f"⚠️  Error: {e}")
        return []


# ── Path resolution ─────────────────────────────────────────────────────────────
def get_ref_dir(brand_slug: str, pool_name: str) -> Path:
    """Resolve the correct ref directory for a brand.

    - product_required=true brands (e.g. cinco-h-ranch):
        pool_dir/references/{product_slug}/
    - flat-pool brands (e.g. island-splash):
        pool_dir/{pool_slug}/ unless pool_dir already points at that pool
    """
    config = load_brand_config(brand_slug)

    pool_dir = Path(config.get('paths', {}).get('pool_dir',
                        REPO_ROOT / 'brand_assets' / brand_slug / 'references'))
    product_required = config.get('product_required', False)

    if product_required:
        # Per-product subdirectory
        prod_slug = slugify(pool_name)
        ref_dir = pool_dir / prod_slug
    else:
        # Flat pool — pool_name is the brand-level pool slug
        pool_slug = slugify(pool_name)
        ref_dir = pool_dir if pool_dir.name == pool_slug else pool_dir / pool_slug

    ref_dir.mkdir(parents=True, exist_ok=True)
    return ref_dir


# ── Pool management ────────────────────────────────────────────────────────────

def load_brand_products(brand_slug: str) -> list:
    config = load_brand_config(brand_slug)
    return config.get('products', [])


def load_ref_manifest(brand_slug: str) -> dict:
    manifest_path = REFS_DATA_DIR / f"{brand_slug}.json"
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"pools": {}, "products": []}


def ref_manifest_entry(brand_slug: str, pool_slug: str, filename: str) -> dict:
    return {
        "filename": filename,
        "url": f"/images/refs/{brand_slug}/{pool_slug}/{filename}",
    }


def sync_ref_to_website(brand_slug: str, pool_name: str, src_path: Path, new_name: str) -> Path:
    """Copy ref image into website public folder and update manifest."""
    pool_slug = slugify(pool_name)
    dest_dir = REFS_PUBLIC_DIR / brand_slug / pool_slug / "pending"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / new_name
    shutil.copy2(src_path, dest_path)

    REFS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = REFS_DATA_DIR / f"{brand_slug}.json"
    manifest = load_ref_manifest(brand_slug)

    pools = manifest.setdefault("pools", {})
    pool = pools.setdefault(pool_slug, {"images": [], "usage_count": {}})
    images = pool.setdefault("images", [])
    entry = ref_manifest_entry(brand_slug, pool_slug, new_name)
    if not any((img.get("filename") if isinstance(img, dict) else Path(str(img)).name) == new_name for img in images):
        images.append(entry)

    if not manifest.get("products"):
        manifest["products"] = [
            {"name": product["name"], "slug": slugify(product["name"])}
            for product in load_brand_products(brand_slug)
            if product.get("name")
        ]

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return dest_path


def update_ref_pool_state(brand_slug: str, pool_name: str, added_count: int) -> None:
    """Increment unapproved count for refs staged into a pool."""
    if added_count <= 0:
        return

    pool_slug = slugify(pool_name)
    state_path = REPO_ROOT / "state" / "ref-pool" / brand_slug / pool_slug / "index.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    else:
        state = {}

    from datetime import datetime

    state.setdefault("brand", brand_slug)
    state["category"] = pool_slug
    state["unapproved"] = state.get("unapproved", 0) + added_count
    state.setdefault("approved", 0)
    state.setdefault("rejected", 0)
    state.setdefault("used", 0)
    state.setdefault("trigger_threshold", 3)
    state.setdefault("triggered", False)
    state["last_updated"] = datetime.now().isoformat()

    state_path.write_text(json.dumps(state, indent=2) + "\n")


def hash_file(path: Path) -> str:
    """Return MD5 hash of a file's contents."""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def get_existing_hashes(brand_slug: str, pool_name: str) -> set:
    """Get MD5 hashes of all existing refs in a pool (both on volume and in brand_assets)."""
    hashes = set()
    # Check volume (persistent Railway storage)
    pending_dir = REFS_PUBLIC_DIR / brand_slug / slugify(pool_name) / "pending"
    if pending_dir.exists():
        for f in pending_dir.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                try:
                    hashes.add(hash_file(f))
                except OSError:
                    pass
    # Check approved dir too
    approved_dir = REFS_PUBLIC_DIR / brand_slug / slugify(pool_name) / "approved"
    if approved_dir.exists():
        for f in approved_dir.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                try:
                    hashes.add(hash_file(f))
                except OSError:
                    pass
    # Check brand_assets ref dir
    ref_dir = get_ref_dir(brand_slug, pool_name)
    if ref_dir.exists():
        for f in ref_dir.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                try:
                    hashes.add(hash_file(f))
                except OSError:
                    pass
    return hashes


def add_ref_to_pool(brand_slug: str, pool_name: str, image_path: Path) -> str:
    """Add a reference image to the pool — writes to REFS volume only.

    Single source of truth: REFS_PUBLIC_DIR (persistent volume on Railway).
    No dual-write to brand_assets (which gets wiped on deploy).
    Returns dest filename on success, empty string if skipped.
    """
    pool_slug = slugify(pool_name)

    # Check for duplicate by content hash against volume only
    new_hash = hash_file(image_path)
    existing_hashes = set()
    for tab in ("pending", "approved", "used"):
        d = REFS_PUBLIC_DIR / brand_slug / pool_slug / tab
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                    try:
                        existing_hashes.add(hash_file(f))
                    except OSError:
                        pass

    if new_hash in existing_hashes:
        print(f"      [skip] Duplicate: {image_path.name}")
        return ""

    # Find next index
    pending_dir = REFS_PUBLIC_DIR / brand_slug / pool_slug / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    existing = [f for f in pending_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
    max_num = 0
    for ref in existing:
        parts = ref.stem.split('_ref_')
        if len(parts) == 2:
            try:
                max_num = max(max_num, int(parts[1]))
            except ValueError:
                pass

    next_num = max_num + 1
    dest_name = f"{pool_slug}_ref_{next_num}{image_path.suffix.lower()}"
    dest_path = pending_dir / dest_name

    shutil.copy2(image_path, dest_path)

    # Verify write succeeded
    if not dest_path.exists() or dest_path.stat().st_size < 10000:
        print(f"      [fail] Write verification failed: {dest_name}")
        return ""

    print(f"      [ref] ✅ {dest_name}  ({dest_path.stat().st_size // 1024} KB)")

    # Update manifest on volume
    REFS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = REFS_DATA_DIR / f"{brand_slug}.json"
    manifest = {"pools": {}, "products": []}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    pools = manifest.setdefault("pools", {})
    pool = pools.setdefault(pool_slug, {"images": [], "usage_count": {}})
    entry = {"filename": dest_name, "url": f"/images/refs/{brand_slug}/{pool_slug}/{dest_name}"}
    if not any((img.get("filename") if isinstance(img, dict) else "") == dest_name for img in pool.get("images", [])):
        pool["images"].append(entry)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    return dest_name


def mark_ref_as_used(brand_slug: str, pool_name: str, ref_path: Path) -> bool:
    """After ad generation, move ref to used-refs/ and update manifest."""
    ref_dir = get_ref_dir(brand_slug, pool_name)
    used_dir = ref_dir / "used-refs"
    used_dir.mkdir(parents=True, exist_ok=True)

    dest_path = used_dir / ref_path.name
    try:
        shutil.move(str(ref_path), str(dest_path))
        print(f"      [used] Moved to used-refs: {ref_path.name}")
    except Exception as e:
        print(f"      [used] ⚠️ Could not move to used-refs: {e}")
        return False

    # Remove from website manifest
    manifest_path = REFS_DATA_DIR / f"{brand_slug}.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            manifest = {"pools": {}, "products": []}
    else:
        manifest = {"pools": {}, "products": []}

    pools = manifest.get("pools", {})
    pool_slug = slugify(pool_name)
    if pool_slug in pools:
        img_key = f"/images/refs/{brand_slug}/{pool_slug}/{ref_path.name}"
        if img_key in pools[pool_slug].get("images", []):
            pools[pool_slug]["images"].remove(img_key)
            usage = pools[pool_slug].setdefault("usage_count", {})
            usage[ref_path.name] = usage.get(ref_path.name, 0) + 1
            manifest["pools"] = pools
            manifest_path.write_text(json.dumps(manifest, indent=2))

    # Remove synced file from website public
    synced_file = REFS_PUBLIC_DIR / brand_slug / pool_slug / ref_path.name
    if synced_file.exists():
        synced_file.unlink()
        print(f"      [web] Removed from website pool: {ref_path.name}")

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Drain Pinterest board to ref pool (no generation)")
    parser.add_argument('--brand', required=True, help="Brand slug (e.g. island-splash, cinco-h-ranch)")
    parser.add_argument('--board-url', required=True, help="Pinterest board URL (short pin.it/... or full board URL)")
    parser.add_argument('--pool', default='all-drinks', help="Pool / product name (default: all-drinks)")
    parser.add_argument('--max-images', type=int, default=500, help="Max images to fetch (default: 500)")
    parser.add_argument('--dry-run', action='store_true', help="Preview images without downloading")

    args = parser.parse_args()

    print(f"\n{'='*50}")
    print(f"  PINTEREST BOARD DRAINER")
    print(f"  (refs only — no generation)")
    print(f"{'='*50}\n")
    print(f"Brand: {args.brand}")
    print(f"Pool:  {args.pool}")
    print(f"URL:   {args.board_url}")
    print(f"Max:   {args.max_images or 'ALL'}\n")

    images = scrape_pinterest_board(args.board_url, args.max_images, '/tmp/pinterest_drain')

    if not images:
        print(f"\n❌ No images found!")
        print(f"\nPossible reasons:")
        print(f"  - Board is private")
        print(f"  - Board requires login")
        print(f"  - Pinterest blocking scrapers")
        print(f"\nTry:")
        print(f"  1. Make the board public temporarily")
        print(f"  2. Download images manually and use: python3 add_refs.py --images <files>")
        return 1

    print(f"\n📦 Found {len(images)} images\n")

    if args.dry_run:
        for img in images[:10]:
            print(f"  Would add: {img.name}")
        if len(images) > 10:
            print(f"  ... and {len(images) - 10} more")
        return 0

    results = []
    skipped = []
    errors = []

    for i, img in enumerate(images, 1):
        try:
            dest = add_ref_to_pool(args.brand, args.pool, img)
            if dest:
                results.append(dest)
                print(f"[{i}/{len(images)}] ✅ {Path(dest).name}")
            else:
                skipped.append(img.name)
        except Exception as e:
            errors.append(str(e))
            print(f"[{i}/{len(images)}] ❌ {img.name}: {e}")

    # Summary
    print(f"\n{'='*50}")
    print(f"  SUMMARY")
    print(f"{'='*50}")
    print(f"\n✅ Staged: {len(results)} images")

    if skipped:
        print(f"⏭️  Skipped duplicates: {len(skipped)}")
    if errors:
        print(f"❌ Failed: {len(errors)}")
        for err in errors[:3]:
            print(f"   {err}")

    update_ref_pool_state(args.brand, args.pool, len(results))

    # Show pool status
    ref_dir = get_ref_dir(args.brand, args.pool)
    total = len([ref for ref in ref_dir.iterdir() if ref.is_file() and ref.suffix.lower() in IMAGE_EXTS])
    print(f"\n📊 Pool '{args.pool}' now has {total} reference images")
    print(f"📝 No ads generated. Run 'splash go' or 'cinco go' when ready.")

    print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
