# 回测全因子复活设计（2026-09-16）

状态：已批准（用户"都行，直接做"）｜前置调研：`.superpowers/sdd/vibetrading-survey.md`（Vibe-Trading 深读）、本轮 QMT 实测探针（`.superpowers/sdd/probe-*.py`）

## 1. 问题

用户质疑"回测结果这么差，真是我那 30 多个因子跑出来的吗"。取证结论：**不是**。

| 证据 | 事实 |
|---|---|
| 因子级命中统计（254 交易日窗口） | 28 个评分因子**只有 13 个命中过**，15 个恒 0：F1/F2/F3/F5/F6/F7、Y1/Y2/Y3/Y5/Y6/Y7/Y8、S2/S3 |
| 根因（逐因子读代码取证） | ①K 线源只给 `(日期, 收盘价)` 两元组、volume 占位 1.0 → F5/Y3/S2/S3 结构性死 ②未注入 `index_kline` → F6 ③未注入 `up_price/last` → F1 ④未注入 `fund` + 缺 `float_mv` → F7/Y1/Y2/Y5/Y6/Y7 ⑤缺分钟级盘面 → F2/F3 |
| 门控同样残缺 | N3/N4/N5 恒 0（缺 em/ticks 注入），8 项门控实际只有 5 项在算 |
| 执行口径 | 回测买价 = 选股日收盘价（本轮**不改**，用户拍板） |

## 2. 数据可得性（本机实测，2026-09-16）

| 数据 | 来源 | 实测结论 |
|---|---|---|
| 日线 OHLCV + 成交额 | QMT 本地 | ✅ 已可用（657 根，2024-01 起）；当前取数只取了 close，需补 |
| 指数日线（上证 000001.SH / 深证成指 399001.SZ） | QMT | ✅ 254 根；两市成交额 8711亿+9680亿=1.84万亿（量级正确） |
| **1 分钟线** | QMT `download_history_data2` | ✅ **可回溯至 2025-09-15**；40 只×1周 = 2.4 秒；全窗口 17,386 股票-日 ≈ 3-4 分钟 / 170MB |
| 流通股本 | QMT `get_instrument_detail` | ✅ 全历史可比（股本变动缓慢，近似可接受） |
| 基本面（Y1/Y8 纯计算；F7/Y6/Y7 支持 asof） | `datasource/fundamental.py` | ✅ 接口就绪；缓存历史只覆盖近 2-3 周，更早需回填 |
| Y2 股东户数 / Y5 概念 | 东财快照 | ⚠ 无历史 → **本轮起每日采集**，历史时段标 0 |
| **分笔/五档（真实封单）** | QMT / 通达信本地 | ❌ 不可得（QMT 本地 tick=0 条；本机无通达信数据）→ F3 用分钟级代理 |

Vibe-Trading 深读结论：其数据层同样**无 tick/五档**、分钟线只有 mootdx 的 2 万根上限（≈3 个月），**救不了本短板**；引擎因只认 OHLCV、无涨跌停/封板语义而不搬。仅借三样（见 §6）。

## 3. 目标与验收

1. **28 个评分因子在回测中全部可算**（F3 为已标注的分钟级代理），门控 8 项全部可算。
2. 回测报告新增**因子存活率表**（命中次数/评估次数/命中率），网页可见——杜绝"跑的是残废版策略"再次发生。
3. 无未来函数：任何因子只能用 ≤ 决策日的数据（1m 只用当日、基本面按 asof）。
4. 测试全绿（六路径基线 844+），且**同一策略、同一窗口、新旧对比**产出报告。

## 4. 架构：按日注入（关键设计）

现状：`Backtester.run(start, end, em=..., ticks=..., mkt=..., sector_map=...)` 的市场上下文是**静态**的（全程同一份），而 N3/N4/N5/F6/F2/F3 需要**逐日**快照。

新增（向后兼容，旧参数保留）：

```python
run(..., day_feed=None)
# day_feed(d: date) -> dict | None，键（全部可选）：
#   "em":            {"max_boards":int, "yesterday_codes":[code], "yesterday_boards":[...]}
#   "ticks":         {code: {"lastPrice","lastClose","amount","timetag","bidPrice","bidVol"}}
#   "index_kline":   [(date_str, close)]      # N1 用的 880368 涨停指数(池子合成)
#   "sh_index_kline":[(date_str, close)]      # F6 用上证(≥21根)
#   "stock":         {code: {"sealed":bool, "tick":{...}, "up_price":float,
#                            "last":float, "last_close":float,
#                            "float_vol":float, "float_mv":float, "fund":{...}}}
```

`run()` 每日取 `day_feed(d)`；`_pick` 把它拆给市场级字段（em/ticks/index_kline/sh_index_kline）与个股级字段（`_stock_ctx` 合并 sealed/tick/up_price/last/last_close/float_vol/fund）。`em`/`ticks`/`mkt` 静态参数仍生效（旧测试与旧调用不受影响）。

**回测侧构造器**：新模块 `backtest/history.py`（放在 `backtest/` 包，与 CLI 同级，避免污染 `prism/` 生产包）：
- `build_kline6(code)`：QMT 日线 OHLCV 6 元组（替代现在只给 close 的取数）
- `build_day_feed(start, end, pool_codes_by_day)`：产出 `day_feed`，内部按日合成 em/ticks/指数序列/个股上下文；1m 特征从特征库读取（§5）
- 一律**只读 ≤ 当日的**数据

## 5. 1 分钟特征层

模块 `prism/bt_intraday.py`（供回测与将来的验证复用）：

- **采集**：`download_features(codes_by_day, progress)` → 对每个 (code, date) 下载 1m（`download_history_data2` 批量）→ 计算特征 → 落盘 `runtime/cache/bt_intraday/<YYYYMM>.json`（按月分片，原子写）
- **特征**（每 (code,date) 一条）：`limit_price`、`first_seal_hm`（首封时间 HH:MM）、`opened`（是否开板）、`open_times`（开板次数）、`sealed_close`（收盘仍封）、`one_word`（一字板）、`on_board_amt`（板上成交额，元）、`on_board_vol`（板上成交量，手）、`bars`
- **只覆盖 ≤ 当日的窗口**；2025-09-15 之前无数据 → 特征缺失，对应因子按 fail-open 0 并在报告标注
- **F2 早封板**：`sealed=True` 且 `first_seal_hm ≤ 10:00` —— 与实盘口径一致（实盘用 `tick.timetag`）
- **F3 封单强度（代理，用户已批准）**：实盘口径 = `bidVol[0]×100×涨停价 ≥ 流通市值×(0.5%|0.2%)`（队列厚度）；回测无队列 → 用**可观测的对偶量**：板上成交额占比越小=队列越结实。代理判定：`on_board_amt / float_mv ≤ T`（T 初值 0.5%，与实盘主板阈值同量纲），**因子内分支并在 note 与报告中标"分钟级代理"**。上线后可用最近有实时数据的交易日对照真 F3 校准 T。

## 6. 借 Vibe-Trading 的三样（MIT，注明来源）

文件 `d:\Vibe-Trading\agent\backtest\...`（详见调研报告 §8）：
1. **涨跌停成交约束 + ¥5 最低佣金 + 100 股手数**：`engines/china_a.py` 的口径 —— prism 需要：买入侧跳过一字板（1m 特征 `one_word` 判定"买不到"）、卖出侧跌停顺延（`exit_rules` 已有，复用）；佣金 `max(amount×费率, 5元)`；股数按 100 股取整（资金模拟的持仓手数）
2. **`validation.py` 三函数**：`monte_carlo_test` / `bootstrap_sharpe_ci` / `walk_forward_analysis`（纯 pandas/numpy）→ 落到 `prism/validation.py`，结果进回测报告（可选字段）
3. 不借：其引擎、数据层、Parquet/DuckDB 缓存（默认关闭且无增量）

## 7. 每日采集（Y2/Y5）

新增 `python -m prism.fund_snapshot`：收盘后对当日涨停池（或指定代码表）调用 `FundamentalFeed.compute_for_stock`（不传 asof = 当日快照）并落 `runtime/cache/fundamental_cache.json`（既有缓存格式，按 `code:YYYYMMDD` 键）→ 从今天起积累真实历史。守护 15:05 选股后挂钩子（复用既有 `zt_refresh_fn` 模式）；也可手动跑。

## 8. 报告与网页

回测报告新增：
- `factor_hits`: `{fid: {"hits": n, "evals": m, "rate": x}}`（含门控因子与评分因子）
- `data_notes`: 数据可得性说明（如"1m 特征覆盖 244/254 日""F3 为代理口径""Y2/Y5 历史缺失期为 0"）
- `validation`: 蒙特卡洛/bootstrap/滚动前推结果（可选）
- `filter_stats` 保留（上一批已加）

网页"回测"页：交易结果下方加**因子存活率表**（因子/命中率/状态：实算 or 代理 or 恒0）+ `data_notes` 公示。

## 9. 不做的（YAGNI）

- 不改买入口径（用户拍板：本轮只复活因子）
- 不搬 Vibe-Trading 引擎/数据层
- 不做分笔/五档采集（不可得；不做假数据）
- 不引入新依赖（validation 三函数是纯 pandas/numpy 移植）

## 10. 风险与披露

- **1m 覆盖边界**：2025-09-15 之前无分钟数据（约占窗口 4%）→ F2/F3 在那段为 0，报告标注
- **F3 是代理**：与实盘真封单不同源，报告与网页均标注；阈值 T 待用真实数据校准
- **Y6/Y7/F7 历史回填**：缓存只覆盖近 2-3 周，更早为 0（可后续找替代源）
- **流通股本**：用当前值近似历史（股本变动慢；送转/增发日会有偏差，报告标注）
- **回测结果会变化**：因子复活后同一策略的回测数值必然与旧版不同（这正是目的），新旧对比一并给出
