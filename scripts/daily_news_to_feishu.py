"""
每日新闻 → 飞书（国际 + 国内热点新闻表）
─────────────────────────────────────────
数据源: 财联社 (akshare.stock_news_main_cx) - 100 条主题新闻，已中文

流程:
  1. 拉财联社 100 条主题新闻
  2. 按关键词 + tag 分类 → 国际 vs 国内
  3. 删除两张表所有历史记录
  4. 写入今天的新闻

飞书表:
  · 国际热点新闻 (tblhKE2rBoOGe82j)
  · 国内热点新闻 (tblPRTqw6qRdcxL6)

cron 推荐: daily_refresh.sh 中加一步，每天早 7:30 跑
"""
import sys
import os
import time
import requests
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from feishu_auth import feishu_token, FEISHU_APP_TOKEN

import akshare as ak

INTL_TABLE = "tblhKE2rBoOGe82j"  # 国际热点新闻
DOMESTIC_TABLE = "tblPRTqw6qRdcxL6"  # 国内热点新闻

# 国际关键词
INTL_KEYWORDS = [
    "美国", "美股", "美联储", "美债", "纳指", "道指", "标普",
    "欧洲", "欧元", "欧盟", "欧股", "英国", "德国", "法国",
    "日本", "日股", "日元", "日经", "韩国", "韩股", "韩元",
    "印度", "澳大利亚", "越南", "巴西", "墨西哥", "俄罗斯",
    "伊朗", "以色列", "沙特", "卡塔尔", "霍尔木兹",
    "OPEC", "WTI", "布伦特",
    "特朗普", "拜登", "鲍威尔", "马斯克",
    "彭博", "路透", "华尔街", "金融时报", "FT",
    "苹果", "微软", "谷歌", "亚马逊", "Meta", "英伟达", "OpenAI",
    "Nvidia", "Tesla", "Apple", "Microsoft",
    "SoftBank", "软银",
]

# 国内关键词
DOMESTIC_KEYWORDS = [
    "A股", "沪指", "深指", "创业板", "科创板", "北证",
    "上证", "深证", "沪深", "港股", "恒指",
    "中证", "中国", "人民币",
    "中央", "证监会", "央行", "国务院",
    "宁德时代", "比亚迪", "茅台", "腾讯", "阿里", "美团",
    "京东", "拼多多", "字节",
    "光伏", "风电", "锂电", "新能源",
    "稀土", "半导体", "存储芯片", "CPO",
]

# tag 分类倾向
INTL_TAGS = {"华尔街原声", "霍尔木兹日报", "周刊提前读"}
DOMESTIC_TAGS = {"今日热点", "光伏观察", "风电观察", "权益周观察", "地产观察"}


def classify_news(title, tag):
    """返回 'intl' / 'domestic'"""
    t = str(title)
    # 优先看 tag
    if tag in INTL_TAGS:
        return "intl"
    if tag in DOMESTIC_TAGS:
        return "domestic"
    # 关键词扫描
    intl_hits = sum(1 for k in INTL_KEYWORDS if k in t)
    dom_hits = sum(1 for k in DOMESTIC_KEYWORDS if k in t)
    if intl_hits > dom_hits:
        return "intl"
    if dom_hits > intl_hits:
        return "domestic"
    # 都没命中 → 默认国内（保守）
    return "domestic"


def extract_field_text(value):
    """飞书字段值兼容处理"""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get("text", "")
    return str(value or "")


def delete_all_records(token, table_id):
    """批量删除某表所有记录"""
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}"
    all_ids = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{base}/records", headers={"Authorization": f"Bearer {token}"},
                        params=params, timeout=30)
        d = r.json()
        items = d.get("data", {}).get("items", [])
        all_ids.extend(item["record_id"] for item in items)
        if not d.get("data", {}).get("has_more"):
            break
        page_token = d["data"].get("page_token")
        if not page_token:
            break

    if not all_ids:
        return 0

    # 批量删除（每批 500）
    deleted = 0
    for i in range(0, len(all_ids), 500):
        batch = all_ids[i:i+500]
        r = requests.post(f"{base}/records/batch_delete",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"records": batch}, timeout=30)
        if r.json().get("code") == 0:
            deleted += len(batch)
        else:
            print(f"  ! 删除批失败: {r.json().get('msg')}")
        time.sleep(0.3)
    return deleted


def write_record(token, table_id, fields):
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}"
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    r = requests.post(f"{base}/records",
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                     json={"fields": fields}, timeout=30)
    return r.json().get("code") == 0


def batch_write(token, table_id, records):
    """批量写入（每批 500）"""
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}"
    success = 0
    for i in range(0, len(records), 500):
        batch = records[i:i+500]
        body = {"records": [{"fields": {k: v for k, v in r.items() if v not in (None, "")}} for r in batch]}
        r = requests.post(f"{base}/records/batch_create",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json=body, timeout=30)
        d = r.json()
        if d.get("code") == 0:
            success += len(batch)
        else:
            print(f"  ! 批量写失败 (batch {i}): {d.get('msg')}")
        time.sleep(0.3)
    return success


def fetch_caixin_news():
    """财联社主题新闻 100 条"""
    for attempt in range(3):
        try:
            df = ak.stock_news_main_cx()
            if df is None or len(df) == 0:
                time.sleep(3)
                continue
            return df
        except Exception as e:
            print(f"  ! 财联社拉取失败 (尝试 {attempt+1}/3): {e}")
            time.sleep(5)
    return None


def main():
    today_str = datetime.now().strftime("%Y-%m-%d")
    print("=" * 80)
    print(f"  📰 每日新闻同步飞书 · {today_str}")
    print("=" * 80)

    print(f"\n[1/4] 拉财联社主题新闻...")
    df = fetch_caixin_news()
    if df is None:
        print("❌ 数据源拉取失败")
        return
    print(f"  拿到 {len(df)} 条")

    print(f"\n[2/4] 分类国际 vs 国内...")
    intl_records = []
    dom_records = []
    for _, r in df.iterrows():
        summary = str(r.get("summary", "")).strip()
        if not summary:
            continue
        tag = str(r.get("tag", ""))
        url = str(r.get("url", ""))
        # 标题取前 40 字
        title = summary[:40] + ("..." if len(summary) > 40 else "")
        category = classify_news(summary, tag)

        if category == "intl":
            intl_records.append({
                "标题（中文）": title,
                "标题（原文）": "",  # 财联社无原文
                "中文总结": summary,
                "摘要": summary,
                "分类": tag,
                "来源": "财联社",
                "来源分类": "财联社主题",
                "链接": {"link": url, "text": "原文"} if url else None,
                "抓取日期": today_str,
            })
        else:
            dom_records.append({
                "标题": title,
                "摘要": summary,
                "领域": tag,
                "来源": "财联社",
                "发布日期": today_str,
                "采集日期": today_str,
                "链接": {"link": url, "text": "原文"} if url else None,
            })

    print(f"  国际: {len(intl_records)} 条")
    print(f"  国内: {len(dom_records)} 条")

    token = feishu_token()

    print(f"\n[3/4] 删除历史数据...")
    d_intl = delete_all_records(token, INTL_TABLE)
    print(f"  国际表删除 {d_intl} 条历史")
    d_dom = delete_all_records(token, DOMESTIC_TABLE)
    print(f"  国内表删除 {d_dom} 条历史")

    print(f"\n[4/4] 写入今日新闻...")
    s_intl = batch_write(token, INTL_TABLE, intl_records)
    print(f"  国际写入 {s_intl} / {len(intl_records)} 条")
    s_dom = batch_write(token, DOMESTIC_TABLE, dom_records)
    print(f"  国内写入 {s_dom} / {len(dom_records)} 条")

    print(f"\n✅ 完成")
    print(f"  国际表：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={INTL_TABLE}")
    print(f"  国内表：https://w5scrwkn9y.feishu.cn/base/{FEISHU_APP_TOKEN}?table={DOMESTIC_TABLE}")


if __name__ == "__main__":
    main()
