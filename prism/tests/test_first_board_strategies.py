# -*- coding: utf-8 -*-
"""first_board_v03/v04 策略文件测试。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.engine import load_strategy
import prism.factors  # noqa: F401
import prism.registry as reg

STRAT_DIR = Path(__file__).parent.parent / "strategies"


def _load(name):
    return load_strategy(STRAT_DIR / ("%s.json" % name))


def test_v04_loads_with_registered_factors():
    s = _load("first_board_v04")
    assert s["id"] == "first_board_v04"
    fids = s["scoring_models"][0]["factors"]
    assert fids == ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9"]
    for fid in fids:
        assert fid in reg.FACTORS, "%s 未注册" % fid
    assert s["composite"]["cap"] == 9.0
    assert s["market_gate"] == {"model": "node", "threshold": 1,
                                "factors": ["N1"]}
    assert s["filters"]["candidate_min_model"] == 3


def test_v03_baseline_matches_v04_except_factors():
    v03 = _load("first_board_v03")
    v04 = _load("first_board_v04")
    assert v03["scoring_models"][0]["factors"] == [
        "F1", "F2", "F3", "F4", "F5", "F6", "F7"]
    assert v03["composite"]["cap"] == 7.0
    for key in ("market_gate", "filters", "sell_rules"):
        assert v03[key] == v04[key], key      # 除因子/上限外完全同构


def test_v04_runs_end_to_end_smoke():
    """空数据 ctx 全链路跑通: 因子 fail-open 0 → 无候选, 不抛异常。"""
    from prism.context import FactorContext
    from prism.engine import run_screen
    s = _load("first_board_v04")
    market_ctx = FactorContext(code="__MKT__", mkt={}, sector_map={})
    stock = FactorContext(code="600000.SH", kline=None, sector_map={},
                          limit_ups=[], mkt={})
    out = run_screen(s, market_ctx, gate_factors={"N1": 1},
                     stock_contexts={"600000.SH": stock})
    assert out["environment_ok"] is True
    assert out["candidates"] == []
