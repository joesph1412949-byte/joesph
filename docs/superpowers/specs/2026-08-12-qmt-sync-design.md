# QMT 账户数据同步层 (qmt-sync) 设计

日期: 2026-08-12
状态: 已通过头脑风暴(方案 A = 文件桥 + 同步层),待实现

## 背景与问题

Vibe-Trading 已部署在 D:\Vibe-Trading,但拿不到用户真实 A 股账户数据:

1. 现有 [qmt_signal_bridge_real.py](../../../qmt_signal_bridge_real.py) 只做"信号 JSON → 实盘下单"单向桥,**不做账户数据回流**。
2. xtquant 查询接口(`query_stock_trades` / `query_stock_orders` / `query_stock_positions` / `query_stock_asset`)**只返回当日数据**,无历史。
3. 用户目标:资金 / 历史持仓 / 当前持仓 / 全部成交**同步进 Vibe-Trading**,供 agent 分析账户、并为策略回测提供真实资金规模与真实成交历史。
4. 用户已确认接入方式 = **miniQMT / XtQuant**(D:\QMT\bin.x64\Lib\site-packages\xtquant 就位)。

## 目标

- 两层数据管道:一次性对账单导入铺历史基线 + 每日 xtquant 增量自动累积
- Vibe-Trading 内可查询账户实况(当前 / 历史),agent 对话可回答"我现在持仓/盈亏多少"
- 成交自动进交易日志 → 影子账户 / 行为分析
- 异常触发主动推送(企业微信 webhook,无 webhook 则退化为站内可查)
- 回测用真实资金规模 + 真实成交历史
- 全程只读账户数据;下单仍走现有 signal bridge,不在此范围

## 架构总览

```
┌─────────────────────────────┐    ┌──────────────────────────────────────┐
│ QMT 同步进程 (独立 Python)      │    │ Vibe-Trading (127.0.0.1:8899)         │
│  D:\cc-joesph\qmt_sync.py     │    │                                      │
│  · xtquant 轮询 资产/持仓(5s)  │──▶│  · 新工具 qmt_account_tool            │
│  · 成交/委托回调 实时落库        │SQLite│    (agent 对话查询, 只读)             │
│  · 一次性对账单导入(历史)       │    │  · 成交镜像进 trade journal → 影子账户 │
│  · 告警规则评估 + 企业微信推送   │    │                                      │
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

### 2. 两层历史数据管道

**一次性(历史基线): 券商对账单导入**
- 支持格式:同花顺 / 东方财富 / 通用 CSV(复用 Vibe-Trading `trade_journal_parsers` 解析逻辑)
- 用户操作:券商客户端/同花顺/东财导出交割单(单次可能只给 3~6 个月,分多段导出)→ 放 `D:\QMT_SYNC\import\` → 跑 `python qmt_sync.py --import-statement <file>`
- 导入即写入 `trades`,并触发历史持仓重建

**永久(每日增量): xtquant**
- `query_stock_asset` / `query_stock_positions` 轮询
- `on_stock_trade` / `on_stock_order` 回调实时落库
- 每日收盘后自动对账:当日成交 vs 当日持仓快照,偏差超阈值告警(防漏单)

### 3. 历史持仓重建算法

- 从 `trades` 按 (stock_code, 时间序) 回放:买入 `volume+=`,`cost` 按含费加权;卖出 `volume-=`
- 与当前持仓(xtquant 实时值)对比校准:偏差超阈值(如 10%)→ 告警"可能漏导入/含分红送股"
- 对账单中"送股/转增/红利"记录:解析为 `direction=BONUS`,volume 增加 + 成本摊薄;v1 支持常见格式,复杂分红走校准告警
- 导入覆盖率:单次导入只含导出日期段,提示"建议逐段导出覆盖全历史"

### 4. Vibe-Trading 查询工具 `qmt_account_tool`

- 新文件 `D:\Vibe-Trading\agent\src\tools\qmt_account_tool.py`,按现有 BaseTool 模式注册
- 操作:
  - `account_now`: 当前总资产 / 可用 / 市值 / 持仓明细
  - `account_history`: 指定日期区间资产与持仓
  - `trade_history`: 指定股票 / 时间段成交
  - `position_detail`: 单票成本 / 盈亏 / 占资产比
- 数据源:只读打开 `D:\QMT_SYNC\qmt_sync.db`;DB 不存在 → 工具禁用(提示先跑同步),fail-open 不报错

### 5. 告警规则(由同步进程评估, 不依赖 Vibe-Trading 存活)

- 同步进程在轮询循环内评估规则(数据最新, Vibe-Trading 关了也照常推)
- 规则配置 `D:\QMT_SYNC\alert_rules.json`(JSON, 可改):
  | 规则 | 默认 |
  |---|---|
  | `position_ratio` 单票市值/总资产 | > 30% |
  | `daily_loss` 当日亏损 | < -3% |
  | `stop_loss` 持仓跌破成本 | < -8% |
  | `position_change` 新开仓/清仓 | 触发 |
- 推送:同步进程直接 POST 企业微信 webhook(配置 `WECOM_WEBHOOK_URL`);未配置 → 仅写 `alerts` 表,Vibe-Trading 内可查
- 去重:同规则同标的 60 分钟内不重复推

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

- 数据全在本机;唯一外传 = 企业微信 webhook 的推送内容(可含账号/标的,由用户自决)
- 工具只读,同步进程只连本机 QMT,不监听公网端口

## 不在范围 (YAGNI)

- 不做下单 / 自动交易(现有 qmt_signal_bridge_real.py 已覆盖,保持单一职责)
- 融资融券特殊科目 v1 按近似处理,标注"近似的"字段
- 历史行情补齐(行情能力用 Vibe-Trading 现有 akshare/tushare/xtdata)
- 多券商 / 多账户并行(v1 单账户,表结构已带 account 字段可扩展)

## 测试 / 验收

1. **假数据生成器**:生成模拟成交序列 → 导入 → 重建持仓 → 与预期比对(覆盖分红送股用例)
2. **连真 QMT 冒烟**:轮询 asset/position 能取到当前账户数据,回调能捕获当日成交
3. **告警规则单测**:各规则阈值边界触发/不触发,60min 去重生效
4. **端到端**:对账单(一段)导入 → 历史持仓重建 → Vibe-Trading 对话里 `qmt_account_tool.account_now` 能回答
5. **fail-open**:QMT 未登录 / DB 缺失时,Vibe-Trading 正常启动,工具返回"未同步"而非崩溃

## 涉及文件

- 新增:`d:\cc-joesph\qmt_sync.py`(同步进程 + 导入 + 重建 + 告警)
- 新增:`D:\QMT_SYNC\qmt_sync.conf` / `alert_rules.json`(运行时生成)
- 修改:`D:\Vibe-Trading\agent\src\tools\qmt_account_tool.py`(新增查询工具 + 注册)
- 复用:Vibe-Trading `trade_journal_parsers`(对账单解析);企业微信推送为同步进程内直接 POST,不依赖 Vibe-Trading 渠道
