#!/usr/bin/env python3
"""
Queue worker — processes one job at a time from the board queue.

Usage:
    python3 skill/scripts/run_queue.py              # process one job
    python3 skill/scripts/run_queue.py --watch      # keep processing as jobs arrive
    python3 skill/scripts/run_queue.py --brand foo  # process all pending for one brand

The queue lives at /home/drewp/dremes-agent/state/board-queue/queue.json
Ads-agent (Hermes) also reads this queue — they share it.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_FILE = REPO_ROOT / "state" / "board-queue" / "queue.json"
SYNC_SCRIPT = REPO_ROOT / "skill" / "scripts" / "sync_ads_to_website.py"


def load_queue() -> dict:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            pass
    return {"jobs": [], "last_updated": ""}


def save_queue(queue: dict) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def find_pending(queue: dict) -> dict | None:
    for job in queue.get("jobs", []):
        if job.get("status") == "pending":
            return job
    return None


def run_scrape(job: dict) -> tuple[bool, str]:
    brand = job["brand"]
    url = job.get("url", "")
    pool = job.get("pool", "drinks")
    max_images = job.get("maxImages", 100)

    cmd = [
        "python3", "skill/scripts/drain_board.py",
        "--brand", brand,
        "--board-url", url,
        "--pool", pool,
        "--max-images", str(max_images),
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800)
    return result.returncode == 0, result.stderr or result.stdout


def run_generate(job: dict) -> tuple[bool, str]:
    brand = job["brand"]
    category = job.get("pool", "drinks")
    cmd = ["python3", "dremes_agent.py", "--brand", brand, "--pool", "--category", category]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=1800)
    return result.returncode == 0, result.stderr or result.stdout


def run_compose(job: dict) -> tuple[bool, str]:
    brand = job["brand"]
    cmd = ["python3", "skill/scripts/compose_posts.py", "--brand", brand, "--min-ads", "3"]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600)
    return result.returncode == 0, result.stderr or result.stdout


def run_schedule(job: dict) -> tuple[bool, str]:
    brand = job["brand"]
    cmd = ["python3", "skill/scripts/schedule_post.py", "--brand", brand, "--from-composed"]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600)
    return result.returncode == 0, result.stderr or result.stdout


def sync_website(brand: str) -> None:
    """Sync generated ads to website after a generate job."""
    result = subprocess.run(
        ["python3", str(SYNC_SCRIPT), "--brand", brand],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  [sync] warning: {result.stderr[:200]}")


DISPATCH = {
    "scrape": run_scrape,
    "generate_ads": run_generate,
    "compose": run_compose,
    "schedule": run_schedule,
}


def process_one(brand_filter: str | None = None) -> dict | None:
    """Process the oldest pending job. Returns the job or None if empty."""
    queue = load_queue()
    job = find_pending(queue)
    if not job:
        return None

    if brand_filter and job.get("brand") != brand_filter:
        return None

    job_id = job["id"]
    job_type = job.get("type", "")
    brand = job.get("brand", "unknown")

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing: {job_type} ({job_id}) for {brand}")

    # Mark as processing
    for j in queue["jobs"]:
        if j["id"] == job_id:
            j["status"] = "processing"
            j["started_at"] = datetime.now().isoformat()
            break
    save_queue(queue)

    # Run
    runner = DISPATCH.get(job_type)
    if not runner:
        success, output = False, f"Unknown job type: {job_type}"
    else:
        success, output = runner(job)

    # Update status
    for j in queue["jobs"]:
        if j["id"] == job_id:
            if success:
                j["status"] = "done"
                j["completed_at"] = datetime.now().isoformat()
                print(f"  ✓ {job_type} done")
                # Sync website after generate jobs
                if job_type == "generate_ads":
                    print(f"  syncing website...")
                    sync_website(brand)
            else:
                j["status"] = "error"
                j["error"] = output[:500]
                j["failed_at"] = datetime.now().isoformat()
                print(f"  ✗ {job_type} failed: {output[:200]}")
            break
    save_queue(queue)
    return job


def main():
    parser = argparse.ArgumentParser(description="Queue worker for asset-ads")
    parser.add_argument("--watch", action="store_true", help="Keep watching for new jobs")
    parser.add_argument("--brand", help="Only process jobs for this brand")
    parser.add_argument("--limit", type=int, default=1, help="Max jobs to process before exiting (default: 1)")
    args = parser.parse_args()

    processed = 0
    while True:
        job = process_one(brand_filter=args.brand)
        if not job:
            if args.watch:
                time.sleep(10)
                continue
            else:
                if processed == 0:
                    print("Queue is empty.")
                break
        processed += 1
        if args.limit and processed >= args.limit:
            break

    if processed > 0:
        print(f"\nProcessed {processed} job(s).")


if __name__ == "__main__":
    main()
