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


def test_F1_today_at_limit_no_prior_limitup_is_first_board():
    # 回归(复审发现): F1 扫描若误包含今日, 今日收盘==涨停价(11.55 = 10.5 的 +10%)
    # 会被判成"已有涨停" → F1 恒 0。今日在涨停价且近历史无涨停 → F1 必须为 1。
    closes = [10.0]*19 + [10.5, 11.55]
    kline = make_kline(closes, [100000]*21)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":11.55,"lastClose":11.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.55,"FloatVolume":1e8}   # 今日在涨停价
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 1
    assert "无涨停" in r["F1"]["note"]

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


def test_N3_real_data_bare_codes_resolve_suffix():
    ds = FakeDS()
    eng = FactorEngine()
    # 东财返回裸6位代码, 而 QMT ticks 键带交易所后缀 → 需按前缀补后缀才能命中
    ticks = {"000001.SZ": {"amount": 1e12, "lastPrice": 11.0, "lastClose": 10.0},
             "600519.SH": {"amount": 1e12, "lastPrice": 20.0, "lastClose": 19.0}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001", "600519", "999999"],  # 999999 无匹配 → 跳过
          "max_boards": 5}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[], em=em)
    assert m["N3"]["score"] == 1   # 000001→.SZ, 600519→.SH 均命中
    assert "东财真数据" in m["N3"]["note"]


def test_N3_real_data_no_match_keeps_zero():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": ["123456"], "max_boards": 5}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[], em=em)
    assert m["N3"]["score"] == 0      # 代码存在但无一命中 → 保持0, 不误走兜底
    assert "无匹配" in m["N3"]["note"]


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


def test_compute_factors_uses_injected_kline_and_index():
    # 传入 kline/index_kline 后, 不再回调 ds.get_kline/get_index_kline
    calls = {"kline": 0, "index": 0}
    class SpyDS(FakeDS):
        def get_kline(self, code, days=120):
            calls["kline"] += 1
            return super().get_kline(code, days)
        def get_index_kline(self, code, days=60):
            calls["index"] += 1
            return super().get_index_kline(code, days)
    kline = make_kline(list(np.linspace(10, 30, 300)), [100000]*300)
    idx = make_kline([3000]*60, [100000]*60)
    ds = SpyDS(kline_map={"000001.SZ": kline}, index_map={"000001.SH": idx})
    eng = FactorEngine()
    tick = {"lastPrice":30.0,"lastClose":29.0,"sealed":True,"amount":1e6,"volume":200000}
    detail = {"UpStopPrice":33.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={},
                            kline=kline, index_kline=idx)
    assert calls["kline"] == 0        # 注入后不再逐只拉
    assert calls["index"] == 0        # 注入后不再重复拉指数
    assert "F6" in r and "S4" in r    # 用注入数据仍算得出因子


# ---------- 修复回归: F1 只看近20日窗口 ----------

def test_F1_old_limitup_beyond_20_days_still_first_board():
    # 回归(修复): 原实现扫全部历史K线, 250天前某天涨停过 → 近20日首板被误判成"老涨停"。
    # 修复后 F1 只回看最近20个交易日, 250天前的涨停不影响今日首板。
    closes = [10.0]*100 + [11.0] + [10.0]*148 + [11.0]  # 索引100(第101天)有涨停, 今日=11.0
    kline = make_kline(closes, [100000]*250)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}   # 今日首板在涨停价
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 1
    assert "无涨停" in r["F1"]["note"]

def test_F1_limitup_within_20_days_not_first_board():
    # 反向: 近20日窗口内(如10天前)有涨停 → 今日涨停不算首板
    closes = [10.0]*240 + [11.0, 11.0] + [12.1]  # 倒数第3天(11.0=前一日10.0的+10%)涨停
    kline = make_kline(closes, [100000]*243)
    ds = FakeDS(kline_map={"000001.SZ": kline})
    eng = FactorEngine()
    tick = {"lastPrice":12.1,"lastClose":11.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":12.1,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 0
    assert "已有涨停" in r["F1"]["note"]


# ---------- 修复回归: F2 timetag 兼容 epoch 毫秒 ----------

def test_F2_string_timetag_early_seal():
    ds = FakeDS()
    eng = FactorEngine()
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "timetag":"20260811 09:30:00","amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F2"]["score"] == 1

def test_F2_string_timetag_late_seal():
    ds = FakeDS()
    eng = FactorEngine()
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "timetag":"20260811 14:05:06","amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F2"]["score"] == 0

def test_F2_epoch_ms_timetag_early_seal():
    # 回归(修复): 新版 xtquant timetag 是毫秒级 epoch int, 原实现按字符串 split 直接崩 → F2 恒 0。
    import datetime as _dt
    ds = FakeDS()
    eng = FactorEngine()
    ts_ms = int(_dt.datetime(2026, 8, 11, 9, 30).timestamp() * 1000)
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "timetag":ts_ms,"amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F2"]["score"] == 1

def test_F2_epoch_ms_timetag_late_seal():
    import datetime as _dt
    ds = FakeDS()
    eng = FactorEngine()
    ts_ms = int(_dt.datetime(2026, 8, 11, 14, 5).timestamp() * 1000)
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "timetag":ts_ms,"amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F2"]["score"] == 0

def test_F2_no_timetag_keeps_zero():
    ds = FakeDS()
    eng = FactorEngine()
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "amount":1e6,"volume":200000}   # 无 timetag
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F2"]["score"] == 0


# ---------- 修复回归: N2 情绪周期区分强度 ----------

def test_N2_many_limitups_warm():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": True} for i in range(55)]
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups)
    assert m["N2"]["score"] == 1   # >=50 直接暖

def test_N2_20_49_with_high_seal_ratio_warm():
    # 回归(修复): 原实现 20~49 家无条件给1; 修复后需封板率>60% 才判暖。
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": (i % 5 != 0)} for i in range(30)]
    # 封板 24/30 = 80% > 60% → 暖
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups)
    assert m["N2"]["score"] == 1

def test_N2_20_49_with_very_low_seal_ratio_cold():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": False} for i in range(30)]
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups)
    assert m["N2"]["score"] == 0   # 封板率 0% → 不判暖


# ---------- 修复回归: N4 昨日连板晋级率真数据 ----------

def test_N4_promotion_ratio_real_data_hit():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001", "000002", "000003"],
          "yesterday_boards": ["000001", "000002", "000003"],   # 昨日3只连板
          "max_boards": 4}                                       # 今日最高连板 <5
    # 今日涨停池含 000001.SZ 与 000002.SZ → 晋级 2/3 = 66.7% > 25% → N4=1
    limit_ups = [{"code": "000001.SZ"}, {"code": "000002.SZ"}]
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups, em=em)
    assert m["N4"]["score"] == 1
    assert "晋级率" in m["N4"]["note"]

def test_N4_promotion_ratio_real_data_miss():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001", "000002", "000003"],
          "yesterday_boards": ["000001", "000002", "000003"],
          "max_boards": 4}   # 最高连板<5 且 晋级率 0/3=0% ≤25% → N4=0
    limit_ups = [{"code": "600000.SH"}]   # 今日涨停不含昨日连板股
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups, em=em)
    assert m["N4"]["score"] == 0

def test_N4_high_boards_still_wins_with_low_ratio():
    # 最高连板>=5 优先满足 → 即使晋级率低也判1(规格"或"关系)
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001"],
          "yesterday_boards": ["000001"],
          "max_boards": 6}
    limit_ups = [{"code": "600000.SH"}]   # 晋级率 0%
    m = eng.compute_market_factors(ds, ticks, limit_ups=limit_ups, em=em)
    assert m["N4"]["score"] == 1
    assert "最高连板" in m["N4"]["note"]
