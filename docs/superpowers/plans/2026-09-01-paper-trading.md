# 模拟实盘账户（100 万 / first_board_v04）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一个活的 A 股模拟账户：100 万虚拟资金、first_board_v04 策略、盘中实时盯盘（实时止盈止损 + 三时点选股买入 + 盘后结算），账本可重启续跑，网页可查盈亏。

**Architecture:** 全部为新增文件——`prism/paper.py`（账户引擎：账本/买卖/结算/查询）、`prism/paper_daemon.py`（常驻调度：tick 轮询 + 时点选股 + 收盘结算）、GUI 只读端点与面板。选股复用现有 `run_screen`+`DataProvider` 链路；记账口径与回测 `Backtester` 完全一致（滑点/佣金/印花税/过户费同值）。状态 `.paper_account.json` 为唯一真相源，原子写 + 保存成功才算交易发生。

**Tech Stack:** Python 3.12 / pytest / QMT(xtquant) 实时行情（仅运行时，测试全 mock）/ Flask + 原生 JS。

## Global Constraints

- **安全边界（spec §8 原样绑定，逐条遵守）：**
  1. 零真实下单：不写 `D:\QMT_SIGNALS`、不调用任何下单接口
  2. 不改实盘行为：只新增文件；实盘在用模块（trader.py/data_source.py/engine.py 等）只调用不修改
  3. 账本完整性：原子写（temp+`os.replace`）；保存成功才算交易发生；流水 append-only；JSON 加载失败 → 拒绝启动保留原文件
  4. 资金不变量：现金 ≥0、持仓 ≤5，执行前校验，绝不记负现金/超仓位
  5. 只读数据：不写任何 .pkl
  6. 进程隔离：模拟盘崩溃只影响自身，实盘零影响
  7. GUI 只读：模拟盘 API 无写端点（初始化走 CLI）
  8. 资源克制：盘中 5 秒一轮轻检查；重活（选股）只跑 3 时点；收盘后待机
- **记账常量（与回测一致，原样使用）：** `fee_rate=0.00025`、`slippage=0.001`、`stamp_duty=0.0005`、`transfer_fee=0.00001`、`position_ratio=0.3`、`max_positions=5`；止盈 0.08/止损 0.05/持有 5 个交易日（读策略 `sell_rules`，缺省同上）
- **测试命令必须带** `--import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 从 **84** 起递增）
- **全部测试离线**：mock 数据源/行情/时钟；**账本文件一律注入 `state_path=tmp_path/...`，绝不读写真实 `.paper_account.json`**；真网仅 Task 8 真实冒烟
- **可修改文件清单（白名单）：** 新增 `prism/paper.py`、`prism/paper_daemon.py`、`prism/tests/test_paper*.py`、`prism_web/app.py`（仅追加端点）、`prism_web/static/app.js`（仅追加渲染函数）、`prism_web/templates/index.html`（仅追加面板）、`prism_web/tests/test_app.py`（仅追加测试）、`.gitignore`（追加一行）。其余文件一律不动。
- **接口契约（跨任务）：**
  - 账本结构：`{"version":1, "created","initial_capital","cash","holdings":[{"code","shares","cost","buy_date","buy_price","entry_nav"}], "trades":[{"ts","date","side","code","price","shares","amount","fee","reason","cash_after"}], "nav_history":[{"date","nav"}], "live_nav", "screens_done":["YYYY-MM-DDTHH:MM"], "settled_dates":["YYYY-MM-DD"]}`
  - 买入费用：`amount = shares*buy_price`；`fee = amount*(fee_rate+transfer_fee)`；现金扣 `amount+fee`
  - 卖出费用：`amount = shares*sell_price`；`fee = amount*(fee_rate+stamp_duty+transfer_fee)`；现金回款 `amount-fee`
  - `buy_price = up_price*(1+slippage)`（up_price=池条目 up_stop_price）；`sell_price = tick_last*(1-slippage)`
  - 选股信号：`buy_from_screen(provider)` 内部复刻 `_run_prism_screen` 流程（build_market_context → gate 预计算 → get_limit_ups → 逐股 build_stock_context → run_screen）
- Windows 控制台 GBK：跑 python 前 `$env:PYTHONIOENCODING='utf-8'`
- git 只本地 commit，**push 前必须问用户**

---

### Task 1: 账本核心（状态/原子保存/加载校验/回滚）

**Files:**
- Create: `prism/paper.py`（本任务只实现账本部分 + `summary`/`detail` 查询）
- Test: `prism/tests/test_paper_account.py`（新建）

**Interfaces:**
- Produces:
  - `PaperAccount(strategy_path=None, initial_capital=1000000.0, position_ratio=0.3, max_positions=5, fee_rate=0.00025, slippage=0.001, stamp_duty=0.0005, transfer_fee=0.00001, state_path=None)`；`state_path` 缺省 = 仓库根 `.paper_account.json`；`strategy_path` 缺省 = `prism/strategies/first_board_v04.json`
  - `init_account(created=None) -> dict`：写入初始账本（幂等：已存在返回现有状态不覆盖）
  - `load() -> bool`：读状态到 `self.state`；文件不存在 → False；JSON 解析失败/结构校验不过 → False 且**不覆盖原文件**
  - `save() -> None`：原子写（`.json.tmp` → `os.replace`）
  - `_snapshot_state() -> dict` / `_restore_state(snap)`：执行临界段用（deepcopy 快照与恢复）
  - `summary() -> dict`、`detail(trade_limit=50) -> dict`：未初始化 → `{"exists": False}`；正常 → 见下方结构
  - 常量 `STATE_FILENAME = ".paper_account.json"`

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""PaperAccount 账本核心测试 — 全离线, 账本一律注入 tmp_path。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.paper import PaperAccount


def _acc(tmp_path, **kw):
    return PaperAccount(state_path=tmp_path / "paper.json", **kw)


def test_init_creates_ledger(tmp_path):
    acc = _acc(tmp_path)
    st = acc.init_account(created="2026-09-01")
    assert st["version"] == 1
    assert st["cash"] == 1000000.0
    assert st["initial_capital"] == 1000000.0
    assert st["holdings"] == [] and st["trades"] == []
    assert st["nav_history"] == []
    assert st["screens_done"] == [] and st["settled_dates"] == []
    assert st["live_nav"] == 1000000.0
    # 幂等: 二次 init 不覆盖
    st2 = acc.init_account(created="2026-09-02")
    assert st2["created"] == "2026-09-01"


def test_load_missing_returns_false(tmp_path):
    acc = _acc(tmp_path)
    assert acc.load() is False
    assert not (tmp_path / "paper.json").exists()   # 不误建文件


def test_load_corrupt_keeps_file(tmp_path):
    p = tmp_path / "paper.json"
    p.write_text("{broken json!!", encoding="utf-8")
    acc = _acc(tmp_path)
    assert acc.load() is False          # 拒绝加载
    assert p.read_text(encoding="utf-8") == "{broken json!!"  # 原文件保留
    # 结构校验: 缺关键键也拒绝
    p.write_text(json.dumps({"version": 1, "cash": 1.0}), encoding="utf-8")
    assert acc.load() is False


def test_save_atomic_and_roundtrip(tmp_path):
    acc = _acc(tmp_path)
    acc.init_account()
    acc.state["cash"] = 900000.0
    acc.save()
    acc2 = _acc(tmp_path)
    assert acc2.load() is True
    assert acc2.state["cash"] == 900000.0
    assert not (tmp_path / "paper.json.tmp").exists()   # 无残留临时文件


def test_summary_and_detail(tmp_path):
    acc = _acc(tmp_path)
    assert acc.summary() == {"exists": False}      # 未初始化
    acc.init_account(created="2026-09-01")
    s = acc.summary()
    assert s["exists"] is True
    assert s["cash"] == 1000000.0 and s["nav"] == 1000000.0
    assert s["total_return_pct"] == 0.0
    assert s["holdings_count"] == 0
    d = acc.detail()
    assert d["holdings"] == [] and d["trades"] == [] and d["nav_history"] == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_account.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt84`
Expected: FAIL（`prism.paper` 不存在 → ImportError/ModuleNotFoundError）

- [x] **Step 3: 实现 paper.py 账本部分**

```python
# -*- coding: utf-8 -*-
"""模拟实盘账户引擎(100万/first_board_v04) — 设计规格 2026-09-01-paper-trading。

安全边界(设计 §8): 本模块绝不写 D:\\QMT_SIGNALS、绝不调用下单接口;
账本 .paper_account.json 原子写, 保存成功才算交易发生; 现金恒>=0、持仓<=max_positions。
数据(行情/K线)只读。"""
import copy
import json
import os
from datetime import datetime
from pathlib import Path

from prism.engine import load_strategy

STATE_FILENAME = ".paper_account.json"
_DEFAULT_STRATEGY = Path(__file__).parent / "strategies" / "first_board_v04.json"
_REQUIRED_KEYS = ("version", "created", "initial_capital", "cash", "holdings",
                  "trades", "nav_history", "live_nav", "screens_done",
                  "settled_dates")


class PaperAccount:
    """模拟账户: 账本 + 买入/卖出执行 + 结算 + 查询。

    执行临界段纪律: 先在内存改, 校验不变量, 再 save(); save 失败 →
    _restore_state 回滚 —— 保存成功才视为交易发生(设计 §5)。"""

    def __init__(self, strategy_path=None, initial_capital=1000000.0,
                 position_ratio=0.3, max_positions=5, fee_rate=0.00025,
                 slippage=0.001, stamp_duty=0.0005, transfer_fee=0.00001,
                 state_path=None):
        self.strategy_path = Path(strategy_path) if strategy_path \
            else _DEFAULT_STRATEGY
        self.initial_capital = float(initial_capital)
        self.position_ratio = float(position_ratio)
        self.max_positions = int(max_positions)
        self.fee_rate = float(fee_rate)
        self.slippage = float(slippage)
        self.stamp_duty = float(stamp_duty)
        self.transfer_fee = float(transfer_fee)
        if state_path is not None:
            self.state_path = Path(state_path)
        else:
            self.state_path = Path(__file__).parent.parent / STATE_FILENAME
        self.state = None
        self._strategy = None

    # ---------- 策略(惰性加载, 与实盘同一份 JSON) ----------
    @property
    def strategy(self):
        if self._strategy is None:
            self._strategy = load_strategy(self.strategy_path)
        return self._strategy

    # ---------- 账本 ----------
    def init_account(self, created=None):
        """初始化账本(幂等: 已存在返回现有, 不覆盖)。"""
        if self.load():
            return self.state
        now = datetime.now()
        self.state = {
            "version": 1,
            "created": created or now.strftime("%Y-%m-%d"),
            "initial_capital": self.initial_capital,
            "cash": self.initial_capital,
            "holdings": [],
            "trades": [],
            "nav_history": [],
            "live_nav": self.initial_capital,
            "screens_done": [],
            "settled_dates": [],
        }
        self.save()
        return self.state

    def load(self):
        """读账本; 不存在/损坏/结构不符 → False(绝不覆盖原文件)。"""
        if not self.state_path.exists():
            return False
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict) or any(k not in raw
                                            for k in _REQUIRED_KEYS):
            return False
        if raw.get("version") != 1:
            return False
        self.state = raw
        return True

    def save(self):
        """原子写: 先写 .tmp 再 os.replace; 任何异常向上抛(调用方回滚)。"""
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---------- 执行临界段 ----------
    def _snapshot_state(self):
        return copy.deepcopy(self.state)

    def _restore_state(self, snap):
        self.state = snap

    # ---------- 查询 ----------
    def summary(self):
        if self.state is None and not self.load():
            return {"exists": False}
        st = self.state
        nav = float(st.get("live_nav") or st.get("initial_capital"))
        return {
            "exists": True,
            "created": st["created"],
            "cash": round(float(st["cash"]), 2),
            "nav": round(nav, 2),
            "total_return_pct": round((nav / st["initial_capital"] - 1) * 100, 2),
            "holdings_count": len(st["holdings"]),
            "updated_at": (st["trades"][-1]["ts"]
                           if st["trades"] else st["created"]),
        }

    def detail(self, trade_limit=50):
        if self.state is None and not self.load():
            return {"exists": False}
        st = self.state
        return {
            "exists": True,
            "holdings": st["holdings"],
            "trades": st["trades"][-trade_limit:][::-1],
            "nav_history": st["nav_history"],
        }
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_account.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt84`
Expected: 5 passed

- [x] **Step 5: Commit**

```bash
git add prism/paper.py prism/tests/test_paper_account.py
git commit -m "feat(prism): 模拟盘账本核心 — 原子保存/加载校验/查询"
```

---

### Task 2: 买入执行（选股消费 + 前置判定 + 一字板 + 手数记账）

**Files:**
- Modify: `prism/paper.py`（追加买入方法组）
- Test: `prism/tests/test_paper_buy.py`（新建）

**Interfaces:**
- Consumes: Task 1 `PaperAccount`（账本/临界段）；现有 `provider.build_market_context/get_limit_ups/build_stock_context/ds.get_kline`、`prism.engine.run_screen`、`prism.registry.get_factor`
- Produces:
  - `buy_from_screen(provider, now=None) -> dict`：`{"candidates", "bought":[{code,shares,price,amount,fee}], "skipped":[{code,reason}], "env_ok", "already_done"?, "error"?}`；时点幂等键 `YYYY-MM-DDTHH:MM` 存入 `screens_done`
  - `_nav_estimate(ticks=None) -> float`：现金 + Σ持仓（有 ticks 按实时价、否则按成本）
  - `_buyable(code, nav) -> str|None`：已持仓/今日已交易/仓位已满/现金不足
  - `_one_word_board(code, up_price, provider) -> bool`：当日 K 线 `low >= up_price - 0.01` → True（买不到）
  - `_execute_buy(code, up_price, now=None) -> dict|None`：手数取整、费用记账、临界段（save 失败回滚返回 None）

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""模拟盘买入执行测试 — mock provider/run_screen/K线, 全离线。"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.engine
import prism.paper as paper_mod
from prism.paper import PaperAccount


NOW = datetime(2026, 9, 1, 10, 0, 0)


class _FakeDS:
    def __init__(self, low=9.9):
        self.low = low

    def get_kline(self, code, days=1):
        import pandas as pd
        return pd.DataFrame({"open": [self.low], "high": [10.0],
                             "low": [self.low], "close": [10.0],
                             "volume": [1e6]})


class _FakeProvider:
    def __init__(self, ups=None, low=9.9):
        self.ups = ups or []
        self.ds = _FakeDS(low=low)

    def build_market_context(self):
        from prism.context import FactorContext
        return FactorContext(code="__MARKET__", limit_ups=self.ups)

    def get_limit_ups(self):
        return self.ups

    def build_stock_context(self, code, **kw):
        from prism.context import FactorContext
        return FactorContext(code=code)


def _fake_run_screen(cands, env_ok=True):
    def f(strategy, market_ctx, gate_factors=None, stock_contexts=None):
        return {"environment_ok": env_ok, "gate_score": 1,
                "candidates": cands,
                "summary": {"candidate_count": len(cands)}}
    return f


CAND = [{"code": "600000.SH", "up_stop_price": 10.0,
         "scores": {"composite": 5.0}}]


@pytest.fixture
def acc(tmp_path, monkeypatch):
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    return a


def test_buy_normal(acc, monkeypatch):
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert out["env_ok"] is True and len(out["bought"]) == 1
    b = out["bought"][0]
    # buy_price=10.0*1.001=10.01; target=30万; shares=floor(300000/10.01/100)*100=29900
    assert b["shares"] == 29900
    assert b["price"] == 10.01
    assert b["amount"] == round(29900 * 10.01, 2)
    assert b["fee"] == round(b["amount"] * 0.00026, 2)
    st = acc.state
    assert st["cash"] == round(1000000.0 - b["amount"] - b["fee"], 2)
    assert len(st["holdings"]) == 1 and st["holdings"][0]["code"] == "600000.SH"
    assert st["trades"][-1]["side"] == "buy"
    assert st["screens_done"] == ["2026-09-01T10:00"]


def test_buy_one_word_board_blocked(acc, monkeypatch):
    """一字板(low==涨停价, 从未开板) → 买不到。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND, low=10.0), now=NOW)
    assert out["bought"] == []
    assert any(s["reason"] == "一字板买不到" for s in out["skipped"])
    assert acc.state["cash"] == 1000000.0


def test_buy_opened_board_ok(acc, monkeypatch):
    """开过板(low<涨停价) → 可买。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND, low=9.9), now=NOW)
    assert len(out["bought"]) == 1


def test_buy_skip_when_held(acc, monkeypatch):
    acc.state["holdings"].append({"code": "600000.SH", "shares": 1000,
                                  "cost": 9.5, "buy_date": "2026-08-30",
                                  "buy_price": 9.5, "entry_nav": 1000000.0})
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert any(s["reason"] == "已持仓" for s in out["skipped"])
    assert out["bought"] == []


def test_buy_skip_max_positions(acc, monkeypatch):
    for i in range(5):
        acc.state["holdings"].append({
            "code": "00000%d.SZ" % i, "shares": 1000, "cost": 9.5,
            "buy_date": "2026-08-30", "buy_price": 9.5, "entry_nav": 1e6})
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert any(s["reason"] == "仓位已满" for s in out["skipped"])
    assert out["bought"] == []


def test_buy_env_not_ok(acc, monkeypatch):
    monkeypatch.setattr(prism.engine, "run_screen",
                        _fake_run_screen(CAND, env_ok=False))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert out["env_ok"] is False and out["bought"] == []
    assert acc.state["screens_done"] == ["2026-09-01T10:00"]  # 时点照记


def test_buy_idempotent_same_slot(acc, monkeypatch):
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert out.get("already_done") is True
    assert len(acc.state["trades"]) == 1        # 不重复买


def test_buy_rollback_on_save_failure(acc, monkeypatch):
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))

    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(acc, "save", boom)
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert out.get("bought") == []              # 执行回滚
    assert acc.state["cash"] == 1000000.0       # 内存回滚
    assert acc.state["holdings"] == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_buy.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt85`
Expected: FAIL（`buy_from_screen` 属性不存在 → AttributeError）

- [x] **Step 3: 实现买入方法组（追加到 prism/paper.py 类内）**

```python
    # ---------- 净值估算 ----------
    def _nav_estimate(self, ticks=None):
        """现金 + Σ持仓市值(ticks 有则按实时价, 无则按成本)。"""
        st = self.state
        if ticks:
            nav = st["cash"] + sum(
                h["shares"] * float((ticks.get(h["code"]) or {}).get(
                    "lastPrice") or h["cost"])
                for h in st["holdings"])
        else:
            nav = st["cash"] + sum(h["shares"] * h["cost"]
                                   for h in st["holdings"])
        return max(float(nav), 0.0)

    # ---------- 买入 ----------
    def buy_from_screen(self, provider, now=None):
        """时点选股(first_board_v04, 同一引擎) + 买入执行。

        幂等: 时点键 YYYY-MM-DDTHH:MM 已在 screens_done → already_done。
        选股失败/保存失败 fail-closed(不记账), 报告 error。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        d = now.strftime("%Y-%m-%d")
        ts_key = "%sT%s" % (d, now.strftime("%H:%M"))
        if ts_key in self.state["screens_done"]:
            return {"candidates": 0, "bought": [], "skipped": [],
                    "env_ok": False, "already_done": True}
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
        try:
            result = run_screen(self.strategy, market_ctx,
                                gate_factors=gate_factors,
                                stock_contexts=stock_contexts)
        except Exception as e:
            snap = self._snapshot_state()
            self.state["screens_done"].append(ts_key)
            try:
                self.save()
            except Exception:
                self._restore_state(snap)
            return {"error": "选股失败: %r" % e}
        env_ok = bool(result.get("environment_ok"))
        bought, skipped = [], []
        if env_ok:
            nav = self._nav_estimate()
            for c in result.get("candidates", []):
                code = c.get("code")
                up = float(c.get("up_stop_price") or 0)
                if not code or up <= 0:
                    skipped.append({"code": code, "reason": "缺涨停价"})
                    continue
                reason = self._buyable(code, nav)
                if reason:
                    skipped.append({"code": code, "reason": reason})
                    continue
                if self._one_word_board(code, up, provider):
                    skipped.append({"code": code, "reason": "一字板买不到"})
                    continue
                done = self._execute_buy(code, up, now=now)
                if done:
                    bought.append(done)
                    nav = self._nav_estimate()   # 买入后更新可用净值
                else:
                    skipped.append({"code": code, "reason": "执行失败"})
        snap = self._snapshot_state()
        self.state["screens_done"].append(ts_key)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "状态保存失败"}
        return {"candidates": len(result.get("candidates", [])),
                "bought": bought, "skipped": skipped, "env_ok": env_ok}

    def _buyable(self, code, nav):
        """买入前置判定; None=可买。"""
        st = self.state
        today = datetime.now().strftime("%Y-%m-%d")
        if any(h["code"] == code for h in st["holdings"]):
            return "已持仓"
        if any(t.get("side") == "buy" and t.get("code") == code
               and t.get("date") == today for t in st["trades"]):
            return "今日已交易"
        if len(st["holdings"]) >= self.max_positions:
            return "仓位已满"
        if st["cash"] < nav * self.position_ratio:
            return "现金不足"
        return None

    def _one_word_board(self, code, up_price, provider):
        """一字板判定: 当日K线 low >= up_price-0.01(全天未开板) → True。"""
        try:
            df = provider.ds.get_kline(code, days=1)
        except Exception:
            return True              # 拿不到K线 → 保守视为买不到
        if df is None or len(df) == 0 or "low" not in getattr(df, "columns", []):
            return True
        low = float(df["low"].iloc[-1])
        return low >= up_price - 0.01

    def _execute_buy(self, code, up_price, now=None):
        """买入执行(临界段: 内存改→不变量校验→save, 失败回滚)。"""
        now = now or datetime.now()
        snap = self._snapshot_state()
        nav = self._nav_estimate()
        target = nav * self.position_ratio
        buy_price = round(up_price * (1 + self.slippage), 4)
        shares = int(target / buy_price / 100) * 100
        if shares <= 0:
            return None
        amount = round(shares * buy_price, 2)
        fee = round(amount * (self.fee_rate + self.transfer_fee), 2)
        cash_after = round(self.state["cash"] - amount - fee, 2)
        if cash_after < 0:
            return None
        d = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["cash"] = cash_after
        self.state["holdings"].append({
            "code": code, "shares": shares, "cost": buy_price,
            "buy_date": d, "buy_price": buy_price, "entry_nav": nav})
        self.state["trades"].append({
            "ts": ts, "date": d, "side": "buy", "code": code,
            "price": buy_price, "shares": shares, "amount": amount,
            "fee": fee, "reason": "screen", "cash_after": cash_after})
        self.state["live_nav"] = round(self._nav_estimate(), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return {"code": code, "shares": shares, "price": buy_price,
                "amount": amount, "fee": fee}
```

（配套：模块级小函数 `_today_str()` = `datetime.now().strftime("%Y-%m-%d")`；上面的 `_buyable` 先导占位写法在落盘时合并为一个函数——实现以一个 `_buyable(self, code, nav)` 为准，逻辑 = `__buyable_impl` 的内容，删除占位行。）

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_buy.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt85`
Expected: 8 passed

- [x] **Step 5: Commit**

```bash
git add prism/paper.py prism/tests/test_paper_buy.py
git commit -m "feat(prism): 模拟盘买入执行 — 时点选股/一字板剔除/手数记账/临界段回滚"
```

---

### Task 3: 卖出执行（tick 监控止盈止损 + T+1 + 临界段）

**Files:**
- Modify: `prism/paper.py`（追加卖出方法组）
- Test: `prism/tests/test_paper_sell.py`（新建）

**Interfaces:**
- Consumes: Task 1 账本/临界段、Task 2 `_nav_estimate`；策略 `sell_rules`（take_profit_pct=8/stop_loss_pct=5，缺省 8%/5%）
- Produces:
  - `sell_check(ticks, now=None) -> list`：逐持仓用实时 `lastPrice` 判定（**成本=含滑点 `cost`**；`price ≥ cost×1.08` → `take_profit`；`price ≤ cost×0.95` → `stop_loss`；**T+1：`buy_date >= 今日` 的持仓跳过**）；每轮结束同步 `live_nav`（按 ticks 盯市）并保存；返回成交列表
  - `_execute_sell(code, price, reason, now=None) -> dict|None`：`sell_price = price×(1-slippage)`；卖出费用 `amount×(fee_rate+stamp_duty+transfer_fee)`；临界段（save 失败回滚返回 None）

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""模拟盘卖出执行测试 — tick 止盈止损/T+1/回滚, 全离线。"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism.paper import PaperAccount


NOW = datetime(2026, 9, 2, 10, 0, 0)      # 9-2(持仓 9-1 买入, 已过 T+1)


@pytest.fixture
def acc(tmp_path):
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    a.state["cash"] = 600000.0
    a.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1000000.0})
    return a


def test_take_profit(acc):
    """price 10.5 >= cost 9.5×1.08=10.26 → 止盈卖出。"""
    out = acc.sell_check({"600000.SH": {"lastPrice": 10.5}}, now=NOW)
    assert len(out) == 1 and out[0]["reason"] == "take_profit"
    sp = out[0]["price"]                    # 10.5×0.999=10.4895
    assert sp == round(10.5 * 0.999, 4)
    amount = round(1000 * sp, 2)
    fee = round(amount * 0.00076, 2)
    assert out[0]["fee"] == fee
    assert acc.state["cash"] == round(600000.0 + amount - fee, 2)
    assert acc.state["holdings"] == []
    assert acc.state["trades"][-1]["side"] == "sell"
    # live_nav 同步(无持仓 → 纯现金)
    assert acc.state["live_nav"] == acc.state["cash"]


def test_stop_loss(acc):
    out = acc.sell_check({"600000.SH": {"lastPrice": 9.0}}, now=NOW)
    # 9.0 <= 9.5×0.95=9.025 → 止损
    assert out[0]["reason"] == "stop_loss"
    assert acc.state["holdings"] == []


def test_no_trigger_keeps_holding(acc):
    out = acc.sell_check({"600000.SH": {"lastPrice": 9.8}}, now=NOW)
    assert out == []
    assert len(acc.state["holdings"]) == 1


def test_t1_same_day_no_sell(tmp_path):
    """当日买入(9-2)当日不卖(T+1)。"""
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    a.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-02", "buy_price": 9.5, "entry_nav": 1e6})
    out = a.sell_check({"600000.SH": {"lastPrice": 11.0}}, now=NOW)
    assert out == [] and len(a.state["holdings"]) == 1


def test_missing_tick_or_zero_price(acc):
    out = acc.sell_check({}, now=NOW)           # 无该股 tick
    assert out == [] and len(acc.state["holdings"]) == 1
    out = acc.sell_check({"600000.SH": {"lastPrice": 0}}, now=NOW)
    assert out == [] and len(acc.state["holdings"]) == 1


def test_sell_rollback_on_save_failure(acc, monkeypatch):
    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(acc, "save", boom)
    out = acc.sell_check({"600000.SH": {"lastPrice": 10.5}}, now=NOW)
    assert out == []
    assert len(acc.state["holdings"]) == 1      # 回滚
    assert acc.state["cash"] == 600000.0


def test_live_nav_updated(acc):
    acc.sell_check({"600000.SH": {"lastPrice": 9.8}}, now=NOW)
    # 1000×9.8=9800 持仓市值 + 600000 现金
    assert acc.state["live_nav"] == round(600000.0 + 9800.0, 2)
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_sell.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt86`
Expected: FAIL（`sell_check` 属性不存在）

- [x] **Step 3: 实现卖出方法组（追加到类内）**

```python
    # ---------- 卖出 ----------
    def sell_check(self, ticks, now=None):
        """tick 监控: 止盈/止损触发卖出; T+1(当日买入不卖); 同步 live_nav。

        ticks: {code: tick_dict}; 缺该股 tick 或价格<=0 → 跳过。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return []
        d = now.strftime("%Y-%m-%d")
        rules = self.strategy.get("sell_rules") or {}
        tp = float(rules.get("take_profit_pct") or 8) / 100.0
        sl = float(rules.get("stop_loss_pct") or 5) / 100.0
        out = []
        for h in list(self.state["holdings"]):
            if h["buy_date"] >= d:          # T+1: 当日买入不卖
                continue
            t = (ticks or {}).get(h["code"]) or {}
            price = float(t.get("lastPrice") or 0)
            if price <= 0:
                continue
            cost = float(h["cost"])
            if price >= cost * (1 + tp):
                reason = "take_profit"
            elif price <= cost * (1 - sl):
                reason = "stop_loss"
            else:
                continue
            done = self._execute_sell(h["code"], price, reason, now=now)
            if done:
                out.append(done)
        snap = self._snapshot_state()
        self.state["live_nav"] = round(self._nav_estimate(ticks), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
        return out

    def _execute_sell(self, code, price, reason, now=None):
        """卖出执行(临界段): 成交价=price×(1-slippage), 印花税仅卖出侧。"""
        now = now or datetime.now()
        snap = self._snapshot_state()
        idx = next((i for i, h in enumerate(self.state["holdings"])
                    if h["code"] == code), None)
        if idx is None:
            return None
        h = self.state["holdings"][idx]
        sell_price = round(price * (1 - self.slippage), 4)
        amount = round(h["shares"] * sell_price, 2)
        fee = round(amount * (self.fee_rate + self.stamp_duty
                              + self.transfer_fee), 2)
        cash_after = round(self.state["cash"] + amount - fee, 2)
        d = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["cash"] = cash_after
        self.state["holdings"].pop(idx)
        self.state["trades"].append({
            "ts": ts, "date": d, "side": "sell", "code": code,
            "price": sell_price, "shares": h["shares"], "amount": amount,
            "fee": fee, "reason": reason, "cash_after": cash_after})
        self.state["live_nav"] = round(self._nav_estimate(), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return {"code": code, "price": sell_price, "shares": h["shares"],
                "amount": amount, "fee": fee, "reason": reason}
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_sell.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt86`
Expected: 7 passed

- [x] **Step 5: Commit**

```bash
git add prism/paper.py prism/tests/test_paper_sell.py
git commit -m "feat(prism): 模拟盘卖出执行 — tick止盈止损/T+1/临界段回滚"
```

---

### Task 4: 盘后结算与净值（到期卖出/净值定格/缺口补算）

**Files:**
- Modify: `prism/paper.py`（追加结算方法组）
- Test: `prism/tests/test_paper_settle.py`（新建）

**Interfaces:**
- Consumes: Task 1 账本、Task 3 `_execute_sell`、Task 2 `_nav_estimate`
- Produces:
  - `settle_day(close_fn, due_fn=None, provider=None, now=None) -> dict`：`close_fn(code, day=None) -> float|None`（收盘价）；`due_fn(code, buy_date) -> bool`（到期判定，缺省用 `_due_by_kline`）；到期持仓按收盘价卖出（reason=`hold_expire`）；净值=现金+Σ持仓×收盘价盯市；`nav_history` 追加 + `settled_dates` 幂等
  - `_due_by_kline(code, buy_date, provider) -> bool`：K线日期（`_n8` 归一化去横线比较）中 `> buy_date` 的 bar 数 ≥ 5 → True
  - `backfill_nav(close_fn, trade_days) -> int`：`nav_history` 末日后、≤今日的交易日逐日按收盘价补算净值（当日同时补 settled_dates）；返回补算天数
  - `summary()` 增强：追加 `nav_points = len(nav_history)`

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""模拟盘结算与净值测试 — 到期卖出/定格/幂等/缺口补算, 全离线。"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism.paper import PaperAccount


NOW = datetime(2026, 9, 8, 15, 5, 0)


@pytest.fixture
def acc(tmp_path):
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    a.state["cash"] = 600000.0
    a.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1e6})
    return a


def test_settle_expire_sell(acc):
    """持有满5交易日 → 收盘价卖出 + 净值定格。"""
    out = acc.settle_day(lambda code, day=None: 10.2,
                         due_fn=lambda c, bd: True, now=NOW)
    assert len(out["closed"]) == 1
    assert out["closed"][0]["reason"] == "hold_expire"
    sp = out["closed"][0]["price"]              # 10.2×0.999=10.1898
    assert sp == round(10.2 * 0.999, 4)
    assert acc.state["holdings"] == []
    # 无持仓 → nav=现金
    assert out["nav"] == acc.state["cash"]
    assert acc.state["nav_history"][-1]["date"] == "2026-09-08"
    assert acc.state["settled_dates"] == ["2026-09-08"]


def test_settle_mark_to_market(acc):
    """未到期持仓按收盘价盯市定格净值。"""
    out = acc.settle_day(lambda code, day=None: 10.0,
                         due_fn=lambda c, bd: False, now=NOW)
    assert out["closed"] == []
    assert len(acc.state["holdings"]) == 1
    assert out["nav"] == round(600000.0 + 1000 * 10.0, 2)
    assert acc.state["nav_history"][0]["nav"] == 610000.0


def test_settle_idempotent(acc):
    acc.settle_day(lambda c, d=None: 10.0, due_fn=lambda c, b: False, now=NOW)
    out = acc.settle_day(lambda c, d=None: 10.0, due_fn=lambda c, b: True,
                         now=NOW)
    assert out.get("already_done") is True
    assert len(acc.state["holdings"]) == 1      # 幂等: 不再卖


def test_settle_no_price_keeps_holding(acc):
    out = acc.settle_day(lambda c, d=None: None, due_fn=lambda c, b: True,
                         now=NOW)
    assert out["closed"] == [] and len(acc.state["holdings"]) == 1


def test_due_by_kline(acc, monkeypatch):
    """_due_by_kline: buy_date 后 K线 bar 数 >=5 → 到期。"""
    import pandas as pd
    class _DS:
        def get_kline(self, code, days=15):
            return pd.DataFrame(
                {"close": [1.0] * 7},
                index=["20260901", "20260902", "20260903", "20260904",
                       "20260907", "20260908", "20260909"])
    class _P:
        ds = _DS()
    assert acc._due_by_kline("600000.SH", "2026-09-01", _P()) is True
    assert acc._due_by_kline("600000.SH", "2026-09-03", _P()) is False


def test_backfill_nav(acc):
    """缺口日补算: nav_history 末日后交易日逐日盯市。"""
    acc.state["nav_history"] = [{"date": "2026-09-01", "nav": 1000000.0}]
    days = ["2026-09-02", "2026-09-03", "2026-09-08", "2026-09-09"]
    n = acc.backfill_nav(
        lambda code, day=None: {"2026-09-02": 9.6, "2026-09-03": 9.4,
                                "2026-09-08": 10.0}.get(day), days)
    assert n == 3                                # 9-9 在未来 → 不补
    hist = acc.state["nav_history"]
    assert hist[-1]["date"] == "2026-09-08"
    assert hist[-1]["nav"] == round(600000.0 + 1000 * 10.0, 2)
    assert [h["nav"] for h in hist][1:] == [609600.0, 609400.0, 610000.0]
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_settle.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt87`
Expected: FAIL（`settle_day` 属性不存在）

- [x] **Step 3: 实现结算方法组（追加到类内）**

```python
    # ---------- 盘后结算 ----------
    @staticmethod
    def _n8(s):
        return str(s).replace("-", "")

    def _due_by_kline(self, code, buy_date, provider):
        """到期判定(缺省): 持仓股K线中 buy_date 之后的交易日数 >= max_hold_days。"""
        try:
            df = provider.ds.get_kline(code, days=15)
        except Exception:
            return False
        if df is None or len(df) == 0:
            return False
        b = self._n8(buy_date)
        after = [ix for ix in df.index if self._n8(ix) > b]
        return len(after) >= self.max_hold_days()

    def max_hold_days(self):
        rules = self.strategy.get("sell_rules") or {}
        return int(float(rules.get("max_hold_days") or 5))

    def settle_day(self, close_fn, due_fn=None, provider=None, now=None):
        """盘后结算: 到期持仓按收盘价卖出 + 当日净值盯市定格 + 幂等。

        close_fn(code, day=None) -> float|None; due_fn(code, buy_date) -> bool
        (缺省 _due_by_kline); 拿不到收盘价的到期持仓保留(下个交易日再结)。"""
        now = now or datetime.now()
        d = now.strftime("%Y-%m-%d")
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        if d in self.state["settled_dates"]:
            return {"already_done": True}
        closed = []
        for h in list(self.state["holdings"]):
            due = due_fn(h["code"], h["buy_date"]) if due_fn \
                else self._due_by_kline(h["code"], h["buy_date"], provider)
            if not due:
                continue
            px = close_fn(h["code"], d)
            if not px or px <= 0:
                continue
            done = self._execute_sell(h["code"], px, "hold_expire", now=now)
            if done:
                closed.append(done)
        snap = self._snapshot_state()
        nav = self.state["cash"] + sum(
            h["shares"] * float(close_fn(h["code"], d) or h["cost"])
            for h in self.state["holdings"])
        self.state["live_nav"] = round(nav, 2)
        self.state["nav_history"].append({"date": d, "nav": round(nav, 2)})
        self.state["settled_dates"].append(d)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "状态保存失败"}
        return {"closed": closed, "nav": round(nav, 2)}

    def backfill_nav(self, close_fn, trade_days):
        """缺口日补算: nav_history 末日后、<=今日的交易日逐日盯市。

        close_fn(code, day) -> float|None(该日该股收盘价); 未来日跳过;
        今日补算时同时记入 settled_dates(幂等防重复结算)。"""
        if self.state is None and not self.load():
            return 0
        last = (self.state["nav_history"][-1]["date"]
                if self.state["nav_history"] else None)
        today = datetime.now().strftime("%Y-%m-%d")
        snap = self._snapshot_state()
        filled = 0
        try:
            for d in trade_days:
                if last and d <= last:
                    continue
                if d > today:
                    continue
                nav = self.state["cash"] + sum(
                    h["shares"] * float(close_fn(h["code"], d) or h["cost"])
                    for h in self.state["holdings"])
                self.state["nav_history"].append(
                    {"date": d, "nav": round(nav, 2)})
                if d == today and d not in self.state["settled_dates"]:
                    self.state["settled_dates"].append(d)
                filled += 1
            if filled:
                self.save()
        except Exception:
            self._restore_state(snap)
            return 0
        return filled
```

（`summary()` 追加一行字段：`"nav_points": len(st["nav_history"])`。）

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_settle.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt87`
Expected: 6 passed

- [x] **Step 5: Commit**

```bash
git add prism/paper.py prism/tests/test_paper_settle.py
git commit -m "feat(prism): 模拟盘结算 — 到期卖出/净值盯市定格/缺口补算"
```

---

### Task 5: 守护调度（时段判定/单轮 tick/时点选股/收盘结算/续跑）

**Files:**
- Create: `prism/paper_daemon.py`
- Test: `prism/tests/test_paper_daemon.py`（新建）

**Interfaces:**
- Consumes: Task 1-4 全部 `PaperAccount` 方法；`prism.data.DataProvider`（`connect/invalidate/get_limit_ups/build_*`）
- Produces:
  - 常量 `SCREEN_TIMES = ("10:00", "13:30", "14:30")`、`SETTLE_AFTER = "15:00"`、`POLL_SECONDS = 5`
  - `PaperDaemon(account, ticks_fn=None, sleep_fn=None, now_fn=None)`：`ticks_fn` 缺省= `provider.ds.get_full_market_ticks`；`sleep_fn/now_fn` 测试注入
  - `in_session(now) -> bool`：周一~五 ∧（09:30-11:30 ∨ 13:00-15:00）
  - `close_fn(code, day=None) -> float|None`：K线收盘价（`_n8` 归一化取 ≤day 最后一根）
  - `due_fn(code, buy_date) -> bool`：透传 `account._due_by_kline`
  - `tick_once(now=None) -> dict`：`{"action", "sells", "buys", "settle"}`——15:00 后未结算→结算；盘中→tick 检查卖出+到点选股（**选股前 `provider.invalidate()` 刷新涨停池缓存**）；行情异常→本轮跳过；账本未初始化→idle
  - `backfill() -> int`：缺口补算（交易日序列取上证指数 `000001.SH` K线日期，无持仓时；有持仓用持仓股）
  - `connect_provider(max_retry=60, retry_wait=10) -> bool`
  - `run_forever()`：load/缺失→init → backfill → connect → 循环 tick_once + sleep + 异常兜底记日志
  - `main()`：`python -m prism.paper_daemon` 入口

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""模拟盘守护调度测试 — mock 时钟/行情/选股, 全离线。"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.engine
import prism.paper_daemon as dm
from prism.paper import PaperAccount
from prism.paper_daemon import PaperDaemon


CAND = [{"code": "600000.SH", "up_stop_price": 10.0,
         "scores": {"composite": 5.0}}]


class _FakeProvider:
    def __init__(self):
        from prism.context import FactorContext
        self._ctx = FactorContext

    def build_market_context(self):
        return self._ctx(code="__MARKET__", limit_ups=CAND)

    def get_limit_ups(self):
        return CAND

    def build_stock_context(self, code, **kw):
        return self._ctx(code=code)

    def invalidate(self):
        pass

    class ds:
        @staticmethod
        def get_full_market_ticks():
            return {"600000.SH": {"lastPrice": 9.8}}

        @staticmethod
        def get_kline(code, days=1):
            import pandas as pd
            return pd.DataFrame({"close": [9.8], "low": [9.7],
                                 "high": [10.0]}, index=["20260902"])


def _daemon(tmp_path, monkeypatch, now_fn=None, ticks=None):
    # 默认 mock run_screen: env_ok=False(不买入) — 需买入行为的测试自行覆盖
    monkeypatch.setattr(
        prism.engine, "run_screen",
        lambda s, m, gate_factors=None, stock_contexts=None:
        {"environment_ok": False, "gate_score": 0, "candidates": [],
         "summary": {"candidate_count": 0}})
    acc = PaperAccount(state_path=tmp_path / "paper.json")
    acc.init_account(created="2026-09-01")
    d = PaperDaemon(acc, ticks_fn=(lambda: ticks) if ticks else None,
                    now_fn=now_fn)
    d.provider = _FakeProvider()
    return d, acc


def test_in_session():
    d = PaperDaemon(PaperAccount(state_path="x.json"))
    assert d.in_session(datetime(2026, 9, 2, 10, 0)) is True      # 周三盘中
    assert d.in_session(datetime(2026, 9, 2, 12, 0)) is False     # 午休
    assert d.in_session(datetime(2026, 9, 2, 15, 1)) is False     # 收盘后
    assert d.in_session(datetime(2026, 9, 5, 10, 0)) is False     # 周六
    assert d.in_session(datetime(2026, 9, 2, 9, 25)) is False     # 未开盘


def test_tick_idle_outside_session(tmp_path, monkeypatch):
    d, _ = _daemon(tmp_path, monkeypatch)
    out = d.tick_once(now=datetime(2026, 9, 2, 12, 0))
    assert out["action"] == "idle" and out["sells"] == []


def test_tick_sells_in_session(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1e6})
    out = d.tick_once(now=datetime(2026, 9, 2, 10, 0, 5))
    # tick 价 9.8 在 9.025~10.26 之间 → 不触发卖出
    assert out["action"] == "tick" and out["sells"] == []
    # live_nav 按 tick 盯市: 100万现金 + 1000×9.8
    assert acc.state["live_nav"] == 1009800.0


def test_tick_screen_at_timepoint(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    calls = []

    def fake_run_screen(strategy, market_ctx, gate_factors=None,
                        stock_contexts=None):
        calls.append(1)
        return {"environment_ok": True, "gate_score": 1, "candidates": CAND,
                "summary": {"candidate_count": 1}}
    monkeypatch.setattr(prism.engine, "run_screen", fake_run_screen)
    out = d.tick_once(now=datetime(2026, 9, 2, 10, 0, 5))
    assert len(calls) == 1                        # 10:00 时点触发选股
    assert acc.state["screens_done"] == ["2026-09-02T10:00"]
    out2 = d.tick_once(now=datetime(2026, 9, 2, 10, 5))
    assert len(calls) == 1                        # 幂等: 同时点不重复
    assert out2["buys"] == []


def test_tick_settle_after_close(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1e6})
    out = d.tick_once(now=datetime(2026, 9, 8, 15, 5))
    assert out["action"] == "settle"
    # _FakeDS 只有当日一根 bar → buy_date 后 0 根 → 未到期 → 不卖, 但净值定格
    assert acc.state["settled_dates"] == ["2026-09-08"]
    assert out["settle"]["closed"] == []
    # nav = 现金100万 + 持仓 1000×9.8(K线收盘价盯市)
    assert out["settle"]["nav"] == 1009800.0
    # 幂等: 二次不重复结算
    out2 = d.tick_once(now=datetime(2026, 9, 8, 15, 10))
    assert out2["settle"] is None
```

（说明：`test_tick_sells_in_session` 的第三行断言写成宽松形式——本测试核心是"时段内 action=tick、卖出未触发"，live_nav 语义由 Task 3 测试锁定；实现时若该行断言冗余可删，但 `action=="tick"` 与 `sells==[]` 必须保留。）

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_daemon.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt88`
Expected: FAIL（`prism.paper_daemon` 不存在）

- [x] **Step 3: 实现 paper_daemon.py（完整文件）**

```python
# -*- coding: utf-8 -*-
"""模拟盘守护进程: 盘中实时盯盘 + 三时点选股 + 收盘结算 + 缺口补算。

用法: python -m prism.paper_daemon
安全(设计 §8): 本进程绝不写 D:\\QMT_SIGNALS、不调用任何下单接口——纯记账;
实盘进程零影响; 行情/K线只读; 单轮异常不影响下一轮。"""
import logging
import time
from datetime import datetime

from prism.paper import PaperAccount

SCREEN_TIMES = ("10:00", "13:30", "14:30")
SETTLE_AFTER = "15:00"
POLL_SECONDS = 5
INDEX_CODE = "000001.SH"          # 上证指数: 交易日历来源


class PaperDaemon:
    """调度器: 每 POLL_SECONDS 一轮; 时点选股/收盘结算由 tick_once 触发。"""

    def __init__(self, account, ticks_fn=None, sleep_fn=None, now_fn=None):
        self.account = account
        self.provider = None
        self.ticks_fn = ticks_fn
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn

    # ---------- 时段 ----------
    def in_session(self, now):
        if now.weekday() >= 5:
            return False
        hm = now.strftime("%H:%M")
        return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")

    # ---------- 数据函数(结算/补算用, 全部只读) ----------
    def close_fn(self, code, day=None):
        """该股收盘价: K线中 <=day 的最后一根(无 day 取最新)。"""
        if self.provider is None:
            return None
        try:
            df = self.provider.ds.get_kline(code, days=5)
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        target = self.account._n8(day) if day else None
        pick = [(self.account._n8(ix), float(r["close"]))
                for ix, r in df.iterrows()]
        rows = [c for k, c in pick if not target or k <= target]
        return rows[-1] if rows and rows[-1] > 0 else None

    def due_fn(self, code, buy_date):
        return self.account._due_by_kline(code, buy_date, self.provider)

    # ---------- 单轮 ----------
    def tick_once(self, now=None):
        now = now or (self.now_fn() if self.now_fn else datetime.now())
        out = {"action": "idle", "sells": [], "buys": [], "settle": None}
        if self.account.state is None and not self.account.load():
            return out
        d = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        if hm >= SETTLE_AFTER:
            st = self.account.state
            if d not in st.get("settled_dates", []) and self.provider:
                out["settle"] = self.account.settle_day(
                    self.close_fn, due_fn=self.due_fn,
                    provider=self.provider, now=now)
                out["action"] = "settle"
            return out
        if not self.in_session(now) or self.provider is None:
            return out
        try:
            ticks = (self.ticks_fn or
                     (lambda: self.provider.ds.get_full_market_ticks()))()
        except Exception:
            return out              # 行情失败 → 本轮跳过, 不记账
        out["action"] = "tick"
        out["sells"] = self.account.sell_check(ticks, now=now)
        for st_time in SCREEN_TIMES:
            if hm >= st_time and "%sT%s" % (d, st_time) \
                    not in self.account.state["screens_done"]:
                try:
                    self.provider.invalidate()   # 刷新涨停池缓存
                except Exception:
                    pass
                out["buys"].append(
                    self.account.buy_from_screen(self.provider, now=now))
        return out

    # ---------- 缺口补算 ----------
    def _trade_days(self):
        if self.provider is None:
            return []
        code = (self.account.state["holdings"][0]["code"]
                if self.account.state and self.account.state["holdings"]
                else INDEX_CODE)
        try:
            df = self.provider.ds.get_kline(code, days=30)
        except Exception:
            return []
        if df is None or len(df) == 0:
            return []
        out = []
        for ix in df.index:
            s = self.account._n8(ix)
            if len(s) == 8 and s.isdigit():
                out.append("%s-%s-%s" % (s[:4], s[4:6], s[6:8]))
        return out

    def backfill(self):
        """重启后缺口日补算(净值曲线无洞)。"""
        days = self._trade_days()
        if not days:
            return 0
        return self.account.backfill_nav(self.close_fn, days)

    # ---------- 连接与主循环 ----------
    def _sleep(self, sec):
        if self.sleep_fn:
            self.sleep_fn(sec)
        else:
            time.sleep(sec)

    def connect_provider(self, max_retry=60, retry_wait=10):
        from prism.data import DataProvider
        for _ in range(max_retry):
            try:
                p = DataProvider()
                p.connect()
                if p.connected:
                    self.provider = p
                    return True
            except Exception:
                pass
            self._sleep(retry_wait)
        return False

    def run_forever(self):
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        log = logging.getLogger("paper_daemon")
        if self.account.state is None and not self.account.load():
            log.info("账本不存在 → 初始化 100 万模拟账户")
            self.account.init_account()
        filled = self.backfill()
        if filled:
            log.info("缺口日补算 %d 天", filled)
        if not self.connect_provider():
            log.error("QMT 连接失败(重试上限), 退出")
            return
        log.info("模拟盘守护启动(策略=%s, 100万, 每%d秒一轮)",
                 self.account.strategy.get("id"), POLL_SECONDS)
        while True:
            try:
                out = self.tick_once()
                log.info("tick %s", {k: (len(v) if isinstance(v, list) else v)
                                     for k, v in out.items()})
            except Exception as e:
                log.exception("tick 异常(忽略, 下轮重试): %r", e)
            self._sleep(POLL_SECONDS)


def main():
    daemon = PaperDaemon(PaperAccount())
    daemon.run_forever()


if __name__ == "__main__":
    main()
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_daemon.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt88`
Expected: 5 passed

- [x] **Step 5: Commit**

```bash
git add prism/paper_daemon.py prism/tests/test_paper_daemon.py
git commit -m "feat(prism): 模拟盘守护调度 — 时段判定/时点选股/收盘结算/缺口补算"
```

---

### Task 6: CLI 入口 + gitignore（初始化/摘要/单轮/守护说明）

**Files:**
- Modify: `prism/paper.py`（文件尾追加 `main(argv=None, account=None)` + `if __name__ == "__main__"` 块）
- Modify: `.gitignore`（追加两行）
- Test: `prism/tests/test_paper_cli.py`（新建）

**Interfaces:**
- Consumes: Task 1-5 全部
- Produces:
  - `main(argv=None, account=None)`：子命令 `--init`（幂等初始化）/ `--summary`（打印摘要 JSON）/ `--once`（连 QMT 后跑一轮当前时点逻辑，复用 `PaperDaemon.tick_once`，连不上打印"QMT 连接失败"）
  - `python -m prism.paper --init|--summary|--once` 可用
  - `.gitignore` 含 `.paper_account.json` 与 `.paper_account.json.tmp`

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""模拟盘 CLI + gitignore 测试 — 全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.paper_daemon as dm
from prism.paper import PaperAccount, main


ROOT = Path(__file__).parent.parent.parent


def test_cli_init_idempotent(tmp_path, capsys):
    main(["--init"], account=PaperAccount(state_path=tmp_path / "p.json"))
    assert (tmp_path / "p.json").exists()
    main(["--init"], account=PaperAccount(state_path=tmp_path / "p.json"))
    out = capsys.readouterr().out
    assert "已初始化" in out


def test_cli_summary(tmp_path, capsys):
    main(["--init"], account=PaperAccount(state_path=tmp_path / "p.json"))
    main(["--summary"], account=PaperAccount(state_path=tmp_path / "p.json"))
    out = capsys.readouterr().out
    assert '"exists": true' in out


def test_cli_once_without_qmt(tmp_path, capsys, monkeypatch):
    def fail_connect(self, max_retry=10, retry_wait=5):
        return False
    monkeypatch.setattr(dm.PaperDaemon, "connect_provider", fail_connect)
    main(["--once"], account=PaperAccount(state_path=tmp_path / "p.json"))
    assert "QMT 连接失败" in capsys.readouterr().out


def test_gitignore_covers_paper_state():
    txt = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".paper_account.json" in txt
    assert ".paper_account.json.tmp" in txt
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_cli.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt89`
Expected: FAIL（`main` 不存在 / gitignore 未含条目）

- [x] **Step 3: 实现（paper.py 文件尾追加 + .gitignore 追加）**

paper.py 文件尾：

```python
def main(argv=None, account=None):
    """CLI: --init 初始化 / --summary 摘要 / --once 单轮(需QMT在线)。"""
    import argparse
    ap = argparse.ArgumentParser(description="模拟实盘账户(100万/first_board_v04)")
    ap.add_argument("--once", action="store_true",
                    help="连 QMT 跑一轮当前时点逻辑(选股/监控/结算)")
    ap.add_argument("--init", action="store_true", help="初始化账户(幂等)")
    ap.add_argument("--summary", action="store_true", help="打印账户摘要")
    a = ap.parse_args(argv)
    acc = account or PaperAccount()
    if a.init:
        acc.init_account()
        print("账户已初始化/存在: %s (初始资金 %.0f)" %
              (acc.state_path, acc.initial_capital))
        return
    if a.summary:
        import json as _j
        print(_j.dumps(acc.summary(), ensure_ascii=False, indent=1))
        return
    if a.once:
        from prism.paper_daemon import PaperDaemon
        d = PaperDaemon(acc)
        if not d.connect_provider(max_retry=10, retry_wait=5):
            print("QMT 连接失败(需盘中在线)")
            return
        import json as _j
        out = d.tick_once()
        print(_j.dumps(out, ensure_ascii=False, indent=1, default=str))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
```

.gitignore 在 "Runtime caches" 段后追加：

```
# 模拟盘账本(本地运行数据, 绝不提交)
.paper_account.json
.paper_account.json.tmp
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_paper_cli.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt89`
Expected: 4 passed

- [x] **Step 5: Commit**

```bash
git add prism/paper.py .gitignore prism/tests/test_paper_cli.py
git commit -m "feat(prism): 模拟盘 CLI(--init/--summary/--once) + 账本gitignore"
```

---

### Task 7: GUI 模拟盘面板（只读端点 + 前端）

**Files:**
- Modify: `prism_web/app.py`（backtest 端点后追加两个只读端点）
- Modify: `prism_web/templates/index.html`（nav 加 tab + main 加面板段）
- Modify: `prism_web/static/app.js`（追加模拟盘渲染 + 30 秒自动刷新）
- Test: `prism_web/tests/test_app.py`（追加 2 个测试）

**Interfaces:**
- Consumes: Task 1 `PaperAccount.summary()/detail()`（`summary` 含 `exists/created/cash/nav/total_return_pct/holdings_count/nav_points/updated_at`；`detail` 含 `holdings/trades/nav_history`）
- Produces:
  - `GET /api/paper/summary`、`GET /api/paper/detail`（只读；账本损坏/异常 → 200 + `{"exists": false, "error": ...}`，不 500）
  - 前端 tab "模拟盘"：总收益/净值/现金/持仓/逐笔/净值历史；30 秒自动刷新；未初始化显示提示

- [x] **Step 1: 写失败测试（追加到 test_app.py 末尾）**

```python
# ---------------- 模拟盘面板 ----------------

def test_paper_endpoints_not_initialized(client, tmp_path, monkeypatch):
    """未初始化账本 → exists=false, 200 不报错。"""
    import prism.paper as paper_mod

    class FakeAcc:
        def __init__(self, **kw):
            self.state_path = tmp_path / "p.json"
            self.state = None
            self._strategy = None
            self.initial_capital = 1000000.0
        def summary(self):
            return {"exists": False}
        def detail(self, trade_limit=50):
            return {"exists": False}

    monkeypatch.setattr(paper_mod, "PaperAccount", FakeAcc)
    r = client.get("/api/paper/summary")
    assert r.status_code == 200
    assert r.get_json()["exists"] is False
    r2 = client.get("/api/paper/detail")
    assert r2.status_code == 200 and r2.get_json()["exists"] is False


def test_paper_endpoints_with_ledger(client, tmp_path, monkeypatch):
    """有账本 → summary 字段契约 + detail 持仓/流水透出(子类注入 tmp 账本)。"""
    import prism.paper as paper_mod

    class RealTmpAcc(paper_mod.PaperAccount):
        def __init__(self, **kw):
            super().__init__(state_path=tmp_path / "p.json", **kw)
    monkeypatch.setattr(paper_mod, "PaperAccount", RealTmpAcc)
    acc = RealTmpAcc()
    acc.init_account(created="2026-09-01")
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 29900, "cost": 10.01,
        "buy_date": "2026-09-01", "buy_price": 10.01, "entry_nav": 1e6})
    acc.save()
    r = client.get("/api/paper/summary")
    s = r.get_json()
    assert s["exists"] is True and s["holdings_count"] == 1
    assert "total_return_pct" in s and "nav_points" in s
    d = client.get("/api/paper/detail").get_json()
    assert d["holdings"][0]["code"] == "600000.SH"
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt90`
Expected: FAIL（404 — 端点不存在）

- [x] **Step 3: 实现端点（app.py 在 `/api/backtest` 函数后追加）**

```python
# ---------------- 模拟盘(只读查询, 设计 §8-7: 无写端点) ----------------

@app.route("/api/paper/summary")
def api_paper_summary():
    from prism.paper import PaperAccount
    try:
        return jsonify(PaperAccount().summary())
    except Exception as e:
        return jsonify({"exists": False,
                        "error": "读取模拟盘账本失败: %r" % e})


@app.route("/api/paper/detail")
def api_paper_detail():
    from prism.paper import PaperAccount
    try:
        return jsonify(PaperAccount().detail())
    except Exception as e:
        return jsonify({"exists": False,
                        "error": "读取模拟盘账本失败: %r" % e})
```

- [x] **Step 4: 实现前端（index.html nav 区末尾 + main 区末尾；app.js 文件尾）**

index.html nav（`</nav>` 前）追加：

```html
    <button class="tab" data-tab="paper" onclick="switchTab('paper')">模拟盘</button>
```

index.html main（`</main>` 前）追加：

```html
    <section id="tab-paper" class="tab-panel">
      <h2>模拟盘</h2>
      <p class="hint">100 万虚拟资金 × 当前实盘策略, 守护进程盘中实时模拟交易
        (到价即卖/三时点选股买入)。逐笔与净值由本地账本
        <code>.paper_account.json</code> 记录, 程序重启自动续跑。</p>
      <div id="paper-summary" class="cards"></div>
      <div id="paper-layout">
        <div>
          <h3>当前持仓</h3>
          <table id="paper-holdings"><thead><tr>
            <th>代码</th><th>股数</th><th>成本</th><th>买入日</th>
          </tr></thead><tbody></tbody></table>
        </div>
        <div>
          <h3>逐笔流水</h3>
          <table id="paper-trades"><thead><tr>
            <th>时间</th><th>方向</th><th>代码</th><th>价格</th><th>股数</th><th>原因</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
      <h3>净值历史</h3>
      <table id="paper-nav"><thead><tr><th>日期</th><th>净值</th></tr></thead><tbody></tbody></table>
    </section>
```

app.js 文件尾追加：

```js
// ---------- 模拟盘面板 ----------
async function refreshPaper() {
  try {
    const sum = await api("/api/paper/summary");
    const det = sum.exists ? await api("/api/paper/detail") : null;
    renderPaper(sum, det);
  } catch (e) { /* 后端未启动时静默 */ }
}

function renderPaper(sum, det) {
  const box = document.getElementById("paper-summary");
  if (!box) return;
  if (!sum.exists) {
    box.innerHTML = "<div class='hint'>模拟盘未初始化 — 运行 " +
      "<code>python -m prism.paper --init</code> 后由守护进程接管</div>";
    return;
  }
  const ret = sum.total_return_pct;
  const cls = ret >= 0 ? "ok" : "fail";
  box.innerHTML =
    `<div class="card"><b>总收益</b> <span class="${cls}">${ret}%</span></div>` +
    `<div class="card"><b>当前净值</b> ${sum.nav.toLocaleString()}</div>` +
    `<div class="card"><b>现金</b> ${sum.cash.toLocaleString()}</div>` +
    `<div class="card"><b>持仓</b> ${sum.holdings_count}/5</div>` +
    `<div class="card"><b>记账天数</b> ${sum.nav_points ?? 0}</div>`;
  const hb = document.querySelector("#paper-holdings tbody");
  if (hb) hb.innerHTML = (det && det.holdings || []).map(h =>
    `<tr><td>${h.code}</td><td>${h.shares}</td><td>${h.cost}</td>` +
    `<td>${h.buy_date}</td></tr>`).join("") ||
    "<tr><td colspan=4 class='hint'>空仓等待信号</td></tr>";
  const tb = document.querySelector("#paper-trades tbody");
  if (tb) tb.innerHTML = (det && det.trades || []).map(t =>
    `<tr><td>${t.ts}</td><td>${t.side}</td><td>${t.code}</td>` +
    `<td>${t.price}</td><td>${t.shares}</td><td>${t.reason}</td></tr>`).join("")
    || "<tr><td colspan=6 class='hint'>暂无交易</td></tr>";
  const nb = document.querySelector("#paper-nav tbody");
  if (nb) nb.innerHTML = (det && det.nav_history || []).map(n =>
    `<tr><td>${n.date}</td><td>${n.nav.toLocaleString()}</td></tr>`).join("")
    || "<tr><td colspan=2 class='hint'>暂无净值记录</td></tr>";
}
refreshPaper();
setInterval(refreshPaper, 30000);
```

- [x] **Step 5: 跑测试确认通过 + 全 app 回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt90`
Expected: 全 passed（27 = 25 存量 + 2 新）

- [x] **Step 6: Commit**

```bash
git add prism_web/app.py prism_web/templates/index.html prism_web/static/app.js prism_web/tests/test_app.py
git commit -m "feat(prism_web): 模拟盘只读面板 — 净值/持仓/逐笔/净值历史"
```

---

### Task 8: 全量回归 + 真实冒烟验收 + 收尾

**Files:**
- Modify: `.superpowers/sdd/progress.md`（台账追加模拟盘记录）
- Modify: `docs/superpowers/plans/2026-09-01-paper-trading.md`（勾选全部步骤）

**Interfaces:**
- Consumes: Task 1-7 全部完成态
- Produces: 全量回归绿 + 真实账本初始化 + 真实数据 `--once` 冒烟结果 + 守护进程启动方式说明（交付用户）

- [x] **Step 1: 全量回归（离线部分必须全绿）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt91`
Expected: 全 passed 0 failed（存量 314 + 新增 ~24 = 约 338）

- [x] **Step 2: 因子体检回归（确认模拟盘未碰任何既有因子）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.factor_check`
Expected: 44/44 通过（与基线一致——模拟盘不注册因子、不改因子）

- [x] **Step 3: 真实冒烟 — 初始化真实账本（用户在场）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.paper --init`
Expected: 输出 `账户已初始化/存在: D:\cc-joesph\.paper_account.json (初始资金 1000000)`；确认 `git status` 中 `.paper_account.json` 被 ignore（不出现在未跟踪列表）

- [x] **Step 4: 真实冒烟 — 单轮真实数据（用户在场）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.paper --once`
Expected: QMT 连接成功；输出 JSON（盘中 → action=tick + 时点选股结果；收盘后 → action=settle + 净值定格）；**确认输出中不出现任何"信号文件/下单"字样，`D:\QMT_SIGNALS` 目录无新文件（安全边界验证）**

- [x] **Step 5: 守护进程启动验证（用户在场, Ctrl-C 可停）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.paper_daemon`
Expected: 日志"模拟盘守护启动(策略=first_board_v04, 100万, 每5秒一轮)"；若盘中 → 每 5 秒 tick 日志；Ctrl-C 停止后再次启动 → 日志无"初始化"（续跑既有账本）+ 缺口补算日志（若有缺口）

- [x] **Step 6: GUI 面板验证（用户在场）**

打开 Web GUI → "模拟盘" tab：显示总收益/净值/现金/持仓/逐笔/净值历史；未初始化路径显示提示（若 Step 3 已 init 则显示真实数据）

- [x] **Step 7: 台账与计划勾选 + Commit**

- `.superpowers/sdd/progress.md` 追加：模拟盘 8 任务完成记录 + 冒烟结果
- 勾选本计划所有 `- [ ]` → `- [x]`
- Commit:

```bash
git add docs/superpowers/plans/2026-09-01-paper-trading.md .superpowers/sdd/progress.md
git commit -m "docs(plan): 模拟实盘账户实施完成(8任务/全量回归/真实冒烟)"
```

- [x] **Step 8: 向用户交付**

报告要点（通俗中文）：功能是什么、怎么每天用（开机自启可选/手动启动命令）、网页哪里看、已披露的简化假设（一字板外排队仍按能买到/跌停按触发价成交）、今日首跑结果
