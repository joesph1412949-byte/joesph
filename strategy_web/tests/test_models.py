# -*- coding: utf-8 -*-
"""models 单元测试 — v2: 节点退出个股评级, 分级用绝对阈值"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import ModelScorer


def _F(fb, mo, mom):
    """构造三模型命中数, 其余因子0。F1-F7=fb个, Y1-Y7=mo个, S1-S7=mom个。"""
    factors = {}
    for i in range(1, 8):
        factors["F%d" % i] = 1 if i <= fb else 0
        factors["Y%d" % i] = 1 if i <= mo else 0
        factors["S%d" % i] = 1 if i <= mom else 0
    return factors


def test_grade_A_best_6():
    r = ModelScorer().score_stock(_F(6, 1, 1))
    assert r["grade"] == "A"
    assert r["strength"] == "极强"
    assert r["position"] == "仓位上限75%"


def test_grade_B_best5_second3():
    r = ModelScorer().score_stock(_F(5, 3, 1))
    assert r["grade"] == "B"
    assert r["strength"] == "强"


def test_grade_B_boundary_second2_is_C():
    # 最强5 但次强2 → 降为 C
    r = ModelScorer().score_stock(_F(5, 2, 1))
    assert r["grade"] == "C"


def test_grade_C_best4():
    r = ModelScorer().score_stock(_F(4, 2, 2))
    assert r["grade"] == "C"
    assert r["strength"] == "中等"


def test_grade_D_best3():
    r = ModelScorer().score_stock(_F(3, 2, 1))
    assert r["grade"] == "D"
    assert r["strength"] == "弱"


def test_grade_E_best2():
    r = ModelScorer().score_stock(_F(2, 1, 1))
    assert r["grade"] == "E"


def test_composite_no_node_weight():
    s = ModelScorer()
    # 全中(无N): 最强/次强/第三各 7 → 7*0.6 + 7*0.25 + 7*0.15 = 7.0; node 因子存在但不计权重
    factors = _F(7, 7, 7)
    factors.update({"N1": 1, "N2": 1, "N3": 1, "N4": 1, "N5": 1})
    r = s.score_stock(factors)
    assert r["first_board"] == 7 and r["node"] == 5   # node 仍统计
    assert abs(r["composite"] - 7.0) < 1e-6
    assert r["grade"] == "A"


def test_composite_best_weighted_emphasizes_strongest_model():
    # 回归: 综合分改为"突出最强模型"(best*0.6 + second*0.25 + third*0.15, 上限7.0)。
    # 目的: 一招鲜(6,2,2)必须排到均衡平庸(4,4,4)之前, 与等级/强度同源, 消除"打架"。
    s = ModelScorer()
    one_trick = s.score_stock(_F(6, 2, 2))
    balanced = s.score_stock(_F(4, 4, 4))
    assert abs(one_trick["composite"] - (6*0.6 + 2*0.25 + 2*0.15)) < 1e-6  # 4.40
    assert abs(balanced["composite"] - (4*0.6 + 4*0.25 + 4*0.15)) < 1e-6   # 4.00
    assert one_trick["composite"] > balanced["composite"]  # 区分度: 一招鲜排前
    assert one_trick["grade"] == "A" and balanced["grade"] == "C"  # 等级一致
    # 全三等分 = 3.0, 纯放大旧权重(0.85)会得 2.55, 新公式把下限也抬起来
    assert abs(s.score_stock(_F(3, 3, 3))["composite"] - 3.0) < 1e-6


def test_missing_factors_treated_as_zero():
    r = ModelScorer().score_stock({})
    assert r["first_board"] == 0
    assert r["grade"] == "E"
    assert r["strength"] == "弱"


def test_classify_market_unchanged():
    s = ModelScorer()
    assert s.classify_market(5) == "高潮期"
    assert s.classify_market(4) == "回暖期"
    assert s.classify_market(3) == "冰点期"
    assert s.classify_market(2) == "退潮期"
