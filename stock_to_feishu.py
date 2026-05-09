"""
把股票研究结果写入飞书「股票研究 Watchlist」表。

设计：研究流程是 Claude Code 在对话里完成（WebSearch + 招股书阅读 + 同业对比），
最后调用本脚本把结构化结果落到飞书表，方便长期跟踪。

用法 1（命令行 JSON 文件）：
  python3 stock_to_feishu.py upsert /path/to/record.json

用法 2（命令行批量）：
  python3 stock_to_feishu.py upsert-many /path/to/records.json

用法 3（Python 模块导入，推荐 Claude Code 直接用）：
  from stock_to_feishu import upsert_stock
  upsert_stock({...})

JSON schema：
{
  "股票名称": "长进光子",
  "代码": "787635",
  "市场": "A股·科创板",   # 单选
  "主营业务": "...",
  "行业归类": "...",
  "AI关联度": "强（直接受益）",   # 单选：极强/强/中/弱/无
  "AI关联逻辑": "...",
  "当前市值": "市值 X 亿/上市前",
  "最近季度业绩": "...",
  "研究结论": "...",
  "关键风险": "...",
  "可比公司": "...",
  "跟踪节奏": "重大事件触发",   # 单选
  "研究状态": "✅ 已深研",        # 单选
  "数据来源": "..."
}
"""
import sys
import os
import json
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN  # noqa: E402

TABLE_ID = "tblaEuCPOlXBlSvP"  # 股票研究 Watchlist
BASE_URL = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{TABLE_ID}"


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def ts_today():
    return int(datetime.strptime(datetime.now().strftime("%Y-%m-%d"),
                                 "%Y-%m-%d").timestamp() * 1000)


def find_record_by_code(token, code):
    """按代码查重，避免重复写入。"""
    if not code:
        return None
    url = f"{BASE_URL}/records/search"
    body = {
        "filter": {
            "conjunction": "and",
            "conditions": [{
                "field_name": "代码",
                "operator": "is",
                "value": [code],
            }],
        },
    }
    resp = requests.post(url, headers=headers(token), json=body)
    items = resp.json().get("data", {}).get("items", [])
    return items[0]["record_id"] if items else None


def upsert_stock(rec, token=None):
    """写入一条股票研究记录（按代码去重 → upsert 行为）。

    自动填充 4 个可信度字段（除非 rec 中已显式指定）：
    - 数据快照时间：默认今天
    - 数据可信度：默认「🟡 中（权威媒体单源）」
    - 信息构成：默认提示文本
    - 双源验证：默认「⚠️ 单源（仅 1 个来源）」
    """
    if token is None:
        token = feishu_token()
    code = rec.get("代码", "")
    fields = {k: v for k, v in rec.items() if v not in ("", None)}
    fields["录入日期"] = fields.get("录入日期", ts_today())
    fields["最近更新"] = ts_today()

    # 4 个可信度字段（默认值，研究时应主动覆盖）
    fields.setdefault("数据快照时间", ts_today())
    fields.setdefault("数据可信度", "🟡 中（权威媒体单源）")
    fields.setdefault("双源验证", "⚠️ 单源（仅 1 个来源）")
    fields.setdefault("信息构成", (
        "实时事实（来自 WebSearch 当日抓取）：见「最近季度业绩」「关键风险」中的具体数字\n"
        "历史事实（公司基本盘）：见「主营业务」「行业归类」\n"
        "我的推断（基于产业逻辑）：见「研究结论」中的判断性语句\n"
        "训练数据已知：公司基本介绍、行业格局基础（可能滞后于今日）"
    ))

    existing_id = find_record_by_code(token, code) if code else None
    if existing_id:
        url = f"{BASE_URL}/records/{existing_id}"
        resp = requests.put(url, headers=headers(token), json={"fields": fields})
        action = "更新"
    else:
        url = f"{BASE_URL}/records"
        resp = requests.post(url, headers=headers(token), json={"fields": fields})
        action = "新增"

    data = resp.json()
    if data.get("code") == 0:
        rid = data["data"]["record"]["record_id"]
        print(f"  · {action}成功 [{rec.get('股票名称', '?')} {code}] → {rid}")
        return rid
    print(f"  ! {action}失败 [{rec.get('股票名称', '?')}]: {data.get('msg')}")
    return None


def upsert_many(records, token=None):
    if token is None:
        token = feishu_token()
    rids = []
    for r in records:
        rid = upsert_stock(r, token=token)
        rids.append(rid)
    return rids


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    path = sys.argv[2]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if cmd == "upsert":
        upsert_stock(data)
    elif cmd == "upsert-many":
        upsert_many(data)
    else:
        print(f"未知命令：{cmd}")
        sys.exit(1)
    print(f"\n查看：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={TABLE_ID}")


if __name__ == "__main__":
    main()
