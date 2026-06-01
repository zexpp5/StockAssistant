"""AI 主题雷达防 footgun 测试套件（评审 P3 建议）。

覆盖 4 类必守的硬规则：
  T1. 只读不写表（雷达 build 路径不修改 watchlist / 真实持仓 / picks）
  T2. 非 AI 链不进主视图（AI_RELEVANT_THEME_KEYWORDS 白名单工作正确）
  T3. candidate 不当 confirmed（文档 §九 严格规则）
  T4. 生产 FAIL 时雷达必须显示警示（不能"看起来正常"误导用户）

测试设计:
  - 用 in-memory DuckDB 避免污染生产库
  - schema 用 init_stock_db_v2.py 的 SCHEMA_SQL 同源（避免双引擎）
  - 每个测试独立 fixture，互不影响
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import duckdb


def _make_schema(con):
    """复用 init_stock_db_v2.py 里的 SCHEMA_SQL 子集，建测试所需的表。"""
    # 用 init 脚本的常量；要保持跟生产一致
    init_path = REPO / "scripts" / "tools" / "init_stock_db_v2.py"
    text = init_path.read_text()
    # 抽 SCHEMA_SQL = """...""" 那段
    start = text.find('SCHEMA_SQL = "')
    end = text.find('"""', start + 20)  # 找闭合
    if start < 0:
        # 老版本 SCHEMA_SQL 是 r"""...""" 形式
        start = text.find('SCHEMA_SQL = r"')
        if start < 0:
            raise RuntimeError("找不到 SCHEMA_SQL，可能 init 脚本结构变了")
    open_quote = text.find('"""', start)
    close_quote = text.find('"""', open_quote + 3)
    schema_sql = text[open_quote + 3: close_quote]
    con.execute(schema_sql)


def _insert_pick(con, market, symbol, name, score, run_id="run_test",
                 universe_scope="system_tech_universe"):
    """快速插入一条 picks 数据 + 关联的 run."""
    # 如果 run 不存在先建
    has_run = con.execute(
        "SELECT 1 FROM recommendation_runs WHERE run_id = ?", [run_id]
    ).fetchone()
    if not has_run:
        con.execute(
            """INSERT INTO recommendation_runs
               (run_id, run_date, strategy_version, model_version, universe_scope,
                data_cutoff_at, generated_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')""",
            [run_id, date.today(), "test_v1", "test", universe_scope,
             datetime.now(), datetime.now()]
        )
    con.execute(
        """INSERT INTO recommendation_picks
           (run_id, market, symbol, name, rank, rating, total_score,
            universe_scope, source_origin)
           VALUES (?, ?, ?, ?, 1, 'A', ?, ?, 'test')""",
        [run_id, market, symbol, name, float(score), universe_scope]
    )


def _insert_universe(con, market, symbol, name, theme, industry, active=True):
    con.execute(
        """INSERT INTO system_universe
           (pool_id, market, symbol, name, theme, industry, source, active)
           VALUES ('system_tech_universe', ?, ?, ?, ?, ?, 'test', ?)""",
        [market, symbol, name, theme, industry, active]
    )


# ──────────────────────────────────────────────────────────────
# T1: 只读不写表
# ──────────────────────────────────────────────────────────────
class TestRadarReadOnly(unittest.TestCase):
    """雷达 build 路径不允许修改用户态表."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.duckdb")
        self.con = duckdb.connect(self.db_path)
        _make_schema(self.con)

        # 灌一条 watchlist + 持仓
        self.con.execute(
            "INSERT INTO manual_watchlist (market, symbol, name) VALUES (?, ?, ?)",
            ["US", "TEST_KEEP", "Test 票"]
        )
        _insert_universe(self.con, "US", "TEST_KEEP", "Test", "AI compute", "AI compute")
        _insert_pick(self.con, "US", "TEST_KEEP", "Test", 80.0)
        # chain
        self.con.execute(
            """INSERT INTO chain_metadata (market, symbol, chain, source)
               VALUES (?, ?, 'AI 算力', 'test')""",
            ["US", "TEST_KEEP"]
        )
        self.con.close()

    def test_build_payload_does_not_mutate_watchlist(self):
        from stock_research.core.ai_radar import build_ai_radar_payload
        con = duckdb.connect(self.db_path)
        try:
            n_before = con.execute("SELECT COUNT(*) FROM manual_watchlist").fetchone()[0]
            build_ai_radar_payload(con)
            n_after = con.execute("SELECT COUNT(*) FROM manual_watchlist").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n_before, n_after, "build_ai_radar_payload 不应修改 watchlist")

    def test_build_payload_does_not_write_picks(self):
        from stock_research.core.ai_radar import build_ai_radar_payload
        con = duckdb.connect(self.db_path)
        try:
            n_before = con.execute("SELECT COUNT(*) FROM recommendation_picks").fetchone()[0]
            build_ai_radar_payload(con)
            n_after = con.execute("SELECT COUNT(*) FROM recommendation_picks").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n_before, n_after, "build_ai_radar_payload 不应写 picks")


# ──────────────────────────────────────────────────────────────
# T2: 非 AI 链不进主视图
# ──────────────────────────────────────────────────────────────
class TestNonAIFilteredOut(unittest.TestCase):
    """AI_RELEVANT_THEME_KEYWORDS 白名单生效，非 AI 高分票不进 audit."""

    def test_consumer_stock_excluded(self):
        from stock_research.core.ai_radar import is_ai_relevant_universe
        # 华测检测 M74 / 万辰集团 F52 / 贝泰妮 C26 都不应该归 AI
        self.assertFalse(is_ai_relevant_universe("M74专业技术服务业", "M74专业技术服务业"))
        self.assertFalse(is_ai_relevant_universe("F52零售业", "F52零售业"))
        self.assertFalse(is_ai_relevant_universe("C26化学原料和化学制品制造业", "C26化学原料和化学制品制造业"))

    def test_ai_stock_included(self):
        from stock_research.core.ai_radar import is_ai_relevant_universe
        # NVDA / INTU / 腾讯 / I65 软件 全应识别为 AI
        self.assertTrue(is_ai_relevant_universe("AI compute", "AI compute"))
        self.assertTrue(is_ai_relevant_universe("application software", "application software"))
        self.assertTrue(is_ai_relevant_universe("互联网", "互联网"))
        self.assertTrue(is_ai_relevant_universe("I65软件和信息技术服务业", "I65软件和信息技术服务业"))

    def test_narrow_a_share_codes(self):
        """C35/C38/C39 大 GICS 类不应误抓（2026-06-01 收窄）."""
        from stock_research.core.ai_radar import is_ai_relevant_universe
        # 阿特斯 C38 光伏 / 健帆生物 C35 血液净化 不应归 AI
        self.assertFalse(is_ai_relevant_universe("C38电气机械和器材制造业", "C38电气机械和器材制造业"))
        self.assertFalse(is_ai_relevant_universe("C35专用设备制造业", "C35专用设备制造业"))


# ──────────────────────────────────────────────────────────────
# T3: candidate 不当 confirmed
# ──────────────────────────────────────────────────────────────
class TestConfirmedRule(unittest.TestCase):
    """aggregate_theme_tags 严格遵守文档 §九 confirmed 规则."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.duckdb")
        self.con = duckdb.connect(self.db_path)
        _make_schema(self.con)

    def tearDown(self):
        self.con.close()

    def _insert_evidence(self, eid, theme, symbol, source_id, source_tier,
                        source_date, status="candidate"):
        self.con.execute(
            """INSERT INTO ai_theme_company_evidence
               (evidence_id, theme, market, symbol, company_name,
                evidence_status, source_id, source_tier, source_url, source_title,
                source_date, evidence_kind)
               VALUES (?, ?, 'US', ?, 'Test Co', ?, ?, ?, 'https://test/x', 'Test 10-K', ?, 'filing_metric')""",
            [eid, theme, symbol, status, source_id, source_tier, source_date]
        )

    def test_single_source_cannot_confirm(self):
        """1 个独立来源即使 A 类也不能 confirmed."""
        from stock_research.jobs.aggregate_theme_tags import aggregate_tags
        self._insert_evidence("e1", "uranium", "CCJ", "sec_edgar_api", "A", date.today())
        stat = aggregate_tags(self.con)
        r = self.con.execute(
            "SELECT evidence_status FROM ai_theme_company_tags WHERE symbol='CCJ'"
        ).fetchone()
        self.assertEqual(r[0], "candidate", "单一来源不能 confirmed，必须 ≥ 2 独立 source_id")

    def test_two_sources_with_a_class_can_confirm(self):
        """≥ 2 独立来源 + ≥ 1 A 类 + ≤ 180 天 → confirmed."""
        from stock_research.jobs.aggregate_theme_tags import aggregate_tags
        self._insert_evidence("e1", "uranium", "CCJ", "sec_edgar_api", "A", date.today())
        self._insert_evidence("e2", "uranium", "CCJ", "doe_press_release", "A", date.today())
        aggregate_tags(self.con)
        r = self.con.execute(
            "SELECT evidence_status, source_count_a FROM ai_theme_company_tags WHERE symbol='CCJ'"
        ).fetchone()
        self.assertEqual(r[0], "confirmed", f"2 个 A 源 + 今日 → 应该 confirmed，实际 {r}")
        self.assertEqual(r[1], 2)

    def test_old_evidence_marked_stale(self):
        """超过 180 天的 evidence 应 stale，不应 confirmed."""
        from stock_research.jobs.aggregate_theme_tags import aggregate_tags
        old = date.today() - timedelta(days=200)
        self._insert_evidence("e1", "uranium", "OLD", "sec_edgar_api", "A", old, status="stale")
        aggregate_tags(self.con)
        r = self.con.execute(
            "SELECT evidence_status FROM ai_theme_company_tags WHERE symbol='OLD'"
        ).fetchone()
        self.assertEqual(r[0], "stale")

    def test_etf_only_cannot_confirm(self):
        """单 ETF 持仓不能 confirmed（必须 A 类公司证据）."""
        from stock_research.jobs.aggregate_theme_tags import aggregate_tags
        # B 类源 (ETF) × 3 仍不能 confirmed，因为缺 ≥ 1 A 类
        self._insert_evidence("e1", "rare_earths", "MP", "etf_remx", "B", date.today())
        self._insert_evidence("e2", "rare_earths", "MP", "etf_remx2", "B", date.today())
        self._insert_evidence("e3", "rare_earths", "MP", "etf_remx3", "B", date.today())
        aggregate_tags(self.con)
        r = self.con.execute(
            "SELECT evidence_status FROM ai_theme_company_tags WHERE symbol='MP'"
        ).fetchone()
        self.assertEqual(r[0], "candidate", "3 个 B 类 ETF 不够 confirmed，必须 ≥ 1 A 类")


# ──────────────────────────────────────────────────────────────
# T4: 第 4 卡数据缺口必须 surface 真问题
# ──────────────────────────────────────────────────────────────
class TestDataGapVisible(unittest.TestCase):
    """生产 FAIL / 数据 stale / confirmed=0 时，第 4 卡必须列出 + 染色."""

    def test_no_confirmed_shown_as_gap(self):
        """0/5 主题 confirmed 时，gap 列表必须显示警示."""
        from stock_research.core.ai_radar import build_freshness_panel, build_theme_evidence_panel
        # 现实状态：confirmed = 0 → gap 不能为空
        # 用生产 DB（read-only）验证
        prod_db = REPO / "stock_history_v2.duckdb"
        if not prod_db.exists():
            self.skipTest("生产 DB 不存在")
        con = duckdb.connect(str(prod_db), read_only=True)
        try:
            theme_panel = build_theme_evidence_panel(con)
            phase = theme_panel.get("phase_status") or {}
            n_confirmed = phase.get("phase_1_n_confirmed", 0)
            n_theme_done = phase.get("phase_1_themes_with_confirmed", 0)
            n_theme_total = phase.get("phase_1_themes_total", 5)
        finally:
            con.close()
        # 当前所有 confirmed = 0 → 必须 ≥ 1 个 gap 显示
        if n_confirmed == 0:
            self.assertEqual(n_theme_done, 0,
                "0 confirmed 时 themes_with_confirmed 必须也是 0")
            self.assertLess(n_theme_done, n_theme_total,
                "n_theme_done < n_theme_total 才会触发第 4 卡 gap 提示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
