# MyQuant — 个人量化交易框架 设计文档

**日期**: 2026-07-27
**作者**: cc-joesph
**状态**: 设计中 → 待实现

---

## 1. 项目概述

MyQuant 是一个面向个人投资者的轻量级量化交易框架，支持 A 股 + 期货/期权的**策略研发 → 历史回测 → 实盘交易**完整链路，对接中金 miniQMT 实现实盘操作。

**运行环境**: WSL2 Ubuntu 24.04 / Python 3.12+ / RTX 4060 GPU（可选）

### 设计目标

- 每一层独立可替换，数据源/策略/券商可插拔
- 回测和实盘用**同一份策略代码**，验证通过直接上线
- 代码量控制在单人能维护的规模（~1500 行）
- Web 面板可视化 + 后台自动运行 + 异常通知

---

## 2. 架构总览

```
                         Web 面板 (Streamlit)
                         持仓/资金/信号/报告
                              ↑ ↓
┌────────────────────────── 策略层 ──────────────────────────┐
│  选股策略(因子/轮动)  +  择时策略(均线/布林/网格)           │
│  信号统一: {action, symbol, size, reason}                  │
│  策略读取账户状态 → 输出交易信号 → 交给引擎执行              │
└───────┬────────────────────────────────┬──────────────────┘
        ↓                                ↓
┌───────┴──────┐                 ┌───────┴──────────────────┐
│   数据层      │                 │      执行层               │
│              │                 ├──────────┬───────────────┤
│ AKShare ────→│                 │ 回测引擎   │ 实盘引擎       │
│ miniQMT ────→│                 │ Broker    │ miniQMT下单    │
│ DuckDB ◄─────│                 │ Recorder  │ 风控审批       │
│              │                 │ Report    │ 通知推送       │
└──────────────┘                 └──────────┴───────────────┘

数据流：数据层 → 策略层(读取) → 策略层(输出信号) → 执行层(成交) → 数据层(记录)
Web流：Web面板 ←→ 策略层/执行层（查询状态） ←→ 数据层（读取报告）
```

---

## 3. 数据层

### 3.1 数据源

| 数据源 | 用途 | 是否需要注册 |
|--------|------|:---:|
| **AKShare** | A 股/期货/期权日线、分钟历史数据 | ❌ 无需 |
| **miniQMT xtdata** | 实盘全推行情，AKShare 故障时降级 | ✅ 已有账号 |

### 3.2 统一接口

```python
from data.provider import DataProvider

dp = DataProvider()
# 历史数据
df = dp.get("000001.SZ", start="2024-01-01", end="2024-12-31", freq="1d")
# 都是 pd.DataFrame，不管底层走了哪个数据源
```

### 3.3 数据源降级策略

```
AKShare 优先 → 失败自动切 miniQMT xtdata → 都失败走本地 DuckDB 缓存
```

### 3.4 本地存储

| 引擎 | 存什么 |
|------|--------|
| **DuckDB** | 历史 K 线数据（列式、压缩、查询快） |
| **SQLite** | 元数据（股票列表、交易日历、策略配置） |

### 3.5 关键文件

```
data/
├── provider.py          # DataProvider 统一接口
├── sources/
│   ├── akshare.py       # AKShare 适配器
│   └── xtdata.py        # miniQMT 适配器
└── store.py             # DuckDB/SQLite 读写
```

---

## 4. 策略层

### 4.1 策略基类

```python
class Strategy:
    def init(self, params: dict) -> None:
        """加载参数、初始化技术指标"""

    def on_bar(self, bar: pd.DataFrame, account: Account) -> list[Signal]:
        """每根 K 线触发一次，返回信号列表"""

    def on_risk(self, signal: Signal, account: Account) -> bool:
        """风控检查，True=通过"""
```

### 4.2 信号格式

```python
@dataclass
class Signal:
    action: str      # "BUY" | "SELL" | "HOLD"
    symbol: str      # "000001.SZ"
    size: int        # 股数
    price: float     # 限价（None=市价）
    reason: str      # 产生原因，如 "ma5_cross_ma20"
```

### 4.3 内置策略

| 策略类别 | 策略名称 | 说明 |
|----------|---------|------|
| 因子选股 | RotationStrategy | 每 N 天算多因子分数，买入前 K 名 |
| 因子选股 | SectorRotationStrategy | 行业轮动，选强势行业买入 |
| 因子选股 | FactorAdapter | 接入 RD-Agent 挖掘的因子直接生成信号 |
| 择时 | MacdGoldenCross | 金叉买入、死叉卖出 |
| 择时 | BollingerBreakout | 布林带上轨突破买入 |
| 择时 | GridTrading | 固定间距网格交易 |
| 组合 | ComboStrategy | 选股+择时组合，选股决定池子，择时决定时机 |

### 4.4 关键文件

```
strategy/
├── base.py              # Strategy 基类 + Signal 定义
├── rotation.py          # 因子选股策略
├── timing.py            # 技术择时策略
└── factor_adapter.py    # RD-Agent 因子桥接
```

---

## 5. 回测层

### 5.1 事件驱动循环

```python
class BacktestEngine:
    def run(self, strategy, data, start_cash=100_000):
        for bar in data:
            signals = strategy.on_bar(bar, account)  # 策略决策
            for sig in signals:
                order = self.broker.execute(sig, account)  # 模拟成交
                self.recorder.log(order, account)          # 记录
            self.recorder.mark(bar, account)  # 每日净值
        return self.recorder.summarize()     # 生成报告
```

### 5.2 模拟券商

| 参数 | 默认值 | 说明 |
|------|:------:|------|
| 佣金 | 万 2.5 | 最低 5 元 |
| 印花税 | 1‰ | 仅卖出（A 股） |
| 滑点 | 0.1% | 市价单额外成本 |
| 涨跌停限制 | 自动 | 达到涨跌停板无法成交 |

### 5.3 报告指标

| 指标 | 说明 |
|------|------|
| 年化收益率 | Annual Return |
| 夏普比率 | Sharpe Ratio |
| 最大回撤 | Max Drawdown（含回撤区间） |
| 胜率 | Win Rate |
| 盈亏比 | Profit/Loss Ratio |
| 换手率 | Turnover |
| 权益曲线图 | 净值曲线 + 基准对比 |

### 5.4 关键文件

```
backtest/
├── engine.py            # 事件驱动回测主循环
├── broker.py            # 模拟券商
├── account.py           # 虚拟账户
├── recorder.py          # 交易记录 + 净值计算
└── report.py            # 报告生成（matplotlib 图表）
```

---

## 6. 实盘交易层

### 6.1 核心原则

**回测和实盘共用同一个策略文件**，区别只在底层引擎：

```python
# 回测
engine = BacktestEngine(strategy, data_source=AKShare)

# 实盘
engine = LiveEngine(strategy, data_source=miniQMT)
```

### 6.2 LiveEngine

```
┌────────────────────────────────────────────┐
│  LiveEngine                                │
│                                            │
│  1. xtdata.subscribe_quote() 订阅全推行情   │
│  2. 每根 K 线到达时触发 strategy.on_bar()   │
│  3. 信号 → xttrader.order_stock() 下单     │
│  4. 每次下单前 → RiskManager 审批          │
│  5. 异常 → notify 微信/邮件通知            │
└────────────────────────────────────────────┘
```

### 6.3 风控规则

| 规则 | 触发条件 | 动作 |
|------|---------|------|
| 单日最大亏损 | 当日浮亏 > 5% 总资产 | 停止交易，平仓所有持仓 |
| 单笔仓位限制 | 单只股票仓位 > 20% | 拒绝下单 |
| 连续亏损保护 | 连续 3 笔亏损 | 暂停 30 分钟，发通知 |
| 交易时段检查 | 非交易时间 | 拒绝下单 |

### 6.4 通知方式

- **ServerChan**: 微信推送（免费，注册即用）
- **邮件**: SMTP 备选

### 6.5 关键文件

```
live/
├── engine.py            # LiveEngine 实盘引擎
├── xt_trader.py         # miniQMT xttrader 封装
├── risk.py              # RiskManager 风控
└── notify.py            # 微信/邮件通知
```

---

## 7. Web 面板

### 7.1 技术选型

**Streamlit**（Python 原生，已安装），运行方式：

```bash
streamlit run web/app.py --server.port 8501
```

### 7.2 面板页面

| 页面 | 内容 |
|------|------|
| **总览** | 账户总资产、今日盈亏、持仓列表 |
| **信号** | 当前策略信号、历史信号记录 |
| **回测** | 上传策略 → 选择参数 → 跑回测 → 看报告 |
| **风控** | 风控状态、当日交易笔数、止损线 |
| **日志** | 实时运行日志，按级别筛选 |

### 7.3 关键文件

```
web/
└── app.py               # Streamlit 多页面应用
```

---

## 8. 运行时序

### 8.1 回测流程

```
用户启动 → 选择策略 + 参数 → 加载历史数据 → BacktestEngine 逐K线回测
→ 生成报告（收益/夏普/回撤）→ 在 Web 面板展示
```

### 8.2 实盘流程

```
用户启动 → 选择策略 + 参数 → LiveEngine 连接 miniQMT → 订阅全推行情
→ 每根K线触发策略 → 信号过风控 → xttrader 下单
→ 查询成交结果 → 更新 Web 面板 → 异常推送微信
```

---

## 9. 配置文件

```yaml
# config.yaml
data:
  primary: akshare          # 主数据源
  fallback: xtdata          # 降级数据源
  cache_dir: ./data/cache   # 本地缓存路径

strategy:
  rotation:
    top_k: 10               # 选股数量
    rebalance_freq: "W"     # 调仓频率: D/W/M
    factors: ["momentum_20", "volatility_60", "turnover_5"]

backtest:
  start_cash: 100000         # 初始资金
  commission: 0.00025        # 佣金率
  slippage: 0.001            # 滑点
  stamp_duty: 0.001          # 印花税（仅卖出）

live:
  max_position_pct: 0.2      # 单股最大仓位
  max_daily_loss_pct: 0.05   # 单日最大亏损
  max_consecutive_loss: 3    # 连续亏损上限

notify:
  server_chan_key: ""        # ServerChan 推送 Key
  email_smtp: ""             # 邮件 SMTP
```

---

## 10. 依赖

```
# requirements.txt
akshare>=1.14
pandas>=2.0
numpy>=1.24
duckdb>=0.10
sqlalchemy>=2.0
streamlit>=1.28
matplotlib>=3.7
plotly>=5.18
pyyaml>=6.0
# miniQMT 的 xtquant 从券商安装包获取，不放在 requirements.txt
```

---

## 11. 开发优先级

| 阶段 | 内容 | 预计时间 |
|:---:|------|:---:|
| **P0** | 数据层（AKShare + DuckDB） + 策略基类 | 1 天 |
| **P1** | 回测引擎 + 报告 | 1 天 |
| **P2** | 内置 3 个策略（旋转/均线/网格） | 0.5 天 |
| **P3** | 实盘层（miniQMT 对接 + 风控 + 通知） | 1 天 |
| **P4** | Web 面板 | 0.5 天 |

---

## 12. 技术决策记录

| 决策 | 选型 | 理由 |
|------|------|------|
| 回测引擎 | 自研 | 代码量 200-300 行，比学 Backtrader 快 |
| 数据存储 | DuckDB | Python 原生嵌入，列式压缩，SQL 查询，零配置 |
| Web 框架 | Streamlit | Python 原生，无需前端知识 |
| 事件循环 | 同步 for 循环 | 简单直观，不需要 asyncio 复杂度 |
| 不依赖 Backtrader/VNPY | ✅ | 自研框架代码量可控，每行都懂 |
