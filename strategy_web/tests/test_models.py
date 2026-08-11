# -*- coding: utf-8 -*-
"""models 单元测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import ModelScorer


def test_A_class_requires_node4_and_firstboard6():
    s = ModelScorer()
    # 首板全中7分, 节点4分, 妖股/势能低
    factors = {"F1":1,"F2":1,"F3":1,"F4":1,"F5":1,"F6":1,"F7":1,
               "Y1":0,"Y2":0,"Y3":0,"Y4":0,"Y5":0,"Y6":0,"Y7":0,
               "S1":0,"S2":0,"S3":0,"S4":0,"S5":0,"S6":0,"S7":0,
               "N1":1,"N2":1,"N3":1,"N4":1,"N5":0}
    r = s.score_stock(factors)
    assert r["first_board"] == 7
    assert r["node"] == 4
    assert r["grade"] == "A"
    assert r["strength"] == "强"  # 节点4+首板7, 但节点≠5 → 强? 需按规则
    # 强弱: 节点≥4 且 首板≥5 → "强"; 综合分 = 7*.3+0+.0+4*.15=2.7


def test_E_class_low_scores():
    s = ModelScorer()
    factors = {"F1":0,"F2":0,"F3":0,"F4":0,"F5":0,"F6":0,"F7":0,
               "Y1":0,"Y2":0,"Y3":0,"Y4":0,"Y5":0,"Y6":0,"Y7":0,
               "S1":0,"S2":0,"S3":0,"S4":0,"S5":0,"S6":0,"S7":0,
               "N1":0,"N2":0,"N3":0,"N4":0,"N5":0}
    r = s.score_stock(factors)
    assert r["grade"] == "E"
    assert r["strength"] == "弱"


def test_composite_score_weights():
    s = ModelScorer()
    # 全部命中: 首板7 妖股7 势能7 节点5 → 综合分 = 7*.3+7*.3+7*.25+5*.15
    factors = {"F1":1,"F2":1,"F3":1,"F4":1,"F5":1,"F6":1,"F7":1,
               "Y1":1,"Y2":1,"Y3":1,"Y4":1,"Y5":1,"Y6":1,"Y7":1,
               "S1":1,"S2":1,"S3":1,"S4":1,"S5":1,"S6":1,"S7":1,
               "N1":1,"N2":1,"N3":1,"N4":1,"N5":1}
    r = s.score_stock(factors)
    assert abs(r["composite"] - (7*0.30 + 7*0.30 + 7*0.25 + 5*0.15)) < 1e-6


def test_missing_factors_treated_as_zero():
    s = ModelScorer()
    r = s.score_stock({})   # 空因子
    assert r["first_board"] == 0
    assert r["node"] == 0
    assert r["grade"] == "E"


def test_classify_market():
    s = ModelScorer()
    assert s.classify_market(5) == "高潮期"
    assert s.classify_market(4) == "回暖期"
    assert s.classify_market(3) == "冰点期"
    assert s.classify_market(2) == "退潮期"


def test_strength_extreme_strong():
    s = ModelScorer()
    # 极强: nd==5 且 任一选股模型≥6 → 首板6 + 节点5
    factors = {"F1":1,"F2":1,"F3":1,"F4":1,"F5":1,"F6":1,"F7":0,
               "Y1":0,"Y2":0,"Y3":0,"Y4":0,"Y5":0,"Y6":0,"Y7":0,
               "S1":0,"S2":0,"S3":0,"S4":0,"S5":0,"S6":0,"S7":0,
               "N1":1,"N2":1,"N3":1,"N4":1,"N5":1}
    r = s.score_stock(factors)
    assert r["strength"] == "极强"
    assert r["position"] == "仓位上限75%"


def test_strength_medium():
    s = ModelScorer()
    # 中等: nd==3 且 任一选股模型≥5 (非极强/非强: 节点<4 且 ≠5) → 首板5 + 节点3
    factors = {"F1":1,"F2":1,"F3":1,"F4":1,"F5":1,"F6":0,"F7":0,
               "Y1":0,"Y2":0,"Y3":0,"Y4":0,"Y5":0,"Y6":0,"Y7":0,
               "S1":0,"S2":0,"S3":0,"S4":0,"S5":0,"S6":0,"S7":0,
               "N1":1,"N2":1,"N3":1,"N4":0,"N5":0}
    r = s.score_stock(factors)
    assert r["strength"] == "中等"
    assert r["position"] == "仓位上限30%"
