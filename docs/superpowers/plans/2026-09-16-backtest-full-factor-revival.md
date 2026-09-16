# 回测全因子复活 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。步骤用 `- [ ]` 跟踪。
> 规格：`docs/superpowers/specs/2026-09-16-backtest-full-factor-revival-design.md`（唯一事实源，冲突以规格为准）

**Goal:** 让回测能算出全部 28 个评分因子与 8 个门控因子（F3 为已标注代理），并给出因子存活率报告。

**Architecture:** 回测新增"按日上下文注入"（`day_feed(d)`）；1 分钟特征库补足盘面级数据；基本面源注入补 Y1/Y8/F7/Y6/Y7；借 Vibe-Trading 的 A 股成交约束与验证函数。

**Tech Stack:** Python 3.12 / pandas / QMT(xtquant) / pytest；无新依赖。

## Global Constraints

- 工作目录 `D:\cc-joesph`；直接 master 提交；**push 前必须问用户**
- 测试：`$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests datasource/tests tt/tests qmt_sync/tests tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNNN`（NNN 递增取未用号；基线 **844**，每任务后不得回退）
- **向后兼容**：`run()` 的 `em/ticks/mkt/sector_map` 静态参数与所有既有测试断言不得破坏
- **无未来函数**：因子与特征只允许使用 ≤ 决策日的数据；1m 只用当日
- **不可得的数据不许造假**（分笔/五档不做假数据；缺失即 fail-open 0 + 报告标注）
- ponytail：能复用不复写、stdlib 优先、最小改动；校验完整性/原子写/幂等 不得简化；捷径加 `# ponytail:` 注释
- 只有 QMT 在线时才能跑真实数据；**测试必须离线可跑**（用夹具/桩）

---

### Task 1: 按日上下文注入 + 日线 OHLCV + 指数/市场统计

**Files:**
- Modify: `prism/backtest.py`（`run`/`_pick`/`_stock_ctx` 接受 `day_feed`）
- Modify: `backtest/cli.py`（`kline_feed` 返回 6 元组；新增 `build_day_feed` 装配并接进 `main()`）
- Test: `prism/tests/test_backtest_dayfeed.py`（新建）

**Interfaces（Produces）**
- `Backtester.run(start, end, sell_rules=None, progress=None, em=None, ticks=None, mkt=None, sector_map=None, day_feed=None)`
- `day_feed(d) -> dict`：键 `em` / `ticks` / `index_kline` / `sh_index_kline` / `stock`（见规格 §4）
- `backtest/cli.py: kline_feed(code) -> [(date, open, high, low, close, volume), ...]`（6 元组；旧 2/3 元组契约仍被 backtest 支持）
- `backtest/cli.py: build_day_feed(start, end, *, use_intraday=False, progress=None) -> callable`

**Steps**
- [ ] 写失败测试：①静态 `em/ticks` 仍生效（回归）②`day_feed` 返回的 `em.max_boards` 被 N4 用到（构造两天不同 max_boards，断言门控分数随日变化）③`stock[code]` 的 `up_price/last` 让 F1 命中（用现有 `_mk_strategy` 风格夹具 + 测试因子）④`kline_feed` 6 元组时 F5/Y3 类因子能读到 volume（用假因子读 `ctx.kline["volume"]` 断言非占位 1.0）
- [ ] 跑测试确认 FAIL
- [ ] 实现 `day_feed` 贯通：`run()` 内每日 `day_ctx = day_feed(d) if day_feed else {}`；`_pick(..., em=day_ctx.get("em", em), ticks=day_ctx.get("ticks", ticks), index_kline=..., sh_index_kline=...)`；`_stock_ctx(code, kline, mkt, sector_map, limit_ups, fund, stock_extra=day_ctx.get("stock", {}).get(code))`，其中 stock_extra 覆盖 `sealed/tick/up_price/last/last_close/float_vol/float_mv` 并写入 `ctx._extra`（`bt_seal_ratio` 见 Task 2）
- [ ] `backtest/cli.py` 的 QMT 日线取数改 6 元组（`get_local_data` 的 o/h/l/c/volume），东财/腾讯兜底路径保持
- [ ] `build_day_feed` 首版：em（`max_boards`/`yesterday_codes`/`yesterday_boards` 由 zt 池历史合成）、ticks（两市成交额 = 上证+深证成指 amount，按日；`lastPrice/lastClose` 给池内股）、`sh_index_kline`（000001.SH 30 根切片）、`index_kline`（先复用池子合成，N1 现状不变）、stock（`up_price`=昨收×档位四舍五入、`last`=当日收盘、`last_close`=昨收、`float_vol`=QMT 流通股本）
- [ ] 测试 PASS + 全量回归
- [ ] 提交 `feat(backtest): 按日上下文注入 + 日线OHLCV + 指数/市场统计(复活F1/F5/F6/Y3/S2/S3/N3/N4/N5)`

---

### Task 2: 1 分钟特征层（F2 + F3 代理）

**Files:**
- Create: `prism/bt_intraday.py`
- Modify: `prism/factors/factor_f3_seal_strength.py`（代理分支）
- Modify: `backtest/cli.py`（`build_day_feed(use_intraday=True)` 时合成 tick/sealed/bt_seal_ratio）
- Test: `prism/tests/test_bt_intraday.py`（新建，离线夹具）

**Interfaces（Produces）**
- `prism/bt_intraday.py: FEATURE_DIR`（`runtime/cache/bt_intraday/`）、`features_for(code, date) -> dict|None`、`download_features(codes_by_day, progress=None) -> {"stock_days": n, "written": n}`
- 特征字段：`limit_price, first_seal_hm, opened, open_times, sealed_close, one_word, on_board_amt, on_board_vol, bars`
- 回测侧：`stock[code]` 增 `tick`（`timetag`=首封时刻毫秒、`lastPrice`=收盘、`lastClose`=昨收、`amount`）、`sealed`（= `sealed_close`）、`bt_seal_ratio`（= `on_board_amt / float_mv`，float_mv 缺则 None）
- F3 分支：`ctx.get("bt_seal_ratio")` 非 None → 代理判定 `ratio <= 0.005`，note 含"回测代理"；None → 走原实盘口径（`bidVol`）

**Steps**
- [ ] 写失败测试：①`features_for` 读夹具特征（用 tmp 目录 + 注入 `FEATURE_DIR`）②F2：`sealed=True` + `timetag` 09:32 → 命中；10:30 → 不命中 ③F3 代理：`bt_seal_ratio=0.001` → 命中且 note 含"代理"；`0.02` → 不命中 ④F3 实盘口径回归：无 `bt_seal_ratio` 时仍用 `bidVol` 判定（既有测试不得破坏）
- [ ] 跑测试确认 FAIL
- [ ] 实现 `bt_intraday.py`：批量下载（`xtdata.download_history_data2`，按周分片）+ 特征计算（首封=首个 `high ≥ limit-0.011` 的分钟；开板=其后 `low < limit-0.011`；板上量额=首封至收盘累加；一字板=`first_seal==0 且未开板`）+ 按月 JSON 原子落盘 + `features_for` 读取缓存
- [ ] F3 代理分支（阈值常量带注释与 `# ponytail:` 校准说明）
- [ ] `build_day_feed(use_intraday=True)`：从特征库合成 `tick/sealed/bt_seal_ratio`
- [ ] 测试 PASS + 全量回归
- [ ] 提交 `feat(prism): 1分钟特征层(复活F2) + F3分钟级代理(已标注)`

---

### Task 3: 基本面注入 + float_mv + 每日快照采集

**Files:**
- Modify: `backtest/cli.py`（注入 `FundamentalFeed`；池条目补 `float_mv`）
- Create: `prism/fund_snapshot.py`（CLI：`python -m prism.fund_snapshot [--date YYYYMMDD]`）
- Modify: `prism/paper_daemon.py`（15:05 选股后挂钩子，失败不影响主流程）
- Test: `prism/tests/test_fund_snapshot.py`（新建）

**Interfaces（Produces）**
- `prism/fund_snapshot.py: snapshot_once(codes, date=None, feed=None) -> {"saved": n, "failed": n}`、`main()`
- `backtest/cli.py: build_day_feed(...)` 的 `stock[code]["fund"]` 来自 `FundamentalFeed().compute_for_stock(code, float_mv=..., asof=date)`（Y5/Y2 由 `_fund_for` 剔除，保持现状）

**Steps**
- [ ] 写失败测试：①`snapshot_once` 用假 feed（不联网）落缓存且键含日期 ②重复调用幂等（同 key 覆盖不重复写）③失败计数正确 ④守护钩子异常不影响选股返回
- [ ] 跑测试确认 FAIL
- [ ] 实现 `fund_snapshot.py`（复用 `FundamentalFeed` 与既有缓存格式；原子写）；`build_day_feed` 注入 fund + 池条目补 `float_mv`（QMT 流通股本×当日收盘，缺失则 None → Y1/Y8 fail-open 0）
- [ ] 守护挂钩子（复用既有 `zt_refresh_fn` 模式的注入点）
- [ ] 测试 PASS + 全量回归
- [ ] 提交 `feat(prism): 基本面注入回测(Y1/Y8/F7/Y6/Y7) + 每日快照采集(Y2/Y5)`

---

### Task 4: 借 Vibe-Trading 三样（A股成交约束 + 验证函数）

**Files:**
- Modify: `prism/backtest.py`（买入跳过一字板；`_simulate_trade`/`_simulate_equity` 加 ¥5 最低佣金与 100 股取整）
- Create: `prism/validation.py`（移植自 `d:\Vibe-Trading\agent\backtest\validation.py`，MIT，函数头注明来源）
- Test: `prism/tests/test_backtest_costs.py`、`prism/tests/test_validation.py`（新建）

**Interfaces（Produces）**
- `prism/validation.py: monte_carlo_test(returns, n=1000, seed=42)`、`bootstrap_sharpe_ci(returns, n=1000, seed=42)`、`walk_forward_analysis(...)`
- 报告新增 `validation`（可选，默认跑；失败不影响回测返回）

**Steps**
- [ ] 读源实现（`d:\Vibe-Trading\agent\backtest\validation.py`）确认签名与算法，原样移植（MIT 注明）
- [ ] 写失败测试：①佣金 `max(amount×费率, 5)`（小额单触发 5 元下限）②股数 100 取整 ③一字板日不建仓（`one_word=True` → 该笔不产生交易，进 `filter_stats`）④validation 三函数对固定收益序列输出稳定（固定 seed）
- [ ] 跑测试确认 FAIL
- [ ] 实现三处改动（最小侵入；`# 借鉴: Vibe-Trading agent/backtest/engines/china_a.py (MIT)` 注明）
- [ ] 测试 PASS + 全量回归
- [ ] 提交 `feat(backtest): 借Vibe-Trading口径(¥5最低佣金/100股/一字板不成交) + validation三函数`

---

### Task 5: 因子存活率报告 + 网页展示 + 全窗口验收

**Files:**
- Modify: `prism/backtest.py`（`factor_hits` / `data_notes` 进报告）
- Modify: `prism_web/app.py` + `prism_web/static/app.js`（回测页展示存活率表与数据说明）
- Modify: `prism_web/tests/test_app.py`、`prism/tests/test_backtest*.py`
- Modify: `MEMORY.md`

**Interfaces（Produces）**
- 报告字段：`factor_hits`（含门控与评分因子）、`data_notes`（1m 覆盖天数、F3 代理、Y2/Y5 历史缺失、流通股本近似）

**Steps**
- [ ] 写失败测试：①`factor_hits` 含全部门控+评分因子且 `evals>0` ②`data_notes` 非空且含代理说明（当 F3 走代理时）③网页接口返回这两个字段
- [ ] 跑测试确认 FAIL
- [ ] 实现统计（复用本轮探针 `.superpowers/sdd/probe-bt-factorhits.py` 的计数方式，包在 `_compute_scores` 调用处计数，不重复计算）
- [ ] 网页：回测结果下方加"因子存活率"表（因子/命中率/状态）+ 数据说明区
- [ ] **全窗口验收**：2025-09-01~2026-09-16 跑 full_factor_v1 与 v04，输出新旧对比（因子存活率 ≥ 26/28、门控 8/8、命中率不应出现 100% 常数因子）；结果写 `docs/reports/回测全因子复活对比_20260916.md`
- [ ] 全量回归 + MEMORY.md 更新 + 提交 `feat: 因子存活率报告+网页展示+全因子复活的回测对比`

---

## 验收口径（全部满足才算完成）

1. `factor_hits` 显示 28 个评分因子中 ≥26 个命中过（F3 为代理口径、允许 1-2 个在窗口内确实不触发），门控 8/8 有评估
2. 无未来函数：1m 特征只取当日；`asof` 传递正确（测试覆盖）
3. 六路径测试全绿（≥844+新增）
4. 新旧回测对比报告产出，差异有解释
5. 不可得数据（分笔/Y2/Y5 历史）在报告与网页明确标注，无假数据
