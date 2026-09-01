# -*- coding: utf-8 -*-
"""F8 行业边际变化测试 — 直接构造 ctx, 全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext
from prism.registry import get_factor

import prism.factors  # noqa: F401  触发因子库扫描注册


def _ctx(scode="BK0475", commodities=None, no_mkt=False):
    mkt = {} if no_mkt else {"futures": {scode: {
        "name": "基础化工", "commodities": commodities or {}}}}
    return FactorContext(code="600000.SH", sector_map={"600000.SH": scode},
                         mkt=mkt)


def _comm(closes, sym="MA0", name="甲醇"):
    dates = ["2026-07-%02d" % (i % 28 + 1) for i in range(len(closes))]
    return {sym: {"name": name, "dates": dates, "close": closes}}


def test_f8_hit():
    # 21 点: 100 → 103.5 (+3.5% > +3%) → 命中
    res = get_factor("F8")["func"](
        _ctx(commodities=_comm([100.0] * 20 + [103.5])))
    assert res["score"] == 1, res["note"]


def test_f8_miss_at_threshold():
    # 恰好 +3.0% 不大于阈值 → 不命中
    res = get_factor("F8")["func"](
        _ctx(commodities=_comm([100.0] * 20 + [103.0])))
    assert res["score"] == 0


def test_f8_any_commodity_hit():
    # 两品种, 仅第二个命中 → 命中(任一品种判定)
    comms = _comm([100.0] * 21, sym="MA0")
    comms["TA0"] = {"name": "PTA", "dates": ["2026-07-01"] * 21,
                    "close": [100.0] * 20 + [105.0]}
    res = get_factor("F8")["func"](_ctx(commodities=comms))
    assert res["score"] == 1, res["note"]


def test_f8_insufficient_data():
    res = get_factor("F8")["func"](_ctx(commodities=_comm([100.0] * 10)))
    assert res["score"] == 0
    assert "不足" in res["note"]


def test_f8_no_commodity():
    res = get_factor("F8")["func"](_ctx(commodities={}))
    assert res["score"] == 0


def test_f8_no_sector():
    res = get_factor("F8")["func"](
        FactorContext(code="600000.SH", sector_map={}, mkt={}))
    assert res["score"] == 0
    assert "无板块归属" in res["note"]


def test_f8_no_mkt():
    # mkt 未注入 → fail-open 0
    res = get_factor("F8")["func"](
        FactorContext(code="600000.SH", sector_map={"600000.SH": "BK0475"}))
    assert res["score"] == 0


def test_f8_registered():
    import prism.registry as reg
    assert "F8" in reg.FACTORS
