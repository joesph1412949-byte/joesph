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
