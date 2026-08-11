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

    def compute_market_factors(self, ds, ticks, limit_ups=None):
        return {"N1":{"score":1},"N2":{"score":1},"N3":{"score":1},
                "N4":{"score":1},"N5":{"score":1}}


class FakeStore:
    def merge(self, auto, code):
        merged = dict(auto)
        if code == "002859.SZ":
            merged["F7"] = 1
        return merged


class FakeScorer:
    def score_stock(self, factors):
        fb = sum(1 for n in ["F1","F2","F3","F4","F5","F6","F7"] if factors.get(n)==1)
        return {"first_board":fb,"monster":0,"momentum":0,"node":5,
                "composite":round(fb*0.3+5*0.15,2),
                "grade":"A" if fb>=6 else "E","strength":"极强","position":"75%"}

    def classify_market(self, node_score):
        return "回暖期"


def test_screen_returns_candidates_when_env_ok():
    r = ScreenRunner(ds=FakeDS(), engine=FakeEngine(),
                     store=FakeStore(), scorer=FakeScorer()).run()
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
        def compute_market_factors(self, ds, ticks, limit_ups=None):
            return {"N1":{"score":0},"N2":{"score":0},"N3":{"score":0},
                    "N4":{"score":0},"N5":{"score":0}}
    r = ScreenRunner(ds=FakeDS(), engine=BadEngine(),
                     store=FakeStore(), scorer=BadScorer()).run()
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
                 scorer=FakeScorer()).run()
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
                     store=FakeStore(), scorer=ModelScorer()).run()
    cand = r["candidates"][0]
    assert cand["factors"]["F1"] == 1            # 拍扁后是 int
    assert cand["scores"]["first_board"] == 7     # F1-F6自动 + F7手填
    assert cand["scores"]["node"] == 5            # 市场 N1-N5 注入
    assert cand["scores"]["grade"] == "A"         # 节点≥4 且 首板≥6
    assert cand["scores"]["strength"] == "极强"    # 节点5 且 任一选股模型≥6
