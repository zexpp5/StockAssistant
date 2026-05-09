"""每日 picks 历史归档（GitHub commit）—— 累积公开可证明的预测记录。

设计哲学：
  权威性 = 时间 × 公开化 × 不可篡改性。
  把每天的 picks 推到一个**只 append 不删除**的 git repo，git 历史 +
  GitHub 服务器时间戳即为"我提前 X 个月看好了 NVDA"的证据链。

工作流：
  1. 拉今日 picks（+ 当前快照里的所有审查结果）
  2. 写到 archive/{YYYY-MM-DD}/picks.csv + audit_summary.md
  3. git add / commit / push（用户预先配好的 GitHub remote）

不在本脚本做的事：
  - 不创建 repo（用户自己 git init + 设 remote）
  - 不配 SSH key（用户自己 ssh-add）
  - 不写敏感数据（凭证、私人备注）

输出文件结构：
  archive/
    2026-05-09/
      picks.csv             ← 当日 picks（时间戳锁定）
      audit_summary.md      ← 当日审查报告
      portfolio_v6.json     ← 当日 v6 优化输出（如有）
    2026-05-10/
      ...
    INDEX.md                ← 自动生成的索引

CLI:
  # 默认：归档今天 + git commit（不 push，用户决定）
  python3 -m stock_research.jobs.archive_picks

  # 指定日期
  python3 -m stock_research.jobs.archive_picks --date 2026-05-09

  # 自动 push
  python3 -m stock_research.jobs.archive_picks --push

  # 只生成文件不 git
  python3 -m stock_research.jobs.archive_picks --no-git
"""
from __future__ import annotations
import argparse
import csv
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .. import config
from ..adapters import feishu, store

logger = logging.getLogger("stock_research.jobs.archive_picks")

ARCHIVE_DIR = _REPO_ROOT / "archive"


# ─────────── 工具 ───────────

def _normalize(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list) and v:
        first = v[0]
        return first.get("text", "") if isinstance(first, dict) else str(first)
    if isinstance(v, dict):
        return v.get("text", "") or v.get("name", "")
    return str(v)


def _get_today_picks(target_date: datetime) -> list[dict]:
    """拉指定日期入选的 picks。"""
    picks = feishu.fetch_picks()
    out = []
    target_ts = datetime.combine(target_date.date(), datetime.min.time()).timestamp() * 1000
    next_ts = target_ts + 86400 * 1000
    for p in picks:
        f = p.get("fields", {})
        pd = f.get("入选日期")
        if pd and target_ts <= pd < next_ts:
            out.append(f)
    return out


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """run git command in cwd, return (returncode, output)."""
    try:
        r = subprocess.run(["git"] + args, cwd=str(cwd),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


# ─────────── 归档 ───────────

def archive_day(target_date: datetime) -> Path:
    """归档单天：写 picks.csv + audit_summary.md + portfolio_v6.json（如有）。

    返回归档目录路径。
    """
    date_str = target_date.strftime("%Y-%m-%d")
    day_dir = ARCHIVE_DIR / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    # 1. picks.csv
    picks = _get_today_picks(target_date)
    csv_path = day_dir / "picks.csv"
    fields = ["代码", "股票名称", "市场", "入选评分", "综合得分",
              "入选时价格", "入选时PEG", "入选时远期PE", "入选时1Y%",
              "AI关联度", "主题分类", "入选理由", "关键看点（催化剂）", "风险提示"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for p in picks:
            w.writerow([_normalize(p.get(k)) for k in fields])
    print(f"  ✅ picks.csv ({len(picks)} 条)")

    # 2. audit_summary.md（来自最新 picks_audit + factor_ic 快照）
    audit = store.load_latest_json(config.AUDIT_DIR, "picks_audit")
    factor_ic = store.load_latest_json(config.AUDIT_DIR, "factor_ic")
    md_path = day_dir / "audit_summary.md"
    md_path.write_text(_render_audit_md(picks, audit, factor_ic, target_date),
                       encoding="utf-8")
    print(f"  ✅ audit_summary.md")

    # 3. portfolio_v6.json（如有最新优化结果）
    optimize_dir = config.AUDIT_DIR.parent / "optimize"
    if optimize_dir.exists():
        plan = store.load_latest_json(optimize_dir, "plan_v6")
        if plan:
            (day_dir / "portfolio_v6.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"  ✅ portfolio_v6.json")

    return day_dir


def _render_audit_md(picks: list[dict], audit: dict | None,
                     factor_ic: dict | None, target_date: datetime) -> str:
    """渲染 audit_summary.md。"""
    date_str = target_date.strftime("%Y-%m-%d")
    lines = [
        f"# StockAssistant 每日审查 · {date_str}",
        "",
        f"_系统版本：v6（5 因子 + Markowitz + 中性化 + ADV + 成本）_",
        f"_当日 picks: {len(picks)} 条 · 不构成投资建议_",
        "",
    ]

    # 评级分布
    rating_counts: dict[str, int] = {"⭐⭐⭐": 0, "⭐⭐": 0, "⭐": 0}
    for p in picks:
        r = _normalize(p.get("入选评分"))
        for k in rating_counts:
            if k in r:
                rating_counts[k] += 1
                break
    lines += [
        "## 评级分布",
        "",
        f"| ⭐⭐⭐ 强烈推荐 | ⭐⭐ 推荐 | ⭐ 关注 |",
        f"|---|---|---|",
        f"| {rating_counts['⭐⭐⭐']} | {rating_counts['⭐⭐']} | {rating_counts['⭐']} |",
        "",
    ]

    # 主题集中度（来自 picks_audit）
    if audit and audit.get("theme_concentration", {}).get("status") == "ok":
        tc = audit["theme_concentration"]
        lines += [
            "## 主题集中度（Risk Parity）",
            "",
            f"{tc['verdict']}",
            "",
            "| 主题 | 数量 | 占比 |",
            "|---|---|---|",
        ]
        for d in tc["distribution"][:10]:
            lines.append(f"| {d['theme']} | {d['n']} | {d['pct']:.1f}% |")
        lines.append("")

    # 估值警告
    if audit and audit.get("valuation_sanity", {}).get("warn_count", 0) > 0:
        vs = audit["valuation_sanity"]
        lines += [
            f"## ⚠️ 估值警告（{vs['warn_count']} 只 ⭐⭐⭐ 推荐）",
            "",
        ]
        for w in vs["warnings"]:
            flags = " / ".join(w["flags"])
            lines.append(f"- **{w['name']}** ({w['code']}): {flags}")
        lines.append("")

    # 因子 IC 状态
    if factor_ic and factor_ic.get("factors"):
        lines += [
            "## 因子治理（IC 监测）",
            "",
            "| 因子 | 状态 | 平均 IC | IR |",
            "|---|---|---|---|",
        ]
        for fname, info in factor_ic["factors"].items():
            a = info.get("alert", {})
            s = info.get("summary", {})
            lines.append(f"| {fname} | {a.get('icon', '?')} {a.get('status', '')} | "
                         f"{s.get('mean_ic', 0):+.3f} | {s.get('ic_ir', 0):+.2f} |")
        lines.append("")

    # 限制声明
    lines += [
        "---",
        "",
        "## 限制声明",
        "",
        "本归档是公开的研究记录，**不构成投资建议**。",
        "数据基于 SEC EDGAR / 港交所 / yfinance / akshare 等公开源。",
        "模型缺陷与学术依据见 [docs/MODEL_CARD.md](../../docs/MODEL_CARD.md)。",
        "",
        f"_archived at {datetime.now().isoformat(timespec='seconds')}_",
    ]
    return "\n".join(lines)


def update_index() -> Path:
    """生成 archive/INDEX.md，列出所有归档日期 + 当日 picks 数。"""
    if not ARCHIVE_DIR.exists():
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    days = sorted([d for d in ARCHIVE_DIR.iterdir()
                   if d.is_dir() and len(d.name) == 10 and d.name[4] == "-"],
                  reverse=True)
    lines = [
        "# StockAssistant 归档索引",
        "",
        f"_自动生成 · 共 {len(days)} 天归档_",
        f"_最后更新：{datetime.now().isoformat(timespec='seconds')}_",
        "",
        "## 公开承诺",
        "",
        "- ✅ **append-only**：归档不删除、不修改",
        "- ✅ **git 时间戳**：commit hash + GitHub 服务器时间签名",
        "- ✅ **完整披露**：命中 / 漏报 / 误报全部留痕（见每月 letter）",
        "",
        "## 归档列表",
        "",
        "| 日期 | picks 数 | 文件 |",
        "|---|---|---|",
    ]
    for d in days:
        csv_path = d / "picks.csv"
        n_picks = 0
        if csv_path.exists():
            with open(csv_path, encoding="utf-8") as f:
                # 用 csv reader 而不是逐行 - 避免字段内嵌换行被多算
                n_picks = max(0, sum(1 for _ in csv.reader(f)) - 1)
        files = ", ".join(p.name for p in d.iterdir() if p.is_file())
        lines.append(f"| [{d.name}](./{d.name}/) | {n_picks} | {files} |")

    index = ARCHIVE_DIR / "INDEX.md"
    index.write_text("\n".join(lines), encoding="utf-8")
    return index


def git_commit(target_date: datetime, push: bool = False) -> bool:
    """git add archive/<date>/* + INDEX.md，commit。"""
    repo_dir = _REPO_ROOT
    if not (repo_dir / ".git").exists():
        print(f"  ⚠️ {repo_dir} 不是 git repo，跳过 commit")
        return False

    rc, _ = _git(["add", f"archive/{target_date.strftime('%Y-%m-%d')}/", "archive/INDEX.md"], repo_dir)
    if rc != 0:
        print(f"  ⚠️ git add 失败")
        return False

    msg = f"archive: {target_date.strftime('%Y-%m-%d')} picks (auto-commit)"
    rc, out = _git(["commit", "-m", msg], repo_dir)
    if rc == 0:
        print(f"  ✅ git commit: {msg}")
    elif "nothing to commit" in out:
        print(f"  · 无变化跳过 commit")
        return True
    else:
        print(f"  ⚠️ git commit 失败: {out[:200]}")
        return False

    if push:
        rc, out = _git(["push"], repo_dir)
        if rc == 0:
            print(f"  ✅ git push 完成")
        else:
            print(f"  ⚠️ git push 失败（可能是 remote 未配）: {out[:200]}")
    else:
        print(f"  · 未 push（用 --push 或手动 git push）")
    return True


# ─────────── 主流程 ───────────

def run(target_date: datetime | None = None,
        push: bool = False, do_git: bool = True) -> dict:
    if target_date is None:
        target_date = datetime.now()

    print(f"\n{'='*60}")
    print(f"  📁 归档每日 picks · {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*60}\n")

    print(f"[1/3] 写归档文件...")
    day_dir = archive_day(target_date)
    print(f"  路径: {day_dir}")

    print(f"\n[2/3] 更新 INDEX.md...")
    index = update_index()
    print(f"  ✅ {index}")

    if do_git:
        print(f"\n[3/3] git commit{' + push' if push else ''}...")
        git_commit(target_date, push=push)
    else:
        print(f"\n[3/3] 跳过 git（用户指定 --no-git）")

    return {"date": target_date.strftime("%Y-%m-%d"), "archive_dir": str(day_dir)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    p = argparse.ArgumentParser(description="每日 picks 公开归档")
    p.add_argument("--date", help="格式 YYYY-MM-DD；默认今天")
    p.add_argument("--push", action="store_true", help="commit 后自动 push")
    p.add_argument("--no-git", action="store_true", help="只生成文件不 git")
    args = p.parse_args()

    target = None
    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"❌ --date 格式错误（应 YYYY-MM-DD），得到 {args.date}")
            return 1

    run(target_date=target, push=args.push, do_git=not args.no_git)
    return 0


if __name__ == "__main__":
    sys.exit(main())
