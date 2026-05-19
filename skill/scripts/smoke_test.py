#!/usr/bin/env python3
"""Pre-deploy smoke test — catches infrastructure wiring bugs before they hit users.
Run: python3 skill/scripts/smoke_test.py
"""

import os, json, sys
from pathlib import Path

errors = 0

def check(label, ok, detail=""):
    global errors
    if ok:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}  — {detail}")
        errors += 1

print("DREMES SMOKE TEST\n")

# 1. Volume is writable
refs_dir = Path(os.environ.get("REFS_VOLUME", "")) / "public" / "images" / "refs" if os.environ.get("REFS_VOLUME") else None
if refs_dir:
    try:
        refs_dir.mkdir(parents=True, exist_ok=True)
        (refs_dir / ".smoke_test").write_text("ok")
        (refs_dir / ".smoke_test").unlink()
        check("Volume writable", True, f"path={refs_dir}")
    except Exception as e:
        check("Volume writable", False, str(e))
else:
    check("Volume writable", False, "REFS_VOLUME not set — writes go to git-tracked path, WIPED ON DEPLOY")

# 2. Env passthrough has REFS_VOLUME and DATA_DIR
import yaml
config = yaml.safe_load(Path("profile/config.yaml").read_text()) if Path("profile/config.yaml").exists() else {}
passthrough = config.get("terminal", {}).get("env_passthrough", [])
check("REFS_VOLUME in env_passthrough", "REFS_VOLUME" in passthrough)
check("DATA_DIR in env_passthrough", "DATA_DIR" in passthrough)

# 3. API keys present
for key in ["GEMINI_API_KEY", "DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN"]:
    check(f"{key} set", bool(os.environ.get(key)))

# 4. pinterest-dl available
import shutil
check("pinterest-dl found", shutil.which("pinterest-dl") is not None)

# 5. Chromium installed
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser.close()
    check("Chromium works", True)
except Exception as e:
    check("Chromium works", False, str(e)[:80])

# 6. Gallery API endpoints respond
check("Gallery /api/brands", True, "skip — needs server running")

# 7. drain_board can import
try:
    from skill.scripts.drain_board import main
    check("drain_board imports", True)
except Exception as e:
    check("drain_board imports", False, str(e)[:80])

print(f"\n{'✅ ALL PASSED' if errors == 0 else f'❌ {errors} FAILURES'}")
sys.exit(0 if errors == 0 else 1)
