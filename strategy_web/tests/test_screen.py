# -*- coding: utf-8 -*-
"""screen 单元测试 — 用假 DataSource/引擎 验证编排逻辑"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from screen import ScreenRunner


class FakeDS:
    def __init__(self):
        self.ticks = {
            "002859.SZ": {"lastPrice":81.32,"lastClose":73.93,"askPrice":[0,0,0,0,0],
                          "bidPrice":[81.32,0,0,0,0],"bidVol":[34661,0,0,0,0],
                          "amount":1.4e9,"volume":181657},
            "000001.SZ": {"lastPrice":10.0,"lastClose":10.0,"askPrice":[10.01,0,0,0,0],
                          "bidPrice":[9.99,0,0,0,0],"bidVol":[100,0,0,0,0],
                          "amount":1e7,"volume":5000},
        }
        self.details = {
            "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技",
                          "FloatVolume":428315200.0,"OpenDate":"20170407"},
            "000001.SZ": {"UpStopPrice":11.0,"InstrumentName":"平安银行",
                          "FloatVolume":2e10,"OpenDate":"19910101"},
        }

    def connect(self):
        pass

    def get_full_market_ticks(self, codes=None):
        return dict(self.ticks)

    def get_limit_up_stocks(self, ticks=None):
        return [{"code":"002859.SZ","name":"洁美科技","last":81.32,"last_close":73.93,
                 "up_stop_price":81.32,"sealed":True,"amount":1.4e9,"volume":181657,
                 "float_volume":428315200.0,"open_date":"20170407"}]

    def get_kline(self, code, days=120):
        import pandas as pd
        import numpy as np
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=days, freq="B"),
                             "open": np.linspace(10,20,days), "high": np.linspace(10,21,days),
                             "low": np.linspace(9,19,days), "close": np.linspace(10,20,days),
                             "volume": np.full(days,100000), "amount": np.full(days,1e6)})

    def get_instruments_bulk(self, codes):
        return {c: self.details[c] for c in codes if c in self.details}

    def get_index_kline(self, code, days=60):
        import pandas as pd
        import numpy as np
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=days, freq="B"),
                             "close": np.linspace(3000,3200,days),
                             "volume": np.full(days,100000)})

    def get_sector_stocks(self, sector):
        return list(self.ticks.keys()) if sector == "沪深A股" else []


class FakeEngine:
    def compute_factors(self, *a, **k):
        return {"F1":1,"F2":1,"F3":1,"F4":0,"F5":1,"F6":1,
                "Y3":1,"Y4":1,"S2":1,"S3":1,"S4":1,"S6":0}

    def compute_market_factors(self, ds, ticks, limit_ups=None, em=None):
        return {"N1":{"score":1},"N2":{"score":1},"N3":{"score":1},
                "N4":{"score":1},"N5":{"score":1}}


class FakeEastMoneyFeed:
    """假东财涨停池: 返回固定 em dict 或抛异常, 绝不发网络请求。"""
    def __init__(self, stats=None, exc=None):
        self.stats = stats
        self.exc = exc

    def get_market_stats(self):
        if self.exc is not None:
            raise self.exc
        return self.stats


DEFAULT_EM = {"daily_counts": [20, 22, 18, 25, 30],
              "yesterday_codes": ["002859.SZ"],
              "max_boards": 6}


class FakeStore:
    def merge(self, auto, code):
        merged = dict(auto)
        if code == "002859.SZ":
            merged["F7"] = 1
        return merged


class FakeScorer:
    def score_stock(self, factors):
        fb = sum(1 for n in ["F1","F2","F3","F4","F5","F6","F7"] if factors.get(n)==1)
        mo = sum(1 for n in ["Y1","Y2","Y3","Y4","Y5","Y6","Y7"] if factors.get(n)==1)
        best = max(fb, mo)
        grade = "A" if best >= 6 else ("B" if best >= 5 else ("C" if best >= 4 else "E"))
        return {"first_board":fb,"monster":mo,"momentum":0,"node":0,
                "composite":round(fb*0.3+mo*0.3,2),
                "grade":grade,"strength":"强" if best>=5 else "弱","position":"50%"}

    def classify_market(self, node_score):
        return "回暖期"


def test_screen_returns_candidates_when_env_ok():
    r = ScreenRunner(ds=FakeDS(), engine=FakeEngine(),
                     store=FakeStore(), scorer=FakeScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    assert r["environment_ok"] is True
    assert len(r["candidates"]) == 1
    assert r["candidates"][0]["code"] == "002859.SZ"
    assert r["candidates"][0]["scores"]["grade"] == "A"
    # 手填因子已合并
    assert r["candidates"][0]["factors"]["F7"] == 1


def test_screen_blocks_when_env_bad():
    class BadScorer(FakeScorer):
        def classify_market(self, node_score):
            return "退潮期"
    # 环境不达标 → 返回 environment_ok=False, 无候选
    class BadEngine(FakeEngine):
        def compute_market_factors(self, ds, ticks, limit_ups=None, em=None):
            return {"N1":{"score":0},"N2":{"score":0},"N3":{"score":0},
                    "N4":{"score":0},"N5":{"score":0}}
    r = ScreenRunner(ds=FakeDS(), engine=BadEngine(),
                     store=FakeStore(), scorer=BadScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    assert r["environment_ok"] is False
    assert r["candidates"] == []


def test_screen_passes_real_tick_and_detail_to_engine():
    class SpyEngine(FakeEngine):
        def __init__(self):
            self.calls = []
        def compute_factors(self, code, tick, detail, ds, sector_map,
                            limit_ups=None, market=None):
            self.calls.append((code, tick, detail))
            return FakeEngine.compute_factors(self, code, tick, detail, ds,
                                              sector_map, limit_ups, market)
    eng = SpyEngine()
    ScreenRunner(ds=FakeDS(), engine=eng, store=FakeStore(),
                 scorer=FakeScorer(), em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                 fund_feed=FakeFundFeed()).run()
    assert len(eng.calls) == 1
    code, tick, detail = eng.calls[0]
    assert code == "002859.SZ"
    assert tick.get("lastPrice") == 81.32             # 真实盘口tick
    assert detail.get("UpStopPrice") == 81.32         # 涨停价
    assert detail.get("FloatVolume") == 428315200.0   # 流通股本


def test_screen_pipeline_real_shapes_produce_scores():
    from models import ModelScorer
    class RealShapeEngine(FakeEngine):
        def compute_factors(self, *a, **k):
            # 真实形状: dict 套 dict
            return {f: {"score": 1, "note": "test"} for f in
                    ["F1", "F2", "F3", "F4", "F5", "F6",
                     "Y3", "Y4", "S2", "S3", "S4", "S6"]}
    r = ScreenRunner(ds=FakeDS(), engine=RealShapeEngine(),
                     store=FakeStore(), scorer=ModelScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    cand = r["candidates"][0]
    assert cand["factors"]["F1"] == 1
    assert "N1" not in cand["factors"]          # 节点不再注入个股 (v2)
    assert cand["scores"]["node"] == 0
    assert cand["scores"]["first_board"] == 7    # F1-F6自动 + F7手填
    assert cand["scores"]["grade"] == "A"        # 绝对阈值: 最强≥6


def test_screen_filters_by_candidate_min_model():
    from models import ModelScorer
    class LowScoringStore(FakeStore):
        def merge(self, auto, code):
            return {f: 1 if f in ["F1", "F2"] else 0 for f in
                    ["F1","F2","F3","F4","F5","F6","F7",
                     "Y1","Y2","Y3","Y4","Y5","Y6","Y7",
                     "S1","S2","S3","S4","S5","S6","S7"]}   # 最强模型=2 < 门槛3
    r = ScreenRunner(ds=FakeDS(), engine=FakeEngine(), store=LowScoringStore(),
                     scorer=ModelScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    assert r["candidates"] == []                 # 全部被门槛过滤掉


def test_screen_passes_em_stats_to_engine():
    class SpyEngine(FakeEngine):
        def __init__(self):
            self.calls = []
        def compute_market_factors(self, ds, ticks, limit_ups=None, em=None):
            self.calls.append((limit_ups, em))
            return FakeEngine.compute_market_factors(self, ds, ticks, limit_ups, em)
    em_stats = {"daily_counts": [10, 12, 8, 15, 20],
                "yesterday_codes": ["002859.SZ"],
                "max_boards": 6}
    eng = SpyEngine()
    ScreenRunner(ds=FakeDS(), engine=eng, store=FakeStore(),
                 scorer=FakeScorer(),
                 em_feed=FakeEastMoneyFeed(stats=em_stats),
                 fund_feed=FakeFundFeed()).run()
    limit_ups, em = eng.calls[0]
    assert em == em_stats
    assert limit_ups[0]["code"] == "002859.SZ"


def test_screen_survives_em_feed_failure():
    # 东财不可达(get_market_stats 抛异常) → em=None, 仍走代理兜底, 不崩溃
    class SpyEngine(FakeEngine):
        def __init__(self):
            self.calls = []
        def compute_market_factors(self, ds, ticks, limit_ups=None, em=None):
            self.calls.append((limit_ups, em))
            return FakeEngine.compute_market_factors(self, ds, ticks, limit_ups, em)
    eng = SpyEngine()
    r = ScreenRunner(ds=FakeDS(), engine=eng, store=FakeStore(),
                     scorer=FakeScorer(),
                     em_feed=FakeEastMoneyFeed(exc=RuntimeError("eastmoney down")),
                     fund_feed=FakeFundFeed()).run()
    assert r["environment_ok"] is True
    assert r["candidates"][0]["code"] == "002859.SZ"
    assert eng.calls[0][1] is None


def test_screen_sorts_candidates_by_composite_desc():
    from models import ModelScorer

    class TwoStockDS(FakeDS):
        def get_limit_up_stocks(self, ticks=None):
            # 故意乱序: 000001(综合分低)在前, 002859(综合分高, F7手填)在后
            return [
                {"code": "000001.SZ", "name": "平安银行", "last": 10.0, "last_close": 10.0,
                 "up_stop_price": 11.0, "sealed": True, "amount": 1e7, "volume": 5000,
                 "float_volume": 2e10, "open_date": "19910101"},
                {"code": "002859.SZ", "name": "洁美科技", "last": 81.32, "last_close": 73.93,
                 "up_stop_price": 81.32, "sealed": True, "amount": 1.4e9, "volume": 181657,
                 "float_volume": 428315200.0, "open_date": "20170407"},
            ]

    r = ScreenRunner(ds=TwoStockDS(), engine=FakeEngine(), store=FakeStore(),
                     scorer=ModelScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    codes = [c["code"] for c in r["candidates"]]
    assert codes == ["002859.SZ", "000001.SZ"]          # 综合分高者在前
    comps = [c["scores"]["composite"] for c in r["candidates"]]
    assert comps == sorted(comps, reverse=True)


class FakeFundFeed:
    def __init__(self, factors=None):
        self.factors = factors or {"Y1": {"score": 1, "note": "test"},
                                   "Y5": {"score": 0, "note": "test"}}
    def compute_for_stock(self, code, float_mv=None):
        return dict(self.factors)


def test_screen_merges_fundamental_and_marks_source():
    from models import ModelScorer
    r = ScreenRunner(ds=FakeDS(), engine=FakeEngine(), store=FakeStore(),
                     scorer=ModelScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    cand = r["candidates"][0]
    assert cand["factors"]["Y1"] == 1              # 东财因子已合并
    assert cand["auto_manual"]["Y1"] == "fundamental"
    assert cand["auto_manual"]["F1"] == "auto"     # QMT 因子
    assert cand["auto_manual"]["F7"] == "manual"   # 手填因子
