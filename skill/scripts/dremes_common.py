"""
Shared utilities for the Dremes ad pipeline.
All scripts import from here instead of copy-pasting slugify, path resolution, etc.
"""

import json
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRANDS_DIR = REPO_ROOT / "brands"

# ── slugify ────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert any text to a URL-safe slug. Consistent across all scripts."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")

# ── brand config loading ───────────────────────────────────────────────────────

def load_brand_config(brand_slug: str) -> dict:
    """Load a brand's JSON config. Returns empty dict if not found."""
    config_path = BRANDS_DIR / f"{brand_slug}.json"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return json.load(f)

# ── path resolution (respects REFS_VOLUME on Railway) ──────────────────────────

REFS_VOLUME = os.environ.get("REFS_VOLUME", "")

if REFS_VOLUME:
    REFS_PUBLIC_DIR = Path(REFS_VOLUME) / "public" / "images" / "refs"
    REFS_DATA_DIR = Path(REFS_VOLUME) / "public" / "data" / "refs"
else:
    REFS_PUBLIC_DIR = REPO_ROOT / "website" / "public" / "images" / "refs"
    REFS_DATA_DIR = REPO_ROOT / "website" / "public" / "data" / "refs"
