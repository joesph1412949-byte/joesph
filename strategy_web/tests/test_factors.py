# -*- coding: utf-8 -*-
"""factors 单元测试 — 用构造的 K线/盘口 数据验证因子计算"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import pytest

from data_source import DataSource
from factors import FactorEngine


def make_kline(closes, volumes=None):
    """构造日线K线 DataFrame"""
    n = len(closes)
    volumes = volumes or np.full(n, 100000)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open": closes, "high": [c*1.02 for c in closes],
        "low": [c*0.98 for c in closes], "close": closes,
        "volume": volumes, "amount": [c*v*100 for c,v in zip(closes, volumes)],
    })


class FakeDS:
    """不连QMT的假 DataSource，只实现 factors 需要的两个方法"""
    def __init__(self, kline_map=None, index_map=None, sector_stocks=None):
        self.kline_map = kline_map or {}
        self.index_map = index_map or {}
        self.sector_stocks = sector_stocks or {}

    def get_kline(self, code, days=120):
        return self.kline_map.get(code, make_kline([20]*60, [100000]*60))

    def get_index_kline(self, code, days=60):
        return self.index_map.get(code, make_kline([3000]*60, [100000]*60))


def test_F1_first_board_requires_no_prior_limitup():
    kline = make_kline([10]*20 + [10.0], [100000]*21)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    # 近20日最高涨幅仅5%(无涨停), 今日涨停
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 1

def test_F1_prior_limitup_fails():
    # 近20日内有涨停(10→11), 则今日不算首板
    closes = [10]*19 + [11.0, 11.0]
    kline = make_kline(closes, [100000]*21)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":11.0,"lastClose":11.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":12.1,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 0

def test_F3_seal_strength_mainboard():
    ds = FakeDS()
    eng = FactorEngine()
    # 主板: 封单金额 = bidVol0×涨停价; 需 ≥ 流通市值×0.5%
    tick = {"lastPrice":10.0,"lastClose":10.0,"sealed":True,
            "bidVol":[1000000,0,0,0,0],"bidPrice":[11.0,0,0,0,0],
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}  # 流通市值=11×1e8=1.1e9
    # 封单额=1e6×11=1.1e7, 1.1e9×0.005=5.5e6 → 达标
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F3"]["score"] == 1

def test_F4_sector_resonance():
    ds = FakeDS()
    eng = FactorEngine()
    sector_map = {"000001.SZ": "SW2半导体", "000002.SZ": "SW2半导体",
                  "000003.SZ": "SW2半导体", "000004.SZ": "SW2半导体"}
    # 板块内涨停家数(通过limit_ups传)≥3
    tick = {"lastPrice":10.0,"lastClose":10.0,"sealed":True}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    limit_ups = [{"code":c} for c in ["000001.SZ","000002.SZ","000003.SZ"]]
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map,
                            limit_ups=limit_ups)
    assert r["F4"]["score"] == 1

def test_Y3_volume_spike():
    # 今日量 = 前5日均量×5 → 达标（前5日均量=10万, 今日=50万）
    kline = make_kline([10]*25, [100000]*24 + [500000]*1)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "amount":1e6,"volume":500000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["Y3"]["score"] == 1

def test_S4_ma_bullish():
    # 构造 60>120>250 且斜率向上: 价格长期上升
    closes = list(np.linspace(10, 30, 300))
    kline = make_kline(closes, [100000]*300)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":30.0,"lastClose":29.0,"sealed":True}
    detail = {"UpStopPrice":33.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["S4"]["score"] == 1

def test_N5_total_amount_threshold():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ":{"amount":1.5e12},"000002.SZ":{"amount":1.0e12}}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[])
    assert m["N5"]["score"] == 1  # 合计2.5万亿>2万亿

def test_N5_below_threshold():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ":{"amount":1.0e12},"000002.SZ":{"amount":0.5e12}}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[])
    assert m["N5"]["score"] == 0
