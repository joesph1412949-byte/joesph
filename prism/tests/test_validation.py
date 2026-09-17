# -*- coding: utf-8 -*-
"""`prism/validation.py`(移植自 Vibe-Trading `agent/backtest/validation.py`, MIT)
+ 报告 validation 字段集成的离线测试。

覆盖:
  ④ 三函数对固定收益序列输出稳定(固定 seed → 逐字段一致)
  ⑤ 移植保真的参数校验分支(n_simulations/n_bootstrap/confidence/n_windows/样本量)
  ⑥ 报告 validation: 有交易/零交易两分支都有该键; 计算失败不影响回测返回
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import pytest

import prism.registry as reg
from prism import backtest, validation
from prism.engine import load_strategy

START = date(2026, 7, 1)


class _T:
    """鸭子类型交易(移植函数只读 `.pnl` 与 `.entry_time`)。"""

    __slots__ = ("pnl", "entry_time")

    def __init__(self, pnl, entry_time):
        self.pnl = pnl
        self.entry_time = entry_time


def _trades(pnls):
    """固定收益序列 → 交易序列(entry_time 用 ISO 日期串, 与净值曲线索引同型)。"""
    return [_T(p, (START + timedelta(days=i)).strftime("%Y-%m-%d"))
            for i, p in enumerate(pnls)]


def _equity(n=20):
    """确定性净值序列(无随机): 涨跌交替, 保证 std>0。"""
    navs = [100.0]
    for i in range(n - 1):
        navs.append(navs[-1] * (1.0 + (0.012 if i % 3 else -0.005)))
    idx = [(START + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    return pd.Series(navs, index=idx)


# ---------------- ④ 固定 seed 输出稳定 ----------------

def test_monte_carlo_stable_with_fixed_seed():
    """④ monte_carlo_test: 同 seed 两次逐字段一致; 换 seed → 置换样本变。"""
    tr = _trades([6.0, -3.0, 8.0, -2.5, 4.0, -1.0, 5.5, -4.0])
    a = validation.monte_carlo_test(tr, 100.0)
    b = validation.monte_carlo_test(tr, 100.0)
    assert a == b
    assert a["n_simulations"] == 1000 and a["n_trades"] == 8
    assert 0.0 <= a["p_value_sharpe"] <= 1.0
    assert len(a["sharpe_samples"]) == 1000
    # 固定 seed 的稳定性 = 可复现; 换 seed 必须换出不同置换(否则 seed 没接上)
    c = validation.monte_carlo_test(tr, 100.0, seed=7)
    assert c["sharpe_samples"] != a["sharpe_samples"]


def test_bootstrap_sharpe_ci_stable_with_fixed_seed():
    """④ bootstrap_sharpe_ci: 同 seed 稳定 + 置信区间自洽。"""
    eq = _equity()
    a = validation.bootstrap_sharpe_ci(eq)
    b = validation.bootstrap_sharpe_ci(eq)
    assert a == b
    assert a["n_bootstrap"] == 1000 and a["confidence"] == 0.95
    assert a["ci_lower"] <= a["median_sharpe"] <= a["ci_upper"]
    assert len(a["sharpe_samples"]) == 1000
    assert 0.0 <= a["prob_positive"] <= 1.0


def test_walk_forward_stable_and_splits_windows():
    """④ walk_forward_analysis: 纯确定性(无 seed) → 两次一致; 窗口首尾相接。"""
    eq, tr = _equity(), _trades([2.0, -1.0, 3.0, 1.5])
    a = validation.walk_forward_analysis(eq, tr)
    b = validation.walk_forward_analysis(eq, tr)
    assert a == b
    assert a["n_windows"] == 5 and len(a["windows"]) == 5
    assert [w["window"] for w in a["windows"]] == [1, 2, 3, 4, 5]
    assert sum(w["trades"] for w in a["windows"]) == len(tr)
    assert a["windows"][0]["start"] == eq.index[0]


# ---------------- ⑤ 移植保真的校验分支 ----------------

def test_input_guards_ported_verbatim():
    """⑤ 参数/样本量校验分支(移植不得简化): 返回 error 而非抛异常。"""
    tr = _trades([1.0, 2.0, 3.0])
    eq = _equity()
    assert "error" in validation.monte_carlo_test(_trades([1.0, 2.0]), 100.0)
    assert "error" in validation.monte_carlo_test(tr, 100.0, n_simulations=0)
    assert "error" in validation.monte_carlo_test(tr, 100.0, seed=-1)
    assert "error" in validation.bootstrap_sharpe_ci(eq, n_bootstrap=0)
    assert "error" in validation.bootstrap_sharpe_ci(eq, confidence=1.0)
    assert "error" in validation.bootstrap_sharpe_ci(_equity(3))
    assert "error" in validation.walk_forward_analysis(_equity(8), tr)


# ---------------- ⑥ 报告集成 ----------------

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


# 5 天池轮换 5 只(涨幅不同 → 收益序列非退化), 持有 5 天 → 净值曲线 10 个点
_POOL = {0: ("600001.SH", 1.020), 1: ("600002.SH", 1.030),
         2: ("600003.SH", 1.015), 3: ("600004.SH", 1.025),
         4: ("600005.SH", 1.010)}


def _bt_run(pool_days=5, **kw):
    """跑一次离线回测: 前 pool_days 天各选 1 只(持有 5 天 → 曲线 10 点)。"""
    s = load_strategy({
        "id": "bt_val", "name": "validation", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": "A1", "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.30, "stop_loss_pct": 0.5,
                       "max_hold_days": 5},
    })

    def zf(d):
        for i, (code, _rate) in _POOL.items():
            if i < pool_days and d == (START + timedelta(days=i)).strftime("%Y%m%d"):
                return [{"code": code, "boards": 1, "theme": "机器人"}]
        return []

    def kf(code):
        rate = next(r for c, r in _POOL.values() if c == code)
        return [((START + timedelta(days=i)).strftime("%Y-%m-%d"),
                 10.0 * rate ** i) for i in range(12)]

    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    return bt.run(START, START + timedelta(days=9), **kw)


def test_report_has_validation_in_both_branches():
    """⑥ 有交易与零交易两个报告分支都必须带 validation 键(结构统一)。"""
    rep = _bt_run()
    assert rep["trades"] == 5
    v = rep["validation"]
    assert set(v) == {"note", "monte_carlo", "bootstrap", "walk_forward"}
    # 数据够(5 笔 / 10 点曲线) → 三个都真算出结果, 不是 error 占位
    for name in ("monte_carlo", "bootstrap", "walk_forward"):
        assert "error" not in v[name], (name, v[name])
    assert v["monte_carlo"]["n_trades"] == 5
    assert v["walk_forward"]["n_windows"] == 5
    # 零交易报告同样带该键(样本不足时各函数自带 error, 但不缺键也不抛)
    empty = _bt_run(pool_days=0)
    assert empty["trades"] == 0
    assert set(empty["validation"]) == {"note", "monte_carlo", "bootstrap",
                                        "walk_forward"}


def test_report_validation_can_be_disabled():
    """⑥ validate=False → 不跑验证(报告字段留空, 结构不变)。"""
    rep = _bt_run(validate=False)
    assert rep["trades"] == 5
    assert rep["validation"] == {}


def test_report_validation_failure_does_not_break_run(monkeypatch):
    """⑥ validation 计算抛异常 → 回测照常返回(报告带 error, 不抛出)。"""
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(backtest.validation, "monte_carlo_test", boom)
    rep = _bt_run()
    assert rep["trades"] == 5
    assert "error" in rep["validation"]
