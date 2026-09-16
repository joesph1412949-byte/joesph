# tt_solo — A股做T策略（自包含）

用**隔夜底仓**做日内高抛低吸（做T）：涨了卖浮仓、跌了买回来，收盘把底仓数量还原，
赚日内差价。底仓不动，浮仓来回。

本目录**自包含**：不 import 主项目的 `prism` / `shared` / `qmt_sync` / `backtest` / `legacy`，
整包拷走即可独立运行（少数底座函数已内联到 `ttcore/_vendor.py`）。决策口径
（band / 档位 / 风控 / 股数算法）与主项目 `tt/` 逐字段一致，唯一刻意例外见 §1 末尾。

> ⚠️ **会动真金白银。** 默认 `dry_run` 不发任何单；真正下单要同时满足
> ①显式 `--live` ②未急停 ③有当日放行条，缺一不可。**先跑 §2 的零副作用演练。**

---

## 1. 这是什么

**底仓 + 浮仓**两段式持仓：

- **底仓**：隔夜持有的长期仓位，做T期间数量不变，是卖出腿的"货源"；
- **浮仓**：日内可动的那部分，用来高抛低吸。

引擎每轮拿行情和档位阶梯比对，算出「该在哪一档挂什么方向的单」，再交给三道闸门和风控
筛查。第 i 档的**卖价 = 参考价 ×(1 + `band_pct`×i)**、**买价 = 参考价 ×(1 − `band_pct`×i)**，
参考价默认昨收（`ref_mode: "prev_close"`）——见 `ttcore/grid.py`。`band_mode: "sigma"`
时带宽不取配置里的 `band_pct`，改由日波动率 × `band_k` 现算。

**唯一的刻意分歧（相对主项目 `tt/`）**：北交所 `920xxx` 的涨跌停比例。

| | 用的判定 | `920xxx` 结果 |
|---|---|---|
| 旧 `tt/risk.py` → `shared.common` | `("8","4")` | **0.10**（±10%，错的） |
| 本包 `ttcore/_vendor.py` | `("8","4","92")` | **0.30**（±30%，正确的） |

后果是实打实的：`920001.BJ` 第 3 档（+10.5%）在旧侧被 `BAND_OUT` 拒单，在新侧正常放行 ——
存在"旧代码会拒、新代码会下"的档位。0.30 才对（北交所确为 ±30%），0.10 会误杀合法委托，
属主项目侧的潜在缺陷。当前配置的标的集里**没有** `920xxx`，所以这条分歧今天不会触发。
`tools/compare_legacy.py` 把它钉成断言：**只许这一处、只许这一个档位**，其余任何分歧都判失败。

## 2. 快速开始（离线演练，零副作用）

最安全的第一步 —— 用离线样本行情跑一轮决策并打印，**不写信号、不下单、不产生任何成交**，
非交易时段也能跑。对交易零副作用；唯一会落盘的是运行数据（刷新
`runtime/state/tt_runtime.json`；账本 `tt_state.json` 缺失或跨日时会新建/翻页）：

```powershell
cd tt_solo
python -m ttcore.daemon --once --sample
```

实测输出（22:12，非交易时段）—— 退出码 **0**：

```
  信号通道: real  (D:\QMT_SIGNALS\real/)
  下发模式: DRY-RUN — 只算不落盘
{
  "ok": true,
  "hhmm": "22:12",
  "phase": "CLOSED",
  "dry_run": true,
  "paused": false,
  "armed": false,
  "env": "real",
  "blocked": "dry_run",
  "signals_written": 0,
  "booked": 0,
  "counts": { "symbols": 4, "intents": 0, "rejected": 5 }
}
  [已拦下] 600900.SH 长江电力 600股 @ 28.239 → SESSION_CLOSED: 非交易时段/已过硬停时点 22:12
  [已拦下] 600900.SH 长江电力 600股 @ 28.388 → SESSION_CLOSED: 非交易时段/已过硬停时点 22:12
  ...（600938.SH 中国海油 ×2、601088.SH 中国神华 ×1，同理）
```

两个字段值得看懂：`blocked: "dry_run"` 是第一道闸门在起作用（哪怕后面两道也过了）；
`rejected` 非 0 说明风控在干活，非交易时段意图会被 `SESSION_CLOSED` 拦下。

跑通这条再往下看。

## 3. 六种运行方式

六个 `.bat` 都在仓库根，且都会设 `PYTHONIOENCODING=utf-8`（Windows 控制台默认 GBK，
不设的话中文输出会乱码甚至抛 `UnicodeEncodeError`）。#1–#4、#6 会自己 `cd /d` 进 `tt_solo/`；
#5 用的是 `arm_today.py` 的绝对路径，在哪儿双击都行。手动敲命令时请照做。

| # | 用途 | 命令（在 `tt_solo/` 下） | 对应 .bat |
|---|------|------------------------|-----------|
| 1 | 守护 · **信号文件通道** · 演练（默认） | `python -m ttcore.daemon --interval 5` | `启动做T守护.bat` |
| 2 | 守护 · **QMT 模拟通道** · 演练 | `python -m ttcore.daemon --interval 5 --env sim` | `启动做T模拟守护.bat` |
| 3 | 守护 · **直连 miniQMT** · 演练 | `python -m ttcore.daemon --direct --interval 5` | `启动做T直连守护.bat` |
| 4 | 守护 · **直连 miniQMT · 实盘** ⚠️ | `python -m ttcore.daemon --direct --live --interval 5` | `启动做T实盘直连.bat` |
| 5 | **当日放行条** / 急停 | `python ttcore/arm_today.py` | `做T-今日放行.bat` |
| 6 | **监控面板** | `python dashboard/app.py` | `启动做T监控台.bat` |

**⚠️ 第 4 行是唯一会真实报单的方式。** 它对应的 `.bat` 里有 8 秒倒计时给你反悔；
先跑第 3 行确认意图无误，再上第 4 行。

两条下发通道**二选一**：

- **信号文件通道**（#1/#2）：守护把信号写成 `<信号根>/<env>/pending/<order_id>.json`，
  由 QMT 内的桥端脚本消费后下单。守护自己不碰任何下单接口。`--env sim` 走 QMT 模拟通道，
  消费方是 QMT 里的 `qmt_signal_bridge_demo.py` —— 注意该桥**不校验账户**，会往 QMT 当前
  登录的那个账户报单，务必先确认 QMT 登在模拟号上。
- **直连通道**（#3/#4）：外部 Python 用 `xtquant` 直连 miniQMT 下单（`ttcore/executor.py`），
  不写信号文件。需要 QMT 客户端已登录、miniQMT 在跑。**`ttcore/executor.py` 是全项目唯一
  会真正下单的地方**，与之并列的只读层（`ttcore/broker.py`）只做查询。

`--direct` 仍然是"二选一"里的一条，不要把两条通道同时开起来。

## 4. 三道闸门：`dry_run` → `paused` → `armed`

三个闸门**串联**，按下面的顺序**短路**判定（`ttcore/daemon.py::run_once`）：

```
① dry_run?  ──是──▶ 只算不落盘（blocked="dry_run"），到此为止
     │否（只有显式 --live 才会「否」）
     ▼
② paused?   ──是──▶ 整体停发（blocked="paused(急停开关打开)"）
     │否
     ▼
③ armed?    ──否──▶ 不发（blocked="not_armed: ..."）
     │是（<信号根>/<env>/armed.txt 含今日 YYYYMMDD）
     ▼
   下发：写信号文件，或交给直连 executor
```

| 闸门 | 默认状态 | 怎么开 | 语义 |
|---|---|---|---|
| **`dry_run`** | **`true`（默认演练）** | 仅 `--live` | 配置里 `dry_run: true`；`--live` 只在内存里把它翻成 `false`，**从不落盘**，进程一退就恢复演练 |
| **`paused`** | 未急停（文件不存在即未按） | —— | `<信号根>/paused` **存在**即整体停发。**创建 = 一键急停**，删掉才恢复 |
| **`armed`** | 未放行 | 当日放行 | `<信号根>/<env>/armed.txt` 必须含**当日** `YYYYMMDD`；**昨天的条今天自动作废** |

**放行条必须每个交易日重做一遍，这是刻意的人工确认点**：程序可以常驻，但每天必须有人点头。

```powershell
python tt_solo/ttcore/arm_today.py --status    # 只看状态，不动任何文件
python tt_solo/ttcore/arm_today.py             # 写今日放行条（并确保无急停）
python tt_solo/ttcore/arm_today.py --pause     # 按急停（创建 paused）
python tt_solo/ttcore/arm_today.py --resume    # 解除急停（删 paused）
python tt_solo/ttcore/arm_today.py --disarm    # 撤销今日放行条（删 armed.txt）
```

两个容易踩的点：

- **`--status` 在"当前不可下单"时退出码是 1**（可以下单才是 0）。这是状态码，不是报错 ——
  别把它当成命令失败。
- `arm_today.py` 只操作 **`real`** 通道的 `armed.txt`（`ENV = "real"` 写死）。跑
  `--env sim` 的守护时，`sim/armed.txt` 得你自己写。

## 5. T+1 硬约束：做T必须有隔夜底仓

A股**当天买入的股票当天不能卖**（T+1）。所以：

- **没有隔夜底仓，做T根本做不起来** —— 卖出腿无货可卖；
- 引擎据此把卖出腿判死在 `NO_BASE_POSITION`（`ttcore/engine.py`）。账户里压根没这只票时
  归因为"账户无该标的持仓, 无法卖出(T+1 无底仓)"；有票但可卖量不够时归因为
  "可卖 N 股(已卖 M), 无足够底仓"。后者按"当日已卖"递减，避免同一轮重复卖同一批底仓。
- 风控另外只让买入腿买回**等量**：`max_net_buy_today_ratio: 0.0` → 日内净买入上限 0 股
  （`engine._max_net_buy_qty`，ratio≤0 即返回 0），也就是严格"归位"、不许越做越加仓。
  这是仓位纪律，不是 T+1 的执行者 —— T+1 由券商/交易所强制，代码既绕不过也不试图绕。

**推论：这套策略不是"从零建仓"的工具，而是"已有底仓之上做增强"的工具。** 底仓从哪来，
不在本项目范围内。

## 6. 目录结构与模块职责

```
tt_solo/
├── README.md               本文件
├── requirements.txt        运行 + 测试依赖
├── ttcore/                 策略核心（自包含）
│   ├── _vendor.py          底座内联：路径根 / 原子写(含 fsync) / 涨跌停比例 / 本机判定
│   ├── grid.py             网格纯逻辑：档位阶梯、档位价（零 IO）
│   ├── risk.py             风控闸门（零 IO）：单笔金额/日内笔数/亏损/偏离/仓位/滑点…
│   ├── state.py            日账本 + 日终归档（tt_history.jsonl）
│   ├── market.py           行情与指标：QMT 行情优先，失败回落离线样本
│   ├── broker.py           miniQMT 账户适配层（**只读**）：资产/持仓/T+1 可卖量/可用资金
│   ├── engine.py           决策编排：把行情+档位+账户+风控拼成 intents / rejected
│   ├── executor.py         直连下单执行器（**全项目唯一真正下单的地方**）
│   ├── config.py           配置加载与校验（文件 → 合并默认值 → 校验）
│   ├── daemon.py           守护：轮询 → 判定三道闸门 → 落信号/直连 → 记账 → 写快照
│   ├── arm_today.py        人工闸门小工具：当日放行 / 急停 / 查状态
│   ├── tt_config.json      策略配置（标的、band、风控阈值、时段）
│   └── sample_data/        离线样本行情（演练用）
├── dashboard/              监控面板（Flask）
│   ├── app.py              后端；只监听 127.0.0.1
│   ├── templates/          index.html 单页
│   ├── static/             echarts.min.js
│   └── tests/              面板测试（接口 / 远程拦截 / 前端契约）
├── tests/                  策略核心测试（192 项）
├── tools/compare_legacy.py 与主项目 tt/ 的对照取证脚本
└── runtime/                运行数据（默认，可覆盖；见 §7）
    ├── state/              账本 tt_state.json、快照 tt_runtime.json、归档 tt_history.jsonl
    └── log/
```

整个套件 192（`tests/`）+ 17（`dashboard/tests/`）= **209 项**。

## 7. 数据归属：谁写哪儿

| 位置 | 默认值 | 覆盖方式 | 里面是什么 |
|---|---|---|---|
| **运行数据根** | `tt_solo/runtime/` | 环境变量 `TT_RUNTIME_DIR` | `state/`（账本、运行时快照、归档）+ `log/`。**这些是本项目自己的数据**，可以随便删/搬 |
| **信号根目录** | `D:/QMT_SIGNALS` | 环境变量 `TT_SIGNAL_ROOT` | `paused`（急停）、`<env>/armed.txt`（放行条）、`<env>/pending/`（待消费信号） |

**`D:/QMT_SIGNALS` 是与外部 QMT 桥端之间的契约，不是本项目的数据目录。** 那个路径由 QMT
侧的脚本读取，改它必须两边一起改 —— 所以它的默认值刻意保持不变。跑演练/测试想跟真实目录
隔离时，用 `TT_SIGNAL_ROOT` 指到临时目录即可（面板启动时会打印一条 warning 提醒已被覆盖）。

**账本记的是"理论成交"，不是真实成交。** 守护按**挂单价**记账（`_book` → `record_fill`），
真实成交价、成交量、手续费**不会回写** —— 权威口径永远以券商回报为准。面板与日志里都
明确标注，不假装它是实际成交。所以 `runtime/state/` 里的账本只适合看"策略打算做什么"，
不能当对账依据。

顺带两个坑：

- `--book-dry-run` 让演练也推进本地账本（便于看清买入腿），但它写的是理论成交，**必须**配
  显式 `--state` 指向演练专用账本；指到实盘默认账本会被 fail-closed 拦下（`_guard_drill_state`）。
  这些假数据会污染 `sold_today` 与档位水位，进而放过本该拦下的买单。
- 运行时快照里的 `source` 字段是**行情源自报值，不是核实结果** —— 离线 `SampleBackend`
  也可能自报 `qmt`。面板因此把它标成"行情源(自报·未核实)"，别拿它当数据源真伪的证据。

## 8. 测试

```powershell
$env:PYTHONIOENCODING='utf-8'
python -m pytest tt_solo -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNNN
```

实测：**209 passed**（`--basetemp` 里的 `NNN` 随便换个数，避免和上一轮残留撞车）。

与主项目 `tt/` 的行为对照（搬家后差异必须**恰好**是 §1 那一条已批准例外）：

```powershell
$env:PYTHONIOENCODING='utf-8'
python tt_solo\tools\compare_legacy.py     # 退出码 0 = 除该例外外一致
```

> ⚠️ **必须在沙箱外跑。** 在受限沙箱里跑 pytest 会出现**假失败** —— 安全删除守卫拦掉
> `--basetemp` 的清理、网络被拦截导致行情相关用例失败。这是环境问题，不是代码缺陷。
> 见到一半用例莫名失败，先确认是不是在沙箱里。
>
> 同理，`compare_legacy.py` 在 GBK 控制台上会因输出 `⇒` 抛 `UnicodeEncodeError` 而退 1 ——
> 那是输出编码问题，不是对照失败。设了 `PYTHONIOENCODING=utf-8` 就正常退 0。

## 9. 安全边界

- **面板只监听 `127.0.0.1`**（`app.run(host="127.0.0.1", ...)`），绝不对外网暴露 ——
  因为它能触发急停/放行这类真实交易闸门。
- **面板只能"关闸"，不能开单**：它不调用任何下单接口，能按急停、能写/撤放行条，
  但发不出委托。
- **`pause` / `arm` 需要 `confirm=true`**，且**只接受本机调用**：判据是"带 `CF-Connecting-IP`
  头 = 经隧道 = 远程；无头且回环/RFC1918 = 本机"。远程调用者拿到 **403**。
- **账户访问只读**：`ttcore/broker.py` 只做 `query_*` 查询，绝不调 `order_stock` /
  `cancel_order_stock`。全项目唯一会下单的模块是 `ttcore/executor.py`，且它只在
  `--direct` 且 `--live` 且 `paused` 不存在且 `armed` 就绪时才被调到。
- **原子写保留 fsync**：`flush + os.fsync + os.replace`，崩溃/断电不会留下半截账本。
- 演练（`--once --sample`）**不写信号、不下单**。需要 `--live` 的写盘动作只有两处：
  信号文件（`--direct` 下则换成真实委托）与账本记账。另有一处与闸门无关的写盘：
  运行时快照 `tt_runtime.json` **每轮都会刷新**（面板靠它取数）。

## 10. 仪表盘

```powershell
cd tt_solo
python dashboard/app.py          # → http://127.0.0.1:5011
```

端口取自环境变量 `TT_WEB_PORT`，默认 **5011**。单页、无构建步骤，ECharts 已随包。
数据来源优先用守护每轮写的运行时快照；快照缺失或超过 20 秒未更新时，现场跑一次**只读计划**
重算（断掉账本写盘句柄，零副作用），此时页面会标注"按配置判定"以区分于实测值。

五个区块：

1. **状态条** —— 三道闸门的当前状态（`dry_run` / `paused` / `armed`），一眼看出为什么没下单。
   顶部四个按钮：急停 / 解除急停 / 今日放行 / 撤销放行。
2. **账户** —— 数据来源标签（`真实账户` / `纸面推演`）、总资产、现金、持仓只数（+ 备注）。
3. **档位阶梯** —— 每个标的一栏：开关状态、带宽、现价、离 MA20 偏离度，以及
   `sellN…sell1 → 中枢 ref → buy1…buyM` 的完整阶梯，已成交的档位打 ✓，
   现价用 `▶ 现价` 插在它在阶梯上的真实位置。
4. **今日战果** —— 实现盈亏、往返次数、当日笔数/上限、熔断状态，以及每个标的的
   卖/买档、净敞口、盈亏（数字出自理论成交账本，见 §7）。
5. **被拦原因排行** —— 按 `reject_code` 聚合的拦截计数 + 样本明细（`NO_BASE_POSITION`、
   `SESSION_CLOSED`、`BAND_OUT`…），用来回答"今天为什么没交易"。

另有一张**历史收益（按日归档）**卡片，画 `tt_history.jsonl` 的逐日序列；没有归档时是空图，
不是错误。
