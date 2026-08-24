# -*- coding: utf-8 -*-
"""板块因子(SEC1/SEC2)测试 — 直接构造 ctx, 验证连阳/10日涨幅逻辑。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.registry import get_factor

import prism.factors  # noqa: F401  触发因子库扫描注册(SEC1/SEC2)


def _ctx(code="600000.SH", sector="BK0475", closes=None):
    from prism.context import FactorContext
    mkt = {"sector": {sector: {"dates": ["2026-07-%02d" % i
                                         for i in range(1, len(closes) + 1)],
                               "close": closes}}}
    return FactorContext(code=code, sector_map={code: sector}, mkt=mkt)


def test_sec1_updays_hit():
    # 结尾连续收阳: 100→101→103→105(4连阳) → 命中
    closes = [100, 100, 101, 103, 105]
    res = get_factor("SEC1")["func"](_ctx(closes=closes))
    assert res["score"] == 1


def test_sec1_updays_miss():
    # 最后一天收阴, 连阳中断 → 不命中
    closes = [100, 101, 103, 105, 104, 103]
    res = get_factor("SEC1")["func"](_ctx(closes=closes))
    assert res["score"] == 0


def test_sec1_no_sector():
    from prism.context import FactorContext
    res = get_factor("SEC1")["func"](
        FactorContext(code="600000.SH", sector_map={}))
    assert res["score"] == 0
    assert "无板块归属" in res["note"]


def test_sec1_no_kline():
    res = get_factor("SEC1")["func"](_ctx(closes=[]))
    assert res["score"] == 0


def test_sec2_10d_hit():
    # 近10日涨幅 > 5%: 8/前收100 → 107
    closes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 107]
    res = get_factor("SEC2")["func"](_ctx(closes=closes))
    assert res["score"] == 1


def test_sec2_10d_miss():
    closes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 103]
    res = get_factor("SEC2")["func"](_ctx(closes=closes))
    assert res["score"] == 0


def test_sec2_insufficient():
    res = get_factor("SEC2")["func"](_ctx(closes=[100, 101]))
    assert res["score"] == 0


# ---------------- SEC3 板块资金流 ----------------

def _ctx_flow(sector="BK0475", net_ins=None):
    from prism.context import FactorContext
    mkt = {"sector_flow": {sector: {
        "dates": ["2026-07-%02d" % i for i in range(1, len(net_ins) + 1)],
        "main_net_in": net_ins}}}
    return FactorContext(code="600000.SH", sector_map={"600000.SH": sector},
                         mkt=mkt)


def test_sec3_flow_positive_hit():
    # 近5日净流入为正 → 命中
    net = [-1e8, 2e8, 3e8, -0.5e8, 1e8]   # 合计 +4.5亿
    res = get_factor("SEC3")["func"](_ctx_flow(net_ins=net))
    assert res["score"] == 1, res["note"]


def test_sec3_flow_negative_miss():
    net = [-2e8, -1e8, 0.5e8, -1e8, -0.5e8]   # 合计 -4亿
    res = get_factor("SEC3")["func"](_ctx_flow(net_ins=net))
    assert res["score"] == 0


def test_sec3_no_sector():
    from prism.context import FactorContext
    res = get_factor("SEC3")["func"](
        FactorContext(code="600000.SH", sector_map={}))
    assert res["score"] == 0
    assert "无板块归属" in res["note"]


def test_sec3_no_flow_data():
    res = get_factor("SEC3")["func"](
        _ctx_flow(sector="BK0475", net_ins=[]))
    assert res["score"] == 0


def test_sec3_flow_insufficient():
    res = get_factor("SEC3")["func"](_ctx_flow(net_ins=[1e8, 2e8]))
    assert res["score"] == 0       # 不足5日


def test_sec_factors_registered():
    import prism.registry as reg
    for fid in ("SEC1", "SEC2", "SEC3"):
        assert fid in reg.FACTORS, "%s 未注册" % fid