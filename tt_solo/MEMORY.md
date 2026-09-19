# tt_solo 记忆（只在做 tt_solo 相关项目时读）

> **适用范围**：`tt_solo/`（`ttcore/` 策略核心 + `dashboard/` :5011 面板 + `tifosi.bat` 入口）、做T执行链。
> **不涉及的会话不需要读本文件**。共享内容（合作偏好 / 环境坑 / 部署 / 账户读取 / 决策史）在根 `MEMORY.md`。
> 维护：做T侧的里程碑与新决策更新本文件。
> 2026-09-18 从根 `MEMORY.md` 按项目拆出，原文各段保持原样，仅归位。
> ⚠️ 文中含**已被推翻的历史决策**（「路线变更：改同花顺手动」），原样保留供追溯，以「路线回退」节为准。

## 当前状态（tt 侧，2026-09-18）

| 维度 | 状态 |
|---|---|
| 实盘（tt 做T策略） | **已接 miniQMT 外部直连**（09-14 晚决策，见「路线回退」节）。代码/测试/闸门就绪，**放行条需每日重写**，`dry_run` 默认仍 True，`--live` 才真报单。**「盘前 ref 取错日」已修两轮**（`303b860` → 真机实测推翻 → `3754841`，见下） |
| 真实账户 | 账号 **88869979**；09-17 07:47 实读总资产 **271,808.12**、4 只持仓（全部 `可卖==持仓`，可做T）；明细见「真实账户快照」节 |
| 测试基线 | **tt_solo 224 绿**（09-19，`--basetemp=pt_tt21`；原 198 + ref 相关 24 例） |

- **tt_solo 批次：做T策略抽成自包含项目 + 仪表盘重建**（09-16，spec/plan 见 `docs/superpowers/{specs,plans}/2026-09-16-tt-solo-extract*`）：`tt/`（24 文件/4362 行）+ `tt_web/` → **`tt_solo/`（唯一实现，旧目录已删）**。
  · **结构**：`tt_solo/ttcore/`（11 模块：`_vendor`/grid/risk/state/broker/market/config/engine/executor/daemon/arm_today）+ `tt_solo/dashboard/`（Flask + 前端，**:5011**）+ `tests/`（**221 绿**）+ `tools/compare_legacy.py`。
  · **依赖剥离（自包含的硬定义）**：只把 5 个符号内联进 `ttcore/_vendor.py`（`atomic_write`/`limit_ratio_for_code`/`is_local_request` + 路径根），把 `prism.live_account` 吸收为 `ttcore/broker.py` → **零 prism/shared import**（AST 扫描护栏常驻，恶意注入实测会红）。
  · **路径归属**：运行数据自带 `tt_solo/runtime/`（`TT_RUNTIME_DIR` 可覆盖）；`TT_SIGNAL_ROOT`（默认 `D:/QMT_SIGNALS`）**是与外部 QMT 桥的契约，刻意不改**。
  · **两个新增功能**（非纯搬家，均经评审）：①**日终归档** `tt_history.jsonl` —— 原 `load()` 遇跨日直接覆盖、前一日永久丢失；现重置前 append（fsync），且 `Ledger(writable=False)` 只读模式让面板能读盘而不写盘。②仪表盘两新接口 `/api/rejections`（按原因码聚合被拦）+ `/api/ledger/history`（收益曲线）。
  · **搬家零回归的证据**：`python tt_solo\tools\compare_legacy.py` exit 0 —— 新旧引擎同输入下 plan 518 字段 / snapshot 92 / state 100 / history 10 全等，且账本非空（6 笔成交、往返 2、盈亏 630.00）。**唯一例外且已钉为断言**：北交所 `920xxx` 涨跌停比例 0.10 → **0.30**（旧代码经 `shared.common` 少了 `"92"` 段会误拒合法单，属修 bug；当前标的池无 920xxx，故潜伏）。
  · **仪表盘**：五区块一屏决策面板（状态条三道闸门 `dry_run→paused→armed` / 账户卡 / 档位阶梯含 ▶现价 / 今日战果 / 被拦原因排行）+ 收益曲线；ECharts **走本地**（无 CDN）；急停/放行双重确认；只监听 `127.0.0.1`；**只能关闸不能下单**。
  · **`.bat` 入口**：6 个做T启动器已改指 tt_solo（daemon 必须以 `tt_solo` 为工作目录，否则 `python -m ttcore.daemon` 找不到模块）。
  · **踩坑（重要）**：①**判断文件编码只看原始字节或 `read` 工具，别信 pwsh 的 stdout** —— 它会把正常 UTF-8 中文显示成乱码；本次曾据此误判「`.bat` 与 `tt_config.json` 是 GBK 需重写」，用字节核验后推翻（`做T` = `e5 81 9a 54`），差点把好文件改坏。②**本机没有 `rg`** → 用 `Select-String` 或 grep 工具。③**测试会改写真实运行数据**：`test_env_sim` 5 处构造 `TTDaemon` 未传 `runtime_path` → 用假快照覆盖面向运维的 `tt_runtime.json`（仪表盘正是读它）；已修（两侧都补 `runtime_path=tmp_path`）。④**护栏最容易空转**：负控只测 `import prism` 而不测 `from prism.x import y` 时，把整个 `ImportFrom` 分支删掉 190 个测试仍全绿 —— 已补正向断言（本仓原有违规全是 ImportFrom 形态）。
  · **并发会话事故（09-16 晚）**：另一会话在同一仓库改 `prism/`、`prism_web/`、`backtest/`，并**把本批 5 个 tt_solo 提交一并推送到 origin/master**（`17ca025`/`182db70`/`26d8af9`/`b7b014f`/`6e9bc55`），绕过了"push 必须先问"的规矩。未回滚（已推送，回滚风险更大）。
- **tt 做T策略（09-13，新增，独立子包）**：在实盘链路上追加「底仓+浮仓 日内高抛低吸」策略。交付：`tt/`（config/grid/risk/state/market/engine/daemon，grid 与 risk 为零 IO 纯逻辑）+ `tt_web/`（Flask 面板，**只监听 127.0.0.1:5010**，ECharts 双图+档位阶梯+被拦归因）+ `tt_README.md` + `启动做T监控台.bat`/`启动做T守护.bat`。**复用不重造**：`common`（后缀/涨跌停/日志）、`exit_rules`（跌停判定）、`prism.live_account`（账户只读）、与 `prism.trader` 同构的信号协议、桥端 `armed`/`paused` 闸门。核心口径：**中枢取前收且当日固定**（漂移就退化成趋势跟踪）+ 带宽=日波动率×k（或 `band_pct`）+ 20MA 三态开关（ENABLED/HALF/DISABLED）；闸门三道（dry_run / paused / armed）+ 生成侧 11 道风控（每道有原因码，面板逐条可见）+ **净敞口(默认严格归位) × 券商 can_use_volume 双保险 T+1**。**默认 dry_run**，本进程只写信号文件、绝不调下单接口。可调环境变量 `TT_SIGNAL_ROOT`/`TT_WEB_PORT`（演练时与真实 QMT 目录隔离）。**88 例测试，全量 808 绿**（720+88，无回归）。实测：`connect()` 可读真实账户 274,783.61、行情走 QMT 实时源、松发 DISABLED 与海油/神华 HALF 判定正确、非交易时段全部被 `SESSION_CLOSED` 拦下（fail-closed 生效）；**真实账户当前不含这四只票** → 实盘会全被 `NO_BASE_POSITION` 拦下（做T前提是先持底仓）。校准要点：`weight ≥ n_units×100×股价÷总资产`，否则每天只被 `SIZE_ZERO` 拦。**上线三步（DRY_RUN 观察→小额真实单→成交对账）一步未做**；账本按挂单价做**理论成交**记账，真实成交价/费用/拒单均未回写。
## tt 做T策略 · 信号桥安全审查发现（2026-09-14）

读源码 + `audit_tt_gate.py` 逐档实证（审查脚本在 WorkBuddy 工作区）。**3 条必改，否则实盘不可用**：

1. **桥端日去重与做T语义冲突（最狠）**：`qmt_signal_bridge_real.py` 的 `DEDUP_ENABLED=True` + `_already_placed_today()` 去重键是 **stock_code** → **每只票每天只放行一单**，做T第2笔起全 `failed: DUPLICATE`，5档阶梯/往返配对/日内归位全废。防重目的已被 tt 的确定性 `order_id`（`TT_日期_代码_方向_档位`，文件名同键 → `write_signals` 天然跳过）覆盖。**改法：去重键 stock_code → order_id**（信号已带 `strategy_id="tt_grid_v1"`，也可按策略区分）。
2. **`risk.max_price_deviation_pct` 与网格带宽打架**：偏离闸门用"委托价 vs 中枢"，而第N档固有偏离 = `N × band` → 硬约束 `n_units × band ≤ max_price_deviation_pct`。实测（5% 上限）：长电 0.53%×5=2.65% 全通；**海油 2.0% 第3档(6%)起全拦 → 只剩2档**；**神华 1.4% 第4档(5.6%)起拦，且第3档股数不足一手 → 只剩2档**。属"静默功能缺失"而非"安全"。**改法：收紧 band / 降 n_units，并在 `config.validate()` 加交叉校验 fail-closed 报错**。
3. **滑点闸门是死检查**：`engine._make_intent` 传 `ladder_price_ref=price`（同值）→ `check_slippage` 的 `abs(price/ref-1)` 恒为 0 → `SLIPPAGE_TOO_BIG` 永不触发，`max_slippage_pct=0.03` 纯装饰。与#2 合看 = **两个闸门角色接反**（防离谱偏离的在误伤深档，防挂错档的被架空）。

**两条操作坑（已验证）**：
- **两条通道 account_id 要求相反**：`qmt_signal_bridge_real.py` 留空 → 自动用 `ContextInfo` 登录账号（**推荐留空**）；`qmt_signal_bridge_demo.py` 有 `if not account_id: return False, "missing account_id"` → **走 sim 通道必须先在 `tt_config.json` 填 account_id**，否则每单 `failed: missing account_id`。
- **真急停 = 撤销 ARM，不是暂停**：`paused` 只被策略侧读取，挡不住已在 `pending/` 的信号；桥端 `drain_queue()` 每轮重读 armed → **删 `armed.txt` 连已入队信号也会被拒（failed: NOT ARMED）**。

**其余判断**：`FILE_MIN_AGE=1.0` 秒冗余（策略侧已 `os.replace` 原子写，纯延迟，建议降到 0.2）；`armed` 策略侧+桥端查两遍可留；建议桥端补单笔金额上限+时段校验作纵深防御（目前手工丢 JSON 进 pending 会被照单全收）。

**前置硬门槛（非技术）**：真实账户总资产 274,783.61，持仓**只有 603268.SH**，三只做T标的**零底仓** → 卖出腿 `NO_BASE_POSITION`、买入腿 `NET_EXPOSURE`（`max_net_buy_today_ratio=0` 严格归位）→ **不补底仓一格都不会成交**。另：`D:/QMT/python/SIGNALBRIDGE.py` 是 **16434 字节单行密文**（疑似 QMT 加密策略产物，非可读源码），**无法文本核对 QMT 里跑的是哪一版桥** → 上线前须确认或改用可读源码。
## 路线变更：tt 暂缓接 miniQMT，改为同花顺手动做T（2026-09-14）

> ⚠️ **本节已被 09-14 晚的决策推翻** —— 见下方「路线回退：tt 改接 miniQMT 外部直连」。
> 保留本节仅为记录决策演进史。

用户决定**暂不实盘接入 miniQMT**，改为在**同花顺用条件单手动执行**：先建底仓 → 再做T。tt 的自动链路（daemon/桥）保持原状挂起，3 条必改项优先级下调。

**沿用同一套策略口径**（`tt_config.json` + `tt/grid.py`），只是执行端换人：
- ref = 前一交易日收盘；档位 = `ref × (1 ± band × n)`，band 取 `symbol.band_pct`；n_units=5；HALF → 只用前 2 档；DISABLED → 当日停做；每档 100 股（1手）
- 9/11 收盘基准状态：长电 28.45 ENABLED(0.53%)、海油 33.91 HALF(2.0%)、神华 47.51 HALF(1.4%)、松发 197.50 DISABLED

**T+1 硬约束（本次最关键判断）**：A股当天买入不可卖 → 做T必须先有隔夜底仓。用户三只标的可用底仓为 0，故**建仓日只能建仓、不能做T，次日起才有得做**。这条要放在任何执行方案的第一句。

**建仓口径**：总仓位 = `weight × 总资产`（9/13 读到 274,783.61）→ 长电1700股 / 海油1200股 / 神华500股 ≈ 112,812 元（41.1%）；拆 A批(60%, 开盘限价=前收×1.003, 跳空>+1.5%放弃当天买) + B批(40%, 同花顺「价格条件单-到价买入」挂 buy1, 长期有效)。底仓/浮仓 = 长电1200/500、海油700/500、神华300/200。

**做T纪律**：09:30–14:55 操作；**日内归位（收盘前净敞口归零，卖出的量当天买回）**；日亏 3000 停手；单笔≤5万；偏离中枢>5% 不下单；开关停用日不做。

**同花顺条件单要点**（已核实）：须券商支持「云条件单」+ 签协议 + 选「全自动委托」（否则只弹提醒）；选云端而非本地；类型「价格下破买入」；委托价用对手价；有效期选长期；A/B 批资金需同时预留。

**每日例程**：WorkBuddy 自动化「做T每日执行卡」（工作日 08:40）→ 拉日K → 用 `tt/grid.py` 重算档位 → 输出 `做T每日执行卡_YYYY-MM-DD.md`。依赖 QMT 在线读持仓；不在线时需用户提供持仓/成交记录。

---
## 路线回退：tt 改接 miniQMT 外部直连（2026-09-14 晚，**现行方案**）

用户推翻上节决策，拍板 **tt 直接接 miniQMT 自动运行**，技术路线选 **外部 Python 直连**（不走信号文件桥）。

**为什么选外部直连（已实测坐实）**：
- 系统 Python 3.12 的 `xtquant` 是**全套**（自带 `datacenter.cp312.pyd` + `xtpythonclient.cp312.pyd`），**不依赖 QMT 内 pyd、无需 PYTHONPATH** → MEMORY 第 161 行的「xtquant 不在系统 python 里」**已过时**。
- `order_stock(account, code, order_type, volume, price_type, price, ...)` 可直接从外部进程调；常量 `STOCK_BUY=23 / STOCK_SELL=24 / FIX_PRICE=11`。与 QMT 内 `passorder` 的 opType 0/1 **是两套体系**，别混。
- **绕开了信号桥的全部已知缺陷**：桥端 `stock_code` 去重键（每票每天只放行一单）、`SIGNALBRIDGE.py` 是 16434 字节单行密文无法核对、`FILE_MIN_AGE` 延迟 —— 直连**不写 pending/*.json**，这些坑全部不适用。

**交付物**：
| 文件 | 作用 |
|---|---|
| `tt/executor.py`（新，~300 行） | **核心**。外部直连下单执行器：`DirectExecutor` + `_DirectBackend`。`_field()` 鸭子类型同时兼容 `Intent`(属性) 和信号 `dict`(键)。含执行层纵深防御（`PRICE_TOO_HIGH`/`AMOUNT_TOO_BIG`）、进程内 `order_id` 幂等、整批账户号校验 |
| `tt/arm_today.py`（新） | 人工闸门工具：放行/查状态/暂停/恢复/撤放行。`--status` 返回退出码 0/1 |
| `tt/daemon.py`（改） | 新增 `--direct` 通道；`run_once()` 四分支闸门；`_exec_direct()`；runtime 快照含 `direct`/`exec`/`exec_stats` |
| `tt/config.py`（改） | 新增 **`grid.max_units`**，把「每档金额分母」与「实际用几档」解耦；`validate()` 校 `max_units ∈ [1, n_units]` + symbol 级 `max_units × band ≤ max_price_deviation_pct` 交叉校验（fail-closed） |
| `tt/engine.py`（改） | ① 修**滑点死检查**：`ladder_price_ref` 从同值 `price` 改为真实阶梯价（`grid.ladder_price`）② 新增运行时 `DEPTH_BEYOND_DEVIATION` 原因码 ③ `plan_symbol` 用 `depth = min(n_units, max_units)` 算 `n_eff` |
| 3 个 .bat | `启动做T直连守护.bat`（DRY-RUN）/ `启动做T实盘直连.bat`（`--live`，含 8 秒警告）/ `做T-今日放行.bat` |
| `tt/tests/test_executor.py`（新 22 例） | dry_run 零报单 / 下单参数 / 幂等 / 账户不匹配 / 柜台拒单 / 金额价格上限 / dict 入参 |
| `tt/tests/test_direct_mode.py`（新 6 例） | 闸门串联：dry_run / paused / armed 缺失 / armed 过期 / armed+live 真报单 / 第二轮幂等 |

**`n_units` 的三重语义坑（关键设计发现）**：`n_units` 同时决定 ①阶梯深度 ②每档金额分母（`unit_value = 总资产 × weight ÷ n_units`）③HALF 缩放基数。直接 5→3 会让单档变大（500/400/100），超出底仓（长电仅 1000、海油仅 700）→ **新增 `max_units` 解耦**：`n_units=5` 保分母、`max_units=3` 限实际档数 → 单档维持 300/200/100。

**配置校准（`tt/tt_config.json`）**：
- `grid`: `n_units=5` + **`max_units=3`**
- 海油 `band_pct`: `2.0` → **`1.65`**（2.0%×3=6% 超 5% 偏离闸门 → 收紧到 1.65%×3=4.95% 卡进）
- `paper_positions` 改为实盘真实持仓（长电 1000 / 海油 700 / 神华 300 / 松发 100）
- `account_id: ""`（直连模式**自动枚举**登录账号，无需填）

**§121 三条必改项的处置**：
1. 桥端 `stock_code` 去重键 → **不再需要**（改直连，不走 pending 队列）
2. `n_units × band ≤ max_price_deviation_pct` 交叉校验 → ✅ **已做**（`config.validate()` + `engine._make_intent` 双重，fail-closed）
3. 滑点闸门 `ladder_price_ref=price` 同值 → ✅ **已修**（传真实阶梯价）

**闸门串联（直连版）**：`dry_run`（默认 True）→ `paused`（`D:/QMT_SIGNALS/paused` 文件存在性）→ `armed`（`D:/QMT_SIGNALS/real/armed.txt` 须含当日 `YYYYMMDD`，**每天重新放行**）。

**测试**：tt 全量 **140 passed**；六路径全量 **887 passed / 0 failed / 0 errors**（脱沙箱复跑，沙箱假失败全部消失）。

**9/15 实盘状态**：`armed.txt` 已写 `20260914`（当日有效）；**9/15 需重新放行**（`python tt/arm_today.py`）。配置 `dry_run` 仍为 `true`，真报单需 `--live`。操作卡见 `docs/做T操作卡_20260915.md`。

**大 QMT 跑法（09-14 晚补充，用户提问后整理）**：三种跑法参数手册见 `docs/大QMT网格参数手册.md`。
- **A1** QMT 内原生写网格（passorder）｜**A2** QMT 跑桥 `qmt/bridge/signal_bridge_real.py`（最省事）｜**B** 外部直连（当前在用）
- **A2 与 B 共用同一份 `tt_config.json`**，随时可切；**但两条通道不能同时开 → 会双重下单**
- **桥端日去重键已修**：`stock_code` → **`order_id`**（新增 `_dedup_key()`）。原逻辑让每票每天只放行 1 单，做T多档全废 → 这是 MEMORY 原「3 条必改项」第 1 条的最终处置
- QMT 内是 **GBK** → 桥脚本**刻意全用英文注释**；`FILE_MIN_AGE` 建议 `1.0 → 0.2`
- **两套下单常量不可混**：QMT `passorder` 用 `opType 0/1`；外部 API 用 `STOCK_BUY=23 / STOCK_SELL=24`

---

---

## 真实账户快照

> 读取方法见 `~/.workbuddy/skills/miniqmt-account-readonly-query/SKILL.md`（已固化为 skill）。

## 2026-09-17 07:47 柜台实读（**最新**）

**账号 88869979**｜总资产 **271,808.12** = 现金 **104,440.12** + 市值 **167,368.00**

| 代码 | 名称 | 持仓 | 可卖 | 成本价 | 9/16 收盘 | 市值 | 浮动盈亏 |
|---|---|---|---|---|---|---|---|
| 600900.SH | 长江电力 | 1,700 | 1,700 | 28.3809 | 28.46 | 48,382 | +134 (+0.28%) |
| 600938.SH | 中国海油 | 1,200 | 1,200 | 33.7103 | 33.08 | 39,696 | -756 (-1.87%) |
| 601088.SH | 中国神华 | 500 | 500 | 47.1965 | 47.10 | 23,550 | -48 (-0.20%) |
| 603268.SH | 松发股份 | 300 | 300 | 188.8465 | 185.80 | 55,740 | -914 (-1.61%) |

**关键判读**：
- **建仓已补满** —— 目标仓位（长电 1700 / 海油 1200 / 神华 500）全部到位；松发从 100 加到 300 股。
- 四只票 `可卖 == 持仓` → 全部过 T+1，**均可做T**。
- 当日委托 0 / 成交 0（读取时未开盘）。走同花顺等外部通道时 QMT 看不到 → **返回 0 不代表没交易**。
- ✅ **市值自校验通过**：`1700×28.46=48,382` / `1200×33.08=39,696` / `500×47.10=23,550` / `300×185.80=55,740`，与 QMT 逐项吻合 → 账户数据准确。

## 2026-09-14 21:36 柜台实读（历史）

**账号 88869979**｜总资产 **274,648.96** = 现金 188,570.96 + 市值 86,078.00

| 代码 | 名称 | 持仓 | 可卖 | 成本价 | 9/14 收盘 | 市值 | 浮动盈亏 |
|---|---|---|---|---|---|---|---|
| 600900.SH | 长江电力 | 1,000 | 1,000 | 28.4253 | 28.63 | 28,630 | +204.70 (+0.72%) |
| 600938.SH | 中国海油 | 700 | 700 | 34.0175 | 33.91 | 23,737 | -75.25 (-0.32%) |
| 601088.SH | 中国神华 | 300 | 300 | 47.4171 | 47.20 | 14,160 | -65.13 (-0.46%) |
| 603268.SH | 松发股份 | 100 | 100 | 183.5858 | 195.51 | 19,551 | +1,192.42 (+6.50%) |

**关键判读**：
- **四只票 `可卖量 == 持仓量`** → 不是当日买入（T+1 下当日买入可卖应为 0）→ 底仓此前已存在。
- 当日委托 0 / 当日成交 0（走同花顺，QMT 通道看不到 → **`query_stock_orders` 返回 0 不代表没交易**）。

**⚚ 重大纠错记录（务必看）**：09-14 21:31 曾生成一份《做T收盘复盘》，用"价格穿越挂单价"**推演**出"长电卖2档、海油卖1档、净卖300股敞口过夜"。
**这是错的** —— 真实账户显示一股未动（若真卖了 200 股长电，持仓应是 800 而非 1000）。该推演文档仍在工作区，**结论已作废，勿采信**。

---
## 做T建仓执行现状（同花顺手动通道）

**路线**：用户 09-14 拍板 **暂时不接 miniQMT 自动执行**，改在**同花顺挂条件单手动做T**。tt 的自动链路原样保留挂起。

**建仓方案（9/13 定，基准 9/11 收盘 + 总资产 27.48 万）**：
- 目标仓位 = `weight × 总资产` → 长电 1700 股 / 海油 1200 股 / 神华 500 股（≈41% 仓位，约 11.28 万）
- 拆两批：**A批 60%** 开盘限价单（前收 × 1.003，跳空 >+1.5% 放弃当天买）；**B批 40%** 同花顺「价格下破买入」条件单，挂 buy1，长期有效

**实际执行结果（9/14 核对）**：

| 标的 | 计划 | 实际 | 缺口 | A批（限价） | B批（下破） |
|---|---|---|---|---|---|
| 长江电力 | 1,700 | 1,000 | -700 | 28.54 **✅成交** | 28.30 **❌未触发** |
| 中国海油 | 1,200 | 700 | -500 | 34.01 **✅成交** | 33.23 **❌未触发** |
| 中国神华 | 500 | 300 | -200 | 47.65 **✅成交** | 46.84 **❌未触发** |
| 松发股份 | 100 | 100 | 0 | 停做 | 停做 |

- **A批全成**：限价单只要当日出现更低价格必成交；今日最低 28.38/33.66/46.97 全部覆盖。
- **B批全未触发**：今日最低距触发价还差 0.08 / 0.43 / 0.13 元。
- → 当前是**半仓建仓完成态**（不是漏单）。**缺口合计 1,400 股 ≈ 4.64 万**（现金 18.86 万够补）。
- **三个待用户拍板的选项**：A 让 B 批继续挂着等回落 ｜ B 上移触发价接受贵 0.5~2% 换当天补满 ｜ C 不补，用现有持仓直接做T（收益绝对额缩水约 45%）。

**9/15（周二）做T档位**（基准已换 9/14 收盘；每档 100 股）：

| 标的 | 开关 | 卖出档 | 买入档 |
|---|---|---|---|
| 长江电力 | ENABLED 全5档 | 28.78 / 28.93 / 29.09 / 29.24 / 29.39 | 28.48 / 28.33 / 28.18 / 28.02 / 27.87 |
| 中国海油 | HALF 前2档 | 34.59 / 35.27 | 33.23 / 32.55 |
| 中国神华 | HALF 前2档 | 47.86 / 48.52 | 46.54 / 45.88 |
| 松发股份 | DISABLED | 停做 | 停做 |

**同花顺条件单要点（已核实）**：须券商支持「云条件单」+ 签协议 + 选「**全自动委托**」（否则只弹提醒）；选**云端**而非本地；类型「价格下破买入」；委托价用**对手价**；有效期选**长期**（当日单收盘即失效）；A/B 批资金需**同时预留**。

**做T纪律 6 条**：① 09:30–14:55 操作，14:30 后不开新仓 ② **日内归位**（收盘前净敞口归零）③ 日亏 3000 停手 ④ 单笔 ≤5 万 ⑤ 偏离中枢 >5% 不下单 ⑥ 开关停用日不做。

## tt 做T策略技术档案（若将来恢复自动执行）

**架构**：`tt/` = config（配置+env白名单校验）/ grid（20MA开关+档位，零IO纯逻辑）/ risk（11道风控纯逻辑）/ state（当日账本+跨日重置+原子写）/ market（QMT实时源+离线回落）/ engine（编排→意图，T+1双保险）/ daemon（轮询→闸门→落盘）+ `tt_web/`（Flask 面板，只监听 127.0.0.1:5010）。

**核心口径**：中枢取**前收且当日固定**（漂移会退化成趋势跟踪）；带宽 = 日波动率 × k（或 `band_pct`）；**20MA 三态开关** ENABLED / HALF（只用前2档）/ DISABLED；每档 100 股。

**安全设计**：
- **三道闸门串联**：`dry_run`（默认 True）→ `paused`（文件存在性）→ `armed`（`real/armed.txt` 须含当日 `YYYYMMDD`）
- **11 道风控**（每道有原因码，面板逐条可见）：熔断 / 启用 / 时段 / 价格 / 整手 / 涨跌停带 / 偏离 / 滑点 / 单笔 / 单标 / 券商可卖量 / 当日次数 / 当日亏损 / 日内净敞口
- **T+1 双保险**：策略侧净敞口（默认严格归位 `max_net_buy_today_ratio=0`）× 券商 `can_use_volume`
- **env 通道**：`--env real|sim`（`config.validate` 白名单校验，非法抛 `ConfigError`）；sim 时 `env_banner()` 明确打出"**桥不校验账户**"警告；sim 仍要求 `sim/armed.txt`（生成侧比桥端严是刻意的）
- **可调环境变量**：`TT_SIGNAL_ROOT` / `TT_WEB_PORT`（演练时与真实 QMT 目录隔离）

**测试**：`tt/tests/` 共 **107 例**（88 + `test_env_sim.py` 19）

**启动入口**：`tt_solo/tifosi.bat`（桌面 `tifosi.bat` 是薄壳）—— 主菜单 1 守护（默认 DRY-RUN）/ 3 模拟守护（sim 通道）/ 4 仪表盘（**:5011**）；主菜单 5 放行、6 闸门状态，高级选项 A 含真报单与急停

**校准要点**：`weight ≥ n_units × 100 × 股价 ÷ 总资产`，否则每天只被 `SIZE_ZERO` 拦。

## 待办（tt 侧｜原「六、待办清单」中属于 tt 的条目，编号沿用原文）

## 🔴 高优先（阻塞实盘 / 有资金风险）

0. **✅【09-17 发现 / 09-19 已修完两轮】盘前运行会把 `ref` 钉成前前一天的收盘**
   - 现象：09-17 07:47 盘前跑 `TTEngine.plan()`，长电 `ref=28.500`；而 9/16 收盘 **28.46**、9/15 收盘才是 28.50 → 整整错一个交易日。
   - 根因：`market.XtdataBackend.ticks()` 的 `tick["lastClose"]` 在盘前仍是**上一交易日盘中那份**快照；引擎优先用它当 `ref`。而 `ledger.get_ref()` 当天第一轮即缓存 ⇒ **盘前启动守护 = 全天档位基于错误中枢**。
   - **第一版修法（`303b860`，已被推翻）**：`prev_close` 用"tick 交易日**新于**日K末根就采信 tick"的启发式。
   - **⚠️ 09-19 真机实测推翻它**：QMT 把 `tick.time/timetag` 打成**当前墙钟**（周六 13:32 测到 `timetag=20260919 13:32:52`），而 `tick.lastClose` 仍是 **09-17 的 28.46**（正确的"前收"此刻应是 09-18 的 **28.27**）⇒ 启发式几乎恒真，**又退回去信陈旧 tick，ref 仍错一天**（实测返回 `tick_newer 28.46`）。
   - **✅ 现行修法（`3754841`）**：**以"今天"为参照系**，彻底不信 tick 的时间戳 ——
     `bars = 日K(升序, 丢掉 close<=0 占位行)`；`末日K == 今天` → `ref = 上一根.close`(`daily_prev`)；否则 `ref = 末根.close`(`daily`)；无日K才回落 tick(`tick_fallback`)。`ref_mode=="open"` 分支不变。
   - **真机只读复测（09-19，QMT 在线）**：`600900.SH → 28.27/daily`、`603268.SH → 208.00/daily`、`600938.SH → 32.30`、`601088.SH → 46.23`，四只票全部与旧口径（陈旧 lastClose）**不同**（探针 `.superpowers/sdd/probe-tt-ref-live.py`、`probe-tt-ref-raw.py`）。
   - ⏳ 仍未做：**真实盘中/盘前时段**再实跑一次（`cd tt_solo; python -m ttcore.daemon --direct --once`，核对 runtime 快照里 `ref_src=="daily"` 且 `ref == 上一交易日收盘`）—— 逻辑已由 24 例离线测试 + 真机只读复测覆盖。
   - 遗留（非阻塞）：`ctx["last_close"]`（涨跌停带/面板显示）仍用陈旧 `tick.lastClose`，**未改口径**；依赖本机墙钟"今天"，时钟错乱会退化为"取末根"。

1. **tt 3 条必改项 —— 已全部处置**（09-14 晚）：
   - 桥端日去重键 `stock_code` → ✅ **已修为 `order_id`**（`qmt/bridge/signal_bridge_real.py` 新增 `_dedup_key()`；外部直连通道本就不走桥，但走 A2 时必需）
   - `n_units × band ≤ max_price_deviation_pct` 交叉校验 → ✅ **已做**（`config.validate()` fail-closed + `engine._make_intent` 运行时 `DEPTH_BEYOND_DEVIATION`）
   - 滑点闸门 `ladder_price_ref=price` 同值 → ✅ **已修**（传真实阶梯价）
2. **确认 QMT 里跑的桥是哪一版**：`D:/QMT/python/SIGNALBRIDGE.py` 是 16434 字节单行密文 → 直连方案下**不再阻塞 tt**（tt 已绕开桥）；但 prism 主策略仍走桥，**该确认仍然有效**。
3. **做T缺口**：账户已建仓 4 只（长电 1000 / 海油 700 / 神华 300 / 松发 100）。**神华只有 300 股，只够第 1 档**（第 2/3 档会被可卖量拦下 = 正确行为）；长电/海油够 3 档。
4. **日内归位纪律**：自动链路已实现（`max_net_buy_today_ratio=0` 严格归位），人工执行时仍是最大未闭环项。
5. **每日放行条**：`armed.txt` 只认当日日期，**每个交易日开盘前必须重写**（`python tt_solo/ttcore/arm_today.py` 或 `tifosi` 主菜单 5）。

## 测试与验证速查（tt 侧）

```bash
# 只跑做T侧（基线 265 绿 = tt_solo 198 + dashboard 23 + qmt_sync 27 + 根 17）
python -m pytest tt_solo/tests tt_solo/dashboard/tests qmt_sync/tests tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_ttNN

# 做T直连 · 干跑（零副作用；**必须在 tt_solo 目录下跑**）
cd tt_solo; python -m ttcore.daemon --direct --once

# 做T直连 · 放行/查状态/急停
python tt_solo/ttcore/arm_today.py            # 今日放行
python tt_solo/ttcore/arm_today.py --status   # 查状态(退出码 0/1)
python tt_solo/ttcore/arm_today.py --pause    # 急停
```

> 通用 pytest 两个坑（basetemp 正斜杠、沙箱假失败）见根 `MEMORY.md`。

## tt 侧工程坑（补充）

- **原子写 / `os.replace`**：`tt_solo/ttcore/_vendor.atomic_write` 同样带「tmp 名带 pid+tid + 失败即清理 + `os.replace` 有界退避重试」；根因与另外两份实现（`shared/common`、`prism/zt_history`）的完整说明见根 `MEMORY.md` 坑表与 `prism/MEMORY.md`。
- **判断文件编码只看原始字节或 `read` 工具**，别信 pwsh stdout（会把正常 UTF-8 中文显示成乱码）——见根 `MEMORY.md` 环境备忘 / 「tt_solo 批次」踩坑记录。
- **测试会改写真实运行数据**：构造 `TTDaemon` 必须传 `runtime_path=tmp_path`，否则用假快照覆盖面向运维的 `tt_runtime.json`（仪表盘正是读它）。
