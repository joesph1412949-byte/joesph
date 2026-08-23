# -*- coding: utf-8 -*-
"""回测 mkt 注入链路测试 — 市场数据层快照通过 run(mkt=) 注入 SEC 因子。全离线。"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism import backtest
from prism.engine import load_strategy
import prism.factors  # noqa: F401  触发 SEC1/SEC2 注册


def _mk_strategy(for_factor="SEC1"):
    return {
        "id": "bt_sec", "name": "板块因子回测", "description": "",
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
    reg.scan_factors("prism.factors", force=True)   # 恢复 SEC1/SEC2 等真实因子
    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}
    yield


def _feeds():
    """涨停池只有 07-01 一只; K线 8 天缓涨。sector_map: 600000 → BK0475。"""
    start = date(2026, 7, 1)
    sector_map = {"600000.SH": "BK0475"}

    def zf(d):
        if d == "20260701":
            return [{"code": "600000.SH", "boards": 1, "theme": "机器人"}]
        return []

    def kf(code):
        closes = [10.0 * (1.01 ** i) for i in range(8)]
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(8)]
        return list(zip(dates, closes))
    return zf, kf, sector_map


def _mkt_snapshot(closes):
    """市场数据快照: BK0475 板块K线(升序)。"""
    dates = ["2026-07-%02d" % (i + 1) for i in range(len(closes))]
    return {"sector": {"BK0475": {"dates": dates, "close": closes}}}


def test_sec1_hit_via_backtest_mkt_injection():
    s = load_strategy(_mk_strategy("SEC1"))
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    # 板块连阳: 结尾连续 4 阳 → SEC1 命中 → 选股成功 1 笔
    mkt = _mkt_snapshot([100, 100, 101, 103, 105, 107, 109, 111])
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3),
                 mkt=mkt, sector_map=sector_map)
    assert rep["trades"] == 1


def test_sec1_miss_without_injection():
    """不注入 mkt → SEC1 fail-open 0 → 无交易(证明注入是命中的必要条件)。"""
    s = load_strategy(_mk_strategy("SEC1"))
    zf, kf, _sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    assert rep["trades"] == 0


def test_sec2_hit_via_backtest_mkt_injection():
    s = load_strategy(_mk_strategy("SEC2"))
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    # 板块近10日涨幅 > 5%: 100 → 107
    mkt = _mkt_snapshot([100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 107])
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3),
                 mkt=mkt, sector_map=sector_map)
    assert rep["trades"] == 1