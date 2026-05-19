#!/bin/bash
# Dremes Agent — start gallery server + Hermes Telegram gateway
set -e

cd /home/drewp/dremes-agent

export HERMES_HOME="${HERMES_HOME:-/tmp/.hermes}"
export HOME="${HOME:-/tmp}"
export USER="${USER:-hermes}"
export GALLERY_PORT="${PORT:-8080}"
export REFS_VOLUME="${REFS_VOLUME:-/data/refs}"
export DATA_DIR="${DATA_DIR:-/data/refs}"
export HERMES_SUPPRESS_SHUTDOWN_NOTIFY="${HERMES_SUPPRESS_SHUTDOWN_NOTIFY:-1}"

# Wire Hindsight memory LLM key from existing OpenRouter key
export HINDSIGHT_LLM_API_KEY="${HINDSIGHT_LLM_API_KEY:-${OPENROUTER_API_KEY}}"

# Persist Hindsight memory DB across Railway deploys
mkdir -p /data/refs/.hindsight 2>/dev/null || true
ln -sfn /data/refs/.hindsight /data/.hindsight 2>/dev/null || true

# Fix volume ownership and create required directories (volume mounts as root)
chown -R hermes:hermes /data/refs 2>/dev/null || true
mkdir -p /data/refs/output/ad-approval /data/refs/output/ads-bad /data/refs/output/posts /data/refs/public/images/refs /data/refs/public/data/refs 2>/dev/null || true
mkdir -p "$HERMES_HOME" 2>/dev/null || true
chown -R hermes:hermes /data/refs "$HERMES_HOME" 2>/dev/null || true

# Wipe stale SQLite lock state after dirty shutdowns. The kanban board is
# runtime coordination state; losing it is better than failing Telegram startup.
rm -f "$HERMES_HOME"/kanban.db "$HERMES_HOME"/kanban.db-journal "$HERMES_HOME"/kanban.db-wal "$HERMES_HOME"/kanban.db-shm
rm -f "$HERMES_HOME"/state.db-journal "$HERMES_HOME"/state.db-wal "$HERMES_HOME"/state.db-shm

# Remove deprecated .env setting (now in config.yaml)
sed -i '/^MESSAGING_CWD=/d' "$HERMES_HOME/.env" 2>/dev/null || true

# Hermes sends active Telegram chats a "Gateway shutting down" warning on
# SIGTERM. Railway sends SIGTERM on every deploy, so that message leaks deploy
# noise to real users. Hermes does not currently expose a config flag for this,
# so patch the installed gateway at startup and keep the default silent.
python3 - <<'PY'
from pathlib import Path

run_py = Path("/home/drewp/.hermes/hermes-agent/gateway/run.py")
if run_py.exists():
    text = run_py.read_text()
    old = "            await self._notify_active_sessions_of_shutdown()\n"
    new = (
        "            if os.environ.get(\"HERMES_SUPPRESS_SHUTDOWN_NOTIFY\", \"1\").lower() "
        "not in (\"1\", \"true\", \"yes\", \"on\"):\n"
        "                await self._notify_active_sessions_of_shutdown()\n"
    )
    if old in text and new not in text:
        run_py.write_text(text.replace(old, new, 1))
PY

# Seed the Railway runtime profile from the repo profile every deploy,
# so config.yaml and SOUL.md updates always take effect.
cp -R /home/drewp/dremes-agent/profile/. "$HERMES_HOME/"
sed -i '/^MESSAGING_CWD=/d' "$HERMES_HOME/.env" 2>/dev/null || true

# Railway manual uploads have occasionally produced an empty website/gallery
# directory. Restore the shared Mini App templates from the pushed branch before
# starting the server so Telegram routes never 404.
mkdir -p /home/drewp/dremes-agent/website/gallery
for page in refs ads posts; do
  target="/home/drewp/dremes-agent/website/gallery/${page}.html"
  if [ ! -s "$target" ]; then
    curl -L --max-time 20 -fsS \
      "https://raw.githubusercontent.com/Pu11en/dremes-agent/master/website/gallery/${page}.html" \
      -o "$target" || true
  fi
done

# Keep script compatibility for tools that read .env, while Railway remains the
# source of truth for secrets.
python3 - <<'PY'
import os
from pathlib import Path
keys = [
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "NOTIFY_CHAT_ID",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "BLOTATO_API_KEY",
    "JINA_API_KEY",
    "HINDSIGHT_LLM_API_KEY",
    "REFS_VOLUME",
    "DATA_DIR",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
    "GITHUB_TOKEN",
]
# Write .env to HERMES_HOME so Hermes can find it
hermes_env = Path(os.environ.get("HERMES_HOME", "/tmp/.hermes")) / ".env"
lines = [f"{key}={os.environ[key]}" for key in keys if os.environ.get(key)]
hermes_env.write_text("\n".join(lines) + ("\n" if lines else ""))
# Also write to repo root for script compatibility
Path("/home/drewp/dremes-agent/.env").write_text("\n".join(lines) + ("\n" if lines else ""))
PY

echo "[start] Starting Dremes Gallery Server on port ${GALLERY_PORT}..."

# Auto-sync: periodically push agent-made changes to GitHub
if [ -n "$GITHUB_TOKEN" ]; then
  echo "[start] Starting auto-sync daemon (interval=${AUTO_SYNC_INTERVAL:-3600}s)..."
  bash /home/drewp/dremes-agent/auto-sync.sh &
  AUTO_SYNC_PID=$!
else
  echo "[start] WARNING: GITHUB_TOKEN not set — auto-sync disabled"
  AUTO_SYNC_PID=""
fi

# Auto-restart gallery if it crashes — the bot sometimes kills it by accident
auto_restart_gallery() {
  local restart_count=0
  while true; do
    echo "[start] Gallery starting (attempt $((restart_count + 1)))..."
    python3 website/server.py &
    GALLERY_PID=$!
    wait $GALLERY_PID 2>/dev/null
    EXIT_CODE=$?
    restart_count=$((restart_count + 1))
    echo "[start] Gallery exited (code $EXIT_CODE), restart in 3s..."
    sleep 3
  done
}

auto_restart_gallery &
GALLERY_SUPERVISOR=$!

cleanup() {
  echo "[start] Shutting down..."
  pkill -f "python3 website/server.py" 2>/dev/null || true
  kill $GALLERY_SUPERVISOR 2>/dev/null || true
  [ -n "$AUTO_SYNC_PID" ] && kill $AUTO_SYNC_PID 2>/dev/null || true
}
trap cleanup EXIT

echo "[start] Starting Hermes Telegram gateway..."
exec hermes gateway run --replace --accept-hooks
