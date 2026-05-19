#!/bin/bash
# auto-sync.sh — periodically commit & push agent-made changes to GitHub
# Runs as a background daemon. Uses GITHUB_TOKEN for auth.
set -e

REPO_DIR="/home/drewp/dremes-agent"
INTERVAL_SECONDS="${AUTO_SYNC_INTERVAL:-300}"  # default: every 5 minutes
INITIAL_DELAY="${AUTO_SYNC_INITIAL_DELAY:-300}"  # wait 5 min before first sync

cd "$REPO_DIR"

# Wait before starting so initial startup processes finish
sleep "$INITIAL_DELAY"

echo "[auto-sync] Daemon started (interval=${INTERVAL_SECONDS}s)"

TRIGGER_FILE="/tmp/trigger-auto-sync"

# Initialize git repo if it doesn't exist (cold Railway boot)
if [ ! -d ".git" ]; then
    echo "[auto-sync] Initializing git repo..."
    git init
    git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/Pu11en/dremes-agent.git"
    git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
    git fetch origin master --depth=1
    git reset --mixed origin/master
    echo "[auto-sync] Git repo initialized"
fi

while true; do
    # Wait for interval OR manual trigger (whichever comes first)
    for ((i=0; i<INTERVAL_SECONDS; i+=10)); do
        sleep 10
        if [ -f "$TRIGGER_FILE" ]; then
            rm -f "$TRIGGER_FILE"
            echo "[auto-sync] Manual trigger received"
            break
        fi
    done

    # Only proceed if GITHUB_TOKEN is set
    if [ -z "$GITHUB_TOKEN" ]; then
        echo "[auto-sync] SKIP: GITHUB_TOKEN not set"
        continue
    fi

    # Stash any local changes from the auto-sync itself (not agent changes)
    # Guard: skip stash if repo has no commits yet (git stash requires HEAD)
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
        git stash --quiet 2>/dev/null || true
    fi

    # Pull latest to avoid conflicts
    git pull --rebase origin master 2>/dev/null || {
        echo "[auto-sync] WARN: pull failed, skipping this cycle"
        git stash pop --quiet 2>/dev/null || true
        continue
    }

    # Pop stashed changes
    git stash pop --quiet 2>/dev/null || true

    # Check if there are any changes
    if git diff --quiet && git diff --cached --quiet; then
        continue  # nothing to push
    fi

    # Stage everything (respects .gitignore)
    git add -A

    # Commit with timestamp
    TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")
    git -c user.name="Dremes Agent" \
        -c user.email="dremes@railway.app" \
        commit -m "auto-sync: agent changes @ $TIMESTAMP" || {
        echo "[auto-sync] WARN: nothing to commit"
        continue
    }

    # Push using GITHUB_TOKEN
    REPO_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/Pu11en/dremes-agent.git"
    if git push "$REPO_URL" master 2>/dev/null; then
        echo "[auto-sync] Pushed changes @ $TIMESTAMP"
    else
        echo "[auto-sync] ERROR: push failed, will retry next cycle"
        # Reset the failed commit
        git reset HEAD~1 --soft 2>/dev/null || true
    fi
done
