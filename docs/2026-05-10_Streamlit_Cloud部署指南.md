# Streamlit Cloud 部署指南

把 [streamlit_app.py](../streamlit_app.py) 部署成公网可访问的 Web App，免费 + 不用自己买服务器。

## 准备工作（已完成 ✅）

- [x] `streamlit_app.py` 改成读 DuckDB（commit `1f0f266`），不再依赖 `data/snapshots/`
- [x] `.streamlit/config.toml` 主题配色 + headless 模式
- [x] `requirements.txt` 含 `duckdb` `streamlit` `pandas`
- [x] `stock_history.duckdb`（15M）已 commit 入库，云端可读
- [x] 仓库公网可访问：[zexpp5/StockAssistant](https://github.com/zexpp5/StockAssistant)

## 部署步骤（你来点）

### 1. 登录 share.streamlit.io

打开 https://share.streamlit.io → 点 **Continue with GitHub**（用 zexpp5 账号）

第一次会弹 OAuth 授权框：

- 必给：**Public repositories** 读权限
- 想部署私库：勾 **All repositories** 或单仓选 **StockAssistant**
- 你这仓库已经 public，给 Public 就够了

### 2. 创建 App

进入 dashboard 后点右上角 **Create app** → 选 **Deploy a public app from GitHub**

填表：

| 字段 | 值 |
|---|---|
| Repository | `zexpp5/StockAssistant` |
| Branch | `main` |
| Main file path | `streamlit_app.py` |
| App URL（可选自定义） | `linearview-stock` 或随意 |

点 **Deploy**。

### 3. 等首次构建（5–10 分钟）

后台会执行：
1. clone 仓库
2. `pip install -r requirements.txt`
3. 执行 `streamlit run streamlit_app.py`

慢点是因为 `requirements.txt` 里 akshare / pandas / numpy 总共 200MB+。**只第一次慢，后面只在你 push 时增量构建**。

成功后会得到形如 `https://linearview-stock.streamlit.app/` 的公网 URL。

## 安装后

### 自动同步

你以后 `git push origin main` —— Streamlit Cloud **自动重启** App 用最新代码。所以每天 launchd 跑完 daily_refresh、`stock_history.duckdb` 数据更新后，**只要你 push 一次**，云上就自动刷新。

> ⚠️ 但 `daily_refresh.sh` 跑完不会自动 commit/push。要自动化推数据上云，需要在 `daily_refresh.sh` 末尾加 `git add -A && git commit -m "auto: $(date)" && git push`。**不建议**——会污染 commit 历史，而且失败会卡住。**建议手动**：每周或重要节点手动 push。

### 资源限制

Streamlit Cloud 免费档：
- 1 GB RAM、1 vCPU
- 数据库文件 < 200MB（你 15M 远低于）
- 无空闲超时（不像 Heroku 会冻结）
- 公网可访问，但有 Streamlit 的访问统计

## 排错

### A. 构建失败：`No module named X`

`requirements.txt` 有缺包 → 加进去 push 再触发重建。

### B. 启动失败：`No such file: stock_history.duckdb`

duckdb 文件没 push 到 `main`。检查：
```bash
git ls-files | grep stock_history.duckdb  # 应该有输出
```

### C. 页面全部空白 / `—`

DuckDB 里没数据 → 本地跑 `python3 migrate_pipeline_to_duckdb.py`、`python3 migrate_snapshots_to_duckdb.py`，再 push。

### D. App 跑慢 / 卡顿

Streamlit Cloud 免费档 1GB RAM。如果 `history_data.json`（830KB）+ duckdb 一起加载内存会涨。当前实现用 `@st.cache_data(ttl=300)`，5 分钟内复用——应该 OK。

## 卸载

Dashboard 里点 App → 右上角 **⋮** → **Delete app**。
