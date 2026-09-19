# -*- coding: utf-8 -*-
"""板块因子(SEC1/SEC2/SEC3/SEC4/SEC6 + S6)测试 — 直接构造 ctx, 验证逻辑。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism.registry import get_factor

import prism.factors  # noqa: F401  触发因子库扫描注册(SEC1/SEC2)


@pytest.fixture(autouse=True)
def _ensure_real_factor_library():
    """本文件全部用例按 id 直取真实因子(SEC1/SEC2/SEC3/SEC4/SEC6), 不注册测试因子。

    它因此隐含依赖"注册表里恰好是真实因子库"。而其他套件的用例
    (test_backtest / test_registry 等)会在自己的 autouse fixture 里
    reg.reset() 换成测试因子, 一旦"还原"快照取在被污染之后, 真实库就会在
    整个 session 里消失 —— 表现为本文件整片 UnknownFactorError(沙箱下稳定复现
    21 例)。此处按需强制重扫, 让本文件不依赖外部状态、自足可跑。
    """
    from prism import registry as _reg
    if "SEC1" not in _reg.FACTORS:
        _reg.scan_factors("prism.factors", force=True)
    yield


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


# ---------------- S6 板块当日走强(与 F4 判别性独立) ----------------
# 2026-09-19 用户拍板: S6 不再做 F4 的复制品。改前 S6 与 F4 表达式逐字相同
# (同为 _utils.sector_count(板块涨停≥3家)), 而 F4 属首板层、S6 属势能层 →
# 同一信号给两层各加一分(双重计数)。改后 S6 改用**已在上下文里的板块指数K线**
# (ctx._extra["mkt"]["sector"], 与 SEC1/SEC2/SEC6 同源, 无新增取数/依赖):
#   所属板块指数当日涨幅 ≥ 1% → 板块整体当日走强。
# 1% 量级与同层已有阈值同档(SEC6 板块5日>2%, SEC2 板块10日>5% ≈ 1%/日),
# 且必须带量级门槛: 仅"当日>0"会完全包含 SEC1(3连阳 ⇒ 今日收阳), 变成同层冗余。

def _ctx_s6(closes, sector="801110", pool=("600000.SH",), code="600000.SH"):
    """S6 上下文: 板块指数K线 close(升序, 最新在最后) + 涨停池(供 F4 对照)。"""
    from prism.context import FactorContext
    mkt = {"sector": {sector: {
        "dates": ["2026-07-%02d" % i for i in range(1, len(closes) + 1)],
        "close": list(closes)}}}
    smap = {code: sector}
    for c in pool:
        smap[c] = sector
    return FactorContext(code=code, sector_map=smap, mkt=mkt,
                         limit_ups=[{"code": c} for c in pool])


def test_s6_sector_up_1pct_hit():
    res = get_factor("S6")["func"](_ctx_s6([100.0, 101.0]))
    assert res["score"] == 1, res["note"]


def test_s6_sector_up_below_1pct_miss():
    res = get_factor("S6")["func"](_ctx_s6([100.0, 100.99]))
    assert res["score"] == 0


def test_s6_sector_down_with_three_limitups_is_not_f4_copy():
    """反向判别①: 板块内 3 只涨停(F4=1) 但板块指数当日收跌 → S6=0。

    改前的 S6 在此恒为 1(与 F4 逐字同式) → 本用例就是"别再复制 F4"的守门人。
    """
    ctx = _ctx_s6([100.0, 99.5],
                  pool=("600000.SH", "600001.SH", "600002.SH"))
    assert get_factor("F4")["func"](ctx)["score"] == 1
    assert get_factor("S6")["func"](ctx)["score"] == 0


def test_s6_sector_up_without_three_limitups_is_not_f4_copy():
    """反向判别②: 板块指数当日 +2% 但板块内仅 1 只涨停 → S6=1, F4=0。

    两向都不同才叫"给了自由度": ①证 S6 不恒等于 F4, ②证 S6 不是 F4 的子集。
    """
    ctx = _ctx_s6([100.0, 102.0], pool=("600000.SH",))
    assert get_factor("F4")["func"](ctx)["score"] == 0
    assert get_factor("S6")["func"](ctx)["score"] == 1


def test_s6_no_mkt():
    """数据缺失 → 归 0(fail-closed), 不静默退化成旧的 sector_count。"""
    from prism.context import FactorContext
    res = get_factor("S6")["func"](
        FactorContext(code="600000.SH", sector_map={"600000.SH": "801110"},
                      limit_ups=[{"code": "600000.SH"}]))
    assert res["score"] == 0


def test_s6_no_sector_map():
    from prism.context import FactorContext
    res = get_factor("S6")["func"](FactorContext(code="600000.SH"))
    assert res["score"] == 0
    assert "无板块归属" in res["note"]


def test_s6_sector_kline_insufficient():
    res = get_factor("S6")["func"](_ctx_s6([100.0]))
    assert res["score"] == 0


def test_s6_base_close_zero():
    res = get_factor("S6")["func"](_ctx_s6([0.0, 101.0]))
    assert res["score"] == 0


def test_s6_non_dict_mkt_does_not_raise():
    """回归(本仓踩过的坑): ctx.get() 拿到 DataFrame 时真值测试会抛 ValueError,
    异常被上层吞成恒 0。此处必须显式判类型 → 不抛且归 0。"""
    import pandas as pd
    from prism.context import FactorContext
    ctx = FactorContext(code="600000.SH", sector_map={"600000.SH": "801110"},
                        mkt=pd.DataFrame({"close": [1.0, 2.0]}))
    assert get_factor("S6")["func"](ctx)["score"] == 0