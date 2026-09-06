# 板块感知层设计（孕育期跟踪 / 阶段定位 / 资金惯性）

- 日期：2026-09-06
- 来源：用户提供的 DeepSeek 对话（2026-09-06）四模块升级建议，经数据层 triage 后保留/降级/去掉
- 决策记录：孕育期仅复盘观察不进因子打分（用户拍板）；事件日历去掉（无稳定数据源/不可回测/Y6 手填已有入口）；资金惯性表去掉东财 fflow 历史回补路径，改为 CLIST 当日快照前向累积（fflow 历史端点 09-06 实测仍全封，CLIST f62 实测通）

## 0. Triage 结论（背景）

| 原建议 | 结论 | 理由 |
|---|---|---|
| 板块孕育期跟踪 | 保留（个股底部放量降级为板块级结构信号） | 板块K线/基准全有，可回测口径；个股级依赖的全市场K线缓存停更 08-31 |
| 资金惯性表 | 保留，换数据源前向累积 | pytdx 无资金流接口；fflow 历史端点被封；CLIST f62 当日快照实测可用 |
| 事件日历前置 | 去掉 | 政策/会议无结构化源；催化级别主观不可回测 |
| 板块阶段定位 | 保留 | 纯计算，板块K线可推导，结构化已验证的退潮识别能力 |

## 1. 数据层（prism/market_data.py）

### 1.1 flow_rank 每日快照（东财 BK 细分行业口径，自洽）

- `fetch_flow_rank_snapshot(probe=None) -> list[dict]`：
  - CLIST `fs=m:90+t:2+f:!50`，`fields=f12,f14,f62`，`fid=f62` 排序；翻页取全（同 fetch_sector_list 模式）。
  - 返回 `[{"code": "BK0486", "name": "传媒", "net_in": 6174208000.0}]`；f62 非数值（`"-"`）的行跳过。
- `build_flow_rank(probe=None) -> dict`：
  - 落盘 `cache["flow_rank"] = {"dates": [升序日期...], "rows": {date: [{code,name,net_in}...]}}`。
  - 幂等：当日已在 dates → **覆盖**当日行（盘后重跑取终值）。
  - 合并写缓存（保留其他段，同 _save_cache 语义）。
  - 返回 `{"dates": n, "sectors_today": n}`。
- CLI：`--build-flow-rank`，完成后打印当日净流入前 5 预览。
- 口径声明：东财 BK 行业与申万 31 一级行业**不同体系**，惯性表自洽使用；**不回填 SEC3**（勿污染申万 flow 段）。

### 1.2 benchmark 上证日K（孕育期相对强度基准）

- `build_benchmark(probe=None, beg=BACKFILL_BEG, end=None) -> dict`：
  - `EastMoneyProbe().fetch_kline("1.000001", beg, end, fields2="f51,f53,f57")`（复用现成板块K线接口，secid=1.000001 上证指数）。
  - 落盘 `cache["benchmark"] = {"dates", "close", "amount"}`，**全量替换**（单指数成本低，自愈）。
  - 拉取失败 → 保留旧缓存并告警（fail-open）。
- CLI：`--build-benchmark`。

### 1.3 mkt_snapshot 透出

- `snap["benchmark"] = cache.get("benchmark")`；`snap["flow_rank"] = cache.get("flow_rank")`。
- 段缺失不写键（fail-open，下游判空）。

## 2. 计算层（prism/sector_stage.py，纯函数）

风格对齐 sector_score.py：只依赖 mkt 切片 dict，不碰网络/缓存/registry；缺数据降级不抛。

模块级常量（全部可调，注释说明口径）：

```python
GEST_MAX_R5 = 8.0        # 孕育期"未启动"上限: 近5日涨幅 < 8%
REL_DAYS, REL_MIN = 3, 2 # 跑赢信号: 近3日中≥2日板块日涨幅>上证日涨幅
SHARE_MIN_DAYS = 20      # 占比信号: 至少20日K线才可比 MA5/MA20
STAGE_R5_MAIN = 5.0      # 主升期: 近5日涨幅 > 5%
STAGE_R3_START = 4.0     # 启动期: 近3日涨幅 > 4%
STAGE_R10_START_MAX = 5.0# 启动期: 且近10日涨幅 ≤ 5%(跳升刚脱离盘整)
STAGE_R5_PEAK = 10.0     # 高潮期: 近5日涨幅 > 10%
STAGE_SHARE_PCT = 0.90   # 高潮期: 占比处自身历史≥90分位
FLOW_TOP_N = 3           # 惯性表: 每日净流入前3
FLOW_STREAK_MIN = 5      # 系统性增配: 连续上榜≥5天
```

### 2.1 `sector_table(mkt) -> list[dict]`

- 输入：mkt 切片（`sector`/`benchmark` 段；防未来由调用方保证——GUI 用 mkt_snapshot 全量，天然只见过去）。
- 占比序列：对齐各板块 dates 构建 `date→amount` 映射（union 排序），`share_i_t = amount_i_t / Σ_j amount_j_t`。总量为 0 的日期该日占比不计。
- 每板块输出：`{code, name, stage, note, r3, r5, r10, share5, share20, share_chg, hits, signals}`。
  - `r_n = close[-1]/close[-n-1] - 1`（%）；数据不足 → None。
  - 孕育信号（`signals` 记命中名）：
    1. `rel`：近 REL_DAYS 日中 ≥REL_MIN 日板块日涨幅 > 上证日涨幅（需 benchmark；按**交易日对齐**——板块与上证在该日都有 K线才计该日，date→close 键比对；缺失则该信号不可判，不计数）。
    2. `share`：`share5 > share20`（≥SHARE_MIN_DAYS 日占比才判）。
    3. `struct`：板块站上 5 日均线 且 近 5 日收阳 ≥3（`close_t > close_{t-1}`）。
  - 孕育期 = `r5 < GEST_MAX_R5` 且 `hits ≥ 2`。

### 2.2 阶段判定 `stage_of(...)`（先到先得，note 记数值依据）

1. 数据不足（close < 11 根或占比 < SHARE_MIN_DAYS）→ `"数据不足"`。
2. **退潮期**：`share5 < share20` 且 `r5 < 0`。
3. **高潮期**：占比分位（板块自身全历史）≥ 0.90 且 `r5 > STAGE_R5_PEAK`。
4. **主升期**：`r5 > STAGE_R5_MAIN` 且 `share5 > share20`。
5. **启动期**：`r3 > STAGE_R3_START` 且 `r10 ≤ STAGE_R10_START_MAX`。
6. **孕育期**：`r5 < GEST_MAX_R5` 且 `hits ≥ 2`。
7. **休整**：其余。

### 2.3 `flow_inertia(flow_rank, top_n=FLOW_TOP_N, need=FLOW_STREAK_MIN) -> list[dict]`

- 每日按 `net_in` 降序取前 top_n 的 code 集合；`streak` = 从最近一天往回连续上榜天数。
- 输出 streak ≥1 的板块：`{code, name, streak, last_net_in, systematic(streak≥need)}`。
- 按 streak 降序、再 last_net_in 降序排列；输入空/缺 → `[]`。

## 3. GUI（prism_web，只读观察）

### 3.1 端点 `GET /api/sector_stage`

- `mkt_snapshot()` → `sector_table` + `flow_inertia`。
- 返回 `{"date": 板块K线最后日期, "sectors": [...], "inertia": [...], "flow_days": 惯性表累积天数}`。
- 异常/空缓存 → 200 + `{"sectors": [], "inertia": [], "flow_days": 0}`（fail-open，不 500）。

### 3.2 前端「板块观察」tab

- tab 按钮 + `section#tab-sector`；两个表：
  - **阶段定位**（31 申万行业）：名称 / 阶段徽标 / 依据 note / 近5日涨幅 / 占比变化 / 孕育信号数。徽标配色：孕育=蓝、启动=青、主升=红、高潮=橙红、退潮=灰、休整/数据不足=默认。
  - **资金惯性**：名称 / 连续上榜天数 / 今日净流入(亿)；streak≥5 标"系统性增配"徽标；表头注明东财 BK 口径、前向累积自启用日起。
- app.js：`switchTab` 支持 + `loadSectorStage()`（所有插值 escHtml），切到该 tab 时拉取。

## 4. 测试（TDD，离线罐头数据）

- `prism/tests/test_sector_stage.py`：占比对齐计算 / 孕育命中与"未启动"排除 / rel 信号缺 benchmark 降级 / 五阶段各分支 / 数据不足 / flow_inertia streak 与排序 / 空输入。
- `prism/tests/test_market_data.py` 增：flow_rank fetch 罐头 http / build_flow_rank 增量+当日覆盖幂等 / benchmark 构建与失败保留旧缓存 / mkt_snapshot 新段透出。
- `prism_web/tests/test_app.py` 增：/api/sector_stage 正常与空缓存 fail-open。

## 5. 非目标

- 不进因子 / 策略 JSON / 模拟盘链路（观察模式，用户拍板；积累样本后再评估是否因子化）。
- 不回补 fflow 历史、不动 SEC3 申万 flow 段。
- 不扩展 build_index / day_snapshot（回测暂不需要；未来因子化时再加）。

## 6. 验收

- 全量 pytest 绿（基线 440 绿 + 1 环境失败）。
- `python -m prism.market_data --build-benchmark --build-flow-rank` 实跑成功落盘。
- 5000 端口 GUI「板块观察」面板出数（浏览器验收）。
