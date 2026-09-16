# 目录结构说明（STRUCTURE）

> 最后一次重组：**2026-09-14**。目标：**根目录只留「入口 + 文档」，代码按项目分家，同一职责的文件放同一个文件夹。**
>
> 配套阅读：`MEMORY.md`（项目现状与协作偏好）、`README.md`（功能说明）。

---

## 一、一页速查

| 文件夹 | 一句话 | 类型 |
|---|---|---|
| `prism/` | **主策略引擎**：36 因子、回测、模拟盘、实盘信号守护 | 项目 |
| `prism_web/` | prism 的网页控制台（`http://127.0.0.1:5000`） | 项目 |
| `tt_solo/` | **做 T 策略（自包含）**：日内 T+0 底仓 + 网格 + 风控 + 守护 + 面板（`http://127.0.0.1:5011`，只读） | 项目 |
| `qmt_sync/` | miniQMT **成交/持仓同步**到本地 SQLite + 告警 | 项目 |
| `datasource/` | **现役数据模块**（v04 网页被删后留下的数据层：DataSource/EastMoney/Fundamental/ManualStore/PerfStore；由 prism 与 prism_web 复用） | 项目 |
| `qmt/` | **miniQMT 桥接与工具**（桥脚本 + 只读自检） | 桥接 |
| `shared/` | **跨项目共享底座**（路径常量、日志、原子写、日期与代码工具、卖出规则） | 底座 |
| `backtest/` | 离线回测：旧版引擎 + 命令行入口 | 工具 |
| `legacy/` | v04 时代的独立脚本（收盘选股等） | 归档代码 |
| `ops/` | 运维：启动器、进程看门狗、**交付冒烟自检 `smoke_check.py`、总结 PDF 生成器 `make_summary_pdf.py`** | 运维 |
| `runtime/` | **运行期数据**：`cache/` 采集缓存 · `state/` 账本/绩效/状态 · `log/` 日志（不入库） | 数据 |
| `docs/` | 报告与设计文档（含 `reports/` 体检与审计报告、`superpowers/` spec 与 plan） | 文档 |
| `archive/` | 历史产物、一次性探针脚本、无关文件 | 归档 |
| `tests/` | 根级测试（覆盖 `backtest/` 与 `shared/exit_rules`） | 测试 |

> 2026-09-15 变更：`strategy_web/` → `datasource/`（只剩现役数据模块，v04 网页外壳已删）；
> 绩效存档 `perf/` 与 `manual_factors.json` 迁入 `runtime/state/`；依赖清单提到根 `requirements.txt`；
> `datasource/tests` 的 123 个测试纳入标准测试命令。

---

## 二、数据流（谁把信号送给谁）

```
                 ┌──────────────── prism/（主引擎）────────────────┐
 东财/通达信 ──▶ │ market_data · zt_history · tdx_source  采集缓存 │
                 │        ↓                                        │
                 │ engine + factors/（36 因子）→ registry 打分     │
                 │        ↓                                        │
                 │ paper.py（模拟盘账本）   trader.py（信号生成）  │
                 │        ↓                        ↓               │
                 │ paper_daemon.py          live_daemon.py（实盘） │
                 └────────────────────────────────┼───────────────┘
                                                  │ 写 JSON 信号文件
                                                  ▼
                                   D:/QMT_SIGNALS/real/pending/*.json
                                                  │
                                                  ▼
                        qmt/bridge/signal_bridge_real.py（在 QMT 终端内运行）
                                                  │ 三道闸门：paused / armed.txt / 当日去重
                                                  ▼
                                            miniQMT 下单
```

关键点：**prism 主进程与 QMT 之间只通过 `D:/QMT_SIGNALS/` 下的 JSON 文件通信**，
桥接脚本 `qmt/bridge/*.py` 是自包含的（不 import 本项目任何模块），
可以在 QMT 的 GBK 解释器里单独跑。

---

## 三、逐目录详解

### `prism/` — 主策略引擎

| 文件 | 作用 |
|---|---|
| `engine.py` | 因子评分引擎、策略加载（`load_strategy` / `set_active_strategy`） |
| `registry.py` | 因子注册表：扫描 `factors/` 自动注册，负责因子元数据与校验 |
| `factors/` | **36 个因子实现**（F 基本面 / S 板块 / N 情绪 / Y 资金 / M 量价 / SEC 等） |
| `strategies/` | 策略 JSON 定义（`first_board_v04.json`、`full_factor_v1.json`…），`.active.json` 是本地指针 |
| `context.py` | 个股上下文对象（因子从这里取数据） |
| `market.py` | 市场环境判定（涨停家数、赚钱效应等） |
| `data.py` | 数据提供者：QMT `xtdata` 封装 + 基本面 feed 注入 |
| `market_data.py` | 东财板块 K 线 / 全球指数 / 资金流采集，落 `runtime/cache/` |
| `zt_history.py` | 历史涨停池生成（用 QMT 本地日 K 反推），落 `runtime/cache/` |
| `tdx_source.py` | **通达信 pytdx 直连**备用源（东财被封时的故障转移） |
| `sector_score.py` / `sector_stage.py` / `sector_etf_map.py` | 板块评分、板块阶段、板块↔ETF 映射 |
| `paper.py` | **模拟盘账户引擎**（账本 `runtime/state/.paper_account.json`） |
| `paper_daemon.py` | 模拟盘守护进程（`python -m prism.paper_daemon`） |
| `live_daemon.py` | **实盘信号守护**：15:05 收盘选股落计划 → 次日 09:26-09:35 发 BUY → 盘中巡检卖 |
| `live_account.py` | miniQMT **只读**账户适配 + 按净值比例算买入股数（`calc_buy_volume`） |
| `trader.py` | 选股结果 → BUY/SELL 信号 JSON（`build_signal` / `write_signals`） |
| `backtest.py` | 回测引擎（**新版**，36 因子全链路） |
| `factor_check.py` | 因子体检 CLI（`python -m prism.factor_check`） |
| `_utils.py` | 因子库共享小工具（迁移自 `strategy_web/factors.py`） |
| `tests/` | 该项目的测试 |

### `prism_web/` — 网页控制台（:5000）

| 文件 | 作用 |
|---|---|
| `app.py` | Flask 后端（策略编辑、选股、回测、绩效、暂停开关） |
| `templates/` `static/` | 页面与前端资源 |
| `tests/` | 网页测试 |

### `tt_solo/` — 做 T 策略（自包含包，2026-09-16 由 `tt/` + `tt_web/` 抽取）

> 自包含：不 import `prism` / `shared` / `qmt_sync` / `backtest` / `legacy`，
> 整包拷走即可独立运行（少数底座函数内联在 `ttcore/_vendor.py`）。
> **操作手册见 `tt_solo/README.md`**；旧 `tt/` 与 `tt_web/`（面板 5010）已退役。

| 文件 | 作用 |
|---|---|
| `ttcore/config.py` | 策略参数（`tt_config.json` 是实际取值） |
| `ttcore/engine.py` | T 决策核心：算意图（intent） |
| `ttcore/grid.py` | 网格档位与挂单价计算 |
| `ttcore/market.py` | 行情获取（tick/快照，失败回落离线样本） |
| `ttcore/risk.py` | **风控闸门**（时段/涨跌停/金额/次数/亏损，先硬后软） |
| `ttcore/state.py` | 状态机 + 账本持久化（`tt_solo/runtime/state/tt_state.json`）+ 日终归档 |
| `ttcore/broker.py` | miniQMT 账户只读适配层 |
| `ttcore/executor.py` | 直连下单执行器（**全项目唯一会真报单的地方**） |
| `ttcore/daemon.py` | 守护进程：轮询 → 决策 → 落信号 → 记账（`python -m ttcore.daemon`） |
| `ttcore/arm_today.py` | 人工闸门工具（放行/急停/看状态） |
| `ttcore/sample_data/` | 离线样本 K 线 |
| `dashboard/` | 做 T 监控台（Flask，`:5011`，只监听本机；能急停，**不能下单**） |
| `tools/compare_legacy.py` | 与旧 `tt/` 的对照取证（证明搬家零回归；旧树删除后失去意义） |
| `tests/` `dashboard/tests/` | 223 例 pytest（2026-09-16） |

### `qmt_sync/` — QMT 成交/持仓同步
`qmt_client.py`（连 QMT）、`sync.py`（拉取并入库）、`db.py`/`models.py`（SQLite）、
`alerts.py`（异常告警）、`cli.py`/`__main__.py`（入口 `python -m qmt_sync`）、`README.md`。

### `qmt/` — miniQMT 桥接与工具 ★本次新建

| 文件 | 作用 | 运行位置 |
|---|---|---|
| `bridge/signal_bridge_real.py` | **实盘信号桥**：唯一会真下单的脚本 | QMT 终端内 |
| `bridge/signal_bridge_demo.py` | 模拟通道桥（⚠️ 不校验账户，下单用 QMT 当前登录账号） | QMT 终端内 |
| `bridge/connection.py` | `XtQuantTrader` 连接/回调薄封装示例 | QMT 终端内 |
| `tools/live_check.py` | **实盘接入前必跑的只读就绪自检**（30+ 项，绝不下单） | 本项目 |
| `tools/diag.py` | 打印 xtquant 关键接口签名，排查版本差异 | 本项目 |
| `tools/order_probe.py` | 探测下单方法是否可用 | 本项目 |

### `shared/` — 跨项目共享底座 ★本次新建

| 文件 | 作用 |
|---|---|
| `common.py` | **路径真相来源**（`PROJECT_ROOT`/`CACHE_DIR`/`STATE_DIR`/`LOG_DIR`/`SIGNAL_ROOT`）、统一日志、A 股代码后缀、涨跌停比例 |
| `exit_rules.py` | 卖出规则引擎：止盈 / 止损 / 持有期 / **T+1** / 可卖量 / 跌停顺延 |

被 `prism`、`prism_web`、`datasource`、`tt_solo/dashboard`、`ops` 共同引用
（`tt_solo/ttcore` **刻意不引用** `shared/`：它自带 `ttcore/_vendor.py` 以便整包拷走）。
导入方式统一为 `from shared.xxx import ...`（各组件先把项目根注入 `sys.path`）。

### `backtest/` — 离线回测 ★本次新建

| 文件 | 作用 |
|---|---|
| `engine.py` | 旧版轻量回测引擎 `BacktestEngine`（原根目录 `backtest.py`） |
| `cli.py` | 命令行入口，接**新版** `prism.backtest.Backtester`（原 `backtest_cli.py`） |

```bash
python -m backtest.cli --start 20260701 --end 20260731
python -m backtest.cli --start 20260701 --end 20260731 --compare
```

### `legacy/` — v04 时代脚本 ★本次新建

| 文件 | 作用 |
|---|---|
| `strategy_close_pick.py` | 收盘选股 + 次日发单（独立于 36 因子引擎，v04 时代入口） |
| `demo_screen_and_send.py` | 选股并发送信号的演示脚本 |

```bash
python legacy/strategy_close_pick.py screen
python legacy/strategy_close_pick.py send
```

### `ops/` — 运维 ★本次新建

| 文件 | 作用 |
|---|---|
| `start_all.py` | 一键启动 qmt_sync + prism_web + Vibe-Trading |
| `watchdog.py` | 进程看门狗：服务挂了自动拉起（可注册开机任务） |
| `prism_launcher.ps1` | 桌面 `PRISM.bat` 的实际逻辑（启动守护 + 网页，双防重复） |

### `runtime/` — 运行期数据 ★本次新建

| 子目录 | 内容 | 可重建？ |
|---|---|---|
| `runtime/cache/` | `.market_data_cache.pkl`、`.zt_history_cache.pkl`、`fundamental_cache.json` | ✅ 可重新采集 |
| `runtime/state/` | `.paper_account.json`（模拟盘账本）、`close_pick_state.json`、`live_state.json`、`positions.json`（做T 自 2026-09-16 起有自己的 `tt_solo/runtime/state/`，不再写这里） | ❌ **不可重建，注意备份** |
| `runtime/log/` | 各组件的按天轮转日志 | ✅ |

整个 `runtime/` 已在 `.gitignore` 中忽略内容，只保留目录骨架（`.gitkeep`）。

---

## 四、根目录文件

### 双击入口（`.bat`）——**刻意留在根目录**

| 文件 | 启动什么 |
|---|---|
| `start_all.bat` | 一键全启（qmt_sync + prism_web + Vibe 前后端） |
| `启动模拟盘.bat` | 模拟盘守护 `python -m prism.paper_daemon` |
| `tt_solo/tifosi.bat`（桌面 `tifosi.bat` 是薄壳） | 做 T 统一入口：守护/直连/模拟（演练）、仪表盘 `:5011`、今日放行、闸门状态；高级选项含真报单 |
| `install_watchdog.bat` | 注册开机任务跑 `ops/watchdog.py`（需管理员） |
| `restart_vibe_backend.bat` | 重启外部 `D:\Vibe-Trading` 后端 |
| 桌面 `PRISM.bat` | → `ops/prism_launcher.ps1`（模拟盘守护 + 网页 :5000） |

> 这些留在根目录是为了**双击即用**；逻辑实现全部在 `ops/`。
> 做 T 是例外：实现在 `tt_solo/tifosi.bat`，桌面 `tifosi.bat` 只是指向它的薄壳。

### 文档与配置

| 文件 | 作用 |
|---|---|
| `STRUCTURE.md` | 本文件——目录与职责索引 |
| `MEMORY.md` | 项目现状、测试基线、已知坑、协作偏好（**会话开头必读**） |
| `README.md` | 项目功能总览 |
| `AGENTS.md` / `CLAUDE.md` | 给 AI 编码助手的工作约定（技能系统、红线清单） |
| `.env` | 环境变量（不入库） |
| `.gitignore` | 忽略规则（含 `runtime/` 全部内容） |

---

## 五、常用命令速查

```bash
# 全量测试（基线 720 passed / 0 failed，另做T tt_solo 223 passed + qmt_sync）
python -m pytest prism/tests prism_web/tests strategy_web/tests tests -q \
    --import-mode=importlib --basetemp=D:\cc-joesph\pt_btNNN

# 做T（自包含包，含面板测试）
python -m pytest tt_solo -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_btNNN

# 做T 与旧 tt/ 的行为对照（退出码 0 = 只差那一处已批准例外）
python tt_solo\tools\compare_legacy.py

# 模拟盘
python -m prism.paper_daemon          # 守护
python -m prism.paper --once          # 只跑一轮

# 实盘（先演练，确认无误再加 --live）
python -m prism.live_daemon           # 默认 dry-run，零副作用
python -m prism.live_daemon --once    # 只跑一轮并打印
python -m prism.live_daemon --live    # 真实写信号（仍需桥端 armed.txt）

# 实盘接入前自检（只读，绝不下单）
python -m qmt.tools.live_check
python -m qmt.tools.live_check --quiet

# 回测
python -m backtest.cli --start 20260701 --end 20260731 --compare

# 因子体检
python -m prism.factor_check
```

---

## 六、还需你留意的事项

1. **重启服务**：本次重组改动了 `common.py` 等被运行中进程加载过的模块，
   正在跑的网页服务（:5000）需要**重启一次**才会用上新的 `runtime/log` 与 `runtime/state` 路径。
2. **根目录残留**（被老进程占用，重启后可手动删除）：
   - `log/`（旧的 `prism_web.log` / `watchdog.log`）
   - `.paper_account.json`（老进程仍按旧路径回写；内容与 `runtime/state/` 版本一致，
     若重启后发现根目录版本**更新时间更新**，请用它覆盖 `runtime/state/.paper_account.json`）
3. **子目录内的运行期文件**（历史遗留，已全部 gitignore，可择机清理）：
   - `prism_web/screen_result.json`、`prism_web/fundamental_cache.json`（Sep 4 旧副本）
   - `strategy_web/screen_result.json`、`strategy_web/fundamental_cache.json`（Aug 14 旧副本）
   - `strategy_web/manual_factors.json`（手工因子，**是数据，别误删**）、`strategy_web/perf/`（绩效存档）
   > 现行生效的基本面缓存是 `runtime/cache/fundamental_cache.json`，
   > 上面两个 `fundamental_cache.json` 是**过期重复副本**。
4. **两套回测并存**：`backtest/engine.py`（旧，简化版）与 `prism/backtest.py`（新，36 因子）。
   回测当前策略请用 `python -m backtest.cli`。
5. **`strategy_web/` 已属 legacy**，但 `ops/watchdog.py` 里仍配着它的拉起项（端口 5000 实际
   已被 `prism_web` 占用）。要彻底理清，建议下次单独处理 `ops/watchdog.py` 的服务清单。
