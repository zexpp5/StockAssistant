"""单一汇率源 —— 全仓库唯一可信汇率常量。

2026-05-22 收敛：此前 5 处硬编码（risk_metrics / trade_delta / backtest_plan_a /
dashboard JS x2），HKD 在不同位置写成 0.91 或 0.92，同一只 0700.HK 不同 tab
RMB 数能差 ~1%。本模块作为单一来源，前端通过 GET /api/fx-rates 拉同一份。

未来升级路径：把 FX_TO_RMB 改成动态值（akshare currency_boc_sina），保持
get_fx_to_rmb() 接口不变，所有调用方零改动。当前先做静态常量收敛。

接口契约：
  FX_TO_RMB[ccy] -> float           # 直接查表（已知 ccy）
  get_fx_to_rmb(ccy) -> float       # 安全获取（未知 ccy 返回 1.0）
  AS_OF -> str                       # 常量更新日（YYYY-MM-DD），前端可展示
"""
from __future__ import annotations

# 本币 → RMB 汇率。
#   USD 7.10 ─ 2026-05 USD/CNY 区间中值
#   HKD 0.917 ─ 此前 0.91/0.92 两套并存,取中值统一
#   其余按公开市价取整位
FX_TO_RMB: dict[str, float] = {
    "CNY": 1.0,
    "USD": 7.10,
    "HKD": 0.917,
    "JPY": 0.046,
    "KRW": 0.0052,
    "AUD": 4.60,
    "GBP": 9.00,
}

AS_OF: str = "2026-05-22"


def get_fx_to_rmb(ccy: str | None) -> float:
    """安全查询本币→RMB 汇率。未知币种返回 1.0（按 CNY 处理）。"""
    if not ccy:
        return 1.0
    return FX_TO_RMB.get(ccy.upper(), 1.0)


def infer_currency_from_ticker(ticker: str | None) -> str:
    """按 ticker 后缀推断本币 —— 与 stock_db._infer_currency_from_ticker 保持一致。

    裸 ticker 默认 USD（与前端 _currencyForTicker 同规则）。
    """
    if not ticker:
        return "USD"
    s = ticker.upper().strip()
    if s.endswith((".SS", ".SZ", ".BJ", ".SH")):
        return "CNY"
    if s.endswith(".HK"):
        return "HKD"
    if s.endswith(".T"):
        return "JPY"
    if s.endswith(".KS"):
        return "KRW"
    if s.endswith(".AX"):
        return "AUD"
    if s.endswith((".L", ".IL")):
        return "GBP"
    return "USD"
