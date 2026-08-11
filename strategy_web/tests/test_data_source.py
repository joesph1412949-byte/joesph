# -*- coding: utf-8 -*-
"""data_source 单元测试 — 用 mock 代替真实 xtdata"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# --- mock xtdata 模块 ---
import types
mock_xt = types.ModuleType("xtquant")
xtdata_mod = types.ModuleType("xtquant.xtdata")

_dummy_kline_df = None

def _make_kline_df(n=120):
    import pandas as pd
    import numpy as np
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "time": dates,
        "open": np.linspace(10, 20, n),
        "high": np.linspace(10.5, 21, n),
        "low": np.linspace(9.5, 19, n),
        "close": np.linspace(10, 20, n),
        "volume": np.full(n, 100000),
        "amount": np.full(n, 1e6),
    })

def _fake_connect():
    return None

def _fake_get_full_tick(codes):
    return {
        "002859.SZ": {
            "lastPrice": 81.32, "lastClose": 73.93, "askPrice": [0,0,0,0,0],
            "bidPrice": [81.32, 81.31, 81.30, 81.29, 81.28],
            "bidVol": [34661, 77, 47, 6, 9], "amount": 1424321000.0,
            "volume": 181657, "high": 81.32, "low": 73.18, "open": 73.93,
            "time": 1786428306000, "timetag": "20260811 14:05:06",
        },
        "600353.SH": {
            "lastPrice": 30.03, "lastClose": 27.30, "askPrice": [0,0,0,0,0],
            "bidPrice": [30.03, 30.02, 30.01, 30.00, 29.99],
            "bidVol": [1000, 500, 300, 200, 100], "amount": 500000000.0,
            "volume": 80000, "high": 30.03, "low": 27.50, "open": 27.50,
            "time": 1786428306000, "timetag": "20260811 14:05:06",
        },
        "000001.SZ": {  # 非涨停股对照
            "lastPrice": 10.0, "lastClose": 10.0, "askPrice": [10.01,10.02,0,0,0],
            "bidPrice": [9.99,9.98,0,0,0], "bidVol": [100,200,0,0,0],
            "amount": 10000000.0, "volume": 5000, "high": 10.05, "low": 9.95,
            "open": 10.0, "time": 1786428306000, "timetag": "20260811 14:05:06",
        },
    }

def _fake_get_instrument_detail_list(codes):
    out = {}
    for c in codes:
        if c == "002859.SZ":
            out[c] = {"InstrumentID":"002859","InstrumentName":"洁美科技",
                      "UpStopPrice":81.32,"FloatVolume":428315200.0,
                      "OpenDate":"20170407","ExchangeID":"SZ"}
        elif c == "600353.SH":
            out[c] = {"InstrumentID":"600353","InstrumentName":"旭光电子",
                      "UpStopPrice":30.03,"FloatVolume":1000000000.0,
                      "OpenDate":"19900101","ExchangeID":"SH"}
        else:
            out[c] = {"InstrumentID":"000001","InstrumentName":"平安银行",
                      "UpStopPrice":11.0,"FloatVolume":20000000000.0,
                      "OpenDate":"19910101","ExchangeID":"SZ"}
    return out

xtdata_mod.connect = _fake_connect
xtdata_mod.get_full_tick = _fake_get_full_tick
xtdata_mod.get_instrument_detail_list = _fake_get_instrument_detail_list
xtdata_mod.get_instrument_detail = lambda c: (_fake_get_instrument_detail_list([c]).get(c))
xtdata_mod.download_history_data = lambda *a, **k: None
xtdata_mod.get_market_data_ex = lambda *a, **k: {"002859.SZ": _make_kline_df(),
                                                  "600353.SH": _make_kline_df(),
                                                  "000001.SZ": _make_kline_df(),
                                                  "000001.SH": _make_kline_df()}
xtdata_mod.get_stock_list_in_sector = lambda s: ["002859.SZ","600353.SH","000001.SZ"]
xtdata_mod.connect_result = None
sys.modules["xtquant"] = mock_xt
xtdata_mod.__name__ = "xtquant.xtdata"
mock_xt.xtdata = xtdata_mod
sys.modules["xtquant.xtdata"] = xtdata_mod

from data_source import DataSource, DataSourceError

@pytest.fixture
def ds():
    d = DataSource()
    d._connected = True  # 跳过真实 connect
    return d

def test_get_full_market_ticks(ds):
    ticks = ds.get_full_market_ticks()
    assert "002859.SZ" in ticks
    assert ticks["002859.SZ"]["lastPrice"] == 81.32

def test_get_limit_up_stocks_detects_ups():
    ds = DataSource()
    ticks = {
        "002859.SZ": {"lastPrice":81.32,"lastClose":73.93},
        "600353.SH": {"lastPrice":30.03,"lastClose":27.30},
        "000001.SZ": {"lastPrice":10.0,"lastClose":10.0},
    }
    ds.get_instruments_bulk = lambda codes: {
        "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技","FloatVolume":428315200.0},
        "600353.SH": {"UpStopPrice":30.03,"InstrumentName":"旭光电子","FloatVolume":1e9},
        "000001.SZ": {"UpStopPrice":11.0,"InstrumentName":"平安银行","FloatVolume":2e10},
    }
    ups = ds.get_limit_up_stocks(ticks)
    codes = [u["code"] for u in ups]
    assert "002859.SZ" in codes and "600353.SH" in codes
    assert "000001.SZ" not in codes  # 未涨停

def test_get_limit_up_marks_sealed():
    ds = DataSource()
    ticks = {"002859.SZ": {"lastPrice":81.32,"lastClose":73.93,
                           "askPrice":[0,0,0,0,0],"bidPrice":[81.32,81.31,0,0,0]}}
    ds.get_instruments_bulk = lambda codes: {
        "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技","FloatVolume":428315200.0}}
    ups = ds.get_limit_up_stocks(ticks)
    assert ups[0]["sealed"] is True  # askPrice[0]==0 → 封板

def test_get_limit_up_not_sealed_when_ask_has_price():
    ds = DataSource()
    ticks = {"002859.SZ": {"lastPrice":81.32,"lastClose":73.93,
                           "askPrice":[10.05,0,0,0,0],"bidPrice":[81.32,81.31,0,0,0]}}
    ds.get_instruments_bulk = lambda codes: {
        "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技","FloatVolume":428315200.0}}
    ups = ds.get_limit_up_stocks(ticks)
    assert ups[0]["sealed"] is False  # askPrice[0] > 0 → 未封板

def test_get_kline_downloads_then_reads(ds):
    df = ds.get_kline("002859.SZ", days=120)
    assert "close" in df.columns and "volume" in df.columns
    assert len(df) > 0

def test_get_instruments_bulk(ds):
    details = ds.get_instruments_bulk(["002859.SZ", "000001.SZ"])
    assert details["002859.SZ"]["InstrumentName"] == "洁美科技"
    assert details["002859.SZ"]["FloatVolume"] == 428315200.0

def test_get_instruments_bulk_takes_batch_path(monkeypatch):
    # 真实 API 返回 {code: detail}，key 是带后缀的完整代码。
    # 验证批量分支真正生效（bulk 被调用、fallback 不触发），而非逐只回退。
    bulk_calls = {"n": 0}
    fallback_calls = {"n": 0}
    def spy_bulk(codes):
        bulk_calls["n"] += 1
        return _fake_get_instrument_detail_list(codes)
    def spy_fallback(c):
        fallback_calls["n"] += 1
        return {"InstrumentID": "000001", "InstrumentName": "fallback"}
    monkeypatch.setattr(xtdata_mod, "get_instrument_detail_list", spy_bulk)
    monkeypatch.setattr(xtdata_mod, "get_instrument_detail", spy_fallback)
    ds = DataSource()
    details = ds.get_instruments_bulk(["002859.SZ", "600353.SH"])
    assert details["002859.SZ"]["InstrumentName"] == "洁美科技"
    assert details["600353.SH"]["InstrumentName"] == "旭光电子"
    assert bulk_calls["n"] == 1
    assert fallback_calls["n"] == 0  # 全部命中批量路径，无需逐只 fallback

def test_get_sector_stocks(ds):
    codes = ds.get_sector_stocks("沪深A股")
    assert "002859.SZ" in codes

def test_get_index_kline(ds):
    df = ds.get_index_kline("000001.SH")
    assert "close" in df.columns
