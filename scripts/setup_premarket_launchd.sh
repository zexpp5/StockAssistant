#!/bin/bash
# Install / reload the US premarket risk gate launchd Agent.
#
# This is separate from setup_launchd.sh on purpose:
# - setup_launchd.sh installs the morning daily_refresh job.
# - this script installs the evening US premarket risk gate.
#
# Usage:
#   bash scripts/setup_premarket_launchd.sh
#   bash scripts/setup_premarket_launchd.sh --uninstall

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.linearview.stockassistant.premarket_gate"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$1" = "--uninstall" ]; then
    echo "Uninstall $LABEL ..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Done."
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>-m</string>
        <string>stock_research.jobs.premarket_gate</string>
    </array>

    <!--
      Beijing time.
      Summer US open = 21:30: run 20:10 / 20:45 / 21:15.
      Winter US open = 22:30: run 21:10 / 21:45 / 22:15.
      The job itself checks the valid premarket window, so off-season slots skip.
    -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>10</integer></dict>
        <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>15</integer></dict>
        <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>10</integer></dict>
        <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>15</integer></dict>
    </array>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>StandardOutPath</key>
    <string>$DIR/data/premarket_gate.log</string>

    <key>StandardErrorPath</key>
    <string>$DIR/data/premarket_gate.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

plutil -lint "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "Installed $LABEL"
echo "Verify:"
echo "  launchctl print gui/$(id -u)/$LABEL"
echo "Dry run:"
echo "  /opt/homebrew/bin/python3 -m stock_research.jobs.premarket_gate --dry-run"
