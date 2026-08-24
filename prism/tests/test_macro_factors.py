# -*- coding: utf-8 -*-
"""宏观因子 N6/N7/N8 测试 — 注入 mkt global 段(纳指/美债/VIX)。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.registry as reg
from prism.context import FactorContext
import prism.factors  # noqa: F401  触发因子注册


def _global_mkt(ndx=None, us10y=None, vix=None):
    """构造 mkt.global 段(升序K线)。传入各序列 close 列表。"""
    glob = {}
    if ndx is not None:
        glob["NDX"] = {"dates": ["2026-07-%02d" % (i + 1)
                                 for i in range(len(ndx))],
                       "close": ndx}
    if us10y is not None:
        glob["US10Y"] = {"dates": ["2026-07-%02d" % (i + 1)
                                   for i in range(len(us10y))],
                         "close": us10y}
    if vix is not None:
        glob["VIX"] = {"dates": ["2026-07-%02d" % (i + 1)
                                 for i in range(len(vix))],
                       "close": vix}
    return {"global": glob}


def _ctx(mkt):
    return FactorContext(code="__MKT__", mkt=mkt)


# ---------------- N6 美股隔夜映射 ----------------

def test_n6_hit_ndx_up_1pct():
    # 昨日涨 1.5% → 命中
    mkt = _global_mkt(ndx=[100, 101, 102.5])
    res = reg.get_factor("N6")["func"](_ctx(mkt))
    assert res["score"] == 1, res["note"]


def test_n6_miss_ndx_flat():
    mkt = _global_mkt(ndx=[100, 101, 101.05])   # 昨日 +0.05%
    res = reg.get_factor("N6")["func"](_ctx(mkt))
    assert res["score"] == 0


def test_n6_no_data():
    res = reg.get_factor("N6")["func"](_ctx({"global": {}}))
    assert res["score"] == 0


# ---------------- N7 美债平稳 ----------------

def test_n7_hit_stable():
    # 20日变化 ≤ 0.3% → 平稳命中
    us10y = [4.4] * 20 + [4.55]   # 20日 +0.15%
    mkt = _global_mkt(us10y=us10y)
    res = reg.get_factor("N7")["func"](_ctx(mkt))
    assert res["score"] == 1, res["note"]


def test_n7_miss_rapid_rise():
    # 20日 +0.5% → 流动性收紧 → 不通过
    us10y = [4.4] * 20 + [4.9]
    mkt = _global_mkt(us10y=us10y)
    res = reg.get_factor("N7")["func"](_ctx(mkt))
    assert res["score"] == 0, res["note"]


def test_n7_insufficient():
    mkt = _global_mkt(us10y=[4.4, 4.5])
    res = reg.get_factor("N7")["func"](_ctx(mkt))
    assert res["score"] == 0


# ---------------- N8 VIX 平稳 ----------------

def test_n8_hit_calm():
    mkt = _global_mkt(vix=[18.0, 17.5, 16.9])   # ≤20 平稳
    res = reg.get_factor("N8")["func"](_ctx(mkt))
    assert res["score"] == 1, res["note"]


def test_n8_miss_panic():
    mkt = _global_mkt(vix=[18.0, 22.0, 26.5])   # >25 恐慌
    res = reg.get_factor("N8")["func"](_ctx(mkt))
    assert res["score"] == 0
    assert "恐慌" in res["note"]


def test_n8_empty():
    mkt = _global_mkt(vix=[])
    res = reg.get_factor("N8")["func"](_ctx(mkt))
    assert res["score"] == 0


def test_macro_factors_registered():
    for fid in ("N6", "N7", "N8"):
        assert fid in reg.FACTORS, "%s 未注册" % fid