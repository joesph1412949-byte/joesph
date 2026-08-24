# -*- coding: utf-8 -*-
"""M6横盘突破 / M7年线企稳 / Y8中市值 / SEC策略加载 测试。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
import pandas as pd

import prism.registry as reg
from prism.context import FactorContext
from prism.engine import load_strategy
from prism.strategies import STRATEGIES_DIR
import prism.factors  # noqa: F401  触发扫描注册


def _kline(closes):
    """构造完整K线 DataFrame(close/volume), 与回测适配层同构。"""
    n = len(closes)
    return pd.DataFrame({
        "close": closes,
        "open": closes, "high": closes, "low": closes,
        "volume": [1.0] * n,
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))


def _ctx(closes):
    return FactorContext(code="600000.SH", kline=_kline(closes))


def _flat_then_break():
    """250日 ~9元横盘 → 最后1日放量站上年线并创60日新高(突破)。"""
    base = [9.0 + 0.01 * (i % 7) for i in range(250)]      # 横盘: 振幅约5%
    ma250 = sum(base[-250:]) / 250                         # ≈9.0x
    # 突破日: 收盘 > MA250 且 > 近60日最高
    hi60 = max(base[-60:])
    return base + [max(ma250 * 1.05, hi60 * 1.02)]


def test_m6_hit():
    closes = _flat_then_break()
    assert len(closes) == 251
    res = reg.get_factor("M6")["func"](_ctx(closes))
    assert res["score"] == 1, res["note"]


def test_m6_requires_250days():
    res = reg.get_factor("M6")["func"](_ctx([9.0] * 100))
    assert res["score"] == 0


def test_m6_no_break_still_flat():
    # 一直横盘无突破 → 0
    closes = [9.0 + 0.01 * (i % 7) for i in range(251)]
    res = reg.get_factor("M6")["func"](_ctx(closes))
    assert res["score"] == 0


def test_m7_hit():
    # 沿年线上方运行, 近5日回踩年线不破 → 命中
    base = [10.0 + 0.02 * i for i in range(250)]           # 缓升趋势
    ma250 = sum(base[-250:]) / 250
    # 近5日: 先下探到年线附近, 再收回年线上方
    dip = ma250 * 1.0
    closes = base + [dip, dip * 1.01, dip * 1.02, dip * 1.015, dip * 1.03]
    res = reg.get_factor("M7")["func"](_ctx(closes))
    assert res["score"] == 1, res["note"]


def test_m7_requires_250days():
    res = reg.get_factor("M7")["func"](_ctx([10.0] * 100))
    assert res["score"] == 0


def test_m7_no_support_falls_through():
    # 已跌破年线 → 0
    base = [10.0 + 0.02 * i for i in range(250)]
    ma250 = sum(base[-250:]) / 250
    closes = base + [ma250 * 0.90] * 5
    res = reg.get_factor("M7")["func"](_ctx(closes))
    assert res["score"] == 0


def test_y8_reads_fund():
    ctx = FactorContext(code="600000.SH", fund={
        "Y8": {"score": 1, "note": "流通市值 48亿 ∈[30,100)亿 中市值启动"}})
    res = reg.get_factor("Y8")["func"](ctx)
    assert res["score"] == 1


def test_y8_missing_fund_fails_open():
    res = reg.get_factor("Y8")["func"](
        FactorContext(code="600000.SH", fund={}))
    assert res["score"] == 0


def test_sector_momentum_strategy_loads():
    """SEC 实验策略可加载且因子都存在。"""
    sp = STRATEGIES_DIR / "sector_momentum.json"
    assert sp.exists()
    s = load_strategy(sp)
    fids = set()
    for m in s["scoring_models"]:
        for f in m["factors"]:
            fids.add(f["id"] if isinstance(f, dict) else f)
    assert fids == {"SEC1", "SEC2", "SEC3", "M1", "M6"}
    # 门槛含宏观因子
    gate = s["market_gate"]["factors"]
    assert set(gate) <= {"N1", "N6", "N7", "N8"} and gate


def test_new_factors_registered():
    for fid in ("M6", "M7", "Y8"):
        assert fid in reg.FACTORS, "%s 未注册" % fid