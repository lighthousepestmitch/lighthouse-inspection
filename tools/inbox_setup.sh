#!/bin/bash
# One-off setup for the Inspection Inbox. Written by Claude on 2026-11-02.
# Run it once in Terminal:   bash ~/lighthouse-inspection/tools/inbox_setup.sh
#
# It asks for your Job Calendar login email and password. The password goes straight into the Mac Keychain
# (you type it, the screen shows nothing, Claude never sees it). Then it does a first pull and sets up a
# 5 minute timer. To switch it off later:  launchctl bootout gui/$(id -u)/au.com.lighthousepest.inspection-inbox
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="au.com.lighthousepest.inspection-inbox"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

read -r -p "Your Job Calendar login email: " EMAIL
[ -n "$EMAIL" ] || { echo "No email, stopping."; exit 1; }

echo "Now type your Job Calendar password (nothing shows as you type), then again to confirm."
security add-generic-password -U -a "$EMAIL" -s lpc-inbox -w

printf '{"email": "%s"}\n' "$EMAIL" > "$HOME/.lpc-inbox.json"
chmod 600 "$HOME/.lpc-inbox.json"

echo "First pull (the last 7 days of jobs sent in)..."
echo "If a box asks to let Python or Terminal into your Desktop folder, click Allow."
/usr/bin/python3 "$HERE/pull_inbox.py" --verbose

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$HERE/pull_inbox.py</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/lpc-inbox.err</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "Done. New jobs will appear in ~/Desktop/Inspection Inbox about 5 minutes after they are sent."
open "$HOME/Desktop/Inspection Inbox" 2>/dev/null || true
