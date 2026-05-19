#!/usr/bin/env python3
"""
Add reference images to a brand's product pool.

Usage:
  python3 skill/scripts/add_refs.py --brand island-splash --product "Mango Passion" --image /path/to/photo.jpg
  python3 skill/scripts/add_refs.py --brand island-splash --product "Mango Passion" --images /path/to/photo1.jpg /path/to/photo2.jpg
  python3 skill/scripts/add_refs.py --brand island-splash --list-products
  python3 skill/scripts/add_refs.py --brand island-splash --product "Mango Passion" --show-pool
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from dremes_common import slugify, load_brand_config, REFS_PUBLIC_DIR, REFS_DATA_DIR


def load_brand(brand_slug: str) -> dict:
    """Load brand config. Raises FileNotFoundError if missing (add_refs convention)."""
    config = load_brand_config(brand_slug)
    if not config:
        raise FileNotFoundError(f"Brand not found: brands/{brand_slug}.json")
    return config


def get_product_ref_dir(brand_slug: str, product_name: str) -> Path:
    """Get the reference directory for a product.

    Uses the brand config's pool_dir as base, with per-product subdirectory
    when the brand uses product-specific pools (product_required=true).
    Falls back to brand_assets/{brand}/references/{product}/ if no config.
    """
    brand = load_brand(brand_slug)

    # Use pool_dir from brand config if set
    pool_dir = brand.get("paths", {}).get("pool_dir", "")
    if pool_dir:
        # For brands with per-product pools, append the product subdirectory
        if brand.get("product_required"):
            prod_slug = slugify(product_name)
            return Path(pool_dir) / prod_slug
        # For flat pool brands, return the pool_dir directly
        return Path(pool_dir)

    # Fallback: construct from brand_assets template
    prod_slug = slugify(product_name)
    return Path(f"brand_assets/{brand_slug}/references/{prod_slug}")


def list_products(brand_slug: str) -> list:
    """List all products for a brand."""
    brand = load_brand(brand_slug)
    products = brand.get('products', [])

    print(f"\n=== Products for {brand.get('display_name', brand_slug)} ===\n")

    if not products:
        print("No products found. Edit brands/{brand_slug}.json to add products.")
        return []

    # Use pool_dir from config if set, otherwise fall back to brand_assets template
    configured_pool = brand.get("paths", {}).get("pool_dir", "")
    if configured_pool:
        pool_base = Path(configured_pool)
    else:
        pool_base = Path(f"brand_assets/{brand_slug}/references")

    for i, prod in enumerate(products, 1):
        prod_slug = slugify(prod['name'])
        if brand.get("product_required"):
            ref_dir = pool_base / prod_slug
        else:
            ref_dir = pool_base

        # Count existing refs
        ref_count = 0
        if ref_dir.exists():
            refs = list(ref_dir.glob("*.jpg")) + list(ref_dir.glob("*.jpeg")) + list(ref_dir.glob("*.png"))
            ref_count = len(refs)

        status = f"[{ref_count} refs]" if ref_count > 0 else "[no refs]"
        print(f"  {i}. {prod['name']} {status}")

    print()
    return products


def show_pool(brand_slug: str, product_name: str) -> list:
    """Show current reference pool for a product."""
    ref_dir = get_product_ref_dir(brand_slug, product_name)
    
    if not ref_dir.exists():
        print(f"\nNo reference pool yet for {product_name}. Run add_refs.py to add images.\n")
        return []
    
    refs = sorted(ref_dir.glob("*.jpg")) + sorted(ref_dir.glob("*.jpeg")) + sorted(ref_dir.glob("*.png"))
    
    print(f"\n=== Reference Pool: {brand_slug} / {product_name} ===\n")
    
    if not refs:
        print("No reference images found.\n")
        return []
    
    for i, ref in enumerate(refs, 1):
        size_kb = ref.stat().st_size // 1024
        print(f"  {i}. {ref.name} ({size_kb} KB)")
    
    print(f"\nTotal: {len(refs)} reference images\n")
    return refs


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def mark_ref_as_used(brand_slug: str, product_name: str, ref_path: Path) -> bool:
    """
    After successful ad generation, mark a ref as used:
    1. Move it from brand_assets/{brand}/references/{product}/ to used-refs/ subfolder
    2. Remove it from the website manifest (available pool)
    Returns True on success.
    """
    prod_slug = slugify(product_name)
    ref_dir = get_product_ref_dir(brand_slug, product_name)
    used_dir = ref_dir / "used-refs"
    used_dir.mkdir(parents=True, exist_ok=True)

    # Move ref to used-refs/
    dest_path = used_dir / ref_path.name
    try:
        shutil.move(str(ref_path), str(dest_path))
        print(f"  [used] Moved to used-refs: {ref_path.name}")
    except Exception as e:
        print(f"  [used] ⚠️ Could not move to used-refs: {e}")
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
    if prod_slug in pools:
        img_key = f"/images/refs/{brand_slug}/{prod_slug}/{ref_path.name}"
        if img_key in pools[prod_slug].get("images", []):
            pools[prod_slug]["images"].remove(img_key)
            # Increment usage count
            usage = pools[prod_slug].setdefault("usage_count", {})
            usage[ref_path.name] = usage.get(ref_path.name, 0) + 1
            manifest["pools"] = pools
            manifest_path.write_text(json.dumps(manifest, indent=2))

    # Also remove the synced file from website public
    synced_file = REFS_PUBLIC_DIR / brand_slug / prod_slug / "pending" / ref_path.name
    if synced_file.exists():
        synced_file.unlink()
        print(f"  [web] Removed from website pool: {ref_path.name}")

    return True


def sync_ref_to_website(brand_slug: str, product_name: str, src_path: Path, new_name: str) -> Path:
    """Copy ref image into website public folder and update manifest."""
    prod_slug = slugify(product_name)
    dest_dir = REFS_PUBLIC_DIR / brand_slug / prod_slug / "pending"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / new_name
    shutil.copy2(src_path, dest_path)

    # Update manifest
    manifest_path = REFS_DATA_DIR / f"{brand_slug}.json"
    REFS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    manifest: dict = {"pools": {}, "products": []}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    pools = manifest.get("pools", {})
    if prod_slug not in pools:
        pools[prod_slug] = {"images": [], "usage_count": {}}
    img_path = f"/images/refs/{brand_slug}/{prod_slug}/{new_name}"
    if img_path not in pools[prod_slug]["images"]:
        pools[prod_slug]["images"].append(img_path)

    manifest["pools"] = pools
    if {"name": product_name, "slug": prod_slug} not in manifest.get("products", []):
        manifest.setdefault("products", []).append({"name": product_name, "slug": prod_slug})

    manifest_path.write_text(json.dumps(manifest, indent=2))
    return dest_path


def add_ref(brand_slug: str, product_name: str, image_path: str) -> str:
    """Add a single reference image to the pool."""
    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    if image_path.suffix.lower() not in ['.jpg', '.jpeg', '.png', '.webp']:
        raise ValueError(f"Not an image file: {image_path.suffix}")

    ref_dir = get_product_ref_dir(brand_slug, product_name)
    ref_dir.mkdir(parents=True, exist_ok=True)

    existing = list(ref_dir.glob("*.jpg")) + list(ref_dir.glob("*.jpeg")) + list(ref_dir.glob("*.png"))

    max_num = 0
    for ref in existing:
        name = ref.stem
        parts = name.split('_ref_')
        if len(parts) == 2:
            try:
                num = int(parts[1])
                max_num = max(max_num, num)
            except ValueError:
                pass

    next_num = max_num + 1
    prod_slug = slugify(product_name)
    new_name = f"{prod_slug}_ref_{next_num}{image_path.suffix.lower()}"
    dest_path = ref_dir / new_name

    shutil.copy2(image_path, dest_path)

    # Sync to website public folder for display
    try:
        sync_ref_to_website(brand_slug, product_name, dest_path, new_name)
        print(f"  [web] Synced to website: images/refs/{brand_slug}/{prod_slug}/{new_name}")
    except Exception as e:
        print(f"  [web] Warning: could not sync to website: {e}")

    return str(dest_path)


def add_refs(brand_slug: str, product_name: str, image_paths: list) -> list:
    """Add multiple reference images."""
    created = []
    for path in image_paths:
        try:
            dest = add_ref(brand_slug, product_name, path)
            created.append(dest)
        except Exception as e:
            print(f"⚠️  Skipped {path}: {e}")
    return created


def main():
    parser = argparse.ArgumentParser(description="Add reference images to a brand's product pool")
    parser.add_argument("--brand", required=True, help="Brand slug (e.g. island-splash)")
    parser.add_argument("--product", help="Product name (e.g. 'Mango Passion')")
    parser.add_argument("--image", help="Single image path to add")
    parser.add_argument("--images", nargs='+', help="Multiple image paths to add")
    parser.add_argument("--list-products", action='store_true', help="List all products for the brand")
    parser.add_argument("--show-pool", action='store_true', help="Show current reference pool for product")
    
    args = parser.parse_args()
    
    # Validate brand exists
    try:
        brand = load_brand(args.brand)
    except FileNotFoundError:
        print(f"\n❌ Brand not found: {args.brand}")
        print(f"Run onboard_brand.py to create it first.\n")
        return 1
    
    # List products mode
    if args.list_products:
        list_products(args.brand)
        return 0
    
    # Show pool mode
    if args.show_pool:
        if not args.product:
            print("\n❌ --product required for --show-pool\n")
            return 1
        show_pool(args.brand, args.product)
        return 0
    
    # Add refs mode
    if not args.product:
        print("\n❌ --product required\n")
        print("Usage:")
        print("  --list-products  List all products for the brand")
        print("  --show-pool      Show current reference pool")
        print("  --product X      Specify product name")
        print("  --image Y        Add single image")
        print("  --images Y Z     Add multiple images\n")
        return 1
    
    images = []
    if args.image:
        images.append(args.image)
    if args.images:
        images.extend(args.images)
    
    if not images:
        print("\n❌ --image or --images required\n")
        return 1
    
    # Add the refs
    print(f"\n=== Adding refs to {args.brand} / {args.product} ===\n")
    
    created = add_refs(args.brand, args.product, images)
    
    if created:
        print(f"✅ Added {len(created)} reference(s):")
        for path in created:
            print(f"   {path}")
        
        # Show updated pool count
        pool = show_pool(args.brand, args.product)
        print(f"\n✅ Pool now has {len(pool)} reference image(s)")
    else:
        print("\n❌ No images added\n")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
