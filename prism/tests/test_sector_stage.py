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
    assert set(a) == {"code", "name", "stage", "note", "r3", "r5", "r10",
                      "share5", "share20", "share_chg", "hits", "signals"}


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
