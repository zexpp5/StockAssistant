#!/bin/bash
# 把 daily_refresh.sh 装成 macOS launchd Agent（替代 cron）。
#
# 为什么用 launchd：cron 在 macOS 上当机器睡眠时不会触发，
# launchd 的 StartCalendarInterval 会在「时间到 + 设备醒着」时执行；
# 错过的触发时间在下次唤醒时立即补跑（远比 cron 可靠）。
#
# 用法：
#   bash setup_launchd.sh           # 安装 / 重新加载
#   bash setup_launchd.sh --uninstall  # 卸载
#
# 装完后建议从 crontab 删掉旧条目：
#   crontab -e   →  删掉 daily_refresh.sh 那一行 → :wq

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.linearview.stockassistant.daily_refresh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$1" = "--uninstall" ]; then
    echo "🗑  卸载 $LABEL ..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✅ 已卸载。crontab 还在的话，记得 crontab -e 删旧条目。"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"

echo "📝 写入 plist → $PLIST"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$DIR/daily_refresh.sh</string>
    </array>

    <!-- 每天 07:30 触发；睡眠错过后醒来补跑 -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>7</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>

    <!-- 装载后不立刻跑（避免 install 时意外触发） -->
    <key>RunAtLoad</key>
    <false/>

    <!-- 跑完不常驻 -->
    <key>KeepAlive</key>
    <false/>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>StandardOutPath</key>
    <string>$DIR/daily_refresh.log</string>

    <key>StandardErrorPath</key>
    <string>$DIR/daily_refresh.log</string>

    <!-- 让脚本能找到 brew 装的 python 等 -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "🔄 重载 launchd job ..."
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo ""
echo "✅ 装载完成。"
echo ""
echo "验证："
launchctl list | grep "$LABEL" || echo "  ⚠️  list 中没看到，检查 ~/Library/LaunchAgents/$LABEL.plist 语法"

echo ""
echo "下一步："
echo "  1. crontab -e  → 删掉 daily_refresh.sh 那一行（避免双触发）"
echo "  2. 想立刻跑一次测试：launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "  3. 看下次触发时间：launchctl print gui/$(id -u)/$LABEL | grep -A2 'next run'"
