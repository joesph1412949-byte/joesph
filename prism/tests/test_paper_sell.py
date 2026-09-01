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
