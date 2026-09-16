# cc-joesph — 策略可视化选股网页 (strategy visual web)

基于 **4 模型 / 24 因子** 量化打分系统构建的本地可视化选股网页。启动 QMT + miniQMT 后,网页实时读取全市场行情与涨停池,输出候选股评分清单、K线详情与模型对比。

> 仅限本机 localhost 使用,不连接任何外部服务,无实盘下单路径。

> **当前主入口已切换至 Prism**(新引擎, 见下文「Prism 引擎」章节): 网页走 `prism_web`, 选股/回测/信号走 `prism` 引擎; 旧 v04 网页(app.py/模板/静态资源)已于 2026-09-15 删除; 其数据模块更名为 `datasource/`, 由 Prism 复用。

## 功能

- **四个页面模块**:市场环境仪表盘 / 候选股评分列表 / 单股 K线+因子详情 / 模型对比
- **市场环境门槛**:节点模型先判情绪环境(`ENV_THRESHOLD=3`),达标才选股,否则提示空仓等待;节点不参与个股综合分
- **三模型个股评分**:首板 / 妖股 / 势能,综合分排序 + 绝对阈值 A–E 分级;`CANDIDATE_MIN_MODEL=3` 候选过滤
- **东财自动因子**:`fundamental.py` 从东方财富自动抓取 6 个个股因子(Y1 小市值 / Y5 多概念 / F7 题材新颖 / Y7 游资现身 / Y6 事件催化 / Y2 筹码干净),逐因子失败自动跳过(fail-open),按日缓存秒回
- **手填因子**:受限因子收敛为 `S1` / `S5`(机构流入,东财不可用) / `S7`,持久化到 `manual_factors.json`,重新选股自动合并
- **K线详情**:ECharts 蜡烛图 + 涨停日标注 + 60日均线 + 突破位

## 评分模型

| 模型 | 综合分权重(按排名) | 因子 |
|------|---------------------|------|
| 首板 first_board | 最强×0.60 | F1–F7 |
| 妖股 monster | 次强×0.25 | Y1–Y7 |
| 势能 momentum | 第三×0.15 | S1–S7 |
| 节点 node | 市场闸门(不参与综合分) | N1–N5 |

**综合分**(排序依据,上限 7.0)= 最强模型×0.60 + 次强×0.25 + 第三×0.15。与等级/强度同源(都看最强模型分),突出单模型绝活、拉开区分度,避免"均衡平庸"排太高。

**A–E 绝对阈值分级**(`models.py`,`best`=最强模型分,`second`=次强):
- A:`best ≥ 6`
- B:`best ≥ 5` 且 `second ≥ 3`
- C:`best ≥ 4`
- D:`best ≥ 3`
- E:其余(不展示)

**强弱区间**(无节点):`best ≥ 6` 极强 / `≥ 5` 强 / `≥ 4` 中等 / 其余弱。

**情绪阶段**(`classify_market`):≥5 高潮期 / 4 回暖期 / 3 冰点期 / <3 退潮期。

## Prism 引擎(新系统, 主入口)

Prism 是新一代量化引擎, 已接管网页选股、回测与交易信号生成(v04 的
models/factors/screen 实现被取代并删除, 数据模块保留在 `datasource/` 供复用)。
**主入口: 网页 `prism_web`, 引擎 `prism`**。

### 目录结构

```text
prism/                       # 引擎(纯库, 无网页依赖)
  registry.py                # 因子注册表: @factor 装饰器 + 扫描注册
  context.py                 # FactorContext: 因子统一上下文(数据缺失一律 None)
  engine.py                  # 策略引擎: load_strategy / run_screen / 打分分级
  data.py                    # 数据适配层: DataProvider(复用 datasource 数据层)
  market.py                  # 市场环境分类(classify_market)
  factors/                   # 因子库: factor_*.py 每文件一个因子(当前 26 个)
  strategies/                # 策略配置: default.json
  backtest.py                # 回测引擎(与实盘同一 engine, 含交易模拟)
  trader.py                  # 交易闭环: 选股结果 → 信号文件 → QMT 桥(需授权)
  factor_check.py            # 因子体检: python -m prism.factor_check
prism_web/                   # 网页(主入口): app.py + templates/ + static/
  tests/                     # 新 API + 旧路由保留测试
```

### 因子库用法

- **因子 = 一个文件**: `prism/factors/factor_<id>.py`, 每文件一个
  `@factor(id=..., name=..., category=...)` 装饰的 `compute(ctx)` 函数,
  返回 `{"score": 0~1, "note": "..."}`。
- **加因子 = 放文件 + 体检**: 复制 `prism/factors/_template.py` 写新因子,
  然后跑 `python -m prism.factor_check` —— 自动注册、校验签名与返回值、
  假数据跑通, 全部 PASS 才算入库。
- **现有 26 因子**: F1–F7(首板) / Y1–Y7(妖股) / S1–S7(势能) / N1–N5(市场节点),
  输出与旧实现逐因子比对一致(见 prism/tests/test_factor_migration.py)。
- 策略里引用因子用 id(如 `"F1"`), 未注册的 id 在 `load_strategy` 时抛
  `UnknownFactorError` 提前暴露配置错误。

### 策略配置

策略 = `prism/strategies/<id>.json`(示例: `default.json`), 字段:

- `id` / `name` / `description`: 元信息
- `market_gate`: 市场门槛 — `factors`(节点因子 id 列表) + `threshold`(达标分数, 默认 3)
- `scoring_models`: 打分模型列表 — 每个模型 `{id, name, weight, factors[]}`, 因子项
  支持简写 `"F1"` / 加权 `{"id": "F1", "weight": 1.0}` / 阈值 `{"id": "F1", "op": ">=", "threshold": 1}`
- `composite`: 综合分组合 — `mode`(`top3_weighted` / `sum` / `average`) + 权重
- `filters`: `candidate_min_model` 候选过滤(最强模型分下限)
- `sell_rules`: 止盈 / 止损 / 最大持有天数(回测与卖出巡检用)

```json
{
  "id": "default",
  "name": "默认四模型策略",
  "market_gate": {"threshold": 3, "factors": ["N1", "N2", "N3", "N4", "N5"]},
  "scoring_models": [
    {"id": "first_board", "weight": 0.60, "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]},
    {"id": "monster",     "weight": 0.25, "factors": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"]},
    {"id": "momentum",    "weight": 0.15, "factors": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [0.60, 0.25, 0.15], "cap": 7.0},
  "filters": {"candidate_min_model": 3},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}
}
```

### 回测 CLI 用法

与实盘共用同一 engine(load_strategy + compute_model_scores), 只加交易模拟层
(手续费万 2.5 / 滑点 0.1% / 卖出规则)。回测所需的因子必须先注册
(`prism.factor_check` 或 `reg.scan_factors`), 数据源用东财历史涨停池
(约保留最近 20 个交易日), 需联网:

```bash
python -m prism.factor_check                        # 因子体检(26 因子全 PASS)
python -c "from backtest_cli import zt_feed, kline_feed; \
from prism.backtest import Backtester; from prism.engine import load_strategy; \
from prism.strategies import STRATEGIES_DIR; from prism import registry as reg; \
import json, datetime; reg.scan_factors('prism.factors', force=True); \
s = load_strategy(STRATEGIES_DIR / 'default.json'); \
bt = Backtester(s, zt_feed, kline_feed); \
print(json.dumps(bt.run(datetime.date(2026,7,27), datetime.date(2026,8,13)), ensure_ascii=False))"
```

网页方式(推荐): `GET /api/backtest?strategy=default&start=20260727&end=20260813`。

### 交易闭环(信号生成 + 授权)

- **信号生成**: `prism.trader.run_daily(strategy, provider, env="sim")` —
  盘后选股 → 生成 BUY 信号 → 写入 `D:/QMT_SIGNALS/<env>/pending/BUY_*.json`。
- **桥协议**: 信号 JSON 消费字段 `order_id / action / stock_code / price / volume /
  account_id`, 与 `qmt_signal_bridge_real.py` 完全一致; `price=0` 桥端按对手价
  市价单处理(pr_type=2)。
- **真实盘授权**: 桥只在 `D:/QMT_SIGNALS/real/armed.txt` 存在且含当天日期时消费
  real 信号; `run_daily(env="real")` 逐候选跳过缺 `up_stop_price` 的候选
  (全部缺价则整体拒单并返回 error, 防止误按市价单)。
- **模拟盘冒烟**: 用假 provider(不连 QMT)驱动 `run_daily(env="sim")`, 验证
  选股 → 信号落盘 → 桥字段齐全全链路(见 Task 9 报告)。

### 自动化开关

- **一键暂停**: 存在 `D:/QMT_SIGNALS/paused` 文件 → `run_daily` 直接返回
  `paused=True`, 不生成新信号(盘后脚本/网页共用)。
- 网页开关(已实现 UI): 页面右上角"自动化"状态徽标 + 一键暂停/恢复按钮
  (读 `GET /api/automation`, 写 `POST {"paused": true|false}`)。

> **网页 UI 状态**: 自动化开关 UI 已上线; 因子库浏览 / 策略管理 / 回测页面
> 本次未实现 —— 对应 API(`/api/factors` `/api/strategies` `/api/strategy/<id>`
> `/api/backtest`)已就绪, UI 待后续迭代补齐。

## 环境要求

- **Windows** + 已安装 **QMT**(迅投)
- **Python 3.12**(QMT / miniQMT 自带,或本机任意 3.8+)
- `xtquant` **不要用 pip 安装** —— 它来自 QMT 的 Python 环境,运行前确保 `import xtquant` 可用(本机默认 `python` 即指向带 xtquant 的解释器)

pip 依赖(根目录 `requirements.txt`):

```bash
pip install flask requests numpy pandas
```

## 运行

以后每次启动就三步:

**1. 先开 QMT**(必需,否则选股取不到行情)
- 打开 QMT 交易终端并登录
- 启用 **miniQMT**(极简模式,行情端口 58610)

**2. 启动网页服务(Prism 主入口)**

```bash
python prism_web/app.py
```

看到 `Running on http://127.0.0.1:5000` 即启动成功。一键启动全部组件
(qmt_sync + prism_web + Vibe-Trading)可用 `python start_all.py` 或双击 `start_all.bat`。

> 网页入口统一为 `prism_web`(5000)与做T面板 `tt_solo/dashboard`(5011); v04 网页入口已移除。
> 做T策略自 2026-09-16 起是自包含包 `tt_solo/`(旧 `tt/` + 旧面板 `tt_web/`(5010) 已退役),
> 操作手册见 `tt_solo/README.md`。

**3. 浏览器访问**

打开 **http://localhost:5000**,点"选股"按钮。

### 注意事项

- 启动时若 QMT 未连接,页面仍可访问,但 `/api/screen` 返回 400(预期行为),页面会提示先开 QMT
- 服务只监听 `127.0.0.1`,不对外暴露
- **首次选股较慢**(每股约 4-5 次东财接口请求,涨停池 60 只约 1-2 分钟);**当天第二次点选股走缓存秒回**(缓存文件 `fundamental_cache.json`)
- **端口冲突**:5000 被占用时,用 `netstat -ano | grep 5000` 找到占用 PID 后 `taskkill //F //PID <pid>`
- 服务后台持续运行,关掉浏览器不影响服务

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 网页主页 |
| `/api/health` | GET | 健康检查,`qmt_connected` 字段 |
| `/api/screen` | POST | 跑完整选股流程(prism 引擎),返回市场环境 + 候选清单;支持 `strategy` 参数 |
| `/api/factors` | GET | 因子库列表(可带 `?category=`) |
| `/api/strategies` | GET | 策略列表 |
| `/api/strategy/<id>` | GET | 策略详情 |
| `/api/backtest` | GET | 回测(`?strategy=&start=&end=YYYYMMDD`) |
| `/api/automation` | GET/POST | 自动化暂停开关(读/写 paused 文件) |
| `/api/stock/<code>/kline` | GET | 个股 120 根 K线(OHLC + ma60 + 涨停价),如 `/api/stock/600519.SH/kline` |
| `/api/stock/<code>/manual` | GET/POST | 读写手动填写的受限因子 |
| `/api/perf` | GET | 绩效统计:按等级(A-E)的候选数/已结算/胜率/平均收益 |
| `/api/perf/backfill` | POST | 用最新行情回填未结算存档的 N 日收益(默认5日,需QMT) |
| `/api/market/limitup` | GET/POST | 涨停股列表快照(刷新 + 秒读) |
| `/api/market/kline` / `/api/market/tick` | GET | 多股 K线 / 盘口 |

## 改进模块(2026-08)

- **题材主线聚类**(`eastmoney.py aggregate_by_theme`):东财涨停池按 hybk 题材聚合,
  识别当日主线题材(涨停家数/连板高度排序),输出 `market.top_themes` 供页面展示;
  F4/S6 板块共振从纯申万行业扩展到**题材共振**(题材优先,申万兜底)。
- **绩效追踪**(`perf_store.py`):每次选股自动按日期存档候选清单(`runtime/state/perf/YYYYMMDD.json`),
  之后用行情回填 N 日实际涨跌,按 A-E 等级统计胜率/平均收益,验证打分体系有效性。
- **回测框架**(`backtest.py` + `backtest_cli.py`):用东财历史涨停池回放简化选股规则
  (环境门槛 + 主线题材 + 连板高度),统计胜率/盈亏比/最大回撤,支持参数网格对比。
  无需 QMT,仅依赖东财公开接口(注意:历史涨停池约保留最近 20 个交易日):

  ```bash
  python backtest_cli.py --start 20260727 --end 20260813 --min-limit 30 --picks 3 --hold 3
  python backtest_cli.py --start 20260727 --end 20260813 --compare    # 参数网格对比
  ```

- **工程整理**:公共配置收敛到 `common.py`(路径/行业池/后缀规则/涨跌停幅度),
  统一日志 `setup_logging`(按天滚动, `log/` 目录);
  `watchdog.py` 进程守护(监控 5000/8899/5899, 挂了自动拉起),
  `install_watchdog.bat`(管理员运行一次)注册开机自启。
- **卖出策略**(`exit_rules.py` + `strategy_close_pick.py exit`):买入发单后自动记账到
  `positions.json`;`exit` 命令拉持仓最新价,按 **止盈(+8%)/止损(-5%)/持有 N 天强制平仓**
  三规则判定,触发则发 **SELL** 信号走同一信号通道(桥已支持卖出):

  ```bash
  python strategy_close_pick.py exit                    # 默认规则巡检
  python strategy_close_pick.py exit --take-profit 0.10 --stop-loss 0.06 --hold-days 3
  python strategy_close_pick.py exit --price 12.50      # 指定 SELL 委托价(默认市价)
  ```

- **仓位/资金管理**:`send` 时查询账户可用资金,单只按 `POSITION_RATIO`(默认 30%)
  预算计算股数(向下取整 100 股倍数);资金通道不可用时回退固定 `VOLUME`。

## 测试

全部离线(无真实网络、无需 QMT)**808 个测试全绿**(2026-09-15 结构归位时的口径: datasource 的
123 个失联测试已纳入, 当时的 `tt/tests` 与根级测试一并跑)。做T 自 2026-09-16 起是自包含包
`tt_solo/`(另有 223 例, 含面板测试), **单独跑**; 旧 `tt/` 删除后下面第一条命令去掉
`tt/tests`、数字随之下调。注意 `prism/tests/` 与根 `tests/` 各有一个
`test_backtest.py`(同名、不同对象), 同命令跑时由 importlib 模式按路径区分:

```bash
python -m pytest prism/tests prism_web/tests datasource/tests tt/tests tests -q   # 808: 全仓一次跑完
python -m pytest tt_solo -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNNN  # 做T 223
python -m prism.factor_check                         # 因子体检: 26 因子全 PASS
```

合计 151 + 17 + 160 = **328 tests**。

## 说明

- 本应用**不做真实下单**。用户明确不使用 `.REAL_ARMED` 安全臂机制,依赖 QMT 自带安全保护。
- **真实盘桥安全闸门**(`qmt_signal_bridge_real.py`):
  - **授权文件**:只有 `D:/QMT_SIGNALS/real/armed.txt` 存在且内容含当天日期(`YYYYMMDD`)时,pending 信号才会被消费下单,防止误触;
  - **当日去重**:同一股票代码每个自然日最多下单一次(记录在 `D:/QMT_SIGNALS/real/placed_today.json`),防止重复发单;
  - `strategy_close_pick.py send` 会校验候选清单生成日期(默认 3 天内,覆盖周末;过期需 `--force` 强制发送)。
- 数据源:个股K线 / 全市场盘口 / 行业板块来自 xtquant(miniQMT);N1/N3/N4 来自东方财富公开涨停池接口。
- 手动因子存于 `runtime/state/manual_factors.json`;损坏的 JSON 会被保留为 `manual_factors.json.corrupt-<时间戳>` 并告警,不静默覆盖。
