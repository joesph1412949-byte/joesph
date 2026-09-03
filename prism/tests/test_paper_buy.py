# -*- coding: utf-8 -*-
"""模拟盘买入(排板委托)测试 — mock provider/run_screen/K线, 全离线。

偏离原"直接成交"语义(本任务改道): buy_from_screen 现在创建排板委托 pending,
不再立即记账成交——原断言 holdings/cash 增长改为 pending 入队 + 冻结语义;
成交判定归 check_pending_buys(Task 2 新状态机, 见 test_paper_queue)。"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.engine
import prism.paper as paper_mod
from prism.paper import PaperAccount


NOW = datetime(2026, 9, 1, 10, 0, 0)


def _tick(last_price, volume, bid_vol=0, last_close=None):
    """xtdata 原生字段名 tick: 封单门槛需 bidVol×price>=2000万。"""
    return {"lastPrice": last_price, "lastVolume": volume,
            "bidVol": [bid_vol], "lastClose": last_close}


def _ticks_for(code, price, volume):
    """候选池盘口注入: 200万股(bidVol)×10元=2000万 恰过封单门槛。"""
    return lambda codes: {code: _tick(price, volume, bid_vol=2_000_000)}


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


def test_buy_creates_pending(acc, monkeypatch):
    """改道语义(偏离原test_buy_normal"直接成交"): 返回排板委托 pending,
    冻结不改 cash、不进 holdings/不落 trades; 成交由 check_pending_buys 判。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW,
                              tick_provider=_ticks_for("600000.SH", 10.0, 1_000_000))
    assert out["env_ok"] is True and len(out["bought"]) == 1
    b = out["bought"][0]
    # 净值30%=30万 / 涨停价10.0(排板成交无上滑) → 30000股; 冻结=shares×price
    assert b["shares"] == 30000
    assert b["price"] == 10.0
    assert b["amount"] == pytest.approx(300000.0, rel=1e-3)
    st = acc.state
    assert st["cash"] == 1000000.0                # 冻结不改 cash
    assert len(st["pending_buys"]) == 1           # 入队而非直接成交
    assert st["holdings"] == [] and st["trades"] == []
    assert st["screens_done"] == ["2026-09-01T10:00"]


def test_buy_one_word_board_blocked(acc, monkeypatch):
    """一字板(low==涨停价, 从未开板) → 买不到。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND, low=10.0), now=NOW)
    assert out["bought"] == []
    assert any(s["reason"] == "一字板买不到" for s in out["skipped"])
    assert acc.state["cash"] == 1000000.0


def test_buy_opened_board_ok(acc, monkeypatch):
    """开过板(low<涨停价) → 可排板(建委托入队)。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND, low=9.9), now=NOW,
                              tick_provider=_ticks_for("600000.SH", 10.0, 1_000_000))
    assert len(out["bought"]) == 1
    assert len(acc.state["pending_buys"]) == 1


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
    acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW,
                        tick_provider=_ticks_for("600000.SH", 10.0, 1_000_000))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW,
                              tick_provider=_ticks_for("600000.SH", 10.0, 1_000_000))
    assert out.get("already_done") is True
    assert len(acc.state["pending_buys"]) == 1    # 不重复排板(原断言 trades==1 → pending)
    assert acc.state["trades"] == []


def test_buy_rollback_on_save_failure(acc, monkeypatch):
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))

    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(acc, "save", boom)
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert out.get("bought") == []              # 建委托回滚
    assert acc.state["cash"] == 1000000.0       # 内存回滚
    assert acc.state["holdings"] == []
    assert acc.state["pending_buys"] == []


def test_buy_no_tick_skips_queue(acc, monkeypatch):
    """兼容路径: 不传 tick_provider(daemon 注入前/CLI --once 无盘口) →
    保守不建委托, skipped 记"排板不通过", 返回字段兼容既有。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert out["env_ok"] is True and out["bought"] == []
    assert any(s["reason"] == "排板不通过" for s in out["skipped"])
    assert acc.state["pending_buys"] == []
    assert acc.state["screens_done"] == ["2026-09-01T10:00"]


# ---------- 终审修复 M-a: _buyable 今日判定用注入 now ----------
def test_buy_skip_traded_today(acc, monkeypatch):
    """同日已成交(排板成交后清仓只剩流水) → 拒买"今日已交易"; 判定用注入 now 的
    日期而非系统时钟(注入 09-02, 系统时钟为 09-01, 旧实现会漏判重买)。

    偏离原测试: 改道后首单=建委托, 故先经 check_pending_buys 成交(queue_fill)
    再清仓, 保留"今日已成交→今日已交易"原意图。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    now2 = datetime(2026, 9, 2, 10, 0, 0)
    acc.buy_from_screen(_FakeProvider(ups=CAND), now=now2,
                        tick_provider=_ticks_for("600000.SH", 10.0, 1_000_000))
    # 成交量穿越前排(1e6+200万+3万)且 last==涨停价 → 排板成交
    acc.check_pending_buys({"600000.SH": _tick(10.0, 3_030_000)},
                           now=now2.replace(minute=1))
    assert len(acc.state["trades"]) == 1
    assert acc.state["trades"][-1]["reason"] == "queue_fill"
    acc.state["holdings"] = []                  # 手动清仓, 排除"已持仓"路径
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=now2.replace(minute=5),
                              tick_provider=_ticks_for("600000.SH", 10.0, 1_000_000))
    assert any(s["reason"] == "今日已交易" for s in out["skipped"])
    assert out["bought"] == []
    assert len(acc.state["trades"]) == 1        # 不重复排板/成交


def test_buy_skip_insufficient_cash(acc, monkeypatch):
    """现金 < 净值×30% → 跳过"现金不足", 不扣款。"""
    monkeypatch.setattr(prism.engine, "run_screen", _fake_run_screen(CAND))
    acc.state["holdings"].append({
        "code": "000001.SZ", "shares": 5000, "cost": 9.5,
        "buy_date": "2026-08-30", "buy_price": 9.5, "entry_nav": 1e6})
    acc.state["cash"] = 12000.0                 # nav=12000+47500=59500, 阈值17850
    out = acc.buy_from_screen(_FakeProvider(ups=CAND), now=NOW)
    assert any(s["reason"] == "现金不足" for s in out["skipped"])
    assert out["bought"] == []
    assert acc.state["cash"] == 12000.0         # 未扣款
