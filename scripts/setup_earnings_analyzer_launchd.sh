#!/bin/bash
# Install / reload the earnings signal analyzer launchd Agent.
#
# 财报信号自动体检：财报发布次日早 08:30，headless claude 联网读财报，
# 对照瓶颈/capex 清单生成白话体检卡推飞书。平日无在窗事件时秒退。
#
# Usage:
#   bash scripts/setup_earnings_analyzer_launchd.sh
#   bash scripts/setup_earnings_analyzer_launchd.sh --uninstall

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.linearview.stockassistant.earnings_analyzer"
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
        <string>stock_research.jobs.earnings_signal_analyzer</string>
    </array>

    <!-- 北京 08:30：盘后财报（北京凌晨发布）此时已出齐；无在窗事件时秒退 -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    </array>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>StandardOutPath</key>
    <string>$DIR/data/earnings_analyzer.log</string>

    <key>StandardErrorPath</key>
    <string>$DIR/data/earnings_analyzer.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL (daily 08:30)."
launchctl list | grep "$LABEL" || true
