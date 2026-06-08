#!/bin/bash
# Install / reload the intraday defense watcher launchd Agent.
#
# The agent runs the watcher every 5 minutes during the US-market Beijing-time
# window. One rule, easy to remember: scan every 5 minutes.
#
# Usage:
#   bash scripts/setup_defense_watcher_launchd.sh
#   bash scripts/setup_defense_watcher_launchd.sh --uninstall

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.linearview.stockassistant.defense_watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="/opt/homebrew/bin/python3"

if [ "$1" = "--uninstall" ]; then
    echo "Uninstall $LABEL ..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Done."
    exit 0
fi

if [ ! -x "$PYTHON" ]; then
    echo "Cannot find python at $PYTHON"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$DIR/data"

tmp="$(mktemp)"

cat > "$tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>stock_research.jobs.defense_watcher</string>
    </array>

    <key>StartCalendarInterval</key>
    <array>
EOF

add_slot() {
    local weekday="$1"
    local hour="$2"
    local minute="$3"
    cat >> "$tmp" <<EOF
        <dict>
            <key>Weekday</key><integer>$weekday</integer>
            <key>Hour</key><integer>$hour</integer>
            <key>Minute</key><integer>$minute</integer>
        </dict>
EOF
}

# launchd Weekday: 1=Monday ... 6=Saturday, 0/7=Sunday.
# Beijing evening: Monday-Friday 20:00-23:55.
for weekday in 1 2 3 4 5; do
    for hour in 20 21 22 23; do
        for minute in 0 5 10 15 20 25 30 35 40 45 50 55; do
            add_slot "$weekday" "$hour" "$minute"
        done
    done
done

# Beijing after midnight: Tuesday-Saturday 00:00-05:15.
for weekday in 2 3 4 5 6; do
    for hour in 0 1 2 3 4; do
        for minute in 0 5 10 15 20 25 30 35 40 45 50 55; do
            add_slot "$weekday" "$hour" "$minute"
        done
    done
    for minute in 0 5 10 15; do
        add_slot "$weekday" 5 "$minute"
    done
done

cat >> "$tmp" <<EOF
    </array>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>StandardOutPath</key>
    <string>$DIR/data/defense_watcher.log</string>

    <key>StandardErrorPath</key>
    <string>$DIR/data/defense_watcher.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONPATH</key>
        <string>$DIR</string>
    </dict>
</dict>
</plist>
EOF

mv "$tmp" "$PLIST"

plutil -lint "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "Installed $LABEL"
echo "Verify:"
echo "  launchctl print gui/$(id -u)/$LABEL"
echo "Dry run:"
echo "  $PYTHON -m stock_research.jobs.defense_watcher --dry-run --no-holdings"
echo ""
echo "If an old crontab defense_watcher entry still exists, remove it to avoid duplicate 15-minute runs."
