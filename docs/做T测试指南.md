# 做T策略 · 测试指南（全程不碰真单）

> 2026-09-15 ｜ 目的：在不报真单的前提下，把算法与链路测透
> 核心原则：**命令行里不出现 `--live`，就永远不会下单**
>
> ⚠️ **2026-09-16 起命令已改指 `tt_solo/`**（旧 `tt/` 已退役）：旧写法
> `python -m tt.daemon ...` 跑的是旧守护、写旧账本，而面板（5011）读 `tt_solo`
> 的账本，两者共用 `D:/QMT_SIGNALS` → 会出现「面板空账，真单照发」。

---

## 一、先确认：怎么保证绝对不下单

三道闸门，**任何一道都能独立拦住报单**：

| 闸门 | 位置 | 当前状态（09-15） |
|---|---|---|
| 1. `dry_run` | `tt_solo/ttcore/tt_config.json` → `"dry_run": true` | ✅ 开着 |
| 2. 放行条 `armed.txt` | `D:/QMT_SIGNALS/real/armed.txt` | ✅ 内容是 `20260914`，今天 `20260915` → **已自动失效** |
| 3. 急停开关 `paused` | `D:/QMT_SIGNALS/paused` | 未创建（需要时一键开） |

**闸门优先级**：`dry_run` > `paused` > `armed`（见 `ttcore/daemon.py` 的 `run_once()` 四分支）。
只要 `dry_run` 还是 `true`，后面两道**根本走不到**。所以今天想测代码，**什么都不用改，直接跑就行**。

> 反过来说：**报真单需要同时满足** `dry_run=false` **且** `--live` **且** 放行条写了当天日期。

---

## 二、四层测试（从零风险到最接近实盘）

| 层 | 命令（先 `cd tt_solo`） | 要 QMT? | 会下单? | 验证什么 |
|---|---|---|---|---|
| **L1** 单元/面板测试 | `python -m pytest tt_solo -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNNN`（在仓库根跑） | ❌ | ❌ | 策略 + 面板全绿（223 例） |
| **L2** 离线样本 | `python -m ttcore.daemon --once --sample --fake-now` | ❌ | ❌ | 全链路跑通（写死的假行情） |
| **L3** 真机干跑 ⭐ | `python -m ttcore.daemon --direct --once` | ✅ | ❌ | **真实行情 / 真实持仓 / 11 道风控 / 档位计算** |
| **L4** 持续干跑 | `python -m ttcore.daemon --direct --interval 5` | ✅ | ❌ | 盘中动态响应（挂一整天看） |

**L3 是今天最值得做的一层** —— 它连的是真账户、真行情，唯一区别就是最后那一下不报单。

---

## 三、L3 操作步骤（真机干跑）

```bash
# 第 1 步：开 QMT 并登录
#   D:\QMT\bin.x64\XtMiniQmt.exe         （miniQMT）
#   或 D:\QMT\bin.x64\XtItClient.exe     （大 QMT，极速模式）

# 第 2 步：不用写放行条（dry-run 在 armed 检查之前，轮不到它）

# 第 3 步：单轮干跑
cd /d D:\cc-joesph\tt_solo
python -m ttcore.daemon --direct --once
```

输出分三块，重点看第三块：

1. **横幅** —— 确认是 `DRY-RUN` 而不是 `LIVE`
2. **闸门状态** —— `dry_run / paused / armed` 三个布尔值，以及 `blocked` 原因
3. **`[可执行]` / `[已拦下]` 明细** —— 每一笔"假如报单会下什么"，含代码、方向、股数、价格、档位、距中枢百分比

想挂久一点观察（推荐，能看到盘中价格变化如何触发不同档位）：

```bash
python -m ttcore.daemon --direct --interval 5
# Ctrl+C 停
```

---

## 四、输出里的常见现象（都不是故障）

| 现象 | 含义 |
|---|---|
| `blocked: dry_run` | ✅ 正常，闸门在工作 |
| `SESSION_CLOSED` | 非交易时段（9:30–11:30 / 13:00–15:00 之外） |
| `NET_EXPOSURE: 买入后日内净敞口 N > 上限 0` | 见下节，**预期行为** |
| `SELLABLE_INSUFFICIENT` | 可卖量不够。神华仅 300 股 → 只够第 1 档 |
| `not_armed` | 放行条没写或已过期（只在非 dry-run 时才可能出现） |
| `DEPTH_BEYOND_DEVIATION` | 最深档固有偏离 > 闸门上限。**配置阶段已做交叉校验，正常不会出现** |
| `PRICE_DEVIATION` | 委托价偏离中枢超过 `max_price_deviation_pct` |

---

## 五、⚠️ dry-run 的一个演练盲区（买腿看不到）

**现象**：离线样本 / 干跑时，卖单都正常列出，买单却总被 `NET_EXPOSURE` 拦下。

**原因**：日内净敞口闸门的定义是

```
净买入 = 当日买入 − 当日卖出   ≤   max_net_buy_qty（默认 0 = 严格归位）
```

买入必须等卖出**成交**后才放行，绝不允许日内净加仓。

而 `ttcore/daemon.py` 的 `run_once()` 里，**dry-run 分支不调用 `_book()`**：

```python
if self.dry_run:
    blocked = "dry_run"
    if self.direct and signals:
        exec_results = self._exec_direct(signals, dry_run=True)
    # ← 这里没有 self._book(...)，所以账本里"今日已卖"永远是 0
```

→ 算净敞口时 `sold_today = 0` → 任何买入都判超限。

**这是预期行为，不是 bug**（dry-run 不该污染账本）。但它意味着：
**dry-run 只能演练"高抛"那条腿，"低吸回补"那条腿看不到。**

**补齐办法：`--book-dry-run`（已实现）**

```bash
cd /d D:\cc-joesph\tt_solo
python -m ttcore.daemon --direct --interval 5 \
    --book-dry-run \
    --state runtime/state/tt_state.drill.json
```

第 1 轮卖单记账 → 第 2 轮起买单就有额度，**两条腿都能演练**。

**安全设计（fail-closed，三道隔离）**：

1. `--book-dry-run` **必须显式给 `--state`**，否则拒绝启动 —— 防止手滑用错账本
2. `--state` **不能指向实盘默认账本**（`runtime/state/tt_state.json`），否则拒绝启动
3. `runtime` 快照自动隔离到 `<state 同名>.runtime.json`，不会盖掉面板读的那份

记账只发生在 dry-run 分支内 → **物理上不可能下单**（实盘走的是另一个 `elif` 分支）。

**实测效果**（离线样本连跑 2 轮）：

| 轮次 | intents | booked | 说明 |
|---|---|---|---|
| 第 1 轮 | 4（全卖出） | 4 | 高抛：长电 2 档 + 海油 1 档 + 神华 1 档 |
| 第 2 轮 | 1（**买入**） | 1 | 低吸：海油买回 400 @33.852，与第 1 轮卖的 400 配对 |

第 2 轮账本显示海油 `sold_today=400 / bought_today=400 / net_exposure=0 / trips=1 / realized_pnl=454.4`
—— **一个完整的 T 走通了**，且 `signals_written: 0`，零报单。

---

## 六、测完想恢复实盘

```bash
# 1. 放行当天
cd /d D:\cc-joesph
python tt_solo/ttcore/arm_today.py

# 2. 改 dry_run=false（或直接用 --live 覆盖）
#    双击 启动做T实盘直连.bat

# 紧急情况
python tt_solo/ttcore/arm_today.py --pause     # 一键急停
python tt_solo/ttcore/arm_today.py --resume    # 解除
```

---

## 七、其他有用的自检

```bash
# 账户只读核查（不下单，纯查持仓/资产/委托/成交）
python -m qmt.tools.live_check
python -m qmt.tools.live_check --quiet

# 放行条状态
python tt_solo/ttcore/arm_today.py --status

# 全量回归（六路径; 做T侧已并入 tt_solo, 不再有独立的 tt/tests）
python -m pytest prism/tests prism_web/tests strategy_web/tests tests tt_solo qmt_sync/tests -q \
    --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNNN
```
