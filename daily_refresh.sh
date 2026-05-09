#!/bin/bash
# AI 股票看板每日自动刷新（8 步）
# 流程：抓价格 → SEC 13F → enrichment → 跨源审计 → 优选 → picks 反向审查 → 历史回顾 → HTML
# 失败时弹 macOS 通知 + 写日志，不中断后续步骤
#
# 安装到 cron（每天早上 7:30 跑一次）：
#   crontab -e
# 然后添加：
#   30 7 * * * /Users/yanli/StockAssistant/daily_refresh.sh >> /Users/yanli/StockAssistant/daily_refresh.log 2>&1

DIR="/Users/yanli/StockAssistant"
PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
FAILED_STEPS=()

cd "$DIR"

notify() {
    # macOS 桌面通知（osascript），跨网络/无人值守也能看到
    local title="$1"
    local msg="$2"
    osascript -e "display notification \"$msg\" with title \"$title\"" 2>/dev/null
}

run_step() {
    local label="$1"
    local script="$2"
    echo ""
    echo "[$label] $script ..."
    # script 以 '-m' 开头 → 当成 python -m module 调用，不传相对脚本名
    if [[ "$script" == -m* ]]; then
        if ! $PYTHON $script; then
            echo "❌ [$label] $script 失败"
            FAILED_STEPS+=("$label/$script")
            notify "📉 股票看板刷新失败" "$label: $script"
        fi
    else
        if ! $PYTHON "$script"; then
            echo "❌ [$label] $script 失败"
            FAILED_STEPS+=("$label/$script")
            notify "📉 股票看板刷新失败" "$label: $script"
        fi
    fi
}

echo ""
echo "================================================"
echo "  ⏰ $TIMESTAMP — 每日刷新开始"
echo "================================================"

run_step "1/8 抓价格" "fetch_stock_prices.py"
run_step "2/8 SEC 13F 刷新" "-m stock_research.jobs.refresh_13f"
run_step "3/8 多源 enrichment" "-m stock_research.jobs.enrich_watchlist --skip-trends"
run_step "4/8 跨源审计" "-m stock_research.jobs.daily_audit"
run_step "5/8 每日优选" "daily_picks.py"
run_step "6/8 picks 反向审查" "-m stock_research.jobs.audit_picks --fast"
run_step "7/8 历史回顾" "weekly_review.py"
run_step "8/8 重建 HTML" "build_stock_dashboard_html.py"

DONE_TS=$(date '+%Y-%m-%d %H:%M:%S')
echo ""
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    echo "✅ 全部完成 — $DONE_TS"
    notify "✅ 股票看板刷新完成" "$DONE_TS · 库已更新"
else
    echo "⚠️  完成但有失败 — $DONE_TS"
    echo "   失败步骤："
    for s in "${FAILED_STEPS[@]}"; do
        echo "     - $s"
    done
fi
echo "  HTML：$DIR/stock_dashboard.html"
echo "  DuckDB：$DIR/stock_history.duckdb"
echo "  每日优选：https://w5scrwkn9y.feishu.cn/base/CuiybJoOMafb9HsZbu2cfVhfnZg?table=tbl7K88JZ0ZMqPIE"

# log 文件 > 5MB 时滚动一次（保留 .1 备份）
LOG="$DIR/daily_refresh.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "$LOG.1"
fi

# 任何步骤失败 → 退出码 1（cron / 监控可识别）
[ ${#FAILED_STEPS[@]} -eq 0 ]
