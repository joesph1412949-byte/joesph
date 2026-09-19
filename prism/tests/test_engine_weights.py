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


def _strategy(models, mode="sum", **comp):
    return {"id": "t", "name": "t", "description": "",
            "market_gate": {"model": "node", "threshold": 0, "factors": []},
            "scoring_models": models,
            "composite": dict({"mode": mode}, **comp),
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


# ---------------- 层权重接线: composite.mode = "weighted_sum" (2026-09-19) ------
# 语义: Σ(scoring_models[i].weight × model_scores[i]) 再 min(..., cap)。
# 权重按**声明序(名字)**对齐 —— 与 top3_weighted 的"按分数排名"是两种语义,
# 故另开一个模式, 不塞进 average。

def _models3():
    """三层夹具: 模型分 1 / 2 / 0, 权重 0.60 / 0.25 / 0.15(声明序≠分序)。"""
    return [{"id": "m1", "name": "首板", "weight": 0.60, "factors": ["A1"]},
            {"id": "m2", "name": "妖股", "weight": 0.25,
             "factors": ["A1", "A2"]},
            {"id": "m3", "name": "势能", "weight": 0.15, "factors": []}]


def _composite(mode, **comp):
    out, _ = engine._compute_scores(
        FactorContext(code="600000.SH"), _strategy(_models3(), mode=mode, **comp))
    return out["composite"]


def test_weighted_sum_uses_declared_layer_weights():
    """0.60×1 + 0.25×2 + 0.15×0 = 1.1 —— 同一夹具下 average=1.0、
    top3_weighted(按分排序)=1.45, 三种语义互不相等(防语义纠缠)。"""
    assert abs(_composite("weighted_sum", cap=9.0) - 1.1) < 1e-9
    assert abs(_composite("average") - 1.0) < 1e-9
    assert abs(_composite("top3_weighted", weights=[0.60, 0.25, 0.15],
                          cap=7.0) - 1.45) < 1e-9


def test_weighted_sum_clamped_by_declared_cap():
    """cap 是声明上限, 越界才收敛(cap 本身不自动改写)。"""
    assert abs(_composite("weighted_sum", cap=1.05) - 1.05) < 1e-9
    assert abs(_composite("weighted_sum", cap=9.0) - 1.1) < 1e-9
    # 缺 cap → 不设上限(与 top3_weighted 的 7.0 兜底不同: 本模式 cap 只用声明值)
    assert abs(_composite("weighted_sum") - 1.1) < 1e-9


def test_weighted_sum_missing_weight_defaults_one():
    """未声明 weight 的模型 → 1.0(与校验器 _num(m.get("weight"), 1.0) 同口径)。"""
    models = [dict(m) for m in _models3()]
    for m in models:
        m.pop("weight")
    out, _ = engine._compute_scores(
        FactorContext(code="600000.SH"),
        _strategy(models, mode="weighted_sum", cap=9.0))
    assert abs(out["composite"] - 3.0) < 1e-9         # 1 + 2 + 0, 各权重 1.0


def test_weighted_sum_dirty_weight_defaults_one():
    """脏权重(非数值/None/NaN/≤0)→ 该层回退 1.0, 不崩、不静默清零。"""
    for bad in ("x", None, float("nan"), 0.0, -1.0):
        models = [dict(m) for m in _models3()]
        models[0]["weight"] = bad
        out, _ = engine._compute_scores(
            FactorContext(code="600000.SH"),
            _strategy(models, mode="weighted_sum", cap=9.0))
        assert abs(out["composite"] - (1.0 + 0.5 + 0.0)) < 1e-9, bad
        assert out["m1"] == 1                       # 原始模型分不受污染


def test_weighted_sum_accepted_by_load_strategy():
    """模式白名单: load_strategy 必须接受 weighted_sum(否则实盘/回测加载即抛)。"""
    s = engine.load_strategy(_strategy(_models3(), mode="weighted_sum", cap=9.0))
    assert s["composite"]["mode"] == "weighted_sum"


@pytest.mark.parametrize("mode", ["average", "top3_weighted", "sum"])
def test_zero_impact_guard_existing_modes_ignore_model_weight(mode):
    """零影响守卫: 模型有没有声明 weight, 既有三种模式的 composite 完全一致。"""
    ctx = FactorContext(code="600000.SH")
    with_w = [m for m in _models3()]                     # 带 weight
    without = [dict(m) for m in _models3()]
    for m in without:
        m.pop("weight")
    comp = {"average": {}, "sum": {},
            "top3_weighted": {"weights": [0.60, 0.25, 0.15], "cap": 7.0}}[mode]
    o1, f1 = engine._compute_scores(ctx, _strategy(with_w, mode=mode, **comp))
    o2, f2 = engine._compute_scores(ctx, _strategy(without, mode=mode, **comp))
    assert o1 == o2 and f1 == f2


def test_candidate_min_model_ignores_layer_weights():
    """R2 边界: 加权只作用于 composite(排序), 不作用于资质线/分级。

    candidate_min_model 消费的是 model_scores 的**原始命中数**; 层权重从
    0.60 改成 0.06 只改排序分, 谁能过线一个不变。
    """
    ctx = FactorContext(code="600000.SH")
    base = {"id": "m1", "name": "m", "factors": ["A1", "A2"]}
    heavy, _ = engine._compute_scores(
        ctx, _strategy([dict(base, weight=0.60)], mode="weighted_sum", cap=9.0))
    light, _ = engine._compute_scores(
        ctx, _strategy([dict(base, weight=0.06)], mode="weighted_sum", cap=9.0))
    assert heavy["m1"] == light["m1"] == 2          # 原始命中数不变
    assert heavy["grade"] == light["grade"]         # 分级只看原始分
    assert (heavy["composite"], light["composite"]) == (1.2, 0.12)  # 只有排序分变
    # 资质线 candidate_min_model=3: 原始分 2 < 3 → 两种权重下都不过线
    for w in (0.60, 0.06):
        s = _strategy([dict(base, weight=w)], mode="weighted_sum", cap=9.0)
        s["filters"]["candidate_min_model"] = 3
        out = engine.run_screen(s, FactorContext(code="__MKT__"),
                                stock_contexts={"600000.SH": ctx})
        assert out["candidates"] == []


def test_full_factor_v1_composite_wired_to_weighted_sum():
    """接线验收: full_factor_v1 的四层权重是活的, cap 保持声明值 9.0。"""
    reg.scan_factors("prism.factors", force=True)
    p = Path(__file__).parent.parent / "strategies" / "full_factor_v1.json"
    s = engine.load_strategy(p)
    comp = s["composite"]
    assert comp["mode"] == "weighted_sum"
    assert abs(comp["cap"] - 9.0) < 1e-9
    assert [m["weight"] for m in s["scoring_models"]] == [0.60, 0.25, 0.15]
    # 声明权重的理论上限 = Σ(层权重 × 该层因子数) = 0.60×9 + 0.25×8 + 0.15×10
    assert abs(sum(m["weight"] * len(m["factors"])
                   for m in s["scoring_models"]) - 8.9) < 1e-9
