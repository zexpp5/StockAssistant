#!/bin/bash
# AI 股票看板每日自动刷新
#
# 🏛️ 2026-05-11 PM 第二轮:飞书 Bitable 100% 退役 — DuckDB 是 single source of truth
#   ▸ 所有数据读写都走 stock_history.duckdb,飞书 Bitable 不再被读也不再被写
#   ▸ 飞书剩余角色:morning_brief / defense_watcher 通过 webhook 推送群机器人卡片
#   ▸ Dashboard 数据全部来自 DuckDB (records + picks + prices JOIN)
#   ▸ Watchlist 编辑入口:dashboard 内联 CRUD modal (写 DuckDB)
#
# 流程：抓价格 → SEC 13F → 13F→json → enrichment → 跨源审计 → v1 优选 → picks 反向审查
#       → 历史回顾 → v6 学术因子选股 → risk-aware 仓位优化 → 调整清单
#       → 风险指标 → 优化方法对比 → 实盘防御 → OpenBB 综合情报
#       → [A 股] IPO 日历 + 事件日历 + 政策事件
#       → [A 股] 选股闭环 + plan_a 后处理约束
#       → [每周] 候选发现 → DuckDB pipeline 同步 → 重建 HTML
# 注意：watchlist/自选股只允许用户通过 dashboard 手动维护，不从 universe 自动回填。
# 失败时弹 macOS 通知 + 写日志，不中断后续步骤
#
# 安装到 cron（推荐双时段，否则 A 股闭环跑出脏数据）：
#   crontab -e
# 然后添加（路径改成你机器上的实际位置）：
#   # 早上 7:30 — 美股 + 全部不依赖 A 股盘后数据的步骤；A 股闭环（21/22）会自动 skip
#   30 7  * * * /Users/yanli/我的代码_新/线性视界/StockAssistant/daily_refresh.sh >> /Users/yanli/我的代码_新/线性视界/StockAssistant/daily_refresh.log 2>&1
#   # 16:30 工作日 — 仅跑 A 股闭环。北向 T+1 + 龙虎榜盘后才出，必须等收盘后跑
#   30 16 * * 1-5 /Users/yanli/我的代码_新/线性视界/StockAssistant/daily_refresh.sh --a-share-only >> /Users/yanli/我的代码_新/线性视界/StockAssistant/daily_refresh.log 2>&1
#
# 模式：
#   ./daily_refresh.sh                 全部步骤（A 股闭环按时间自动跳过/执行）
#   ./daily_refresh.sh --a-share-only  仅跑 A 股闭环 + DuckDB 同步 + 重建 HTML
#   ./daily_refresh.sh --skip-a-share  完全跳过 A 股闭环（极端 fallback）

MODE="full"
for arg in "$@"; do
    case "$arg" in
        --a-share-only) MODE="a_share_only" ;;
        --skip-a-share) MODE="skip_a_share" ;;
    esac
done

# 默认值可以被环境变量覆盖；部署时 export DIR=/your/path
DIR="${DIR:-$(cd "$(dirname "$0")" && pwd)}"
if [ -z "${PYTHON:-}" ]; then
    if [ -x "/opt/homebrew/bin/python3" ]; then
        PYTHON="/opt/homebrew/bin/python3"
    else
        PYTHON="python3"
    fi
fi
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
FAILED_STEPS=()

# A 股是否在收盘后时段（hour >= 16，含周末）。北向 T+1、龙虎榜盘后才发布，
# 16:00 之前跑 a_share_picks 的"今日"信号实际是 T-1 的，会污染选股结果。
HOUR=$(date +%H)
DOW=$(date +%u)   # 1-7，6/7 是周末
A_SHARE_READY=0
if [ "$DOW" -ge 6 ] || [ "$HOUR" -ge 16 ]; then
    A_SHARE_READY=1
fi

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
        # 不能引号化 $script：当 script 形如 "path/x.py --dry-run" 需要 shell 拆词
        if ! $PYTHON $script; then
            echo "❌ [$label] $script 失败"
            FAILED_STEPS+=("$label/$script")
            notify "📉 股票看板刷新失败" "$label: $script"
        fi
    fi
}

echo ""
echo "================================================"
echo "  ⏰ $TIMESTAMP — 每日刷新开始（mode=$MODE, a_share_ready=$A_SHARE_READY）"
echo "================================================"

# ── A 股闭环步骤封装：单独定义以便两种模式复用 ──
# Step 21b (写飞书 A 股优选) 已废 (2026-05-11 PM 第二轮): 飞书 Bitable 100% 退役
# 2026-05-12: step 22 (apply_a_share_constraints) 拆出独立跑 — 它处理的是美股 plan_v5
#   不是 A 股 picks，不该被 A 股收盘时间锁；之前合在这里导致美股 plan_constrained 卡 33 小时
run_a_share_steps() {
    if [ "$MODE" = "skip_a_share" ]; then
        echo ""
        echo "[A 股闭环] 跳过 — --skip-a-share 模式"
        return
    fi
    if [ "$A_SHARE_READY" = "0" ]; then
        echo ""
        echo "[21/25 A 股优选] 跳过 — 当前 ${HOUR}:00 非 A 股收盘后时段（要求 ≥16:00 工作日 或 周末）"
        echo "  原因：北向资金 T+1、龙虎榜盘后才发布，盘前/盘中跑会用 T-1 数据污染选股"
        echo "  收盘后请单独跑：./daily_refresh.sh --a-share-only"
        return
    fi
    # require-after-close：python 层再做一次防御，万一 cron 配错也不会跑出脏数据
    # 2026-05-12 三审：去掉 --dry-run，让 a_share_picks 真写 DuckDB picks 表
    # （并行会话 b65a488 已加 A 股入 DuckDB 逻辑，不开 dry-run 才能生效）
    run_step "21/25 A 股优选（6 因子闭环，写 DuckDB）" "-m stock_research.jobs.a_share_picks --require-after-close"
}

# ── --a-share-only 模式：只跑 A 股闭环 + DuckDB 同步 + 重建 HTML，跳过其他 ──
if [ "$MODE" = "a_share_only" ]; then
    if [ "$A_SHARE_READY" = "0" ]; then
        echo "❌ --a-share-only 但当前非收盘后时段 ($(date +%H):%M)，退出（避免脏数据）"
        exit 1
    fi
    run_a_share_steps
    # a_share_picks 跑完后重跑约束器 — A 股 holdings 可能变化，需要刷新美股 plan_constrained
    run_step "10b/25 plan_a 后处理（美股仓位约束）" "-m stock_research.jobs.apply_a_share_constraints"
    run_step "24/25 DuckDB pipeline 同步" "scripts/migrate/migrate_pipeline_to_duckdb.py"
    run_step "24b/25 产业链分级标注（重建 HTML 前）" "scripts/tools/classify_watchlist_chains.py"
    run_step "25/25 重建 HTML" "scripts/pipeline/build_stock_dashboard_html.py"
    run_step "26 早安简报（主入口 · 每天打开看这一份）" "-m stock_research.jobs.morning_brief"
    run_step "27 生产闭环验收" "scripts/tools/production_acceptance_check.py"
    DONE_TS=$(date '+%Y-%m-%d %H:%M:%S')
    echo ""
    if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
        echo "✅ A 股闭环完成 — $DONE_TS"
        notify "✅ A 股闭环完成" "$DONE_TS"
    else
        echo "⚠️  A 股闭环有失败 — $DONE_TS"
        for s in "${FAILED_STEPS[@]}"; do echo "     - $s"; done
    fi
    [ ${#FAILED_STEPS[@]} -eq 0 ]
    exit $?
fi

run_step "1/25 抓价格" "scripts/pipeline/fetch_stock_prices.py"
run_step "2/25 SEC 13F 刷新" "-m stock_research.jobs.refresh_13f"
run_step "3/25 SEC 13F → track_13f.json（dashboard 用）" "scripts/pipeline/_build_track_13f_from_sec.py"
run_step "4/25 多源 enrichment" "-m stock_research.jobs.enrich_watchlist --skip-trends"
run_step "5/25 跨源审计" "-m stock_research.jobs.daily_audit"
run_step "6/25 每日优选 v1（旧体系 · dry-run 基线）" "scripts/pipeline/daily_picks.py --dry-run"
run_step "7/25 picks 反向审查" "-m stock_research.jobs.audit_picks --fast"
run_step "8/25 历史回顾" "scripts/pipeline/weekly_review.py"

# 每日新闻同步飞书（财联社 100 条 → 国际/国内分类 → 删历史只保留当天）
run_step "8b/25 每日新闻同步飞书" "scripts/daily_news_to_feishu.py"

# v6 学术因子流水线（Piotroski + 12-1 动量 + 1 月反转 + PEAD + 分析师）
run_step "9/25 v6 学术因子选股（已落 DuckDB picks）" "scripts/pipeline/daily_picks_v5.py"
# 9b 港股 picks：3 因子学术版（F-Score + 12-1 mom + 1m rev）
# south_flow 模块已写但权重临时 0（五审发现 akshare 个股持股 % API 列名失效）
# 待 daily prefetch cache 方案落地后恢复 south_flow 0.15
# 早班可跑（akshare 年报 + yfinance entry），与美股 daily_picks_v5 同时段
run_step "9b/25 港股 picks（3 因子 + DuckDB picks 表，south_flow standby）" "scripts/pipeline/hk_picks.py"
run_step "10/25 risk-aware 仓位优化（方案 A v6）" "-m stock_research.jobs.optimize_portfolio"
# 2026-05-12: step 22 (apply_a_share_constraints) 从 run_a_share_steps 拆出来挪到这里
#   它的输入是 plan_a_v5.json（美股 plan），输出 plan_a_v5_constrained.json
#   命名上叫 "a_share_constraints" 但实际处理美股仓位约束（A 股 holdings → 美股 plan），
#   不该和 A 股 picks 绑定收盘时间。早班 7:30 就要跑出最新 constrained 版供 dashboard 用。
run_step "10b/25 plan_a 后处理（美股仓位约束）" "-m stock_research.jobs.apply_a_share_constraints"
run_step "10c/25 推荐质量闸门（调仓前）" "scripts/tools/recommendation_quality_gate.py"
run_step "11/25 调整清单（卖/买/调）→ trade_delta.json" "scripts/pipeline/trade_delta.py"
# Step 12 已废 (2026-05-11 PM 第二轮): 飞书 Bitable 100% 退役,trade_delta 走 JSON+DuckDB

# 专业分析数据
run_step "13/25 风险指标 (VaR/Sharpe/Calmar)" "scripts/pipeline/risk_metrics.py"
run_step "14/25 仓位优化方法对比" "scripts/pipeline/optimize_portfolio_legacy.py"
run_step "15/25 历史数据预拉（dashboard 历史 tab 用）" "scripts/pipeline/_fetch_history_for_dashboard.py"

# v7 实盘防御（C 终极版：VIX + 200MA + 单股 -15% 止损 + 宏观 + PCR）
run_step "16/25 实盘防御检查" "-m stock_research.jobs.realtime_defense"

# v7.5 OpenBB 综合情报（宏观 + 行业轮动 + 商品 + PCR + 内部人）
run_step "17/25 OpenBB 综合情报" "-m stock_research.jobs.openbb_intelligence --quick"

# v8.0 A 股事件层（新增）：
#   - IPO 日历：每天抓即将申购+已申购未上市+近 30 日上市，AI 主题打标
#   - 事件日历：解禁 90d + 减增持 ±60d + 最近 4 季财报公告日（PEAD 用真实日）
#   - 政策事件：扫 7 天新闻流，识别政策受益主题（用于 daily_picks 主题加权）
run_step "18/25 IPO 打新日历" "-m stock_research.jobs.ipo_daily"
run_step "19/25 事件日历（解禁/减持/财报）" "-m stock_research.jobs.event_calendar_daily"
run_step "20/25 产业政策事件扫描" "-m stock_research.jobs.policy_scan_daily"

# v9.0 A 股选股闭环：
#   - a_share_picks: 6 因子合成（Piotroski + 动量 + 反转 + LHB + 北向 + PEAD + 政策）
#                    + 风险加权 + ST/涨停过滤 + sector_cap → data/a_share_picks.json
# ⚠️ 仅在收盘后（≥16:00 工作日 或 周末）执行；早班 7:30 跑会被 A_SHARE_READY=0 跳过
# 注：apply_a_share_constraints 已从此处拆出（见 step 10b），不再受收盘时间锁
run_a_share_steps

# AI 推荐：用今天已落 DuckDB 的 prices + picks 做全池快速排名。
# 不排除 watchlist，避免 AI 推荐页变成"自选股之外"的补充名单。
# 深因子全量慢跑保留在 scripts/tools/discover_candidates.py，适合离线/周末跑。
run_step "23/25 全池 AI 推荐（每日）" "scripts/tools/build_pool_recommendations.py"

# 2026-05-11 PM: 推荐准确度评估 — 每天跑(即使 discovery 本身跳过),
# 因为要给过去 70 天的所有推荐刷新 1d/5d/20d/60d alpha 数据。
run_step "23b/25 推荐准确度评估（每日）" "scripts/tools/evaluate_discovery.py"
run_step "23c/25 推荐质量闸门（收盘后复核）" "scripts/tools/recommendation_quality_gate.py"
run_step "23d/25 推荐有效性证据报告" "scripts/tools/recommendation_evidence_report.py"

# DuckDB pipeline 同步：把今天刷新过的根目录数据 JSON（risk_metrics / track_13f / plan_a_v5
# / history_data / optimization_result / factor_scores_today / reverse_validation_*）
# 增量插入到 stock_history.duckdb 的 snapshots(category='pipeline') 表，
# 使「数据源切换 = DuckDB」的看板能拿到当天数据。脚本幂等，按 mtime 时间戳去重。
run_step "24/25 DuckDB pipeline 同步" "scripts/migrate/migrate_pipeline_to_duckdb.py"

run_step "24b/25 产业链分级标注（重建 HTML 前）" "scripts/tools/classify_watchlist_chains.py"

run_step "25/25 重建 HTML" "scripts/pipeline/build_stock_dashboard_html.py"

# 25b 周一专属：walk-forward OOS 校验（验证近 12 个月因子组合月度表现）
# 学术依据：Bailey & Lopez de Prado (2014) JPM — walk-forward 是减少 backtest overfit 的金标准
# DOW=1 表示周一（date +%u 输出 1=Mon...7=Sun）；其他日子跳过
if [ "$DOW" = "1" ]; then
    WF_START=$($PYTHON -c "from datetime import date; d=date.today(); y=d.year-1; print(f'{y}-{d.month:02d}')")
    WF_END=$(date '+%Y-%m')
    run_step "25b/25 walk-forward OOS 校验（每周一）" \
        "-m stock_research.jobs.walk_forward_backtest --start $WF_START --end $WF_END --top-k 5"
else
    echo ""
    echo "[25b/25 walk-forward OOS] 跳过 — 仅周一执行（今天 weekday=$DOW，1=Mon）"
fi

# 26 早安简报 — 这是真正的"主入口"，把所有 JSON 拼成一份每天能读完的 markdown。
# 设了 FEISHU_BRIEF_WEBHOOK 还会自动推送到飞书群机器人。
run_step "26 早安简报（主入口 · 每天打开看这一份）" "-m stock_research.jobs.morning_brief"
run_step "27 生产闭环验收" "scripts/tools/production_acceptance_check.py"

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
echo "  📋 主入口 — 早安简报：$DIR/morning_brief.md"
echo "  HTML（调试）：$DIR/stock_dashboard.html"
echo "  DuckDB（数据落地）：$DIR/stock_history.duckdb"

# log 文件 > 5MB 时滚动一次（保留 .1 备份）
LOG="$DIR/daily_refresh.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "$LOG.1"
fi

# 任何步骤失败 → 退出码 1（cron / 监控可识别）
[ ${#FAILED_STEPS[@]} -eq 0 ]
