# -*- coding: utf-8 -*-
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism import backtest
from prism.engine import load_strategy


def _mk_strategy():
    return {
        "id": "bt", "name": "回测策略", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": "A1", "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    }


@pytest.fixture(autouse=True)
def _factors():
    reg.reset()
    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}
    @reg.factor(id="A1", name="a", category="通用", description="")
    def f_a(ctx):
        return {"score": 1, "note": ""}
    yield


def _feeds():
    start = date(2026, 7, 1)
    # 涨停池: 只有 07-01 有 1 只 600000
    def zf(d):
        if d == "20260701":
            return [{"code": "600000.SH", "boards": 1, "theme": "机器人"}]
        return []
    # K线: 600000 从 10 涨到 12(5天后 +20%)
    def kf(code):
        closes = [10.0 * (1.02 ** i) for i in range(8)]
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(8)]
        return list(zip(dates, closes))
    return zf, kf


# ---------------- 简报 3 个测试 ----------------

def test_backtest_runs_full_strategy():
    s = load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    assert rep["trades"] == 1
    assert rep["win_rate"] == 1.0


def test_backtest_fee_and_slippage_apply():
    s = load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf,
                             fee_rate=0.00025, slippage=0.001)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    # 有交易且收益率考虑了费用滑点
    assert rep["trades"] == 1
    assert rep["avg_return_pct"] is not None


def test_backtest_sell_rules_hit():
    s = load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    # 止盈 5%: 5日后 +20% > +5% → 止盈卖出(而非持有到期)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3),
                 sell_rules={"take_profit_pct": 0.05, "stop_loss_pct": 0.05,
                             "max_hold_days": 5})
    assert rep["trades"] == 1


# ---------------- 补充测试 ----------------

def test_backtest_empty_pool_report():
    """涨停池全空 → 空仓报告, 统计字段为 None。"""
    s = load_strategy(_mk_strategy())
    bt = backtest.Backtester(s, zt_feed=lambda d: [], kline_feed=lambda c: [])
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    assert rep["trades"] == 0
    assert rep["trading_days"] == 0
    assert rep["win_rate"] is None
    assert rep["avg_return_pct"] is None
    assert rep["profit_loss_ratio"] is None
    assert rep["max_drawdown_pct"] is None
    assert rep["total_return_pct"] is None


def test_backtest_gate_blocks_when_environment_bad():
    """市场门槛不达标(节点因子返回0) → 空仓, 不选股。"""
    s = _mk_strategy()
    s["market_gate"] = {"model": "node", "threshold": 1, "factors": ["G0"]}

    @reg.factor(id="G0", name="g0", category="node", description="")
    def f_g0(ctx):
        return {"score": 0, "note": ""}

    s = load_strategy(s)
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    assert rep["trades"] == 0
    assert rep["win_rate"] is None


def test_backtest_fee_impact():
    """手续费越高, 单笔净收益越低。"""
    s = load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt0 = backtest.Backtester(s, zt_feed=zf, kline_feed=kf, fee_rate=0.0)
    bt1 = backtest.Backtester(s, zt_feed=zf, kline_feed=kf, fee_rate=0.01)
    rep0 = bt0.run(date(2026, 7, 1), date(2026, 7, 3))
    rep1 = bt1.run(date(2026, 7, 1), date(2026, 7, 3))
    assert rep0["trades"] == 1 and rep1["trades"] == 1
    assert rep1["avg_return_pct"] < rep0["avg_return_pct"]


def test_backtest_compare_params_grid():
    """参数网格对比: 每组参数一行, 含盈亏/回撤等统计。"""
    s = load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rows = bt.compare_params(date(2026, 7, 1), date(2026, 7, 3), [
        {"take_profit": 0.08, "stop_loss": 0.05, "hold_days": 5},
        {"take_profit": 0.05, "stop_loss": 0.05, "hold_days": 5},
    ])
    assert len(rows) == 2
    assert rows[0]["take_profit"] == 0.08 and rows[1]["take_profit"] == 0.05
    assert rows[0]["trades"] == 1 and rows[1]["trades"] == 1
    for row in rows:
        assert "win_rate" in row and "avg_return_pct" in row
        assert "max_drawdown_pct" in row and "hold_days" in row
