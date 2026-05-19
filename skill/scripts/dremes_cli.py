#!/usr/bin/env python3
"""
Dremes CLI — clean commands for the Telegram bot to run.
No janky multi-step terminal hunts. Each command does one thing fully.

Usage:
  python3 skill/scripts/dremes_cli.py clear-refs --brand island-splash --pool drinks
  python3 skill/scripts/dremes_cli.py generate-ads --brand island-splash --pool drinks
  python3 skill/scripts/dremes_cli.py compose --brand island-splash
  python3 skill/scripts/dremes_cli.py schedule --brand island-splash
  python3 skill/scripts/dremes_cli.py status --brand island-splash
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dremes_common import slugify, REFS_PUBLIC_DIR, REFS_DATA_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRAND_ASSETS_DIR = REPO_ROOT / "brand_assets"
PUBLIC_REFS_DIR = REFS_PUBLIC_DIR  # alias for local consistency
DATA_DIR = os.environ.get("DATA_DIR", "")
OUTPUT_DIR = Path(DATA_DIR) / "output" if DATA_DIR else REPO_ROOT / "output"



# ── Clear Refs ────────────────────────────────────────────────────────────────

def cmd_clear_refs(brand: str, pool: str):
    """Permanently delete ALL refs for a brand/pool from everywhere."""
    pool_slug = slugify(pool)
    deleted = 0

    # 1. Gallery public images (pending + rejected)
    for subdir in ["pending", "rejected"]:
        d = PUBLIC_REFS_DIR / brand / pool_slug / subdir
        if d.exists():
            count = len(list(d.iterdir()))
            shutil.rmtree(d)
            d.mkdir(parents=True)
            deleted += count
            print(f"  deleted {count} from gallery/{subdir}")

    # 2. Brand assets refs
    ref_dirs = [
        BRAND_ASSETS_DIR / brand / "references" / pool_slug,
        BRAND_ASSETS_DIR / brand / "references" / pool_slug / "used-refs",
    ]
    for d in ref_dirs:
        if d.exists():
            count = len([f for f in d.iterdir() if f.is_file()])
            shutil.rmtree(d)
            d.mkdir(parents=True)
            deleted += count
            print(f"  deleted {count} from brand_assets/{d.name}")

    # 3. Manifest
    manifest_path = REFS_DATA_DIR / f"{brand}.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            if pool_slug in manifest.get("pools", {}):
                manifest["pools"][pool_slug] = {"images": [], "usage_count": {}}
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"  cleared manifest entry for {brand}/{pool_slug}")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  warning: could not update manifest: {e}")

    # 4. State tracking
    state_path = REPO_ROOT / "state" / "ref-pool" / brand / pool_slug / "index.json"
    if state_path.exists():
        state_path.unlink()
        print(f"  removed state tracking")

    print(f"\nDone. {deleted} refs permanently deleted.")
    return 0


# ── Status ────────────────────────────────────────────────────────────────────

def cmd_status(brand: str):
    """Show counts for a brand across all pools."""
    pools = []
    known_pool_names = set()

    brand_config = REPO_ROOT / "brands" / f"{brand}.json"
    if brand_config.exists():
        try:
            cfg = json.loads(brand_config.read_text())
            for product in cfg.get("products", []):
                if product.get("pool_slug"):
                    known_pool_names.add(product["pool_slug"])
        except (json.JSONDecodeError, OSError):
            pass

    # Detect pools from manifest
    manifest_path = REFS_DATA_DIR / f"{brand}.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            for pool_name, pool_data in manifest.get("pools", {}).items():
                known_pool_names.add(pool_name)
                pools.append({
                    "name": pool_name,
                    "pending": len(pool_data.get("images", [])),
                    "used": len(pool_data.get("usage_count", {})),
                })
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: scan filesystem
    if not pools:
        brand_refs = PUBLIC_REFS_DIR / brand
        if brand_refs.exists():
            for pool_dir in brand_refs.iterdir():
                if pool_dir.is_dir():
                    known_pool_names.add(pool_dir.name)
                    pending_dir = pool_dir / "pending"
                    rejected_dir = pool_dir / "rejected"
                    pending = len(list(pending_dir.iterdir())) if pending_dir.exists() else 0
                    rejected = len(list(rejected_dir.iterdir())) if rejected_dir.exists() else 0
                    pools.append({"name": pool_dir.name, "pending": pending, "rejected": rejected})

    existing_names = {p["name"] for p in pools}
    for pool_name in sorted(known_pool_names - existing_names):
        pools.append({"name": pool_name, "pending": 0, "used": 0})

    # Check ads approval state
    ad_path = OUTPUT_DIR / "ad-approval" / f"{brand}.json"
    ads_pending = ads_approved = ads_bad = 0
    if ad_path.exists():
        try:
            ad_data = json.loads(ad_path.read_text())
            ads_pending = len(ad_data.get("pending", []))
            ads_approved = len(ad_data.get("approved", []))
            ads_bad = len(ad_data.get("bad", []))
        except (json.JSONDecodeError, OSError):
            pass

    print(f"\n{brand}")
    print(f"{'='*40}")
    for p in pools:
        parts = [f"{p['name']}: {p.get('pending', 0)} pending"]
        if 'rejected' in p:
            parts.append(f"{p['rejected']} rejected")
        if 'used' in p:
            parts.append(f"{p['used']} used")
        print(f"  Refs — {' | '.join(parts)}")
    print(f"  Ads — {ads_pending} pending | {ads_approved} approved | {ads_bad} bad")

    return 0


# ── Generate Ads ──────────────────────────────────────────────────────────────

def cmd_generate_ads(brand: str, pool: str, count: int = 0, research: str | None = None):
    """Run ad generation via dremes_agent.py."""
    cmd = ["python3", "dremes_agent.py", "--brand", brand, "--pool", "--category", pool]
    if count > 0:
        cmd.extend(["--count", str(count)])
    if research:
        cmd.extend(["--research", research])

    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800)

    # Show output lines that matter (skip progress noise)
    for line in result.stdout.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("[") and len(stripped) < 200:
            print(stripped)

    if result.returncode != 0:
        print(f"error: generation failed (exit {result.returncode})")
        for line in result.stderr.split("\n")[-5:]:
            if line.strip():
                print(f"  {line.strip()}")
        return 1

    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dremes CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # clear-refs
    cr = sub.add_parser("clear-refs", help="Delete all refs for a brand/pool")
    cr.add_argument("--brand", required=True)
    cr.add_argument("--pool", required=True)

    # status
    st = sub.add_parser("status", help="Show ref + ad counts for a brand")
    st.add_argument("--brand", required=True)

    # generate-ads
    ga = sub.add_parser("generate-ads", help="Run ad generation")
    ga.add_argument("--brand", required=True)
    ga.add_argument("--pool", required=True)
    ga.add_argument("--count", type=int, default=0, help="Number of ads (0 = all)")
    ga.add_argument("--research", default=None, help="On-demand Jina web research query")

    args = parser.parse_args()

    if args.command == "clear-refs":
        return cmd_clear_refs(args.brand, args.pool)
    elif args.command == "status":
        return cmd_status(args.brand)
    elif args.command == "generate-ads":
        return cmd_generate_ads(args.brand, args.pool, args.count, getattr(args, 'research', None))

    return 0


if __name__ == "__main__":
    sys.exit(main())
