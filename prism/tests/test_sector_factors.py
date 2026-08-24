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
    for fid in ("SEC1", "SEC2", "SEC3", "SEC4", "SEC6"):
        assert fid in reg.FACTORS, "%s 未注册" % fid


# ---------------- SEC4 板块拥挤度 ----------------

def _ctx_amount(sector="BK0475", amounts=None):
    """构造含 amount 的板块K线 ctx(245+ 天成交额)。"""
    import pandas as pd
    from prism.context import FactorContext
    n = len(amounts)
    mkt = {"sector": {sector: {
        "dates": ["2026-01-%02d" % (i % 28 + 1) for i in range(n)],
        "close": [100.0] * n,
        "amount": amounts}}}
    return FactorContext(code="600000.SH", sector_map={"600000.SH": sector},
                         mkt=mkt)


def test_sec4_normal_volume_hit():
    # 近5日均成交额接近240日均值(放大1.1倍) → 健康命中
    amounts = [1e9] * 240 + [1.1e9] * 5
    res = get_factor("SEC4")["func"](_ctx_amount(amounts=amounts))
    assert res["score"] == 1, res["note"]


def test_sec4_crowded_miss():
    # 近5日均成交额 3倍于基数 → 拥挤过热 → 不通过
    amounts = [1e9] * 240 + [3e9] * 5
    res = get_factor("SEC4")["func"](_ctx_amount(amounts=amounts))
    assert res["score"] == 0
    assert "拥挤" in res["note"]


def test_sec4_insufficient():
    res = get_factor("SEC4")["func"](_ctx_amount(amounts=[1e9] * 10))
    assert res["score"] == 0


def test_sec4_no_sector():
    from prism.context import FactorContext
    res = get_factor("SEC4")["func"](
        FactorContext(code="600000.SH", sector_map={}))
    assert res["score"] == 0


# ---------------- SEC6 行业联合动量 ----------------

def _ctx_joint(stock_closes, sector_closes=None, sector="BK0475"):
    """构造个股K线 + 板块K线的 ctx(共振测试)。"""
    import pandas as pd
    from prism.context import FactorContext
    n = len(stock_closes)
    kline = pd.DataFrame({
        "close": stock_closes, "open": stock_closes,
        "high": stock_closes, "low": stock_closes,
        "volume": [1.0] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="B"))
    mkt = {"sector": {sector: {
        "dates": [(pd.Timestamp("2026-01-01") + pd.Timedelta(days=i))
                  .strftime("%Y-%m-%d") for i in range(n)],
        "close": sector_closes or stock_closes}}}
    return FactorContext(code="600000.SH", kline=kline,
                         sector_map={"600000.SH": sector}, mkt=mkt)


def test_sec6_joint_hit():
    # 个股5日+6%, 板块5日+3% → 共振命中
    stock = [10, 10.1, 10.2, 10.3, 10.4, 10.6]
    sector = [100, 101, 102, 103, 102.5, 103.2]
    res = get_factor("SEC6")["func"](_ctx_joint(stock, sector))
    assert res["score"] == 1, res["note"]


def test_sec6_stock_only_no_resonance():
    # 个股涨6% 但板块平 → 无共振 → 0
    stock = [10, 10.1, 10.2, 10.3, 10.4, 10.6]
    sector = [100, 99.5, 100.5, 100, 101, 100.5]
    res = get_factor("SEC6")["func"](_ctx_joint(stock, sector))
    assert res["score"] == 0


def test_sec6_stock_weak():
    # 个股 5日<3% → 0
    stock = [10, 10.05, 10.1, 10.15, 10.2, 10.25]
    sector = [100, 101, 102, 103, 104, 105]
    res = get_factor("SEC6")["func"](_ctx_joint(stock, sector))
    assert res["score"] == 0


def test_sec6_insufficient():
    res = get_factor("SEC6")["func"](_ctx_joint([10, 11]))
    assert res["score"] == 0