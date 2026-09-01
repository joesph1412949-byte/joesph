# -*- coding: utf-8 -*-
"""futures 数据层测试: COMMODITY_MAP 结构 / fetch 缓存写入 / _slice_mkt 切片。全离线。"""
from datetime import date
from unittest import mock

from prism import market_data as md


def _fake_df(rows):
    import pandas as pd
    return pd.DataFrame(rows)


def test_commodity_by_name_covers_core_sectors():
    # 核心周期行业必须有映射, 每个行业至少 1 个品种(名称驱动, 规避申万版本差异)
    for name in ("基础化工", "煤炭", "钢铁", "有色金属", "石油石化"):
        assert name in md.COMMODITY_BY_NAME, name
        assert md.COMMODITY_BY_NAME[name]


def test_futures_snapshot_keys_follow_sector_cache(monkeypatch):
    # futures 快照键 = 缓存 sectors 段的板块代码(与 sector_map/mkt["sector"] 同源)
    monkeypatch.setattr(md, "_load_cache", lambda: {
        "sectors": {"801030": {"name": "基础化工"},
                    "801950": {"name": "煤炭"},
                    "801150": {"name": "医药生物"}}})
    monkeypatch.setattr(md, "fetch_futures", lambda force=False: {
        "MA0": {"name": "甲醇", "dates": ["2026-07-01"], "close": [1.0]},
        "JM0": {"name": "焦煤", "dates": ["2026-07-01"], "close": [2.0]}})
    out = md.futures_snapshot()
    assert set(out) == {"801030", "801950"}      # 医药生物无映射 → 不出现
    assert "MA0" in out["801030"]["commodities"]
    assert "JM0" in out["801950"]["commodities"]


def test_commodity_symbols_all_in_futures_names():
    # COMMODITY_BY_NAME 引用的每个品种必须在 _FUTURES_NAMES 有定义,
    # 否则 fetch_futures 不会采集它, 快照里该品种静默缺失(Task 1: 工业硅=SI0,
    # PS0 实为多晶硅; brief 名表误写 PS0 → SI0 永不采集)。
    for syms in md.COMMODITY_BY_NAME.values():
        for sym in syms:
            assert sym in md._FUTURES_NAMES, sym


def test_fetch_futures_writes_cache(monkeypatch):
    # fixture 须 ≥60 行: fetch_futures 对 <60 行的品种按死数据跳过
    # (Task 1 探针结论, brief 实现块的 len(df) < 60 守卫)
    dates = (["2026-04-%02d" % (i + 1) for i in range(30)]
             + ["2026-05-%02d" % (i + 1) for i in range(30)])
    closes = [2400.0 + i for i in range(60)]
    df = _fake_df({"date": dates, "close": closes})
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: df)
    monkeypatch.setattr(md, "_load_cache", lambda: {})
    monkeypatch.setattr(md, "_save_cache", lambda c: None)
    out = md.fetch_futures()
    assert out["MA0"]["close"] == closes
    assert out["MA0"]["dates"] == dates


def test_fetch_futures_skips_failed_symbol(monkeypatch):
    def boom(sym):
        raise RuntimeError("net down")
    monkeypatch.setattr(md, "_fetch_futures_daily", boom)
    monkeypatch.setattr(md, "_load_cache", lambda: {})
    monkeypatch.setattr(md, "_save_cache", lambda c: None)
    out = md.fetch_futures()
    assert "MA0" not in out  # fail-open: 失败品种跳过


def test_slice_mkt_slices_futures():
    from prism.backtest import _slice_mkt
    mkt = {"futures": {"801722": {"name": "基础化工", "commodities": {
        "MA0": {"name": "甲醇", "dates": ["2026-07-01", "2026-07-02",
                             "2026-07-03"],
                "close": [1.0, 2.0, 3.0]}}}}}
    out = _slice_mkt(mkt, date(2026, 7, 2))
    ma = out["futures"]["801722"]["commodities"]["MA0"]
    assert ma["dates"] == ["2026-07-01", "2026-07-02"]
    assert ma["close"] == [1.0, 2.0]
