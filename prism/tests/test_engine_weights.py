# -*- coding: utf-8 -*-
"""评分侧按位因子权重(审查 I-2)测试 — 假注册表, 全离线。

语义: 字符串形态因子按位读模型 weights(缺失/长度不齐/脏值 → 回退 1.0);
dict 形态保留自带 weight; 全 1.0 权重路径与无 weights 键完全等价(零影响守卫)。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism.context import FactorContext
from prism import engine


@pytest.fixture(autouse=True)
def _factors():
    reg.reset()

    @reg.factor(id="A1", name="a1", category="通用", description="")
    def f_a1(ctx):
        return {"score": 1, "note": ""}

    @reg.factor(id="A2", name="a2", category="通用", description="")
    def f_a2(ctx):
        return {"score": 1, "note": ""}

    yield


def _strategy(models):
    return {"id": "t", "name": "t", "description": "",
            "market_gate": {"model": "node", "threshold": 0, "factors": []},
            "scoring_models": models,
            "composite": {"mode": "sum"},
            "filters": {"candidate_min_model": 1}}


def test_positional_weights_applied():
    """I-2: 加权生效 — factors=[A1,A2] weights=[2.0,1.0] 双命中 → 3.0 而非 2.0。"""
    ctx = FactorContext(code="600000.SH")
    s = _strategy([{"id": "m1", "name": "m", "weight": 1.0,
                    "factors": ["A1", "A2"], "weights": [2.0, 1.0]}])
    out, _ = engine._compute_scores(ctx, s)
    assert out["m1"] == 3.0


def test_weights_all_one_equals_no_weights_key():
    """零影响守卫: weights 全 1.0 与无 weights 键 → 模型分相同且等于命中数。"""
    ctx = FactorContext(code="600000.SH")
    base = {"id": "m1", "name": "m", "weight": 1.0, "factors": ["A1", "A2"]}
    o_one, f_one = engine._compute_scores(
        ctx, _strategy([dict(base, weights=[1.0, 1.0])]))
    o_none, f_none = engine._compute_scores(ctx, _strategy([dict(base)]))
    hits = sum(f_one.values())
    assert o_one["m1"] == o_none["m1"] == hits == 2
    assert f_one == f_none


def test_weights_short_falls_back_one():
    """长度不齐: weights 只覆盖前位, 缺位回退 1.0(与校验器对齐规则一致)。"""
    ctx = FactorContext(code="600000.SH")
    s = _strategy([{"id": "m1", "name": "m", "weight": 1.0,
                    "factors": ["A1", "A2"], "weights": [2.0]}])
    out, _ = engine._compute_scores(ctx, s)
    assert out["m1"] == 3.0      # 2.0 + 缺位 1.0


def test_dirty_weights_fall_back_one():
    """脏权重(非数值)→ 整体回退 1.0, 不崩(回测/实盘共用路径)。"""
    ctx = FactorContext(code="600000.SH")
    s = _strategy([{"id": "m1", "name": "m", "weight": 1.0,
                    "factors": ["A1", "A2"], "weights": ["x", None]}])
    out, _ = engine._compute_scores(ctx, s)
    assert out["m1"] == 2


def test_dict_form_keeps_own_weight():
    """dict 形态自带 weight 不被按位权重覆盖(既有形态语义不变)。"""
    ctx = FactorContext(code="600000.SH")
    s = _strategy([{"id": "m1", "name": "m", "weight": 1.0,
                    "factors": [{"id": "A1", "weight": 3.0}]}])
    out, _ = engine._compute_scores(ctx, s)
    assert out["m1"] == 3.0
