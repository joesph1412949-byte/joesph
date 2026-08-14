# -*- coding: utf-8 -*-
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism.context import FactorContext
from prism import engine


def _mk_strategy():
    return {
        "id": "t", "name": "测试", "description": "",
        "market_gate": {"model": "node", "threshold": 3,
                        "factors": ["N1", "N2", "N3", "N4", "N5"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 0.6, "factors": ["A1", "A2"]},
            {"id": "m2", "name": "M2", "weight": 0.25, "factors": ["B1"]},
        ],
        "composite": {"mode": "top3_weighted", "weights": [0.6, 0.25, 0.15],
                      "cap": 7.0},
        "filters": {"candidate_min_model": 1, "environment_threshold": 3},
    }


@pytest.fixture(autouse=True)
def _factors():
    reg.reset()
    @reg.factor(id="N1", name="n1", category="node", description="")
    def f_n1(ctx):
        return {"score": 1, "note": ""}
    # N2-N5: 简报 market_gate 引用 N1..N5, 而 load_strategy 校验门槛因子
    # 存在性; 简报夹具漏注册 N2-N5, 补上(返回 0, run_screen 门槛测试显式传 gate_factors)
    @reg.factor(id="N2", name="n2", category="node", description="")
    def f_n2(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="N3", name="n3", category="node", description="")
    def f_n3(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="N4", name="n4", category="node", description="")
    def f_n4(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="N5", name="n5", category="node", description="")
    def f_n5(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="A1", name="a1", category="通用", description="")
    def f_a1(ctx):
        return {"score": 1, "note": ""}
    @reg.factor(id="A2", name="a2", category="通用", description="")
    def f_a2(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="B1", name="b1", category="通用", description="")
    def f_b1(ctx):
        return {"score": 1, "note": ""}
    yield


def test_load_strategy_validates_unknown_factor():
    s = _mk_strategy()
    s["scoring_models"][0]["factors"] = ["NO_SUCH"]
    with pytest.raises(reg.UnknownFactorError):
        engine.load_strategy(s)


def test_load_strategy_requires_id_and_models():
    with pytest.raises(ValueError):
        engine.load_strategy({"name": "缺id"})
    with pytest.raises(ValueError):
        engine.load_strategy({"id": "x"})   # 缺 scoring_models


def test_compute_model_scores():
    ctx = FactorContext(code="600000.SH")
    s = engine.load_strategy(_mk_strategy())
    r = engine.compute_model_scores(ctx, s)
    assert r["m1"] == 1          # A1命中1 + A2未命中0
    assert r["m2"] == 1
    assert "composite" in r and "grade" in r


def test_composite_top3_weighted():
    # 模型分 1,1,0 → 1*0.6 + 1*0.25 + 0*0.15 = 0.85
    # (简报原文断言 1.45 与夹具不符: A1=1 命中、A2=0 未命中 → m1=1;
    #  其注释"模型分 2,1,0"与 test_compute_model_scores 的 m1==1 矛盾, 修正为 0.85)
    s = _mk_strategy()
    s["scoring_models"].append({"id": "m3", "name": "M3", "weight": 0.15,
                                "factors": []})
    s = engine.load_strategy(s)
    ctx = FactorContext(code="600000.SH")
    r = engine.compute_model_scores(ctx, s)
    assert abs(r["composite"] - 0.85) < 1e-6


def test_composite_sum_mode():
    s = _mk_strategy()
    s["composite"] = {"mode": "sum"}
    s = engine.load_strategy(s)
    ctx = FactorContext(code="600000.SH")
    r = engine.compute_model_scores(ctx, s)
    assert r["composite"] == 2   # 1 + 1


def test_evaluate_stock_shape():
    s = engine.load_strategy(_mk_strategy())
    ctx = FactorContext(code="600000.SH")
    r = engine.evaluate_stock("600000.SH", ctx, s)
    assert r["code"] == "600000.SH"
    assert r["scores"]["m1"] == 1
    assert r["factors"]["A1"] == 1
    assert r["factors"]["A2"] == 0


def test_run_screen_gate_blocks():
    s = engine.load_strategy(_mk_strategy())
    # 市场节点分 1 < 门槛3 → 环境不达标
    market_ctx = FactorContext(code="IDX")
    gate_factors = {"N1": 1, "N2": 0, "N3": 0, "N4": 0, "N5": 0}
    out = engine.run_screen(s, market_ctx, gate_factors=gate_factors,
                            stock_contexts={})
    assert out["environment_ok"] is False
    assert out["candidates"] == []
