#!/usr/bin/env python3
"""
Creative Director: Compose Instagram posts from approved ads.

Reads all approved ad sidecars, feeds them to an LLM as a creative director,
and the LLM decides how to group ads into posts based on visual narrative flow.

Usage:
  python3 skill/scripts/compose_posts.py --brand island-splash --min-ads 50
  python3 skill/scripts/compose_posts.py --brand island-splash --dry-run
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Use persistent volumes when on Railway
DATA_DIR = os.environ.get("DATA_DIR", "")
if DATA_DIR:
    OUTPUT_DIR = Path(DATA_DIR) / "output"
else:
    OUTPUT_DIR = REPO_ROOT / "output"

ADS_DIR = OUTPUT_DIR
POSTS_DIR = OUTPUT_DIR / "posts"


def get_approved_ads(brand: str) -> set[str]:
    """Get set of ad filenames that are approved (ready for composition)."""
    approval_file = OUTPUT_DIR / "ad-approval" / f"{brand}.json"
    if not approval_file.exists():
        return set()
    try:
        data = json.loads(approval_file.read_text())
        approved = set()
        for fname in data.get("approved", []):
            base = fname.replace(".png", "").replace(".jpg", "")
            approved.add(base)
            approved.add(base + ".png")
            approved.add(base + ".instructions")
        return approved
    except Exception as e:
        print(f"Warning: failed to read approval file: {e}")
        return set()


def get_consumed_ads(brand: str) -> set[str]:
    """Get set of ad filenames already reserved by active composed posts."""
    consumed = set()
    if POSTS_DIR.exists():
        for post_file in POSTS_DIR.glob(f"{brand}_*.json"):
            try:
                data = json.loads(post_file.read_text())
            except Exception:
                continue
            posts = data if isinstance(data, list) else data.get("posts", [])
            for post in posts:
                if post.get("status") == "rejected":
                    continue
                for fname in post.get("ad_filenames", []):
                    base = fname.replace(".png", "").replace(".jpg", "").replace(".instructions", "")
                    consumed.add(base)
                    consumed.add(base + ".png")
                    consumed.add(base + ".instructions")

    # Backward compatibility for older approval files that already have a
    # consumed bucket from the previous compose behavior.
    approval_file = OUTPUT_DIR / "ad-approval" / f"{brand}.json"
    if not approval_file.exists():
        return consumed
    try:
        data = json.loads(approval_file.read_text())
        for fname in data.get("consumed", []):
            base = fname.replace(".png", "").replace(".jpg", "")
            consumed.add(base)
            consumed.add(base + ".png")
            consumed.add(base + ".instructions")
        return consumed
    except Exception as e:
        print(f"Warning: failed to read approval file: {e}")
        return set()


def mark_ads_consumed(brand: str, ad_keys: list[str]):
    """Mark ads as consumed in the approval JSON so they disappear from pool."""
    approval_file = OUTPUT_DIR / "ad-approval" / f"{brand}.json"
    if not approval_file.exists():
        print(f"Warning: approval file not found: {approval_file}")
        return
    try:
        data = json.loads(approval_file.read_text())
        matched = 0
        for key in ad_keys:
            base = key.replace(".instructions", "").replace(".png", "").replace(".jpg", "")
            # Remove from approved, add to consumed
            if base + ".png" in data.get("approved", []):
                data["approved"].remove(base + ".png")
                data.setdefault("consumed", []).append(base + ".png")
                matched += 1
            elif base in data.get("approved", []):
                data["approved"].remove(base)
                data.setdefault("consumed", []).append(base)
                matched += 1
            else:
                print(f"  Warning: ad not found in approved: {base}")
        approval_file.write_text(json.dumps(data, indent=2))
        print(f"Marked {matched}/{len(ad_keys)} ads as consumed")
    except Exception as e:
        print(f"Error marking ads as consumed: {e}")


def extract_all_sidecars(brand: str) -> list[dict]:
    """Read all ad sidecar files and build catalog, excluding consumed ads."""
    if not ADS_DIR.exists():
        return []
    
    approved = get_approved_ads(brand)
    consumed = get_consumed_ads(brand)
    print(f"Approved pool: {len(approved)} ads | Already consumed: {len(consumed)} ads")
    
    sidecars = []
    # Sidecars are directly in output/ with brand prefix in filename
    for sidecar in ADS_DIR.glob("*.instructions.txt"):
        if brand not in sidecar.stem and not any(b in sidecar.stem for b in ['island-splash', 'splash_20', 'cinco-h-ranch', 'cinco_20']):
            continue
        if brand == 'island-splash' and not any(x in sidecar.stem for x in ['island-splash', 'splash_20']):
            continue
        if brand == 'cinco-h-ranch' and not any(x in sidecar.stem for x in ['cinco-h-ranch', 'cinco_20']):
            continue
        
        # Skip consumed ads
        stem = sidecar.stem
        if stem in consumed or stem.replace(".instructions", "") in consumed:
            continue
        
        # Only include ads that are in the approved pool
        if stem not in approved and stem.replace(".instructions", "") not in approved:
            continue
        
        try:
            content = sidecar.read_text()
            image_filename = sidecar.name.replace(".instructions.txt", ".png")
            ad = {
                "filename": image_filename,
                "path": str(sidecar),
                "products": [],
                "mood": "",
                "headline": "",
                "vibe_keywords": [],
                "visual_theme": "",
            }

            # Extract PRODUCTS
            m = re.search(r"^PRODUCTS:\s*(.+)$", content, re.MULTILINE)
            if m:
                ad["products"] = [p.strip() for p in m.group(1).split(",")]

            # Extract MOOD
            m = re.search(r"^MOOD:\s*(.+)$", content, re.MULTILINE)
            if m:
                ad["mood"] = m.group(1).strip()

            # Extract TEXT STRATEGY / headline
            m = re.search(r"HEADLINE:\s*(.+)$", content, re.MULTILINE)
            if m:
                ad["headline"] = m.group(1).strip()

            # Extract VIBE SHIFT keywords
            m = re.search(r"VIBE SHIFT:\s*(.+?)(?:\n\n|\n[A-Z]|$)", content, re.MULTILINE | re.DOTALL)
            if m:
                keywords = re.findall(r'\b\w+\b', m.group(1))
                ad["vibe_keywords"] = [k for k in keywords if len(k) > 3][:10]

            # Extract visual theme from REVERSE ANALYSIS
            m = re.search(r"REVERSE ANALYSIS:\s*(.+?)(?:\n\n|\n[A-Z]|$)", content, re.MULTILINE | re.DOTALL)
            if m:
                ad["visual_theme"] = m.group(1).strip()[:200]

            sidecars.append(ad)
        except Exception as e:
            print(f"Warning: failed to read {sidecar}: {e}")

    return sidecars


def build_creative_catalog(sidecars: list[dict]) -> str:
    """Build a readable catalog for the LLM creative director."""
    lines = ["# Island Splash Ad Catalog\n"]
    lines.append(f"Total ads: {len(sidecars)}\n")
    lines.append("=" * 50 + "\n\n")

    for i, ad in enumerate(sidecars, 1):
        lines.append(f"## Ad {i}: {ad['filename']}")
        lines.append(f"Products: {', '.join(ad['products']) or 'N/A'}")
        lines.append(f"Mood: {ad['mood'] or 'N/A'}")
        lines.append(f"Headline: {ad['headline'] or 'N/A'}")
        lines.append(f"Vibe: {', '.join(ad['vibe_keywords']) or 'N/A'}")
        lines.append(f"Visual: {ad['visual_theme'] or 'N/A'}")
        lines.append("")

    return "\n".join(lines)


def get_creative_direction_prompt(brand: str, catalog: str) -> str:
    """Generate the creative director prompt."""
    brand_configs = {
        "island-splash": {
            "name": "Island Splash",
            "desc": "Florida Caribbean juice brand — fun, laid-back, tropical, island time",
            "hashtags": "#IslandSplash #TropicalFlavors #CaribbeanJuice #NaturalIngredients",
            "voice": "tropical paradise, escape to the islands, Caribbean vibes",
        },
        "cinco-h-ranch": {
            "name": "Cinco H Ranch",
            "desc": "Texas ranch skincare — honest, no fluff, historic homestead recipes",
            "hashtags": "#CincoHRanch #TexasMade #NaturalSkincare #RanchStandard",
            "voice": "Texas pride, homestead heritage, honest ingredients",
        },
    }

    cfg = brand_configs.get(brand, brand_configs["island-splash"])

    return f"""You are the creative director for {cfg['name']}, a {cfg['desc']} brand.

Your job: Compose Instagram carousel posts from this catalog of generated ads.

Rules:
- A post must be a CAROUSEL of 2-4 images. No solo posts.
- Make 4-8 posts total — more posts is better than fewer
- EVERY ad in the catalog MUST be assigned to exactly one post
- Group ads by VISUAL NARRATIVE FLOW — what story does this sequence tell as someone swipes through?
- Consider: color harmony, visual rhythm, product variety, mood consistency
- Distribute ads evenly — aim for 3 images per carousel

Brand voice: {cfg['voice']}

Output format (JSON):
{{
  "posts": [
    {{
      "post_id": "post_1",
      "ad_filenames": ["file1", "file2", "file3", "file4", "file5"],  // 4-8 images
      "post_type": "carousel",
      "creative_concept": "One sentence explanation of why these ads go together and what story they tell",
      "caption_angle": "The narrative angle for the caption — what should the caption emphasize?",
      "recommended_slots": ["morning", "evening", "anytime"]  // suggested posting time
    }}
  ]
}}

IMPORTANT:
- Return ONLY valid JSON, no markdown code blocks
- Every ad_filename must match exactly with the catalog
- All posts must be carousels of 4-8 images
- When 3+ ads share visual or narrative elements, group them together

---

{catalog}
"""


def call_llm(prompt: str, model: str = "gemini-2.5-flash") -> str:
    """Call LLM with the creative director prompt."""
    from google.genai import Client

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Need GEMINI_API_KEY")

    client = Client(api_key=api_key)

    full_prompt = """You are an expert Instagram creative director with 10+ years of experience composing visual content for brands.

CRITICAL: Return ONLY valid JSON, no markdown code blocks, no explanations outside the JSON.

""" + prompt

    response = client.models.generate_content(
        model=model,
        contents=full_prompt,
    )

    return response.text


def parse_llm_response(response: str) -> dict:
    """Parse LLM JSON output."""
    # Strip markdown code blocks if present
    response = re.sub(r"```json\s*", "", response)
    response = re.sub(r"```\s*", "", response)
    return json.loads(response.strip())


def generate_captions_for_posts(brand: str, posts: list[dict]) -> list[dict]:
    """Generate captions for each composed post."""
    # Import and run generate_caption for each post
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from skill.scripts.generate_caption import generate

    for post in posts:
        ad_files = post.get("ad_filenames", [])
        if ad_files:
            # Strip .instructions.txt extension for generate_caption
            clean_files = [f.replace(".instructions.txt", "").replace(".instructions", "") for f in ad_files]
            result = generate(brand, clean_files, [], dry_run=False)
            post["caption"] = result["caption"]
            post["hashtags"] = result["hashtags"]
            print(f"  Generated caption for {post['post_id']}: {post['caption'][:50]}...")
        else:
            post["caption"] = ""
            post["hashtags"] = ""

    return posts


def load_brand(brand_slug: str) -> dict:
    path = REPO_ROOT / "brands" / f"{brand_slug}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def create_posts(brand: str, post_groups: list[dict], dry_run: bool = False, generate_captions: bool = True) -> dict:
    """Save composed posts to disk as review drafts.

    Compose is intentionally non-publishing. It does not schedule posts and it
    does not consume approved ads. Human approval happens in the posts UI.
    """
    POSTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = POSTS_DIR / f"{brand}_{timestamp}.json"
    brand_cfg = load_brand(brand)
    platforms = brand_cfg.get("scheduling", {}).get("platforms", ["instagram"])

    review_posts = []
    for idx, post in enumerate(post_groups, 1):
        original_id = post.get("post_id") or f"post_{idx}"
        post["post_id"] = f"{timestamp}_{original_id}"
        post["status"] = "needs_review"
        post["platforms"] = platforms
        post["platform_status"] = {
            platform: {
                "status": "pending",
                "scheduled_at": None,
                "posted_at": None,
                "external_post_id": None,
                "last_error": None,
            }
            for platform in platforms
        }
        post["approved_at"] = None
        post["approved_by"] = None
        post["scheduled_at"] = None
        post["created_at"] = datetime.now().isoformat()
        post["revision_history"] = []
        review_posts.append(post)

    posts_data = {
        "brand": brand,
        "created_at": datetime.now().isoformat(),
        "posts": review_posts,
        "total_ads_used": sum(len(p["ad_filenames"]) for p in review_posts),
        "total_posts": len(review_posts),
    }

    if not dry_run:
        output_file.write_text(json.dumps(posts_data, indent=2))
        print(f"Posts saved to: {output_file}")
    else:
        print(f"[DRY RUN] Would save {len(review_posts)} review posts")

    return posts_data


def run_compose(brand: str, min_ads: int = 50, dry_run: bool = False, model: str = "gemini-2.5-flash"):
    """Main composition flow."""
    print(f"\n=== Creative Director: {brand} ===")
    print(f"Minimum ads required: {min_ads}")

    # Step 1: Read all sidecars
    print("\n[1/3] Reading ad sidecars...")
    sidecars = extract_all_sidecars(brand)
    print(f"Found {len(sidecars)} ads")

    if len(sidecars) < min_ads:
        print(f"Not enough ads: have {len(sidecars)}, need {min_ads}")
        print("Run again with --min-ads lower to force composition")
        if dry_run:
            return
        resp = input(f"Continue anyway? (y/n): ")
        if resp.lower() != "y":
            sys.exit(0)

    # Step 2: Build catalog and call LLM
    print("\n[2/3] Consulting creative director...")
    catalog = build_creative_catalog(sidecars)
    prompt = get_creative_direction_prompt(brand, catalog)

    if dry_run:
        print("\n[Dry run] Would send catalog to LLM:")
        print(f"Catalog has {len(sidecars)} ads")
        print(f"Prompt preview:\n{prompt[:500]}...")
        return

    try:
        response = call_llm(prompt, model)
        post_groups = parse_llm_response(response)
    except Exception as e:
        print(f"Error calling LLM: {e}")
        sys.exit(1)

    # Step 3: Generate captions for each post
    print("\n[3/4] Generating captions...")
    post_groups["posts"] = generate_captions_for_posts(brand, post_groups["posts"])

    # Step 4: Save posts
    print("\n[4/4] Creating posts...")
    result = create_posts(brand, post_groups["posts"], dry_run)

    # Summary
    print(f"\n=== Composition Complete ===")
    print(f"Posts created: {result['total_posts']}")
    print(f"Ads used: {result['total_ads_used']}/{len(sidecars)}")
    print()
    for post in post_groups["posts"]:
        print(f"  {post['post_id']}: {len(post['ad_filenames'])} images - {post.get('caption', 'no caption')[:50]}...")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compose Instagram posts with creative direction")
    parser.add_argument("--brand", required=True, help="Brand slug")
    parser.add_argument("--min-ads", type=int, default=50, help="Minimum ads required before composing")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without calling LLM")
    parser.add_argument("--model", default="gemini-2.5-flash", help="LLM model to use")
    args = parser.parse_args()

    run_compose(args.brand, args.min_ads, args.dry_run, args.model)
