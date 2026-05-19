#!/usr/bin/env python3
"""
Dynamic caption + hashtag generator for Island Splash.
Uses narrative flow structures, not bullet points.
Hashtags: 10-12 for good discoverability.

Usage:
  python3 skill/scripts/generate_caption.py --brand island-splash --ad-files splash_20260420_103950.instructions
  python3 skill/scripts/generate_caption.py --brand island-splash --products "Mango Passion, Sorrel"
"""

import argparse
import json
import random
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "output"
ADS_DIR = REPO_ROOT / "website" / "public" / "images" / "ads"


# ----------------------------------------------------------------
# Narrative caption structures — no bullets, flowing paragraphs
# ----------------------------------------------------------------

# HOOKS — these stop the scroll
HOOKS = [
    "Tropical mornings hit different 🌴☀️",
    "Caribbean in a cup 🥥🏝️",
    "Five flavors. Zero compromise. 💯",
    "Real Caribbean taste in every sip 🌺",
    "The Caribbean called 🏝️ We answered. ✨",
    "Bold. Bright. Caribbean. 🍹",
    "Pure island in every pour 🌴✨",
    "Your next favorite tropical drink is here 🥤🌴",
    "Straight from the Caribbean 🌿💚",
    "When the Caribbean makes juice, you listen 🍹🏝️",
    "This is not juice. This is island in a glass 🌴🔥",
    "One sip and you're transported 🏝️✨",
    "The Caribbean doesn't do boring 💪🍹",
    "Made with island sunshine ☀️🌴",
    "Juice that actually tastes like the fruit 🍃💯",
    "Real fruit. Real flavor. Real Caribbean 🌺🍹",
    "Island time in a bottle 🏝️🥤",
    "Caribbean vibes only ✨🌴",
    "Tropical energy hits different 💥🌴",
    "Your daily island escape starts here 🏝️☀️",
]

# PRODUCT LINEUPS — varies by count
PRODUCT_TEMPLATES_1 = [
    "{product} hits different when it's made from real fruit 🍹",
    "{product}. Simple. Real. Caribbean. 💯🌴",
    "{product} for people who actually taste the difference 😤🍹",
    "Real {product} flavor. Nothing else. 🌿✨",
]

PRODUCT_TEMPLATES_2 = [
    "{p1} meets {p2} 😍 Two Caribbean classics, one perfect sip",
    "{p1} and {p2} exist in the same glass and it's beautiful 🏝️",
    "From {p1} to {p2} 🌍 The Caribbean knows flavors",
    "{p1} plus {p2} 🍹 Pure Caribbean in every drop",
]

PRODUCT_TEMPLATES_3P = [
    "{front}, {mid2} & {last} 🍹 The Caribbean doesn't hold back",
    "{front} + {mid2} + {last} 💥 Three Caribbean icons, one carousel",
    "{front}, {mid2}, {last} 🌴 All hitting different. Tap through 👉",
    "{front} to {last} 🏝️ Caribbean flavor, every sip",
]

# BODY lines — adds flavor/descriptive copy
BODY_LINES = [
    "Zero artificial anything 💚 Just fruit, water, and island sunshine 🌴☀️",
    "Handcrafted. Family recipes. Real Caribbean tradition 🍹🏝️",
    "Bold flavor for bold people 💪🌴",
    "Made for people who actually taste the difference 😤🍹",
    "The Caribbean in a glass 💯 No passport required 🏝️",
    "Natural. Refreshing. Addictive. One sip and you're hooked 🌺✨",
    "Every sip tastes like island time 🌴💫",
    "Caribbean made, globally loved 🌍❤️‍🔥",
    "From our islands to your hands 🌴 Real fruit, real flavor",
    "This is what happens when the Caribbean makes juice 🍹🔥",
    "Pure. Simple. Tropical. 🌿✨",
    "Real Caribbean flavor in every drop 💧🍹",
    "No artificial. No compromise. Just island 🌴💯",
    "Caribbean craft, Caribbean soul 🌺🏝️",
    "100% natural. 100% island. 100% delicious 💥🌴",
]

# CTAs — fun, not corporate
CTAs = [
    "Which one's calling your name? 👇🌴",
    "Tag someone who needs a tropical escape! 🏝️💨",
    "Sip and share 🌴☝️",
    "Comment below 👇🍹",
    "Double tap if you're thirsty 🥤😍",
    "Save this for your next grocery run 📌💯",
    "Caribbean approved ✅🌴",
    "Tag a tropical lover 🌺❤️",
    "Drop a 🍹 if you're ready 🙌",
    "Follow for more island vibes 🏝️✨",
    "Tap through the carousel 👉🍹",
    "Which flavor is your fav? 👇😋",
    "Someone who needs this? Tag them 🏝️🏝️",
]


def extract_products_from_instruction_file(brand: str, filename: str) -> list[str]:
    """Read instruction file to extract real product names."""
    base = filename.replace('.instructions', '').replace('.png', '').replace('.jpg', '')
    inst_path = OUTPUT_DIR / f"{base}.instructions.txt"
    if not inst_path.exists():
        inst_path = ADS_DIR / brand / f"{base}.instructions.txt"
    if not inst_path.exists():
        return []
    try:
        content = inst_path.read_text()
        m = re.search(r"^PRODUCTS:\s*(.+)$", content, re.MULTILINE)
        if m:
            return [p.strip() for p in m.group(1).split(",")]
    except OSError:
        pass
    return []


def extract_key_themes(brand: str, filenames: list[str]) -> list[str]:
    """Pull flavor keywords from instruction files."""
    themes = []
    keywords = ["tropical", "citrus", "spicy", "sweet", "refreshing", "caribbean",
                "fresh", "natural", "fruit", "juice", "zesty", "bold", "smooth"]
    for fname in filenames:
        base = fname.replace('.instructions', '').replace('.png', '')
        inst_path = OUTPUT_DIR / f"{base}.instructions.txt"
        if not inst_path.exists():
            inst_path = ADS_DIR / brand / f"{base}.instructions.txt"
        if inst_path.exists():
            try:
                text = inst_path.read_text().lower()
                for kw in keywords:
                    if kw in text:
                        themes.append(kw)
            except OSError:
                pass
    return themes


def load_brand_config(brand: str) -> dict:
    path = REPO_ROOT / "brands" / f"{brand}.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def hashtags_allowed(brand: str) -> bool:
    cfg = load_brand_config(brand)
    forbidden = cfg.get("global_forbidden_text", [])
    return not any(rule.get("pattern") == "#" and rule.get("severity") == "error" for rule in forbidden)


def load_state(brand: str) -> dict:
    state_path = OUTPUT_DIR / f"{brand}_caption_state.json"
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"used_captions": [], "used_hashtags": []}


def save_state(brand: str, state: dict) -> None:
    state_path = OUTPUT_DIR / f"{brand}_caption_state.json"
    with open(state_path, "w") as f:
        json.dump(state, f)


# ----------------------------------------------------------------
# Hashtag sets
# ----------------------------------------------------------------

BRAND_TAGS = [
    "#IslandSplash", "#TropicalFlavors", "#CaribbeanJuice", "#NaturalIngredients",
    "#TropicalDrinks", "#CaribbeanStyle", "#FreshJuice", "#TropicalJuice",
    "#IslandLife", "#CaribbeanMade", "#FreshTropical", "#IslandVibes",
]

PRODUCT_TAGS = {
    "mango": ["#Mango", "#MangoPassion", "#MangoJuice", "#TropicalMango"],
    "passion": ["#PassionFruit", "#PassionFruitJuice", "#PassionFruit"],
    "sorrel": ["#Sorrel", "#SorrelDrink", "#CaribbeanSorrel", "#SorrelTea"],
    "guava": ["#Guava", "#GuavaJuice", "#TropicalGuava", "#GuavaPine"],
    "pine": ["#Pineapple", "#PineGinger", "#TropicalPine", "#PineApple"],
    "ginger": ["#Ginger", "#GingerShot", "#GingerRoot", "#GingerHealth"],
    "mauby": ["#Mauby", "#MaubyBark", "#CaribbeanMauby", "#MaubyDrink"],
    "peanut": ["#PeanutPunch", "#PeanutDrink", "#CaribbeanPeanut"],
    "lime": ["#Lime", "#LimeJuice", "#KeyLime", "#Citrus"],
    "citrus": ["#Citrus", "#CitrusJuice", "#FreshCitrus"],
    "tropical": ["#Tropical", "#TropicalVibes", "#TropicalFlavors"],
    "caribbean": ["#Caribbean", "#CaribbeanStyle", "#CaribbeanEats", "#CaribbeanDrinks"],
    "juice": ["#FreshJuice", "#NaturalJuice", "#TropicalJuice"],
}

TRENDING_TAGS = [
    "#smallbusiness", "#shoplocal", "#fyp", "#foryoupage", "#viral",
    "#foodie", "#foodstagram", "#foodporn",
    "#healthylifestyle", "#natural", "#health",
    "#tropicalvibes", "#tropical", "#caribbeanstyle",
]


def build_hashtag_set(brand: str, products: list[str]) -> list[str]:
    """Build 5 hashtags max: brand tag + top product tag + trending + niche."""
    selected = set()
    product_lower = [p.lower() for p in products]

    # 1 brand tag
    brand_tags = BRAND_TAGS
    selected.update(random.sample(brand_tags, 1))

    # 1 top product tag
    for pl in product_lower:
        for key, tags in PRODUCT_TAGS.items():
            if key in pl:
                selected.update(random.sample(tags, 1))
                break
        if len(selected) >= 2:
            break

    # 1 trending/engagement tag
    selected.update(random.sample(TRENDING_TAGS, 1))

    # Fill to 5 with niche/foodie tags
    niche = ["#foodie", "#foodstagram", "#tropicalvibes", "#tropical", "#caribbeanstyle", "#freshjuice", "#natural"]
    for n in random.sample(niche, len(niche)):
        if len(selected) >= 5:
            break
        selected.add(n)

    return list(selected)[:5]


# ----------------------------------------------------------------
# Caption builder
# ----------------------------------------------------------------

def build_caption(brand: str, products: list[str]) -> str:
    """Build a flowing narrative caption, no bullet points."""
    lines = []

    # 1. Hook
    lines.append(random.choice(HOOKS))

    # 2. Product lineup (if products detected)
    if products:
        unique = list(dict.fromkeys(products))
        if len(unique) == 1:
            tpl = random.choice(PRODUCT_TEMPLATES_1)
            lines.append(tpl.format(product=unique[0]))
        elif len(unique) == 2:
            tpl = random.choice(PRODUCT_TEMPLATES_2)
            lines.append(tpl.format(p1=unique[0], p2=unique[1]))
        else:
            front = unique[0]
            mid2 = unique[1]
            last = unique[-1]
            if len(unique) == 3:
                tpl = random.choice(PRODUCT_TEMPLATES_3P)
                lines.append(tpl.format(front=front, mid2=mid2, last=last))
            else:
                # 4+ products — list first 3 with "and X more"
                tpl = random.choice(PRODUCT_TEMPLATES_3P)
                lines.append(tpl.format(front=front, mid2=mid2, last=last))

    # 3. Body line (50% chance — don't overcrowd)
    if random.random() > 0.5:
        lines.append(random.choice(BODY_LINES))

    # 4. CTA
    lines.append(random.choice(CTAs))

    return "\n\n".join(lines)


def generate_unique_caption(brand: str, state: dict, products: list[str]) -> str:
    for _ in range(30):
        caption = build_caption(brand, products)
        if caption not in state["used_captions"]:
            state["used_captions"].append(caption)
            return caption
    # Reset if stuck
    state["used_captions"] = []
    caption = build_caption(brand, products)
    state["used_captions"].append(caption)
    return caption


def generate_unique_hashtags(brand: str, state: dict, products: list[str]) -> str:
    for _ in range(20):
        tags = build_hashtag_set(brand, products)
        tag_str = " ".join(tags)
        if tag_str not in state["used_hashtags"]:
            state["used_hashtags"].append(tag_str)
            return tag_str
    state["used_hashtags"] = []
    tags = build_hashtag_set(brand, products)
    tag_str = " ".join(tags)
    state["used_hashtags"].append(tag_str)
    return tag_str


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def generate(brand: str, ad_filenames: list[str] = None, products: list[str] = None, dry_run: bool = False):
    if products is None:
        products = []

    # Extract real products from instruction files
    if ad_filenames:
        for fname in ad_filenames:
            extracted = extract_products_from_instruction_file(brand, fname)
            for p in extracted:
                if p not in products:
                    products.append(p)
        themes = extract_key_themes(brand, ad_filenames)
    else:
        themes = []

    state = load_state(brand)

    caption = generate_unique_caption(brand, state, products)
    hashtags = generate_unique_hashtags(brand, state, products) if hashtags_allowed(brand) else ""

    if not dry_run:
        save_state(brand, state)

    return {
        "caption": caption,
        "hashtags": hashtags,
        "brand": brand,
        "products": products,
        "themes": themes,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate unique brand caption + hashtags")
    parser.add_argument("--brand", required=True, help="Brand slug")
    parser.add_argument("--ad-files", help="Comma-separated ad filenames to analyze")
    parser.add_argument("--products", help="Comma-separated product names (fallback)")
    parser.add_argument("--dry-run", action="store_true", help="Don't save state")
    args = parser.parse_args()

    ad_files = [f.strip() for f in args.ad_files.split(",")] if args.ad_files else None
    products = [p.strip() for p in args.products.split(",")] if args.products else []

    result = generate(args.brand, ad_files, products, args.dry_run)

    print(f"\nCaption:\n{result['caption']}\n")
    print(f"Hashtags:\n{result['hashtags']}")
    if result.get("products"):
        print(f"\nProducts: {', '.join(result['products'])}")

    if args.dry_run:
        print("\n(dry-run — state not saved)")
