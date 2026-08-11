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
    # 终审修复: 原测试在"今日未涨停"分支通过, 从未真正命中"近历史有涨停→非首板"分支。
    # 重建: 今日在涨停价(12.1), 且近历史有一天(11.0) == 前一日(10.0)的+10%涨停价。
    closes = [10]*19 + [11.0, 12.1]
    kline = make_kline(closes, [100000]*21)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":12.1,"lastClose":11.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":12.1,"FloatVolume":1e8}   # 今日在涨停价
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 0
    assert "已有涨停" in r["F1"]["note"]   # 走的是"近历史已有涨停"分支, 而非"今日未涨停"

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

def test_N4_high_chain_no_crash():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    # 30+ 涨停且封板率>60% → N4=1, 且不抛异常 (em=None → 代理兜底分支)
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": True} for i in range(35)]
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups)
    assert m["N4"]["score"] == 1
    assert "60%" in m["N4"]["note"]


# ---------- N1/N3/N4 东财真数据分支 + 代理兜底分支 ----------

def test_N1_real_data_above_mean():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": [], "max_boards": 5}
    # 今日涨停 40 > 近5日均值 25 → N1=1
    limit_ups = [{"code": "000001.SZ"}] * 40
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups, em=em)
    assert m["N1"]["score"] == 1
    assert "东财真数据" in m["N1"]["note"]


def test_N1_real_data_below_mean():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [50, 60, 55, 40, 45], "yesterday_codes": [], "max_boards": 5}  # 均值50
    limit_ups = [{"code": "000001.SZ"}] * 30
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups, em=em)
    assert m["N1"]["score"] == 0


def test_N1_fallback_flat_index():
    ds = FakeDS()  # 880368 为平线 3000 → 不高于自身5日均值 → N1=0
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[{"code": "000001.SZ"}], em=None)
    assert m["N1"]["score"] == 0
    assert "兜底" in m["N1"]["note"]


def test_N1_fallback_no_index_data():
    ds = FakeDS(index_map={"880368.SH": None})  # 880368 无数据 → 今日有涨停即1
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[{"code": "000001.SZ"}], em=None)
    assert m["N1"]["score"] == 1


def test_N3_real_data_positive():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12, "lastPrice": 11.0, "lastClose": 10.0},
             "000002.SZ": {"amount": 1e12, "lastPrice": 10.5, "lastClose": 10.0}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001.SZ", "000002.SZ", "000003.SZ"],  # 缺 000003 应被跳过
          "max_boards": 5}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[], em=em)
    assert m["N3"]["score"] == 1
    assert "东财真数据" in m["N3"]["note"]


def test_N3_real_data_negative():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12, "lastPrice": 9.0, "lastClose": 10.0}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": ["000001.SZ"], "max_boards": 5}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[], em=em)
    assert m["N3"]["score"] == 0


def test_N3_fallback_today_pool():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "000001.SZ", "last": 11.0, "last_close": 10.0}]
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups, em=None)
    assert m["N3"]["score"] == 1
    assert "兜底" in m["N3"]["note"]


def test_N4_real_data_high_board():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": [], "max_boards": 5}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[], em=em)
    assert m["N4"]["score"] == 1
    assert "东财真数据" in m["N4"]["note"]


def test_N4_real_data_low_board():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": [], "max_boards": 4}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[], em=em)
    assert m["N4"]["score"] == 0
