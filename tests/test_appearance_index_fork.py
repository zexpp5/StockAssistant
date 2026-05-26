"""F17: 验证「连 N 日」算法在「跌出又回来」case 下的正确性。

测试构造 3 个 run，每只票走不同轨迹：
  ALWAYS    在 run1/2/3 都在  → consecutive=3, count=3
  NEW_TODAY 只在 run3        → consecutive=1, count=1, 🆕
  DROPPED   在 run1/2 在,run3 不在 → 不出现在 appearance_index (dropouts 单独算)
  RETURNED  在 run1/3 在,run2 不在 → consecutive=1, count=2（最关键 case：分叉）
  CONSEC2   在 run2/3 在,run1 不在 → consecutive=2, count=2

跑临时 DuckDB 做隔离测试，不污染生产库。
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "pipeline"))


def test_fork_case_in_appearance_index():
    import duckdb

    # 用临时目录而不是 NamedTemporaryFile（后者会创建空文件，DuckDB 拒绝当 db 用）
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test_appearance.duckdb")

    try:
        con = duckdb.connect(db_path)
        # 构造 schema (跟生产一致)
        con.execute("""
            CREATE TABLE recommendation_runs (
                run_id VARCHAR PRIMARY KEY,
                run_date DATE,
                strategy_version VARCHAR,
                model_version VARCHAR,
                universe_scope VARCHAR,
                data_cutoff_at TIMESTAMP,
                generated_at TIMESTAMP,
                status VARCHAR,
                notes VARCHAR
            )
        """)
        con.execute("""
            CREATE TABLE recommendation_picks (
                run_id VARCHAR,
                market VARCHAR,
                symbol VARCHAR,
                name VARCHAR,
                rank INTEGER,
                rating VARCHAR,
                signal VARCHAR,
                total_score DOUBLE,
                factor_scores_json VARCHAR,
                recommendation_reason VARCHAR,
                risk_flags_json VARCHAR,
                entry_price DOUBLE,
                entry_currency VARCHAR,
                universe_scope VARCHAR,
                source_origin VARCHAR,
                created_at TIMESTAMP
            )
        """)
        # 3 个 run
        runs = [
            ("run1", "2026-05-24", "2026-05-24 12:00:00"),
            ("run2", "2026-05-25", "2026-05-25 12:00:00"),
            ("run3", "2026-05-26", "2026-05-26 12:00:00"),
        ]
        for run_id, run_date, gen_at in runs:
            con.execute(
                "INSERT INTO recommendation_runs (run_id, run_date, generated_at) VALUES (?, ?, ?)",
                [run_id, run_date, gen_at],
            )

        # ticker → list of (run_id, rank, score)
        scenarios = {
            "ALWAYS":    [("run1", 5, 80.0), ("run2", 4, 82.0), ("run3", 3, 85.0)],
            "NEW_TODAY": [("run3", 8, 78.0)],
            "DROPPED":   [("run1", 6, 76.0), ("run2", 7, 75.0)],
            "RETURNED":  [("run1", 10, 70.0), ("run3", 9, 71.0)],  # 中间 run2 跌出
            "CONSEC2":   [("run2", 12, 72.0), ("run3", 11, 73.0)],
        }
        for symbol, picks in scenarios.items():
            for run_id, rank, score in picks:
                con.execute(
                    "INSERT INTO recommendation_picks (run_id, symbol, rank, total_score, rating) "
                    "VALUES (?, ?, ?, ?, 'strong_buy')",
                    [run_id, symbol, rank, score],
                )
        con.close()

        # monkey-patch _duckdb_path
        import importlib.util
        spec = importlib.util.spec_from_file_location("bsd", str(REPO / "scripts" / "pipeline" / "build_stock_dashboard_html.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._duckdb_path = lambda: db_path

        # 跑算法
        idx = mod._build_appearance_index()
        total_runs = idx["_meta"]["total_runs"]
        assert total_runs == 3, f"expected 3 runs, got {total_runs}"

        def fail(msg): raise AssertionError(msg)

        # ALWAYS: count=3, consecutive=3
        a = idx["ALWAYS"]
        if not (a["count"] == 3 and a["consecutive"] == 3):
            fail(f"ALWAYS expected count=3,consec=3, got {a}")

        # NEW_TODAY: count=1, consecutive=1, first_seen 是 2026-05-26
        n = idx["NEW_TODAY"]
        if not (n["count"] == 1 and n["consecutive"] == 1 and n["first_seen_date"] == "2026-05-26"):
            fail(f"NEW_TODAY expected count=1,consec=1,first=05-26, got {n}")

        # DROPPED 应该有记录（count=2）但 consecutive != total_runs
        # 实际上 _build_appearance_index 只返回出现过的，DROPPED 出现在 run1/2 但 run3 没—— consecutive 应该是 0
        d = idx["DROPPED"]
        if not (d["count"] == 2 and d["consecutive"] == 0):
            fail(f"DROPPED expected count=2,consec=0, got {d}")

        # 🔥 关键 case: RETURNED — 在 run1 和 run3 在,run2 跌出
        # consecutive 从 run3 向前数: run3 在(+1), run2 应该是 rn=2 但 RETURNED 不在 run2 →
        # 算法 expected=3 看 run_ranks 第一个 == 3 → +1 → expected=2;
        # run_ranks 下一个是 1 (因为 RETURNED 在 run1)，1 != 2 → break
        # 所以 consecutive=1, count=2 ✓
        r = idx["RETURNED"]
        if not (r["count"] == 2 and r["consecutive"] == 1):
            fail(f"RETURNED expected count=2,consec=1（关键分叉 case）, got {r}")

        # CONSEC2: run2 和 run3 都在, run1 不在
        # run_ranks 从 desc = [3, 2], expected=3 → match (+1, expected=2) → match (+1, expected=1)
        # consecutive=2, count=2
        c = idx["CONSEC2"]
        if not (c["count"] == 2 and c["consecutive"] == 2):
            fail(f"CONSEC2 expected count=2,consec=2, got {c}")

        print("✅ 全部 5 个 case 通过：")
        print(f"  ALWAYS    count={a['count']} consec={a['consecutive']} (期望 3/3)")
        print(f"  NEW_TODAY count={n['count']} consec={n['consecutive']} first={n['first_seen_date']} (期望 1/1/05-26)")
        print(f"  DROPPED   count={d['count']} consec={d['consecutive']} (期望 2/0)")
        print(f"  RETURNED  count={r['count']} consec={r['consecutive']} ⭐ 关键分叉 (期望 2/1)")
        print(f"  CONSEC2   count={c['count']} consec={c['consecutive']} (期望 2/2)")

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    test_fork_case_in_appearance_index()
