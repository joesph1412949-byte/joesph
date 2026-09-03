# 模拟盘排板队列状态机 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模拟盘买入升级为排板队列状态机（排队-成交/开板撤单/收盘失效三结局，成交=新增成交量穿越队列，冻结资金，卖出端跌停顺延）。

**Architecture:** 账本加 `pending_buys`（排板委托集）与 `canceled_pending_codes`（当日撤单码，禁重排）；守护每轮经 `tick_fn`（封装 `ds.get_full_market_ticks`）检查排板三结局；成交判定唯以"新增累计成交量 ≥ 前方封单+本单 且仍封板"为据；资金冻结（available = cash − Σ冻结）保不变量；跌停日卖出跳过顺延。

**Tech Stack:** Python 3.12 / 既有模拟盘（paper.py/paper_daemon.py）/ flask GUI（复用）/ pytest 全离线（tmp 账本注入 + tick mock）。

## Global Constraints

- **账本兼容**：`version` 保持 1（load 校验 `raw.get("version") != 1` 不动）；新键 `pending_buys`/`canceled_pending_codes` 缺省 `[]`（`setdefault` 在 load 时补——旧账本平滑迁移）；`init_account` 新账本直接含两键
- **状态机三结局（spec §2.3）**：①成交：`ΔV = tick.lastVolume − base_volume ≥ queued_shares + shares` **且** `|last − price| ≤ 0.001`（仍封板）→ 按 `price`（涨停价，无上滑）整单成交；②开板撤单：`last < price − 0.001` → 解冻 + 未成交流水（`queue_cancel_break`）+ code 进 `canceled_pending_codes`（当日禁重排）；③收盘失效：结算时清 `pending_buys`（`queue_expire` 流水 + 解冻）。**封单减少不算成交**
- **前置板质量检查（spec §2.1）**：创建委托时 ①封单金额 = `tick.bidVol[0] × price ≥ 2000万`（tick 缺失 → 保守不建记 skips"无盘口"）；②`now < 14:30` 才建（14:30 时点整体作废，选股照跑只记"尾盘不排"skips）
- **资金冻结（spec §3）**：创建时 `available = cash − Σpending.frozen ≥ amount` 否则跳过；成交扣款 = `shares×price + 买入费`（佣金万2.5+过户费万0.1，与既有 `_execute_buy` 同款）；解冻差额；`cash ≥ 0` 恒成立
- **三重幂等（spec §2.4）**：已持仓 / 今日已成交(trades buy date=今日) / 已在 pending / 今日已撤单(canceled_pending_codes) —— 四道任意一道挡 → 不建委托（`_buyable` 返回值复用）
- **卖出端跌停顺延（spec §4）**：`sell_check`：`last ≤ 昨收×(1−幅度)` → 当日跳过（跌停幅度：code 前缀 30/68 → 0.20，其余 0.10；昨收=`tick.lastClose`，缺失 → 不判跌停照常卖，fail-open 并披露）
- **只动白名单**：`prism/paper.py`、`prism/paper_daemon.py`、`prism_web/templates/index.html`、`prism_web/static/app.js`、测试文件；`data.py` **不动**（tick 数据经 daemon `tick_fn` 封装注入，`ds.get_full_market_ticks` 已存在）
- **tick 字段口径（xtdata full_tick 原生字段名，全链路统一）**：`lastPrice→现价`、`lastVolume→当日累计成交量(手)`、`bidVol[0]/bidPrice[0]→买一量/买一价(封单)`、`lastClose→昨收`；daemon `tick_fn(codes)->{code:{...原生字段...}}` 直接把 `ds.get_full_market_ticks` 结果原样透传（异常 → 空 dict，本轮不判定保守等待）；`sell_check` 既有 `lastPrice` 用法不变——同一命名空间
- **临界段纪律**：任何状态变更（建委托/成交/撤单/失效/解冻）走 `_snapshot_state()`→改→`save()`→失败回滚；**保存成功才算事件发生**
- **测试命令**：`--import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 从 **116** 起）；`$env:PYTHONIOENCODING='utf-8'` 先行；全离线
- **ponytail 约束（用户指定延续）**：复用既有 `_execute_buy`/`sell_check` 结构与临界段宏→最小 diff；不加队列抽象/不引入委托队列类（list 即可）；绝不简化掉：三结局判定、冻结不变量、三重幂等、原子保存
- git 只本地 commit，push 前问用户

---

### Task 1: 账本 v2（新键 + 兼容 + summary/detail 扩展）

**Files:**
- Modify: `prism/paper.py`（_REQUIRED_KEYS、init_account、load、summary、detail）
- Test: `prism/tests/test_paper_account.py`（追加）、`prism/tests/test_paper_cli.py` 抽查

**Interfaces:**
- Produces:
  - state 新键：`pending_buys: []`、`canceled_pending_codes: []`（账本文件内）
  - `summary()` 响应加 `"pending_count": N`
  - `detail(trade_limit=50)` 响应加 `"pending": [...]`（原样 list，含每项 code/shares/price/queued_shares/base_volume/created/slot/frozen）
  - 兼容：旧账本（无新键）load 后 `pending_buys=[]`、`canceled_pending_codes=[]`——**version 恒 1**

- [x] **Step 1: 写失败测试（追加 test_paper_account.py）**

```python
def test_ledger_v2_keys_present():
    """init 的新账本含排队键; summary/detail 带排队字段。"""
    from prism.paper import PaperAccount
    acc = PaperAccount(state_path=tmp_path / "p.json")
    acc.init_account()
    assert acc.state["pending_buys"] == []
    assert acc.state["canceled_pending_codes"] == []
    s = acc.summary()
    assert s["pending_count"] == 0
    d = acc.detail()
    assert d["pending"] == []


def test_ledger_v1_old_book_migrates():
    """旧账本(无新键, version=1) load 平滑: 缺省空列表。"""
    from prism.paper import PaperAccount
    import json
    st = {"version": 1, "created": "2026-09-01",
          "initial_capital": 1000000.0, "cash": 1000000.0,
          "holdings": [], "trades": [], "nav_history": [],
          "live_nav": 1000000.0, "screens_done": [], "settled_dates": []}
    p = tmp_path / "old.json"
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    acc = PaperAccount(state_path=p)
    assert acc.load() is True
    assert acc.state["pending_buys"] == []
    assert acc.state["canceled_pending_codes"] == []
    assert acc.state["version"] == 1
```

（注意 test 内 `tmp_path` 需 fixture 参数——模板上下文，实现者确保签名 `def test_...(tmp_path)`。）

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_account.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt116`
Expected: 新 2 测试 FAIL（KeyError pending_buys / summary 无字段）

- [x] **Step 3: 实现**

paper.py 三处小改：

```python
_REQUIRED_KEYS = ("version", "created", "initial_capital", "cash",
                  "holdings", "trades", "nav_history", "live_nav",
                  "screens_done", "settled_dates",
                  "pending_buys", "canceled_pending_codes")
```
（若 _REQUIRED_KEYS 原为 10 键，现 12 键——注意 load 的 `any(k not in raw)` 对旧账本会 False→新键必含。见下 load 兼容，两者配合。）

init_account state 加两键（"settled_dates": [] 后）：
```python
            "pending_buys": [],
            "canceled_pending_codes": [],
```

load 兼容（`self.state = raw` 前补缺省）：
```python
        raw.setdefault("pending_buys", [])
        raw.setdefault("canceled_pending_codes", [])
        self.state = raw
```
（_REQUIRED_KEYS 含新键 → 旧账本缺键 load 返回 False？矛盾！——**关键**：_REQUIRED_KEYS 检查必须在 setdefault **之前**也放行旧账本。见 Step 4 说明——检查改用「10 个核心键」子集，新键只 setdefault 不参与 must-have 校验。）

修正：load 的 must-have 校验用核心键集合（原 10 键），新键放宽：

```python
        _CORE_KEYS = ("version", "created", "initial_capital", "cash",
                      "holdings", "trades", "nav_history", "live_nav",
                      "screens_done", "settled_dates")
        if not isinstance(raw, dict) or any(k not in raw
                                            for k in _CORE_KEYS):
            return False
```
（`_REQUIRED_KEYS` 保留原 10 键定义不动——**不改 _REQUIRED_KEYS**，避免快照测试连锁；新键用 `_CORE_KEYS` 之外的 setdefault 补齐。实现时核对：若既有测试断言 _REQUIRED_KEYS 长度/内容，保持不变最稳。）

summary() 加：
```python
        "pending_count": len(self.state.get("pending_buys", [])),
```

detail() 加：
```python
        out["pending"] = list(self.state.get("pending_buys", []))
```

- [x] **Step 4: 跑测试确认通过 + paper 全回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_account.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt116`
Expected: 全 passed（存量 + 新 2）
再跑 `prism/tests` 全量确认零破坏。

- [x] **Step 5: Commit**

```bash
git add prism/paper.py prism/tests/test_paper_account.py
git commit -m "feat(prism): 账本v2扩展 — pending_buys/canceled_codes键+summary/detail字段+旧账本平滑"
```

---

### Task 2: 排板状态机核心（建委托 + 三结局 + 冻结）

**Files:**
- Modify: `prism/paper.py`（`buy_from_screen` 改道、`_buyable` 扩展、新增 `create_pending_buy`、`check_pending_buys`、`_execute_fill_buy`、`_dispose_pending`）
- Test: `prism/tests/test_paper_queue.py`（新建，约 10 测试）

**Interfaces:**
- Consumes: Task 1 账本新键；`datetime`/`_snapshot_state`/`_execute_buy` 费用宏（对齐用）
- Produces:
  - `create_pending_buy(code, up_price, now, tick) -> dict|None`：返回新委托 dict（或 None=skips；skips 原因并入 buy_from_screen 的 skipped 列表）
  - `check_pending_buys(ticks, now=None) -> {"filled":[...], "canceled":[...]}`：处理全部 pending 三结局（每个事件独立临界段）
  - `_execute_fill_buy(pending, now)`：成交记账（内部临界段）
  - `_dispose_pending(code, reason, now)`：撤单/失效共用（解冻 + 未成交流水 + 幂等键）

- [x] **Step 1: 写失败测试（新建 test_paper_queue.py，10 测试以下表为准）**

下表 10 个测试全部逐字落盘（fixture `acc(tmp_path)` 复用；tick 构造 helper `_tick(last_price, volume, bid_vol=0, last_close=None)` 返回 `{"lastPrice","lastVolume","bidVol":[bid_vol],"lastClose"}`）：

| # | 测试名 | 场景 | 关键断言 |
|---|---|---|---|
| 1 | `test_create_freezes_cash` | up=10.0 建委托 | pending 1 条、frozen≈金额、`cash` 不变=100万、`available_cash()`=100万−frozen |
| 2 | `test_fill_when_queue_crossed` | ΔV=queued+shares 且 last=price | holdings 1、cash=100万−成交额−费、trades 有 `queue_fill`、pending 空 |
| 3 | `test_cancel_on_break` | last < price−0.001 | pending 空、cash 复原、trades 有 `queue_cancel_break`、code 进 `canceled_pending_codes` |
| 4 | `test_expire_at_settle` | 调 `_dispose_pending(code,"queue_expire",now)` | pending 空、cash 复原、expire 流水 |
| 5 | `test_freeze_blocks_second` | 两笔都 30% 各 30 万 | 第二笔 `available_cash` 不足 → None，仅 1 条 pending |
| 6 | `test_dup_pending_blocked` | 同 code 已 pending | `_buyable` 返回"已在排队中" → `create_pending_buy` None |
| 7 | `test_canceled_code_blocks_retry` | 撤单后同 code | `canceled_pending_codes` 含 code → 再建 None（"今日已撤单"） |
| 8 | `test_gate_seal_amount` | bid_vol1×price < 2000万 | 建委托 None |
| 9 | `test_gate_tail_no_queue` | now=14:30:xx | 建委托 None |
| 10 | `test_fill_price_no_slip` | 成交价 | holdings 的 price == 挂单价 10.0（非 10.01） |

示例（测试 1 落盘形态）：
```python
def test_create_freezes_cash(acc):
    from datetime import datetime
    p = acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        _tick(10.0, 1_000_000, bid_vol=500_000))
    assert p is not None and p["shares"] == 3000        # 30万/10元 → 3000股
    assert p["frozen"] == pytest.approx(30000.0, rel=1e-3)
    assert len(acc.state["pending_buys"]) == 1
    assert acc.state["cash"] == 1_000_000.0             # 冻结不改 cash
    assert acc.available_cash() == pytest.approx(1_000_000.0 - 30000.0)
    out = acc.check_pending_buys({"600000": _tick(10.0, 1_000_000)},
                                 now=datetime(2026, 9, 2, 10, 0, 35))
    assert out == {"filled": [], "canceled": []}        # ΔV=0 不足
    assert len(acc.state["pending_buys"]) == 1          # 保持排队
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_queue.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt117`
Expected: FAIL（create_pending_buy 不存在）

- [x] **Step 3: 实现（paper.py）**

`buy_from_screen` 候选循环中 `done = self._execute_buy(code, up, now=now)` 改为：

```python
                tick = (provider_tick or {}).get(code) or {}
                p = self.create_pending_buy(code, up, now, tick)
                if p is None:
                    skipped.append({"code": code, "reason": "排板不通过"})
                    continue
                bought.append(p)
                nav = self._nav_estimate()
```
（`provider_tick`：buy_from_screen 新参 `provider_tick=None`，由 daemon 在时点前拉一次候选池 tick 传入——时点选股在守护进程里有 tick_fn，见 Task 3。**兼容**：不传时 tick={} → create 里 tick 缺失 → 封单门槛检查跳过但尾盘检查照走——不，spec 说 tick 缺失保守不建。定：tick 缺失 → 不建（skips "无盘口"）。daemon 必传 ✓ CLI 路径（--once 无盘口）会空——可接受，披露。）

`_buyable` 扩为四道（顺序保持语义）：

```python
    def _buyable(self, code, nav, now):
        st = self.state
        today = now.strftime("%Y-%m-%d")
        if any(h["code"] == code for h in st["holdings"]):
            return "已持仓"
        if any(t.get("side") == "buy" and t.get("code") == code
               and t.get("date") == today for t in st["trades"]):
            return "今日已交易"
        if any((p.get("code") == code) for p in st.get("pending_buys", [])):
            return "已在排队中"
        if code in st.get("canceled_pending_codes", []):
            return "今日已撤单"
        if len(st["holdings"]) >= self.max_positions:
            return "仓位已满"
        if st["cash"] < nav * self.position_ratio:
            return "现金不足"
        return None
```

新方法（放 `_execute_buy` 之前，费用宏复用）：

```python
    def available_cash(self):
        """可用现金 = cash − Σ冻结(排板挂单锁定额)。"""
        return self.state["cash"] - sum(p.get("frozen", 0.0)
                                        for p in self.state.get("pending_buys", []))

    def create_pending_buy(self, code, up_price, now, tick):
        """排板委托: 前置检查(五关+封单+尾盘)→冻结→入 pending。
        tick 缺失返回 None(保守: 无盘口不排)。"""
        if not tick:
            return None
        nav = self._nav_estimate()
        reason = self._buyable(code, nav, now)
        if reason:
            return None
        if now.strftime("%H:%M") >= "14:30":
            return None
        bv = tick.get("bidVol") or [0]
        bid_vol = bv[0] or 0
        if bid_vol * up_price < 20_000_000:
            return None
        # 金额(沿用 _execute_buy 的手数/金额口径: 净值30% → 100股取整)
        amount = nav * self.position_ratio
        shares = int(amount // (up_price * 100)) * 100
        if shares <= 0:
            return None
        frozen = shares * up_price
        if self.available_cash() < frozen:
            return None
        snap = self._snapshot_state()
        d = now.strftime("%Y-%m-%d")
        self.state["pending_buys"].append({
            "code": code, "shares": shares, "price": up_price,
            "amount": round(frozen, 4), "frozen": round(frozen, 4),
            "queued_shares": int(bid_vol),
            "base_volume": int(tick.get("lastVolume") or 0),
            "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "slot": now.strftime("T%H:%M")})
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return self.state["pending_buys"][-1]

    def check_pending_buys(self, ticks, now=None):
        """每轮排板判定: 成交/开板撤单/保持。每事件独立临界段。"""
        now = now or datetime.now()
        filled, canceled = [], []
        for p in list(self.state.get("pending_buys", [])):
            t = ticks.get(p["code"])
            if not t:
                continue                      # tick 缺失 → 保持排队
            last = t.get("lastPrice")
            if last is None:
                continue
            if last < p["price"] - 0.001:
                self._dispose_pending(p["code"], "queue_cancel_break", now)
                canceled.append(p["code"])
                continue
            dvol = int(t.get("lastVolume") or 0) - p["base_volume"]
            if dvol >= p["queued_shares"] + p["shares"] \
                    and abs(last - p["price"]) <= 0.001:
                if self._execute_fill_buy(p, now):
                    filled.append(p["code"])
        return {"filled": filled, "canceled": canceled}

    def _execute_fill_buy(self, p, now):
        """排板成交记账(临界段): 按挂单价成交, 无上滑; 解冻差額。"""
        code, shares, price = p["code"], p["shares"], p["price"]
        amount = shares * price
        fee = amount * 0.00026            # 佣金万2.5+过户费万0.1(同 _execute_buy)
        snap = self._snapshot_state()
        try:
            st = self.state
            if st["cash"] < amount + fee:
                return False
            st["cash"] = round(st["cash"] - amount - fee, 4)
            st["holdings"].append({"code": code, "shares": shares,
                                   "price": price, "fee": round(fee, 4),
                                   "buy_date": now.strftime("%Y-%m-%d")})
            st["trades"].append({"side": "buy", "code": code, "shares": shares,
                                 "price": price, "fee": round(fee, 4),
                                 "date": now.strftime("%Y-%m-%d"),
                                 "reason": "queue_fill",
                                 "ts": now.strftime("%Y-%m-%dT%H:%M:%S")})
            st["pending_buys"] = [x for x in st["pending_buys"]
                                  if x["code"] != code]
            self.save()
            return True
        except Exception:
            self._restore_state(snap)
            return False

    def _dispose_pending(self, code, reason, now):
        """撤单/失效(临界段): 解冻 + 未成交流水 + 幂等键。"""
        snap = self._snapshot_state()
        try:
            st = self.state
            p = next((x for x in st["pending_buys"] if x["code"] == code), None)
            if p is None:
                return
            st["pending_buys"] = [x for x in st["pending_buys"]
                                  if x["code"] != code]
            st["trades"].append({"side": "buy", "code": code,
                                 "shares": p["shares"], "price": p["price"],
                                 "date": now.strftime("%Y-%m-%d"),
                                 "reason": reason,
                                 "ts": now.strftime("%Y-%m-%dT%H:%M:%S")})
            if reason == "queue_cancel_break":
                if code not in st["canceled_pending_codes"]:
                    st["canceled_pending_codes"].append(code)
            self.save()
        except Exception:
            self._restore_state(snap)
```

（注：`_execute_fill_buy` 里 `now` 来自 check 传入；`holdings` 加仓字段与既有 `_execute_buy` 对齐——实现时对照现有 holdings 条目结构，避免字段不一致。）

- [x] **Step 4: 落全 10 测试并跑绿**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_queue.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt117`
Expected: 10 passed

- [x] **Step 5: 归回归（paper 全量）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt117`
Expected: 全绿（含既有 buy/sell/settle 测试——buy_from_screen 改道后 `test_paper_buy` 需同步：原测试期待 _execute_buy 直接成交 → 改为期待 pending 或适配 provider_tick 注入。**测试同步是本任务必做部分**）

- [x] **Step 6: Commit**

```bash
git add prism/paper.py prism/tests/test_paper_queue.py prism/tests/test_paper_buy.py
git commit -m "feat(prism): 排板状态机核心 — 建委托/三结局/冻结/三重幂等"
```

---

### Task 3: 卖出跌停顺延 + 守护集成（tick 轮询/结算清理/跨日清理）

**Files:**
- Modify: `prism/paper.py`（`sell_check` 加跌停跳过）
- Modify: `prism/paper_daemon.py`（`tick_fn` 封装、`tick_once` 每轮 `check_pending_buys`、结算前清 pending、跨日启动清理、时点 `provider_tick` 注入）
- Test: `prism/tests/test_paper_sell.py`（追加跌停）、`prism/tests/test_paper_daemon.py`（追加集成 3-4）

**Interfaces:**
- Consumes: Task 2 `check_pending_buys(ticks, now)`、`_dispose_pending(code, reason, now)`、`create_pending_buy(code, up, now, tick)`；既有 `sell_check`；daemon 既有 `tick_once`/`run_forever`/`connect_provider`
- Produces:
  - daemon 私有 `_quote_ticks(codes) -> dict`：`self.provider.ds.get_full_market_ticks(codes)` 原样透传（原生字段名），异常 → `{}`（fail-open 保守）
  - `tick_once` 每轮在 sell_check 之后调 `check_pending_buys(self._quote_ticks([...pending codes...]), now)`；时点分支调用 `buy_from_screen` 前先 `self._quote_ticks([候选 codes])` 注入 `provider_tick`
  - 结算分支（15:00 后）先清 pending（`_dispose_pending` each, reason `queue_expire`）再 `settle_day`
  - `run_forever` 启动时清理跨日 pending（created 非当日 → expire）
  - 卖出端：`sell_check` 持仓循环内、止盈止损判定前加跌停跳过

- [x] **Step 1: 写失败测试**

test_paper_sell.py 追加：

```python
def test_sell_skips_limit_down(acc_sell, tmp_path):
    """跌停日(现价 ≤ 昨收×0.9) 卖出跳过; 次日恢复可卖。"""
    from datetime import datetime
    # acc_sell: 已有持仓(买入昨日, 非 T+1), 设 cost 使现价在止盈/止损区间内
    acc_sell.state["trades"] = []   # 清空流水避免干扰
    tick = {"lastPrice": 9.0, "lastClose": 10.0}   # 10% 跌停: 10×0.9=9.0
    out = acc_sell.sell_check({"600000": tick},
                              now=datetime(2026, 9, 2, 14, 0, 0))
    assert out == [] and len(acc_sell.state["holdings"]) == 1   # 跳过不卖
    # 次日 12:00, 价格回到 10.8(非跌停, 且 >10×1.05 触发止盈) → 恢复可卖判定
    out2 = acc_sell.sell_check({"600000": {"lastPrice": 10.8,
                                           "lastClose": 10.0}},
                               now=datetime(2026, 9, 3, 12, 0, 0))
    assert len(out2) == 1
```

（`acc_sell` fixture 需在 test_paper_sell.py 已存在或自建——实现时按既有文件 fixture 惯例补：持仓 code="600000"、cost=10.0、buy_date="2026-09-01"。）

test_paper_daemon.py 追加：

```python
def test_tick_once_checks_pending_each_round(mk_daemon):
    """每轮排板检查: 成交后 pending 移除、仓位出现。"""
    from datetime import datetime
    daemon = mk_daemon()   # 既有工厂: account+ticks_fn 注入
    daemon.account.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        {"lastPrice": 10.0, "lastVolume": 1_000_000, "bidVol": [500_000],
         "lastClose": 10.0})
    daemon.account.state["screens_done"].append("T10:00")   # 屏蔽时点重跑
    now = datetime(2026, 9, 2, 10, 1, 0)
    # ticks_fn 返回穿越队列的成交量
    out = daemon.tick_once(now=now)
    assert out["pending_filled"] == ["600000"]
    assert len(daemon.account.state["pending_buys"]) == 0
    assert len(daemon.account.state["holdings"]) == 1
```

（tick_once 返回结构需扩展 `pending_filled`/`pending_canceled` 键——实现时并入返回 dict，既有断言不受影响因为 key 是新增。）

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_sell.py prism/tests/test_paper_daemon.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt118`
Expected: 新测试 FAIL（无跌停跳过 / tick_once 无 pending 检查）

- [x] **Step 3: 实现**

paper.py `sell_check` 止盈止损判定前插入（持仓循环内、`price <= 0` 检查后）：

```python
            # 跌停日卖不出(顺延次日): 现价 ≤ 昨收×(1-幅度); 幅度按板块
            lc = float(t.get("lastClose") or 0)
            if lc > 0:
                ratio = 0.20 if (h["code"].startswith("30")
                                 or h["code"].startswith("68")) else 0.10
                if price <= lc * (1 - ratio) + 0.001:
                    continue
```

paper_daemon.py：

```python
    def _quote_ticks(self, codes):
        """排板轮询行情(原生字段透传); 异常 → 空(fail-open 保守)。"""
        if not codes or not getattr(self.provider, "ds", None):
            return {}
        try:
            return self.provider.ds.get_full_market_ticks(list(codes)) or {}
        except Exception:
            return {}
```

`tick_once` 改造：
- 轮询分支（卖检查后）加：
```python
        pcodes = [p["code"] for p in self.account.state.get("pending_buys", [])]
        if pcodes:
            q = self._quote_ticks(pcodes)
            r = self.account.check_pending_buys(q, now=now)
            out["pending_filled"] = r["filled"]
            out["pending_canceled"] = r["canceled"]
```
- 时点分支（`buy_from_screen` 调用前）加 provider_tick 注入：
```python
                cand_codes = [c.get("code") for c in
                              (self._candidate_codes() or [])]   # 见下注
                ptick = self._quote_ticks(cand_codes) if cand_codes else {}
                res = self.account.buy_from_screen(self.provider, now=now,
                                                   slot=slot,
                                                   provider_tick=ptick)
```
  注：候选 codes 需在调 buy_from_screen 前可得——简化：时点先跑一次轻量"涨停池码列表"（provider.get_limit_ups 的 codes）再传；或 buy_from_screen 内部对每候选调 `_quote_ticks([code])`（一次一票，N≤10，可接受且最简——**选后者**：buy_from_screen 加可选 `tick_provider=callable(code)->tick`，daemon 传 `self._quote_ticks` 的单码版）：
```python
                # paper.py buy_from_screen 候选循环内:
                tick = {}
                if tick_provider:
                    try:
                        tick = (tick_provider([code]) or {}).get(code) or {}
                    except Exception:
                        tick = {}
                p = self.create_pending_buy(code, up, now, tick)
```
  （create_pending_buy 签名保持 `(code, up_price, now, tick)`——buy_from_screen 加 `tick_provider=None` 参数。）

- 结算分支（settle_day 之前）加：
```python
            for p in list(self.account.state.get("pending_buys", [])):
                self.account._dispose_pending(p["code"], "queue_expire", now)
```
- `run_forever` 启动清理（connect 成功、backfill 之后）：
```python
        today = datetime.now().strftime("%Y-%m-%d")
        for p in list(self.account.state.get("pending_buys", [])):
            if not (p.get("created") or "").startswith(today):
                self.account._dispose_pending(p["code"], "queue_expire",
                                              datetime.now())
```

- [x] **Step 4: 跑测试确认通过 + paper 全量回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt118`
Expected: 全绿（含既有 daemon/sell 测试——tick_once 返回键新增不影响既有断言）

- [x] **Step 5: Commit**

```bash
git add prism/paper.py prism/paper_daemon.py prism/tests/test_paper_sell.py prism/tests/test_paper_daemon.py
git commit -m "feat(prism): 卖出跌停顺延 + 守护排板轮询/结算清理/跨日清理"
```

---

### Task 4: GUI 排队区 + 全量回归 + 真实冒烟 + 收尾

**Files:**
- Modify: `prism_web/templates/index.html`（模拟盘 tab 加"排队中"区）
- Modify: `prism_web/static/app.js`（renderPaper 渲染 pending）
- Test: `prism_web/tests/test_app.py`（DOM 冒烟 1 + detail 字段契约）

**Interfaces:**
- Consumes: Task 1 `detail()` 的 `pending` 区；Task 2/3 全部
- Produces: 网页"排队中"可视（code/名称/委托价/股数/前方封单/等待时长）+ 未成交流水天然显示

- [x] **Step 1: 写失败测试**

test_app.py 追加：

```python
def test_paper_pending_dom(client):
    """模拟盘 tab 含排队区骨架。"""
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert "排队中" in html and "paper-pending" in html


def test_paper_detail_pending_field(client, monkeypatch):
    """detail 返回含 pending 区(空账本为空列表)。"""
    r = client.get("/api/paper/detail")
    assert r.status_code == 200
    assert "pending" in r.get_json()
    assert r.get_json()["pending"] == []
```

（test_paper_detail_pending_field 需复用既有 test_app 的 paper mock 机制——实现时核对现有 RealTmpAcc 注入模式沿用。）

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt119`
Expected: 新 2 测试 FAIL（无排队区 / detail 无 pending 键——Task 1 已加键则第二个可能已过，以实际为准）

- [x] **Step 3: 实现**

index.html 模拟盘 tab（持仓表前）加：

```html
        <h4>排队中 <span id="paper-pending-count" class="hint"></span></h4>
        <div id="paper-pending"></div>
```

app.js `renderPaper` 内（现有 holdings 渲染后）追加：

```js
  const pendBox = document.getElementById("paper-pending");
  const pendCount = document.getElementById("paper-pending-count");
  const pend = det.pending || [];
  if (pendCount) pendCount.textContent = pend.length ? `（${pend.length} 笔）` : "";
  pendBox.innerHTML = pend.length ? `
    <table class="tbl"><tr><th>代码</th><th>委托价</th><th>股数</th>
      <th>前方封单</th><th>已排队</th></tr>` + pend.map(p => `
      <tr><td>${escHtml(p.code)}</td><td>${p.price}</td><td>${p.shares}</td>
        <td>${p.queued_shares}</td>
        <td>${minsSince(p.created)}分</td></tr>`).join("") + `</table>`
    : `<div class="hint">无排队委托</div>`;
```

（`minsSince` 小助手：按分钟差格式化；`escHtml` 已存在 ✓；`det` 为 renderPaper 现 detail 变量名——实现时按实际命名核对。）

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt119`
Expected: 全绿（基线 401 + 新增 ~18 = 约 419）

- [x] **Step 5: 真实冒烟（用户在场；收盘后主要验证结算清理路径）**

1. `python -m prism.paper --summary` → 确认账本正常（含 pending_count 字段）
2. 手动造一条 pending 验证存活/清理：用 `--once`?（CLI 无建委托入口——冒烟用测试账户 json 手工注入 pending → 起守护短跑 → 观察当日清理/跨日清理）——**简化**：验证 `--summary` 字段 + 守护启动日志无异常 + 若开盘时段则观察真实排队（不在场时段则等下一交易日）
3. 网页模拟盘 tab：排队区可见（无委托时"无排队委托"提示）
4. 安全确认：QMT_SIGNALS 零写入；账本原子保存正常

- [x] **Step 6: 台账 + 勾选 + 交付**

```bash
git add docs/superpowers/plans/2026-09-02-paper-queue.md
git commit -m "docs(plan): 排板队列状态机实施完成(4任务/全量回归/冒烟)"
```

- [x] **Step 7: 交付报告（通俗）**

要点：模拟盘现在"打板要排队"了（成交要穿越队列、开板就撤、收盘作废）；网页"排队中"栏看得到正在排什么；未成交流水透明可查；卖出端跌停顺延；数字会比以前难看是真实的；等待下一交易日真实验收（10:00 时点看真实排队/成交/撤单）