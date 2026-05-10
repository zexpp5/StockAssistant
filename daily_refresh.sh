#!/bin/bash
# AI 股票看板每日自动刷新（19 步）
# 流程：抓价格 → SEC 13F → 13F→json → enrichment → 跨源审计 → v1 优选 → picks 反向审查
#       → 历史回顾 → v6 学术因子选股 → Markowitz 仓位优化 → 调整清单 → 写飞书
#       → 风险指标 → 优化方法对比 → 实盘防御 → OpenBB 综合情报
#       → [每周] 候选发现 → 重建 HTML
# 失败时弹 macOS 通知 + 写日志，不中断后续步骤
#
# 安装到 cron（每天早上 7:30 跑一次）：
#   crontab -e
# 然后添加（脚本会自动 cd 到自己所在目录，路径请改成你机器上的实际位置）：
#   30 7 * * * /Users/yanli/我的代码_新/线性视界/StockAssistant/daily_refresh.sh >> /Users/yanli/我的代码_新/线性视界/StockAssistant/daily_refresh.log 2>&1

# 默认值可以被环境变量覆盖；部署时 export DIR=/your/path
DIR="${DIR:-$(cd "$(dirname "$0")" && pwd)}"
PYTHON="${PYTHON:-python3}"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
FAILED_STEPS=()

cd "$DIR"

# 自动加载 .env（飞书凭证 + Finnhub key 等）
if [ -f "$DIR/.env" ]; then
    set -a; source "$DIR/.env"; set +a
fi

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

run_step "1/19 抓价格" "fetch_stock_prices.py"
run_step "2/19 SEC 13F 刷新" "-m stock_research.jobs.refresh_13f"
run_step "3/19 SEC 13F → track_13f.json（dashboard 用）" "_build_track_13f_from_sec.py"
run_step "4/19 多源 enrichment" "-m stock_research.jobs.enrich_watchlist --skip-trends"
run_step "5/19 跨源审计" "-m stock_research.jobs.daily_audit"
run_step "6/19 每日优选 v1（旧体系）" "daily_picks.py"
run_step "7/19 picks 反向审查" "-m stock_research.jobs.audit_picks --fast"
run_step "8/19 历史回顾" "weekly_review.py"

# v6 学术因子流水线（Piotroski + 12-1 动量 + 1 月反转 + PEAD + 分析师）
run_step "9/19 v6 学术因子选股 + 写飞书" "daily_picks_v5.py"
run_step "10/19 Markowitz 仓位优化（方案 A v6）" "build_plan_a_v5.py"
run_step "11/19 调整清单（卖/买/调）→ trade_delta.json" "trade_delta.py"
run_step "12/19 写飞书（trade_delta → 每日优选表）" "write_trade_delta_to_feishu.py"

# 专业分析数据
run_step "13/19 风险指标 (VaR/Sharpe/Calmar)" "risk_metrics.py"
run_step "14/19 仓位优化方法对比" "optimize_portfolio_legacy.py"
run_step "15/19 历史数据预拉（dashboard 历史 tab 用）" "_fetch_history_for_dashboard.py"

# v7 实盘防御（C 终极版：VIX + 200MA + 单股 -15% 止损 + 宏观 + PCR）
run_step "16/19 实盘防御检查" "-m stock_research.jobs.realtime_defense"

# v7.5 OpenBB 综合情报（宏观 + 行业轮动 + 商品 + PCR + 内部人）
run_step "17/19 OpenBB 综合情报" "-m stock_research.jobs.openbb_intelligence --quick"

# 候选发现：扫 SOXX/IGM/IRBO/BAI 找 watchlist 之外的因子高分股。
# 每只股票要拉 yfinance 财报+价格+分析师，全跑一次 ~20-30 分钟，每周刷新一次足够。
# 触发条件：data/discovery_candidates.json 不存在 或 mtime 距今 ≥ 6 天。
DISC_FILE="$DIR/data/discovery_candidates.json"
DISC_STALE=1
if [ -f "$DISC_FILE" ]; then
    AGE_SEC=$(( $(date +%s) - $(stat -f%m "$DISC_FILE" 2>/dev/null || stat -c%Y "$DISC_FILE") ))
    if [ "$AGE_SEC" -lt 518400 ]; then  # 6 days
        DISC_STALE=0
    fi
fi
if [ "$DISC_STALE" = "1" ]; then
    run_step "18/19 候选发现（每周）" "discover_candidates.py"
else
    AGE_DAY=$(( AGE_SEC / 86400 ))
    echo ""
    echo "[18/19 候选发现] 跳过 — 上次 $AGE_DAY 天前刚跑过（< 6 天，无需重跑）"
fi

run_step "19/19 重建 HTML" "build_stock_dashboard_html.py"

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
if [ -n "$FEISHU_BASE_TOKEN" ] && [ -n "$FEISHU_PICKS_TABLE_ID" ]; then
    echo "  每日优选：https://w5scrwkn9y.feishu.cn/base/$FEISHU_BASE_TOKEN?table=$FEISHU_PICKS_TABLE_ID"
fi

# log 文件 > 5MB 时滚动一次（保留 .1 备份）
LOG="$DIR/daily_refresh.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "$LOG.1"
fi

# 任何步骤失败 → 退出码 1（cron / 监控可识别）
[ ${#FAILED_STEPS[@]} -eq 0 ]
