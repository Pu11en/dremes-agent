#!/usr/bin/env python3
"""Dremes -- multi-brand sequential ad generator.

Usage:
  python3 dremes_agent.py --brand <slug> <ref_image_path>
  python3 dremes_agent.py --brand <slug> --pool

Brand configs live at brands/<slug>.json. Default brand is `island-splash`.

Pipeline (per ref):
  1. Pick product(s) from per-brand tally (count matches ref product count)
  2. Reverse-engineer analysis (subject, composition, decorative elements)
  3. Composer (gemini-2.5-pro) builds final image-gen prompt from brand config
  4. Pre-gen forbidden-text scan (errors abort, warnings flag)
  5. gemini-3-pro-image-preview generates the image (primary), GPT Image 2 fallback
  6. Save sidecar (ref + analyses + final prompt) + update tally
  7. Print output image path on last line (for the Hermes thin-trigger skill)
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image

ENV_PATH = Path(__file__).resolve().parent / ".env"
if ENV_PATH.exists():
    with ENV_PATH.open() as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)

from google.genai import Client
from google.genai.types import Blob, ImageConfig, Part

REPO_ROOT = Path(__file__).resolve().parent
BRANDS_DIR = REPO_ROOT / "brands"

# Use persistent volume for output when on Railway
DATA_DIR = os.environ.get("DATA_DIR", "")
if DATA_DIR:
    OUTPUT_DIR = Path(DATA_DIR) / "output"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
else:
    OUTPUT_DIR = REPO_ROOT / "output"

REFS_VOLUME = os.environ.get("REFS_VOLUME", "")
if REFS_VOLUME:
    REFS_PUBLIC_DIR = Path(REFS_VOLUME) / "public" / "images" / "refs"
else:
    REFS_PUBLIC_DIR = REPO_ROOT / "website" / "public" / "images" / "refs"

POOL_PROCESSED_DIRNAME = "used"
POOL_EXTS = (".jpg", ".jpeg", ".png", ".webp")
POOL_PACING_SECONDS = 20

TEXT_MODEL = "gemini-2.5-pro"
VISION_MODEL = "gemini-2.5-pro"
IMAGE_MODEL = "gemini-3-pro-image-preview"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_TEXT_MODEL = "google/gemini-2.5-pro"
OR_IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"

DEFAULT_BRAND = "island-splash"


# ── Telegram notifications ──────────────────────────────────────────────────

def _telegram_target_chat_id() -> str:
    return os.environ.get("NOTIFY_CHAT_ID", "") or os.environ.get("TELEGRAM_HOME_CHANNEL", "")


def _telegram_bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _send_telegram_message(text: str) -> bool:
    token = _telegram_bot_token()
    chat_id = _telegram_target_chat_id()
    if not token or not chat_id:
        return False
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[dremes] telegram message failed: {e}", file=sys.stderr)
        return False


def _send_telegram_photo(photo_path: Path, caption: str) -> bool:
    token = _telegram_bot_token()
    chat_id = _telegram_target_chat_id()
    if not token or not chat_id or not photo_path.exists():
        return False

    boundary = f"----dremes{int(time.time() * 1000)}"
    fields = [
        ("chat_id", chat_id),
        ("caption", caption[:1024]),
    ]
    body = bytearray()
    for name, value in fields:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"\r\n'
        "Content-Type: image/png\r\n\r\n"
        .encode()
    )
    body.extend(photo_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[dremes] telegram photo failed for {photo_path}: {e}", file=sys.stderr)
        return False


# ── Brand config loading ─────────────────────────────────────────────────────

def load_brand(slug: str) -> dict:
    path = BRANDS_DIR / f"{slug}.json"
    if not path.exists():
        raise RuntimeError(f"brand config not found: {path}")
    cfg = json.loads(path.read_text())
    cfg.setdefault("paths", {})
    cfg.setdefault("identity", {})
    cfg.setdefault("global_forbidden_text", [])
    cfg.setdefault("ad_creative_rules", [])
    cfg.setdefault("products", [])
    return cfg


def log(brand: dict, msg: str) -> None:
    print(f"[{brand['slug']}] {msg}", file=sys.stderr)


# ── Gemini client + retry/fallback ───────────────────────────────────────────

def _client() -> Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not loaded from profile .env")
    return Client(api_key=key)


RETRY_BACKOFFS = [5, 15, 45, 90]
TRANSIENT_MARKERS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED", "high demand", "quota")


def _is_transient(err: Exception) -> bool:
    msg = str(err)
    return any(m.lower() in msg.lower() for m in TRANSIENT_MARKERS)


def _with_retry(fn, label: str):
    last_err: Exception | None = None
    for i, delay in enumerate([0] + RETRY_BACKOFFS):
        if delay:
            print(f"[dremes] {label} retry {i}/{len(RETRY_BACKOFFS)} after {delay}s…", file=sys.stderr)
            time.sleep(delay)
        try:
            return fn()
        except Exception as e:
            last_err = e
            if not _is_transient(e):
                raise
            print(f"[dremes] {label} transient: {e}", file=sys.stderr)
    raise RuntimeError(f"{label} failed after {len(RETRY_BACKOFFS)} retries: {last_err}")


def _with_fallback(label: str, primary, fallback, tertiary=None):
    try:
        return _with_retry(primary, f"{label}/primary")
    except Exception as e:
        print(f"[dremes] {label} primary failed ({e}) -- falling back to secondary", file=sys.stderr)
        try:
            return _with_retry(fallback, f"{label}/secondary")
        except Exception as e2:
            if tertiary:
                print(f"[dremes] {label} secondary failed ({e2}) -- falling back to tertiary", file=sys.stderr)
                try:
                    return _with_retry(tertiary, f"{label}/tertiary")
                except Exception as e3:
                    raise RuntimeError(f"{label} all providers failed. primary: {e}; secondary: {e2}; tertiary: {e3}")
            raise RuntimeError(f"{label} both providers failed. primary: {e}; secondary: {e2}")


from openai import OpenAI  # noqa: E402


# ── OpenAI helpers ───────────────────────────────────────────────────────────

def _openai_key() -> str:
    k = os.environ.get("OPENAI_API_KEY")
    if not k:
        raise RuntimeError("OPENAI_API_KEY not loaded from environment")
    return k


def _openai_image_call(prompt: str, image_paths: list[str]) -> bytes:
    """Generate image using OpenAI GPT Image 2 with reference images as input."""
    client = OpenAI(api_key=_openai_key())
    content = [{"type": "text", "text": prompt}]
    for path in image_paths:
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{img_b64}"}
        })
    response = client.responses.create(
        model="gpt-image-2",
        input=[{"role": "user", "content": content}],
        temperature=1,
    )
    for output in response.output:
        if output.type == "image":
            return base64.b64decode(output.image_url.split(",", 1)[1])
    raise RuntimeError(f"OpenAI returned no image: output_types={[o.type for o in response.output]}")


# ── Gemini helpers ───────────────────────────────────────────────────────────

def _or_key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k:
        raise RuntimeError("OPENROUTER_API_KEY not loaded from profile .env")
    return k


def _or_datauri(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _or_post(payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {_or_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:4003",
            "X-Title": "dremes",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}")


def _or_text_call(model: str, system: str | None, user_parts: list) -> str:
    content = []
    for p in user_parts:
        if isinstance(p, str):
            content.append({"type": "text", "text": p})
        elif isinstance(p, dict) and "path" in p:
            content.append({"type": "image_url", "image_url": {"url": _or_datauri(p["path"])}})
        elif hasattr(p, "inline_data"):  # Part with Blob
            blob = p.inline_data
            b64 = base64.b64encode(blob.data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{blob.mime_type};base64,{b64}"}})
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    data = _or_post({"model": model, "messages": messages}, timeout=300)
    return data["choices"][0]["message"]["content"].strip()


def _or_image_call(model: str, prompt: str, image_paths: list[str]) -> bytes:
    content = []
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": _or_datauri(p)}})
    content.append({"type": "text", "text": prompt})
    data = _or_post(
        {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
        },
        timeout=600,
    )
    msg = data["choices"][0]["message"]
    for img in (msg.get("images") or []):
        url = (img.get("image_url") or {}).get("url") or img.get("url") or ""
        if url.startswith("data:"):
            return base64.b64decode(url.split(",", 1)[1])
    if isinstance(msg.get("content"), list):
        for part in msg["content"]:
            if part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    return base64.b64decode(url.split(",", 1)[1])
    raise RuntimeError(f"OpenRouter returned no image: keys={list(msg.keys())}")


def _image_part(path: str) -> Part:
    img = Image.open(path)
    buf = io.BytesIO()
    fmt = "PNG" if str(path).lower().endswith(".png") else "JPEG"
    img.save(buf, format=fmt)
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    return Part(inline_data=Blob(data=buf.getvalue(), mime_type=mime))


# ── Tally + product picking ──────────────────────────────────────────────────

def load_tally(brand: dict) -> dict:
    path = Path(brand["paths"]["tally_path"])
    if path.exists():
        return json.loads(path.read_text())
    return {p["name"]: 0 for p in brand["products"]}


def save_tally(brand: dict, tally: dict) -> None:
    path = Path(brand["paths"]["tally_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tally, indent=2))


def products_for_category(brand: dict, category: str | None = None) -> list[dict]:
    """Return products eligible for a pool/category.

    Brands like Island Splash have one broad category with all products.
    Brands like Cinco H Ranch use product.pool_slug to group products by category.
    """
    products = list(brand.get("products", []))
    if not category:
        return products
    scoped = [p for p in products if p.get("pool_slug") == category]
    return scoped or products


def pick_products(brand: dict, count: int, produce_hints: list[str], tally: dict, category: str | None = None) -> list[dict]:
    """Pick `count` products from brand -- pure least-used rotation.
    Produce hints used only as tiebreaker when usage counts are equal."""
    hints_lower = " ".join(produce_hints).lower()
    candidates = products_for_category(brand, category)
    if not candidates:
        raise RuntimeError(f"brand '{brand['slug']}' has no products configured")

    def keyword_score(prod: dict) -> int:
        return -sum(1 for kw in prod.get("keywords", []) if kw in hints_lower)

    ordered = sorted(
        candidates,
        key=lambda p: (tally.get(p["name"], 0), keyword_score(p), p["name"]),
    )
    selected: list[dict] = []
    while len(selected) < count:
        selected.extend(ordered)
    return selected[:count]


def find_product(brand: dict, name_or_trigger: str) -> dict | None:
    """Resolve a product by exact name (case-insensitive) or by trigger keyword."""
    needle = name_or_trigger.strip().lower()
    for p in brand["products"]:
        if p["name"].lower() == needle:
            return p
    for p in brand["products"]:
        if needle in [t.lower() for t in p.get("triggers", [])]:
            return p
    return None


def lock_products(brand: dict, count: int, locked_name: str) -> list[dict]:
    """Resolve `locked_name` to a product, then return it cloned `count` times."""
    p = find_product(brand, locked_name)
    if not p:
        avail = ", ".join(prod["name"] for prod in brand["products"])
        raise RuntimeError(f"product '{locked_name}' not found in brand '{brand['slug']}'. Available: {avail}")
    return [p] * count


# ── Prompts ──────────────────────────────────────────────────────────────────

REVERSE_PROMPT = """You are reverse-engineering this reference ad so a creative team can rebuild it pixel-for-pixel with a different brand's products. Your analysis must be EXHAUSTIVE — a designer reading it should be able to recreate the ad without seeing the original.

Output in clean markdown. Describe ONLY what IS present. Do NOT invent, embellish, or add clichés. Note absences explicitly — knowing what is NOT there is as important as what is.

## Subject
The main focus — person, animal, object, or scene. Describe in granular detail:
- If person: age range, gender presentation, ethnicity/coloring, hair style and color, expression (specific — not just "happy" but "slight closed-mouth smile, eyes squinting"), pose (exact body position — "right hand held at chest height, palm up supporting the product"), clothing (color, fabric, style, neckline, sleeves)
- If object: material, finish, condition, exact arrangement
- Positioning: where in frame, facing direction, relation to other elements

## Product Count
PRODUCT COUNT: N (an exact integer. Count DISTINCT PRODUCT TYPES, not individual items. If 9 bars of the same soap are stacked, that is 1 product type. If a box + 3 identical bars, that is 2 types. Count only visually distinct product varieties — same label/same shape = same product. Bottles, jars, tubes, boxes, bars, packets each count. If NO products visible, say 0.)

## Product Details
For EACH product container, numbered, describe:
- Container type and shape (e.g. "cylindrical glass bottle, 8cm in frame, fluted neck, cork stopper")
- Exact position in frame (use percentages: "centered at 40% from left, 55% from top")
- Angle (facing forward, tilted 15° left, lying flat, etc.)
- Size relative to frame (very small/small/medium/large/hero — as % of frame height)
- Cap/lid state (on/off/open/partially unscrewed/missing)
- Label: shape, background color, border, illustration style
- Surface: glossy, matte, frosted, textured, condensation droplets, reflections

## Composition
Describe the FULL frame structure:
- Aspect ratio if identifiable
- Framing: tight close-up / medium / wide / environmental
- Visual hierarchy: what the eye hits first, second, third — and WHY (size, contrast, position)
- 3×3 grid — what occupies EACH of the nine zones. Be specific about what fills each zone, not just "empty space"
- Negative space: where is it, how much (% of frame), what purpose does it serve
- Depth layers: foreground, midground, background — what occupies each

## Produce / Ingredients
Every visible fruit, vegetable, herb, spice, grain, raw ingredient, or food item. For each:
- Name the ingredient
- Quantity (single, cluster of 3, scattered handful)
- Position in frame
- State (whole, sliced, peeled, bruised, dripping, fresh, dried)
If NONE visible, say: PRODUCE: NONE

## Text
Catalog EVERY visible text element. For each text zone:
- Zone label (top_banner, center_overlay, bottom_strip, corner_badge, product_label, fine_print, CTA_button, watermark)
- Exact position (% from top, % from left, alignment)
- Full verbatim text — copy every word exactly as it appears, preserving case, line breaks with /
- Font characteristics: family feel (serif/sans-serif/script/display/monospace), weight (thin/light/regular/medium/bold/heavy/black), style (italic, all-caps, title case, lowercase), approximate size relative to frame (tiny/fine-print/small/medium/large/headline/hero)
- Color (specific: hex if discernible, or descriptive like "warm cream #F5E6D3")
- Any text effects: drop shadow, outline, gradient fill, emboss, metallic
If NO text anywhere in the image, say: TEXT: NONE

## Decorative Elements
Every graphic element that is NOT product, produce, subject, text, or background. Include:
- Splashes, drips, droplets, splatter (liquid type, color, direction of motion)
- Sparkles, stars, glitter, particles (size, density, distribution)
- Bubbles, foam, suds
- Borders, frames, lines, dividers, geometric shapes
- Arrows, pointers, callout shapes
- Badges, seals, ribbons, banners
- Icons, symbols, emoji-like graphics
- Shadows: drop shadows, cast shadows, vignette edges (separate from lighting shadows)
- Overlays: gradients, light leaks, film grain, texture layers
For each, describe type, color, position, scale, density. If NONE, say: DECORATIVE: NONE

## Background
Exhaustive background description:
- Structure: solid color / gradient (direction + color stops) / environmental photo scene / abstract pattern / studio sweep
- Color(s): specific — hex if solid, gradient stops if gradient, dominant tones if photo
- Texture: smooth, rough, paper, concrete, wood grain, fabric weave, marble, water, sky
- Depth: flat, shallow, deep, infinite
- Any background objects: architectural features, furniture, landscape elements, props — describe each

## Lighting
Precise lighting breakdown:
- Key light direction (clock face: "from 10 o'clock, slightly above")
- Quality: hard (crisp defined shadows) / soft (diffused gentle falloff) / mixed
- Fill: presence, direction, ratio relative to key
- Rim/backlight: presence, color, intensity
- Highlights: where on products/subject, shape (round specular, linear, broad soft), intensity (bright/clipped, medium, subtle)
- Shadows: where they fall, softness of edge, length, opacity
- Overall exposure: bright and airy / balanced / moody and dark / high-contrast

## Mood
5–8 precise mood keywords. Not generic — specific and evocative. Examples: "sun-drenched Mediterranean terrace" not "sunny"; "quiet artisanal workshop at dawn" not "calm"."""


def _brand_voice_block(selected: list[dict], brand: dict) -> str:
    """Build brand voice guidance for freeform text generation."""
    lines = []
    lines.append("  Brand voice: " + brand.get('identity', {}).get('voice', 'plain and honest, no fluff'))
    for p in selected:
        note = p.get("voice_note", "")
        if note:
            lines.append("  " + p['name'] + ": " + note)
        else:
            lines.append("  " + p['name'] + ": use brand voice, keep it plain and honest")
    return "\n".join(lines)


def _product_text_block(selected: list[dict], brand: dict) -> str:
    """Build product-specific text guidance for the TEXT REPLACEMENT section.
    
    Brand headlines are the ONLY options for headline text.
    Product claims and voice_note are body-copy / supporting flavor — never headlines.
    real_ingredients for ingredient callouts.
    """
    lines = []
    
    # PRIMARY: Brand headlines — these are the ONLY headline options
    brand_headlines = brand["identity"].get("allowed_headlines", [])
    if brand_headlines:
        lines.append("  HEADLINE OPTIONS (pick 1-2, verbatim — THESE ARE THE ONLY HEADLINES YOU MAY USE):")
        for h in brand_headlines:
            lines.append(f"    • {h}")
        lines.append("")
    
    # SECONDARY: Product voice notes / claims — body copy only, never headlines
    for p in selected:
        name = p["name"]
        claims = p.get("real_claims", [])
        ingredients = p.get("real_ingredients", "")
        voice_note = p.get("voice_note", "")
        
        if voice_note:
            lines.append(f"  {name} — BODY COPY GUIDANCE (use in small text / callouts, NEVER as headline): {voice_note}")
        
        if ingredients:
            lines.append(f"  {name} — INGREDIENTS (use in small print or callout): {ingredients}")
    
    return "\n".join(lines) if lines else "  Use the product name as the primary text. Keep it simple and honest."



def build_composer_system(brand: dict, selected: list[dict], has_logo: bool = False, research_context: str | None = None) -> str:
    """Templated composer system prompt -- preserve / replace / adapt model."""
    name = brand["display_name"]
    palette = brand["identity"]["palette"]["description"]
    voice_block = _brand_voice_block(selected, brand)

    cap_lines = []
    for p in selected:
        cap_lines.append(f"    - {name} {p['name']} ({p['container']}): {p['cap_rule']}")
    cap_block = "\n".join(cap_lines) if cap_lines else f"    - All {name} products: see product config"

    # Collect forbidden text patterns
    forbidden_patterns = list(brand.get("global_forbidden_text", []))
    selected_names = {p["name"] for p in selected}
    for p in brand.get("products", []):
        if p["name"] in selected_names:
            forbidden_patterns.extend(p.get("forbidden_text", []))
    seen = set()
    unique_forbidden = []
    for item in forbidden_patterns:
        pat = item.get("pattern", "")
        if pat and pat.lower() not in seen:
            seen.add(pat.lower())
            unique_forbidden.append(item)
    if unique_forbidden:
        forbidden_lines = "\n".join(f"    - '{item['pattern']}' -- {item['reason']}" for item in unique_forbidden)
        forbidden_block = f"""FORBIDDEN WORDS -- NEVER use these anywhere in your output prompt:
{forbidden_lines}"""
    else:
        forbidden_block = ""

    research_block = ""
    if research_context:
        research_block = f"""
RESEARCH CONTEXT (on-demand research results — use to ground the ad in real-world accuracy):
{research_context}
"""

    return f"""You write prompts for an image model that transforms competitor ads into ads for {name}.
You are literal and precise. You ONLY use product names, claims, headlines, colors, and ingredients
EXACTLY as listed in the user's message. Never invent, never summarize, never paraphrase.

CRITICAL RULES:
- Use exactly the product count from the product list in the user's message. If the user lists 7 variants, use all 7.
- TEXT LAYOUT: Match the reference's typography pattern (line count, font contrast, positioning). TEXT WORDS: Must be verbatim from the HEADLINE OPTIONS list. Never substitute product-descriptive words like ingredients or claims as a headline.
- Every visible word must match the label artwork, the HEADLINE OPTIONS list, or the logo. Zero invented text.

Output ONLY a flat JSON object — no markdown, no explanation:
{{
  "scene": "one sentence describing the final ad",
  "products": [{{"product": "Exact product name from user's list", "input": "INPUT N", "position": "where in frame"}}],
  "text": [{{"zone": "headline or label position", "text": "Verbatim from the HEADLINE OPTIONS list — product claims are NEVER headlines", "style": "font and color"}}],
  "background": "background description using ONLY listed colors",
  "lighting": "lighting description",
  "decorative": ["adapted decorative elements"],
  "logo": "corner placement and size",
  "do_not": ["what must NOT appear"]
}}"""


# ── Analysis calls ───────────────────────────────────────────────────────────

def reverse_analyze(ref_path: str) -> str:
    def gemini():
        client = _client()
        resp = client.models.generate_content(
            model=VISION_MODEL,
            contents=[_image_part(ref_path), REVERSE_PROMPT],
        )
        return resp.candidates[0].content.parts[0].text.strip()

    def openrouter():
        return _or_text_call(OR_TEXT_MODEL, None, [{"path": ref_path}, REVERSE_PROMPT])

    return _with_fallback("reverse_analyze", gemini, openrouter)


# vibe_shift_analyze removed -- we use the reference's own lighting/composition directly
# no separate aesthetic layer

# ── Jina on-demand research ────────────────────────────────────────────────

def _jina_key() -> str | None:
    return os.environ.get("JINA_API_KEY") or None


def run_research(query: str, deep: bool = False) -> str | None:
    """Run Jina web research and return compact context for prompt injection.
    Returns None if Jina is not configured or research fails."""
    if not _jina_key():
        return None
    try:
        from skill.scripts.jina_research import research_topic, research_to_context
        result = research_topic(query, num_results=3, deep_read=deep)
        ctx = research_to_context(result, max_chars=3000)
        return ctx
    except Exception as e:
        print(f"[dremes] research failed (non-fatal): {e}", file=sys.stderr)
        return None


def _strip_md(text: str) -> str:
    return re.sub(r"\*+", "", text)


def parse_product_count(reverse_text: str) -> int:
    clean = _strip_md(reverse_text)
    m = re.search(r"PRODUCT COUNT:\s*(\d+)", clean, re.IGNORECASE)
    if m:
        return max(1, min(7, int(m.group(1))))
    return 1


def parse_produce(reverse_text: str) -> list[str]:
    clean = _strip_md(reverse_text)
    m = re.search(
        r"PRODUCE\s*/?\s*INGREDIENTS:\s*(.*?)(?=\n[A-Z][A-Z ]+:|\Z)",
        clean,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return []
    body = m.group(1).strip()
    if body.upper().startswith("NONE"):
        return []
    return [body]


def build_brand_rules_block(brand: dict) -> str:
    """Combine ad_creative_rules from the brand JSON with any legacy
    enforcement_additions from a rules_path file (Island Splash compat)."""
    parts: list[str] = [f"- {r}" for r in brand.get("ad_creative_rules", [])]

    rules_path = brand["paths"].get("rules_path")
    if rules_path:
        rp = Path(rules_path)
        if rp.exists():
            rules = json.loads(rp.read_text())
            for r in rules.get("enforcement_additions", []):
                if r.get("active"):
                    parts.append(f"- {r['text']}")
    return "\n".join(parts)


def _parse_composer_json(raw_text: str, brand: dict) -> dict:
    """Parse the composer's JSON output, with fallback extraction from markdown fences."""
    import re as _re

    # Strip markdown fences if present
    text = raw_text.strip()
    fence_m = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if fence_m:
        text = fence_m.group(1).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find the first { ... } block
    brace_m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if brace_m:
        try:
            return json.loads(brace_m.group(0))
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        f"composer did not return valid JSON for brand '{brand['slug']}'. "
        f"Raw response (first 500 chars): {raw_text[:500]}"
    )


def _assemble_prompt_from_json(brand: dict, ad: dict) -> str:
    """Assemble a structured ad JSON into the final image-generation prompt.

    Handles ALL formats the LLM might output:
    - Flat schema (scene, product_placement, text_zones, ...)
    - creative_brief (PRESERVE/REPLACE/ADAPT)
    - sections format (sections.layout/products/text_elements/visual_elements)
    - Universal fallback: stringify the entire JSON as a creative brief
    """
    palette = brand["identity"]["palette"]["description"]
    product_format = brand["identity"].get("format", "consumer product")
    display_name = brand["display_name"]

    scene = ""
    placements = []
    zones = []
    bg = ""
    deco = []
    lighting = ""
    mood = []
    logo = ""
    negative = []

    # ── Format detection and extraction ──

    # Format 1: Flat schema (the intended format)
    if "scene" in ad or "product_placement" in ad or "text_zones" in ad:
        scene = ad.get("scene", "")
        placements = ad.get("product_placement", [])
        zones = ad.get("text_zones", [])
        bg = ad.get("background", "")
        deco = ad.get("decorative", [])
        lighting = ad.get("lighting", "")
        mood = ad.get("mood", [])
        logo = ad.get("logo", "")
        negative = ad.get("negative", [])

    # Format 2: creative_brief (PRESERVE/REPLACE/ADAPT)
    elif "creative_brief" in ad:
        _extract_creative_brief(ad, scene_ref := [""], placements, zones, bg_ref := [""], deco, lighting_ref := [""], mood, logo_ref := [""], negative)
        scene = scene_ref[0]
        bg = bg_ref[0]
        lighting = lighting_ref[0]
        logo = logo_ref[0]

    # Format 3: sections format
    elif "sections" in ad:
        _extract_sections(ad, scene_ref := [""], placements, zones, bg_ref := [""], deco, lighting_ref := [""], mood, logo_ref := [""], negative)
        scene = scene_ref[0]
        bg = bg_ref[0]
        lighting = lighting_ref[0]
        logo = logo_ref[0]

    # Format 4: Universal fallback — pass JSON as clean creative brief
    if not scene and not placements and not zones:
        import json as _json
        json_text = _json.dumps(ad, indent=2)
        scene = f"Generate a {display_name} ad. Creative brief:\n{json_text}\n\nPaste product images exactly. Never redraw labels. Use ONLY the brand colors listed above."

    # ── Assemble final prompt ──
    lines = [f"Photorealistic 4:5 portrait ad. {scene}"]
    lines.append("")

    if placements:
        lines.append("PRODUCT PLACEMENT:")
        for pp in placements:
            if isinstance(pp, dict):
                name = pp.get("product", "Product")
                inp = pp.get("input_ref", "INPUT 2")
                pos = pp.get("position", "center")
                scale = pp.get("scale", "medium")
                state = pp.get("container_state", "")
                extra = f", {state}" if state else ""
                lines.append(f"- {name} ({inp}): {pos}, {scale}{extra}")
                lines.append(f"  PASTE {inp} image exactly — never redraw, recolor, or reinterpret the label.")
        lines.append("")

    if zones:
        lines.append("TEXT:")
        for tz in zones:
            if isinstance(tz, dict):
                zone = tz.get("zone", "overlay")
                style = tz.get("style", "")
                text = tz.get("text", "")
                style_str = f", {style}" if style else ""
                lines.append(f'- [{zone}]{style_str}: "{text}"')
        lines.append("")

    if bg:
        lines.append(f"BACKGROUND: {bg}")
        lines.append(f"Use ONLY brand color palette: {palette}. Apply 60-30-10 rule.")
        lines.append("")

    if deco:
        lines.append("DECORATIVE ELEMENTS:")
        for d in deco:
            lines.append(f"- {d}")
        lines.append("")

    if lighting:
        lines.append(f"LIGHTING: {lighting}")
        lines.append("")

    if mood:
        lines.append(f"MOOD: {', '.join(mood)}")
        lines.append("")

    if logo:
        lines.append(f"LOGO: {logo}")
        lines.append("")

    lines.append(f"PRODUCT: {product_format} — paste the EXACT product image, never redraw it.")
    lines.append("")

    # Inject global visual rules as hard negative constraints
    visual_rules = brand.get("identity", {}).get("global_visual_rules") or brand.get("global_visual_rules", [])
    if visual_rules:
        lines.append("HARD VISUAL RULES — VIOLATING THESE RUINS THE AD:")
        for rule in visual_rules:
            lines.append(f"- {rule}")
        lines.append("")

    for n in negative:
        if isinstance(n, str):
            lines.append(n)

    return "\n".join(lines)


def _extract_creative_brief(ad, scene_out, placements, zones, bg_out, deco, lighting_out, mood, logo_out, negative):
    """Extract flat fields from creative_brief (PRESERVE/REPLACE/ADAPT) format.
    
    Handles multiple nesting variants:
    - creative_brief.PRESERVE / creative_brief.REPLACE / creative_brief.ADAPT
    - execution_plan.PRESERVE / execution_plan.REPLACE / execution_plan.ADAPT
    - Top-level PRESERVE / REPLACE / ADAPT (already handled by format detection)
    """
    # Find the triage container — could be creative_brief, execution_plan, or ad itself
    triage = None
    for container_key in ("creative_brief", "execution_plan"):
        container = ad.get(container_key, {})
        if isinstance(container, dict) and ("PRESERVE" in container or "REPLACE" in container or "preserve" in container or "replace" in container):
            triage = container
            break
    if triage is None:
        # Try ad itself
        if isinstance(ad, dict) and ("PRESERVE" in ad or "REPLACE" in ad or "preserve" in ad):
            triage = ad
    
    if triage is None:
        return
    
    preserve = triage.get("PRESERVE", triage.get("preserve", {}))
    replace = triage.get("REPLACE", triage.get("replace", {}))
    adapt = triage.get("ADAPT", triage.get("adapt", {}))

    scene_parts = []
    if isinstance(preserve, dict):
        for k in ("composition", "framing", "camera_angle", "subject_pose"):
            v = preserve.get(k, "")
            if isinstance(v, str) and v:
                scene_parts.append(v)
        lighting_out[0] = preserve.get("lighting", "")
    scene_out[0] = " ".join(scene_parts) if scene_parts else str(preserve)

    if isinstance(replace, dict):
        subj = replace.get("subject", {})
        if isinstance(subj, dict):
            mapping = subj.get("product_mapping", [])
            if isinstance(mapping, list):
                for i, m in enumerate(mapping):
                    if isinstance(m, dict):
                        placements.append({
                            "product": m.get("replace_with", f"Product {i+1}"),
                            "input_ref": f"INPUT {i+2}",
                            "position": m.get("position", "center"),
                            "scale": "medium",
                            "container_state": subj.get("instruction", "")[:80],
                        })

        txt = replace.get("text", {})
        if isinstance(txt, dict):
            labels = txt.get("on_product_labels", "")
            if labels and isinstance(labels, str):
                zones.append({"zone": "product_label", "text": labels, "style": "product label text"})

        palette_block = replace.get("color_palette", {})
        if isinstance(palette_block, dict):
            bg_out[0] = palette_block.get("background", str(palette_block))
        else:
            bg_out[0] = str(palette_block)

        logo_block = replace.get("logo", {})
        logo_out[0] = str(logo_block) if logo_block else ""

    if isinstance(adapt, dict):
        deco_raw = adapt.get("decorative_elements", "")
        if isinstance(deco_raw, str) and deco_raw:
            deco.append(deco_raw)
        mood_raw = adapt.get("overall_mood", "")
        if isinstance(mood_raw, str):
            mood.extend([w.strip() for w in mood_raw.replace(",", " ").split() if len(w.strip()) > 2][:8])
        final_check = adapt.get("final_check", {})
        if isinstance(final_check, dict):
            for v in final_check.values():
                if isinstance(v, str):
                    negative.append(v)
                elif isinstance(v, list):
                    negative.extend([x for x in v if isinstance(x, str)])


def _extract_sections(ad, scene_out, placements, zones, bg_out, deco, lighting_out, mood, logo_out, negative):
    """Extract flat fields from sections format."""
    sections = ad.get("sections", {})
    if not isinstance(sections, dict):
        return

    # Concept as scene fallback
    concept = ad.get("creative_concept", "")
    scene_parts = [concept] if concept else []

    layout = sections.get("layout", {})
    if isinstance(layout, dict):
        for key in ("PRESERVE", "REPLACE", "ADAPT"):
            block = layout.get(key, {})
            if isinstance(block, dict):
                details = block.get("details", [])
                if isinstance(details, list):
                    for d in details:
                        if isinstance(d, str):
                            scene_parts.append(d)
                summary = block.get("summary", "")
                if isinstance(summary, str) and summary:
                    scene_parts.append(summary)

    scene_out[0] = " ".join(scene_parts) if scene_parts else str(sections)

    # Products
    products = sections.get("products", {})
    if isinstance(products, dict):
        product_list = products.get("product_list", [])
        if isinstance(product_list, list):
            for i, p in enumerate(product_list):
                if isinstance(p, dict):
                    placements.append({
                        "product": p.get("product_name", f"Product {i+1}"),
                        "input_ref": f"INPUT {i+2}",
                        "position": p.get("placement", "center"),
                        "scale": "medium",
                        "container_state": str(p.get("quantity", "")),
                    })

    # Text elements
    text_elements = sections.get("text_elements", {})
    if isinstance(text_elements, dict):
        elements = text_elements.get("elements", [])
        if isinstance(elements, list):
            for e in elements:
                if isinstance(e, dict):
                    zones.append({
                        "zone": e.get("zone", "overlay"),
                        "text": e.get("verbatim_text", ""),
                        "style": str(e.get("font_characteristics", "")),
                    })

    # Visual elements
    visual = sections.get("visual_elements", {})
    if isinstance(visual, dict):
        background = visual.get("background", {})
        if isinstance(background, dict):
            desc = background.get("description", "")
            palette_data = background.get("color_palette", {})
            bg_parts = [desc]
            if isinstance(palette_data, dict):
                bg_parts.extend(str(v) for v in palette_data.values())
            bg_out[0] = " ".join(bg_parts)

        lighting_data = visual.get("lighting", {})
        if isinstance(lighting_data, dict):
            lighting_out[0] = lighting_data.get("description", str(lighting_data))
        elif isinstance(lighting_data, str):
            lighting_out[0] = lighting_data

        deco_elements = visual.get("decorative_elements", [])
        if isinstance(deco_elements, list):
            for d in deco_elements:
                if isinstance(d, dict):
                    deco.append(d.get("description", str(d)))
                elif isinstance(d, str):
                    deco.append(d)

    # Brand guidelines → negative constraints
    guidelines = sections.get("brand_guidelines", {})
    if isinstance(guidelines, dict):
        rules = guidelines.get("rules", [])
        if isinstance(rules, list):
            for r in rules:
                if isinstance(r, str):
                    negative.append(r)

    # Final checks → negative constraints
    checks = sections.get("final_checks", [])
    if isinstance(checks, list):
        for c in checks:
            if isinstance(c, str):
                negative.append(c)

    # Logo from text_elements
    if isinstance(text_elements, dict):
        elements = text_elements.get("elements", [])
        if isinstance(elements, list):
            for e in elements:
                if isinstance(e, dict) and e.get("zone") == "brand_logo":
                    logo_out[0] = e.get("placement", "bottom-right corner")


def compose_prompt(
    brand: dict,
    selected: list[dict],
    reverse: str,
    ref_path: str,
    product_paths: list[str],
    logo_path: str | None,
    research_context: str | None = None,
) -> dict:
    """Compose the structured ad JSON from reference analysis + brand config.

    Returns a dict with fields: ad_concept, scene, product_placement, text_zones,
    background, decorative, lighting, mood, logo, negative.
    """
    rules_block = build_brand_rules_block(brand)
    product_names = ", ".join(p["name"] for p in selected)
    product_text_block = _product_text_block(selected, brand)

    # Build image parts for the composer call
    image_parts: list = [_image_part(ref_path)]
    for pp in product_paths:
        image_parts.append(_image_part(pp))
    has_logo = bool(logo_path and os.path.exists(logo_path))
    if has_logo:
        image_parts.append(_image_part(logo_path))

    system = build_composer_system(brand, selected, has_logo, research_context=research_context)
    
    # Build per-product data block with full details
    product_data_lines = []
    for i, p in enumerate(selected):
        claims = " / ".join(f'"{c}"' for c in p.get("real_claims", []))
        ingredients = p.get("real_ingredients", "")
        product_data_lines.append(
            f"[Product {i+1}] {p['name']} | {p['container']} | "
            f"Claims: {claims} | "
            f"Ingredients: {ingredients}"
        )
    product_data_block = "\n".join(product_data_lines)
    
    # Build headline options
    headlines = brand["identity"].get("allowed_headlines", [])
    headline_block = "\n".join(f'  "{h}"' for h in headlines)
    
    # Brand rules (just the creative rules, not methodology)
    rules = brand.get("ad_creative_rules", [])
    rules_block = "\n".join(f"- {r}" for r in rules)
    
    palette = brand["identity"]["palette"]["description"]
    
    user_parts: list = [
        *image_parts,
        f"""REFERENCE ANALYSIS:
{reverse}

SELECTED PRODUCTS (use these exact names only):
{product_data_block}

BRAND HEADLINES (verbatim only):
{headline_block}

BRAND COLORS (use only these):
{palette}

LOGO: {brand['display_name']} logo in bottom-right corner, small, no box, no banner.

BRAND RULES:
{rules_block}

Build a flat JSON describing the ad. Use ONLY the product names, claims, headlines, and colors listed above. Output ONLY the JSON.""",
    ]

    def gemini():
        client = _client()
        contents: list = user_parts  # type: ignore[assignment]
        resp = client.models.generate_content(
            model=TEXT_MODEL,
            contents=contents,
        )
        return resp.candidates[0].content.parts[0].text.strip()

    def openrouter():
        return _or_text_call(OR_TEXT_MODEL, system, user_parts)

    raw_text = _with_fallback("compose_prompt", gemini, openrouter)
    return _parse_composer_json(raw_text, brand)


# ── Pre-generation forbidden-text scan ───────────────────────────────────────

def collect_forbidden_patterns(brand: dict, selected: list[dict]) -> list[dict]:
    patterns: list[dict] = []
    patterns.extend(brand.get("global_forbidden_text", []))
    selected_names = {p["name"] for p in selected}
    for p in brand["products"]:
        if p["name"] in selected_names:
            patterns.extend(p.get("forbidden_text", []))
    return patterns


def _pattern_to_regex(needle: str) -> str:
    """Convert a plain-text needle to a regex that matches the needle literally,
    except '#' which is treated as a hashtag (not hex) via a lookaround.
    Pure alphabetic patterns get whitespace boundaries to avoid false positives
    inside hyphenated or compound words (e.g., 'FREE' matching 'noise-free')."""
    if needle == "#":
        # Match # only when NOT preceded or followed by a hex digit (i.e., not part of a color code like #F0E0B0)
        return r"(?<![0-9A-Fa-f])#(?![0-9A-Fa-f])"
    escaped = re.escape(needle)
    # If the pattern is purely alphabetic, enforce whitespace boundaries
    if needle.isalpha():
        return r"(?<![^\s])" + escaped + r"(?![^\s])"
    return escaped


def _strip_forbidden_block(text: str) -> str:
    """Remove blocks that legitimately contain forbidden strings as negative
    instructions: FORBIDDEN IN OUTPUT, STRICT CONSTRAINTS, and TEXT CONTENT GUIDANCE."""
    # Known major section headers in the composer prompt
    section_pattern = r"(?:FORBIDDEN\s+IN\s+OUTPUT|STRICT\s+CONSTRAINTS|REFERENCE\s+SUBJECT|PRODUCT\s+REPLACEMENT|TEXT\s+CONTENT\s+GUIDANCE|TEXT\s+STRATEGY|LOGO|STYLE\s+OVERLAY)"
    # Strip FORBIDDEN IN OUTPUT block
    text = re.sub(
        r"FORBIDDEN\s+IN\s+OUTPUT.*?\n(?=\s*" + section_pattern + r")",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip STRICT CONSTRAINTS block
    text = re.sub(
        r"STRICT\s+CONSTRAINTS:\s*.*?\n(?=\s*" + section_pattern + r")",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip TEXT CONTENT GUIDANCE block
    text = re.sub(
        r"TEXT\s+CONTENT\s+GUIDANCE:\s*.*?\n(?=\s*" + section_pattern + r")",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text


def scan_ad_json_forbidden(ad_json: dict, patterns: list[dict]) -> list[dict]:
    """Scan text fields in the structured ad JSON for forbidden patterns.

    Checks scene, text_zones[].text, background, decorative, and ad_concept.
    Returns list of {pattern, severity, reason, field} hits.
    """
    hits: list[dict] = []
    fields_to_scan = [
        ("ad_concept", ad_json.get("ad_concept", "")),
        ("scene", ad_json.get("scene", "")),
        ("background", ad_json.get("background", "")),
    ]
    for zone in ad_json.get("text_zones", []):
        fields_to_scan.append((f"text_zones.{zone.get('zone', '?')}", zone.get("text", "")))
    for deco in ad_json.get("decorative", []):
        fields_to_scan.append(("decorative", str(deco)))
    for neg in ad_json.get("negative", []):
        fields_to_scan.append(("negative", str(neg)))

    for field_name, haystack in fields_to_scan:
        if not haystack:
            continue
        if not isinstance(haystack, str):
            haystack = str(haystack)
        haystack_lower = haystack.lower()
        for p in patterns:
            needle = str(p.get("pattern", ""))
            if not needle:
                continue
            regex = _pattern_to_regex(needle)
            if re.search(regex, haystack_lower):
                hits.append({
                    "pattern": p["pattern"],
                    "severity": p.get("severity", "warning"),
                    "reason": p.get("reason", ""),
                    "field": field_name,
                })
    return hits


def scan_forbidden(text: str, patterns: list[dict]) -> list[dict]:
    """Return list of {pattern, severity, reason} hits found in text (case-insensitive).
    Uses regex matching; '#' is treated specially to exclude hex color codes.
    The FORBIDDEN IN OUTPUT block is excluded from scanning since it legitimately
    contains the forbidden strings as instructions to the image model."""
    haystack = _strip_forbidden_block(text).lower()
    hits: list[dict] = []
    for p in patterns:
        needle = str(p.get("pattern", ""))
        if not needle:
            continue
        regex = _pattern_to_regex(needle)
        if re.search(regex, haystack, re.IGNORECASE):
            hits.append({
                "pattern": p["pattern"],
                "severity": p.get("severity", "warning"),
                "reason": p.get("reason", ""),
            })
    return hits


# ── Image generation ─────────────────────────────────────────────────────────

def _build_input_index(brand: dict, selected: list[dict], has_logo: bool, include_ref: bool = True) -> str:
    """Build a simple instruction block — no 'INPUT N' labels that the model might render as text."""
    name = brand["display_name"]
    product_list = ", ".join(p["name"] for p in selected)
    
    lines = []
    if include_ref:
        lines.append(
            f"Transform the first image (competitor ad) into a {name} ad. "
            f"Replace all competitor products with the {name} product images that follow. "
            f"Replace all text with the approved text below. "
            f"Replace all colors with the {name} palette. "
            f"Keep the composition, camera angle, and lighting from the first image."
        )
    lines.append(
        f"Products to use: {product_list}. "
        f"Paste each product image exactly — never redraw, recolor, or reinterpret any label. "
        f"The label artwork on each product image IS the final label."
    )
    if has_logo:
        lines.append(
            f"The {name} logo appears last. Place it small in the bottom-right corner — "
            f"no box, no banner, no oversized placement."
        )
    return "\n".join(lines) + "\n\n"


def generate_image(
    brand: dict,
    ref_path: str,
    product_paths: list[str],
    logo_path: str,
    selected: list[dict],
    ad_json: dict,
    out_path: Path,
) -> None:
    client = _client()
    # Build prompt first, then images — Gemini official pattern for editing
    has_logo = bool(logo_path and os.path.exists(logo_path))
    index_block = _build_input_index(brand, selected, has_logo, include_ref=True)
    final_prompt = _assemble_prompt_from_json(brand, ad_json)
    full_prompt = index_block + final_prompt
    
    # Text prompt FIRST, then images (Google cookbook pattern for image editing)
    contents: list = [full_prompt]
    contents.append(_image_part(ref_path))      # reference to edit
    for p in product_paths:
        contents.append(_image_part(p))          # product images
    if has_logo:
        contents.append(_image_part(logo_path))  # logo

    def gemini():
        resp = client.models.generate_content(
            model=IMAGE_MODEL,
            contents=contents,
            config={
                "response_modalities": ["IMAGE"],
                "image_config": ImageConfig(aspect_ratio="4:5"),
            },
        )
        for part in resp.candidates[0].content.parts:
            if part.inline_data:
                return part.inline_data.data
        raise RuntimeError("Image model returned no image")

    or_image_paths = [ref_path] + list(product_paths)
    if has_logo:
        or_image_paths.append(logo_path)

    def openrouter():
        return _or_image_call(OR_IMAGE_MODEL, full_prompt, or_image_paths)

    # Primary: Gemini → Fallback: OpenAI GPT Image 2
    try:
        img_bytes = _with_fallback("generate_image", gemini, lambda: _openai_image_call(full_prompt, or_image_paths))
    except RuntimeError as e:
        raise  # already wrapped
    img = Image.open(io.BytesIO(img_bytes))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))


# ── Sidecar ──────────────────────────────────────────────────────────────────

def save_sidecar(
    brand: dict,
    out_path: Path,
    ref_path: str,
    selected: list[dict],
    reverse: str,
    ad_json: dict,
    forbidden_warnings: list[dict],
) -> None:
    sidecar = out_path.with_suffix(".instructions.txt")
    warn_block = ""
    if forbidden_warnings:
        warn_block = "\n=== FORBIDDEN-TEXT WARNINGS ===\n" + "\n".join(
            f"- [{h['severity']}] '{h['pattern']}' in field '{h.get('field', '?')}': {h['reason']}" for h in forbidden_warnings
        ) + "\n"
    assembled_prompt = _assemble_prompt_from_json(brand, ad_json)
    sidecar.write_text(
        f"BRAND: {brand['slug']} ({brand['display_name']})\n"
        f"REF: {ref_path}\n"
        f"OUTPUT: {out_path}\n"
        f"PRODUCTS: {', '.join(p['name'] for p in selected)}\n"
        f"TIMESTAMP: {datetime.now().isoformat()}\n"
        f"{warn_block}"
        f"\n=== REVERSE ANALYSIS ===\n{reverse}\n"
        f"\n=== STRUCTURED AD JSON ===\n{json.dumps(ad_json, indent=2)}\n"
        f"\n=== ASSEMBLED IMAGE PROMPT ===\n{assembled_prompt}\n"
    )


# ── Main pipeline ────────────────────────────────────────────────────────────

def run_one(brand: dict, ref_path: str, locked_product: str | None = None, category: str | None = None, research: str | None = None) -> Path:
    if not os.path.exists(ref_path):
        raise FileNotFoundError(f"ref not found or unreadable: {ref_path}")

    if brand.get("product_required") and not locked_product and not category:
        avail = ", ".join(p["name"] for p in brand["products"])
        raise RuntimeError(
            f"brand '{brand['slug']}' requires --product or --category. Available: {avail}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    out_path = OUTPUT_DIR / f"{brand['slug']}_{ts}.png"

    log(brand, f"reverse-engineering {ref_path}")
    reverse = reverse_analyze(ref_path)
    product_count = parse_product_count(reverse)
    produce = parse_produce(reverse)
    log(brand, f"ref has {product_count} product(s)")

    tally = load_tally(brand)
    if locked_product:
        selected = lock_products(brand, product_count, locked_product)
        log(brand, f"locked product: {selected[0]['name']} (×{product_count})")
    else:
        selected = pick_products(brand, product_count, produce, tally, category=category)
        if category:
            log(brand, f"category: {category}")
        log(brand, f"products: {', '.join(p['name'] for p in selected)}")

    log(brand, "composing final prompt")
    products_dir = Path(brand["paths"]["products_dir"])
    product_paths: list[str] = []
    for prod in selected:
        p = products_dir / prod["label_file"]
        if p.exists():
            product_paths.append(str(p))
        else:
            log(brand, f"WARN: product image missing: {p}")
    logo_path = brand["paths"].get("logo_path")

    # Run on-demand research if requested
    research_context: str | None = None
    if research:
        log(brand, f"running on-demand research: {research}")
        research_context = run_research(research, deep=False)
        if research_context:
            log(brand, "research context injected into composer")
        else:
            log(brand, "WARN: research returned no results (Jina may not be configured)")

    final_prompt = compose_prompt(brand, selected, reverse, ref_path, product_paths, logo_path, research_context=research_context)
    # final_prompt is now a dict (structured ad JSON), not a string

    patterns = collect_forbidden_patterns(brand, selected)
    # Scan the structured JSON fields for forbidden text (per-field validation)
    hits = scan_ad_json_forbidden(final_prompt, patterns)
    errors = [h for h in hits if h["severity"] == "error"]
    warnings = [h for h in hits if h["severity"] != "error"]
    for h in warnings:
        log(brand, f"WARN forbidden-text in '{h.get('field', '?')}': '{h['pattern']}' -- {h['reason']}")
    if errors:
        msg = "; ".join(f"'{h['pattern']}' in '{h.get('field', '?')}' ({h['reason']})" for h in errors)
        raise RuntimeError(f"ad JSON contains FORBIDDEN text (severity=error): {msg}")

    log(brand, f"generating image → {out_path}")
    generate_image(brand, ref_path, product_paths, brand["paths"]["logo_path"], selected, final_prompt, out_path)

    save_sidecar(brand, out_path, ref_path, selected, reverse, final_prompt, warnings)

    for prod in selected:
        tally[prod["name"]] = tally.get(prod["name"], 0) + 1
    save_tally(brand, tally)

    # Copy to website public folder and update ads.json
    ad_label = sync_ad_to_website(brand, out_path, selected)
    caption = f"Generated ad for {brand.get('display_name') or brand['slug']}"
    if ad_label:
        caption += f"\nName: {ad_label}"
    product_names = ", ".join(p["name"] for p in selected)
    if product_names:
        caption += f"\nProducts: {product_names}"
    _send_telegram_photo(out_path, caption)

    return out_path


def sync_ad_to_website(brand: dict, out_path: Path, selected: list[dict]) -> str:
    """Copy generated ad to website/public/images/ads/{brand}/ and append to website/public/data/{brand}.json."""
    import shutil

    slug = brand["slug"]
    brand_img_dir = REPO_ROOT / "website" / "public" / "images" / "ads" / slug
    brand_img_dir.mkdir(parents=True, exist_ok=True)

    # Copy image to brand-specific folder
    dest_path = brand_img_dir / out_path.name
    shutil.copy2(out_path, dest_path)

    # Also copy sidecar if exists
    sidecar = out_path.with_suffix(".instructions.txt")
    if sidecar.exists():
        shutil.copy2(sidecar, brand_img_dir / sidecar.name)

    # Update brand-specific JSON file
    brand_json_path = REPO_ROOT / "website" / "public" / "data" / f"{slug}.json"
    ads_data = []
    if brand_json_path.exists():
        try:
            ads_data = json.loads(brand_json_path.read_text())
        except Exception:
            ads_data = []

    product_name = selected[0]["name"] if selected else out_path.stem

    new_ad = {
        "id": out_path.name,
        "filename": out_path.name,
        "path": f"/images/ads/{slug}/{out_path.name}",
        "product_name": product_name,
        "status": "new",
        "brand": slug,
        "created_at": datetime.now().isoformat()
    }

    # Replace if already exists by id (filename), otherwise append.
    # Deduplicate on id so the same image file is never added twice.
    replaced = False
    for i, ad in enumerate(ads_data):
        if ad.get("id") == new_ad["id"] or ad.get("filename") == new_ad["filename"]:
            ads_data[i] = new_ad
            replaced = True
            break
    if not replaced:
        ads_data.append(new_ad)

    brand_json_path.write_text(json.dumps(ads_data, indent=2))
    log(brand, f"synced to website: {dest_path}")

    # Also initialize entry in ad-approval JSON so approve/bad buttons work
    return _sync_ad_to_approval(slug, out_path.name, selected)


def _ad_label_prefix(slug: str) -> str:
    if slug == "cinco-h-ranch":
        return "Cinco"
    if slug == "island-splash":
        return "Island"
    return slug.replace("-", " ").title()


def _ad_product_label(selected: list[dict]) -> str:
    product = selected[0]["name"] if selected else "Ad"
    aliases = {
        "Rejuvenating Face + Body Cream": "Cream",
        "Honey Vanilla Soap": "Soap",
        "Sunscreen Stick": "Sunscreen",
    }
    return aliases.get(product, product)


def _next_ad_label(state: dict, slug: str, selected: list[dict]) -> str:
    labels = state.setdefault("labels", {})
    seq = len(labels) + 1
    used = set(labels.values())
    prefix = _ad_label_prefix(slug)
    product = _ad_product_label(selected)
    while True:
        label = f"{prefix} {product} {seq:03d}"
        if label not in used:
            return label
        seq += 1


def _sync_ad_to_approval(slug: str, ad_id: str, selected: list[dict]) -> str:
    """Add ad to approval JSON in format server expects: {"pending": [...], "approved": [...], "bad": [...]}"""
    approval_dir = OUTPUT_DIR / "ad-approval"
    approval_dir.mkdir(parents=True, exist_ok=True)
    approval_file = approval_dir / f"{slug}.json"

    state = {"pending": [], "approved": [], "bad": []}
    if approval_file.exists():
        try:
            state = json.loads(approval_file.read_text())
            # Normalize from old format if needed
            if "ads" in state and "pending" not in state:
                old = state.pop("ads", {})
                state["pending"] = [v.get("filename", k) for k, v in old.items() if v.get("status") == "pending"]
                state["approved"] = [v.get("filename", k) for k, v in old.items() if v.get("status") == "approved"]
                state["bad"] = [v.get("filename", k) for k, v in old.items() if v.get("status") == "bad"]
        except (json.JSONDecodeError, OSError):
            state = {"pending": [], "approved": [], "bad": []}

    state.setdefault("labels", {})
    state.setdefault("products", {})
    if ad_id not in state["labels"]:
        state["labels"][ad_id] = _next_ad_label(state, slug, selected)
    if selected:
        state["products"][ad_id] = selected[0]["name"]

    if not any(ad_id in state.get(bucket, []) for bucket in ("pending", "approved", "bad", "consumed")):
        state.setdefault("pending", []).append(ad_id)
    approval_file.write_text(json.dumps(state, indent=2))
    return state["labels"].get(ad_id, "")


def resolve_pool_dir(brand: dict, locked_product: str | None, category: str | None = None) -> Path:
    """Which directory to read approved refs from.

    Uses REFS_PUBLIC_DIR (respects REFS_VOLUME on Railway) where the gallery
    stores approved refs after human review. Falls back to brand_assets locally.
    """
    pool_slug = category or brand.get("default_pool", "drinks")
    if brand.get("product_required") and locked_product:
        prod = find_product(brand, locked_product)
        if not prod:
            raise RuntimeError(f"locked product '{locked_product}' not in brand '{brand['slug']}'")
        pool_slug = prod.get("pool_slug") or prod["name"].lower().replace(" ", "-")
    approved_dir = REFS_PUBLIC_DIR / brand["slug"] / pool_slug / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    return approved_dir


def list_pool(brand: dict, locked_product: str | None = None, category: str | None = None) -> list[Path]:
    pool_dir = resolve_pool_dir(brand, locked_product, category)
    pool_dir.mkdir(parents=True, exist_ok=True)
    refs: list[Path] = []
    for p in sorted(pool_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in POOL_EXTS:
            refs.append(p)
    return refs


def run_pool(brand: dict, locked_product: str | None = None, category: str | None = None, limit: int = 0, research: str | None = None) -> int:
    refs = list_pool(brand, locked_product, category)
    pool_dir = resolve_pool_dir(brand, locked_product, category)
    if limit > 0 and len(refs) > limit:
        refs = refs[:limit]
        log(brand, f"limited to {limit} ref(s) (of {len(list_pool(brand, locked_product, category))} available)")
    if not refs:
        log(brand, f"pool is empty -- nothing to do ({pool_dir})")
        return 1
    log(brand, f"pool has {len(refs)} ref(s) in {pool_dir}; processing sequentially")
    if locked_product:
        log(brand, f"all refs locked to product: {locked_product}")
    successes: list[tuple[str, Path]] = []
    failures: list[tuple[str, str]] = []
    for i, ref in enumerate(refs, 1):
        if i > 1:
            log(brand, f"pacing {POOL_PACING_SECONDS}s before next ref…")
            time.sleep(POOL_PACING_SECONDS)
        log(brand, f"=== pool {i}/{len(refs)}: {ref.name} ===")
        try:
            out = run_one(brand, str(ref), locked_product=locked_product, category=category, research=research)
            import shutil as _shutil
            pool_slug = category or brand.get("default_pool", "drinks")
            web_used_dir = REFS_PUBLIC_DIR / brand["slug"] / pool_slug / "used"
            web_used_dir.mkdir(parents=True, exist_ok=True)
            used_ref = web_used_dir / ref.name
            if used_ref.exists():
                used_ref.unlink()
            _shutil.move(str(ref), str(used_ref))
            successes.append((ref.name, out))
            print(str(out))
            sys.stdout.flush()
        except Exception as e:
            log(brand, f"FAIL on {ref.name}: {e}")
            _send_telegram_message(
                f"Ad generation failed for {brand.get('display_name') or brand['slug']}.\n"
                f"Ref: {ref.name}\n"
                f"Stopped on error: {e}"
            )
            failures.append((ref.name, str(e)))
            break
    log(brand, f"pool done: {len(successes)} succeeded, {len(failures)} failed")
    for name, err in failures:
        log(brand, f"  FAIL {name}: {err}")
    return 0 if successes else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Dremes -- multi-brand ad generator")
    parser.add_argument("--brand", default=DEFAULT_BRAND, help="Brand slug (matches brands/<slug>.json)")
    parser.add_argument("--product", default=None, help="Lock to one product (name or trigger keyword); overrides rotation")
    parser.add_argument("--pool", action="store_true", help="Drain the brand's pool dir sequentially")
    parser.add_argument("--category", default=None, help="Pool category subdirectory (e.g. drinks for island-splash)")
    parser.add_argument("--count", type=int, default=0, help="Max refs to process (0 = all)")
    parser.add_argument("--research", default=None, help="On-demand Jina web research query to inject into composer prompt")
    parser.add_argument("ref", nargs="?", help="Reference image path (required unless --pool)")
    args = parser.parse_args()

    brand = load_brand(args.brand)

    if args.product:
        # Validate early so a bad keyword fails before any API call.
        find_or_fail = find_product(brand, args.product)
        if not find_or_fail:
            avail = ", ".join(p["name"] for p in brand["products"])
            print(f"ERROR: product '{args.product}' not found in brand '{brand['slug']}'. Available: {avail}", file=sys.stderr)
            return 2
        # Pass the canonical name so logs/sidecars are consistent.
        args.product = find_or_fail["name"]

    if args.pool:
        return run_pool(brand, locked_product=args.product, category=args.category, limit=args.count)

    if not args.ref:
        parser.error("ref path required unless --pool is set")
    if not os.path.exists(args.ref):
        print(f"ERROR: ref not found: {args.ref}", file=sys.stderr)
        return 2

    out_path = run_one(brand, args.ref, locked_product=args.product, research=args.research)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
