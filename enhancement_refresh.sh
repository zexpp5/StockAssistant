#!/bin/bash
# enhancement_refresh.sh — 早班异步增强子进程（2026-06-08 流水线改造）
#
# 设计见 docs/2026-06-08_早班快线_异步增强_流水线改造方案.md。
# 角色：早班核心快线在 28 push 之后 detached fork 本脚本（nohup ... &），核心不等它。
#   本脚本慢慢抓 SEC/Form4/HKEX/主题证据/F-Score 全量/策略诊断/次新等重网络增强，
#   抓完写回 JSON/DuckDB + 写 enhancement_status.json（各块新鲜度）+ 轻量重建 dashboard。
#   绝不碰 picks/plan/brief（早班已定稿已推飞书）。
#
# 三条保险（与主线解耦）：
#   ① 独立 .enhancement_refresh.pid（不靠主线 PID）；启动 reconcile 清死锁。
#   ② 每步硬超时(estep)，超时/失败记 DEGRADED 继续；总看门狗 45min 防再挂 11 小时。
#   ③ 跑完只更新增强状态 + 重建 dashboard，不重发简报、不改已发结论。

DIR="${DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$DIR" || exit 1

if [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON="/opt/homebrew/bin/python3"   # 不是 anaconda：akshare/yfinance 装在这
else
    PYTHON="python3"
fi

# 自动加载 .env（飞书凭证 / Finnhub key 等）
if [ -f "$DIR/.env" ]; then
    set -a; source "$DIR/.env" 2>/dev/null; set +a
fi

DOW=$(date +%u)
STATUS_DIR="$DIR/data/latest"
mkdir -p "$STATUS_DIR"
ENH_STATUS="$STATUS_DIR/enhancement_status.json"
ENH_STEPS=$(mktemp -t enh_steps.XXXXXX)   # JSONL：每步一行 {section,label,status,...}
TS_START=$(date '+%Y-%m-%d %H:%M:%S')

notify() { osascript -e "display notification \"$2\" with title \"$1\"" 2>/dev/null; }

# --- 独立 PID 锁 ---
PID_LOCK="$DIR/.enhancement_refresh.pid"
if [ -f "$PID_LOCK" ]; then
    OLD_PID=$(cat "$PID_LOCK" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "❌ 另一个 enhancement_refresh 还在跑（PID $OLD_PID），本轮退出。"
        exit 1
    else
        echo "⚠️  发现旧增强 PID ($OLD_PID 已死)，清理后继续。"
        rm -f "$PID_LOCK"
    fi
fi
echo $$ > "$PID_LOCK"

# --- 总看门狗 45min（防再挂死霸占；增强正常 ≤ ~40min）---
MAX_RUNTIME_SECONDS="${ENHANCEMENT_MAX_SECONDS:-2700}"
MAIN_PID=$$
(
    sleep "$MAX_RUNTIME_SECONDS"
    if kill -0 "$MAIN_PID" 2>/dev/null; then
        echo "⏱️  enhancement_refresh 超过 ${MAX_RUNTIME_SECONDS}s — 总看门狗强杀，释放锁。"
        pkill -TERM -P "$MAIN_PID" 2>/dev/null; kill -TERM "$MAIN_PID" 2>/dev/null
        sleep 10
        pkill -KILL -P "$MAIN_PID" 2>/dev/null; kill -KILL "$MAIN_PID" 2>/dev/null
    fi
) &
WATCHDOG_PID=$!
disown "$WATCHDOG_PID" 2>/dev/null

# 写 enhancement_status.json（聚合 ENH_STEPS）
write_enh_status() {
    local overall="$1"
    ENH_STATUS="$ENH_STATUS" ENH_STEPS="$ENH_STEPS" ENH_OVERALL="$overall" \
    ENH_STARTED="$TS_START" ENH_PID="$$" \
    "$PYTHON" - <<'PY' 2>/dev/null || true
import json, os, datetime, tempfile
steps = []
p = os.environ["ENH_STEPS"]
if os.path.exists(p):
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line:
            steps.append(json.loads(line))
sections = {}
for s in steps:
    sections[s["section"]] = {
        "label": s.get("label"),
        "status": s.get("status"),                # fresh | degraded
        "last_refresh": s.get("ended_at"),
        "duration_seconds": s.get("duration_seconds"),
    }
payload = {
    "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
    "started_at": os.environ.get("ENH_STARTED"),
    "main_pid": int(os.environ.get("ENH_PID") or 0),
    "status": os.environ.get("ENH_OVERALL"),      # RUNNING | OK | DEGRADED | INTERRUPTED
    "sections": sections,
    "degraded_sections": [k for k, v in sections.items() if v.get("status") == "degraded"],
    "steps": steps,
}
out = os.environ["ENH_STATUS"]
fd, tmp = tempfile.mkstemp(prefix=".enh_status_", suffix=".json", dir=os.path.dirname(out))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
os.replace(tmp, out)
PY
}

# on_exit：清锁 + 收看门狗 + 若仍 RUNNING 标 INTERRUPTED（R5 同款自愈）
ENH_TERMINAL=0
on_exit() {
    rm -f "$PID_LOCK"
    if [ -n "${WATCHDOG_PID:-}" ]; then pkill -P "$WATCHDOG_PID" 2>/dev/null; kill "$WATCHDOG_PID" 2>/dev/null; fi
    if [ "${ENH_TERMINAL:-0}" != "1" ]; then write_enh_status "INTERRUPTED"; fi
    rm -f "$ENH_STEPS"
}
trap on_exit EXIT

# 启动 reconcile：上一轮死掉但 enhancement_status 仍 RUNNING → 标 INTERRUPTED
if [ -f "$ENH_STATUS" ]; then
    ENH_STATUS="$ENH_STATUS" SELF_PID="$$" "$PYTHON" - <<'PY' 2>/dev/null || true
import json, os
f = os.environ["ENH_STATUS"]
try:
    d = json.load(open(f, encoding="utf-8"))
except Exception:
    raise SystemExit(0)
if d.get("status") != "RUNNING":
    raise SystemExit(0)
pid = d.get("main_pid") or 0
self_pid = int(os.environ.get("SELF_PID") or 0)
if pid and pid != self_pid:
    try:
        os.kill(int(pid), 0); raise SystemExit(0)   # 还活着
    except ProcessLookupError:
        pass
    except PermissionError:
        raise SystemExit(0)
d["status"] = "INTERRUPTED"
import datetime
d["note"] = "reconcile@启动：上一轮增强异常终止未写终态"
json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
fi

# --- estep：带超时 + DEGRADED + 锁感知 retry 的增强步执行器 ---
#   estep <section> <label> <script> [timeout_s]
#   失败/超时一律 DEGRADED（增强步永不阻断），记 ENH_STEPS。
DEGRADED_COUNT=0
estep() {
    local section="$1"; local label="$2"; local script="$3"; local timeout_s="${4:-300}"
    local started ended started_epoch dur err_log to_marker attempt cmd_pid wd_pid rc st
    err_log=$(mktemp -t enh_err.XXXXXX); to_marker=$(mktemp -t enh_to.XXXXXX)
    started=$(date '+%Y-%m-%d %H:%M:%S'); started_epoch=$(date +%s)
    echo ""; echo "[增强·$section] $label — $script ... (上限 ${timeout_s}s)"
    st="fresh"
    for attempt in 1 2 3; do
        : > "$to_marker"
        $PYTHON $script 2> >(tee "$err_log" >&2) &
        cmd_pid=$!; wd_pid=""
        (
            sleep "$timeout_s"
            if kill -0 "$cmd_pid" 2>/dev/null; then
                echo "TIMEOUT" > "$to_marker"
                echo "⏱️  [增强·$section] 超过 ${timeout_s}s — 终止该步" >&2
                pkill -TERM -P "$cmd_pid" 2>/dev/null; kill -TERM "$cmd_pid" 2>/dev/null
                sleep 5
                pkill -KILL -P "$cmd_pid" 2>/dev/null; kill -KILL "$cmd_pid" 2>/dev/null
            fi
        ) &
        wd_pid=$!; disown "$wd_pid" 2>/dev/null
        wait "$cmd_pid" 2>/dev/null; rc=$?
        pkill -P "$wd_pid" 2>/dev/null; kill "$wd_pid" 2>/dev/null
        if [ "$rc" -eq 0 ]; then st="fresh"; break; fi
        if [ -s "$to_marker" ]; then
            echo "⚠️  [增强·$section] 超时 → DEGRADED"; st="degraded"; break
        fi
        if grep -qiE "Conflicting lock|Could not set lock|database is locked" "$err_log" && [ "$attempt" -lt 3 ]; then
            echo "🔒 [增强·$section] DuckDB 锁冲突，sleep 30s 重试..."; sleep 30; continue
        fi
        echo "⚠️  [增强·$section] 失败 → DEGRADED（增强步不阻断）"; st="degraded"; break
    done
    rm -f "$err_log" "$to_marker"
    ended=$(date '+%Y-%m-%d %H:%M:%S'); dur=$(($(date +%s) - started_epoch))
    [ "$st" = "degraded" ] && DEGRADED_COUNT=$((DEGRADED_COUNT+1))
    ENH_SEC="$section" ENH_LABEL="$label" ENH_ST="$st" ENH_STARTED2="$started" ENH_ENDED="$ended" ENH_DUR="$dur" ENH_STEPS="$ENH_STEPS" \
    "$PYTHON" - <<'PY' 2>/dev/null || true
import json, os
row = {"section": os.environ["ENH_SEC"], "label": os.environ["ENH_LABEL"], "status": os.environ["ENH_ST"],
       "started_at": os.environ["ENH_STARTED2"], "ended_at": os.environ["ENH_ENDED"],
       "duration_seconds": int(os.environ.get("ENH_DUR") or 0)}
open(os.environ["ENH_STEPS"], "a", encoding="utf-8").write(json.dumps(row, ensure_ascii=False) + "\n")
PY
    write_enh_status "RUNNING"
}

echo "================================================"
echo "  🔧 $TS_START — 早班异步增强开始（PID $$, DOW=$DOW）"
echo "================================================"
write_enh_status "RUNNING"

# === 重网络增强步（全部 DEGRADE 级，永不阻断已发出的早班简报）===

# F-Score 全量重算（早班只补缺，这里全刷新季报 → 刷新 computed_at 新鲜度）
estep "f_score" "F-Score 全量重算（季报）" "scripts/tools/compute_piotroski_v2.py --markets US,HK,CN" 600

# 事件/披露增强
estep "ipo"        "IPO 打新日历"                       "-m stock_research.jobs.ipo_daily" 180
estep "hkex"       "港股 HKEX 披露易公告"               "-m stock_research.jobs.event_calendar_hk_hkex_daily" 240
estep "sec_edgar"  "美股 SEC EDGAR（8-K/13G/13D/14A）"   "-m stock_research.jobs.event_calendar_us_sec_daily" 240
estep "form4"      "美股 SEC Form 4 内部人交易"          "-m stock_research.jobs.event_calendar_us_form4_daily" 300
estep "junior"     "次新股+解禁雷达"                     "-m stock_research.jobs.junior_stock_watcher" 600

# AI 主题雷达证据（周一全量 ETF+SEC 扫描，平日轻量）
if [ "$DOW" = "1" ]; then
    estep "ai_theme" "AI 主题雷达证据（周一全量 ETF+SEC）" "-m stock_research.jobs.ai_theme_evidence_refresh --refresh-etf --scan-sec" 600
else
    estep "ai_theme" "AI 主题雷达证据（每日轻量）"         "-m stock_research.jobs.ai_theme_evidence_refresh" 240
fi

# 策略诊断/调权/shadow（只读研究，不影响今日结论）
estep "strategy_eval"  "V2 pick alpha 评估"        "scripts/tools/evaluate_v2_picks.py" 300
estep "strategy_eval"  "V2 策略验证汇总"           "scripts/tools/build_strategy_validation_v2.py" 180
estep "strategy_diag"  "策略失败诊断"              "scripts/tools/strategy_failure_diagnosis.py --markets all --horizon 1d" 180
estep "strategy_diag"  "策略调权建议"              "scripts/tools/strategy_tuning_proposal.py --horizon 1d" 180
estep "strategy_diag"  "shadow 调权模拟"           "scripts/tools/build_shadow_tuning_run.py --horizon 1d" 180
estep "strategy_diag"  "shadow 生产门禁"           "scripts/tools/evaluate_shadow_tuning_run.py" 120
estep "strategy_diag"  "US shadow 预检"            "scripts/tools/us_shadow_preflight_check.py" 120
estep "strategy_diag"  "US-only 生产验收"          "scripts/tools/us_production_acceptance_check.py" 120
estep "strategy_diag"  "推荐规则快速体检"          "scripts/tools/recommendation_readiness_check.py" 120
estep "strategy_diag"  "US 严筛试运行"             "scripts/tools/us_strict_trial.py" 180
estep "early_radar"    "早发现雷达"                "-m stock_research.jobs.early_growth_radar" 240

# 增强数据落地后，轻量重建 dashboard 那几块（不重发简报）
estep "dashboard" "重建 HTML（增强块刷新）" "scripts/pipeline/build_stock_dashboard_html.py" 180

TS_DONE=$(date '+%Y-%m-%d %H:%M:%S')
if [ "$DEGRADED_COUNT" -eq 0 ]; then
    echo ""; echo "✅ 早班异步增强完成 — $TS_DONE（全部 fresh）"
    write_enh_status "OK"
else
    echo ""; echo "⚠️  早班异步增强完成 — $TS_DONE（$DEGRADED_COUNT 块 degraded，dashboard 已标新鲜度）"
    write_enh_status "DEGRADED"
fi
ENH_TERMINAL=1
