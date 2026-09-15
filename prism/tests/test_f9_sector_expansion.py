# -*- coding: utf-8 -*-
"""F9 板块延展性测试 — 有高度(涨停≥3且连板≥2)且扩张(今日≥昨日)。全离线。

连板口径(v04 修复, live/回测统一): 连板股 = 昨日也涨停的今日涨停股
(今日池 code ∈ mkt.zt_prev codes)。boards 字段实盘不可得
(datasource/data_source.get_limit_up_stocks 产物无该键), 今日∩昨日
判定两侧口径一致; 含判别用例: boards 字段即使存在也不参与判定。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext
from prism.registry import get_factor

import prism.factors  # noqa: F401


SECTOR_MAP = {"600000.SH": "BK0475", "000001.SZ": "BK0475",
              "000002.SZ": "BK0475", "600519.SH": "BK0475"}

# 3 家同板块涨停(条目不带 boards——连板数完全由 zt_prev 决定)
UP3 = [{"code": "600000.SH"}, {"code": "000001.SZ"}, {"code": "000002.SZ"}]
# 昨日池: 600000/000001 昨日亦板(=今日连板 2 只), 600519 昨日板但今日未板
ZT_PREV_LB2 = ["600000.SH", "000001.SZ", "600519.SH"]


def _ctx(limit_ups, prev_codes, code="600000.SH"):
    mkt = {"zt_prev": {"date": "2026-07-02", "codes": prev_codes}}
    return FactorContext(code=code, sector_map=SECTOR_MAP,
                         limit_ups=limit_ups, mkt=mkt)


def test_f9_hit():
    res = get_factor("F9")["func"](_ctx(UP3, ZT_PREV_LB2))
    assert res["score"] == 1, res["note"]
    assert "连板2只" in res["note"], res["note"]


def test_f9_not_expanding():
    # 今日 3 < 昨日 4 → 不扩张
    res = get_factor("F9")["func"](
        _ctx(UP3, ZT_PREV_LB2 + ["000002.SZ"]))
    assert res["score"] == 0
    assert "扩张" in res["note"]


def test_f9_few_limit_ups():
    # 板块涨停 2 家 < 3 → 无高度
    ups = [{"code": "600000.SH"}, {"code": "000001.SZ"}]
    res = get_factor("F9")["func"](_ctx(ups, ["600000.SH"]))
    assert res["score"] == 0


def test_f9_no_consecutive():
    # 3 家但昨日池无交集(全首板) → 连板 0 → 无高度。
    # 判别点: boards 字段不参与判定——即使 boards=2, 昨日池无交集不算连板
    # (旧 boards 口径回潮时此用例 FAIL: 旧实现按 boards≥2 数出 2 只 → 误命中)。
    ups = [{"code": "600000.SH", "boards": 2},
           {"code": "000001.SZ", "boards": 2},
           {"code": "000002.SZ", "boards": 1}]
    res = get_factor("F9")["func"](_ctx(ups, ["600519.SH"]))
    assert res["score"] == 0, res["note"]
    assert "连板0只" in res["note"], res["note"]


def test_f9_boards_field_ignored():
    """判别用例(修复核心): boards=1 但昨日也涨停 = 真连板 → 计入连板。

    旧 boards 实现按 boards<2 剔除(实盘池条目无 boards 键 → 恒 0),
    此用例在旧代码下 FAIL(证明判别力: 连板判定已不依赖 boards 字段)。
    """
    ups = [{"code": "600000.SH", "boards": 1},
           {"code": "000001.SZ", "boards": 1},
           {"code": "000002.SZ", "boards": 1}]
    res = get_factor("F9")["func"](_ctx(ups, ["600000.SH", "000001.SZ"]))
    assert res["score"] == 1, res["note"]
    assert "连板2只" in res["note"], res["note"]


def test_f9_missing_prev_fail_closed():
    # 昨日池缺失 → fail-closed 0(设计 §3.2: 无昨日基准无法判定扩张)
    res = get_factor("F9")["func"](_ctx(UP3, []))
    assert res["score"] == 0
    assert "昨日" in res["note"]


def test_f9_no_sector():
    ctx = FactorContext(code="600000.SH", sector_map={},
                        limit_ups=UP3,
                        mkt={"zt_prev": {"codes": ["x"]}})
    res = get_factor("F9")["func"](ctx)
    assert res["score"] == 0


def test_f9_registered():
    import prism.registry as reg
    assert "F9" in reg.FACTORS
