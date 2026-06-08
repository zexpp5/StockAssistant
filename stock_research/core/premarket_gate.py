"""美股盘前风险闸门 — 开盘前回答"今晚能不能买"。

为什么是独立模块（不是 defense_watcher 的延长）：
  defense_watcher 的语义是"大盘风险变差才推"，作息 8:00-22:00 用收盘价；
  它是给 A 股盘前 + 美股盘后用的。本模块是另一个值班岗位：
  在【美股开盘前】（北京 20-21 点）主动扫一遍跨市场，回答一个问题——
  "今晚这个环境，适不适合开新仓？"

为什么比"看韩国跌/看英伟达跌"可靠：
  它不是事件后总结，而是每天固定把 8 类信号全扫一遍、加权、再绑到你的持仓：
    - NVDA/AVGO 盘前跌      → 归因 AI 硬件链
    - AAPL/GOOGL/MSFT 跌    → 归因 Nasdaq 权重/平台股
    - 利率飙               → 归因成长股估值压力
    - 海外科技先跌         → 归因跨市场 risk-off
    - 都亮                 → 直接橙/红
  所以不是盯某一只，而是看"压力从哪来，传导到你哪些持仓"。

Phase 1 信号族（7 开 + 1 延后）：
  1. 美股期货 ES/NQ/RTY     —— 最直接的开盘预读（近 24h 交易）
  2. 利率/美元 10Y/5Y/DXY    —— 急涨杀成长股估值
  3. VIX/期权 PCR           —— 恐慌情绪（复用 options_signals）
  4. 巨头盘前 mag7 广度      —— 7 只里几只盘前跌超 1%
  5. 板块 XLK/SMH/SOXX/XLP/XLU —— 成长杀 vs 单一板块、防御轮动
  6. 海外领先 KOSPI/日经/台股/港股 —— 美股开盘前它们已收，真·领先读数
  7. 宏观日历 NFP/CPI/FOMC   —— 事件日风险（硬编排表 + 启发式）
  8. 财报余波（AVGO 类）     —— Phase 2，需财报日历

颜色沿用现有语言（NONE/LOW/HIGH/CRITICAL），不发明新体系：
  🟢 NONE     正常研究
  🟡 LOW      少量试探，不追涨
  🟠 HIGH     不开新仓，等开盘 30-60 分钟
  🔴 CRITICAL 原则上不买，只处理纪律线

边界（诚实说）：
  - 判断的是【环境】（能不能买），不承诺具体跌几个点。
  - 盘前个股流动性薄，巨头盘前报价偶有噪声；期货才是最稳读数。
  - 没有带"一致预期 vs 实际"的实时经济日历，事件日用硬编排表 + 启发式。

设计：compute_gate(quotes=...) 可注入行情，便于测试复现历史场景；
     job 层负责 fetch_all_quotes() 拉真实数据再传入。后端算一次，前端只渲染。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# 行情口径与符号表
# ──────────────────────────────────────────────────

# canonical key → yfinance 候选符号（按优先级 fallback）
UNIVERSE: dict[str, list[str]] = {
    # 期货
    "ES": ["ES=F"], "NQ": ["NQ=F"], "RTY": ["RTY=F"],
    # 利率 / 美元
    "US10Y": ["^TNX"], "US5Y": ["^FVX"], "DXY": ["DX-Y.NYB", "^DXY"],
    # 波动率
    "VIX": ["^VIX"],
    # 巨头（盘前广度）
    "AAPL": ["AAPL"], "MSFT": ["MSFT"], "NVDA": ["NVDA"], "GOOGL": ["GOOGL"],
    "AMZN": ["AMZN"], "META": ["META"], "TSLA": ["TSLA"],
    # 板块
    "XLK": ["XLK"], "SMH": ["SMH"], "SOXX": ["SOXX"], "XLP": ["XLP"], "XLU": ["XLU"],
    # 海外领先（美股开盘前已收）
    "KOSPI": ["^KS11"], "NIKKEI": ["^N225"], "TWSE": ["^TWII"], "HSI": ["^HSI"],
}

MEGA7 = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"]
_PREMARKET_KEYS = set(MEGA7)  # 这些用股票盘前报价口径
CN_NAME = {  # 给新手看的中文名
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "GOOGL": "谷歌",
    "AMZN": "亚马逊", "META": "Meta", "TSLA": "特斯拉",
}

# 持仓敏感度分类（绑持仓用）
AI_HARDWARE = {
    "NVDA", "AVGO", "MRVL", "AMD", "MU", "SMCI", "DELL", "HPE", "TSM", "ASML",
    "ARM", "QCOM", "INTC", "LRCX", "AMAT", "KLAC", "ANET", "VRT", "CRDO",
    # 港股/A 股硬件链（按需扩）
    "0981.HK", "1347.HK",
}
MEGA_PLATFORM = {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "NFLX"}

ICON = {"NONE": "🟢", "LOW": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
TEMPLATE = {"NONE": "blue", "LOW": "yellow", "HIGH": "orange", "CRITICAL": "red"}
SEVERITY_ORDER = {"NONE": 0, "LOW": 1, "HIGH": 2, "CRITICAL": 3}

CAN_BUY = {
    "NONE": "可正常研究/按计划操作。",
    "LOW": "少量试探可以，但不要追涨；单笔新开仓控制小一点。",
    "HIGH": "不建议开新仓；想买也等开盘 30-60 分钟企稳再看。",
    "CRITICAL": "原则上今晚不开新仓，只处理已有持仓的纪律线（止损/减仓）。",
}

# 颜色 → 一句话人话标题（给新手）
HEADLINE_PLAIN = {
    "NONE": "🟢 今晚环境正常，可以按计划研究/操作",
    "LOW": "🟡 今晚略偏谨慎：可以小仓试探，但别追高",
    "HIGH": "🟠 今晚环境偏差：先别开新仓",
    "CRITICAL": "🔴 今晚环境很差：原则上别买，只管好手里已有的",
}

# 信号族权重（期货最直接，权重最高）
# 数据质量保险丝阈值：覆盖率低于此，绿/黄不给买入结论
MIN_COVERAGE = 0.6
MIN_MEGACAP_PREMARKET_QUOTES = 4

WEIGHTS = {
    "futures": 3.0,
    "rates": 2.0,
    "vol": 2.0,
    "megacap": 2.0,
    "sector": 1.5,
    "overseas": 1.5,
    "macro": 1.0,
}

# NYSE 全天休市（2026；盘前闸门用来避免周末/假日/手工测试产物被当成有效信号）
US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def is_us_trading_day(d: date) -> bool:
    """Conservative NYSE trading-day check for the premarket gate."""
    return d.weekday() < 5 and d.isoformat() not in US_HOLIDAYS_2026


def _epoch_to_iso(v: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


# ──────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────

@dataclass
class FamilySignal:
    key: str
    label: str
    stress: float = 0.0           # 0-3 压力分
    headline: str = ""            # 一句话结论（技术口径，带数字）
    plain: str = ""               # 人话解释（给新手看的飞书卡/横幅）
    tags: list[str] = field(default_factory=list)  # 如 ai_hardware / event_pending / rates_spike
    data: dict[str, Any] = field(default_factory=dict)
    available: bool = True        # 数据是否拿到

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GateResult:
    as_of: str
    generated_at: str
    color: str = "NONE"           # NONE/LOW/HIGH/CRITICAL
    composite: float = 0.0        # 0-3 加权综合压力
    can_buy: str = ""
    headline_plain: str = ""      # 颜色→一句话人话标题
    top_alarm: str = ""           # 🚨 最该注意的一条（最严重信号，置顶突出）
    tailwind_score: int = 0       # 顺风读数：有几样「有利」(仅环境层面，绿灯时才看)
    is_tailwind: bool = False     # 绿灯且明显顺风
    tailwind_reasons: list[str] = field(default_factory=list)  # 为什么算顺风(人话)
    reasons: list[str] = field(default_factory=list)       # 触发原因（技术口径，带数字）
    reasons_plain: list[str] = field(default_factory=list)  # 触发原因（人话，按严重度标🔴/🟠）
    families: list[dict] = field(default_factory=list)     # 各族明细
    holdings_impact: list[dict] = field(default_factory=list)  # [{symbol, reason}]
    pressure_sources: list[str] = field(default_factory=list)
    coverage: float = 1.0         # 数据覆盖率（拿到/应拿）
    insufficient_data: bool = False  # 数据质量保险丝：覆盖率过低，绿/黄时不给买入结论
    notes: list[str] = field(default_factory=list)

    @property
    def icon(self) -> str:
        return ICON.get(self.color, "⚪")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["icon"] = self.icon
        return d


# ──────────────────────────────────────────────────
# 行情获取（yfinance；job 层调用，测试可绕过）
# ──────────────────────────────────────────────────

def _fetch_one(yf_symbols: list[str], prefer_premarket: bool = False) -> dict[str, Any]:
    """拉单个标的的 last / prev_close / pct。

    口径：
      - prev_close 用 fast_info.previous_close（上一交易日结算/收盘）
      - last 用 fast_info.last_price；股票盘前优先 info['preMarketPrice']
      - 期货近 24h 交易 → last 即盘前/隔夜价（最稳）
      - 海外指数（已收盘）→ last≈今日收盘，pct=今日全天涨跌
    """
    out = {
        "last": None,
        "prev_close": None,
        "pct": None,
        "source": "",
        "source_kind": "",
        "ok": False,
        "premarket": False,
        "quote_time": None,
        "quote_date": None,
        "stale_for_premarket": bool(prefer_premarket),
    }
    try:
        import yfinance as yf
    except Exception as e:  # pragma: no cover
        out["source"] = f"yfinance 不可用: {e}"
        return out

    for sym in yf_symbols:
        try:
            t = yf.Ticker(sym)
            last = prev = None
            premarket = False

            if prefer_premarket:
                try:
                    info = t.info or {}
                    pm = info.get("preMarketPrice")
                    rc = info.get("regularMarketPreviousClose") or info.get("previousClose")
                    if pm and rc:
                        last, prev, premarket = float(pm), float(rc), True
                        out["source_kind"] = "premarket"
                        out["quote_time"] = _epoch_to_iso(
                            info.get("preMarketTime") or info.get("regularMarketTime")
                        )
                        out["stale_for_premarket"] = False
                except Exception:
                    pass

            if last is None or prev is None:
                try:
                    fi = t.fast_info
                    last = float(fi.last_price)
                    prev = float(fi.previous_close)
                    out["source_kind"] = "fast_info"
                except Exception:
                    last = prev = None

            if last is None or prev is None:
                h = t.history(period="5d", interval="1d")
                closes = [float(x) for x in h["Close"].tolist() if x == x] if len(h) else []
                if len(closes) >= 2:
                    last, prev = closes[-1], closes[-2]
                    out["source_kind"] = "daily_history"
                    try:
                        out["quote_date"] = h.index[-1].date().isoformat()
                    except Exception:
                        pass

            if last is not None and prev and prev > 0:
                if not prefer_premarket:
                    out["stale_for_premarket"] = False
                out.update(
                    last=round(last, 4),
                    prev_close=round(prev, 4),
                    pct=round((last / prev - 1.0) * 100.0, 3),
                    source=sym,
                    ok=True,
                    premarket=premarket,
                )
                return out
        except Exception as e:
            logger.debug("fetch %s 失败: %s", sym, str(e)[:80])
            continue

    out["source"] = "无数据"
    return out


def fetch_all_quotes() -> dict[str, dict]:
    """拉全 UNIVERSE 的行情快照，返回 {canonical_key: quote}。"""
    quotes: dict[str, dict] = {}
    for key, syms in UNIVERSE.items():
        quotes[key] = _fetch_one(syms, prefer_premarket=key in _PREMARKET_KEYS)
    return quotes


def _pct(quotes: dict, key: str) -> float | None:
    q = quotes.get(key) or {}
    return q.get("pct") if q.get("ok") else None


def _val(quotes: dict, key: str) -> float | None:
    q = quotes.get(key) or {}
    return q.get("last") if q.get("ok") else None


def _quote_is_reliable_premarket(q: dict) -> bool:
    """Whether a stock quote is acceptable for premarket breadth.

    Live yfinance stock data is only reliable here when it exposes an explicit
    preMarketPrice. Unit tests inject source=mock to validate historical scenes.
    """
    if not q or not q.get("ok"):
        return False
    if q.get("source") == "mock" or q.get("source_kind") == "mock":
        return True
    return bool(q.get("premarket"))


# ──────────────────────────────────────────────────
# 各信号族（每个返回 0-3 压力分）
# ──────────────────────────────────────────────────

def _sig_futures(quotes: dict) -> FamilySignal:
    """美股期货：ES/NQ/RTY。科技重 NQ 是这本账的主读数。"""
    nq, es, rty = _pct(quotes, "NQ"), _pct(quotes, "ES"), _pct(quotes, "RTY")
    avail = [p for p in (nq, es, rty) if p is not None]
    sig = FamilySignal("futures", "美股期货")
    if not avail:
        sig.available = False
        sig.headline = "期货数据未取到"
        return sig
    # 主读数：NQ 优先，否则 ES；广度看三者
    head = nq if nq is not None else es
    worst = min(avail)
    sig.data = {"NQ": nq, "ES": es, "RTY": rty}
    if head is None:
        head = worst
    if head <= -1.5:
        sig.stress = 3.0
    elif head <= -0.8:
        sig.stress = 2.0
    elif head <= -0.3:
        sig.stress = 1.0
    else:
        sig.stress = 0.0
    # 三者齐跌（广度）加压
    if len(avail) >= 2 and all(p <= -0.5 for p in avail):
        sig.stress = min(3.0, sig.stress + 0.5)
        sig.tags.append("broad_futures_down")
    sig.headline = f"NQ {nq:+.2f}% · ES {es:+.2f}%" if nq is not None and es is not None else f"期货主读数 {head:+.2f}%"
    if head <= -1.5:
        sig.tags.append("futures_deep")
    if head <= -0.3:
        sig.plain = (f"美股还没开盘，但「盘前预演价」已经在跌——科技股那档预计低开约 {abs(head):.1f}%。"
                     "（期货=开盘前的预测价，跌得多通常预示开盘不好）")
    else:
        sig.plain = f"美股盘前的「预演价」还算稳（科技股那档 {head:+.1f}%）。"
    return sig


def _sig_rates(quotes: dict) -> FamilySignal:
    """利率/美元：10Y 急涨 + DXY 走强 → 杀成长股估值。看变化不看绝对值。"""
    sig = FamilySignal("rates", "利率/美元")
    q10 = quotes.get("US10Y") or {}
    dxy_pct = _pct(quotes, "DXY")
    last10, prev10 = q10.get("last"), q10.get("prev_close")
    bps = None
    if q10.get("ok") and last10 is not None and prev10 is not None:
        # ^TNX 现为收益率百分点（如 4.54）；若历史口径 ×10 则 >20，统一成 bps
        scale = 1.0 if last10 < 20 else 0.1
        bps = (last10 - prev10) * scale * 100.0
    if bps is None and dxy_pct is None:
        sig.available = False
        sig.headline = "利率/美元数据未取到"
        return sig
    sig.data = {"ten_year": last10, "ten_year_bps_1d": round(bps, 1) if bps is not None else None,
                "dxy_pct": dxy_pct}
    rate_stress = 0.0
    if bps is not None:
        if bps >= 12:
            rate_stress = 3.0
        elif bps >= 8:
            rate_stress = 2.0
        elif bps >= 4:
            rate_stress = 1.0
        if bps >= 8:
            sig.tags.append("rates_spike")
    dxy_stress = 0.0
    if dxy_pct is not None:
        if dxy_pct >= 0.8:
            dxy_stress = 2.0
        elif dxy_pct >= 0.4:
            dxy_stress = 1.0
    sig.stress = max(rate_stress, dxy_stress * 0.8)
    parts = []
    if bps is not None:
        parts.append(f"10Y {last10:.2f}% ({bps:+.0f}bp)")
    if dxy_pct is not None:
        parts.append(f"DXY {dxy_pct:+.2f}%")
    sig.headline = " · ".join(parts) if parts else "—"
    if bps is not None and bps >= 4:
        sig.plain = (f"美国国债利率在往上走（10 年期到 {last10:.2f}%）。利率一涨，估值高的科技股最吃亏——"
                     "因为钱放银行/买债的收益变高了，大家就不愿再给科技股付高价。")
        if dxy_pct is not None and dxy_pct >= 0.4:
            sig.plain += "同时美元也在走强，对成长股是双重压力。"
    elif dxy_pct is not None and dxy_pct >= 0.4:
        sig.plain = f"美元在走强（{dxy_pct:+.1f}%），通常对高估值科技股不利。"
    else:
        sig.plain = "利率和美元都还平稳，对估值没额外压力。"
    return sig


def _sig_vol(quotes: dict) -> FamilySignal:
    """VIX + 期权 PCR。VIX≥40 触发 CRITICAL 硬覆盖（在聚合处理）。"""
    sig = FamilySignal("vol", "波动率/期权")
    q = quotes.get("VIX") or {}
    vix, prev = q.get("last"), q.get("prev_close")
    if not q.get("ok") or vix is None:
        sig.available = False
        sig.headline = "VIX 未取到"
        return sig
    chg = ((vix / prev - 1.0) * 100.0) if prev else None
    sig.data = {"vix": vix, "vix_chg_pct": round(chg, 1) if chg is not None else None}
    if vix >= 40:
        sig.stress = 3.0
        sig.tags.append("vix_panic")
    elif vix >= 30:
        sig.stress = 2.5
        sig.tags.append("vix_high")
    elif vix >= 20:
        sig.stress = 2.0
    elif vix >= 16:
        sig.stress = 1.0
    else:
        sig.stress = 0.0
    if chg is not None and chg >= 15:
        sig.stress = min(3.0, sig.stress + 0.5)
        sig.tags.append("vix_jump")
    sig.headline = f"VIX {vix:.1f}" + (f" ({chg:+.0f}%)" if chg is not None else "")
    # 期权 PCR（best-effort，网络可能失败）
    try:
        from stock_research.core import options_signals
        diag = options_signals.diagnose()
        pcr = diag.get("pcr_volume")
        if pcr is not None:
            sig.data["pcr_volume"] = pcr
            if pcr >= 1.2:
                sig.stress = min(3.0, sig.stress + 0.5)
                sig.tags.append("pcr_bearish")
                sig.headline += f" · PCR {pcr:.2f}"
    except Exception as e:
        logger.debug("PCR 跳过: %s", str(e)[:60])
    chg_txt = f"，一天跳了 {chg:.0f}%" if chg is not None and chg >= 15 else ""
    if vix >= 30:
        sig.plain = f"市场「恐慌指数」VIX 冲到 {vix:.0f}（很高）{chg_txt}，说明投资者在恐慌抛售。"
    elif vix >= 20:
        sig.plain = f"市场「恐慌指数」VIX 到 {vix:.0f}{chg_txt}，情绪偏紧张。"
    elif chg is not None and chg >= 15:
        sig.plain = f"市场「恐慌指数」VIX 虽不高（{vix:.0f}）但{chg_txt[1:]}，紧张在升温。"
    else:
        sig.plain = f"市场情绪平稳（恐慌指数 VIX {vix:.0f}，不高）。"
    return sig


def _sig_megacap(quotes: dict) -> FamilySignal:
    """巨头盘前广度：7 只里几只盘前跌超 1%。看广度不看单只。"""
    sig = FamilySignal("megacap", "巨头盘前")
    raw = {k: quotes.get(k) or {} for k in MEGA7}
    pcts = {k: q.get("pct") for k, q in raw.items() if _quote_is_reliable_premarket(q)}
    have = {k: v for k, v in pcts.items() if v is not None}
    skipped = [k for k, q in raw.items() if q.get("ok") and not _quote_is_reliable_premarket(q)]
    if len(have) < MIN_MEGACAP_PREMARKET_QUOTES:
        sig.available = False
        sig.headline = f"巨头可靠盘前价不足（{len(have)}/{len(MEGA7)}）"
        sig.plain = ("科技巨头没有拿到足够可靠的盘前成交价，"
                     "这部分不参与今晚结论，避免把昨收/旧价当成盘前信号。")
        sig.data = {
            "usable": list(have.keys()),
            "skipped_stale_or_regular": skipped,
            "min_required": MIN_MEGACAP_PREMARKET_QUOTES,
        }
        return sig
    down1 = [k for k, v in have.items() if v <= -1.0]
    avg = sum(have.values()) / len(have)
    sig.data = {"pct": have, "down_over_1pct": down1, "avg": round(avg, 2),
                "usable": list(have.keys()), "skipped_stale_or_regular": skipped}
    n = len(down1)
    if n >= 5:
        sig.stress = 3.0
    elif n >= 3:
        sig.stress = 2.0
    elif n >= 1:
        sig.stress = 1.0
    else:
        sig.stress = 0.0
    if avg <= -1.5:
        sig.stress = min(3.0, sig.stress + 0.5)
    if down1:
        sig.tags.append("megacap_broad" if n >= 3 else "megacap_partial")
    sig.headline = f"{n}/{len(have)} 只盘前跌超 1%（均 {avg:+.2f}%）"
    if down1:
        sig.headline += "：" + " ".join(down1[:5])
    if n >= 1:
        names = "、".join(CN_NAME.get(k, k) for k in down1[:5])
        sig.plain = (f"美股 7 大科技巨头里有 {n} 个开盘前就在跌（{names}）。"
                     "这几只权重特别大，它们跌，整个指数就难看。")
    else:
        sig.plain = "7 大科技巨头盘前没明显下跌，权重股暂时稳。"
    return sig


def _sig_sector(quotes: dict) -> FamilySignal:
    """板块轮动：成长(XLK/SMH/SOXX)杀 + 防御(XLP/XLU)抗 = risk-off。半导体专门标。"""
    sig = FamilySignal("sector", "板块/风格")
    xlk, smh, soxx = _pct(quotes, "XLK"), _pct(quotes, "SMH"), _pct(quotes, "SOXX")
    xlp, xlu = _pct(quotes, "XLP"), _pct(quotes, "XLU")
    semis = [p for p in (smh, soxx) if p is not None]
    growth = [p for p in (xlk, smh, soxx) if p is not None]
    defensive = [p for p in (xlp, xlu) if p is not None]
    if not growth:
        sig.available = False
        sig.headline = "板块数据未取到"
        return sig
    g_avg = sum(growth) / len(growth)
    d_avg = (sum(defensive) / len(defensive)) if defensive else None
    spread = (d_avg - g_avg) if d_avg is not None else None  # 正 = 防御跑赢成长 = risk-off
    sig.data = {"XLK": xlk, "SMH": smh, "SOXX": soxx, "XLP": xlp, "XLU": xlu,
                "growth_avg": round(g_avg, 2),
                "defensive_avg": round(d_avg, 2) if d_avg is not None else None,
                "rotation_spread": round(spread, 2) if spread is not None else None}
    # 成长走弱
    if g_avg <= -2.0:
        sig.stress = 3.0
    elif g_avg <= -1.2:
        sig.stress = 2.0
    elif g_avg <= -0.5:
        sig.stress = 1.0
    else:
        sig.stress = 0.0
    # risk-off 轮动加压
    if spread is not None and spread >= 1.0 and g_avg < 0:
        sig.stress = min(3.0, sig.stress + 0.5)
        sig.tags.append("risk_off_rotation")
    # 半导体专门标（AI 硬件链）
    if semis and (sum(semis) / len(semis)) <= -1.5:
        sig.tags.append("ai_hardware")
    sig.headline = f"成长均 {g_avg:+.2f}%"
    if d_avg is not None:
        sig.headline += f" · 防御均 {d_avg:+.2f}%"
    if semis:
        sig.headline += f" · 半导体 {sum(semis)/len(semis):+.2f}%"
    semi_txt = f"（其中芯片股最惨，约 {sum(semis)/len(semis):.0f}%）" if semis and sum(semis)/len(semis) < 0 else ""
    if g_avg <= -0.5:
        if d_avg is not None and d_avg > 0:
            sig.plain = (f"科技/成长类全线跌{semi_txt}，而平时抗跌的「防御类」（水电、日用必需品）反而在涨——"
                         "典型的「资金从激进股票往保守股票躲」，整体在避险。")
        else:
            sig.plain = f"科技/成长类普遍下跌{semi_txt}。"
    else:
        sig.plain = "各板块没出现明显的避险轮动。"
    return sig


def _sig_overseas(quotes: dict) -> FamilySignal:
    """海外领先：KOSPI/日经/台股/港股。美股开盘前已收 = 真·领先读数。半导体重的 KOSPI/台股专门标。"""
    sig = FamilySignal("overseas", "海外领先")
    kospi, nikkei = _pct(quotes, "KOSPI"), _pct(quotes, "NIKKEI")
    twse, hsi = _pct(quotes, "TWSE"), _pct(quotes, "HSI")
    have = {k: v for k, v in {"KOSPI": kospi, "NIKKEI": nikkei, "TWSE": twse, "HSI": hsi}.items()
            if v is not None}
    if not have:
        sig.available = False
        sig.headline = "海外指数未取到"
        return sig
    avg = sum(have.values()) / len(have)
    sig.data = {"pct": have, "avg": round(avg, 2)}
    if avg <= -2.0:
        sig.stress = 3.0
    elif avg <= -1.2:
        sig.stress = 2.0
    elif avg <= -0.5:
        sig.stress = 1.0
    else:
        sig.stress = 0.0
    # 半导体重市场（韩国/台湾）先跌 = AI 硬件链领先信号
    semis_mkt = [v for k, v in have.items() if k in ("KOSPI", "TWSE")]
    if semis_mkt and (sum(semis_mkt) / len(semis_mkt)) <= -1.5:
        sig.tags.append("ai_hardware")
        sig.tags.append("asia_semis_lead")
    sig.headline = " · ".join(f"{k} {v:+.1f}%" for k, v in have.items())
    _cn = {"KOSPI": "韩国", "NIKKEI": "日本", "TWSE": "台湾", "HSI": "香港"}
    if avg <= -0.5:
        parts_cn = "、".join(f"{_cn[k]} {v:+.1f}%" for k, v in have.items())
        # 单一市场明显异动（跌得比平均凶很多）→ 直接点名喊出来
        worst_k = min(have, key=have.get)
        worst_v = have[worst_k]
        lead = ""
        if worst_v <= -2.0 and worst_v <= avg - 1.0:
            lead = f"**{_cn[worst_k]}股市明显异动、大跌 {abs(worst_v):.1f}%**——这是今晚亚洲最强的坏信号。"
        sig.plain = lead + f"比美股更早开盘的亚洲市场已收盘，普遍在跌（{parts_cn}）。"
        if "asia_semis_lead" in sig.tags:
            sig.plain += "韩国和台湾芯片股扎堆，它们先跳水，往往是美国芯片股的「预告片」。"
    else:
        sig.plain = "亚洲市场今天没明显下跌，没给美股递坏消息。"
    return sig


# ── 宏观日历（硬编排表 + 启发式）──────────────────────────

# 2026 FOMC 会议日（美联储提前一年公布；待人工校准）。值为会议第二天（决议/发布会日）。
FOMC_2026 = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}


def _macro_events_for(d: date) -> list[dict]:
    """给定日期的美国宏观事件。NFP=每月第一个周五（启发式），CPI≈每月 10-15 号（启发式），FOMC=编排表。"""
    events: list[dict] = []
    iso = d.isoformat()
    # NFP：每月第一个周五
    if d.weekday() == 4 and d.day <= 7:
        events.append({"type": "NFP", "label": "非农就业", "release_bj_hint": "夏令时约 20:30"})
    # CPI：每月 10-15 号的工作日（粗启发，待编排表替换）
    if 10 <= d.day <= 15 and d.weekday() < 5:
        events.append({"type": "CPI", "label": "CPI 通胀", "release_bj_hint": "夏令时约 20:30"})
    # FOMC：编排表
    if iso in FOMC_2026:
        events.append({"type": "FOMC", "label": "美联储决议", "release_bj_hint": "夏令时约 02:00 次日 + 鲍威尔发布会"})
    return events


def _sig_macro(as_of: date, now: datetime | None = None) -> FamilySignal:
    """宏观日历：今晚有没有 NFP/CPI/FOMC。事件未发布 = 不确定性溢价（最低 1 分）。"""
    sig = FamilySignal("macro", "宏观日历")
    events = _macro_events_for(as_of)
    sig.data = {"events": events, "date": as_of.isoformat()}
    if not events:
        sig.stress = 0.0
        sig.headline = "今晚无重磅宏观数据"
        sig.plain = "今晚没有重磅经济数据公布，少一个突发变量。"
        return sig
    labels = "/".join(e["label"] for e in events)
    # 发布前（北京 20:30 前，夏令时）不确定性更高；发布后市场已在定价，靠其它族体现
    pending = True
    if now is not None:
        pending = now.hour < 21  # 粗略：21 点前视为发布前/刚发布
    if pending:
        sig.stress = 1.0
        sig.tags.append("event_pending")
        sig.headline = f"⚠️ 今晚有 {labels}（发布前，结果出来前别重仓押方向）"
        sig.plain = (f"今晚有重磅经济数据要公布（{labels}）。数据出来前谁也不知道好坏，"
                     "这种时候重仓押一个方向风险大，最好等数据落地再说。")
    else:
        sig.stress = 0.5
        sig.tags.append("event_released")
        sig.headline = f"今晚 {labels} 已/将发布，关注数据 vs 预期"
        sig.plain = f"今晚的经济数据（{labels}）已经/即将公布，市场正在消化结果。"
    return sig


# ──────────────────────────────────────────────────
# 聚合
# ──────────────────────────────────────────────────

def _color_from(composite: float) -> str:
    if composite >= 1.9:
        return "CRITICAL"
    if composite >= 1.1:
        return "HIGH"
    if composite >= 0.5:
        return "LOW"
    return "NONE"


def _holdings_overlay(families: list[FamilySignal], holdings: list[dict] | None) -> list[dict]:
    """把今晚的压力源绑到你的持仓上：点名哪几只在风口。"""
    if not holdings:
        return []
    all_tags = {t for f in families for t in f.tags}
    fam_by_key = {f.key: f for f in families}
    rates_hot = fam_by_key.get("rates") and fam_by_key["rates"].stress >= 2
    ai_hardware_hot = "ai_hardware" in all_tags
    futures_hot = fam_by_key.get("futures") and fam_by_key["futures"].stress >= 2
    megacap_hot = "megacap_broad" in all_tags

    impact: list[dict] = []
    for h in holdings:
        sym = str(h.get("symbol") or h.get("code") or "").upper()
        mkt = (h.get("market") or "").upper()
        if not sym:
            continue
        # 只对美股做盘前归因（A/港股不受美股盘前直接影响）
        if mkt and mkt not in ("US", "美股", "USA", "NASDAQ", "NYSE"):
            continue
        base = sym.split(".")[0]
        reasons = []
        if base in AI_HARDWARE or sym in AI_HARDWARE:
            if ai_hardware_hot:
                reasons.append("你这只是芯片/AI 硬件股，今晚芯片是重灾区，别加仓，盯好你的止损线")
            elif futures_hot:
                reasons.append("科技股盘前走弱，今晚开盘波动可能大，先观望")
        if base in MEGA_PLATFORM:
            if megacap_hot:
                reasons.append("大科技股今晚被普遍抛售，别追高")
            elif rates_hot:
                reasons.append("利率上涨会压高估值科技股，今晚留意")
        # 注：不做"利率高就给所有持仓贴标签"的兜底——防御/低估值票（如 KO）
        # 反而相对受益，乱贴反而误导。只对能明确分类的持仓归因。
        if reasons:
            impact.append({"symbol": sym, "market": mkt or "US", "reason": "；".join(reasons)})
    return impact


def _tailwind(families: list[FamilySignal]) -> tuple[int, list[str]]:
    """顺风读数：防守闸门之外，数一数有几样是「有利」的（仅环境层面，不喊买某只）。"""
    by = {f.key: f for f in families}
    score = 0
    reasons: list[str] = []
    fut = by.get("futures")
    if fut and fut.available:
        vals = [v for v in (fut.data.get("NQ"), fut.data.get("ES")) if v is not None]
        head = max(vals) if vals else None
        if head is not None and head >= 0.5:
            score += 1
            reasons.append(f"美股盘前「预演价」在涨（科技股那档 {head:+.1f}%）")
    vol = by.get("vol")
    if vol and vol.available:
        vix = vol.data.get("vix")
        if vix is not None and vix < 16:
            score += 1
            reasons.append(f"市场情绪平静（恐慌指数 VIX 只有 {vix:.0f}，偏低）")
    mega = by.get("megacap")
    if mega and mega.available:
        avg = mega.data.get("avg")
        if avg is not None and avg >= 0.5:
            score += 1
            reasons.append(f"科技巨头盘前普遍在涨（平均 {avg:+.1f}%）")
    sec = by.get("sector")
    if sec and sec.available:
        g = sec.data.get("growth_avg")
        if g is not None and g >= 0.5:
            score += 1
            reasons.append(f"科技/成长板块在涨（平均 {g:+.1f}%）")
    ovs = by.get("overseas")
    if ovs and ovs.available:
        avg = ovs.data.get("avg")
        if avg is not None and avg >= 0.5:
            score += 1
            reasons.append(f"亚洲市场普涨，给美股递了好消息（平均 {avg:+.1f}%）")
    rate = by.get("rates")
    if rate and rate.available:
        bps = rate.data.get("ten_year_bps_1d")
        if bps is not None and bps <= -4:
            score += 1
            reasons.append(f"美国利率在回落（10 年 {bps:+.0f}bp），利好高估值科技股")
    return score, reasons


def compute_gate(
    quotes: dict[str, dict] | None = None,
    as_of: date | None = None,
    now: datetime | None = None,
    holdings: list[dict] | None = None,
) -> GateResult:
    """主入口。quotes 不传则实时拉取（测试可注入复现历史场景）。"""
    if as_of is None:
        as_of = (now or datetime.now()).date()
    if quotes is None:
        quotes = fetch_all_quotes()

    families = [
        _sig_futures(quotes),
        _sig_rates(quotes),
        _sig_vol(quotes),
        _sig_megacap(quotes),
        _sig_sector(quotes),
        _sig_overseas(quotes),
        _sig_macro(as_of, now),
    ]

    # 加权综合（只对拿到数据的族计权）
    num = den = 0.0
    avail_cnt = 0
    for f in families:
        if not f.available:
            continue
        w = WEIGHTS.get(f.key, 1.0)
        num += f.stress * w
        den += w
        avail_cnt += 1
    composite = (num / den) if den > 0 else 0.0
    color = _color_from(composite)

    # 硬覆盖规则
    notes: list[str] = []
    vix_fam = next((f for f in families if f.key == "vol"), None)
    fut_fam = next((f for f in families if f.key == "futures"), None)
    if vix_fam and "vix_panic" in vix_fam.tags:
        color = "CRITICAL"
        notes.append("VIX≥40 触发 CRITICAL 硬覆盖")
    if fut_fam and fut_fam.available:
        nq = fut_fam.data.get("NQ")
        if nq is not None and nq <= -2.0 and SEVERITY_ORDER[color] < SEVERITY_ORDER["HIGH"]:
            color = "HIGH"
            notes.append("纳指期货跌超 2% 触发 ≥HIGH 硬覆盖")

    # 数据覆盖率
    coverage = round(avail_cnt / len(families), 2) if families else 0.0
    if coverage < 0.6:
        notes.append(f"⚠️ 数据覆盖率仅 {coverage:.0%}，结论置信度下降")

    # 触发原因 & 压力源（按严重度排序，🔴 严重 / 🟠 留意，让重点跳出来）
    pressure_sources = [f.label for f in families if f.available and f.stress >= 2.0]
    ranked = sorted([f for f in families if f.available and f.stress >= 1.0],
                    key=lambda x: -x.stress)
    reasons = []
    reasons_plain = []
    for f in ranked:
        reasons.append(f"{f.label}：{f.headline}")
        if f.plain:
            dot = "🔴" if f.stress >= 2.0 else "🟠"
            reasons_plain.append(f"{dot} {f.plain}")

    # 🚨 最该注意：最严重的那一条，单独拎出来置顶
    top_alarm = ""
    if ranked and ranked[0].stress >= 2.0:
        top_alarm = "🚨 最该注意：" + ranked[0].plain

    holdings_impact = _holdings_overlay(families, holdings)

    # 顺风读数：只在绿灯(无风险)时看「环境有没有特别有利」，不喊买某只股
    tw_score, tw_reasons = _tailwind(families)
    is_tailwind = (color == "NONE") and tw_score >= 3
    headline = HEADLINE_PLAIN.get(color, "")
    can_buy = CAN_BUY.get(color, "")
    if is_tailwind:
        headline = "🟢 今晚顺风：环境有利，可以正常买"
        can_buy = "环境顺风，可以按计划买。但「环境顺风」≠「某只股就该追高」，仍按你的纪律来。"
        reasons_plain = ["🟢 " + r for r in tw_reasons]
        reasons = [f"顺风：{r}" for r in tw_reasons]

    # 数据质量保险丝：覆盖率过低时，绿/黄不给"可以买"的全清结论（缺的部分可能藏着风险）；
    # 红/橙仍可警（已有信号足以预警）。
    insufficient_data = coverage < MIN_COVERAGE
    if insufficient_data:
        is_tailwind = False
        if SEVERITY_ORDER.get(color, 0) < SEVERITY_ORDER["HIGH"]:
            headline = "❓ 数据不足：行情没拿全，今晚不给可靠结论"
            can_buy = (f"今晚只拿到约 {coverage:.0%} 的行情数据，不全——不给买入/卖出结论。"
                       "建议手动确认或保守对待（缺的那部分可能正藏着风险）。")
            reasons_plain = ["❓ 部分行情源没取到，无法确认环境是否安全。"]
            reasons = [f"数据不足：覆盖率仅 {coverage:.0%}"]
        else:
            notes.append("⚠️ 数据不全，但已有信号足以预警")

    return GateResult(
        as_of=as_of.isoformat(),
        generated_at=(now or datetime.now()).isoformat(timespec="seconds"),
        color=color,
        composite=round(composite, 3),
        can_buy=can_buy,
        headline_plain=headline,
        top_alarm=top_alarm,
        tailwind_score=tw_score,
        is_tailwind=is_tailwind,
        tailwind_reasons=tw_reasons,
        reasons=reasons,
        reasons_plain=reasons_plain,
        families=[f.to_dict() for f in families],
        holdings_impact=holdings_impact,
        pressure_sources=pressure_sources,
        coverage=coverage,
        insufficient_data=insufficient_data,
        notes=notes,
    )


# ──────────────────────────────────────────────────
# 历史回溯 / 战绩（预警准不准的自我分析）
# ──────────────────────────────────────────────────
#
# 逻辑：每天盘前记一笔预警(color/composite/原因)，第二天美股收盘后用【真实涨跌】
# 回填，自动判定这次预警是"真预警/虚惊/漏报/正常"，从而算命中率。
# 这让闸门对自己的判断负责——不是发完就忘。
#
#   预警了(🟠/🔴) + 当天真跌 → TRUE_POSITIVE  真预警(好)
#   预警了(🟠/🔴) + 当天没跌 → FALSE_ALARM    虚惊一场(警报太松)
#   没预警(🟢/🟡) + 当天真跌 → MISS           漏报(最糟，要复盘)
#   没预警(🟢/🟡) + 当天没跌 → TRUE_NEGATIVE  正常(对)

OUTCOME_CN = {
    "TRUE_POSITIVE": "✅ 真预警(警了，事后真跌)",
    "FALSE_ALARM": "🟡 虚惊(警了，但没跌)",
    "MISS": "❌ 漏报(没警，结果跌了)",
    "TRUE_NEGATIVE": "✅ 正常(没警，也没跌)",
}

# "真跌"的门槛：标普 ≤ -0.8% 或 纳指 ≤ -1.2%（盘中级别的明显下跌）
_BAD_SPY = -0.8
_BAD_NQ = -1.2


def fetch_realized_move(date_iso: str) -> dict[str, Any]:
    """某交易日美股【真实】涨跌：标普(SPY) / 纳指(QQQ) 当日收盘 vs 前一交易日。

    用于回填历史预警的"事后结果"。该日非交易日 / 数据未出 → 返回 None。
    """
    out: dict[str, Any] = {"spy_pct": None, "nq_pct": None, "settled_at": None}
    try:
        import yfinance as yf
    except Exception:
        return out
    try:
        target = date.fromisoformat(date_iso)
    except Exception:
        return out
    start = (target - timedelta(days=8)).isoformat()
    end = (target + timedelta(days=3)).isoformat()
    for key, sym in (("spy_pct", "SPY"), ("nq_pct", "QQQ")):
        try:
            h = yf.Ticker(sym).history(start=start, end=end)
            if h is None or len(h) < 2:
                continue
            rows = [(idx.date(), float(c)) for idx, c in zip(h.index, h["Close"]) if c == c]
            # 找到 target 当天及其前一交易日
            for i in range(1, len(rows)):
                if rows[i][0] == target:
                    prev = rows[i - 1][1]
                    if prev > 0:
                        out[key] = round((rows[i][1] / prev - 1.0) * 100.0, 2)
                    break
        except Exception as e:
            logger.debug("realized %s 失败: %s", sym, str(e)[:60])
    if out["spy_pct"] is not None or out["nq_pct"] is not None:
        out["settled_at"] = datetime.now().isoformat(timespec="seconds")
    return out


def score_outcome(color: str, spy_pct: float | None, nq_pct: float | None) -> str | None:
    """对照预警 vs 真实涨跌，判定这次预警的对错。数据缺 → None（未结算）。"""
    if spy_pct is None and nq_pct is None:
        return None
    warned = SEVERITY_ORDER.get(color, 0) >= SEVERITY_ORDER["HIGH"]
    spy = spy_pct if spy_pct is not None else 0.0
    nq = nq_pct if nq_pct is not None else 0.0
    bad_day = (spy <= _BAD_SPY) or (nq <= _BAD_NQ)
    if warned and bad_day:
        return "TRUE_POSITIVE"
    if warned and not bad_day:
        return "FALSE_ALARM"
    if not warned and bad_day:
        return "MISS"
    return "TRUE_NEGATIVE"


# 样本量门槛：低于这个数，战绩只供观察、不当统计依据（诚实优先）
MIN_SAMPLE = 20
# VIX-only 基准的预警阈值（Whaley 2009 的常用线）
VIX_BASELINE_WARN = 20.0


def _day_return(r: dict) -> float | None:
    """当天大盘代表涨跌：优先标普，缺则纳指。"""
    a = r.get("actual") or {}
    spy, nq = a.get("spy_pct"), a.get("nq_pct")
    return spy if spy is not None else nq


def _is_bad_day(r: dict) -> bool:
    """当天是否"真跌"（标普≤-0.8% 或 纳指≤-1.2%）。"""
    a = r.get("actual") or {}
    spy = a.get("spy_pct") if a.get("spy_pct") is not None else 0.0
    nq = a.get("nq_pct") if a.get("nq_pct") is not None else 0.0
    return (spy <= _BAD_SPY) or (nq <= _BAD_NQ)


def summarize_history(records: list[dict], min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """战绩汇总：命中率/漏报 + 按颜色分档 + 基准对照 + 样本量诚实标注。"""
    settled = [r for r in records if r.get("outcome")]
    n = len(settled)
    c = {k: 0 for k in OUTCOME_CN}
    for r in settled:
        c[r["outcome"]] = c.get(r["outcome"], 0) + 1
    tp, fa, miss, tn = c["TRUE_POSITIVE"], c["FALSE_ALARM"], c["MISS"], c["TRUE_NEGATIVE"]
    warn_total = tp + fa            # 一共发了多少次橙/红警报
    bad_total = tp + miss           # 一共有多少个真跌的日子
    precision = round(tp / warn_total * 100) if warn_total else None  # 警报里真跌占比
    recall = round(tp / bad_total * 100) if bad_total else None       # 真跌被抓到的比例
    accuracy = round((tp + tn) / n * 100) if n else None

    # ① 按颜色分档：每档事后平均涨跌 + 真跌占比（闸门有用 → 红档明显比绿档惨）
    buckets = {}
    for color in ("NONE", "LOW", "HIGH", "CRITICAL"):
        rs = [r for r in settled if r.get("color") == color]
        rets = [_day_return(r) for r in rs if _day_return(r) is not None]
        buckets[color] = {
            "n": len(rs),
            "avg_return": round(sum(rets) / len(rets), 2) if rets else None,
            "bad_rate": round(sum(1 for r in rs if _is_bad_day(r)) / len(rs) * 100) if rs else None,
        }

    # ② 基准对照：闸门必须跑赢这俩"笨办法"才算真有用
    bad_days = sum(1 for r in settled if _is_bad_day(r))
    # 基准A：永远说绿（从不预警）→ 抓真跌能力=0，但靠"多数日子没事"也有不低的 accuracy
    base_never = {
        "recall_pct": 0 if bad_days else None,
        "accuracy_pct": round((n - bad_days) / n * 100) if n else None,
    }
    # 基准B：只看 VIX（VIX≥20 就预警）—— 用闸门当晚观察到的 VIX
    vix_rs = [r for r in settled if r.get("vix") is not None]
    base_vix = None
    if vix_rs:
        vtp = vfa = vmiss = 0
        for r in vix_rs:
            warned = r["vix"] >= VIX_BASELINE_WARN
            bad = _is_bad_day(r)
            if warned and bad:
                vtp += 1
            elif warned and not bad:
                vfa += 1
            elif (not warned) and bad:
                vmiss += 1
        vwarn, vbad = vtp + vfa, vtp + vmiss
        base_vix = {
            "n": len(vix_rs),
            "precision_pct": round(vtp / vwarn * 100) if vwarn else None,
            "recall_pct": round(vtp / vbad * 100) if vbad else None,
            "miss": vmiss,
        }

    # ③ 顺风验证：说「顺风」的那些天，事后真涨了吗（与防守对称的另一半）
    tw_days = [r for r in settled if r.get("is_tailwind")]
    tw_rets = [_day_return(r) for r in tw_days if _day_return(r) is not None]
    tailwind = {
        "n": len(tw_days),
        "rose": sum(1 for r in tw_days if (_day_return(r) or 0) > 0),   # 事后真涨
        "backfired": sum(1 for r in tw_days if _is_bad_day(r)),         # 说顺风却反而大跌(打脸)
        "avg_return": round(sum(tw_rets) / len(tw_rets), 2) if tw_rets else None,
    }

    return {
        "settled_days": n,
        "warnings_issued": warn_total,
        "true_positive": tp, "false_alarm": fa, "miss": miss, "true_negative": tn,
        "precision_pct": precision,   # 发警报的准度
        "recall_pct": recall,         # 抓真跌的能力
        "accuracy_pct": accuracy,
        "bad_days": bad_days,
        "enough_sample": n >= min_sample,   # 样本够不够（不够则 UI 灰显、仅供观察）
        "min_sample": min_sample,
        "color_buckets": buckets,
        "baseline_never_warn": base_never,
        "baseline_vix_only": base_vix,
        "tailwind": tailwind,
    }


# ──────────────────────────────────────────────────
# CLI（本地手测）
# ──────────────────────────────────────────────────

def _main():
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(f"\n🚦 美股盘前风险闸门 — {datetime.now():%Y-%m-%d %H:%M}\n")
    print("拉行情中（期货/利率/VIX/巨头/板块/海外）...")
    res = compute_gate()
    print(f"\n{res.icon} {res.color}  综合压力 {res.composite:.2f}/3  覆盖率 {res.coverage:.0%}")
    print(f"今晚能不能买：{res.can_buy}\n")
    if res.pressure_sources:
        print("压力源：" + "、".join(res.pressure_sources))
    print("\n触发明细：")
    for r in res.reasons or ["（各族均平稳）"]:
        print(f"  • {r}")
    if res.notes:
        print("\n备注：")
        for n in res.notes:
            print(f"  • {n}")
    print("\n（完整 JSON 见 --json）")
    import sys
    if "--json" in sys.argv:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
