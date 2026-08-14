# Prism — 可配置因子库策略系统 — 设计文档

> 日期: 2026-08-14
> 状态: 待用户审阅
> 前置: 已有 strategy_web(固定 4 模型 24 因子)、回测框架(backtest.py 简化规则)、
>        QMT 信号桥(qmt_signal_bridge_real.py)、绩效追踪(perf_store.py)、
>        177 个自动化测试

## 1. 目标

把固定"4 模型 × 24 因子"的选股系统升级为**可配置策略系统**,实现:

1. **因子库**:单文件单因子,自动扫描注册;加因子 = 丢一个文件,零改动其他代码
2. **策略配置**:JSON 定义策略(选哪些因子、权重、阈值、组合方式、卖点);从因子库挑因子组策略,不写代码
3. **自然语言生成因子**:用户用中文描述因子 → AI 按因子协议生成代码 → 自动体检 → 进因子库 → 回测验证
4. **回测完善**:回测跑**真实策略**(完整因子, 与实盘同引擎),含手续费/滑点/仓位/止盈止损
5. **自动化交易**:策略定时选股 → 信号自动生成 → 桥自动下单(保留授权文件安全闸门)
6. **系统改名**:strategy_web → **Prism**(棱镜: 把市场分解成因子光谱)

最终目的:让 Prism 接管 miniQMT 交易,实现"描述因子 → 组策略 → 回测验证 → 自动交易"的完整闭环。

## 2. 架构总览(方案 A: 引擎-插件分层)

```
D:\cc-joesph\
├── prism\                        # ★ 新引擎(独立包, 网页/CLI/回测/交易共用)
│   ├── __init__.py
│   ├── registry.py               # 因子注册表: 扫描 factors/ 自动注册(名字→实现)
│   ├── context.py                # 因子上下文: 所有因子收到的统一数据包
│   ├── engine.py                 # 核心编排: 按策略配置 算因子→评分→选股
│   ├── backtest.py               # 回测: 复用 engine + 交易模拟(手续费/滑点/仓位/止盈止损)
│   ├── trader.py                 # 交易: 选股结果→信号文件→QMT桥(需授权)
│   ├── factor_check.py           # 因子体检: 注册/签名/假数据跑通 自动验证
│   ├── factors\                  # ★ 因子库: 一个因子一个文件, 自动注册
│   │   ├── __init__.py           #   扫描 factor_*.py 自动注册
│   │   ├── _template.py          #   空白因子模板(供 AI 生成/用户复制)
│   │   ├── factor_f1_first_board.py
│   │   ├── factor_y3_volume_spike.py
│   │   └── ...(24 个现有因子逐一拆入, 借机重构 S5 等)
│   └── strategies\               # ★ 策略配置: 一个 JSON 一个策略
│       ├── default.json          #   兼容现有 4 模型 24 因子的默认策略
│       └── (用户以后自行新增)
├── prism_web\                    # ★ 网页(由 strategy_web 改造, 展示层)
│   ├── app.py                    # Flask API: 因子库/策略/回测/绩效/自动化开关
│   └── templates\ static\        #   页面: 因子库浏览 / 策略管理 / 回测 / 绩效
├── qmt_signal_bridge_real.py     # 桥(基本不动, 继续负责实际下单)
├── common.py / watchdog.py       # 已有公共组件
└── backtest_cli.py               # 退役(被 prism 回测取代)
```

**核心原则**:
- 因子、策略、回测、交易四层独立,通过统一数据结构通信
- **回测与实盘共用同一引擎**(同一 registry + 同一策略配置)→ 结果可信
- 网页只做展示与配置入口,不承载策略逻辑

## 3. 因子协议

### 3.1 一个因子的完整长相

```python
# prism/factors/factor_y3_volume_spike.py
# 名称: Y3 倍量突破 | 类别: 妖股 | 说明: 涨停日量能 >= 前5日均量 x3
from prism.registry import factor

@factor(
    id="Y3",                  # 因子编号(策略配置里引用它)
    name="倍量突破",           # 网页上显示的中文名
    category="monster",       # 归属模型类别(首板/妖股/势能/节点/通用)
    description="涨停日成交量达到前5日均量的3倍以上",
)
def compute(ctx):
    """ctx 是统一数据包, 因子只取自己需要的字段, 不用管数据从哪来。"""
    kline = ctx.kline
    if kline is None or len(kline) < 6:
        return {"score": 0, "note": "K线不足"}
    vols = kline["volume"].tolist()
    today_v = vols[-1]
    ma5_prev = sum(vols[-6:-1]) / 5
    if ma5_prev <= 0:
        return {"score": 0, "note": "均量为0"}
    hit = today_v >= ma5_prev * 3
    return {"score": 1 if hit else 0,
            "note": "今日量/前5日均量=%.1f倍" % (today_v / ma5_prev)}
```

### 3.2 协议规则(仅 3 条)

| 要素 | 规则 |
|---|---|
| **注册** | `@factor(id=..., name=..., category=..., description=...)` 装饰器 |
| **函数** | `compute(ctx)` → 返回 `{"score": 0或1或数值, "note": "说明"}` |
| **数据** | 只从 `ctx` 取;拿不到 → 返回 `{"score": 0, "note": "数据缺失"}`(fail-open, 永不崩溃) |

### 3.3 因子上下文(ctx)内容

```
ctx.kline        日K线(DataFrame: open/high/low/close/volume/amount)
ctx.tick         实时盘口(封单金额/封板时间/买卖五档)
ctx.index_kline  大盘K线
ctx.sector_map   板块/题材归属
ctx.limit_ups    当日涨停池
ctx.em           东财市场数据(题材聚合/连板数/昨日池)
ctx.fund         东财个股因子(概念/龙虎榜/股东户数/公告/融资)
ctx.manual       网页手填因子
ctx.float_mv     流通市值
ctx.code         股票代码
ctx.last/last_close/up_price/sealed   常用盘口速取字段
```

### 3.4 输出形态: 布尔与数值

- **布尔因子**: `{"score": 1|0, "note": "..."}` → 命中/未命中(现有 F1-Y7 均为布尔)
- **数值因子**: `{"score": 2.5, "note": "..."}` → 连续值(换手率/量比等),策略配置里配阈值判定

### 3.5 数据源途径(因子数据从哪来)

| 途径 | 内容 | 现状 |
|---|---|---|
| QMT/xtquant | 行情/盘口/K线/板块/详情 | ✅ 已有 |
| 东方财富 | 涨停池/概念/龙虎榜/股东户数/公告/融资 | ✅ 已有 |
| 网页手填 | 受限因子 | ✅ 已有 |
| 未来扩展 | 财报/舆情/Level-2 等 | 预留接口 |

## 4. 策略配置(JSON)

### 4.1 默认策略 `prism/strategies/default.json`

```json
{
  "id": "default",
  "name": "默认四模型策略",
  "description": "兼容现有 4 模型 24 因子的默认策略",
  "market_gate": {
    "model": "node",
    "threshold": 3,
    "factors": ["N1", "N2", "N3", "N4", "N5"]
  },
  "scoring_models": [
    {"id": "first_board", "name": "首板", "weight": 0.60,
     "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]},
    {"id": "monster", "name": "妖股", "weight": 0.25,
     "factors": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"]},
    {"id": "momentum", "name": "势能", "weight": 0.15,
     "factors": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [0.60, 0.25, 0.15], "cap": 7.0},
  "filters": {"candidate_min_model": 3, "environment_threshold": 3},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}
}
```

### 4.2 因子条目三种形态

```json
{"id": "Y3"}                              // 简写: 权重1, 无阈值
{"id": "F4", "weight": 2.0}               // 加权
{"id": "换手率", "op": ">", "threshold": 5.0}   // 数值因子阈值判定
```

### 4.3 组合方式 composite.mode

| mode | 含义 |
|---|---|
| `top3_weighted` | 现有: 最强×0.60 + 次强×0.25 + 第三×0.15(上限7.0) |
| `sum` | 各模型分简单加和 |
| `max` | 只取最强模型分 |
| `average` | 各模型分平均 |

### 4.4 用户操作(不写代码)

- 换因子: 改 JSON 里的因子 id
- 调权重: 改 weight
- 建新策略: 复制 JSON 改名改内容
- 新策略即时出现在网页

## 5. 回测引擎(prism/backtest.py)

```
输入: 策略 JSON + 日期区间 + 数据源(东财历史: 涨停池/K线)
流程: 逐日回放 →
  拉当日涨停池 → 跑 market_gate(节点因子) → 不达标空仓
  → 对每只候选跑完整策略因子 → 模型分 → 综合分 → 过滤 → 排序 → 选股
  → 模拟成交: 手续费(默认万2.5) + 滑点(默认0.1%) + 仓位控制(单只上限)
  → 按策略 sell_rules 止盈/止损/T+N 卖出
输出: 胜率 / 盈亏比 / 平均收益 / 最大回撤 / 收益曲线 / 每日明细 / 参数网格对比
```

- **与现有 backtest.py 的区别**: 现有回测用"涨停家数+主线题材"简化规则;新回测跑**真实策略配置**(与实盘同一引擎、同一因子、同一配置),回测选股 = 实盘选股,结果可信。
- 交易模拟细节: 手续费、滑点、仓位上限、止盈/止损/T+N 卖出(复用 exit_rules 规则)。

## 6. 自动化交易闭环(prism/trader.py)

```
盘后定时(计划任务) → trader.py
  → 跑策略选股 → 结果存盘(同时进 perf_store 绩效追踪)
  → 生成买入信号 → D:/QMT_SIGNALS/real/pending/  (授权文件存在才被桥消费)
  → 桥每2秒扫 → 自动下单
盘中/盘后 → exit 巡检(已有) → 止盈止损 SELL 信号 → 桥卖出
安全:
  - 授权文件机制保留(D:/QMT_SIGNALS/real/armed.txt 含当天日期才下单)
  - 每次下单记录留痕(桥已有 done/failed/trades 目录)
  - 网页一键暂停(停止生成信号, 但已发信号仍由桥执行)
  - 信号文件带策略 id 与 created_at, 可追溯
```

## 7. 迁移计划(4 步, 每步可独立验证)

| 步骤 | 内容 | 验证 |
|---|---|---|
| **1. 骨架** | prism/ 包 + registry + context + engine 空壳 + _template.py + factor_check | 新增单元测试 |
| **2. 因子迁移** | 24 个现有因子逐一拆入 factors/(保持 id/逻辑一致);借机重构:S5 尝试接入东财融资融券接口(若探针验证不可用则保持 fail-open 手填, 与现状一致)、F2 时间解析等公共逻辑统一进 prism 公共工具 | 旧测试→新引擎跑出相同结果(逐因子比对) |
| **3. 回测+交易** | backtest 完整版(真实策略+交易模拟) + trader 信号生成 | 回测测试 + 模拟盘(sim)验证 |
| **4. 网页改造** | strategy_web → prism_web: 因子库浏览/策略管理/回测页面/自动化开关 | 网页测试 + 手工验证 |

- 每步完成后旧系统照常运行;第 4 步收尾整体切换。
- 全程 177 个现有测试守护 + 新增测试。
- 数据兼容: 现有 manual_factors.json / fundamental_cache.json / perf/ 存档继续使用。

## 8. 命名

- 系统名: **Prism**(棱镜——把市场光线分解成因子光谱)
- 目录: `prism/`(引擎) + `prism_web/`(网页, 由 strategy_web 改造)
- 原 strategy_web 在迁移完成后退役(代码并入 prism_web, 保留历史提交可查)

## 9. 测试策略

- 因子协议测试: registry 扫描/注册、ctx 缺数据 fail-open、布尔/数值输出
- 因子迁移测试: 每个因子在假数据下输出与旧实现一致(逐因子比对测试)
- 策略配置测试: default.json 加载、未知因子报错、数值阈值判定
- 回测测试: 假数据回放、手续费/滑点/卖出规则、参数网格
- 交易测试: 信号文件格式、授权检查、暂停开关
- 网页测试: 因子库/策略/回测 API
- 目标: 全部离线可跑(除明确标注需 QMT/网络的集成测试)

## 10. 范围外(以后再说)

- Python 插件式高级策略(方案 C 的能力)
- 参数自动寻优(网格/随机搜索权重阈值)
- 更多数据源(财报/舆情/Level-2)
- 多账号、多策略并行交易
