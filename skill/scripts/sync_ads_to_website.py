#!/usr/bin/env python3
"""
Sync ads to website data files.

Scans website/public/images/ads/{brand}/ for all generated ad images and
rebuilds website/public/data/{brand}.json so the website shows everything
that's actually on disk.

Usage:
    python3 skill/scripts/sync_ads_to_website.py --brand island-splash
    python3 skill/scripts/sync_ads_to_website.py --all
"""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = REPO_ROOT / "website" / "public"

# Use persistent volumes when on Railway
REFS_VOLUME = os.environ.get("REFS_VOLUME", "")
DATA_DIR = os.environ.get("DATA_DIR", "")
if DATA_DIR:
    OUTPUT_DIR = Path(DATA_DIR) / "output"
else:
    OUTPUT_DIR = REPO_ROOT / "output"


def extract_product_from_filename(filename: str) -> str:
    """Guess product name from filename."""
    # island-splash_20260427_185150_633.png -> try to match product
    return ""


def _filename_matches_brand(filename: str, slug: str) -> bool:
    """Check if a filename belongs to a specific brand."""
    fname = filename.lower()
    # Direct brand slug match
    if fname.startswith(slug.replace("-", "_") + "_") or fname.startswith(slug.lower() + "_"):
        return True
    # Brand-specific alternate prefixes
    alternates = {
        "island-splash": ["splash_", "island_splash_"],
        "cinco-h-ranch": ["cinco_"],
    }
    for prefix in alternates.get(slug, []):
        if fname.startswith(prefix):
            return True
    return False


def get_or_create_ad_entry(ads_data: list[dict], filename: str, slug: str) -> dict:
    """Find existing entry or create a new one."""
    for ad in ads_data:
        if ad.get("id") == filename or ad.get("filename") == filename:
            return ad
    return None


def sync_brand(slug: str, dry_run: bool = False) -> dict:
    # On Railway, ads live on volume (OUTPUT_DIR). Locally, they're in website/public/.
    img_dirs = [OUTPUT_DIR]
    local_img_dir = PUBLIC_DIR / "images" / "ads" / slug
    if local_img_dir.exists():
        img_dirs.append(local_img_dir)
    data_file = PUBLIC_DIR / "data" / f"{slug}.json"
    approval_file = OUTPUT_DIR / "ad-approval" / f"{slug}.json"

    if not img_dirs[0].exists() and not any(d.exists() for d in img_dirs[1:]):
        print(f"  No image directories for {slug}")
        return {"added": 0, "updated": 0, "skipped": 0}

    # Load existing data file
    ads_data = []
    if data_file.exists():
        try:
            ads_data = json.loads(data_file.read_text())
        except Exception:
            ads_data = []

    existing_ids = {ad.get("id") for ad in ads_data}

    # Load existing approval state
    approval_data = {"ads": {}, "pending_count": 0, "approved_count": 0, "bad_count": 0}
    if approval_file.exists():
        try:
            approval_data = json.loads(approval_file.read_text())
        except Exception:
            pass
    approval_data.setdefault("ads", {})

    # Scan image directory
    added = 0
    updated = 0
    skipped = 0
    seen_ids = set()

    for img_dir in img_dirs:
        if not img_dir.exists():
            continue
        for img_path in sorted(img_dir.iterdir()):
            if img_path.suffix.lower() not in [".png", ".jpg", ".jpeg", ".webp"]:
                continue

            filename = img_path.name

            # Only include files matching this brand
            if not _filename_matches_brand(filename, slug):
                continue

            seen_ids.add(filename)

            existing = get_or_create_ad_entry(ads_data, filename, slug)

            if existing:
                # Update status from disk
                existing["path"] = f"/images/ads/{slug}/{filename}"
                existing["filename"] = filename
                updated += 1
            else:
                # New ad — detect product from filename or instructions sidecar
                product_name = ""

                # Try sidecar for product info
                sidecar = img_dir / f"{img_path.stem}.instructions.txt"
                if sidecar.exists():
                    content = sidecar.read_text().lower()
                    products = ["Mango Passion", "Mauby", "Peanut Punch", "Lime",
                               "Guava Pine", "Sorrel", "Pine Ginger",
                               "Honey Vanilla Soap", "Rejuvenating Face + Body Cream", "Sunscreen Stick"]
                    for prod in products:
                        if prod.lower().replace(" + ", " ").replace(" ", "") in content.replace(" ", "").replace("+", ""):
                            product_name = prod
                            break
                    # Fallback: try filename
                    if not product_name:
                        for prod in products:
                            if prod.lower().split()[0] in content:
                                product_name = prod
                                break

                new_ad = {
                    "id": filename,
                    "filename": filename,
                    "path": f"/images/ads/{slug}/{filename}",
                    "product_name": product_name,
                    "status": "new",
                    "brand": slug,
                    "created_at": datetime.fromtimestamp(img_path.stat().st_mtime).isoformat(),
                }
                ads_data.append(new_ad)

                # Add to approval state in new format
                key = filename.replace('.png', '').replace('.jpg', '').replace('.jpeg', '')
                if filename not in approval_data.get("pending", []) and filename not in approval_data.get("approved", []):
                    approval_data.setdefault("pending", []).append(filename)

                added += 1

    # Add missing approval entries for ads already in data but not in approval
    for ad in ads_data:
        fid = ad.get("id") or ad.get("filename")
        fid = ad.get("filename", ad.get("id"))
        if fid and fid not in approval_data.get("pending", []) and fid not in approval_data.get("approved", []):
            if ad.get("status", "new") in ("new", "pending"):
                approval_data.setdefault("pending", []).append(fid)

    if dry_run:
        print(f"  [DRY RUN] Would add {added} ads, update {updated} for {slug}")
        print(f"  [DRY RUN] Would write {len(ads_data)} entries to {data_file}")
        return {"added": added, "updated": updated, "dry_run": True}

    # Write data file
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(json.dumps(ads_data, indent=2))

    # Write approval file
    approval_dir = OUTPUT_DIR / "ad-approval"
    approval_dir.mkdir(parents=True, exist_ok=True)
    approval_file.write_text(json.dumps(approval_data, indent=2))

    print(f"  {slug}: added={added}, updated={updated}, total={len(ads_data)}")
    print(f"  Wrote {data_file}")
    print(f"  Wrote {approval_file}")

    return {"added": added, "updated": updated, "total": len(ads_data)}


def main():
    parser = argparse.ArgumentParser(description="Sync ads to website data files")
    parser.add_argument("--brand", help="Brand slug (e.g. island-splash)")
    parser.add_argument("--all", action="store_true", help="Sync all brands")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    args = parser.parse_args()

    brands = []
    if args.all:
        brands = ["island-splash", "cinco-h-ranch"]
    elif args.brand:
        brands = [args.brand]
    else:
        # Auto-detect from brands dir
        brands_dir = REPO_ROOT / "brands"
        if brands_dir.exists():
            brands = [f.stem for f in brands_dir.glob("*.json")]

    if not brands:
        print("No brands found")
        return

    print(f"Syncing {len(brands)} brand(s)...")
    for brand in brands:
        print(f"\n{brand}:")
        sync_brand(brand, dry_run=args.dry_run)

    print("\nDone.")


if __name__ == "__main__":
    main()
