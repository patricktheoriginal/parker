#!/bin/bash
# start_mac_agent.sh — run the Parker Agent + Cloudflare tunnel on this Mac,
# keep the machine awake, and write the public URL where you can read it.
#
# Manual use:   bash remote_agent/start_mac_agent.sh
# (Or it's launched automatically by the LaunchAgent — see install_autostart.sh.)

set -u
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$PROJECT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
PORT=8770
URL_FILE="$HOME/.parker_agent_url.txt"
LOG="$HOME/.parker_agent.log"

echo "$(date) — starting Parker agent stack" >> "$LOG"

# Keep the Mac awake (no sleep) while this runs. -i prevents idle sleep.
caffeinate -i -w $$ &

# Start the agent (files + command execution). Restart it if it dies.
(
  while true; do
    "$PY" "$PROJECT/remote_agent/agent.py" --port "$PORT" --allow-exec >>"$LOG" 2>&1
    echo "$(date) — agent exited, restarting in 3s" >> "$LOG"
    sleep 3
  done
) &

sleep 2

# Start the Cloudflare tunnel.
#  - If a NAMED tunnel is configured (remote_agent/tunnel.conf sets NAMED_HOST),
#    use it → the URL is FIXED (https://NAMED_HOST). Set once, never changes.
#  - Otherwise fall back to a quick tunnel with a random *.trycloudflare.com URL.
TUNNEL_CONF="$PROJECT/remote_agent/tunnel.conf"
NAMED_HOST=""
NAMED_NAME=""
[ -f "$TUNNEL_CONF" ] && . "$TUNNEL_CONF"

if command -v cloudflared >/dev/null 2>&1; then
  if [ -n "$NAMED_HOST" ] && [ -n "$NAMED_NAME" ]; then
    echo "https://$NAMED_HOST" > "$URL_FILE"
    echo "$(date) — using named tunnel: https://$NAMED_HOST" >> "$LOG"
    (
      while true; do
        cloudflared tunnel run "$NAMED_NAME" >>"$LOG" 2>&1
        echo "$(date) — named tunnel exited, restarting in 5s" >> "$LOG"
        sleep 5
      done
    ) &
  else
    (
      while true; do
        cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | while read -r line; do
          echo "$line" >> "$LOG"
          u=$(echo "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
          if [ -n "$u" ]; then
            echo "$u" > "$URL_FILE"
            echo "$(date) — quick tunnel URL: $u" >> "$LOG"
          fi
        done
        echo "$(date) — quick tunnel exited, restarting in 5s" >> "$LOG"
        sleep 5
      done
    ) &
  fi
else
  echo "cloudflared not installed — LAN only. Install: brew install cloudflared" >> "$LOG"
fi

echo "Parker agent stack started."
echo "Public URL will be written to: $URL_FILE"
echo "Logs: $LOG"

# Keep this script alive so caffeinate (-w \$\$) stays active.
wait
