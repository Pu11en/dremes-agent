#!/usr/bin/env python3
"""
Populate the client showcase site with approved ad images.

Reads approved ads from the Railway volume, copies PNGs into the
drewpullen-site repo, rebuilds the manifest JSON, commits, and pushes.

Usage:
  python3 skill/scripts/populate_client_site.py --brand island-splash
  python3 skill/scripts/populate_client_site.py --brand cinco-h-ranch
  python3 skill/scripts/populate_client_site.py --all
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = os.environ.get("DATA_DIR", "")
if DATA_DIR:
    OUTPUT_DIR = Path(DATA_DIR) / "output"
else:
    OUTPUT_DIR = REPO_ROOT / "output"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CLIENT_REPO = "https://github.com/Pu11en/drewpullen-site.git"

def _clone_url() -> str:
    if GITHUB_TOKEN:
        return f"https://{GITHUB_TOKEN}@github.com/Pu11en/drewpullen-site.git"
    return CLIENT_REPO

CLONE_DIR = Path(tempfile.mkdtemp(prefix="drewpullen-site-"))


def get_approved_ads(brand: str) -> list[str]:
    """Read approved ad filenames from the volume."""
    approval_file = OUTPUT_DIR / "ad-approval" / f"{brand}.json"
    if not approval_file.exists():
        print(f"No approval file found: {approval_file}")
        return []
    try:
        data = json.loads(approval_file.read_text())
        return data.get("approved", [])
    except Exception as e:
        print(f"Failed to read approval file: {e}")
        return []


def get_product_name(brand: str, filename: str) -> str:
    """Extract product name from the ad's sidecar file."""
    sidecar = OUTPUT_DIR / f"{Path(filename).stem}.instructions.txt"
    if not sidecar.exists():
        return ""
    try:
        content = sidecar.read_text()
        m = re.search(r"^PRODUCTS:\s*(.+)$", content, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def populate_brand(brand: str) -> dict:
    """Copy approved PNGs and rebuild manifest for one brand."""
    approved = get_approved_ads(brand)
    if not approved:
        print(f"No approved ads for {brand}")
        return {"brand": brand, "copied": 0, "skipped": 0}

    # Clone the client repo
    print(f"Cloning {CLIENT_REPO}...")
    if CLONE_DIR.exists():
        shutil.rmtree(CLONE_DIR)
    result = subprocess.run(
        ["git", "clone", "--depth=1", _clone_url(), str(CLONE_DIR)],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"Clone failed: {result.stderr}")

    # Ensure directories exist
    ads_dir = CLONE_DIR / "public" / "ads" / brand
    data_dir = CLONE_DIR / "public" / "data" / "ads"
    ads_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Copy PNGs
    copied = 0
    skipped = 0
    manifest = []

    for fname in approved:
        src = OUTPUT_DIR / fname
        if not src.exists():
            # Try with .png extension
            src = OUTPUT_DIR / f"{Path(fname).stem}.png"

        dst = ads_dir / fname
        if src.exists():
            shutil.copy2(src, dst)
            product = get_product_name(brand, fname)
            manifest.append({
                "filename": fname,
                "product_name": product,
            })
            copied += 1
            print(f"  ✓ {fname}")
        else:
            print(f"  ✗ missing: {fname}")
            skipped += 1

    # Write manifest
    manifest_path = data_dir / f"{brand}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"  Wrote {len(manifest)} entries to {manifest_path.name}")

    # Commit and push
    subprocess.run(
        ["git", "-C", str(CLONE_DIR), "add", "public/ads/", "public/data/ads/"],
        capture_output=True, timeout=30
    )
    result = subprocess.run(
        ["git", "-C", str(CLONE_DIR), "commit", "-m",
         f"populate: {brand} — {copied} approved ads"],
        capture_output=True, text=True, timeout=30
    )
    if "nothing to commit" not in result.stdout + result.stderr:
        push = subprocess.run(
            ["git", "-C", str(CLONE_DIR), "push", "origin", "main"],
            capture_output=True, text=True, timeout=60
        )
        if push.returncode == 0:
            print(f"  Pushed to GitHub ✓")
        else:
            print(f"  Push failed: {push.stderr[:200]}")
    else:
        print(f"  No changes to push (already up to date)")

    # Cleanup
    shutil.rmtree(CLONE_DIR, ignore_errors=True)

    return {"brand": brand, "copied": copied, "skipped": skipped, "total": len(manifest)}


def main():
    parser = argparse.ArgumentParser(description="Populate client site with approved ads")
    parser.add_argument("--brand", help="Brand slug (e.g. island-splash)")
    parser.add_argument("--all", action="store_true", help="Populate all brands")
    args = parser.parse_args()

    brands = []
    if args.all:
        brands = ["island-splash", "cinco-h-ranch"]
    elif args.brand:
        brands = [args.brand]
    else:
        print("Specify --brand or --all")
        return 1

    results = []
    for brand in brands:
        print(f"\n=== {brand} ===")
        try:
            r = populate_brand(brand)
            results.append(r)
        except Exception as e:
            print(f"Failed: {e}")
            results.append({"brand": brand, "error": str(e)})

    # Summary
    total = sum(r.get("total", 0) for r in results)
    copied = sum(r.get("copied", 0) for r in results)
    print(f"\nDone. {copied} images populated across {len(brands)} brand(s).")
    print(f"Site: https://www.drewpullen.com/brands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
