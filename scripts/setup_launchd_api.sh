#!/bin/bash
# 把 stock_research API（uvicorn :8765）装成 macOS launchd Agent，常驻 + 开机自启。
#
# 为什么需要这个：stock_dashboard.html 的 Watchlist 编辑等页面要 fetch 127.0.0.1:8765，
# 不挂常驻 daemon 的话每次开机 / 重启都得手动 uvicorn，页面会卡在「✗ 未启动」。
#
# 与 setup_launchd.sh（daily_refresh）的区别：
#   - daily_refresh 是定时任务：StartCalendarInterval + KeepAlive=false
#   - API 是常驻服务：RunAtLoad=true + KeepAlive=true（崩溃自动拉起）
#
# 用法：
#   bash scripts/setup_launchd_api.sh           # 安装 / 重新加载
#   bash scripts/setup_launchd_api.sh --uninstall

set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.linearview.stockassistant.api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UVICORN="/opt/homebrew/bin/uvicorn"

if [ "$1" = "--uninstall" ]; then
    echo "🗑  卸载 $LABEL ..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✅ 已卸载。"
    exit 0
fi

if [ ! -x "$UVICORN" ]; then
    echo "❌ 找不到 uvicorn at $UVICORN — 先 pip install uvicorn 或改本脚本里的 UVICORN 路径"
    exit 1
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
        <string>$UVICORN</string>
        <string>stock_research.api.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8765</string>
    </array>

    <!-- 登录就启动 -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 进程退出 / 崩溃后自动拉起 -->
    <key>KeepAlive</key>
    <true/>

    <!-- 崩溃后最少等 10s 再拉，避免 import 失败时疯狂重启 -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>WorkingDirectory</key>
    <string>$DIR</string>

    <key>StandardOutPath</key>
    <string>$DIR/logs/api.log</string>

    <key>StandardErrorPath</key>
    <string>$DIR/logs/api.log</string>

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

mkdir -p "$DIR/logs"

echo "🔄 重载 launchd job ..."
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo ""
echo "✅ 装载完成。等 2-3s 让 uvicorn 起来..."
sleep 3

echo ""
echo "验证："
launchctl list | grep "$LABEL" || echo "  ⚠️  launchctl list 没看到 — 检查 $PLIST 语法"
echo ""
echo -n "  HTTP 探活: "
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/health || echo "未响应"

echo ""
echo "管理命令："
echo "  日志:           tail -f $DIR/logs/api.log"
echo "  手动重启:       launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "  停止 (临时):    launchctl unload $PLIST"
echo "  彻底卸载:       bash $0 --uninstall"
