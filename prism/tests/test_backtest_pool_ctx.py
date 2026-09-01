# -*- coding: utf-8 -*-
"""回测池上下文注入测试 — 今日池(limit_ups)与昨日池(zt_prev)进入个股 ctx。全离线。"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism import backtest
from prism.engine import load_strategy
import prism.factors  # noqa: F401  触发真实因子注册


def _mk_strategy(for_factor):
    return {
        "id": "bt_pool", "name": "池注入回测", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": for_factor, "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    }


@pytest.fixture(autouse=True)
def _node_factor():
    reg.reset()
    reg.scan_factors("prism.factors", force=True)   # F4 等真实因子

    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}
    yield


SECTOR_MAP = {"600000.SH": "BK0475", "000001.SZ": "BK0475",
              "000002.SZ": "BK0475", "600519.SH": "BK0475"}

POOL_0701 = [{"code": "600000.SH", "boards": 1},
             {"code": "000001.SZ", "boards": 2},
             {"code": "000002.SZ", "boards": 1},
             {"code": "600519.SH", "boards": 1}]
POOL_0702 = [{"code": "600000.SH", "boards": 1},
             {"code": "000001.SZ", "boards": 1},
             {"code": "000002.SZ", "boards": 1}]
POOL_0703 = [{"code": "600000.SH", "boards": 2},
             {"code": "000001.SZ", "boards": 2},
             {"code": "000002.SZ", "boards": 1}]


def _feeds():
    start = date(2026, 7, 1)

    def zf(d):
        return {"20260701": POOL_0701, "20260702": POOL_0702,
                "20260703": POOL_0703}.get(d, [])

    def kf(code):
        closes = [10.0 * (1.01 ** i) for i in range(10)]
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(10)]
        return list(zip(dates, closes))
    return zf, kf


def _bt(for_factor):
    s = load_strategy(_mk_strategy(for_factor))
    zf, kf = _feeds()
    return backtest.Backtester(s, zt_feed=zf, kline_feed=kf)


def test_f4_sector_resonance_hits_with_pool_injection():
    """F4(板块共振≥3家)依赖 ctx.limit_ups: 注入后回测可命中(改前恒 0)。"""
    bt = _bt("F4")
    picked = bt._pick(POOL_0703, asof=date(2026, 7, 3), mkt={},
                      sector_map=SECTOR_MAP)
    assert len(picked) == 3          # 池内 3 只同板块, 家数 3 ≥ 3 → 全过


def test_pick_injects_prev_pool_as_zt_prev():
    """prev_pool → mkt["zt_prev"]["codes"], 个股 ctx 可读; 缺省不注入。"""

    @reg.factor(id="T9", name="t", category="test", description="")
    def f_t(ctx):
        prev = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes")
        hit = prev == ["600000.SH", "000001.SZ", "000002.SZ"]
        return {"score": 1 if hit else 0, "note": ""}

    bt = _bt("T9")
    picked = bt._pick(POOL_0703, asof=date(2026, 7, 3), mkt={},
                      sector_map=SECTOR_MAP, prev_pool=POOL_0702)
    assert len(picked) == 3          # 昨日池代码精确匹配 → 全过
    picked = bt._pick(POOL_0703, asof=date(2026, 7, 3), mkt={},
                      sector_map=SECTOR_MAP)
    assert picked == []              # 不传 prev_pool → zt_prev 缺失 → 无候选


def test_run_maintains_prev_pool_across_days():
    """run() 逐日维护 prev_pool: 7-03 的"昨日"= 7-02 池; 首日/不匹配日不命中。"""

    @reg.factor(id="T9", name="t", category="test", description="")
    def f_t(ctx):
        prev = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes")
        hit = prev == ["600000.SH", "000001.SZ", "000002.SZ"]
        return {"score": 1 if hit else 0, "note": ""}

    bt = _bt("T9")
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3), mkt={},
                 sector_map=SECTOR_MAP)
    dates = {t["date"] for t in rep["trade_log"]}
    assert "2026-07-03" in dates      # 昨日池=7-02 → T9 命中
    assert "2026-07-01" not in dates  # 首日 prev_pool=[] → 不命中
    assert "2026-07-02" not in dates  # 昨日池=7-01(4只) → 不匹配 → 不命中
