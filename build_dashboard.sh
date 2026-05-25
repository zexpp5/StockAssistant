#!/usr/bin/env bash
# 单次重建 stock_dashboard.html — 强制走 daily_refresh 同款解释器（brew python3）。
# 必须存在：否则 `python build_stock_dashboard_html.py` 会被 anaconda 默认解释器
# 接走，缺 duckdb 模块就生成 degraded HTML（事故 2026-05-23）。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /opt/homebrew/bin/python3 "$REPO_ROOT/scripts/pipeline/build_stock_dashboard_html.py" "$@"
