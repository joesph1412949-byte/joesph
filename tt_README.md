# tt — A股「底仓 + 浮仓」做T策略（miniQMT 实盘出口）

在 cc-joesph 既有实盘链路上追加的一个**日内高抛低吸**策略。把前面研究里
"波动率定带宽、日重置网格、趋势态停做"的结论，落成可跑、可看、可急停的代码。

- 策略 id：`tt_grid_v1`
- 标的：长江电力 / 中国海油A / 中国神华A（松发股份默认停用）
- 模式：**默认演练（dry-run）**，不写任何信号

---

## 1. 快速开始

```bash
cd D:/cc-joesph

# ① 离线演练（样本行情 + 纸面账户，零副作用，非交易时段也能跑）
python -m tt.daemon --once --sample --fake-now

# ② 用真实行情演练（只读 QMT 行情与账户，仍不下单）
python -m tt.daemon --once

# ③ 持续守护（演练）
python -m tt.daemon --interval 5

# ④ 监控面板
python tt_web/app.py            # http://127.0.0.1:5010
```

Windows 可双击 `启动做T监控台.bat` / `启动做T守护.bat`。

### 直连模式（推荐 — 外部 Python 直接下单）

**为什么用直连**：实测 `xtquant.xttrader` 在本机可直接 `connect()==0`，
且 `order_stock / order_stock_async / cancel_order_stock*` 全部可用，因此
**无需把桥脚本粘进 QMT 策略编辑器**。那条老路受 GBK 编码约束、无法调试、
无法回归测试，且 QMT 内 `python/SIGNALBRIDGE.py` 是**密文**、无法核对版本。

```bash
# 直连演练（连真实账户只读 + 算意图，dry-run 默认开，绝不下单）
python -m tt.daemon --direct --once

# 直连守护（dry-run，长期挂着看它想干什么）
python -m tt.daemon --direct --interval 5

# 直连实盘（需 paused 不存在 + real/armed.txt 含今日日期）
python -m tt.daemon --direct --live --interval 5
```

Windows 双击：`启动做T直连守护.bat`（演练）/ `启动做T实盘直连.bat`（实盘）。

**每日放行条**（人工闸门，替代桥端的 armed 检查）：

```bash
python tt/arm_today.py            # 写今日放行条(并确保无急停)
python tt/arm_today.py --status   # 只看状态
python tt/arm_today.py --pause    # 一键急停
python tt/arm_today.py --resume   # 解除急停
python tt/arm_today.py --disarm   # 撤销放行条
```

Windows 可双击 `做T-今日放行.bat`。

> **每天必须重新放行**：`armed.txt` 里的日期是昨天的 → 今天不放行、零报单。
> 这是刻意的设计：守护可以常驻，但**每天必须有人点头**。

### 信号文件通道（旧路，仍保留 — 即"大 QMT 跑桥"）

> **两条通道不能同时开**：`--direct` 与桥同时跑会**双重下单**。
> 切换到本通道必须去掉 `--direct`。

大 QMT 内嵌跑的法律：把 `qmt/bridge/signal_bridge_real.py` 粘进 QMT 策略编辑器，
QMT 自己调 `passorder` 消费信号文件。桥端 6 个参数与 `passorder` 填法、
以及 A1（QMT 内原生写网格）的参数对应，**见 `docs/大QMT网格参数手册.md`**。

要点速记：
- QMT 内是 **GBK**，桥脚本**刻意全用英文注释**，别改成中文
- `FILE_MIN_AGE` 建议 `1.0 → 0.2`（做T对延迟敏感；tt 侧已原子写，1 秒是纯等待）
- 日去重已从 `stock_code` 改为 **`order_id`**（原逻辑会让每票每天只放行 1 单，多档全废）
- `passorder` 的 `opType` 是 **0/1**，与外部 API 的 `STOCK_BUY=23/24` **不是一套**，别混

```bash
# ⑤ 模拟通道演练（信号写 D:/QMT_SIGNALS/sim/，仍不下发）
python -m tt.daemon --once --sample --fake-now --env sim

# ⑥ 模拟通道真实下发（需同时满足：paused 不存在 + sim/armed.txt 含当日日期）
python -m tt.daemon --interval 5 --env sim --live
```

Windows 可双击 `启动做T模拟守护.bat`（默认 DRY-RUN，要真下发自己加 `--live`）。

`--env` 只决定**信号写到哪个队列**（`real` 或 `sim`），不决定桥端连的是哪个账户。

> **⚠️ 跑模拟通道前必须知道**
> `qmt_signal_bridge_demo.py` 的 `_check_safety()` 在 sim 模式下直接返回 `True`
> （注释 `simulation mode (safe)`），**它不检查账户**——下单用的是 **QMT 当前登录的那个账户**。
> 所以：**启动该桥之前，务必确认 QMT 登录的是模拟账户，不是真实资金账户。**
> 否则模拟信号会打到真实账户上。
>
> 另外注意：tt 在 sim 模式下**仍然要求 `armed` 闸门**
> （须写 `D:/QMT_SIGNALS/sim/armed.txt`，内容含当日 `YYYYMMDD`）。
> 生成侧比桥端更严，是刻意的——不能因为桥松就跟着松。

---

## 2. 安全模型（三道闸门 + 十一风控）

**闸门**（两条通道都串联；任一不满足只记录、不下发）：

| 闸门 | 位置 | 说明 |
|---|---|---|
| `dry_run` | 配置 / `--live` | 默认 True，只算不落盘/不报单 |
| `paused` | `D:/QMT_SIGNALS/paused` | 一键急停，存在即整体停发 |
| `armed` | `D:/QMT_SIGNALS/<env>/armed.txt` | 须含当日 `YYYYMMDD`，最终闸门 |

**风控**（`tt/risk.py`，按顺序短路，任一不过即拒并记录原因码）：

| 顺序 | 检查 | 原因码 |
|---|---|---|
| 1 | 熔断器 / 标的启用 | `CIRCUIT_BREAKER` / `SYMBOL_DISABLED` |
| 2 | 交易时段（收敛时段只允许归位买入） | `SESSION_CLOSED` / `SESSION_CONVERGE` |
| 3 | 委托价合法 / 整手 | `PRICE_INVALID` / `VOLUME_NOT_LOT` |
| 4 | 涨跌停带内 | `BAND_OUT` |
| 5 | 偏离中枢上限 | `DEVIATION_TOO_BIG` |
| 6 | 滑点上限 | `SLIPPAGE_TOO_BIG` |
| 7 | 单笔金额上限 | `AMOUNT_TOO_BIG` |
| 8 | 单标的市值上限 | `POSITION_CAP` |
| 9 | 券商可卖量（T+1 硬约束） | `SELLABLE_INSUFFICIENT` |
| 10 | 当日次数 / 当日亏损 | `DAILY_TRADES_CAP` / `DAILY_LOSS_CAP` |
| 11 | 日内净敞口（默认严格归位） | `NET_EXPOSURE` |

**T+1 的两道保险**：账本层"买回不超过已卖出"（日内净持仓只减不增）
+ 券商层 `can_use_volume` 硬约束。两者同时满足才算合规。

**两条通道的职责边界（重要）**：

| 通道 | 谁下单 | 铁律 |
|---|---|---|
| **直连**（`--direct`） | `tt/executor.py`（外部 Python `order_stock`） | 唯一会报单的地方；账户号必须匹配才放行 |
| **信号文件**（默认） | QMT 内的 `signal_bridge_real.py` | 策略进程只写文件、绝不下单 |

直连通道的执行层还有**纵深防御**（即便 engine 已查过也再兜一道）：
单笔金额上限、委托价上限、整手校验、账户一致性、`order_id` 进程内幂等。

---

## 3. 做T是怎么回事（三条不可协商的规则）

1. **中枢当日固定**。`ref` 取前收（可配开盘价），当天不再漂移。
   档位 = `ref × (1 ± 带宽 × 档号)`，卖 1..N 在上、买 1..N 在下。
   中枢跟着价格跑就退化成趋势跟踪 —— 在趋势年份必然负超额。

2. **带宽必须显著大于成本**。单档间距默认 = 日波动率 × `band_k`，
   或直接用 `band_pct` 指定。往返成本约 0.08%~0.12%，
   带宽过窄会被成本吃光（长电 0.53% 已是可用的窄端）。

3. **趋势态停做**。20 日均线开关：
   - `DISABLED`：偏离 ≥4% + 均线上斜 + 近 20 日涨幅 >8% → 停做（松发就是这种）
   - `HALF`：价在均线上方且均线上斜 → 只做前一半档位
   - `ENABLED`：其余 → 正常

> 分区间回测显示：做T超额与区间涨跌幅相关系数 **-0.47**。
> **越涨越不该做T**。这套开关就是为了把这件事自动化。

---

## 4. 目录结构

```
tt/                       策略子包
├── config.py             配置加载 + 校验(fail-closed)
├── tt_config.json        参数(标的/带宽/风控)
├── grid.py               网格引擎(纯逻辑, 零IO)
├── risk.py               风控引擎(纯逻辑, 零IO)
├── state.py              当日T账本 + 档位水位 + 跨日重置
├── market.py             行情(QMT xtdata / 离线样本)
├── engine.py             编排: 行情+网格+账本+风控 → 意图
├── executor.py           直连执行器(唯一会真报单的地方)
├── arm_today.py          人工闸门工具(放行/急停/看状态)
├── daemon.py             守护: 轮询 → 闸门 → 落信号/报单 → 记账
├── sample_data/          离线样本K线(非交易时段演示用)
└── tests/                140 例 pytest

tt_web/                   可视化
├── app.py                Flask(只监听 127.0.0.1)
├── templates/index.html  ECharts 面板
└── static/echarts.min.js

runtime/state/tt_state.json    运行时状态(当日账本, 自动跨日重置)
runtime/state/tt_runtime.json  守护每轮写的快照(面板优先读它)
```

> 路径说明：目录重组后运行数据统一收进 `runtime/`（由 `shared/common.py`
> 的 `STATE_DIR` 提供，单一真相来源）。`.gitignore` 忽略其内容。

---

## 5. 配置要点（`tt/tt_config.json`）

```jsonc
{
  "dry_run": true,                    // 改成 false 才可能落信号(仍需闸门)
  "paper_total_asset": 500000,        // 账户不可用时的纸面基数
  "paper_positions": {"600900.SH": 5000},  // 纸面底仓(仅演示)
  "max_units_per_round": 2,           // 单轮单标的最多补几档

  "grid": {
    "band_mode": "sigma",             // sigma=日波动率×k | fixed=用 band_pct
    "band_k": 1.0,
    "n_units": 5,
    "ref_mode": "prev_close",         // prev_close | open
    "sigma_window": 60
  },

  "session": {
    "open_start": "09:30",
    "open_end": "14:55",
    "converge_after": "14:30",        // 之后只允许归位买入
    "hard_stop_after": "14:57"
  },

  "risk": {
    "max_single_order_amount": 50000, // 单笔金额上限(元)
    "max_daily_trades": 20,           // 当日成交次数上限
    "max_daily_loss": 3000,           // 当日亏损熔断(元)
    "max_price_deviation_pct": 0.05,  // 委托价偏离中枢上限
    "max_position_pct": 0.20,         // 单标的市值 / 总资产
    "max_net_buy_today_ratio": 0.0,   // 0 = 严格归位(不做净加仓)
    "max_consecutive_failures": 3,    // 熔断阈值
    "max_slippage_pct": 0.03
  },

  "symbols": [
    {"code": "600900.SH", "name": "长江电力", "enabled": true,
     "weight": 0.18,                  // 该标的浮仓 / 总资产
     "band_pct": 0.53,                // 单档间距(%), 覆盖 sigma 口径
     "n_units": 5}
  ]
}
```

### 按资金量校准 `weight`

单档股数 = `总资产 × weight ÷ n_units ÷ 挂单价`，向下取整到整手。
**太小会不足一手（`SIZE_ZERO`）**。经验公式：

```
weight ≥ n_units × 100 × 股价 ÷ 总资产
```

例：总资产 27.5 万、神华约 47.5 元、5 档 → `weight ≥ 5×100×47.5/275000 ≈ 0.086`。
低于此值该票每天都只会被 `SIZE_ZERO` 拦下。

---

## 6. 监控面板

`http://127.0.0.1:5010`（**只监听本机**，可急停故不对外暴露）

- **徽章条**：演练/实盘、急停、ARM、熔断、数据源、时点
- **警示条**：把"为什么本轮没下单"逐条写出来
- **KPI**：可执行意图 / 被拦 / 当日成交 / 当日做T盈亏 / 往返次数 / 交易阶段
- **账户与风控**：资产、可卖量、各限额占用
- **档位与走势**：价格 + MA20 + 卖/买档位水平线；各标的档位触发进度
- **决策明细**：可执行意图 与 **被拦意图（含原因码）** 并排
- **控制**：一键急停 / 恢复 / 写 ARM / 撤销 ARM（写操作都要 `confirm`）

数据来源：优先读守护写的 `tt_runtime.json`（20 秒内视为新鲜），
过期则现场跑一次**只读 plan**（断掉账本写盘句柄，零副作用）。

---

## 7. 测试

```bash
cd D:/cc-joesph
python -m pytest tt/tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_ttNN
```

88 例，覆盖：网格定价与穿越、启停开关三态、风控 11 道闸门（含短路顺序）、
账本 FIFO 配对与跨日重置、编排幂等（档位水位 / 确定性 order_id）、
中枢当日固定、单标的异常隔离、配置合法性。

---

## 8. 上线三步（**一步都还没做**）

1. **DRY_RUN 观察**：跑满至少 2 个交易日，看面板上的意图是否符合预期；
2. **小额真实单**：把 `weight` 调到最小可用值（每档 1 手），
   打开 ARM，确认桥端能正确消费并回报成交；
3. **对账**：把券商成交回报与 `tt_state.json` 的理论账本比对，
   偏差可解释后再放大。

> 目前账本按**挂单价**做理论成交记账，真实成交价、手续费、滑点均未回写。
> 面板与日志都标注为"理论值"，不要当成实际盈亏。

---

## 9. 已知边界

- **交易日历**：仅用 `weekday` 判断，无节假日日历，靠 ARM 闸门兜底；
- **成交回报未回写**：真实成交价/费用/拒单尚未接回账本；
- **停牌未识别**：停牌票行情不动，网格不会触发（等于自然跳过），但无显式告警；
- **做T本身是负期望工具**：历史回测中做T相对买入持有的超额多为负值。
  它的价值在于**降低持仓成本与缩小波动敞口**，不是收益引擎。

---

> 本策略仅供研究与技术演示，不构成投资建议。做T属高频交易行为，实际成交受
> 流动性、滑点与执行纪律影响，可能产生显著偏离；历史表现不代表未来收益。
> 投资者应结合自身风险承受能力独立决策并自行承担风险。真实下单前请务必
> 完成上述"上线三步"验收。
