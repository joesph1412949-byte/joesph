# QMT 账户数据同步层 (qmt-sync) 设计

日期: 2026-08-12
状态: 头脑风暴通过(方案 A = 文件桥 + 同步层);审阅后 v1 范围已收窄(见"v1 范围"),待实现

## 背景与问题

Vibe-Trading 已部署在 D:\Vibe-Trading,但拿不到用户真实 A 股账户数据:

1. 现有 [qmt_signal_bridge_real.py](../../../qmt_signal_bridge_real.py) 只做"信号 JSON → 实盘下单"单向桥,**不做账户数据回流**。
2. xtquant 查询接口(`query_stock_trades` / `query_stock_orders` / `query_stock_positions` / `query_stock_asset`)**只返回当日数据**,无历史。
3. 用户目标:资金 / 历史持仓 / 当前持仓 / 全部成交**同步进 Vibe-Trading**,供 agent 分析账户、并为策略回测提供真实资金规模与真实成交历史。
4. 用户已确认接入方式 = **miniQMT / XtQuant**(D:\QMT\bin.x64\Lib\site-packages\xtquant 就位)。

## v1 范围(用户审阅后收窄, 先做实时监控)

- **v1 只做实时监控**:当前资金 / 当前持仓 / 当日成交与委托,通过 xtquant 增量自动累积落库
- 告警 = **站内查询**(无企业微信,不做 webhook 外推)
- **对账单导入 + 历史持仓重建 → 推迟到 Phase 2**(用户暂不需要,待定)
- 其余目标(影子账户、回测供数)依赖 v1 数据落地后逐步启用

## 目标

- v1: xtquant 实时同步 资金/持仓/当日成交/委托 到 SQLite
- Vibe-Trading 内可查询账户实况(当前 + 累计以来),agent 对话可回答"我现在持仓/盈亏多少"
- 成交记录可镜像进交易日志,为后续影子账户 / 行为分析铺路
- 站内告警:规则触发写 `alerts` 表,Vibe-Trading 工具可查
- 全程只读账户数据;下单仍走现有 signal bridge,不在此范围

## 架构总览

```
┌─────────────────────────────┐    ┌──────────────────────────────────────┐
│ QMT 同步进程 (独立 Python)      │    │ Vibe-Trading (127.0.0.1:8899)         │
│  D:\cc-joesph\qmt_sync.py     │    │                                      │
│  · xtquant 轮询 资产/持仓(5s)  │──▶│  · 新工具 qmt_account_tool            │
│  · 成交/委托回调 实时落库        │SQLite│    (agent 对话查询, 只读)             │
│  · 告警规则评估 → 写 alerts 表  │    │  · 成交镜像进 trade journal → 影子账户 │
└─────────────────────────────┘    └──────────────────────────────────────┘
      数据目录 D:\QMT_SYNC\qmt_sync.db (SQLite)
```

全部在本机 Windows,文件 + SQLite,无网络依赖(除企业微信 webhook 外传)。

## 设计

### 1. 存储: SQLite `D:\QMT_SYNC\qmt_sync.db`

由同步进程独占写,Vibe-Trading 工具只读打开。

| 表 | 字段(核心) | 写入时机 |
|---|---|---|
| `account_assets` | account, total_asset, cash, market_value, frozen, update_time | 轮询, 间隔可配(默认 5s) |
| `positions` | account, stock_code, volume, available, cost_price, market_price, market_value, profit, update_time | 轮询 upsert(默认 5s) |
| `trades` | account, trade_id, stock_code, direction, price, volume, amount, fee, occur_time | 成交回调 + 对账单导入 |
| `orders` | account, order_id, stock_code, direction, price, volume, status, insert_time | 委托回调 |
| `watchlist` | stock_code, note | 手动配置(自选股) |
| `alerts` | rule, stock_code, message, triggered_at | 告警触发, 60min 去重 |

### 2. 数据管道(v1 = 每日增量; Phase 2 = 历史导入)

**v1(核心): xtquant 每日增量**
- `query_stock_asset` / `query_stock_positions` 轮询(默认 5s)
- `on_stock_trade` / `on_stock_order` 回调实时落库
- 每日收盘后自动对账:当日成交 vs 当日持仓快照,偏差超阈值写 `alerts`(防漏单)

**Phase 2(推迟, 本期不做): 券商对账单导入铺历史基线**
- 支持格式:同花顺 / 东方财富 / 通用 CSV(复用 Vibe-Trading `trade_journal_parsers`)
- 导入即写入 `trades`,并触发历史持仓重建
- 入口预留:CLI `--import-statement <file>`,本期只占位不实现

### 3. 历史持仓重建(Phase 2, 本期不做)

- 从 `trades` 按 (stock_code, 时间序) 回放:买入 `volume+=`,`cost` 按含费加权;卖出 `volume-=`
- 与当前持仓(xtquant 实时值)对比校准:偏差超阈值 → 告警(防漏导入/分红送股)
- v1 不重建历史持仓;`positions` 表只存 xtquant 实时快照

### 4. Vibe-Trading 查询工具 `qmt_account_tool`

- 新文件 `D:\Vibe-Trading\agent\src\tools\qmt_account_tool.py`,按现有 BaseTool 模式注册
- 操作:
  - `account_now`: 当前总资产 / 可用 / 市值 / 持仓明细
  - `account_history`: 指定日期区间资产与持仓
  - `trade_history`: 指定股票 / 时间段成交
  - `position_detail`: 单票成本 / 盈亏 / 占资产比
- 数据源:只读打开 `D:\QMT_SYNC\qmt_sync.db`;DB 不存在 → 工具禁用(提示先跑同步),fail-open 不报错

### 5. 告警规则(站内, 由同步进程评估)

- 同步进程在轮询循环内评估规则(数据最新, Vibe-Trading 关了也照常落库)
- 规则配置 `D:\QMT_SYNC\alert_rules.json`(JSON, 可改):
  | 规则 | 默认 |
  |---|---|
  | `position_ratio` 单票市值/总资产 | > 30% |
  | `daily_loss` 当日亏损 | < -3% |
  | `stop_loss` 持仓跌破成本 | < -8% |
  | `position_change` 新开仓/清仓 | 触发 |
- **站内**:触发写 `alerts` 表;`qmt_account_tool` 提供 `alerts` 操作,agent 对话可查"最近有哪些告警"
- **不做企业微信/webhook 外推**(用户无企业微信,v1 明确排除)
- 去重:同规则同标的 60 分钟内不重复写

### 6. 回测接入真实数据

- 回测启动时读 `account_assets` 最新总资产作为初始资金(真实仓位规模)
- `trade_history` 作为策略基准 / 实盘 vs 回测对比
- 当前持仓作为影子账户起点 / 前向测试初始状态
- 本设计只提供数据接入;回测本身用 Vibe-Trading 现有 backtest / shadow_account 模块

### 7. 运行与配置

- 依赖 xtquant:运行前 `set PYTHONPATH=D:\QMT\bin.x64\Lib\site-packages` 或用 QMT 自带 python
- 需要 QMT 终端已登录且开启 miniQMT 模式(用户侧确认)
- 启动:`python qmt_sync.py [--import-statement <file>] [--watch]`
- 可选:任务计划程序开机自启
- 所有配置走 `D:\QMT_SYNC\qmt_sync.conf`(或 .env 同风格),密钥不入库

### 8. 安全

- 数据全在本机,无任何外传
- 工具只读,同步进程只连本机 QMT,不监听公网端口

## 不在范围 (YAGNI)

- 不做下单 / 自动交易(现有 qmt_signal_bridge_real.py 已覆盖,保持单一职责)
- 融资融券特殊科目 v1 按近似处理,标注"近似的"字段
- 历史行情补齐(行情能力用 Vibe-Trading 现有 akshare/tushare/xtdata)
- 多券商 / 多账户并行(v1 单账户,表结构已带 account 字段可扩展)

## 测试 / 验收(v1)

1. **连真 QMT 冒烟**:轮询 asset/position 能取到当前账户数据,回调能捕获当日成交
2. **告警规则单测**:各规则阈值边界触发/不触发,60min 去重生效
3. **端到端**:跑起同步进程后,Vibe-Trading 对话里 `qmt_account_tool.account_now` 能回答当前持仓/盈亏
4. **fail-open**:QMT 未登录 / DB 缺失时,Vibe-Trading 正常启动,工具返回"未同步"而非崩溃
5. **Phase 2 预留**:`--import-statement` 入口存在但明确返回"Phase 2 未实现",不静默产生假数据

## 涉及文件

- 新增:`d:\cc-joesph\qmt_sync.py`(v1 增量同步 + 站内告警;Phase 2 导入/重建仅占位)
- 新增:`D:\QMT_SYNC\qmt_sync.conf` / `alert_rules.json`(运行时生成)
- 修改:`D:\Vibe-Trading\agent\src\tools\qmt_account_tool.py`(新增查询工具 + 注册)
- 复用:v1 不依赖 Vibe-Trading 的渠道/解析器;对账单解析器在 Phase 2 才用
