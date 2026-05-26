#!/bin/bash
# AI 股票看板每日自动刷新
#
# 🏛️ 2026-05-11 PM 第二轮:飞书 Bitable 100% 退役 — DuckDB 是 single source of truth
#   ▸ 所有数据读写都走 stock_history_v2.duckdb（V2 库；V1 stock_history.duckdb 已退役），飞书 Bitable 不再被读也不再被写
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
        --morning)      MODE="morning" ;;      # 08:30 早班：快线，跳过慢研究
        --research)     MODE="research" ;;     # 21:00 夜班：只跑慢研究
    esac
done

# is_research_step / is_morning_step：用来给慢研究步骤打守卫，让 morning 跳过、research 才跑
is_research_step() {
    [ "$MODE" = "full" ] || [ "$MODE" = "research" ]
}
# is_morning_step：morning + full 都跑；research 跳过（不重复算）
is_morning_step() {
    [ "$MODE" = "full" ] || [ "$MODE" = "morning" ]
}
# needs_ipo_data：IPO & 次新股 tab 数据源（step 18 + 19b），早班/夜班/full 都跑
# 早班 8:30 看「今日可申购」必须有最新数据；A 股收盘单独闭环（--a-share-only）跳过
# 实测 19b 全量 ~15s（美股缓存命中、A 股次新股池 77 只逐个拉），早班可接受
needs_ipo_data() {
    [ "$MODE" = "full" ] || [ "$MODE" = "research" ] || [ "$MODE" = "morning" ]
}

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
PIPELINE_RUN_ID="${MODE}_$(date "+%Y%m%d_%H%M%S")"
PIPELINE_STARTED_AT="$TIMESTAMP"

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
A_SHARE_PRODUCTION_MODE="${A_SHARE_PRODUCTION_MODE:-auto}"
export A_SHARE_PRODUCTION_MODE

notify() {
    # macOS 桌面通知（osascript），跨网络/无人值守也能看到
    local title="$1"
    local msg="$2"
    osascript -e "display notification \"$msg\" with title \"$title\"" 2>/dev/null
}

# D' 防止两个 daily_refresh.sh 同时跑（2026-05-25 事故根因：旧 run 残留 + 新 run 启动
# → 同时持有 DuckDB 写锁 → 整条 pipeline 秒级 FAIL）。必须在 notify 定义之后。
PID_LOCK="$DIR/.daily_refresh.pid"
if [ -f "$PID_LOCK" ]; then
    OLD_PID=$(cat "$PID_LOCK" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "❌ 另一个 daily_refresh.sh 还在跑（PID $OLD_PID，mode 未知），本轮退出避免 DuckDB 锁冲突。"
        notify "🚫 daily_refresh 跳过" "PID $OLD_PID 仍在运行"
        exit 1
    else
        echo "⚠️  发现旧 PID 文件 ($OLD_PID 已死)，清理后继续。"
        rm -f "$PID_LOCK"
    fi
fi
echo $$ > "$PID_LOCK"
trap 'rm -f "$PID_LOCK"' EXIT

PIPELINE_STATUS_DIR="$DIR/data/latest"
PIPELINE_STATUS_FILE="$PIPELINE_STATUS_DIR/pipeline_status.json"
PIPELINE_STATUS_STEPS="$PIPELINE_STATUS_DIR/.pipeline_status_${PIPELINE_RUN_ID}.jsonl"
mkdir -p "$PIPELINE_STATUS_DIR"
: > "$PIPELINE_STATUS_STEPS"

pipeline_sink_for_label() {
    local label="$1"
    case "$label" in
        *"抓价格"*) echo "DuckDB.price_daily + data/prices_*.json" ;;
        *"13F → track_13f"*) echo "data/latest/track_13f.json + DuckDB.snapshots" ;;
        *"SEC 13F"*) echo "data/sec_13f/* + data/latest/track_13f.json" ;;
        *"多源 enrichment"*) echo "DuckDB.source_raw_snapshots(v2_system_enrichment)" ;;
        *"V2 系统池 enrichment"*) echo "DuckDB.source_raw_snapshots(v2_system_enrichment) + financial_statements" ;;
        *"跨源审计"*) echo "data/snapshots/audit/*" ;;
        *"每日优选"*|*"v6 学术因子"*|*"港股 picks"*|*"A 股优选"*) echo "DuckDB.recommendation_picks + data/latest/factor caches" ;;
        *"picks 反向审查"*) echo "DuckDB.snapshots(category='picks_audit')" ;;
        *"历史回顾"*) echo "DuckDB.pick_outcomes" ;;
        *"每日新闻"*) echo "Feishu/news sync output" ;;
        *"仓位优化"*|*"plan_a 后处理"*) echo "data/latest/plan_a_v5*.json + data/latest/optimization_result.json" ;;
        *"推荐质量闸门"*) echo "data/latest/recommendation_quality_gate.json" ;;
        *"调整清单"*) echo "data/latest/trade_delta*.json" ;;
        *"风险指标"*) echo "data/latest/risk_metrics.json" ;;
        *"历史数据预拉"*) echo "data/latest/history_data.json" ;;
        *"实盘防御"*) echo "data/snapshots/audit/realtime_defense_*.json" ;;
        *"OpenBB"*) echo "data/snapshots/audit/openbb_intel_*.json + docs/letters/*" ;;
        *"IPO"*) echo "data/ipo_calendar.json + data/reports/ipo_daily_*.md" ;;
        *"事件日历"*) echo "data/event_calendar.json" ;;
        *"次新股+解禁雷达"*) echo "data/latest/junior_stock_radar.json" ;;
        *"政策"*) echo "data/policy_events.json" ;;
        *"全池 AI 推荐"*) echo "data/discovery_candidates.json + DuckDB.recommendation_runs/picks" ;;
        *"推荐准确度"*) echo "DuckDB.pick_outcomes" ;;
        *"推荐有效性"*) echo "data/latest/recommendation_evidence.json + data/reports/recommendation_evidence.md" ;;
        *"DuckDB pipeline"*) echo "DuckDB.snapshots(category='pipeline')" ;;
        *"产业链分级"*) echo "DuckDB.system_universe(theme/industry → chain 推断)" ;;
        *"重建 HTML"*) echo "stock_dashboard.html" ;;
        *"walk-forward"*) echo "data/latest/walk_forward*.json + strategy validation artifacts" ;;
        *"早安简报"*) echo "morning_brief.md + data/reports/morning_brief_*.md" ;;
        *"生产闭环验收"*) echo "data/latest/production_acceptance_check.json" ;;
        *"汇率"*) echo "data/latest/fx_rates.json + /api/fx-rates" ;;
        *) echo "见脚本输出" ;;
    esac
}

record_pipeline_step() {
    local label="$1"
    local script="$2"
    local status="$3"
    local started_at="$4"
    local ended_at="$5"
    local duration_seconds="$6"
    local sink
    sink="$(pipeline_sink_for_label "$label")"
    PIPELINE_STATUS_STEPS="$PIPELINE_STATUS_STEPS" \
    PIPELINE_STEP_LABEL="$label" \
    PIPELINE_STEP_SCRIPT="$script" \
    PIPELINE_STEP_STATUS="$status" \
    PIPELINE_STEP_STARTED_AT="$started_at" \
    PIPELINE_STEP_ENDED_AT="$ended_at" \
    PIPELINE_STEP_DURATION="$duration_seconds" \
    PIPELINE_STEP_SINK="$sink" \
    "$PYTHON" - <<'PY' || true
import json
import os

row = {
    "label": os.environ.get("PIPELINE_STEP_LABEL", ""),
    "script": os.environ.get("PIPELINE_STEP_SCRIPT", ""),
    "status": os.environ.get("PIPELINE_STEP_STATUS", ""),
    "started_at": os.environ.get("PIPELINE_STEP_STARTED_AT", ""),
    "ended_at": os.environ.get("PIPELINE_STEP_ENDED_AT", ""),
    "duration_seconds": int(os.environ.get("PIPELINE_STEP_DURATION") or 0),
    "sink": os.environ.get("PIPELINE_STEP_SINK", ""),
}
with open(os.environ["PIPELINE_STATUS_STEPS"], "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}

write_pipeline_status() {
    local status="$1"
    local completed_at="$2"
    PIPELINE_STATUS_FILE="$PIPELINE_STATUS_FILE" \
    PIPELINE_STATUS_STEPS="$PIPELINE_STATUS_STEPS" \
    PIPELINE_RUN_ID="$PIPELINE_RUN_ID" \
    PIPELINE_MODE="$MODE" \
    PIPELINE_STATUS="$status" \
    PIPELINE_STARTED_AT="$PIPELINE_STARTED_AT" \
    PIPELINE_COMPLETED_AT="$completed_at" \
    PIPELINE_A_SHARE_READY="$A_SHARE_READY" \
    PIPELINE_A_SHARE_MODE="$A_SHARE_PRODUCTION_MODE" \
    "$PYTHON" - <<'PY' || true
import json
import os
import tempfile
from collections import Counter
from datetime import datetime

steps = []
steps_path = os.environ["PIPELINE_STATUS_STEPS"]
if os.path.exists(steps_path):
    with open(steps_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                steps.append(json.loads(line))

counts = Counter(step.get("status") for step in steps)
failed = [step for step in steps if step.get("status") == "FAIL"]
slowest = sorted(steps, key=lambda s: s.get("duration_seconds") or 0, reverse=True)[:8]

payload = {
    "run_id": os.environ.get("PIPELINE_RUN_ID"),
    "mode": os.environ.get("PIPELINE_MODE"),
    "status": os.environ.get("PIPELINE_STATUS"),
    "started_at": os.environ.get("PIPELINE_STARTED_AT"),
    "updated_at": datetime.now().isoformat(timespec="seconds"),
    "completed_at": os.environ.get("PIPELINE_COMPLETED_AT") or None,
    "a_share_ready": os.environ.get("PIPELINE_A_SHARE_READY") == "1",
    "a_share_mode": os.environ.get("PIPELINE_A_SHARE_MODE"),
    "schedule": [
        {"name": "早盘主线", "planned_time": "08:30", "scope": "美股为主，补 A 股/港股最新可用行情", "command": "./daily_refresh.sh"},
        {"name": "A/H 收盘线", "planned_time": "16:30", "scope": "A 股、港股收盘行情和 picks", "command": "./daily_refresh.sh --a-share-only"},
        {"name": "增强研究线", "planned_time": "21:00", "scope": "财报、13F、OpenBB、历史行情、风险指标、深度研究材料", "command": "待拆分 research_refresh.sh"},
        {"name": "周频策略验证", "planned_time": "每周一", "scope": "walk-forward、策略周报、组合表现复盘", "command": "daily_refresh.sh 周一自动执行"},
    ],
    "step_counts": dict(counts),
    "failed_steps": failed,
    "slowest_steps": slowest,
    "steps": steps,
}

out = os.environ["PIPELINE_STATUS_FILE"]
os.makedirs(os.path.dirname(out), exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".pipeline_status_", suffix=".json", dir=os.path.dirname(out))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, out)
PY
}

run_step() {
    local label="$1"
    local script="$2"
    local started_at
    local ended_at
    local started_epoch
    local duration_seconds
    local status="OK"
    local err_log
    local attempt
    err_log=$(mktemp -t pipeline_step.XXXXXX)
    started_at=$(date '+%Y-%m-%d %H:%M:%S')
    started_epoch=$(date +%s)
    echo ""
    # D'' 锁感知 retry：最多 3 次。非锁错误立刻 FAIL 不重试；锁冲突 sleep 30s 后再试。
    # 2026-05-25 事故根因：DuckDB 写锁瞬时冲突让 0c 秒级 FAIL，下游全用旧 universe。
    for attempt in 1 2 3; do
        if [ "$attempt" -gt 1 ]; then
            echo "[$label] $script (attempt $attempt/3 · 锁冲突 retry) ..."
        else
            echo "[$label] $script ..."
        fi
        # script 以 '-m' 开头 → 当成 python -m module 调用，不传相对脚本名
        # tee 把 stderr 同时落到 err_log（用于判定是否锁冲突）和原 stderr（用户看得到）
        if [[ "$script" == -m* ]]; then
            if $PYTHON $script 2> >(tee "$err_log" >&2); then
                [ "$attempt" -gt 1 ] && status="OK_RETRY" || status="OK"
                break
            fi
        else
            # 不能引号化 $script：当 script 形如 "path/x.py --dry-run" 需要 shell 拆词
            if $PYTHON $script 2> >(tee "$err_log" >&2); then
                [ "$attempt" -gt 1 ] && status="OK_RETRY" || status="OK"
                break
            fi
        fi
        # 非锁冲突立刻 FAIL，避免对真正的错误浪费重试时间
        if ! grep -qiE "Conflicting lock|Could not set lock|database is locked" "$err_log"; then
            echo "❌ [$label] $script 失败（非锁冲突，不重试）"
            FAILED_STEPS+=("$label/$script")
            notify "📉 股票看板刷新失败" "$label: $script"
            status="FAIL"
            break
        fi
        if [ "$attempt" -lt 3 ]; then
            echo "🔒 [$label] DuckDB 锁冲突，sleep 30s 后重试..."
            sleep 30
        else
            echo "❌ [$label] $script 锁冲突重试 2 次后仍失败"
            FAILED_STEPS+=("$label/$script")
            notify "📉 股票看板刷新失败（锁持续冲突）" "$label: $script"
            status="FAIL"
        fi
    done
    rm -f "$err_log"
    ended_at=$(date '+%Y-%m-%d %H:%M:%S')
    duration_seconds=$(($(date +%s) - started_epoch))
    record_pipeline_step "$label" "$script" "$status" "$started_at" "$ended_at" "$duration_seconds"
    write_pipeline_status "RUNNING" ""
}

echo ""
echo "================================================"
echo "  ⏰ $TIMESTAMP — 每日刷新开始（mode=$MODE, a_share_ready=$A_SHARE_READY, a_share_mode=$A_SHARE_PRODUCTION_MODE）"
echo "================================================"
write_pipeline_status "RUNNING" ""

# ── A 股闭环步骤封装：单独定义以便两种模式复用 ──
# Step 21b (写飞书 A 股优选) 已废 (2026-05-11 PM 第二轮): 飞书 Bitable 100% 退役
# 2026-05-12: step 22 (apply_a_share_constraints) 拆出独立跑 — 它处理的是美股 plan_v5
#   不是 A 股 picks，不该被 A 股收盘时间锁；之前合在这里导致美股 plan_constrained 卡 33 小时
run_a_share_steps() {
    if [ "$MODE" = "skip_a_share" ]; then
        echo ""
        echo "[A 股闭环] 跳过 — --skip-a-share 模式"
        local ts
        ts=$(date '+%Y-%m-%d %H:%M:%S')
        record_pipeline_step "21/25 A 股优选" "--skip-a-share" "SKIP" "$ts" "$ts" "0"
        write_pipeline_status "RUNNING" ""
        return
    fi
    if [ "$A_SHARE_PRODUCTION_MODE" = "off" ]; then
        echo ""
        echo "[A 股闭环] 跳过 — A_SHARE_PRODUCTION_MODE=off"
        local ts
        ts=$(date '+%Y-%m-%d %H:%M:%S')
        record_pipeline_step "21/25 A 股优选" "A_SHARE_PRODUCTION_MODE=off" "SKIP" "$ts" "$ts" "0"
        write_pipeline_status "RUNNING" ""
        return
    fi
    if [ "$A_SHARE_READY" = "0" ]; then
        echo ""
        echo "[21/25 A 股优选] 跳过 — 当前 ${HOUR}:00 非 A 股收盘后时段（要求 ≥16:00 工作日 或 周末）"
        echo "  原因：北向资金 T+1、龙虎榜盘后才发布，盘前/盘中跑会用 T-1 数据污染选股"
        echo "  收盘后请单独跑：./daily_refresh.sh --a-share-only"
        local ts
        ts=$(date '+%Y-%m-%d %H:%M:%S')
        record_pipeline_step "21/25 A 股优选" "收盘后才运行：./daily_refresh.sh --a-share-only" "SKIP" "$ts" "$ts" "0"
        write_pipeline_status "RUNNING" ""
        return
    fi
    run_step "20c/25 A 股权重校准（price-only IC）" \
        "-m stock_research.jobs.calibrate_a_share_factor_weights --universe ${A_SHARE_CALIBRATION_UNIVERSE:-static} --limit ${A_SHARE_CALIBRATION_LIMIT:-42}"
    A_SHARE_ENABLED_NOW=$($PYTHON -c "from stock_research import config; print('1' if config.A_SHARE_PRODUCTION_ENABLED else '0')" 2>/dev/null || echo "0")
    # require-after-close：python 层再做一次防御，万一 cron 配错也不会跑出脏数据
    if [ "$A_SHARE_ENABLED_NOW" = "1" ]; then
        run_step "21/25 A 股优选（校准权重，写 DuckDB）" "-m stock_research.jobs.a_share_picks --require-after-close --universe ${A_SHARE_UNIVERSE:-auto} --universe-limit ${A_SHARE_UNIVERSE_LIMIT:-80} --workers ${A_SHARE_WORKERS:-1}"
    else
        run_step "21/25 A 股优选（研究模式，不写 DuckDB）" "-m stock_research.jobs.a_share_picks --require-after-close --dry-run --universe ${A_SHARE_UNIVERSE:-auto} --universe-limit ${A_SHARE_UNIVERSE_LIMIT:-80} --workers ${A_SHARE_WORKERS:-1}"
    fi
}

# ── --a-share-only 模式：只跑 A 股闭环 + DuckDB 同步 + 重建 HTML，跳过其他 ──
if [ "$MODE" = "a_share_only" ]; then
    if [ "$A_SHARE_READY" = "0" ]; then
        echo "❌ --a-share-only 但当前非收盘后时段 ($(date +%H):%M)，退出（避免脏数据）"
        ts=$(date '+%Y-%m-%d %H:%M:%S')
        record_pipeline_step "A 股闭环启动检查" "./daily_refresh.sh --a-share-only" "FAIL" "$ts" "$ts" "0"
        write_pipeline_status "FAIL" "$ts"
        exit 1
    fi
    run_step "0b/25 汇率刷新（单一 FX 源）" "scripts/tools/refresh_fx_rates.py"
    run_a_share_steps
    # a_share_picks 跑完后重跑约束器 — A 股真实持仓约束可能变化，需要刷新美股 plan_constrained
    run_step "10b/25 plan_a 后处理（美股仓位约束）" "-m stock_research.jobs.apply_a_share_constraints"
    run_step "24/25 DuckDB pipeline 同步" "scripts/migrate/migrate_pipeline_to_duckdb.py"
    # 24b: V2 产业链分类（rule_classify + manual_override → chain_metadata 表 → dashboard 链条 pill）
    run_step "24b/25 V2 产业链分类入库" "scripts/tools/classify_chain_v2.py"
    # 注：F-Score 计算已挪到 step 1c（必须在 build_v2_recommendations 之前，让 picks 当日带 f_score）
    run_step "25/25 重建 HTML" "scripts/pipeline/build_stock_dashboard_html.py"
    run_step "26 早安简报（今日决策台的飞书镜像 · 本地生成，验收后再推送）" "-m stock_research.jobs.morning_brief --no-push"
    A_SHARE_ENABLED_NOW=$($PYTHON -c "from stock_research import config; print('1' if config.A_SHARE_PRODUCTION_ENABLED else '0')" 2>/dev/null || echo "0")
    if [ "$A_SHARE_ENABLED_NOW" = "1" ]; then
        run_step "27 生产闭环验收" "scripts/tools/production_acceptance_check.py"
    else
        run_step "27 生产闭环验收" "scripts/tools/production_acceptance_check.py --allow-a-share-disabled"
    fi
    DONE_TS=$(date '+%Y-%m-%d %H:%M:%S')
    echo ""
    if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
        echo "✅ A 股闭环完成 — $DONE_TS"
        notify "✅ A 股闭环完成" "$DONE_TS"
    else
        echo "⚠️  A 股闭环有失败 — $DONE_TS"
        for s in "${FAILED_STEPS[@]}"; do echo "     - $s"; done
    fi
    if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
        write_pipeline_status "OK" "$DONE_TS"
        echo ""
        echo "[28 早安简报推送] pipeline_status=OK 后重新生成并推送..."
        if ! $PYTHON -m stock_research.jobs.morning_brief; then
            echo "⚠️  早安简报最终推送失败（pipeline 已 OK，本地数据不回滚）"
        fi
    else
        write_pipeline_status "FAIL" "$DONE_TS"
    fi
    [ ${#FAILED_STEPS[@]} -eq 0 ]
    exit $?
fi

# ────── M = morning 必跑（今日决策路径）；R = research 单独跑（慢任务夜班）──────
# M — V1 表 DROP 守卫（V2 cutover 常态化：防止 legacy CREATE TABLE 把 V1 表偷偷带回）
is_morning_step && run_step "0a/25 V1 表 DROP 守卫（V2 schema 完整性）" \
    "scripts/tools/drop_v1_tables_v2.py"
# M
is_morning_step && run_step "0b/25 汇率刷新（单一 FX 源）" "scripts/tools/refresh_fx_rates.py"
# M
run_step "0c/25 V2 系统池刷新（live universe → system_universe/pool_membership）" \
    "scripts/tools/refresh_system_universe_v2.py"
run_step "1/25 抓价格（手动 watchlist + 科技/AI universe）" "scripts/pipeline/fetch_stock_prices.py --source both"
# M — V2 Piotroski P5-Lite（必须早于 build_v2_recommendations，让 picks 当日带上 f_score）
# yfinance 99 只财报 ~ 4 分钟；A 股暂未实现（akshare 财报接口结构差异较大）
run_step "1c/25 V2 F-Score 计算（P5-Lite · 美/港股 → factor_metadata）" \
    "scripts/tools/compute_piotroski_v2.py --markets US,HK"
# M — V2 推荐 run（必须在 step 10/23 之前跑，让两者拿到今日 picks 而不是昨日）
run_step "1b/25 V2 推荐 run（system_universe → recommendation_picks/portfolio_plans）" \
    "scripts/tools/build_v2_recommendations.py"
# R — SEC 13F 刷新（拉 10+ 大基金季度持仓变动，慢）
is_research_step && run_step "2/25 SEC 13F 刷新" "-m stock_research.jobs.refresh_13f"
is_research_step && run_step "3/25 SEC 13F → track_13f.json（dashboard 用）" "scripts/pipeline/_build_track_13f_from_sec.py"
# M — V2 系统池 enrichment（system_universe → industry/earnings/详情页字段）
run_step "4b/25 V2 系统池 enrichment" \
    "scripts/tools/enrich_system_universe_v2.py --reuse-recent-days 7 --skip-trends --skip-akshare --sleep-sec 0.02 --per-symbol-timeout-sec 18"
# 2026-05-20 V1 cutover：删 step 4 (V1 enrich_watchlist) / 5 (V1 daily_audit) /
# 6 (V1 daily_picks dry-run) / 7 (audit_picks V1 reviews) / 8 (weekly_review V1 picks)

# R — 每日新闻同步飞书（财联社 100 条 → 国际/国内分类）
is_research_step && run_step "8b/25 每日新闻同步飞书" "scripts/daily_news_to_feishu.py"

# M — v6 学术因子流水线（watchlist 空时静默退出，无慢操作）
run_step "9/25 v6 学术因子选股（已落 DuckDB picks）" "scripts/pipeline/daily_picks_v5.py"
run_step "9b/25 港股 picks（3 因子 + DuckDB picks 表，south_flow standby）" "scripts/pipeline/hk_picks.py"
run_step "10/25 risk-aware 仓位优化（方案 A v6）" "-m stock_research.jobs.optimize_portfolio"
# step 10b 处理美股 plan_constrained（真实持仓约束 → 美股 plan），早班必须跑
run_step "10b/25 plan_a 后处理（美股仓位约束）" "-m stock_research.jobs.apply_a_share_constraints"
run_step "10c/25 推荐质量闸门（调仓前）" "scripts/tools/recommendation_quality_gate.py"
run_step "11/25 调整清单（卖/买/调）→ trade_delta.json" "scripts/pipeline/trade_delta.py"

# M — 专业分析数据（风险指标 morning 必跑；history 预拉是 research）
run_step "13/25 风险指标 (VaR/Sharpe/Calmar)" "scripts/pipeline/risk_metrics.py"
# 2026-05-20 删 step 14 (optimize_portfolio_legacy V1 路径)
is_research_step && run_step "15/25 历史数据预拉（dashboard 历史 tab 用）" "scripts/pipeline/_fetch_history_for_dashboard.py"

# M — 实盘防御（VIX + 200MA + 单股 -15% 止损）
run_step "16/25 实盘防御检查" "-m stock_research.jobs.realtime_defense"

# R — OpenBB 宏观 + 行业轮动（quick 但仍要 1-2 分钟）
is_research_step && run_step "17/25 OpenBB 综合情报" "-m stock_research.jobs.openbb_intelligence --quick"

# R — A 股事件层（IPO / 解禁 / 政策；19 比较慢）
needs_ipo_data && run_step "18/25 IPO 打新日历" "-m stock_research.jobs.ipo_daily"
is_research_step && run_step "19/25 事件日历（解禁/减持/财报）" "-m stock_research.jobs.event_calendar_daily"
is_research_step && run_step "19a/25 港股事件日历（yfinance 财报+超预期）" "-m stock_research.jobs.event_calendar_hk_daily"
needs_ipo_data && run_step "19b/25 次新股+解禁雷达（IPO & 次新股 tab 数据源）" "-m stock_research.jobs.junior_stock_watcher"
is_research_step && run_step "20/25 产业政策事件扫描" "-m stock_research.jobs.policy_scan_daily"

# v9.0 A 股选股闭环（仅 morning + full 兜底；A 股 picks 主路径走 --a-share-only 16:30 单独跑）
# - 早班 08:30 (morning) 跑会被 A_SHARE_READY=0 自动跳过
# - 21:00 research 路径不进 A 股 picks（避免和 16:30 a-share line 重复）
is_morning_step && run_a_share_steps

# M — AI 推荐（dashboard 全池排名 + 质量闸门复核 + 证据报告）
run_step "23/25 全池 AI 推荐（每日）" "scripts/tools/build_pool_recommendations.py"

# benchmark close 灌入 price_daily（SPY/^HSI/000300.SS）— evaluate_v2_picks 算 alpha 的本地数据源
run_step "23a-pre/25 基准指数行情灌入" "scripts/pipeline/ingest_benchmark_prices.py"

# M — V2 pick alpha 评估（扫过去 70 天 recommendation_runs，每只 pick 算 1d/5d/20d
# alpha 写 pick_outcomes；已成熟样本不重算，幂等；带网络 yfinance benchmark 但有内存缓存）
run_step "23a/25 V2 pick alpha 评估" "scripts/tools/evaluate_v2_picks.py"
run_step "23a2/25 V2 策略验证汇总" "scripts/tools/build_strategy_validation_v2.py"

# R — 旧 discovery 准确度评估（V1 discovery_tracking 路径，clean v2 上无新数据）
# 2026-05-20 删 step 23b (evaluate_discovery V1 discovery_tracking)，已由 evaluate_v2_picks 取代
# M
run_step "23c/25 推荐质量闸门（收盘后复核）" "scripts/tools/recommendation_quality_gate.py"
run_step "23d/25 推荐有效性证据报告" "scripts/tools/recommendation_evidence_report.py"
# 2026-05-21 V2 cutover 补洞：替代被删的 V1 audit_picks，喂 dashboard「买前审查」tab
run_step "23e/25 picks 反向审查（V2 · Risk Parity + 估值 + Markowitz）" "-m stock_research.jobs.audit_picks_v2 --fast"
run_step "23f/25 真实持仓每日体检（评分/建议/说明）" "-m stock_research.jobs.real_holding_review"

# M — DuckDB pipeline 同步 + HTML 重建 + brief + 验收
# （这几步在 morning 必跑，research mode 不重做避免覆盖 morning 已落地的 dashboard）
is_morning_step && run_step "24/25 DuckDB pipeline 同步" "scripts/migrate/migrate_pipeline_to_duckdb.py"
is_morning_step && run_step "24b/25 V2 产业链分类入库" "scripts/tools/classify_chain_v2.py"
# 注：F-Score 计算已挪到 step 1c（必须在 build_v2_recommendations 之前，让 picks 当日带 f_score）
is_morning_step && run_step "25/25 重建 HTML" "scripts/pipeline/build_stock_dashboard_html.py"

# R — 周一专属 walk-forward OOS 校验（每周一夜班 21:00 跑；morning 不跑）
if is_research_step && [ "$DOW" = "1" ]; then
    WF_START=$($PYTHON -c "from datetime import date; d=date.today(); y=d.year-1; print(f'{y}-{d.month:02d}')")
    WF_END=$(date '+%Y-%m')
    run_step "25b/25 walk-forward OOS 校验（每周一）" \
        "-m stock_research.jobs.walk_forward_backtest --start $WF_START --end $WF_END --top-k 5"
elif is_research_step; then
    echo ""
    echo "[25b/25 walk-forward OOS] 跳过 — 仅周一执行（今天 weekday=$DOW，1=Mon）"
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    record_pipeline_step "25b/25 walk-forward OOS 校验（每周一）" "仅周一执行" "SKIP" "$ts" "$ts" "0"
    write_pipeline_status "RUNNING" ""
fi

# M — 早安简报 + 生产验收
is_morning_step && run_step "26 早安简报（今日决策台的飞书镜像 · 本地生成，验收后再推送）" "-m stock_research.jobs.morning_brief --no-push"
A_SHARE_ENABLED_NOW=$($PYTHON -c "from stock_research import config; print('1' if config.A_SHARE_PRODUCTION_ENABLED else '0')" 2>/dev/null || echo "0")
if is_morning_step; then
    if [ "$A_SHARE_ENABLED_NOW" = "1" ]; then
        run_step "27 生产闭环验收" "scripts/tools/production_acceptance_check.py"
    else
        run_step "27 生产闭环验收" "scripts/tools/production_acceptance_check.py --allow-a-share-disabled"
    fi
    # 2026-05-21 DB 出仓后，用户状态由 state_backup/*.json 持久化（DuckDB 是衍生物）
    run_step "28 用户状态备份 → state_backup/state_YYYY-MM-DD.json" "scripts/tools/backup_state_to_json.py"
fi

# 周日：周末复盘（模型推 vs 你做）· 飞书需配置 FEISHU_WEEKLY_WEBHOOK 或 FEISHU_BRIEF_WEBHOOK
if [ "$DOW" = "7" ]; then
    run_step "29 周末复盘（动作 vs 信号）" "-m stock_research.jobs.weekly_self_review --push"
else
    echo ""
    echo "[29 周末复盘] 跳过 — 仅周日执行（今天 weekday=$DOW，7=Sun）"
fi

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
echo "  📋 产品主入口：dashboard 默认首页「今日决策台」（HTML 见下）"
echo "  📨 飞书镜像：$DIR/morning_brief.md（每天 08:30 推群）"
echo "  HTML：$DIR/stock_dashboard.html"
echo "  DuckDB（数据落地）：${STOCK_DB_PATH:-$DIR/stock_history_v2.duckdb}"
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    write_pipeline_status "OK" "$DONE_TS"
    if is_morning_step; then
        echo ""
        echo "[28 早安简报推送] pipeline_status=OK 后重新生成并推送..."
        if ! $PYTHON -m stock_research.jobs.morning_brief; then
            echo "⚠️  早安简报最终推送失败（pipeline 已 OK，本地数据不回滚）"
        fi
    fi
else
    write_pipeline_status "FAIL" "$DONE_TS"
fi

# log 文件 > 5MB 时滚动一次（保留 .1 备份）
LOG="$DIR/daily_refresh.log"
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG")" -gt 5242880 ]; then
    mv "$LOG" "$LOG.1"
fi

# 任何步骤失败 → 退出码 1（cron / 监控可识别）
[ ${#FAILED_STEPS[@]} -eq 0 ]
