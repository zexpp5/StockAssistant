"""A 股交易状态过滤器：ST/退市/停牌/涨跌停 + A 股版流动性约束。

为什么要做：
  美股因子模型直接套到 A 股会出灾难。Markowitz 算出 12% 仓位，但实际：
    - 股票当日涨停 → 买不进去（成交 = 0）
    - 股票被 ST → 涨跌停限制变成 ±5%，风险特征完全不同
    - 股票停牌 → 持有期间无法出场
    - 单日下大单 → A 股 T+1 + 分时撮合，冲击成本远高于美股

本模块：
  1. 用一次 spot_em 调用拿全 A 股快照，本地分类 ST / 涨跌停 / 停牌 / 流通量
  2. 提供过滤器：filter_tradable(codes) → (可买列表, {代码: 拦截原因})
  3. 提供 A 股版流动性约束：cap_by_volume_cn（替代美股 ADV）

设计原则：
  - 所有数据来自一次性快照（避免 N 次 API call 触发限流）
  - 快照可缓存（对 watchlist 5 分钟刷新一次足够）
  - 纯函数，无 I/O 副作用
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ───────────── 数据结构 ─────────────

@dataclass
class StockStatus:
    """单只 A 股的当日交易状态。"""
    code: str
    name: str = ""
    price: float | None = None
    change_pct: float | None = None         # 涨跌幅（百分比，10 = 10%）
    volume: float | None = None             # 当日成交量（手）
    amount: float | None = None             # 当日成交额（元）
    turnover_rate: float | None = None      # 换手率（%）
    market_cap: float | None = None         # 总市值（元）
    circulating_cap: float | None = None    # 流通市值（元）

    # 派生状态
    is_st: bool = False                     # ST / *ST / S*ST
    is_delisting: bool = False              # 退市整理期
    is_suspended: bool = False              # 停牌
    is_new: bool = False                    # 新股（首日 44% 限制）
    limit_up: bool = False                  # 已封死涨停（>= limit-0.05pp，盘口几乎无筹码）
    limit_down: bool = False                # 已封死跌停
    near_limit_up: bool = False             # 接近涨停（limit-0.5pp ~ limit-0.05pp，仍可成交）
    near_limit_down: bool = False           # 接近跌停
    board: str = ""                         # main / star / chinext / bse （主板/科创/创业/北交）
    limit_pct: float = 10.0                 # 当日涨跌幅限制（百分比）

    # 给上层判定用
    tradable: bool = True                   # 可买入（综合）
    block_reasons: list[str] = field(default_factory=list)


@dataclass
class SpotSnapshot:
    """一次性抓取的全 A 股 spot 快照，附带派生分类。"""
    fetched_at: datetime
    by_code: dict[str, StockStatus]
    raw_count: int = 0
    # 盘前抓取标志：>95% 个股 volume==0 → 视为非交易时段。
    # 盘前 akshare 返回上一交易日收盘价 + volume=0，原版会把整个市场误判停牌。
    is_premarket: bool = False


# ───────────── 板块判定 ─────────────

def _classify_board(code: str) -> tuple[str, float]:
    """根据股票代码返回 (板块, 当日涨跌幅限制)。

    A 股涨跌幅规则（截至 2026 年）：
      - 主板（沪 60xxxx / 深 000xxx 001xxx 002xxx 003xxx）：±10%
      - ST 类：±5%（在 _is_st 判定后会覆盖）
      - 科创板（688xxx）：±20%
      - 创业板（300xxx）：±20%
      - 北交所（8xxxxx / 92xxxx 等）：±30%
      - 新股上市首日：主板 44%、创业/科创/北交 无限制（暂不区分，统一按板块返回）
    """
    if not code or not code.isdigit() or len(code) != 6:
        return ("unknown", 10.0)
    if code.startswith("688"):
        return ("star", 20.0)        # 科创板
    if code.startswith("300") or code.startswith("301"):
        return ("chinext", 20.0)     # 创业板（300xxx 主流；301xxx 2020 后新增段）
    if code.startswith(("60", "000", "001", "002", "003")):
        return ("main", 10.0)        # 主板
    # 北交所：83/87/88（沿用新三板老段）+ 92（2024 起新发行段）+ 43（历史精选层，已基本无活跃）
    # 注意：单纯 "8" 开头会误抓港股通通道、跨境 ETF 等，必须用前两位
    if code.startswith(("83", "87", "88", "92", "43")):
        return ("bse", 30.0)         # 北交所
    return ("other", 10.0)


# ───────────── 名称分类 ─────────────

def _is_st(name: str) -> bool:
    if not name:
        return False
    upper = name.upper().replace(" ", "")
    return "ST" in upper or "*ST" in upper or "S*ST" in upper


def _is_delisting(name: str) -> bool:
    """退市整理期股票名称带'退'字（如'乐视退''*ST 鞋退'）。"""
    return bool(name) and "退" in name


def _is_new_stock(name: str) -> bool:
    """新股识别：akshare A 股 spot 数据中，新股名称会带前缀。

      - "N" 前缀：上市首日（无涨跌幅限制，主板首日 44%、其他无限制）
      - "C" 前缀：上市第 2-5 个交易日（仍宽幅波动）

    判定条件：第一个字符是 N/C，且第二个字符是汉字（避开英文公司名误抓）。
    """
    if not name or len(name) < 2:
        return False
    if name[0] not in ("N", "C"):
        return False
    # 第二字符必须是汉字（CJK 统一表意文字）
    return "一" <= name[1] <= "鿿"


def _is_suspended_row(price: Any, volume: Any, amount: Any) -> bool:
    """停牌检测：价格 0 / NaN / 成交量 = 0 都算（akshare 对停牌股的字段填充不一）。"""
    if price is None or (isinstance(price, float) and price != price):
        return True
    if price == 0:
        return True
    # 成交量 0 但价格 > 0 → 一字停牌（开盘前撤单）或全日停盘
    if (volume is None or volume == 0) and (amount is None or amount == 0):
        return True
    return False


def _is_limit(change_pct: float | None, limit_pct: float, eps: float = 0.05) -> tuple[bool, bool]:
    """已封死涨/跌停 — 盘口几乎无筹码，无法成交。

    eps=0.05 收紧到极小值：>= 9.95% 才算"封死"。原值 0.3 把 9.7% 也判定涨停，
    会错杀大量"接近涨停但仍可成交"的票（封单不足，9:45 之后常有撤封）。

    实务区分：
      - 9.95% ~ 10.00%：封死，盘口零筹码（一字板/T 字板尾盘）→ buy 不进
      - 9.50% ~ 9.95% ：接近涨停但仍可成交（near_limit_up）→ 用 _is_near_limit 标记
      - < 9.50%       ：正常波动
    """
    if change_pct is None:
        return (False, False)
    return (change_pct >= limit_pct - eps, change_pct <= -limit_pct + eps)


def _is_near_limit(change_pct: float | None, limit_pct: float,
                   hard_eps: float = 0.05, near_eps: float = 0.5
                   ) -> tuple[bool, bool]:
    """接近涨/跌停（仍可成交，机构通常不视为完全锁仓）。

    near_eps=0.5pp：limit-0.5 到 limit-0.05 之间。
    返回 (near_limit_up, near_limit_down)。
    """
    if change_pct is None:
        return (False, False)
    near_up = (limit_pct - near_eps <= change_pct < limit_pct - hard_eps)
    near_dn = (-limit_pct + hard_eps < change_pct <= -limit_pct + near_eps)
    return (near_up, near_dn)


# ───────────── 主入口：抓全 A 股快照 ─────────────

_SNAPSHOT_CACHE: dict[str, tuple[float, SpotSnapshot]] = {}
_CACHE_TTL_SEC = 300  # 5 分钟


def fetch_spot_snapshot(use_cache: bool = True) -> SpotSnapshot | None:
    """一次抓取全 A 股 spot 数据，本地派生 ST/涨跌停/停牌/板块。

    使用 akshare 的 stock_zh_a_spot_em()（东方财富）。返回包含 5000+ 只股票的快照。
    """
    if use_cache:
        cached = _SNAPSHOT_CACHE.get("all")
        if cached and time.time() - cached[0] < _CACHE_TTL_SEC:
            return cached[1]

    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare not installed; pip install akshare")
        return None

    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        logger.warning("akshare spot_em failed: %s", e)
        return None

    if df is None or df.empty:
        return None

    # ── 盘前检测 ──
    # 9:25 集合竞价之前所有 A 股 volume==0；如果 >95% 都是 0，说明非交易时段。
    # 此时 spot 返回上一交易日收盘价，原版会把全市场判停牌 + change_pct=0 不会触发涨跌停 → 整体逻辑废掉。
    # 解决：标 is_premarket=True，并跳过停牌/涨跌停的派生（仅保留名称类信号 ST/退市）。
    try:
        zero_vol_count = int((df["成交量"].fillna(0) == 0).sum())
    except Exception:
        zero_vol_count = 0
    is_premarket = zero_vol_count > 0.95 * len(df)
    if is_premarket:
        logger.warning("a_share_filters: 检测到盘前/非交易时段（%.0f%% 个股零成交），"
                       "停牌/涨跌停派生已禁用", zero_vol_count / len(df) * 100)

    by_code: dict[str, StockStatus] = {}
    for _, r in df.iterrows():
        code = str(r.get("代码", "")).strip()
        if not code:
            continue

        name = str(r.get("名称", "")).strip()
        price = _to_float(r.get("最新价"))
        change_pct = _to_float(r.get("涨跌幅"))
        volume = _to_float(r.get("成交量"))   # 单位：手
        amount = _to_float(r.get("成交额"))   # 单位：元
        turnover = _to_float(r.get("换手率"))
        cap = _to_float(r.get("总市值"))
        cir = _to_float(r.get("流通市值"))

        board, default_limit = _classify_board(code)
        is_st = _is_st(name)
        is_delisting = _is_delisting(name)
        is_new = _is_new_stock(name)
        # ST 主板 → 5%；ST 科创/创业 → 仍 20%；退市整理期 → 10%；新股首日宽幅
        if is_st and board == "main":
            limit_pct = 5.0
        elif is_delisting:
            limit_pct = 10.0
        elif is_new and name.startswith("N") and board == "main":
            # N 首日主板 ±44%（沪深两市新股首日实际为 ±44% 临停规则下的有效幅度）
            # 涨跌停判定按 44% 处理，避免把首日 +30% 误判涨停
            limit_pct = 44.0
        elif is_new and board in ("star", "chinext"):
            # 科创板/创业板新股前 5 日无价格限制，给一个超大值规避涨跌停误判
            limit_pct = 999.0
        else:
            limit_pct = default_limit

        # 盘前：spot 数据是上一交易日收盘 + volume=0，无法派生当日交易状态
        if is_premarket:
            is_suspended = False
            limit_up = limit_down = False
            near_limit_up = near_limit_down = False
        else:
            is_suspended = _is_suspended_row(price, volume, amount)
            limit_up, limit_down = _is_limit(change_pct, limit_pct)
            near_limit_up, near_limit_down = _is_near_limit(change_pct, limit_pct)

        # 综合判定：可不可以买
        block_reasons: list[str] = []
        if is_suspended:
            block_reasons.append("停牌")
        if is_st:
            block_reasons.append("ST 风险警示")
        if is_delisting:
            block_reasons.append("退市整理期")
        if limit_up:
            block_reasons.append(f"封涨停 ({change_pct:.2f}%)")
        # 注意：near_limit_up 不进 block_reasons — 仍可成交，留给上层决定是否拒绝

        by_code[code] = StockStatus(
            code=code, name=name, price=price, change_pct=change_pct,
            volume=volume, amount=amount, turnover_rate=turnover,
            market_cap=cap, circulating_cap=cir,
            is_st=is_st, is_delisting=is_delisting, is_suspended=is_suspended,
            is_new=is_new,
            limit_up=limit_up, limit_down=limit_down,
            near_limit_up=near_limit_up, near_limit_down=near_limit_down,
            board=board, limit_pct=limit_pct,
            tradable=len(block_reasons) == 0,
            block_reasons=block_reasons,
        )

    snapshot = SpotSnapshot(
        fetched_at=datetime.now(),
        by_code=by_code,
        raw_count=len(by_code),
        is_premarket=is_premarket,
    )
    _SNAPSHOT_CACHE["all"] = (time.time(), snapshot)
    return snapshot


# ───────────── 过滤器 ─────────────

def filter_tradable(codes: list[str],
                    snapshot: SpotSnapshot | None = None,
                    *,
                    allow_st: bool = False,
                    allow_limit_up: bool = False,
                    allow_near_limit_up: bool = True,
                    allow_suspended: bool = False
                    ) -> tuple[list[str], dict[str, list[str]]]:
    """过滤可交易股票。

    返回 (tradable_codes, blocked_reasons)：
      tradable_codes  剩下的可买股票代码列表
      blocked_reasons {code: [理由1, 理由2, ...]}（被剔除原因）

    参数：
      allow_st             是否保留 ST 股（默认 False）
      allow_limit_up       是否保留**封死**涨停股（默认 False，盘口零筹码买不进）
                           —— 卖出场景应设为 True
      allow_near_limit_up  是否保留**接近**涨停（9.5%-9.95%）（默认 True，仍可成交）
                           —— 极保守可设 False
      allow_suspended      是否保留停牌股（默认 False）

    盘前快照（snapshot.is_premarket=True）下，is_suspended/limit_up 都是 False（无法派生），
    故仅靠 ST/退市判定，结果会偏宽松，由上层决定是否信任。

    用法：
      from stock_research.core.a_share_filters import fetch_spot_snapshot, filter_tradable
      snap = fetch_spot_snapshot()
      tradable, blocked = filter_tradable(["600519", "000651", "603893"], snap)
    """
    snapshot = snapshot or fetch_spot_snapshot()
    if snapshot is None:
        # 拉不到快照 → 保守不做过滤（避免误杀），但记日志
        logger.warning("filter_tradable: snapshot unavailable, skipping filter")
        return list(codes), {}

    if snapshot.is_premarket:
        logger.info("filter_tradable: 盘前快照，停牌/涨跌停判定已禁用，仅过滤 ST/退市")

    tradable: list[str] = []
    blocked: dict[str, list[str]] = {}

    for code in codes:
        # 把 600519.SS / sh600519 / 600519 都标准化成 6 位
        std = _strip_code(code)
        st = snapshot.by_code.get(std)
        if st is None:
            blocked[code] = ["无快照数据（可能不是 A 股 / 已退市 / 北交所外）"]
            continue

        reasons = []
        if st.is_suspended and not allow_suspended:
            reasons.append("停牌")
        if st.is_st and not allow_st:
            reasons.append("ST 风险警示")
        if st.is_delisting:
            reasons.append("退市整理期")
        if st.limit_up and not allow_limit_up:
            reasons.append(f"封涨停 ({st.change_pct:.2f}%)")
        if st.near_limit_up and not allow_near_limit_up:
            reasons.append(f"接近涨停 ({st.change_pct:.2f}%)")

        if reasons:
            blocked[code] = reasons
        else:
            tradable.append(code)

    return tradable, blocked


def filter_sellable(codes: list[str],
                    snapshot: SpotSnapshot | None = None) -> tuple[list[str], dict[str, list[str]]]:
    """卖出场景过滤：跌停股、停牌股不能卖；ST 可卖（持仓减仓）。"""
    snapshot = snapshot or fetch_spot_snapshot()
    if snapshot is None:
        return list(codes), {}

    sellable: list[str] = []
    blocked: dict[str, list[str]] = {}
    for code in codes:
        std = _strip_code(code)
        st = snapshot.by_code.get(std)
        if st is None:
            blocked[code] = ["无快照数据"]
            continue
        reasons = []
        if st.is_suspended:
            reasons.append("停牌")
        if st.limit_down:
            reasons.append(f"跌停 ({st.change_pct:.2f}%)")
        if reasons:
            blocked[code] = reasons
        else:
            sellable.append(code)
    return sellable, blocked


# ───────────── A 股流动性约束（替代 ADV）─────────────

def cap_by_volume_cn(target_weights: dict[str, float],
                     prev_weights: dict[str, float],
                     amount_yuan_20d_avg: dict[str, float],
                     portfolio_value_yuan: float,
                     *,
                     max_amount_pct: float = 0.03,
                     min_avg_amount_yuan: float = 5e7
                     ) -> tuple[dict[str, float], list[str]]:
    """A 股版流动性约束：限制单日净买入/卖出 ≤ max_amount_pct × 20 日均成交额。

    与 cap_by_adv 的区别：
      - ADV 是美股概念（Average Daily Volume in $），美股全天连续撮合 + T+0
      - A 股是 9:30-11:30 + 13:00-15:00 分时撮合 + T+1，单日下大单冲击成本远高
      - 故默认 3%（比美股 5% 更保守），且额外要求 20 日均成交额 ≥ 5000 万

    参数：
      target_weights              Markowitz 输出的目标权重 {code: w}
      prev_weights                当前持仓权重 {code: w}
      amount_yuan_20d_avg         {code: 20 日均成交额（元）}
      portfolio_value_yuan        组合总市值（元）
      max_amount_pct              单日交易占 20 日均成交额上限（默认 3%）
      min_avg_amount_yuan         强制门槛：20 日均成交额 < 此值的股票直接拒绝
                                  （默认 5000 万；保守可设 1e8 = 1 亿）

    返回 (capped_weights, warnings)
    """
    if portfolio_value_yuan <= 0:
        raise ValueError("portfolio_value_yuan must be > 0")

    capped: dict[str, float] = {}
    warnings: list[str] = []

    for code, target in target_weights.items():
        prev = prev_weights.get(code, 0.0)
        delta_w = target - prev
        delta_yuan = delta_w * portfolio_value_yuan
        avg_amount = amount_yuan_20d_avg.get(code, 0.0)

        # 门槛 1：流动性太差，直接拒绝（保持原仓位）
        if avg_amount < min_avg_amount_yuan:
            capped[code] = prev
            warnings.append(
                f"{code}: 20 日均成交额 ¥{avg_amount/1e8:.2f}亿 < ¥{min_avg_amount_yuan/1e8:.2f}亿，拒绝"
            )
            continue

        # 门槛 2：单日交易额限流
        max_yuan_per_side = avg_amount * max_amount_pct
        if abs(delta_yuan) > max_yuan_per_side:
            sign = 1 if delta_yuan > 0 else -1
            capped_delta_yuan = sign * max_yuan_per_side
            capped_delta_w = capped_delta_yuan / portfolio_value_yuan
            capped[code] = prev + capped_delta_w
            warnings.append(
                f"{code}: 目标 Δ={delta_w:+.2%} (¥{delta_yuan/1e4:,.0f}万), "
                f"超 {max_amount_pct:.1%} 均额(¥{avg_amount/1e8:.2f}亿)上限, 截到 Δ={capped_delta_w:+.2%}"
            )
        else:
            capped[code] = target

    return capped, warnings


# ───────────── 工具 ─────────────

def _to_float(v) -> float | None:
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _strip_code(code: str) -> str:
    """把 600519.SS / sh600519 / 600519 / 600519.SH 都规范化为 6 位代码。"""
    if not code:
        return ""
    s = str(code).upper().strip()
    # 去前缀
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # 去后缀
    for suffix in (".SS", ".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    s = s.lstrip(".")
    # 提取连续 6 位数字
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return digits


def status_summary(snapshot: SpotSnapshot) -> dict[str, int]:
    """全 A 股状态分布（用于看板/报告）。"""
    counts = {
        "total": snapshot.raw_count,
        "tradable": 0, "st": 0, "delisting": 0, "suspended": 0,
        "limit_up": 0, "limit_down": 0,
        "main": 0, "star": 0, "chinext": 0, "bse": 0, "other": 0,
    }
    for st in snapshot.by_code.values():
        if st.tradable:
            counts["tradable"] += 1
        if st.is_st:
            counts["st"] += 1
        if st.is_delisting:
            counts["delisting"] += 1
        if st.is_suspended:
            counts["suspended"] += 1
        if st.limit_up:
            counts["limit_up"] += 1
        if st.limit_down:
            counts["limit_down"] += 1
        counts[st.board] = counts.get(st.board, 0) + 1
    return counts


# ───────────── CLI ─────────────

def _main():
    """CLI：python -m stock_research.core.a_share_filters [code1 code2 ...]
    无参数时打印全市场状态分布；带参数时对指定代码做可买性判定。
    """
    import sys
    import json

    snap = fetch_spot_snapshot()
    if snap is None:
        print("ERROR: 无法抓取 A 股快照（akshare 失败 / 非交易日 / 网络问题）")
        sys.exit(1)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not args:
        # 模式 1：打印全市场分布
        counts = status_summary(snap)
        print(f"📊 A 股全市场状态（{snap.fetched_at:%Y-%m-%d %H:%M}）")
        print(f"  总股票数: {counts['total']}")
        print(f"  可交易:   {counts['tradable']} ({counts['tradable']/counts['total']*100:.1f}%)")
        print(f"  ST/*ST:   {counts['st']}")
        print(f"  退市整理: {counts['delisting']}")
        print(f"  停牌:     {counts['suspended']}")
        print(f"  涨停:     {counts['limit_up']}")
        print(f"  跌停:     {counts['limit_down']}")
        print()
        print(f"  主板:     {counts['main']}")
        print(f"  科创板:   {counts['star']}")
        print(f"  创业板:   {counts['chinext']}")
        print(f"  北交所:   {counts['bse']}")

        # Top 涨停 / 跌停举例
        ups = [s for s in snap.by_code.values() if s.limit_up][:5]
        if ups:
            print(f"\n  示例涨停（前 5）：")
            for s in ups:
                print(f"    {s.code} {s.name} {s.change_pct:+.2f}%")
        return

    # 模式 2：对指定代码做判定
    print(f"🔍 检查 {len(args)} 只股票（{snap.fetched_at:%Y-%m-%d %H:%M} 快照）\n")
    tradable, blocked = filter_tradable(args, snap)
    for code in args:
        std = _strip_code(code)
        st = snap.by_code.get(std)
        if st is None:
            print(f"  ❓ {code}: 无数据")
            continue
        flag = "✅" if st.tradable else "❌"
        reasons = " | ".join(st.block_reasons) if st.block_reasons else "可买"
        print(f"  {flag} {code} {st.name:<10} ¥{st.price:>7.2f} {st.change_pct:+6.2f}% "
              f"[{st.board:<7}±{st.limit_pct:.0f}%] {reasons}")
    print(f"\n  汇总：可买 {len(tradable)} / 拦截 {len(blocked)}")


if __name__ == "__main__":
    _main()
