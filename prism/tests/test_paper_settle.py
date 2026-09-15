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


def test_backfill_nav(acc):
    """缺口日补算: nav_history 末日后交易日逐日盯市; created 之前不补(M-b)。"""
    acc.state["nav_history"] = []               # 全空 → last=None, 从头补
    days = ["2026-08-28", "2026-09-02", "2026-09-03", "2026-09-08",
            "2026-09-09"]
    # 修正并注明: 简报蓝图内部直读真实时钟判"今日", 而系统时钟(2026-09-01)
    # ≠ 测试帧(NOW=9-8) → 简报测试原样必失败(n=0)。按测试意图(9-9 为未来、
    # 9-8 为今日)给 backfill_nav 注入 now=NOW(与 settle_day 同款 now 参数)。
    n = acc.backfill_nav(
        lambda code, day=None: {"2026-09-02": 9.6, "2026-09-03": 9.4,
                                "2026-09-08": 10.0}.get(day), days, now=NOW)
    assert n == 3                 # 08-28 在 created(09-01) 之前 → 不补; 9-9 未来 → 不补
    hist = acc.state["nav_history"]
    assert [h["date"] for h in hist] == ["2026-09-02", "2026-09-03",
                                         "2026-09-08"]
    assert [h["nav"] for h in hist] == [609600.0, 609400.0, 610000.0]
    assert acc.state["settled_dates"] == ["2026-09-08"]   # 今日补算即结算
