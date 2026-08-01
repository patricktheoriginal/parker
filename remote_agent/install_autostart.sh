#!/bin/bash
# install_autostart.sh — make the Parker Agent + tunnel start automatically when
# you log in to this Mac (via a LaunchAgent), and keep the Mac awake.
#
# Run once:   bash remote_agent/install_autostart.sh
# Remove:     bash remote_agent/install_autostart.sh --uninstall

set -u
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.parker.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
START="$PROJECT/remote_agent/start_mac_agent.sh"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "Removed Parker agent auto-start."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$START</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.parker_agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.parker_agent.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo "Installed. The Parker agent + tunnel will start on login and keep running."
echo "It also started now."
sleep 8
if [ -f "$HOME/.parker_agent_url.txt" ]; then
  echo "Current public URL: $(cat "$HOME/.parker_agent_url.txt")"
else
  echo "URL not ready yet — check ~/.parker_agent_url.txt in a few seconds."
fi
