#!/usr/bin/env python3
"""
Onboard a new brand into the Dremes ad pipeline.

Palette is auto-extracted from the logo image (colorthief).
User can override with --colors.

Creates:
  - brands/<slug>.json         (brand config, current schema)
  - Volume dirs under REFS_VOLUME (ref pools, output, ad-approval)
  - brand_assets/<slug>/        (logo + product images dirs)

Usage from agent:
  python3 skill/scripts/onboard_brand.py --name "My Brand" \
      --products "Soap, Cream" --logo-file /path/to/logo.png \
      --product-files /path/a.png,/path/b.png

After running, the brand appears in the gallery immediately (no deploy needed).
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REFS_VOLUME = os.environ.get("REFS_VOLUME", "")
DATA_DIR = os.environ.get("DATA_DIR", "") or REFS_VOLUME


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


def extract_palette(image_path: str, count: int = 4) -> list:
    """Extract dominant hex colors from an image using colorthief."""
    try:
        from colorthief import ColorThief
        ct = ColorThief(image_path)
        dominant = ct.get_color(quality=1)
        palette = ct.get_palette(color_count=min(count, 6), quality=1)
        hexes = []
        for rgb in palette:
            hexes.append(f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}")
        return hexes
    except ImportError:
        print("  [!] colorthief not installed — install with: pip install colorthief")
        return []
    except Exception as e:
        print(f"  [!] palette extraction failed: {e}")
        return []


def build_brand_config(args) -> dict:
    """Build brand JSON matching current schema (schema_version 1)."""
    slug = args.slug or slugify(args.name)

    products = []
    if args.products:
        names = [p.strip() for p in args.products.split(",")]
        containers = None
        if args.containers:
            containers = [c.strip() for c in args.containers.split(",")]
        for i, name in enumerate(names):
            p_slug = slugify(name)
            container = (containers[i] if containers and i < len(containers)
                         else args.container or "bottle")
            products.append({
                "name": name,
                "label_file": f"{p_slug}.png",
                "container": container,
                "cap_rule": args.cap_rule or "match the product image",
                "pool_slug": p_slug if args.product_required else None,
                "triggers": [p_slug.replace("-", " ")] if args.product_required else None,
                "keywords": p_slug.split("-"),
                "allowed_claims": [],
                "forbidden_text": [],
                "voice_note": "",
                "real_claims": [],
                "real_ingredients": ""
            })

    # Compute actual logo extension for the config path
    logo_ext = ".png"
    if args.logo_file and os.path.exists(args.logo_file):
        logo_ext = os.path.splitext(args.logo_file)[1] or ".png"

    palette_hex = []
    # Auto-extract from logo if no --colors and logo exists
    if args.colors:
        palette_hex = [c.strip() for c in args.colors.split(",")]
    elif args.logo_file and os.path.exists(args.logo_file):
        palette_hex = extract_palette(args.logo_file)
    palette_desc = args.palette_desc or " ".join(palette_hex)

    prop_themes = [t.strip() for t in args.prop_themes.split(",")] if args.prop_themes else []
    forbidden_props = [t.strip() for t in args.forbidden_props.split(",")] if args.forbidden_props else []

    default_forbidden = [
        {"pattern": "#", "severity": "error", "reason": "no hashtags"},
        {"pattern": "www.", "severity": "error", "reason": "no URLs"},
        {"pattern": ".com", "severity": "error", "reason": "no URLs"},
        {"pattern": "@", "severity": "error", "reason": "no social handles"},
        {"pattern": "FREE", "severity": "error", "reason": "no fake promotions"},
        {"pattern": "% OFF", "severity": "error", "reason": "no fake promotions"},
        {"pattern": "GIVEAWAY", "severity": "error", "reason": "no fake promotions"},
        {"pattern": "$", "severity": "warning", "reason": "no pricing"},
    ]

    config = {
        "schema_version": 1,
        "slug": slug,
        "display_name": args.name,
        "product_required": args.product_required,

        "scheduling": {
            "posts_per_day": args.posts_per_day or 2,
            "time_slots": [t.strip() for t in args.time_slots.split(",")] if args.time_slots else ["09:00", "17:00"],
            "platforms": args.platforms.split(",") if args.platforms else ["instagram"],
            "instagram_account_id": args.ig_account or "TBD",
            "facebook_account_id": args.fb_account or "",
            "carousel_max_slides": args.carousel_max or 10,
        },

        "paths": {
            "logo_path": str(REPO_ROOT / "brand_assets" / slug / "logo" / "logo.png"),
            "products_dir": str(REPO_ROOT / "brand_assets" / slug / "products"),
            "tally_path": str(Path(DATA_DIR or REPO_ROOT) / f"{slug}.usage.json") if DATA_DIR else "",
            "pool_dir": str(REPO_ROOT / "brand_assets" / slug / "references"),
            "rules_path": None,
        },

        "identity": {
            "vibe": args.vibe or "",
            "palette": {
                "hex": palette_hex,
                "description": palette_desc,
            },
            "prop_themes": prop_themes,
            "forbidden_prop_themes": forbidden_props,
            "allowed_headlines": [h.strip() for h in args.headlines.split(",")] if args.headlines else [],
            "allowed_vibe_phrases": [p.strip() for p in args.vibe_phrases.split(",")] if args.vibe_phrases else [],
            "voice": args.voice or args.vibe or "",
        },

        "global_forbidden_text": default_forbidden,
        "ad_creative_rules": [
            "Product labels must match the provided product images exactly.",
            "No mascots, cartoon characters, or personified objects.",
            "Logo appears once, small, in a corner — no effects, no boxes.",
            "Background and color grade must use ONLY brand palette colors.",
        ],

        "products": products,
    }

    return config


def create_structure(slug: str, products: list, logo_file: str, product_files: list):
    """Create all directories and copy assets. Returns list of what was done."""
    actions = []

    # 1. Brand assets (git-tracked)
    assets_dir = REPO_ROOT / "brand_assets" / slug
    for sub in ["logo", "products"]:
        d = assets_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        actions.append(f"mkdir {d}")

    # Copy logo
    if logo_file and os.path.exists(logo_file):
        ext = os.path.splitext(logo_file)[1] or ".png"
        dst = assets_dir / "logo" / f"logo{ext}"
        shutil.copy2(logo_file, dst)
        actions.append(f"copied logo → {dst}")

    # Copy product images
    for i, pf in enumerate(product_files or []):
        if pf and os.path.exists(pf):
            prod = products[i] if i < len(products) else None
            name = slugify(prod["name"]) if prod else f"product_{i}"
            ext = os.path.splitext(pf)[1] or ".png"
            dst = assets_dir / "products" / f"{name}{ext}"
            shutil.copy2(pf, dst)
            actions.append(f"copied product → {dst}")

    # 2. Volume directories (on Railway these survive deploys)
    if REFS_VOLUME:
        vol = Path(REFS_VOLUME)
        # Reference pool dirs
        for prod in products:
            pool = prod.get("pool_slug") or slugify(prod.get("name", ""))
            if pool:
                for tab in ["pending", "approved"]:
                    d = vol / "public" / "images" / "refs" / slug / pool / tab
                    d.mkdir(parents=True, exist_ok=True)
        # Output + ad-approval
        out = vol / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "ad-approval").mkdir(parents=True, exist_ok=True)
        actions.append(f"volume dirs ready at {vol}")

    # 3. Brand config
    brands_dir = REPO_ROOT / "brands"
    brands_dir.mkdir(exist_ok=True)
    config_path = brands_dir / f"{slug}.json"
    actions.append(f"brand JSON → {config_path}")

    return actions, config_path


def main():
    parser = argparse.ArgumentParser(description="Onboard a new brand")
    parser.add_argument("--name", required=True, help="Brand display name")
    parser.add_argument("--slug", help="URL-safe slug (auto from name)")
    parser.add_argument("--vibe", help="Brand vibe description")
    parser.add_argument("--colors", help="Comma-separated hex (e.g. '#FF6B35,#00B4D8')")
    parser.add_argument("--palette-desc", help="Description of palette")
    parser.add_argument("--products", help="Comma-separated product names")
    parser.add_argument("--container", help="Default product container type (default: bottle)")
    parser.add_argument("--containers", help="Comma-sep per-product container types (aligns with --products)")
    parser.add_argument("--cap-rule", help="Cap color rule")
    parser.add_argument("--product-required", action="store_true", help="Each product has its own reference pool")
    parser.add_argument("--headlines", help="Comma-sep allowed headlines")
    parser.add_argument("--vibe-phrases", help="Comma-sep vibe phrases")
    parser.add_argument("--voice", help="Brand voice description")
    parser.add_argument("--format", help="Brand visual format description")
    parser.add_argument("--prop-themes", help="Comma-sep allowed prop themes")
    parser.add_argument("--forbidden-props", help="Comma-sep forbidden prop themes")
    parser.add_argument("--platforms", help="Social platforms (default: instagram)")
    parser.add_argument("--time-slots", help="Post times (e.g. '09:00,17:00')")
    parser.add_argument("--posts-per-day", type=int, default=2)
    parser.add_argument("--carousel-max", type=int, default=10)
    parser.add_argument("--ig-account", help="Instagram account ID")
    parser.add_argument("--logo-file", help="Path to logo image to copy")
    parser.add_argument("--product-files", help="Comma-sep paths to product images")
    parser.add_argument("--dry-run", action="store_true", help="Show config without writing")

    args = parser.parse_args()

    if not args.slug:
        args.slug = slugify(args.name)

    config = build_brand_config(args)

    if args.dry_run:
        print(json.dumps(config, indent=2))
        return 0

    # Parse product files
    prod_files = []
    if args.product_files:
        prod_files = [p.strip() for p in args.product_files.split(",")]

    # Create everything
    actions, config_path = create_structure(
        args.slug, config["products"],
        args.logo_file, prod_files
    )

    # Write brand config
    config_path.write_text(json.dumps(config, indent=2))

    for a in actions:
        print(f"  [✓] {a}")

    print(f"\nBrand '{args.name}' ({args.slug}) ready.")
    print(f"Config: {config_path}")
    print(f"Next: drain references with: python3 skill/scripts/drain_board.py --brand {args.slug} --board-url <PIN_URL> --pool <pool>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
