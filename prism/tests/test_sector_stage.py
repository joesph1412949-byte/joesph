# -*- coding: utf-8 -*-
"""sector_stage 单元测试 — 罐头 mkt 切片, 全离线确定性。"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.sector_stage as ss


def _dates(n):
    """n 个伪交易日(ISO 字符串; 与自然日历无关, date 键对齐即可)。"""
    d0 = date(2026, 6, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _mk(sectors, bench=None, n=30):
    """sectors: {code: (closes, amounts)}; bench: 上证 closes(同 n)。"""
    ds = _dates(n)
    sec = {c: {"dates": ds, "close": cl, "amount": am, "name": "S" + c}
           for c, (cl, am) in sectors.items()}
    mkt = {"sector": sec}
    if bench is not None:
        mkt["benchmark"] = {"dates": ds, "close": bench, "amount": [1e8] * n}
    return mkt


def _row(table, code):
    return next(r for r in table if r["code"] == code)


# ---------------- 占比 ----------------

def test_share_series_ratio():
    n = 25
    mkt = _mk({"A": ([100.0] * n, [3e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert abs(a["share5"] - 0.75) < 1e-9
    assert abs(a["share20"] - 0.75) < 1e-9
    assert abs(a["share_chg"]) < 1e-9


# ---------------- 孕育信号 ----------------

def test_gestation_share_struct_hits():
    """无 benchmark: share+struct 两信号 → 孕育期。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * 20 + [4e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "孕育期"
    assert a["signals"] == ["share", "struct"]
    assert a["hits"] == 2


def test_gestation_rel_with_benchmark():
    """上证走平、板块末3日连涨 → rel 信号命中。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert "rel" in a["signals"]
    assert a["stage"] == "孕育期"


def test_rel_skipped_without_benchmark():
    """benchmark 缺失 → rel 不可判不计数(fail-open)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert "rel" not in a["signals"]


def test_gestation_excluded_when_launched():
    """5日涨 12% ≥ 8% 未启动上限 → 不判孕育(判高潮: 占比分位+大涨)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 102.0, 104.0, 106.0, 108.0, 112.0]
    mkt = _mk({"A": (closes, [1e8] * 20 + [4e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "高潮期"


# ---------------- 五阶段分支 ----------------

def test_stage_ebb():
    """占比回落 + 5日下跌 → 退潮期。"""
    n = 30
    closes = [100.0] * 24 + [103.0, 102.5, 102.0, 101.5, 101.0, 100.0]
    mkt = _mk({"A": (closes, [4e8] * 20 + [1e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "退潮期"


def test_stage_peak():
    """占比历史分位≥90% 且 5日涨>10% → 高潮期。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 102.0, 104.0, 106.0, 108.0, 112.0]
    mkt = _mk({"A": (closes, [1e8] * 29 + [20e8]),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "高潮期"


def test_stage_main():
    """5日涨6%>5% 且占比升 → 主升期(占比分位命中但涨幅≤10%不判高潮)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 106.0]
    mkt = _mk({"A": (closes, [1e8] * 20 + [2e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "主升期"


def test_stage_start():
    """3日涨4.5%>4% 且 10日涨4.5%≤5% → 启动期(先于孕育判定)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.0, 100.0, 100.2, 102.0, 104.5]
    mkt = _mk({"A": (closes, [1e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "启动期"


def test_stage_rest():
    """全平: 无任何信号 → 休整。"""
    n = 30
    mkt = _mk({"A": ([100.0] * n, [1e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "休整"


def test_stage_insufficient_data():
    """K线<11 根 → 数据不足(不抛)。"""
    mkt = _mk({"A": ([100.0] * 10, [1e8] * 10)}, n=10)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "数据不足"


def test_sector_table_metrics():
    """r3/r5/r10 数值与行结构。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert abs(a["r5"] - 3.0) < 1e-9
    assert abs(a["r10"] - 3.0) < 1e-9
    assert abs(a["r3"] - (103.0 / 101.0 - 1) * 100) < 1e-9
    assert a["name"] == "SA"
    # W1 扩展: 行结构恒含新四列(缺省参时 new_high/etf 为 None, pos_cap/
    # week_rank 为内在列恒输出)
    assert set(a) == {"code", "name", "stage", "note", "r3", "r5", "r10",
                      "share5", "share20", "share_chg", "hits", "signals",
                      "new_high", "etf", "pos_cap", "week_rank"}


def test_dirty_close_with_benchmark_no_crash():
    """收盘序列含 None(脏数据) + benchmark 存在 → 不崩溃且日期对齐不错位。"""
    n = 30
    closes = [100.0] * 24 + [None, 100.5, 101.0, 101.5, 102.0, 103.0]
    am = [1e8] * n
    mkt = _mk({"A": (closes, am),
               "B": ([100.0] * n, am)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "孕育期"
    assert "rel" in a["signals"]


# ---------------- 资金惯性 ----------------

def test_flow_inertia_streaks():
    """A/B 连续6天前3(系统性增配), C 中途掉出(streak0 剔除), D 后2天上榜。"""
    dates, rows = [], {}
    for i in range(6):
        d = "2026-09-%02d" % (i + 1)
        dates.append(d)
        day = [{"code": "BK_A", "name": "甲", "net_in": 5e8},
               {"code": "BK_B", "name": "乙", "net_in": 4e8},
               {"code": ("BK_C" if i < 4 else "BK_E"), "name": "x",
                "net_in": 1e8}]
        if i >= 4:
            day.append({"code": "BK_D", "name": "丁", "net_in": 2e8})
        rows[d] = day
    out = ss.flow_inertia({"dates": dates, "rows": rows})
    assert [r["code"] for r in out] == ["BK_A", "BK_B", "BK_D"]
    assert out[0]["streak"] == 6 and out[0]["systematic"] is True
    assert out[1]["streak"] == 6 and out[1]["systematic"] is True
    assert out[2]["streak"] == 2 and out[2]["systematic"] is False
    assert out[2]["last_net_in"] == 2e8


def test_flow_inertia_empty():
    assert ss.flow_inertia(None) == []
    assert ss.flow_inertia({}) == []
    assert ss.flow_inertia({"dates": [], "rows": {}}) == []


# ---------------- 60日新高家数(W1) ----------------

def _zt(code, closes):
    """zt 缓存条目形态: {"dates": [...], "close": [...]}. 键带后缀(F9 契约)."""
    return {code: {"dates": _dates(len(closes)), "close": closes}}


def test_new_high_counts_basic():
    """A 最新收盘创 60 日新高 → nh=1; B 没创 → 0; 各行业 base 独立。"""
    smap = {"600001.SH": "银行", "000002.SZ": "银行", "300003.SZ": "传媒"}
    zt = {}
    zt.update(_zt("600001.SH", [float(100 + i) for i in range(60)]))   # 创新高
    zt.update(_zt("000002.SZ", [float(200 - i) for i in range(60)]))   # 单边跌
    zt.update(_zt("300003.SZ", [100.0] * 60))                          # 平盘=新高
    out = ss.new_high_counts(smap, zt, window=60)
    assert out["银行"] == {"nh": 1, "base": 2}
    assert out["传媒"] == {"nh": 1, "base": 1}


def test_new_high_counts_short_history_excluded():
    """len(close)<60 不计入 base(也不计 nh)。单边跌序列 → nh=0 严格可判。"""
    smap = {"600001.SH": "银行", "000002.SZ": "银行"}
    zt = {}
    zt.update(_zt("600001.SH", [100.0] * 59))                # 不足 60 → 剔除
    zt.update(_zt("000002.SZ", [float(200 - i) for i in range(60)]))  # 跌
    out = ss.new_high_counts(smap, zt, window=60)
    assert out == {"银行": {"nh": 0, "base": 1}}


def test_new_high_counts_window_param():
    """window=5: 近5日(含当日)最高即算。"""
    smap = {"600001.SH": "银行"}
    closes = [100.0] * 60
    closes[-2] = 105.0   # 昨日高点, 今收 104 未破昨日但破前58日
    zt = _zt("600001.SH", closes)
    assert ss.new_high_counts(smap, zt, window=5)["银行"] == {"nh": 0, "base": 1}
    closes[-1] = 106.0   # 破近5日高点 → 新高
    zt = _zt("600001.SH", closes)
    assert ss.new_high_counts(smap, zt, window=5)["银行"] == {"nh": 1, "base": 1}


def test_new_high_counts_empty_and_missing():
    """空映射/缓存缺该股/行业无 base → 不崩, 无股行业不输出或输出 0。"""
    assert ss.new_high_counts({}, {}) == {}
    # 映射里的股在缓存缺失 → 行业 base=0, 输出 0(不崩)
    out = ss.new_high_counts({"600001.SH": "银行"}, {}, window=60)
    assert out["银行"] == {"nh": 0, "base": 0}


# ---------------- POS_CAP 常量(W1) ----------------

def test_pos_cap_values():
    """六阶段纸面仓位上限按 spec 照抄。"""
    assert ss.POS_CAP == {"孕育期": 30, "启动期": 50, "主升期": 75,
                          "高潮期": 50, "退潮期": 10, "休整期": 30}


# ---------------- sector_table 可选参扩展(W1) ----------------

def test_sector_table_extension_defaults_none():
    """缺省(不传新参) → new_high/etf 列 None; pos_cap/week_rank 为内在列
    恒输出(flat 行 → 休整/上限30); 存量字段不变(向后兼容)。"""
    n = 30
    mkt = _mk({"A": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["new_high"] is None
    assert a["etf"] is None
    assert a["pos_cap"] == 30          # 休整 → 休整期 30
    assert isinstance(a["week_rank"], int)
    assert set(a) == {"code", "name", "stage", "note", "r3", "r5", "r10",
                      "share5", "share20", "share_chg", "hits", "signals",
                      "new_high", "etf", "pos_cap", "week_rank"}


def test_sector_table_week_rank():
    """按 r5 降序名次; r5 缺失(K线≤window 根)不参与(rank=None)。"""
    n = 30
    up = [100.0] * 24 + [100.0, 102.0, 104.0, 106.0, 108.0, 112.0]
    mid = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"UP": (up, [1e8] * n), "MID": (mid, [1e8] * n)}, n=n)
    mkt["sector"]["BAD"] = {"dates": _dates(5), "close": [100.0] * 5,
                            "amount": [1e8] * 5, "name": "BAD"}
    rows = {r["code"]: r for r in ss.sector_table(mkt)}
    assert rows["UP"]["week_rank"] == 1
    assert rows["MID"]["week_rank"] == 2
    assert rows["BAD"]["r5"] is None          # 5 根 → r5 不可算
    assert rows["BAD"]["week_rank"] is None   # r5 缺失不参与


def _named_mkt(named, n=30):
    """{行业名: (closes, amounts)} → mkt 切片(名称与行业同名, 便于对齐断言)。"""
    ds = _dates(n)
    return {"sector": {name: {"dates": ds, "close": cl, "amount": am,
                              "name": name}
                       for name, (cl, am) in named.items()}}


def test_sector_table_new_high_and_etf_injected():
    """注入 new_high/etf_map/etf_quotes → 行按名称对齐; 无 ETF 行业 etf=None。"""
    n = 30
    flat = ([100.0] * n, [1e8] * n)
    mkt = _named_mkt({"银行": flat, "传媒": flat})
    nh = {"银行": {"nh": 3, "base": 40}}
    etf_map = {"银行": {"code": "512800.SH", "name": "银行ETF"},
               "传媒": {}}   # 留空 = 无锚点
    quotes = {"512800.SH": {"amount": 2.5e9, "pct_chg": 1.2}}
    rows = {r["code"]: r for r in ss.sector_table(
        mkt, new_high=nh, etf_map=etf_map, etf_quotes=quotes)}
    assert rows["银行"]["new_high"] == {"nh": 3, "base": 40}
    assert rows["银行"]["etf"] == {"code": "512800.SH", "name": "银行ETF",
                                   "amount": 2.5e9, "pct_chg": 1.2}
    assert rows["传媒"]["new_high"] is None      # 名字不在 nh 表 → None
    assert rows["传媒"]["etf"] is None           # 映射留空 → None


def test_sector_table_etf_quote_missing_fails_open():
    """映射有锚点但 quotes 缺该码 → code/name 保留, 数值 None(fail-open)。"""
    n = 30
    mkt = _named_mkt({"银行": ([100.0] * n, [1e8] * n)})
    etf_map = {"银行": {"code": "512800.SH", "name": "银行ETF"}}
    rows = {r["code"]: r for r in ss.sector_table(
        mkt, etf_map=etf_map, etf_quotes={})}
    assert rows["银行"]["etf"] == {"code": "512800.SH", "name": "银行ETF",
                                   "amount": None, "pct_chg": None}


def test_sector_table_pos_cap_by_stage():
    """阶段 → 纸面上限; "休整"标签归一到"休整期"; 数据不足 → None。"""
    n = 30
    up = [100.0] * 24 + [100.0, 102.0, 104.0, 106.0, 108.0, 112.0]
    mkt = _mk({"UP": (up, [1e8] * 20 + [4e8] * 10),
               "FLAT": ([100.0] * n, [1e8] * n),
               "BAD": ([100.0] * 10, [1e8] * 10)}, n=n)
    rows = {r["code"]: r for r in ss.sector_table(mkt)}
    assert rows["UP"]["stage"] == "高潮期"
    assert rows["UP"]["pos_cap"] == 50
    assert rows["FLAT"]["stage"] == "休整"
    assert rows["FLAT"]["pos_cap"] == 30
    assert rows["BAD"]["stage"] == "数据不足"
    assert rows["BAD"]["pos_cap"] is None
