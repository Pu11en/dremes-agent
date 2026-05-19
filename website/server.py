#!/usr/bin/env python3
"""Dremes gallery server — serves static files and provides API endpoints for ref/ad approval."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent  # dremes-agent root

BRANDS_DIR = BASE / "brands"
OUTPUT_DIR = BASE / "output"
PUBLIC_DIR = ROOT / "public"
BRAND_ASSETS_DIR = BASE / "brand_assets"
REFS_VOLUME = os.environ.get("REFS_VOLUME", "")
DATA_DIR = os.environ.get("DATA_DIR", "")

# Use persistent volume for output when on Railway
if DATA_DIR:
    OUTPUT_DIR = Path(DATA_DIR) / "output"
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Volume may not be writable yet — start.sh fixes this on next boot
        OUTPUT_DIR = BASE / "output"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

AD_APPROVAL_DIR = OUTPUT_DIR / "ad-approval"
ADS_BAD_DIR = OUTPUT_DIR / "ads-bad"
POSTS_DIR = OUTPUT_DIR / "posts"
REFS_DATA_DIR = ROOT / "public" / "data" / "refs"

if REFS_VOLUME:
    REFS_VOLUME_DIR = Path(REFS_VOLUME)
    REFS_PUBLIC_DIR = REFS_VOLUME_DIR / "public" / "images" / "refs"
    REFS_DATA_DIR = REFS_VOLUME_DIR / "public" / "data" / "refs"
else:
    REFS_VOLUME_DIR = None
    REFS_PUBLIC_DIR = PUBLIC_DIR / "images" / "refs"

NOTIFY_CHAT_ID = os.environ.get("NOTIFY_CHAT_ID", "")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

GENERATION_LOCK = threading.Lock()
GENERATION_JOBS = {}


def _generation_key(brand_slug, pool):
    return f"{brand_slug}/{pool}"


def _generation_snapshot(key):
    with GENERATION_LOCK:
        job = GENERATION_JOBS.get(key)
        if not job:
            return {"running": False, "status": "idle", "generated": 0, "approved_left": 0}
        return {k: v for k, v in job.items() if k != "thread"}


def _set_generation_state(key, **updates):
    with GENERATION_LOCK:
        job = GENERATION_JOBS.setdefault(key, {})
        job.update(updates)


def _run_generation_job(brand_slug, pool, key):
    generated = 0
    _set_generation_state(
        key,
        running=True,
        status="running",
        stop_requested=False,
        generated=0,
        approved_left=len(get_gallery_state(brand_slug, pool).get("approved", [])),
        started_at=time.time(),
        finished_at=None,
    )
    while True:
        with GENERATION_LOCK:
            stop_requested = GENERATION_JOBS.get(key, {}).get("stop_requested", False)
        if stop_requested:
            _set_generation_state(key, running=False, status="stopped", finished_at=time.time())
            return

        approved_left = len(get_gallery_state(brand_slug, pool).get("approved", []))
        _set_generation_state(key, approved_left=approved_left, generated=generated)
        if approved_left <= 0:
            _set_generation_state(key, running=False, status="done", finished_at=time.time(), approved_left=0)
            return

        cmd = ["python3", "dremes_agent.py", "--brand", brand_slug, "--pool", "--category", pool, "--count", "1"]
        try:
            result = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            _set_generation_state(key, running=False, status="stopped", finished_at=time.time())
            return

        if result.returncode != 0:
            print(f"[gallery] generation stopped for {key}: exit {result.returncode}", file=sys.stderr, flush=True)
            for line in (result.stderr or "").splitlines()[-8:]:
                print(f"[gallery] generation stderr: {line}", file=sys.stderr, flush=True)
            _set_generation_state(key, running=False, status="stopped", finished_at=time.time())
            return

        generated += 1
        _set_generation_state(
            key,
            generated=generated,
            approved_left=len(get_gallery_state(brand_slug, pool).get("approved", [])),
        )


def discover_pools(brand_slug):
    """Discover pools from volume (REFS_PUBLIC_DIR) and brand_assets fallback."""
    pools = set()
    # Primary: volume path where refs actually live
    vol_dir = REFS_PUBLIC_DIR / brand_slug
    if vol_dir.exists():
        for entry in vol_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                pools.add(entry.name)
    # Fallback: brand_assets (local dev)
    ba_dir = BRANDS_DIR.parent / "brand_assets" / brand_slug / "references"
    if ba_dir.exists():
        for entry in ba_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                pools.add(entry.name)
    if not pools:
        pools.add("default")
    return sorted(pools)


def load_brands():
    brands = {}
    if BRANDS_DIR.exists():
        for f in sorted(BRANDS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                slug = data.get("slug") or f.stem
                brands[slug] = data
            except (json.JSONDecodeError, OSError):
                pass
    return brands


def get_gallery_state(brand_slug, pool):
    """Rebuild gallery state from filesystem."""
    ref_dir = REFS_PUBLIC_DIR / brand_slug / pool
    state = {"pending": [], "approved": [], "used": []}
    for tab in state:
        d = ref_dir / tab
        if d.exists():
            state[tab] = sorted(f.name for f in d.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    return state


def _filename_matches_brand(filename: str, slug: str) -> bool:
    """Check if a filename belongs to a specific brand."""
    fname = filename.lower()
    s = slug.lower()
    if fname.startswith(s.replace("-", "_") + "_") or fname.startswith(s + "_"):
        return True
    alternates = {
        "island-splash": ["splash_", "island_splash_"],
        "cinco-h-ranch": ["cinco_"],
    }
    for prefix in alternates.get(slug, []):
        if fname.startswith(prefix):
            return True
    return False


def get_ads_state(brand_slug):
    """Load ad approval state — migrates old format, filters to existing images matching brand."""
    ad_file = AD_APPROVAL_DIR / f"{brand_slug}.json"
    if not ad_file.exists():
        return {"pending": [], "approved": [], "bad": []}
    try:
        data = json.loads(ad_file.read_text())
        migrated = False

        # Migrate from old format {"ads": {"key": {"status": "pending"}}} → new format
        if "ads" in data and "pending" not in data:
            old = data.pop("ads", {})
            data["pending"] = []
            data["approved"] = []
            data["bad"] = []
            for k, v in old.items():
                status = v.get("status", "pending") if isinstance(v, dict) else "pending"
                fname = v.get("filename", k) if isinstance(v, dict) else k
                data.setdefault(status, []).append(fname)
            migrated = True

        labels = data.setdefault("labels", {})
        products = data.setdefault("products", {})
        result = {}
        for tab in ("pending", "approved", "bad"):
            entries = data.get(tab, [])
            # Filter: must exist on disk AND match this brand
            existing = []
            for e in entries:
                image_exists = (OUTPUT_DIR / e).exists()
                if tab == "bad":
                    image_exists = image_exists or (ADS_BAD_DIR / brand_slug / e).exists()
                if image_exists and _filename_matches_brand(e, brand_slug):
                    existing.append(e)
            if len(existing) != len(entries):
                migrated = True
                data[tab] = existing
            result[tab] = []
            for fname in existing:
                if fname not in labels:
                    labels[fname] = _make_ad_label(brand_slug, fname, data)
                    migrated = True
                if fname not in products:
                    product = _ad_product_from_sidecar(fname)
                    if product:
                        products[fname] = product
                        migrated = True
                result[tab].append({
                    "filename": fname,
                    "label": labels.get(fname, fname),
                    "product": products.get(fname, ""),
                })

        if migrated:
            ad_file.write_text(json.dumps(data, indent=2))
        return result
    except (json.JSONDecodeError, OSError):
        return {"pending": [], "approved": [], "bad": []}


def _ad_product_from_sidecar(filename: str) -> str:
    sidecar = OUTPUT_DIR / f"{Path(filename).stem}.instructions.txt"
    if not sidecar.exists():
        return ""
    try:
        for line in sidecar.read_text(errors="ignore").splitlines():
            if line.startswith("PRODUCTS:"):
                return line.split(":", 1)[1].strip().split(",")[0].strip()
    except OSError:
        return ""
    return ""


def _ad_product_short(product: str) -> str:
    aliases = {
        "Rejuvenating Face + Body Cream": "Cream",
        "Honey Vanilla Soap": "Soap",
        "Sunscreen Stick": "Sunscreen",
    }
    return aliases.get(product, product or "Ad")


def _make_ad_label(brand_slug: str, filename: str, data: dict) -> str:
    if brand_slug == "cinco-h-ranch":
        prefix = "Cinco"
    elif brand_slug == "island-splash":
        prefix = "Island"
    else:
        prefix = brand_slug.replace("-", " ").title()
    product = _ad_product_short(data.setdefault("products", {}).get(filename) or _ad_product_from_sidecar(filename))
    used = set(data.setdefault("labels", {}).values())
    seq = len(used) + 1
    while True:
        label = f"{prefix} {product} {seq:03d}"
        if label not in used:
            return label
        seq += 1


def _ad_filename(entry):
    return entry if isinstance(entry, str) else entry.get("filename", "")


def get_posts_state(brand_slug):
    """Load composed posts."""
    posts = []
    if POSTS_DIR.exists():
        for f in sorted(POSTS_DIR.glob(f"{brand_slug}_*.json")):
            try:
                data = json.loads(f.read_text())
                if isinstance(data, list):
                    file_posts = data
                else:
                    file_posts = data.get("posts", [])
                for post in file_posts:
                    if isinstance(post, dict):
                        post = dict(post)
                        post["_source_file"] = f.name
                        posts.append(post)
            except (json.JSONDecodeError, OSError):
                pass
    return posts


def _post_files(brand_slug):
    return sorted(POSTS_DIR.glob(f"{brand_slug}_*.json")) if POSTS_DIR.exists() else []


def _load_post_file(path):
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return {"brand": "", "posts": data}, True
    data.setdefault("posts", [])
    return data, False


def _write_post_file(path, data, was_list):
    payload = data.get("posts", []) if was_list else data
    path.write_text(json.dumps(payload, indent=2) + "\n")


def find_post(brand_slug, post_id):
    for path in _post_files(brand_slug):
        try:
            data, was_list = _load_post_file(path)
        except (json.JSONDecodeError, OSError):
            continue
        for idx, post in enumerate(data.get("posts", [])):
            if post.get("post_id") == post_id:
                return path, data, was_list, idx, post
    return None, None, None, None, None


def _reset_post_approval(post, reason):
    if post.get("status") in ("scheduled", "posted"):
        return
    post.setdefault("revision_history", []).append({
        "at": datetime.now().isoformat(),
        "reason": reason,
        "previous_status": post.get("status"),
    })
    post["status"] = "needs_review"
    post["approved_at"] = None
    post["approved_by"] = None
    for platform, state in post.setdefault("platform_status", {}).items():
        if state.get("status") not in ("scheduled", "posted"):
            state["status"] = "pending"
            state["last_error"] = None


def _move_ad_state(brand_slug, filename, target):
    ad_file = AD_APPROVAL_DIR / f"{brand_slug}.json"
    data = {"pending": [], "approved": [], "bad": []}
    if ad_file.exists():
        try:
            data.update(json.loads(ad_file.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    for bucket in ("pending", "approved", "bad", "consumed"):
        if filename in data.get(bucket, []):
            data[bucket].remove(filename)
    data.setdefault(target, []).append(filename)
    ad_file.parent.mkdir(parents=True, exist_ok=True)
    ad_file.write_text(json.dumps(data, indent=2) + "\n")

    if target == "bad":
        src = OUTPUT_DIR / filename
        if src.exists():
            bad_dir = ADS_BAD_DIR / brand_slug
            bad_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(bad_dir / filename))


class DremesHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # Suppress default HTTP access logs — they drown out bot output on Railway
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _serve_file(self, fpath, content_type=None):
        """Serve a file from outside the document root with correct MIME type."""
        if content_type:
            ctype = content_type
        else:
            ext = fpath.suffix.lower()
            mime_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
                ".ico": "image/x-icon",
            }
            ctype = mime_map.get(ext, "application/octet-stream")
        body = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_gallery_page(self, page):
        """Serve shared gallery HTML without cache for Mini App/WebView clients."""
        fpath = ROOT / "gallery" / f"{page}.html"
        if not fpath.exists():
            return self._send_json({"error": "Not found"}, 404)
        body = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            print(f"[gallery] GET crash: {e}", file=sys.stderr, flush=True)
            try:
                self._send_json({"error": "internal"}, 500)
            except Exception:
                pass

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path in ("", "/"):
            self.path = "/" + (("?" + parsed.query) if parsed.query else "")
        elif path.endswith(("/refs", "/ads", "/posts", "/swipe")):
            # Serve from shared gallery template — JS reads brand from URL
            page = path.rstrip("/").split("/")[-1]
            return self._serve_gallery_page(page)

        # Serve brand assets (logos, etc.) from brand_assets/ directory
        if path.startswith("/brand_assets/"):
            brand_asset_path = BRAND_ASSETS_DIR / path[len("/brand_assets/"):]
            if brand_asset_path.exists():
                return self._serve_file(brand_asset_path)
            return self._send_json({"error": "Not found"}, 404)

        # Brand pages — serve the static HTML
        if path in ("", "/") or path.endswith(("/refs", "/ads", "/posts")):
            return super().do_GET()

        # Brand pages requested with the old .html URLs should still use the
        # shared gallery templates. Otherwise stale per-brand copies can be
        # served instead of the fixed page.
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] != "api" and parts[1] in ("refs.html", "ads.html", "posts.html", "swipe.html"):
            page = parts[1].removesuffix(".html")
            return self._serve_gallery_page(page)

        # Brand refs with pool segment — serve refs.html
        # e.g. /island-splash/refs/drinks -> refs.html (JS picks up pool from URL)
        if len(parts) == 3 and parts[0] != "api" and parts[1] in ("refs", "ads", "posts"):
            return self._serve_gallery_page(parts[1])

        # API: list brands
        if path == "/api/brands":
            brands = load_brands()
            result = {}
            for slug, data in brands.items():
                pools = data.get("pools", {})
                if not pools:
                    pool_list = discover_pools(slug)
                    for product in data.get("products", []):
                        if product.get("pool_slug"):
                            pool_list.append(product["pool_slug"])
                    pool_list = sorted(set(pool_list))
                    pools = {p: p.replace("-", " ").title() for p in pool_list}
                result[slug] = {
                    "name": data.get("display_name") or data.get("name", slug),
                    "pools": pools,
                    "pool_dir": data.get("paths", {}).get("pool_dir", ""),
                }
            return self._send_json(result)

        # API: ads state
        if path.startswith("/api/ads/") and path.count("/") == 3:
            brand_slug = path.split("/")[3]
            return self._send_json(get_ads_state(brand_slug))

        # API: posts
        if path.startswith("/api/posts/") and path.count("/") == 3:
            brand_slug = path.split("/")[3]
            posts = get_posts_state(brand_slug)
            return self._send_json(posts)

        # API: generation status for one brand/pool
        if path.startswith("/api/gallery/") and path.endswith("/generate-status"):
            parts = path.split("/")
            if len(parts) >= 6:
                brand_slug = parts[3]
                pool = parts[4]
                key = _generation_key(brand_slug, pool)
                data = _generation_snapshot(key)
                data["approved_left"] = len(get_gallery_state(brand_slug, pool).get("approved", []))
                return self._send_json(data)
            return self._send_json({"error": "Not found"}, 404)

        # API: gallery state
        if path.startswith("/api/gallery/") and len(path.split("/")) >= 4:
            parts = path.split("/")
            brand_slug = parts[3]
            pool = parts[4] if len(parts) > 4 else "default"
            return self._send_json(get_gallery_state(brand_slug, pool))

        # Serve ref images from REFS_PUBLIC_DIR (which may be on the volume)
        if path.startswith("/public/images/refs/") and REFS_VOLUME_DIR:
            vol_path = REFS_VOLUME_DIR / path.lstrip("/")
            if vol_path.exists():
                return self._serve_file(vol_path)
            return self._send_json({"error": "Not found"}, 404)

        # Thumbnail endpoint — generate+cache small WebP for fast gallery loading
        if path.startswith("/thumb/"):
            filename = path.split("/thumb/")[1]
            ad_path = OUTPUT_DIR / filename
            if not ad_path.exists():
                return self._send_json({"error": "Not found"}, 404)
            thumb_dir = OUTPUT_DIR / "thumb"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            thumb_path = thumb_dir / (Path(filename).stem + ".webp")
            if not thumb_path.exists():
                try:
                    from PIL import Image
                    img = Image.open(ad_path)
                    img.thumbnail((400, 400), Image.LANCZOS)
                    img.save(thumb_path, "WEBP", quality=75)
                except Exception as e:
                    print(f"[gallery] thumb gen failed for {filename}: {e}", file=sys.stderr, flush=True)
                    return self._serve_file(ad_path)  # fallback to full
            return self._serve_file(thumb_path, content_type="image/webp")

        # Serve ad images from OUTPUT_DIR (which may be on the volume)
        if path.startswith("/public/images/ads/"):
            parts = path.split("/")
            if len(parts) >= 6:
                filename = parts[-1]
                ad_path = OUTPUT_DIR / filename
                if ad_path.exists():
                    return self._serve_file(ad_path)
            return self._send_json({"error": "Not found"}, 404)

        # Serve static files
        return super().do_GET()

    def _send_telegram_message(self, chat_id, text, parse_mode="HTML"):
        """Send a message to a Telegram chat using the Bot API.

        Non-blocking — failures don't affect the gallery action.
        Retries up to 3 times with 1s delay.
        """
        if not BOT_TOKEN or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=10)
                if resp.status == 200:
                    return True
            except Exception:
                if attempt < 2:
                    time.sleep(1)
        return False

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            print(f"[gallery] POST crash: {e}", file=sys.stderr, flush=True)
            try:
                self._send_json({"error": "internal"}, 500)
            except Exception:
                pass

    def _do_POST(self):
        path = self.path.rstrip("/")

        # Notify Telegram chat (called from Mini App after approve/reject)
        # Falls back to NOTIFY_CHAT_ID env var if chat_id is not provided by frontend
        if path == "/api/notify":
            body = self._read_body()
            chat_id = body.get("chat_id", "") or NOTIFY_CHAT_ID
            ok = self._send_telegram_message(
                chat_id,
                body.get("text", ""),
                body.get("parse_mode", "HTML"),
            )
            return self._send_json({"status": "ok" if ok else "skipped"})

        # Start ad generation for approved refs in a brand/pool. The worker runs
        # one ad at a time so stop requests take effect between ads.
        if path.startswith("/api/gallery/") and path.endswith("/generate"):
            parts = path.split("/")
            if len(parts) < 6:
                return self._send_json({"status": "error", "message": "Not found"}, 404)
            brand_slug = parts[3]
            pool = parts[4]
            key = _generation_key(brand_slug, pool)
            approved_left = len(get_gallery_state(brand_slug, pool).get("approved", []))
            if approved_left <= 0:
                return self._send_json({"status": "empty", "running": False, "approved_left": 0})

            with GENERATION_LOCK:
                existing = GENERATION_JOBS.get(key)
                if existing and existing.get("running"):
                    snapshot = {k: v for k, v in existing.items() if k != "thread"}
                    snapshot["status"] = "already_running"
                    return self._send_json(snapshot)
                thread = threading.Thread(target=_run_generation_job, args=(brand_slug, pool, key), daemon=True)
                GENERATION_JOBS[key] = {
                    "running": True,
                    "status": "starting",
                    "stop_requested": False,
                    "generated": 0,
                    "approved_left": approved_left,
                    "started_at": time.time(),
                    "finished_at": None,
                    "thread": thread,
                }
                thread.start()
            return self._send_json({"status": "started", "running": True, "generated": 0, "approved_left": approved_left})

        # Stop after the current ad finishes.
        if path.startswith("/api/gallery/") and path.endswith("/generate-stop"):
            parts = path.split("/")
            if len(parts) < 6:
                return self._send_json({"status": "error", "message": "Not found"}, 404)
            brand_slug = parts[3]
            pool = parts[4]
            key = _generation_key(brand_slug, pool)
            with GENERATION_LOCK:
                job = GENERATION_JOBS.get(key)
                if job and job.get("running"):
                    job["stop_requested"] = True
                    job["status"] = "stopping"
            return self._send_json(_generation_snapshot(key))

        # Approve refs
        if path.startswith("/api/gallery/") and path.endswith("/approve"):
            parts = path.split("/")
            brand_slug = parts[3]
            pool = parts[4]
            body = self._read_body()
            files = body.get("files", [])
            ref_dir = REFS_PUBLIC_DIR / brand_slug / pool
            approved = 0
            for f in files:
                src = ref_dir / "pending" / f
                if src.exists():
                    dst = ref_dir / "approved" / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    approved += 1
            if approved:
                msg = f"✅ Approved {approved} ref{'s' if approved != 1 else ''} for {brand_slug} ({pool})"
                self._send_telegram_message(NOTIFY_CHAT_ID, msg)
            return self._send_json({"status": "ok", "approved": approved})

        # Reject/delete refs
        if path.startswith("/api/gallery/") and path.endswith("/reject"):
            parts = path.split("/")
            brand_slug = parts[3]
            pool = parts[4]
            body = self._read_body()
            files = body.get("files", [])
            action = body.get("action", "reject")
            ref_dir = REFS_PUBLIC_DIR / brand_slug / pool
            rejected = 0
            for f in files:
                if action == "reset":
                    src = ref_dir / "approved" / f
                    if not src.exists():
                        src = ref_dir / "rejected" / f
                else:
                    src = ref_dir / "pending" / f
                    if not src.exists():
                        src = ref_dir / "rejected" / f
                if src.exists():
                    if action == "delete":
                        src.unlink()
                    elif action == "reset":
                        dst = ref_dir / "pending" / f
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if src.parent.name in ("rejected", "approved"):
                            shutil.move(str(src), str(dst))
                            rejected += 1
                    else:  # reject — move to rejected/
                        dst = ref_dir / "rejected" / f
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                        rejected += 1

            # Telegram notification
            if rejected:
                if action == "delete":
                    msg = f"🗑️ Deleted {rejected} ref{'s' if rejected != 1 else ''} for {brand_slug} ({pool})"
                elif action == "reset":
                    msg = f"↩️ Reset {rejected} ref{'s' if rejected != 1 else ''} for {brand_slug} ({pool})"
                else:
                    msg = f"✗ Rejected {rejected} ref{'s' if rejected != 1 else ''} for {brand_slug} ({pool})"
                self._send_telegram_message(NOTIFY_CHAT_ID, msg)

            # Also clean up manifest if any files were deleted
            if action == "delete" and rejected > 0:
                manifest_path = REFS_DATA_DIR / f"{brand_slug}.json"
                if manifest_path.exists():
                    try:
                        manifest = json.loads(manifest_path.read_text())
                        pool_data = manifest.get("pools", {}).get(pool, {})
                        pool_data["images"] = [
                            img for img in pool_data.get("images", [])
                            if img.get("filename") not in files
                        ]
                        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
                    except (json.JSONDecodeError, OSError):
                        pass

            return self._send_json({"status": "ok", "rejected": rejected})

        # Review ads (approve/bad)
        if path.startswith("/api/ads/") and path.endswith("/review"):
            parts = path.split("/")
            brand_slug = parts[3]
            body = self._read_body()
            action = body.get("action", "")
            files = body.get("files", [])
            ad_file = AD_APPROVAL_DIR / f"{brand_slug}.json"
            if not ad_file.exists():
                return self._send_json({"status": "error", "message": "No ad data"}, 404)
            data = json.loads(ad_file.read_text())

            count = 0
            for f in files:
                if action == "pending" and f in data.get("approved", []):
                    data["approved"].remove(f)
                    data.setdefault("pending", []).append(f)
                    count += 1
                elif action == "bad" and f in data.get("approved", []):
                    data["approved"].remove(f)
                    data.setdefault("bad", []).append(f)
                    count += 1
                elif f in data.get("pending", []):
                    data["pending"].remove(f)
                    if action == "approve":
                        data.setdefault("approved", []).append(f)
                    elif action == "bad":
                        data.setdefault("bad", []).append(f)
                    count += 1

            ad_file.write_text(json.dumps(data, indent=2))

            if count:
                verb = "approved" if action == "approve" else ("moved back to pending" if action == "pending" else "marked bad")
                msg = f"✅ {verb} {count} ad{'s' if count != 1 else ''} for {brand_slug}"
                self._send_telegram_message(NOTIFY_CHAT_ID, msg)

            return self._send_json({"status": "ok", "action": action, "count": count})

        # Review/edit composed posts. Compose creates drafts only; these routes
        # are the human approval/edit layer before anything can be scheduled.
        if path.startswith("/api/posts/"):
            parts = path.split("/")
            if len(parts) < 6:
                return self._send_json({"status": "error", "message": "Not found"}, 404)
            brand_slug = parts[3]
            post_id = parts[4]
            action = parts[5]
            body = self._read_body()

            post_path, data, was_list, idx, post = find_post(brand_slug, post_id)
            if not post:
                return self._send_json({"status": "error", "message": "Post not found"}, 404)

            if action == "approve":
                if not post.get("ad_filenames"):
                    return self._send_json({"status": "error", "message": "Post needs ads before approval"}, 400)
                if not post.get("caption"):
                    return self._send_json({"status": "error", "message": "Post needs a caption before approval"}, 400)
                platforms = post.get("platforms", [])
                if not platforms:
                    return self._send_json({"status": "error", "message": "Select at least one platform"}, 400)
                post["status"] = "approved"
                post["approved_at"] = datetime.now().isoformat()
                post["approved_by"] = body.get("approved_by", "human")

            elif action == "reject":
                if post.get("status") in ("scheduled", "posted"):
                    return self._send_json({"status": "error", "message": "Cannot reject a scheduled/posted post"}, 409)
                post["status"] = "rejected"
                post["approved_at"] = None
                post["approved_by"] = None

            elif action == "caption":
                if post.get("status") in ("scheduled", "posted"):
                    return self._send_json({"status": "error", "message": "Cannot edit a scheduled/posted post"}, 409)
                post["caption"] = body.get("caption", "")
                post["hashtags"] = body.get("hashtags", post.get("hashtags", ""))
                _reset_post_approval(post, "caption edited")

            elif action == "regenerate-caption":
                if post.get("status") in ("scheduled", "posted"):
                    return self._send_json({"status": "error", "message": "Cannot edit a scheduled/posted post"}, 409)
                try:
                    sys.path.insert(0, str(BASE))
                    from skill.scripts.generate_caption import generate
                    result = generate(brand_slug, post.get("ad_filenames", []), [], dry_run=False)
                    post["caption"] = result.get("caption", "")
                    post["hashtags"] = result.get("hashtags", "")
                    _reset_post_approval(post, "caption regenerated")
                except Exception as e:
                    return self._send_json({"status": "error", "message": f"Caption generation failed: {e}"}, 500)

            elif action == "platforms":
                if post.get("status") in ("scheduled", "posted"):
                    return self._send_json({"status": "error", "message": "Cannot edit a scheduled/posted post"}, 409)
                platforms = body.get("platforms", [])
                if not isinstance(platforms, list):
                    return self._send_json({"status": "error", "message": "platforms must be a list"}, 400)
                post["platforms"] = platforms
                current = post.setdefault("platform_status", {})
                for platform in platforms:
                    current.setdefault(platform, {
                        "status": "pending",
                        "scheduled_at": None,
                        "posted_at": None,
                        "external_post_id": None,
                        "last_error": None,
                    })
                for platform in list(current.keys()):
                    if platform not in platforms and current[platform].get("status") not in ("scheduled", "posted"):
                        current.pop(platform, None)
                _reset_post_approval(post, "platforms edited")

            elif action == "remove-ad":
                if post.get("status") in ("scheduled", "posted"):
                    return self._send_json({"status": "error", "message": "Cannot edit a scheduled/posted post"}, 409)
                filename = body.get("filename", "")
                disposition = body.get("disposition", "approved")
                if filename in post.get("ad_filenames", []):
                    post["ad_filenames"] = [f for f in post.get("ad_filenames", []) if f != filename]
                    if disposition == "pending":
                        _move_ad_state(brand_slug, filename, "pending")
                    elif disposition == "bad":
                        _move_ad_state(brand_slug, filename, "bad")
                    _reset_post_approval(post, f"ad removed: {disposition}")

            elif action == "replace-ad":
                if post.get("status") in ("scheduled", "posted"):
                    return self._send_json({"status": "error", "message": "Cannot edit a scheduled/posted post"}, 409)
                old_file = body.get("old_file", "")
                new_file = body.get("new_file", "")
                approved = [_ad_filename(entry) for entry in get_ads_state(brand_slug).get("approved", [])]
                if new_file not in approved:
                    return self._send_json({"status": "error", "message": "Replacement ad must be approved"}, 400)
                files = post.get("ad_filenames", [])
                if old_file in files:
                    post["ad_filenames"] = [new_file if f == old_file else f for f in files]
                else:
                    post.setdefault("ad_filenames", []).append(new_file)
                _reset_post_approval(post, "ad replaced")

            elif action == "schedule":
                if post.get("status") != "approved" or not post.get("approved_at"):
                    return self._send_json({"status": "error", "message": "Approval required"}, 403)
                platform_status = post.get("platform_status", {})
                if any(v.get("external_post_id") or v.get("status") in ("scheduled", "posting", "posted") for v in platform_status.values()):
                    return self._send_json({"status": "error", "message": "Already scheduled or posted"}, 409)
                if not post.get("platforms"):
                    return self._send_json({"status": "error", "message": "No platforms selected"}, 400)

                cmd = [
                    "python3", "skill/scripts/schedule_post.py",
                    "--brand", brand_slug,
                    "--carousel-ads", ",".join(post.get("ad_filenames", [])),
                    "--caption", post.get("caption", ""),
                    "--hashtags", post.get("hashtags", ""),
                    "--platforms", ",".join(post.get("platforms", [])),
                ]
                result = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    for platform in post.get("platforms", []):
                        post.setdefault("platform_status", {}).setdefault(platform, {})["status"] = "failed"
                        post["platform_status"][platform]["last_error"] = (result.stderr or result.stdout)[-500:]
                    _write_post_file(post_path, data, was_list)
                    return self._send_json({"status": "error", "message": "Schedule failed"}, 500)
                scheduled_at = datetime.now().isoformat()
                post["status"] = "scheduled"
                post["scheduled_at"] = scheduled_at
                for platform in post.get("platforms", []):
                    state = post.setdefault("platform_status", {}).setdefault(platform, {})
                    state["status"] = "scheduled"
                    state["scheduled_at"] = scheduled_at

            else:
                return self._send_json({"status": "error", "message": "Unknown action"}, 404)

            data["posts"][idx] = post
            _write_post_file(post_path, data, was_list)
            return self._send_json({"status": "ok", "post": post})

        return self._send_json({"status": "error", "message": "Not found"}, 404)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server — handles concurrent requests without blocking."""
    daemon_threads = True


def find_port(start=8080, max_tries=10):
    for port in range(start, start + max_tries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.bind(("0.0.0.0", port))
            s.close()
            return port
        except OSError:
            continue
    return start  # fallback


def main():
    port = int(os.environ.get("PORT") or os.environ.get("GALLERY_PORT") or find_port(8080))
    for attempt in range(5):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), DremesHandler)
            print(f"[gallery] Dremes gallery server on http://0.0.0.0:{port}", flush=True)
            server.serve_forever()
        except OSError as e:
            print(f"[gallery] Port {port} failed: {e}", file=sys.stderr, flush=True)
            port = find_port(port + 1)
            time.sleep(1)
        except KeyboardInterrupt:
            print("\n[gallery] Shutting down", flush=True)
            break
    else:
        print("[gallery] Could not bind any port after 5 attempts", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
