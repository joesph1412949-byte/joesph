# QMT 本地数据源 — 让 agent 顺畅拿 A股行情/K线/涨停 设计文档

> 日期: 2026-08-13
> 状态: 待用户审阅

## 目标

Vibe-Trading agent 现在拿 A股数据走外部 HTTP 源(eastmoney/akshare/yfinance/tushare),慢、被墙或要 token。strategy_web 已经通过 xtquant(miniQMT)打通了本地实时数据,但只暴露了选股接口。

本次把 strategy_web 扩展成一个**本地 QMT 数据服务**,新增 K线/涨停股/实时盘口三个只读接口,并**优化选股 51 秒重筛**(不碰选股逻辑,只改数据获取方式);Vibe-Trading 侧新增 `qmt_data` 工具让 agent 走本地数据。数据只在 127.0.0.1 内流转,不下单、不外发、不常驻内存。

## 架构与数据流

```
QMT miniQMT ──xtdata──▶ strategy_web(Flask, Py3.7, 127.0.0.1:5000)
                              │  GET /api/market/kline?codes=..&days=..   批量K线(实时, 用完即弃)
                              │  GET /api/market/limitup                  涨停股列表(读快照, 秒回)
                              │  POST /api/market/limitup                 涨停股列表(实时重算+落快照)
                              │  GET /api/market/tick?codes=..            指定股票实时盘口(实时)
                              │  (已有) /api/screen  POST跑/落快照  +  /api/screen/latest 秒读
                              ▲
                              │ urllib(标准库)
Vibe-Trading agent ──qmt_data 工具──┤
```

- strategy_web 是唯一能 import xtquant 的进程(Py3.7);Vibe-Trading(Py3.12)只通过 HTTP 读数据。
- agent 侧工具用标准库 `urllib.request`,不 import xtquant/flask。

## 环境约束(不变,沿用既有)

| 组件 | 解释器 | 关键依赖 |
|------|--------|----------|
| strategy_web | Python 3.7.4 | xtdata(cp37 pyd, junction 链接进 site-packages) + flask 2.1.3 |
| Vibe-Trading 后端/agent | `.venv`(Python 3.12) | 无 xtquant 依赖,工具用标准库 urllib |

- xtdata **无 `connect()`**:miniQMT 通过 RPC 自动连接,用 `get_sector_list()` 探针确认链路(已修)。
- 本机主机名含 GBK 字节,`app.run()` 前需 monkeypatch `socket.getfqdn`(已修)。

## 数据契约

### ① 批量 K线 `GET /api/market/kline`

参数:
- `codes`(必填,逗号分隔,带交易所后缀,如 `600000.SH,000001.SZ`)
- `days`(可选,默认 120)
- `period`(可选,默认 `1d`;本次只保证 `1d`,分钟线 best-effort)

返回(200):
```json
{
  "ok": true,
  "data": {
    "600000.SH": {
      "dates": ["2026-01-05", "..."],
      "open": [10.0, "..."], "high": ["..."], "low": ["..."],
      "close": ["..."], "volume": ["..."], "amount": ["..."]
    },
    "000001.SZ": { "...": "同上" }
  }
}
```
- 单只拉取失败 → 该股不在 `data` 里(其余照常),不整体 500。
- 全部失败 → `{"ok": false, "error": "..."}`(500)。
- 实现:新增 `DataSource.get_kline_bulk(codes, days, period)`,用 `download_history_data2(codes, period)` + `get_market_data_ex([], codes, period, count=days)` 一次批量。

### ② 涨停股列表(快照缓存)

- `GET /api/market/limitup` → 读快照(秒回);无快照 → `{"ok": false, "error": "尚未刷新, 请先 POST /api/market/limitup"}`(404)。
- `POST /api/market/limitup` → 实时重算(拉全市场 tick + 识别涨停)+ 落盘快照 + 返回。

返回体:
```json
{
  "ok": true,
  "generated_at": "2026-08-13 14:00:00",
  "data": [
    {
      "code": "600000.SH", "name": "浦发银行",
      "last": 10.5, "last_close": 9.8, "up_stop_price": 10.78,
      "sealed": true, "amount": 123456789.0, "volume": 12345,
      "float_volume": 123456789.0, "open_date": "19991110"
    }
  ]
}
```
- 字段即 `DataSource.get_limit_up_stocks` 的既有返回,不改。
- 快照文件 `strategy_web/limitup_result.json`,原子写(tmp + `os.replace`),与 `screen_result.json` 同模式。

### ③ 实时盘口 `GET /api/market/tick`

参数:
- `codes`(必填,逗号分隔;不填则 400,避免全市场 5000+ 股 dump)

返回(200):
```json
{
  "ok": true,
  "data": {
    "600000.SH": {
      "lastPrice": 10.5, "lastClose": 9.8,
      "askPrice": [10.51, "...", "...", "...", "..."],
      "bidPrice": [10.5, "...", "...", "...", "..."],
      "bidVol": [100, "..."], "volume": 12345, "amount": 123456789.0,
      "high": 10.6, "low": 9.7, "open": 9.8, "time": 1786428306000
    }
  }
}
```
- 实现:复用 `DataSource.get_full_market_ticks(codes)`,只拉指定股票,实时、不缓存。
- 找不到的股票不返回(与 get_full_tick 行为一致)。

## 改动点

### ① strategy_web / data_source.py — 新增批量 K线方法

```python
def get_kline_bulk(self, codes, days=250, period="1d"):
    """批量拉多只股票日K线, 返回 {code: DataFrame}。失败个股跳过。"""
```
- `download_history_data2(codes, period)` 一次触发批量下载;`get_market_data_ex([], codes, period=period, count=days)` 一次批量读。
- 单只失败/无数据 → 该 code 不进结果 dict。

### ② strategy_web / screen.py — 选股提速(不改选股逻辑)

1. **指数K线只拉一次**:`run()` 开头拉一次 `index_kline = self.ds.get_index_kline("000001.SH", days=30)`,传入每只涨停股的 `compute_factors`(替代现在每只股内部重复调)。
2. **涨停股K线批量**:`run()` 里 `kline_map = self.ds.get_kline_bulk([lu["code"] for lu in limit_ups], days=250)`,循环里 `compute_factors` 用 `kline_map.get(code)` 替代内部 `ds.get_kline(code, days=250)`。
3. `compute_factors` 签名新增两个可选参数 `kline=None, index_kline=None`:传入则用之(F1/F5/Y3/Y4/S2/S3/S4 用 `kline`,F6 用 `index_kline`),不传则回落现有内部拉取(保持测试/兼容)。

> 东财个股因子并发(ThreadPoolExecutor)本次**不做**——有反爬风险,先上①②两个零风险提速,实测不够再上(Phase 2)。

### ③ strategy_web / app.py — 三个新路由

- `GET /api/market/kline`、`GET /api/market/tick`(实时,无缓存)
- `GET /api/market/limitup`(读快照)、`POST /api/market/limitup`(实时+落快照)
- 快照路径常量 `LIMITUP_SNAPSHOT_PATH = Path(__file__).parent / "limitup_result.json"`(测试可 monkeypatch),写入 `.gitignore`。

### ④ Vibe-Trading 新工具 `qmt_data`(新建 `src/tools/qmt_data_tool.py`)

仿 `strategy_screen_tool.py` 的只读 + fail-open + urllib 模式。

- `name = "qmt_data"`,`is_readonly = True`
- 参数 schema:
  ```json
  {
    "type": "object",
    "properties": {
      "operation": {
        "type": "string",
        "enum": ["kline", "limitup", "limitup_refresh", "tick", "health"]
      },
      "codes": { "type": "string", "description": "逗号分隔股票代码(kline/tick 必填), 如 '600000.SH,000001.SZ'" },
      "days": { "type": "integer", "default": 120 },
      "period": { "type": "string", "default": "1d" }
    },
    "required": ["operation"]
  }
  ```
- 映射:`kline`→GET `/api/market/kline`;`limitup`→GET `/api/market/limitup`;`limitup_refresh`→POST `/api/market/limitup`;`tick`→GET `/api/market/tick`;`health`→GET `/api/health`。
- 超时:kline/limitup_refresh 180s(批量下载首拉慢),limitup/tick/health 10s。
- fail-open:连接失败/超时/非 200 → `{"ok": false, "error": "QMT 数据服务不可用, 请先运行 strategy_web(app.py): ..."}`,绝不抛异常。
- base URL 环境变量 `STRATEGY_WEB_URL` 覆盖(默认 `http://127.0.0.1:5000`)。

## 错误处理与边界

- 所有新接口只读行情,无下单路径;仅 127.0.0.1 监听,不外发。
- 内存「流式」:批量 K线/全市场 tick 在请求处理完即释放,不常驻;涨停快照落盘成小 JSON(读时加载、返回后释放)。
- 磁盘:历史K线是 xtquant 自身缓存(`D:\QMT\userdata_mini\datadir`,现 157M 封顶量级),非新增负担。
- QMT 未连接时,kline/tick/limitup 返回 400/500 带错误提示,agent fail-open 如实透出。

## 测试策略

- **strategy_web**(沿用现有 pytest,离线 mock xtdata):
  - `get_kline_bulk`:mock `download_history_data2`/`get_market_data_ex`,断言批量调用一次 + 返回 `{code: df}` + 单只失败跳过。
  - 三个新路由:mock `DataSource` 方法,断言 kline/tick 参数校验(codes 必填)、limitup 快照落盘 + 读快照 404 分支。
  - `run()` 提速后仍正确:mock 批量方法,断言候选清单结果与提速前等价(指数K线/批量K线被调用一次而非 N 次)。
- **Vibe-Trading 工具**:monkeypatch `urllib.request.urlopen`,测 5 个 operation 正常路径 + fail-open(连接失败、非 200、超时)。
- 现有 strategy_web 100 测试 + Vibe-Trading strategy_screen 7 测试保持通过。

## 全局约束

- 只读、本地、无下单、无外发;不 push 到任何 remote(本地工作流)。
- Python 版本严格按环境约束表(strategy_web 用 Py3.7,agent 用 .venv Py3.12)。
- agent 工具遵循既有 BaseTool 模式(子类、`execute(**kwargs)`、fail-open)。
