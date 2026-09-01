# -*- coding: utf-8 -*-
"""F9 板块延展性测试 — 有高度(涨停≥3且连板≥2)且扩张(今日≥昨日)。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext
from prism.registry import get_factor

import prism.factors  # noqa: F401


SECTOR_MAP = {"600000.SH": "BK0475", "000001.SZ": "BK0475",
              "000002.SZ": "BK0475", "600519.SH": "BK0475"}

UP3_LB2 = [{"code": "600000.SH", "boards": 2},
           {"code": "000001.SZ", "boards": 2},
           {"code": "000002.SZ", "boards": 1}]


def _ctx(limit_ups, prev_codes, code="600000.SH"):
    mkt = {"zt_prev": {"date": "2026-07-02", "codes": prev_codes}}
    return FactorContext(code=code, sector_map=SECTOR_MAP,
                         limit_ups=limit_ups, mkt=mkt)


def test_f9_hit():
    res = get_factor("F9")["func"](
        _ctx(UP3_LB2, ["600000.SH", "000001.SZ", "600519.SH"]))
    assert res["score"] == 1, res["note"]


def test_f9_not_expanding():
    # 今日 3 < 昨日 4 → 不扩张
    res = get_factor("F9")["func"](
        _ctx(UP3_LB2, ["600000.SH", "000001.SZ", "000002.SZ", "600519.SH"]))
    assert res["score"] == 0
    assert "扩张" in res["note"]


def test_f9_few_limit_ups():
    # 板块涨停 2 家 < 3 → 无高度
    ups = [{"code": "600000.SH", "boards": 2},
           {"code": "000001.SZ", "boards": 1}]
    res = get_factor("F9")["func"](_ctx(ups, ["600000.SH"]))
    assert res["score"] == 0


def test_f9_no_boards():
    # 3 家但连板 0(全首板) → 无高度
    ups = [{"code": "600000.SH", "boards": 1},
           {"code": "000001.SZ", "boards": 1},
           {"code": "000002.SZ", "boards": 1}]
    res = get_factor("F9")["func"](_ctx(ups, ["600000.SH"]))
    assert res["score"] == 0


def test_f9_missing_prev_fail_closed():
    # 昨日池缺失 → fail-closed 0(设计 §3.2: 无昨日基准无法判定扩张)
    res = get_factor("F9")["func"](_ctx(UP3_LB2, []))
    assert res["score"] == 0
    assert "昨日" in res["note"]


def test_f9_no_sector():
    ctx = FactorContext(code="600000.SH", sector_map={},
                        limit_ups=UP3_LB2,
                        mkt={"zt_prev": {"codes": ["x"]}})
    res = get_factor("F9")["func"](ctx)
    assert res["score"] == 0


def test_f9_registered():
    import prism.registry as reg
    assert "F9" in reg.FACTORS
