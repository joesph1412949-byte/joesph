# 次日开盘前五执行策略 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** full_factor_v1 就地升级——三层等权打分 + 收盘选前五、次日开盘价买入、一字板转排队、止盈 15%。

**Architecture:** 选股引擎(run_screen)不动；执行层进策略 JSON `execution` 块；paper.py 新增 pick_top5_at_close/execute_open_buys 两方法；paper_daemon.py 调度改为 15:05 选股 + 09:26-09:35 开盘买入窗口，移除盘中排队时点。

**Tech Stack:** Python 3.12 stdlib + pytest；Windows；策略 JSON。

## Global Constraints

- 规格文件: `docs/superpowers/specs/2026-09-04-open-top5-execution-design.md`（唯一事实源，冲突以 spec 为准）
- 测试命令: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt127`（basetemp 序号递增，不重复用）
- 现有基线 419 全绿；每任务后不得回退
- ponytail 约束: 能复用不复写、stdlib 优先、最小改动；`# ponytail:` 标注留下的捷径
- 校验完整性/原子写/幂等键/指针回落 等 既有安全机制 一律不得简化
- 工作目录 `D:\cc-joesph`；提交本地即可不推远端

---

### Task 1: 策略 JSON 等权 + execution 块 + TP 0.15

**Files:**
- Modify: `prism/strategies/full_factor_v1.json`
- Modify: `prism/tests/test_factor_migration.py`（test_full_factor_v1_loads 断言扩展）
- Check: `prism_web/static/app.js`（编辑器对 composite.weights 的依赖，仅查不重构）

**Interfaces:**
- Produces: JSON 顶层新增 `"execution": {"mode": "next_open_topn", "top_n": 5, "pct": 0.15, "open_window": "09:26-09:35", "pick_slot": "15:05", "one_word_fallback": "queue"}`；`composite` = `{"mode": "average", "cap": 9.3333}`（weights 键删除）；`sell_rules.take_profit_pct` = 0.15。Task 2/3/4 依赖这些键名。

- [ ] **Step 1: 改 JSON**（逐字）

```json
  "composite": {"mode": "average", "cap": 9.3333},
  "filters": {"candidate_min_model": 3, "environment_threshold": 3},
  "sell_rules": {"take_profit_pct": 0.15, "stop_loss_pct": 0.05,
                 "max_hold_days": 5},
  "execution": {"mode": "next_open_topn", "top_n": 5, "pct": 0.15,
                "open_window": "09:26-09:35", "pick_slot": "15:05",
                "one_word_fallback": "queue"}
```

（composite 行替换原 L16-17 两行；sell_rules 替换 L19-20；execution 插在 sell_rules 之后、收尾 `}` 之前。scoring_models 里的 weight 字段保留不动——模型级 weight 仅 top3_weighted 模式使用，average 模式忽略，删除会牵连编辑器回显。）

- [ ] **Step 2: 测试断言扩展**（test_factor_migration.py 的 test_full_factor_v1_loads 内追加）

```python
    comp = s["composite"]
    assert comp["mode"] == "average" and abs(comp["cap"] - 9.3333) < 1e-9
    assert "weights" not in comp
    ex = s["execution"]
    assert ex["top_n"] == 5 and ex["pct"] == 0.15
    assert s["sell_rules"]["take_profit_pct"] == 0.15
```

- [ ] **Step 3: 编辑器依赖检查**：`git grep -n "top3_weighted\|composite" -- prism_web/static/app.js`。若编辑器加载策略时读 `composite.weights` 用于回显（模板策略编辑页），给缺失分支加空值兜底（如 `(s.composite||{}).weights||[]`），不加新功能。

- [ ] **Step 4: 跑测**：`python -m pytest prism/tests/test_factor_migration.py prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt127` → 全绿。

- [ ] **Step 5: Commit** `feat(prism): full_factor_v1 三层等权+execution块+止盈15%`

---

### Task 2: paper.py 收盘选股 pick_top5_at_close + 仓位从 execution.pct

**Files:**
- Modify: `prism/paper.py`（策略加载点、新方法）
- Create: `prism/tests/test_open_top5.py`

**Interfaces:**
- Consumes: 引擎 `run_screen(strategy, market_ctx, gate_factors, stock_contexts)`（与 buy_from_screen L187-212 同一编排）；Task 1 的 `execution.top_n/pct/pick_slot`。
- Produces: `PaperAccount.pick_top5_at_close(provider, now=None, slot=None) -> {"picked": [...], "env_ok": bool, "already_done"?: True}`；state 新键 `planned_buys` = `[{"code": str, "score": float, "date": "YYYY-MM-DD", "for_date": "YYYY-MM-DD"}]`；模块函数 `_next_weekday(d: str) -> str`（跳过周六日）；`self.position_ratio` 在策略加载后被 `execution.pct` 覆盖（无 execution 块回落构造参数值）。

- [ ] **Step 1: 写失败测试**（test_open_top5.py 新建；provider/tick 构造参照 test_paper_queue.py 的 _tick 与 test_paper_buy.py 的 fake provider 模式，全离线）

```python
# -*- coding: utf-8 -*-
"""次日开盘前五执行: 收盘选股 + 开盘买入 + 一字板替补 (全离线)。"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism.paper import PaperAccount, _next_weekday


STRATEGY = {"market_gate": {"factors": ["N1"], "threshold": 1},
            "scoring_models": [{"id": "m", "weight": 1.0, "factors": ["F1"]}],
            "composite": {"mode": "average", "cap": 9.3333},
            "sell_rules": {"take_profit_pct": 0.15, "stop_loss_pct": 0.05,
                           "max_hold_days": 5},
            "execution": {"mode": "next_open_topn", "top_n": 2, "pct": 0.15,
                          "open_window": "09:26-09:35", "pick_slot": "15:05",
                          "one_word_fallback": "queue"}}


class FakeProvider:
    """build_market_context→门禁因子全 1; get_limit_ups→候选池。"""
    def __init__(self, codes):
        self.codes = codes
    def build_market_context(self):
        class Ctx:  # N1 函数读什么本测试不依赖——门禁因子直接注入
            pass
        return {"__fake__": True}
    def get_limit_ups(self):
        return [{"code": c, "up_stop_price": 10.0} for c in self.codes]
    def build_stock_context(self, code):
        return {"code": code}


@pytest.fixture
def acc(tmp_path, monkeypatch):
    sp = tmp_path / "s.json"
    import json
    sp.write_text(json.dumps(STRATEGY), encoding="utf-8")
    a = PaperAccount(strategy_path=sp)
    a.init_account(created="2026-09-04")
    # 门禁/评分因子离线桩: N1/F1 恒 1(分数=组合分恒定, 靠代码决胜测 topN)
    import prism.registry as reg
    monkeypatch.setattr(reg, "FACTOR_FUNCS", {"N1": lambda ctx: {"score": 1},
                                              "F1": lambda ctx: {"score": 1}},
                        raising=False)
    # 引擎取因子走 reg.get_factor(fid)["func"] —— 若注册表结构不同,
    # 按实际结构打桩(实现者读 registry.py 后对齐), 意图: N1/F1 恒命中
    return a


def test_next_weekday_skips_weekend():
    assert _next_weekday("2026-09-04") == "2026-09-07"   # 周五→周一
    assert _next_weekday("2026-09-07") == "2026-09-08"   # 周一→周二


def test_pick_top5_stores_plans(acc, monkeypatch):
    prov = FakeProvider(["600000", "600001", "600002"])
    monkeypatch.setattr("prism.engine.run_screen",
                        lambda strategy, market_ctx, gate_factors=None,
                        stock_contexts=None: {
                            "environment_ok": True, "gate_score": 1,
                            "candidates": [
                                {"code": c, "up_stop_price": 10.0,
                                 "scores": {"composite": 5.0 - i}} for i, c in
                                enumerate(prov.codes)],
                            "summary": {}})
    out = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 5),
                                 slot="15:05")
    assert out["env_ok"] and len(out["picked"]) == 2      # top_n=2
    plans = acc.state["planned_buys"]
    assert [p["code"] for p in plans] == ["600000", "600001"]  # 分降序
    assert all(p["for_date"] == "2026-09-07" for p in plans)   # 周五→周一
    assert "2026-09-04T15:05" in acc.state["screens_done"]     # 幂等键
    # 重复调用同日同时点 → already_done, 计划不变
    out2 = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 6),
                                  slot="15:05")
    assert out2.get("already_done") and len(acc.state["planned_buys"]) == 2


def test_pick_gate_fail_empty(acc, monkeypatch):
    prov = FakeProvider(["600000"])
    monkeypatch.setattr("prism.engine.run_screen",
                        lambda strategy, market_ctx, gate_factors=None,
                        stock_contexts=None: {
                            "environment_ok": False, "gate_score": 0,
                            "candidates": [], "summary": {}})
    out = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 5),
                                 slot="15:05")
    assert out["env_ok"] is False and acc.state["planned_buys"] == []


def test_pick_replaces_old_plans(acc, monkeypatch):
    acc.state["planned_buys"] = [{"code": "999999", "score": 9.0,
                                  "date": "2026-09-03",
                                  "for_date": "2026-09-04"}]
    ...同 test_pick_top5_stores_plans 的 run_screen 桩...
    assert [p["code"] for p in acc.state["planned_buys"]] != ["999999"]
```

（`...` 处按 test_pick_top5_stores_plans 原样补全桩——计划不重复粘贴，实现者复制同文件已有桩。分数并列决胜用例：候选 composite 相同 → 按 code 升序取前 N。）

- [ ] **Step 2: 跑测确认 FAIL**（`pick_top5_at_close`/`_next_weekday` 不存在 → AttributeError/ImportError）

- [ ] **Step 3: 实现**。paper.py 模块级：

```python
def _next_weekday(d):
    """下一交易日(仅跳周六日, 不含节假日历——ponytail: 模拟盘可接受)。"""
    from datetime import date, timedelta
    y, m, dd = map(int, d.split("-"))
    cur = date(y, m, dd) + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur.isoformat()
```

仓位覆盖（在加载策略 JSON 的赋值点之后——`grep -n "self.strategy = " prism/paper.py` 定位）：

```python
        # execution.pct 覆盖默认仓位(spec §4); 无 execution 块回落构造参数
        exec_pct = ((self.strategy or {}).get("execution") or {}).get("pct")
        if exec_pct:
            self.position_ratio = float(exec_pct)
```

选股方法（与 buy_from_screen 共用编排，抽小helper避免复制门禁求值段 L190-200）：

```python
    def _screen_candidates(self, provider):
        """门禁求值 + 引擎选股(pick/盘中排队共用编排)。"""
        from prism.engine import run_screen
        from prism import registry as reg
        market_ctx = provider.build_market_context()
        gate_fids = (self.strategy.get("market_gate") or {}).get("factors", [])
        gate_factors = {}
        for fid in gate_fids:
            try:
                res = reg.get_factor(fid)["func"](market_ctx)
                if isinstance(res, dict):
                    gate_factors[fid] = 1 if res.get("score") else 0
                else:
                    gate_factors[fid] = 1 if res else 0
            except Exception:
                gate_factors[fid] = 0
        limit_ups = provider.get_limit_ups()
        stock_contexts = {}
        for lu in limit_ups:
            try:
                stock_contexts[lu["code"]] = provider.build_stock_context(
                    lu["code"])
            except Exception:
                pass
        result = run_screen(self.strategy, market_ctx,
                            gate_factors=gate_factors,
                            stock_contexts=stock_contexts)
        return result

    def pick_top5_at_close(self, provider, now=None, slot=None):
        """收盘选股(spec §5): 门禁→打分→前 top_n 存 planned_buys(整体替换)。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        d = now.strftime("%Y-%m-%d")
        ts_key = "pickT%s" % (slot or now.strftime("%H:%M"))
        if ts_key in self.state["screens_done"]:
            return {"picked": [], "env_ok": False, "already_done": True}
        top_n = int(((self.strategy.get("execution") or {}).get("top_n"))
                    or 5)
        try:
            result = self._screen_candidates(provider)
        except Exception as e:
            snap = self._snapshot_state()
            self.state["screens_done"].append(ts_key)
            try:
                self.save()
            except Exception:
                self._restore_state(snap)
            return {"error": "选股失败: %r" % e}
        env_ok = bool(result.get("environment_ok"))
        cands = sorted(result.get("candidates", []),
                       key=lambda c: (-float(c.get("scores", {})
                                            .get("composite", 0)),
                                      str(c.get("code"))))
        picked = [{"code": c["code"],
                   "score": float(c.get("scores", {}).get("composite", 0)),
                   "date": d, "for_date": _next_weekday(d)}
                  for c in cands[:top_n if env_ok else 0]]
        snap = self._snapshot_state()
        self.state["planned_buys"] = picked
        self.state["screens_done"].append(ts_key)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "状态保存失败"}
        return {"picked": picked, "env_ok": env_ok}
```

同时把 buy_from_screen 的门禁求值段改为调用 `self._screen_candidates(provider)`（消重；buy_from_screen 本身保留——打板机制保有，仅守护调度不再调它）。幂等键加 `pickT` 前缀与盘中 `T%H:%M` 键不冲突。

- [ ] **Step 4: 跑测 PASS** + `python -m pytest prism/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt127` 全绿。

- [ ] **Step 5: Commit** `feat(prism): 收盘选股前五落计划+仓位从execution.pct`

---

### Task 3: paper.py 开盘买入 execute_open_buys + 无滑点买入

**Files:**
- Modify: `prism/paper.py`（_execute_buy 加 slip 参数、新方法）
- Modify: `prism/tests/test_open_top5.py`

**Interfaces:**
- Consumes: Task 2 的 `planned_buys` 结构；`create_pending_buy(code, up_price, now, tick)`（既有，封单门槛/半残守卫/冻结全 inherited）；`_execute_buy(code, up_price, now=None)`（L433）。
- Produces: `_execute_buy(self, code, up_price, now=None, slip=None)`（slip=None→self.slippage；0.0→无滑点，返回值不变）；`PaperAccount.execute_open_buys(tick_provider, now=None) -> {"bought": [...], "queued": [...], "skipped": [{code, reason}]}`——消费 `for_date == 今日` 的计划并整体清空；`for_date != 今日` 的计划不动。

- [ ] **Step 1: 写失败测试**（追加进 test_open_top5.py；tick 用 test_paper_queue._tick 同构 `{"lastPrice", "lastVolume", "bidVol", "lastClose", "open"}`——注意需带 `open` 字段，扩展本地 helper）

```python
def _otick(open_px, last_close, bid_vol=100_000, last_volume=1_000_000):
    """开盘买入用 tick: open=开盘价, lastClose=昨收。"""
    return {"lastPrice": open_px, "open": open_px, "lastClose": last_close,
            "lastVolume": last_volume, "bidVol": [bid_vol]}


def test_open_buy_at_open_price(acc, monkeypatch):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"600000": _otick(10.50, 10.0)}       # 开盘+5%, 非板
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["bought"] == ["600000"] and out["queued"] == []
    h = acc.state["holdings"][0]
    assert h["cost"] == 10.50                      # 开盘价无滑点
    assert h["shares"] == 14200                    # 100万×15%=15万; 150000//(10.5*100)=142手→14200股
    assert acc.state["planned_buys"] == []         # 消费后清空


def test_open_buy_one_word_queues(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"600000": _otick(11.0, 10.0, bid_vol=2_000_000)}  # 开盘=涨停(一字)
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["queued"] == ["600000"] and out["bought"] == []
    p = acc.state["pending_buys"][0]
    assert p["price"] == 11.0 and p["base_volume"] == 1_000_000  # 走排队状态机
    assert acc.state["planned_buys"] == []


def test_open_buy_down_limit_skipped(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"600000": _otick(9.0, 10.0)}          # 开盘=跌停
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["bought"] == [] and out["queued"] == []
    assert out["skipped"] == [{"code": "600000", "reason": "跌停开盘"}]
    assert acc.state["planned_buys"] == []         # 仍清空(消费)


def test_open_buy_missing_tick_skipped(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    out = acc.execute_open_buys(lambda codes: {},
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["skipped"] == [{"code": "600000", "reason": "无行情"}]
    assert acc.state["planned_buys"] == []


def test_open_buy_stale_plan_untouched(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-03",
                                  "for_date": "2026-09-04"}]   # 昨日计划
    out = acc.execute_open_buys(lambda codes: {"600000": _otick(10.5, 10.0)},
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["bought"] == [] and len(acc.state["planned_buys"]) == 1
```

- [ ] **Step 2: 跑测 FAIL**（方法不存在）。

- [ ] **Step 3: 实现**。_execute_buy 签名加 `slip=None`，`buy_price = round(up_price * (1 + (self.slippage if slip is None else slip)), 4)`。新方法：

```python
    def execute_open_buys(self, tick_provider, now=None):
        """开盘买入窗口消费(spec §5): 按开盘价买/一字板转排队/跌停跳过。
        仅消费 for_date==今日 的计划; 处理后整体清空。窗口判定在守护侧。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        today = now.strftime("%Y-%m-%d")
        plans = [p for p in self.state.get("planned_buys", [])
                 if p.get("for_date") == today]
        if not plans:
            return {"bought": [], "queued": [], "skipped": []}
        try:
            ticks = tick_provider([p["code"] for p in plans]) or {}
        except Exception:
            ticks = {}
        bought, queued, skipped = [], [], []
        snap = self._snapshot_state()
        try:
            for p in plans:
                code = p["code"]
                t = ticks.get(code)
                if not t or not t.get("open") or not t.get("lastClose"):
                    skipped.append({"code": code, "reason": "无行情"})
                    continue
                prev = float(t["lastClose"])
                up = round(prev * 1.1, 2)          # 今日涨停价(主 板 10%)
                low = round(prev * 0.9, 2)         # 今日跌停价
                open_px = float(t["open"])
                nav = self._nav_estimate()
                reason = self._buyable(code, nav, now)
                if reason:
                    skipped.append({"code": code, "reason": reason})
                    continue
                if open_px >= up - 0.001:          # 开盘即板 → 排队替补
                    if self.create_pending_buy(code, up, now, t):
                        queued.append(code)
                    else:
                        skipped.append({"code": code, "reason": "排板不通过"})
                elif open_px <= low + 0.001:       # 跌停开盘 → 保护跳过
                    skipped.append({"code": code, "reason": "跌停开盘"})
                elif self._execute_buy(code, open_px, now, slip=0.0):
                    bought.append(code)
                else:
                    skipped.append({"code": code, "reason": "买入失败"})
            self.state["planned_buys"] = [
                x for x in self.state.get("planned_buys", [])
                if x.get("for_date") != today]
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "开盘买入失败"}
        return {"bought": bought, "queued": queued, "skipped": skipped}
```

涨停/跌停价口径：主板 ±10% `round(prev*1.1, 2)`/`round(prev*0.9, 2)`——与引擎 up_stop_price 同式（`grep -n "1.1" prism/engine.py` 对齐写法）。ST/创业板差异属既有引擎口径，不在此扩（`# ponytail: 跟随引擎口径`）。

- [ ] **Step 4: 跑测 PASS** + `python -m pytest prism/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt127` 全绿。

- [ ] **Step 5: Commit** `feat(prism): 开盘买入窗口(开盘价成交/一字板转排队/跌停跳过)`

---

### Task 4: 守护调度重接 + 启动清理 + 测试同步

**Files:**
- Modify: `prism/paper_daemon.py`（L13 SCREEN_TIMES、L120-133 排队时点循环、run_forever L216 启动清理）
- Modify: `prism/tests/test_paper_daemon.py`（10:00/13:30 用例改造）

**Interfaces:**
- Consumes: Task 2 `pick_top5_at_close(provider, now, slot="15:05")`；Task 3 `execute_open_buys(tick_provider, now)`；既有 tick_provider 注入模式（daemon 现调用 buy_from_screen 的传参方式，`grep -n "tick_provider" prism/paper_daemon.py`）。
- Produces: 调度常量 `PICK_SLOT = "15:05"`、`OPEN_WINDOW = ("09:26", "09:35")`；tick_once 动作键扩展 `"pick"`/`"open_buys"`；启动清理清 `for_date < 今日` 的 planned_buys。

- [ ] **Step 1: 改造失败测试**（test_paper_daemon.py：删/改 10:00/13:30 建队列断言；新增）

```python
def test_tick_once_pick_slot(monkeypatch, ...):
    """hm>=15:05 且未 pick 过 → pick_top5_at_close 触发, action=pick。"""
    # 桩 account.pick_top5_at_close 记录调用; now=周五 15:06
    # 断言 out["action"]=="pick" 且 slot=="15:05"

def test_tick_once_open_window(monkeypatch, ...):
    """09:26-09:35 且有今日计划 → execute_open_buys 触发, action=open_buys。"""
    # 桩 account.execute_open_buys; now=交易日 09:30

def test_tick_once_no_intraday_queue(monkeypatch, ...):
    """10:00/13:30 不再触发 buy_from_screen。"""
    # 桩 buy_from_screen 断言不被调用; now=10:01

def test_startup_purges_stale_plans(...):
    """启动清理: for_date<今日 的 planned_buys 清空。"""
```

（桩的具体构造与该文件既有 fake provider/account 模式一致——实现者读现有测试后对齐，断言语义如注释。）

- [ ] **Step 2: 跑测 FAIL**。

- [ ] **Step 3: 实现**。L13：

```python
PICK_SLOT = "15:05"
OPEN_WINDOW = ("09:26", "09:35")
```

tick_once 中 L120-133 的 SCREEN_TIMES 循环整段删除，替换为：

```python
        # 开盘买入窗口(spec §5): 09:26-09:35, 消费 for_date==今日 的计划
        if now.weekday() < 5 and OPEN_WINDOW[0] <= hm <= OPEN_WINDOW[1] \
                and self.provider:
            if self.account.state.get("planned_buys"):
                out["open_buys"] = self.account.execute_open_buys(
                    self._candidate_ticks, now=now)
                out["action"] = "open_buys"
        # 收盘选股(spec §5): 15:05(settle 分支之后, 同 tick 顺序天然靠后)
        if now.weekday() < 5 and hm >= PICK_SLOT \
                and ("pick%s" % d) not in st.get("screens_done", []) \
                and self.provider:
            out["pick"] = self.account.pick_top5_at_close(
                self.provider, now=now, slot=PICK_SLOT)
            out["action"] = "pick"
```

（pick 幂等键实际由 pick_top5_at_close 内 `pickT15:05` 管；守护侧 `pick%sT%s` 判定可省——以实现时与 settle/窗口分支的先后顺序为准，保证同 tick 先 settle 后 pick。`self._candidate_ticks` 名以 daemon 现有 tick_provider 注入实现为准对齐。）

run_forever 启动清理（L216 跨日清理处）追加：

```python
        # 计划清理(spec §6): for_date<今日 的开盘买入计划作废(记 skip 不追买)
        today = datetime.now().strftime("%Y-%m-%d")
        stale = [p for p in self.account.state.get("planned_buys", [])
                 if p.get("for_date", "") < today]
        if stale:
            self.account.state["planned_buys"] = [
                p for p in self.account.state["planned_buys"]
                if p.get("for_date", "") >= today]
            self.account.save()
            logger.info("计划清理: 作废 %d 只(错过开盘窗口)" % len(stale))
```

- [ ] **Step 4: 跑测 PASS** + 全量 `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt127` 全绿（基线 419+新增）。

- [ ] **Step 5: Commit** `feat(prism): 守护调度改为15:05选股+09:26开盘买入窗口(移除盘中排队时点)`

---

### Task 5: 收尾——MEMORY.md + 台账 + 交付

- [ ] **Step 1:** MEMORY.md 模拟盘行更新：执行节奏（15:05 选前五 → 次日 09:26-09:35 开盘价买 15%/只 → 一字板转排队 → 止盈 15%/止损 5%/5 日）、三层等权、归因切换点（生效首个交易日）。
- [ ] **Step 2:** `.superpowers/sdd/progress.md` 记录三任务与验证证据；plan 复选框全勾。
- [ ] **Step 3:** Commit `docs: 开盘前五执行策略落地记录`。
