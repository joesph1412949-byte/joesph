# prism 记忆（只在做 prism 相关项目时读）

> **适用范围**：`prism/`（主引擎：36 因子 / 策略 / 回测 / 模拟盘 / 实盘守护）、`prism_web/`（网页 :5000）、
> `datasource/`（现役数据模块）、`backtest/`（回测 CLI）、`qmt/`（miniQMT 实盘桥）。
> **不涉及这些的会话不需要读本文件**。共享内容（合作偏好 / 环境坑 / 部署 / 账户读取 / 决策史）在根 `MEMORY.md`。
> 维护：prism 侧的里程碑与新决策更新本文件。
> 2026-09-18 从根 `MEMORY.md` 按项目拆出，原文各段保持原样，仅归位。

## 当前状态（prism 侧，2026-09-18 盘中）

| 维度 | 状态 |
|---|---|
| 测试基线 | **七路径 1441 绿 / 0 失败**（09-19 批次 W1 实跑：`1441 passed, 25 warnings in 113.52s`；证据 `.superpowers/sdd/fixw1-full-final4.txt`）。⚠️ **用例数不是可靠指纹**（未跟踪测试文件会被一并收集：1099↔1134↔1150↔1391↔1441），**"零失败"才是不变量**。⚠️ **更硬的教训（09-19 实测）**：**全量七路径不是顺序无关的** —— 同一份代码连跑 5 次得到 **5 个不同失败集**，且失败文件**单独跑全绿**（`77 passed`）。根因是 ≥4 个会话并发写同一棵工作树（最多 40+ 脏文件），会读到别人在途的半成品。⇒ **"零失败基线"只在"无人并发写树"时才有意义**；判定某个失败归谁，必须看它落在谁的文件上（或做 `git archive HEAD` 纯净树对照）。**口径校正**：老记录里的「945/995」是 6 路径（漏 `tt_solo/dashboard/tests` 23 例），别再用 |
| 模拟盘 | 守护**当前未运行**（09-17 23:38 起过一次 paper_daemon + prism_web，09-18 14:30 实测只剩一个回测进程）；账本仍 1,000,000 现金、**零持仓** |
| 实盘（prism 主策略） | **未上线**。代码已通（`prism/live_daemon.py`），三步验收一步未做 |
| Git | **本地领先 origin 约 18 个提交，未推送**（09-19 审计批次；`origin/master` 停在 `0445b46`）。已推送的最近一次是 `f205fed..1d2b55b` + `..51eac57`。**push 前仍必须先问用户** |
| 首板拆解（观察层） | `prism/first_board_review.py` 五维拆解 + `prism_web`「首板拆解」tab（含板块阶段列）；**09-19 接上 `sector_stage`**（产业逻辑维，五维全可用）。**不进因子打分/买卖链路**。细节见根 `MEMORY.md`「首板盘后拆解」节 |
| QMT | **09-18 23:32 实测在线**（`xtdata.connect()` 成功返回 IPythonApiClient, 服务 127.0.0.1:58610）—— 同日 14:31 曾离线, 状态会变; **跑长任务前仍先探一次**。注意: **xtquant 逐码调用会硬崩进程**（68 只逐个 download_history_data2 直接崩到 Python 异常都捕获不了）→ 必须批量一次下载 |

## 2026-09-19 全仓审计与修复批次（prism 侧）

> 完整报告：`docs/reports/审计_20260919_未修问题与新想法.md`（从仓库**重新取证**、非转述子代理，含"已推翻更正"13 条）。共享教训见根 `MEMORY.md`。**全部本地未推**。

**已修复并提交（SHA 顺序即提交序）**：

| SHA | 内容 |
|---|---|
| `2697985` | 涨跌停口径统一：`shared/common` 补北交所 `92` 段；F1 删自带实现改走 `shared.common`；加**参数化一致性守卫**（common vs exit_rules 逐码相等） |
| `48fd258` | **东财涨停池"空池"不再当交易日**：非交易日/超保留期返回的是 `[]` 而非 `null`（实测）；改后不占 `_hybk_history` 配额、不计 `daily_counts`、不当"昨日"；20 日历日全空 → `get_market_stats` 返回 `None`。收益：周一 `yesterday_codes` 从 `[]`（N3 死）切回周五真数据，F7 窗口 2→4 交易日 |
| `badee56` | 模拟盘账目诚实性：选股失败**不再占当日幂等键**（此前 QMT 抖一下 = 当天零建仓且日志与"今天没票"不可区分）；双腿 ¥5 最低佣金；**撤单/失效流水 `side` 改 `cancel`/`expire`**（此前 9 条撤单在面板上被显示成 9 笔"买入"）；paper 侧补北交所 30% 档 |
| `1c07934` | **实盘链路 fail-closed**：`positions()` 查询失败/None → `None`（**不再与"确实空仓"不可区分**，此前会**抹掉实盘账本并返回 `trusted=True`**）；对账**自愈采纳**（券商有账本无 → 取 `open_price`/`buy_date=今日`）；`tick_once` **最开头**查急停；可卖量拿不到不回落全量；建仓受**可用现金**约束（`budget=min(总资产,可用现金)` 逐只扣减）；`exit_rules` 坏数据不再中断整轮、`buy_date` 未知不回落 today（T+1 交给券商可卖量） |
| `3675466`+`ac6f188` | **层权重接线**：新增 `composite.mode="weighted_sum"` = `min(Σ(模型 weight×层分), cap)`，**按名字**加权；`full_factor_v1.json` 切到该模式。**边界**：加权只改排序，不动 `candidate_min_model`/分级（已用 `filter_stats` 逐项相同实证） |
| `e94e5d3`+`81a620b`+`65c8fa8` | **S6 独立自由度**：改前 S6 与 F4 **逐字相同**（回测命中 5041 vs 5041 逐位相同）；改为「板块指数当日涨幅≥1%」。**A/B（79 交易日、固定 weighted_sum）：+3.25%→−0.54%、sharpe 0.45→0.22、`filtered_min_model` 959→1274** —— 单窗口/n≈300/无显著性检验，**不可据此断言"更差"**；可断言"去双重计数达成 + 资质线副作用由 S6 单独造成（与 composite 模式无关，已实证分离）"。保留/调权/回撤**留待用户拍板** |
| `6cbe510` | 涨跌停档位补 `689`（科创CDR 20%）与 `400/420`（老三板 5%，从 `"4"` 前缀拆出）。**实测 `689009.SH` 真的进过涨停池**（`2025-02-21`，54.05→62.03=+14.76% 被 0.10 档误判）⇒ `backtest/cli.py` 该日 `up_price` 59.46→64.86。**⚠️ 只做了一半**：`prism/zt_history.py:70-77 _limit_ratio`（决定**池子成员**那份）仍缺 689 ⇒ 该股**仍会**被收进池子 → 已排队等合并批次 |
| `43731ae` | **交易日闸门**：`schedule.in_session` 加周末闸门（`prism/zt_history.py` 亦被其改动，见下"方案被推翻"） |
| `4a44bc8` | **`prism/strategy_lint.py`**（新）：策略 JSON 声明键消费自检（AST 扫"哪些键从没被生产代码读过"），未消费即 exit 1 |
| `b3781c9` | ops/测试诚实性：`ops/smoke_check.py:150` **恒假条件**修活（此前交付自检**无条件**打印"CLI --help 可用"，且引用了不存在的 `scripts/` 路径）+ 新增 `pyproject.toml`（`--import-mode=importlib` 此前只活在命令行）+ `.gitignore` 补 `*.tmp`/`*.pem`/`/_*.py`/`/_*.txt`/`.workbuddy/` |
| `c50162d` | 删已证实死代码（旧 `backtest/engine.py` + `legacy/` + `tt_solo/tools/compare_legacy.py` + `qmt` 三个零引用工具，**含会向真实账号打 8 笔 `passorder` 的 `order_probe.py`**）。14 文件 / **+28 −1744**；**删除净贡献 = −7 例**（根 `tests/test_backtest.py` 的 7 个 node id），**引入 0 个新失败**（用 `git show HEAD:` 取回被删代码做 A/B 对照）。⚠️ **生死线**：`backtest/__init__.py` 必须同批去掉 `from .engine import BacktestEngine`，否则 `prism_web/app.py:68` 的 `from backtest.cli import ...` 会连带 import 已删模块、**把 5000 打挂**（已实测 `python -c "import prism_web.app"` exit 0）。级联：`legacy/` 一删，`shared/common.SECTORS` 与 `runtime/state/close_pick_state.json` 都**再无消费者**（数据文件未删） |

**实测口径更正（重要，别再用旧说法）**：
- **"涨跌停缺 92 档让回测算错"** → `92` 段**零影响**（池子 5224 只里北交所 **0 只**）；`400/420` 拆分也**零影响**；**只有 `689` 段有影响（1 只股 × 1 天）**。
- **"交易所后缀 4 份副本已造成北交所漏判"** → **过度声称**。`sector_map` 5220 键与 zt 缓存 5224 只**只有 0/3/6 开头**（`.BJ` 0 只），两种口径逐键**差异 0 个** ⇒ 是**维护陷阱**，不是活跃 bug（但手写后缀实际有 **11 处**，整类会丢 BJ）。
- **北交所为何不在池子里**：真因是 `build_cache`/`refresh_cache` 取 `xtdata.get_stock_list_in_sector("沪深A股")`（**该板块本身不含 BJ**），**不是** `zt_history._with_suffix` —— 那函数**零调用、是死代码**（我的假说被实测推翻，如实记下）。
- **`backtest/cli.py` 会改写真实缓存**：`load_market_data()` → `market_data.futures_snapshot()` **在跑批时联网采集并回写** `.market_data_cache.pkl` ⇒ **连跑两轮期货数据不同**（F8 命中 666↔719）⇒ **A/B 必须先用 `factor_hits` 逐因子核验两臂一致**。已让 `market_data` 批次把 `futures_snapshot()` 改成**默认只读**。
- **通达信 K 线通道当前是死的**：`python -m prism.tdx_source` 自检个股/大盘/板块日K **全 0 根**，逐台直连 pytdx 均 `TdxFunctionCallError`，**只放行财务/统计**。⇒ `benchmark`（停更在 09-07）的降级链改为 **东财 → 通达信 → QMT**（`000001.SH` 本地有货，174 根至 2026-09-18，正好补上缺的 9 个交易日）。

**仍未闭环（需用户拍板 / 排队）**：
1. **`prism/zt_history.py:70-77 _limit_ratio` 对齐**（决定池子成员）—— 排队合并批次：连 `datasource/factors.py:87-88`、`datasource/data_source.py:94`、`prism/tdx_source.py:85`、`tt_solo/ttcore/_vendor.py:101`(代码+`:8` 注释) 与守卫扩展一起做。
2. **`first_board_review.py:324` 调 `live_account.seal_snapshot()`，该函数不存在**（被 `except` 吞成 None）→ 并发会话的新功能，**只报告未修**。
3. `GET /api/first_board?refresh=1` **会 `write_text` 覆盖 `docs/reports/首板拆解_<日期>.md`**（可为并发会话的脏文件），且护栏对 GET 一律放行、无单飞锁 → 应改 POST + 进 `_LOCAL_ONLY`（**归属并发会话，待用户定**）。
4. `ops/watchdog.py` 服务清单与 `start_all.py` 不一致（**不守护 `qmt_sync`**），且 `install_watchdog.bat` 的计划任务**从未安装成功**（`cd /d` 是 cmd 语法、在 PowerShell 里报错）→ "有守护"是文档假象。
5. `shared/common.with_market_suffix` 把 `400xxx → .BJ`，而其档位自 `6cbe510` 起是 0.05（老三板）→ **同文件内不一致**；**老三板正确后缀待查，无证据不改**。

## 项目现状（截至 2026-09-18）

**prism**：A股量化系统。选股引擎（36 因子 / 3 策略 / JSON 策略文件）+ 回测 + Flask 网页 GUI（5000 端口）+ 模拟盘守护。Windows + Python 3.12 + QMT miniQMT（xtquant：`C:\Users\28037\AppData\Local\Programs\Python\Python312\Lib\site-packages`）+ **通达信 pytdx 1.72（同目录，09-05 装）**。

- **回测全因子复活（09-16 起，spec/plan `docs/superpowers/{specs,plans}/2026-09-16-backtest-full-factor-revival*`）**：起因是用户质疑"回测结果这么差，真是我那 30 多个因子跑出来的吗"——取证结论**不是**（28 个评分因子只有 13 个命中过、15 个恒 0；门控 8 项只有 5 项在算）。五个任务的最终状态：
  · **Task 1/2**（09-16/17，`0054c88..cb471bc`）：按日上下文注入 `day_feed(d)`（复活 N3/N4/N5/F1/F6/S2/S3）+ 日线 OHLCV 6 元组（复活 F5/Y3）+ `prism/bt_intraday.py` 1 分钟特征层（复活 F2；F3 走**已标注的分钟级代理**）。
  · **Task 3**（09-17/18，`5bd45c5`+`8cccbfd`+`e509dc2`）：基本面注入（Y1/Y8 纯计算 + F7/Y6/Y7 按 `asof` 窗口；Y5/Y2 快照类**两侧剔除**防未来）+ `prism/fund_snapshot.py` 每日快照采集（守护 15:05 钩子，等涨停池刷新线程结束再采、空池报 WARNING 不装成功）+ 池条目补 `float_mv`。
    · **用户 09-17 拍板（选项 A）：回测侧基本面默认只读缓存、不联网**，`--fetch-fund` 才联网；网页回测端点一律 offline。**离线模式绝不写缓存**（否则空/半截结果被 `if key in self._cache` 永久钉住，联网运行与每日快照再也补不上）。
    · 动因是实测：东财单个未缓存 (股票,日) 基本面 = **41.8 秒**（Y6 公告端点 `np-anotice-stock` 独占 35.5s）→ 全窗口 254 日 × ~40 只 ≈ 9000 对 ≈ **100 小时**，一次 4 天窗口真数据回测跑了 27 分钟仍在爬（CPU 仅 16s）被强杀。改后同一命令 **4 秒**跑完。
  · **Task 4**（09-18，`3783878`+`173e36c`）：借 Vibe-Trading(MIT) 三样 —— **¥5 最低佣金（买/卖双腿各收一次）**、100 股整手、买入侧跳过一字板（缺 `one_word` 记"未知"不造假）+ `prism/validation.py` 三函数原样移植（`monte_carlo_test`/`bootstrap_sharpe_ci`/`walk_forward_analysis`，报告 `validation` 默认跑、失败不影响返回）。顺手修两个既有 bug：`_report` 有交易分支漏 `data_notes`、`_simulate_equity` 卖出分支丢建仓成本调整。
  · **Task 5**（09-18，`86c9110`）：报告新增 **`factor_hits`**（门控+评分因子命中率/评估数）+ `data_notes`（1m/基本面覆盖、F3 代理、Y2/Y5 缺失、流通股本近似）+ 网页回测页"因子存活率"表与数据说明区 + 全窗口验收对比 `docs/reports/回测全因子复活对比_20260916.md`。
  · **全窗口验收（2025-09-01~2026-09-16，254 交易日，QMT 在线时跑的）**：`full_factor_v1` **1068 笔 / 收益 267.03% / 胜率 43.54% / sharpe 2.81**；`first_board_v04` **482 笔 / 227.08% / 54.77% / 3.73**。**评分因子 25/28 命中过、门控 8/8**；恒 0 三个：`Y2`/`Y5`（回测刻意剔除的快照类）+ `S2`（量价堆积密度：探针实测 16341 次评估里"60日≥20天放量"与"60日振幅≤10%"**零次同时成立**，属该宇宙里确实不触发，非数据缺失）。无 rate=1.0 常数因子。
  · **同代码同窗口、只关数据供给（`--no-market-data`）**：`full_factor_v1` 254 天全被门控拦下 **0 笔**、v04 候选 17386 全被模型分过滤 **0 笔** —— 这就是"残废版数据供给"与复活后的对照。
  · 测试：七路径 **1058 绿**（当时基线）；各任务均经独立子代理两阶段审查（Task 3 一次 Needs fixes→修复→复评 Approved；Task 4 一次 Needs fixes→修复→复评 Approved；Task 5 一次 Needs fixes + **全分支终审 With fixes**）。
  · **✅ 09-19 复采复跑（最新口径，覆盖旧数字）**：补采 1m 那 4 天缺口（445 股日）后覆盖 **16,277/17,296 股日 / 244 天**（2025-09-16~2026-09-18），重跑两策略默认口径 ⇒ `full_factor_v1` **1067 笔 / 315.20% / 胜率 43.67% / sharpe 3.02 / 回撤 19.01%**、`first_board_v04` **479 笔 / 248.40% / 55.11% / 3.91**；门控 8/8、**无 rate=1.0 常数因子**。差异归因：1m 覆盖变好 + C1 补齐（未做逐项消融，属"最可能来源"）。产物 `.superpowers/sdd/t6-*.json`。
  · **✅ 09-19 验收口径裁定（用户授权）**：`Y2`/`Y5` 是规格 §7/§10 要求的**设计剔除**（快照类无历史、带回测未来性）⇒ 不计入分母；**`S2` 已从 `full_factor_v1` 势能板块层摘除**（全窗口 **0/16524**；改"截止 T-1"窗口后交集仍 **0/492** —— 两子条件在涨停池宇宙量级互斥；摘除前/后同窗回测**逐项一致** 1067 笔 / 315.20% / sharpe 3.02 / `filter_stats` 逐字相同）⇒ 现行 27 个评分因子中 25 命中，恒 0 只剩设计剔除的 Y2/Y5，即**可评估因子 25/25 = 100% 命中过**，数据管道层面没有一个因子因缺数据而死（`composite.cap` 9.3333→9.0，S2 因子仍注册）。详见 `docs/reports/回测全因子复活对比_20260916.md` §6.1/§6.2/§9 与 `.superpowers/sdd/s2-diagnosis.md`。
  · **✅ 09-19 另一真 bug（`5cd7293`）**：`_day_payload` 取今收/昨收**硬取 6 元组的 `[4]`** ⇒ 一旦某只票只有 close（老 zt 缓存兜底 / QMT 缺该股本地日线）就 `IndexError`，**整天的按日上下文装配失败并静默退回静态参数**（N3/N4/N5/F1/F6/F2/F3 + 基本面/1m 注入当天全丢）。改成走三档契约的 `_kline_close`；并新增"只有收盘价"的**披露条目**（`data_notes`: 日K契约 N/M 只股票无 volume → F5/Y3/S2/S3/M6/M7 对其失效），避免再次静默。
  · **终审抓到并修掉一个真 Critical（C1，`298d586`）**：`fund_snapshot` 与守护每日快照调 `compute_for_stock` **不传 `float_mv`** → 落下的缓存条目不含 Y1/Y8；之后回测带 `float_mv` 问同一天，被 `if key in self._cache` **短路** ⇒ **Y1/Y8 永久为 0**（真实缓存里 24 条同指纹）。而文档还教用户用 `fund_snapshot --date` 补历史 = **越照做 Y1/Y8 死得越多**。修法：命中缓存时就地补齐 Y1/Y8（offline 只返回不落盘，自动治愈已坏条目）。
  · 同轮修掉：守护在 15:00~15:05 重启时仍可能整日空采（6h 节流挡住当日刷新）→ 空池强制刷新+重试一次；验收报告 1m 缺口归因改正（15 日 = 11 日保留边界 + **4 日 20260615~18 待补采**）；报告 §7.4 不可复现的"复跑逐字节相同"改成如实表述 + sha256 落档；`atomic_write` 的 tmp 名加 pid+线程 id（该缓存有多个写者，固定 tmp 名会互相踩）。
  · **未闭环项（09-19 更新）**：①②③**均已闭环**（S2 已摘除、口径已裁定、4 天 1m 已补采并复跑）④**已 push**（`f205fed..1d2b55b` / `..51eac57` / `..36feb07` / `..5cd7293`）。仍留：`F7/Y6/Y7` 的基本面历史只覆盖 24/254 天（要靠 `prism.fund_snapshot --date` 逐日回填，属数据积累而非代码问题）；`2025-09-01~09-15` 无 1m 数据是 QMT 保留边界（不可得，已披露）。
  · 遗留 Minor 清单在 `.superpowers/sdd/progress.md`（Task 3 八条、Task 4 八条、Task 5 六条、终审十三条中已修 12 条）。

- **09-18 晚追加：push + 一轮 bug 猎杀修复**（用户拍板"push，再看看有没有其他bug"）：
  · **push 两次**：`f205fed..1d2b55b`（复活批次 8 个提交）→ `1d2b55b..51eac57`（后 4 个修复提交）。核过 `Joesph_key.pem`/`.workbuddy/`/`_ui_backup_*` **从未入库**。
  · **tt `ref` 盘前取错日**（原「🔴 高优先 #0」）**已修，且修了两轮**（详见 `tt_solo/MEMORY.md`）：`303b860` 第一版用"tick 交易日新于日K"启发式 —— **09-19 真实 QMT 实测被推翻**（QMT 把 `tick.timetag` 打成**当前墙钟**，启发式几乎恒真 ⇒ 又退回去信陈旧 tick，ref 仍错一天）；`3754841` 改成**以"今天"为参照系**（末根日K==今天 → 取上一根 `daily_prev`；否则取末根），真机复测 4 只票全部落到正确的上一交易日收盘。
  · **只读 bug 猎手 + 终审复评各抓到一批真缺陷**（`1903efa` 修）：①守护把"全股失败 `{"saved":0,"failed":40}`"当成功 → 当天 Y2/Y5（不可回补）永久丢失却说"采集完成" ②**整文件回写缓存 → 跨写者丢更新**（长寿命 feed 会抹掉快照线程/CLI 新增的键；`atomic_write` 的 pid+tid tmp 名只保证"不写坏"、不保证"不丢写"） ③联网半截条目（网络类一个都没成功仍落盘）被缓存短路**永久钉死** ④`--no-market-data` 连带静默关掉基本面注入。
  · **`os.replace` 的 Windows 真坑**（`9349b3c` + `51eac57`）：**只要目标文件被任何读句柄打开，`os.replace` 就抛 `PermissionError(13, 拒绝访问)`** —— 加"按路径写锁"只解决写-写互踩；修法是给 `os.replace` 加**有界退避重试**（只重试 PermissionError / winerror∈{5,32}，上限 ~1s，耗尽抛出）。**三份原子写实现都要带**：`shared/common.atomic_write`、`prism/zt_history._atomic_pickle`、`tt_solo/ttcore/_vendor.atomic_write`。修后 5 次全量连绿（实现者 3 次 + 控制者 2 次）。
  · 缓存合并还补了"**读失败不覆盖**"（`_load_cache` 读异常曾被当 `{}` ⇒ 合并退化成整文件覆盖 = 原病）。
  · **原子写统一**（`af2f60f` + `36feb07`）：`prism_web/app.py`×3 与 `prism/engine.py`×1 不再手写 `tmp+os.replace`，改走 `shared.common.atomic_write`（唯一 tmp 名 + fsync + 退避重试 + 失败清理一次拿全）；`zt_history._atomic_pickle` 与 `tt_solo/ttcore/_vendor.atomic_write` 补齐"失败即清理 tmp"；4 处"恒真空的 tmp 残留断言"改回真守卫（负控：放一个假残留时旧断言仍通过、新断言失败）。**全量 1135 绿连跑 4 次**（实现者 2 + 控制者 2）。
  · push 收尾：`1d2b55b..51eac57`、`19316c2`、`19316c2..36feb07` —— **master 与 origin 完全同步**。
  · 已知遗留（非阻塞）：`prism/engine.py` 的 `import os` 已随统一原子写变为无用（已删）；`prism/paper.py` 无需改（它本就走 `atomic_write`）。

- **网页回测静默零交易修复**（09-16，用户报"三个策略回测全无交易"）：**不是选股条件苛刻**——根因是 `prism_web/app.py` 的 `/api/backtest` **从未注入市场数据**（mkt/sector_map），而 `backtest/cli.py` 默认注入 → 依赖它们的 10 个因子（N6-N8/F8/F9/SEC1-4/SEC6）恒 0：v04 候选**全 0 分**被 `candidate_min_model=3` 全过滤；full_factor_v1 门控 N6-N8 恒 0 → 最多 2 分 < 3 → **门永远不开**。证据：同区间 CLI 注入=30/124 笔、CLI `--no-market-data`=0/0 笔、网页=0/0 笔（gate_notes 与"关注入"逐字相同）。
  · 修法：CLI 的市场数据装配抽成 **`backtest/cli.py: load_market_data()`**（网页与 CLI 共用，消除双份实现）→ 网页端点复用；回测报告新增 **`filter_stats`**（门控拦截天数/候选数/被模型分过滤/被板块过滤）+ **`market_data`** 标志，前端零交易时把归因摊开（不再静默）。
  · 实测（20260801–20260904）：v04 **30 笔 / +11.4%**、full_factor_v1 **124 笔 / +28.3%**（网页与 CLI 逐字一致）。**v03 仍 0 笔属设计**——它是 v04 的对照组（仅 F1-F7），回测内核里只有 F4 算得出 → 1894 只候选 100% 被模型分门槛过滤；需实时盘口数据（实盘/模拟盘）才有意义，已在界面上如实标注。
  · 全量 **844 绿**（840+4 新测试：回测诊断 2 + 网页注入/警示 2）。
- **ponytail 全仓精简 + 结构归位**（2026-09-15，commit 9bb567a..，**生产代码净 -824 行**）：三块并行审计（数据/交易/网页层）后逐条 `git grep` 证死再删——
  · 删：market_data 整套「按日索引」死子系统、zt_history 重复定义的 `qmt_zt_feed`、旧买入路径 `buy_from_screen`、`composite mode="max"` 幽灵模式、trader 四个零调用钩子、tdx 三个死函数、`_report` 里带 `sharpe=7.07` 常数的死分支等；
  · 抽：`prism/schedule.py`（两守护共用调度）、`engine.resolve_strategy/gate_evaluate`、`shared.common.atomic_write/next_weekday`（**升级为 mkdir+fsync 最强版**，tt 的三份副本归并过来）、`_batch_download`、`_strategy_path`、`_cut`；
  · 交易层做过**差分回归**：新旧 backtest 5 场景 + paper 7 场景逐字段 IDENTICAL 才提交。
  · **结构**：`strategy_web/` → **`datasource/`**（v04 网页外壳已删，只剩现役数据模块 data_source/eastmoney/fundamental/manual_store/perf_store + factors 迁移比对基准；其 123 个测试纳入标准命令）；绩效存档 `perf/` 与 `manual_factors.json` 迁入 `runtime/state/`；依赖清单提根 `requirements.txt`；`ops/watchdog.py`+`start_all.py` 的「strategy_web 占 5000」陷阱修正为 prism_web（Py3.12）；死索引 `.market_data_index.pkl`(1.85MB)、根 `log/`、测试残留 bak、10 个 `pt_*` 临时目录已清。
  · **交付视角测试**：新增 `ops/smoke_check.py`（导入/缓存/14 条 GET 路由/计算层/状态健康五关，503 记为可接受降级、500 记缺陷）——首跑抓到 `/api/stock/<code>/kline` 在 QMT 停机时 **500 裸异常**，已修为「QMT→通达信降级 + 两头都挂返回 503 + 友好文案」；另修 `_report` 死分支、`_share_series` 收窄、README/STRUCTURE 路径同步。
  · 审计与执行明细：`docs/reports/ponytail精简审计_20260915.md`；项目总结（HR 用）生成器 `ops/make_summary_pdf.py`。

- **板块周度跟踪**（09-07/08，commit d0adcbc..68b3195，**纯观察面板**）：`prism/sector_etf_map.py`（31 行业 ETF 锚点映射，24 锚点+7 留空，QMT 逐码验证）+ `sector_stage.new_high_counts()`（60 日新高家数，zt 缓存直算）+ `sector_table()` 扩展四列（week_rank/etf/new_high/pos_cap 纸面上限）+ app.py 组装（**801 前置过滤=纯申万宇宙**[占比分母与旧面板有出入，已记录]、双重翻译 6位码→801→行业名、memo 双 mtime）+ 板块观察 tab 新四列（**建议上限=纸面参考未接入交易**）。周排名/新高/占比拥挤等 triage：C3 机构共识去掉、C1 惯性/C2 拥挤查重已有、状态调制暂缓（红线）。ETF 行情**按日刷新**（本地末根<今日才批量下载，memo 失败也占当日槽；09-08 终审 I-1 修冻结病）。`build_sector_cache` 尾部过期重采修复（原"已有即跳过"日期永久冻结 09-02；变短守卫防静默截断）。全量 **518 绿**
- **板块感知层**（09-06/07，commit 3b368c0..361956c，**观察模式不进打分**——用户拍板）：`prism/sector_stage.py` 纯计算（孕育期三信号：近3日≥2日跑赢上证/成交额占比MA5>MA20/站上5日线且近5日阳≥3，未启动 r5<8%；五阶段判定 退潮>高潮>主升>启动>孕育>休整；资金惯性 streak=每日净流入前3连续上榜，≥5日=系统性增配）+ market_data 新段 `flow_rank`（**东财 BK 细分行业口径自洽，勿回填 SEC3 申万体系**；CLIST f62 当日快照**前向累积**幂等、空快照不落盘）+ `benchmark`（上证日K全量替换，09-07 已落 165 日）+ CLI `--build-flow-rank`/`--build-benchmark`（**容错：一段被封不连累另一段，点名失败非零退出**）+ GUI `/api/sector_stage` +「板块观察」tab（**需重启 5000 Flask 生效**）。**升级对话 triage 沉淀为 skill `.claude/skills/prism-upgrade-triage/SKILL.md`**（三问门：数据层可办到/取数容易/实测有效 → 保留/降级/去掉 + 用户拍板；已过 RED/GREEN 子代理测试）

- **全项目体检 + miniQMT 实盘就绪评估**（09-13，报告 `docs/reports/项目体检报告_未完成任务与miniQMT实盘就绪_20260913.md`）：①修**测试隔离真缺陷**——`test_backtest`/`test_registry` 的 autouse fixture `reg.reset()` 有可能先于 conftest 快照执行，使还原快照取到"残缺状态"(1~2 个测试因子)，把真实 36 因子库从整个 session 抹掉 → `test_sector_factors` 整片 `UnknownFactorError`（沙箱下稳定 21 例复现）。修法双层：conftest 加会话级干净基线 + `SEC1` 哨兵自愈；`test_sector_factors` 加 autouse fixture 按需 force 重扫（自足化，根治）。②修 **`registry._clear_factor_bytecode` 健壮性**：只吞 `OSError`，而沙箱 safe-delete 抛 `SystemExit`(BaseException) 会穿透 → 缓存清理失败中断因子注册；改捕获 `BaseException`（KeyboardInterrupt 除外）。③新增 **`qmt_live_check.py`**：实盘只读就绪自检（接口/数据格式/配置/运行 四类 32 项，只 query + 方法存在性，绝不下单；`--quiet` 只看问题；退出码 0/1/2）。④数据缓存停更已刷新：板块K线 09-04→**09-11**、NDX/SPX/DJIA 09-04→**09-11**（东财封→新浪源）、US10Y/VIX 09-03→**09-10**（FRED）。⑤**miniQMT 实测就绪**（只读探测）：`connect()=0`、账号 `88869979`、总资产 274,783.61、持仓 1 只；`order_stock/order_stock_async/cancel_order_stock*` **均在**——**外部 Python 可直接下单，无需把桥脚本粘进 QMT**（现有代码只用了它的查询能力）。
- **通达信数据层**（09-05，commit 2f46e51）：`prism/tdx_source.py` —— pytdx 直连，**补充源定位**（QMT 优先，取不到时降级顶上；板块成分股/证券列表仍走 QMT）。服务器白名单 4 台（123.125.108.14 是残废已剔除）；**板块/指数 K 线必须走 get_index_bars**（get_security_bars 返回乱码内存）；连接会被巨量请求污染 → 每调用前健康检查+换机重连；`tdx_source.set_enabled(False)` 全局开关（测试 conftest 已默认禁用）。自检：`python -m prism.tdx_source`。pytdx 无美股/宏观——NDX/US10Y/VIX 仍走 akshare/FRED
- **数据层遗留修复批次**（09-07，ac5ba0b..2007be4）：④zt_history 刷新链路——停更 08-31 根因=`build_cache` 增量只补新股、存量 K 线永不更新（重跑 --build 也没用）；新增 `refresh_cache`（存量尾部续传 90 天窗+新股只进尾部+落盘全改原子写 tmp+os.replace）+ CLI `--refresh` + `prev_day_pool` 陈旧保护（>10 自然日 fail-closed 空+warning）+ 守护自动刷新钩子（启动连接成功后+收盘选股后各一次，6h 节流，`zt_refresh_fn` 可注入）。已实跑回补：索引 399→404 天（09-01..09-07 齐；09-04 池 41 只），今日 15:05 选股 F9 首次拿到真"昨日池"。**注意 09-07 条目是 13:13 盘中临时值，收盘后刷新一次才作数**。⑤Y1/Y8 float_mv 兜底——`data.py` QMT FloatVolume 缺失/0 时降级 `tdx_source.float_shares`（**liutongguben 单位=股已实测**，茅台 12.5 亿股对上）；float_mv 统一=股本×现价均正才算否则 None；fund_feed 吃到兜底值；QMT 正常时零 tdx 调用（有否定用例）。测试 470→486（+16）
- **因子断链修复**（09-05 同 commit）：①F6 恒 0 根因=指数K线从未回填个股 ctx **且** index_kline 字段被 N1(涨停指数880368,≥6根)与 F6(上证指数,≥21根)语义冲突共用 → 新增独立字段 `sh_index_kline`（build_market_context 抓 QMT 上证K线，失败降级通达信）②M6/M7 实盘恒 0=K线拉 250 根但因子要 251 根算 MA250 → 改 260 ③回测量能：kline_feed 升级 OHLCV 契约（向后兼容三档：2/3/6 元组按长度识别），东财/腾讯源给真实成交量，mkt 注入改默认开启（`--no-market-data` 关闭）
- **factor_check 升级**（09-05）：除"空上下文不崩"外，新增 5 个数据齐全合成场景（首板封板/放量突破/均线多头/市场门控/妖股）跑命中率，全 0 的标 ZERO-HIT。**36 因子已全部场景命中**——证明恒 0 全是数据供给问题非因子逻辑。坑：因子里 `ctx.get(x) or y` 遇 DataFrame 真值测试抛 ValueError 被 except 吞成恒 0（factor_check 实际抓到过一次这种回归）
- **模拟盘**：100 万 paper trading；守护由**用户双击桌面 `PRISM.bat`** 启动（2026-09-04 起——09-04 会话故障曾带走守护两天半，教训：守护不寄生 agent 会话；PRISM.bat=守护+网页 GUI 双防重复启动；仓库内 `启动模拟盘.bat` 保留为守护单启动）；当前激活策略 = **full_factor_v1**（09-04 起主力；default 已删除——净值归因切换点 09-03，账本不清零）
- **交易节奏**（09-04 起，spec 2026-09-04-open-top5）：15:05 收盘选股（门禁 3/8 → 三层**等权**打分 → 前 5 存 planned_buys）→ 次日 09:26-09:35 开盘买入窗口（每只净值 **15%**；开盘价直接买；一字板/开盘即板 → 按真实涨停价转排队；跌停开盘跳过；非当日行情快照 fail-closed 跳过）→ 盘中每 5 秒排队检查+卖出（止盈 **15%**/止损 5%/持有 5 日，跌停顺延）→ 15:00 结算。**盘中 10:00/13:30 排队时点已移除**，打板机制仅作一字板替补
- **排板成交模型**（09-04 修正）：两路径——钉板吃穿全队列成交 + **炸板吃穿初始队列成交**（真实打板主通道，09-03/04 九委托零成交的根因=旧条件结构性近不可满足）；撤单流水带 dvol_shares 事后归因；半残 tick（lastVolume=0）不建委托
- **全因子四层策略**（09-03）：36 因子（门控 N1-N8 / 首板 F1-F9 w0.60 / 妖股 Y1-Y8 w0.25 / 势能板块 M6·M7·S2-S6·SEC1-4·SEC6 w0.15）；M1-M5 与 S1/S5/S7 已删；策略库仅存 v04/v03/full_factor_v1
- **策略编辑器**（09-01 完成）：网页新建策略/设为默认/复制底稿；指针 `prism/strategies/.active.json` 热重载（选股+回测+模拟盘处处生效）
- **排板队列状态机**（09-03 完成，089ea9d）：买入排队制（成交=新增成交量穿越前方封单+本单且仍封板；开板撤单当日禁排；收盘作废；资金冻结；跌停卖出顺延）。423 测试全绿。**待下一交易日真实验收**（13:30 时点排队第一现场/T+1/hold_expire 对照）

- **F3 封单强度 ×100 修复**（09-03，ded9e1a）：bidVol/lastVolume 均为手（xtdata 官方示例 L1492 实证）；回测对比零影响（回测无实时盘口，F3 在回测恒 0——v04 九因子回测实际 8 个生效）；修复意义在实盘/模拟盘真实盘口
- **未推 GitHub**：**0 个**——09-14 目录重组 + tt_solo 迁移 + 09-16 网页回测修复批次，已于 **09-16 推送**（`4a8554a..f205fed`）。**push 前仍必须先问用户**
  - 09-13 已重启守护（PRISM.bat，15:12）：paper_daemon + prism_web 在跑；zt 缓存 15:19 自动刷新
  - **模拟盘 09-08 午后~09-13 空窗**：账本最后选股 `2026-09-07T15:05`，守护 09-08 午后停摆 → 09-08/09/10/11 四个交易日无选股无成交（非代码问题，进程没在跑）；现金仍 1,000,000、无持仓（09-03/04 九委托零成交的已知结果）
  - **发现重复 web 进程**：`prism_web\app.py` 两个实例（14248 占 5000 / 16604 冗余），启动器只查端口监听，双开仍可能漏网
- **守护重启待办**：现跑的守护是 09-07 12:28 启动的旧代码，**无自动刷新钩子**；且 app.py/静态资源 09-07/08 有改动（板块观察 tab 四新列）——**重启 PRISM.bat 一次**同时激活两件事；启动即补当日真实收盘涨停池（旧盘中临时值由陈旧保护兜底，但重启更干净）
## 因子管理惯例（2026-09-03 起）

- **新增因子默认组合进 full_factor_v1**：按四层归位——环境/情绪类→market_gate（N 系）；首板确认类→first_board 模型（F 系）；妖股类→monster 模型（Y 系）；形态/板块类→momentum 模型（M/S/SEC 系）。加入策略 JSON 对应层 factors 数组并**重算 composite.cap**（Σ 模型权重×该模型因子数）。特殊要求（如仅供实验/仅供回测）才不入，需用户明说
## 观察项 / 遗留

- `D:\cc-joesph\.tdx_probe\`（09-05 探测用 venv，6397 文件）沙箱批量删除需确认，留待用户自行删
- 排板真实验收（下一交易日）；守护断连重试需人工重启（60×10s）
- triage Minor 列表见 `.superpowers/sdd/progress.md`（编辑器 M1-M9、模拟盘 T2/M-d/M-f/M-i/M-j 等，均非阻塞）
- **因子数据遗留**（09-05 审计；09-06 大修、09-07 数据层批次后**剩 1 项**）：~~①fundamental 未来函数~~ ✅ 已修（`_ref(asof)` 全路由、窗口双向 [cutoff, ref]、缓存按 asof 分日；Backtester 可注 fund_feed，Y5/Y2 快照类回测剔除，commit 3dba8f6 已推送）~~③sector_map 6 行业零覆盖~~ ✅ 已补齐（5220 只，F8 煤炭/石油石化映射恢复）~~④zt_history_index 停更无调度~~ ✅ 已修+已回补（09-07 刷新链路：refresh_cache 尾部续传+--refresh+陈旧保护+守护钩子；停更根因=增量只补新股）~~⑤Y1/Y8 float_mv 靠实时 tick~~ ✅ 已接线（tdx float_shares 兜底，单位=股实测）②**SEC3 资金流仍缺 15/31 板块**（东财 fflow 端点对 801120/801720/801890/801950 等持续封禁+部分 EMPTY，09-05/06 多轮 30s 间隔重试 0 成功；SEC3 fail-open 得 0；恢复手段：`python -m prism.market_data --build-sectors`（增量只补缺的）或等东财解封；勿用东财 BK 码回填——口径不同会污染申万体系）
- **市场数据缓存基线**（09-06）：板块K线 31 行业到 09-02（申万源乐咕自身延迟，`--build-sectors --source sw` 下一交易日收盘后追平）；global NDX/SPX/DJIA/UDI 09-04（新浪源）、US10Y/VIX 09-03（FRED）；fundamental_cache.json 按 asof 分日后旧条目仍兼容（key 含日期段）
- **东财 push2(clist) 09-07 起封禁中**（RemoteDisconnected，裸 requests 同样失败）。**09-13 复查：`push2his` 也已封**（全球指数/上证基准采集全部 RemoteDisconnected；而 09-06 时它还活着）→ 后果：benchmark 停更 09-07、flow_rank 仍 0 天、flow 仍 16/31、UDI 停 09-04（新浪/FRED 均无美元指数序列）。**benchmark 的可行替代=复用已建好的 `tdx_source`（通达信 get_index_bars 取上证指数）**，待接线
- **miniQMT 实盘接入缺口**（09-13 评估，详见体检报告 §3.2）：**P0 两项已落地（09-13 晚）**——①**prism 引擎→实盘出口**：新增 `prism/live_daemon.py`（15:05 收盘选股只落计划 → 次日 09:26-09:35 写 BUY 信号 + 记 `positions.json` → 盘中卖出巡检；默认 dry-run，`--live` 才落信号，只写信号文件绝不下单）②**卖出链路**：复用 `exit_rules`（新增 `enforce_t1` / `can_use_volume` / `is_limit_down` / `today` 注入）+ `prism/live_account.py`（只读账户，`calc_buy_volume` 按总资产×execution.pct 算整手）③顺带补：确定性 order_id 幂等（#6）、券商持仓对账（#4 部分）、持仓上限/单只比例/账户查询失败 fail-closed（#5 部分）、T+1（#8）。测试新增 `prism/tests/test_live_daemon.py` 22 例，全量 **720 绿**。**仍缺**：交易日历（仅 weekday，靠桥端 armed 兜底）、真实成交价/费用回写、日内最大亏损/停牌识别；**上线三步验收（DRY_RUN→sim→小额真实单）一步未做**——演练入口 `python -m prism.live_daemon`（默认零副作用）

## 信号桥与实盘接入（prism 主策略链路）

**通信方式**：prism 主进程与 QMT **只通过 `D:/QMT_SIGNALS/` 下的 JSON 文件通信**。
桥脚本 `qmt/bridge/*.py` **自包含**（不 import 本项目任何模块），可在 QMT 的 GBK 解释器里单独跑。

```
prism/ 主引擎 → 写 JSON → D:/QMT_SIGNALS/real/pending/*.json
                            ↓
              qmt/bridge/signal_bridge_real.py（在 QMT 终端内运行）
                            ↓ 三道闸门：paused / armed.txt / 当日去重
                        miniQMT 下单 → 券商柜台
```

**重要事实**：`order_stock / order_stock_async / cancel_order_stock*` 接口**都在** → **外部 Python 可直接下单，无需把桥脚本粘进 QMT**（现有代码只用了查询能力）。

**prism 实盘出口（P0 已落地）**：`prism/live_daemon.py`（15:05 收盘选股落计划 → 次日 09:26-09:35 写 BUY 信号 + 记 `positions.json` → 盘中卖出巡检；默认 dry-run，`--live` 才落信号）+ `prism/live_account.py`（只读账户，`calc_buy_volume` 按总资产×execution.pct 算整手）。测试 `prism/tests/test_live_daemon.py` 22 例。

**实盘接入缺口（仍未做）**：交易日历（仅 weekday，靠桥端 armed 兜底）、真实成交价/费用回写、日内最大亏损、停牌识别；**三步验收（DRY_RUN 观察 → 小额真实单 → 成交对账）一步未做**。

## 待办（prism 侧｜原「六、待办清单」中属于 prism 的条目，编号沿用原文）

- **确认 QMT 里跑的桥是哪一版**：`D:/QMT/python/SIGNALBRIDGE.py` 是 16434 字节单行密文，**prism 主策略仍走桥，该确认仍然有效**（该条在原待办里挂在 tt 名下，全文见 `tt_solo/MEMORY.md`；tt 已改外部直连但走 A2 通道时仍用同一份桥）。
## 🟡 中优先（工程完整性）

5. **`runtime/` 路径切换**：运行中的 :5000 服务需**重启一次**才用上新路径；根目录 `log/` 与 `.paper_account.json` 被老进程占用，重启后可删。
6. **重复 web 进程**：`prism_web/app.py` 曾有两个实例（14248 占 5000 / 16604 冗余），启动器只查端口监听，双开可能漏网 → 建议改成按进程名查。（09-18 14:30 又见到一次双开）
7. **守护断连不自动重启**（60×10s 后需人工）→ 考虑接 `ops/watchdog.py`。
8. **`ops/watchdog.py` 服务清单**仍配着 legacy `strategy_web`（端口 5000 实际已被 prism_web 占用）。
9. **`git push`**：✅ **09-18 已推送**（`f205fed..1d2b55b` + `1d2b55b..51eac57`）；下次 push 前仍必须问用户。
10. **回测复活批次的遗留 Minor**（`.superpowers/sdd/progress.md` 有全文，均非阻塞）：Task 3 八条（网络类覆盖只按"天"计 / `ZT_REFRESH_WAIT` 超时分支无测试 / 守护空池告警在周中节假日误报 / `--date ""` 仍 exit 0 / Y1/Y8 文案缺"缺 float_mv"归因 等）、Task 4 八条（买入腿盖戳与兜底分支不自洽 / 盖戳非幂等 / `--oos` 付双份 validation 载荷 等）。
11. **`S2`（量价堆积密度）在本窗口零命中**：探针实测 16341 次评估里"60日≥20天放量"与"60日振幅≤10%"**从未同时成立** → 不是数据缺失，是**因子条件对该宇宙近乎不可达**。**等用户拍板**：调参（属策略变更）/ 承认它只适合别的场景。同时验收口径"28 个评分因子 ≥26 命中"字面为 **25/28**（Y2/Y5 为规格要求的设计剔除）→ 也需用户拍板分母口径。
12. **1m 特征缺口**：`20260615~18` **4 个交易日是采集缺口（可补）** + `2025-09-01~09-15` 11 日早于 QMT 保留边界（不可得，那段 F2/F3 恒 0 已披露）。补采命令（需 QMT 在线）：`python -m backtest.cli --start 20260615 --end 20260618 --build-intraday`；补完应重跑全窗口验收（C1 修复后 `20260910` 的 3 个股票日 Y1/Y8 会补齐，影响 ≤3 股日）。
13. **网页交易页需重启才见新功能**：`factor_hits` 表与数据说明区（09-18）要**重启 prism_web** 才生效。上线面最小自检三件：①日志出现"基本面快照采集完成"②`fundamental_cache.json` 当日新条目**含 Y1/Y8**③`bt_intraday` 当日条目含 `one_word` 键。

## 🟢 低优先（历史遗留 / 数据源）

10. **东财 `push2(clist)` 与 `push2his` 均已封禁**（RemoteDisconnected）→ 后果：benchmark 停更 09-07、flow_rank 0 天、flow 16/31、UDI 停 09-04。
    **可行替代**：benchmark 复用 `tdx_source`（通达信 `get_index_bars` 取上证指数）**待接线**。
11. **SEC3 资金流缺 15/31 板块**（东财 fflow 对 801120/801720/801890/801950 等持续封禁）→ `python -m prism.market_data --build-sectors` 增量补；**勿用东财 BK 码回填**（口径不同会污染申万体系）。
12. **模拟盘 5 交易日空窗**（09-08 午后~09-13 守护停摆）→ 非代码问题；重启守护后曾补跑。
13. **排板队列状态机真实验收**未做（下一交易日：13:30 时点排队第一现场 / T+1 / hold_expire 对照）。
14. **子目录内过期重复副本**：`prism_web/fundamental_cache.json`、`strategy_web/fundamental_cache.json`（现行生效的是 `runtime/cache/fundamental_cache.json`）。注意 `strategy_web/manual_factors.json` **是数据别删**。
15. **`strategy_web/` 已 legacy**，可择机彻底移除。

## prism 侧工程血泪坑

| # | 坑 | 现象 | 解法 |
|---|---|---|---|
| 1 | 因子里 `ctx.get(x) or y` | 遇 DataFrame 真值测试抛 `ValueError` 被 except 吞成恒 0 | 显式判 None，不要用 `or` 兜底 |
| 2 | **QMT `download_history_data2` 无超时会挂死** | 全窗口 1m 采集跑到 4700/14890 后 25 分钟零进展（CPU 平、socket 连在 QMT 上）；`--build-intraday` **只在最后按月落盘**，一挂全丢 | **逐月/逐旬独立进程 + 硬超时看门狗**（脚本范例 `.superpowers/sdd/intraday-months.ps1`；202604 整月两次 300s 超时，拆旬才过）。补采后 1m 覆盖 = **15832 (股,日)/239 天**（2025-09-16 起，早于此 QMT 无 1m 数据） |
| 3 | **东财基本面单股 41.8s**（Y6 公告端点 35.5s） | 回测注入真 feed → 4 天窗口 27 分钟仍在爬；全窗口 ≈100 小时 | 回测侧**默认只读缓存不联网**（09-17 用户拍板）；历史用 `python -m prism.fund_snapshot --date YYYYMMDD` 逐日回填 |
| 4 | **`requests` 的 timeout 不等于"总时长上限"** | 标量 timeout 同时管 connect 与 read，但**不覆盖 DNS 解析**，且 read timeout 是"每次 socket 读"→ 服务端慢速分片可远超 5s（实测 35.5s 全在一个端点） | 别把"设了 timeout"当"不会挂"；对外部取数要么加总时长看门狗，要么默认离线 |
| 5 | **报告里的"覆盖率"可能是假覆盖** | 修复前 `data_notes` 写「基本面: 个股日覆盖 17296/17296」，而缓存实际只覆盖 26 天（原因：Y1/Y8 纯计算只要当日有 `float_mv` 就有值，被误计成"已覆盖"） | 计数要**按类拆**（网络类 vs 纯计算），披露要**点名真因**（"未采集的日子为 0"）；审查时拿真实产物对账，别信自报 |
| 6 | **QMT 连续跑几小时后本地读盘会劣化** | 09-18 00:21 后日线读取从 ~20ms/只 变成 2.0→4.5s/只、多线程无加速（xtdata 串行化）；一次复跑 45 分钟仍卡在 K 线预取 | 长回测**分段跑**；跑前先探一次单只耗时；QMT 离线时每个代码还要付 ~2s 连接超时（5 日池 185 只 = 378 秒，看着像卡死） |
| 7 | **派生量塞进"按日缓存"会被缓存短路永久钉死** | Y1/Y8 是 `float_mv` 的纯函数，但快照/守护调 `compute_for_stock` **不传 `float_mv`** → 缓存条目缺 Y1/Y8；`if key in self._cache: return` 让之后带 `float_mv` 的查询**永远拿不到**（真实缓存 24 条中招） | ①缓存命中分支**就地补齐**可派生因子（本批已修）②给缓存写者列清单：同一 key 的不同调用方必须传齐参数，否则先落者定生死 |
| 8 | **交付物里的"覆盖率/进度"数字可能不可靠**（同 #5） | 例：报告写"复跑逐字节相同(662,314 B)"，实际那是**日志内紧凑 JSON 长度**，落盘文件是 695,207 B；`--no-market-data` 被当成"旧代码结果" | 报告里区分"日志字符数 / 落盘字节 / sha256"；对照实验要标明"近似旧口径"而非"旧代码"；能落 sha256 就落 |

## 测试与验证速查（prism 侧）

```bash
# 全量测试（七路径；老记录 1058 绿 @298d586、09-18 实测 1134 绿 @51eac57，见「当前状态」基线；
# 用例数会因未跟踪的测试文件波动，**零失败才是不变量**；测试离线可跑，脱沙箱更准）
python -m pytest prism/tests prism_web/tests datasource/tests tt_solo/tests tt_solo/dashboard/tests qmt_sync/tests tests -q \
    --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNNN
# NNN 递增，下一个 220

# 回测（QMT 在线才快；基本面默认只读缓存不联网，联网取数须显式 --fetch-fund）
python -m backtest.cli --start 20250901 --end 20260916 --strategy full_factor_v1
python -m backtest.cli --start 20250901 --end 20260916 --strategy full_factor_v1 --no-market-data  # 近似"旧残废数据供给"对照(0 笔)
python -m backtest.cli --start 20250901 --end 20260916 --build-intraday   # 1m 特征采集(会下载；逐月跑更稳)

# 实盘接入前自检（只读，绝不下单）
python -m qmt.tools.live_check          # 31 通过 / 0 阻断
python -m qmt.tools.live_check --quiet

# 模拟盘 / 实盘演练（默认零副作用）
python -m prism.paper --once
python -m prism.live_daemon --once

# 因子体检 / 通达信源自检
python -m prism.factor_check
python -m prism.tdx_source      # 6 项：连接/个股日K/大盘指数/板块指数/快照/流通股本
```

> 通用 pytest 两个坑（basetemp 正斜杠、沙箱假失败）见根 `MEMORY.md`。
