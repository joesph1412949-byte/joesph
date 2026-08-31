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


def test_evaluate_stock_carries_up_stop_price_and_last():
    """候选 dict 必须带 up_stop_price/last(交易信号价格来源)。"""
    s = engine.load_strategy(_mk_strategy())
    ctx = FactorContext(code="600000.SH", up_price=10.55, last=10.5)
    r = engine.evaluate_stock("600000.SH", ctx, s)
    assert r["up_stop_price"] == 10.55
    assert r["last"] == 10.5


def test_evaluate_stock_up_price_none_passthrough():
    """ctx.up_price 缺失时保留 None(不造 0), 由 trader 端兜底。"""
    s = engine.load_strategy(_mk_strategy())
    ctx = FactorContext(code="600000.SH")
    r = engine.evaluate_stock("600000.SH", ctx, s)
    assert r["up_stop_price"] is None
    assert r["last"] is None


def test_run_screen_gate_blocks():
    s = engine.load_strategy(_mk_strategy())
    # 市场节点分 1 < 门槛3 → 环境不达标
    market_ctx = FactorContext(code="IDX")
    gate_factors = {"N1": 1, "N2": 0, "N3": 0, "N4": 0, "N5": 0}
    out = engine.run_screen(s, market_ctx, gate_factors=gate_factors,
                            stock_contexts={})
    assert out["environment_ok"] is False
    assert out["candidates"] == []


def test_factor_hit_bad_threshold_type_not_fail_open():
    """审查 Minor: threshold 类型错误(如 "abc")→ 未命中, 不逃逸 TypeError。"""
    s = _mk_strategy()
    s["scoring_models"] = [{"id": "m1", "name": "M1", "weight": 1.0,
                            "factors": [{"id": "A1", "op": ">",
                                         "threshold": "abc"}]}]
    s = engine.load_strategy(s)
    ctx = FactorContext(code="600000.SH")
    # 不再抛 TypeError(网页 500), 而是未命中
    r = engine.compute_model_scores(ctx, s)
    assert r["m1"] == 0
    # 因子命中位同样为 0(_compute_scores 内部)
    _, factors = engine._compute_scores(ctx, s)
    assert factors["A1"] == 0


def test_factor_hit_unknown_op_falls_back_score():
    """审查 Minor: op 不在白名单 → 退回 score>0 判定(与旧行为一致)。"""
    s = _mk_strategy()
    s["scoring_models"] = [{"id": "m1", "name": "M1", "weight": 1.0,
                            "factors": [{"id": "A1", "op": "??",
                                         "threshold": 1}]}]
    s = engine.load_strategy(s)
    ctx = FactorContext(code="600000.SH")
    r = engine.compute_model_scores(ctx, s)
    assert r["m1"] == 1   # A1 score=1 > 0 → 命中


# ---------------- v5 实盘板块评分门(run_screen) ----------------

def test_run_screen_sector_score_gate_and_attach():
    """评分开启+ctx带mkt: 低分板块候选被剔除, 通过者附 sector_score。"""
    from prism import sector_score as ss
    s = {
        "id": "t", "name": "t", "description": "",
        "market_gate": {"model": "node", "threshold": 0, "factors": []},
        "scoring_models": [{"id": "m", "name": "m", "weight": 1.0,
                            "factors": []}],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 0},
        "sector_score": {"enabled": True, "threshold": 75,
                         "position": {"step": 0.05, "cap_ratio": 0.45}},
    }
    # 因子0分全靠 candidate_min_model=0 放行; 评分数据: 801110 满分, 801999 无
    closes = [100.0 * (1.011 ** i) for i in range(11)]
    mkt = {"sector": {"801110": {"dates": ["2026-07-%02d" % (i + 1)
                                                for i in range(11)],
                                 "close": closes}}}
    market_ctx = FactorContext(code="__MKT__", mkt=mkt,
                               sector_map={"600000.SH": "801110",
                                           "000001.SZ": "801999"})
    stock_ctxs = {c: FactorContext(code=c, kline=None)
                  for c in ("600000.SH", "000001.SZ")}
    out = engine.run_screen(s, market_ctx, stock_contexts=stock_ctxs)
    codes = [c["code"] for c in out["candidates"]]
    assert codes == ["600000.SH"]                 # 801999 无评分 → 剔除
    assert out["candidates"][0]["sector_score"] > 75


def test_run_screen_sector_score_disabled_noop():
    """评分关闭/ctx无mkt → 行为与 v4 完全一致, 不附键。"""
    s = {"id": "t", "name": "t", "description": "",
         "market_gate": {"model": "node", "threshold": 0, "factors": []},
         "scoring_models": [{"id": "m", "name": "m", "weight": 1.0,
                             "factors": []}],
         "composite": {"mode": "sum"},
         "filters": {"candidate_min_model": 0}}
    market_ctx = FactorContext(code="__MKT__")
    stock_ctxs = {"600000.SH": FactorContext(code="600000.SH", kline=None)}
    out = engine.run_screen(s, market_ctx, stock_contexts=stock_ctxs)
    assert len(out["candidates"]) == 1
    assert "sector_score" not in out["candidates"][0]


def test_run_screen_sector_score_empty_scores_fail_closed():
    """mkt 注入但算不出任何评分(sector 段空) → 全部候选剔除(fail-closed,
    与回测侧同语义, 设计 §3.5)。"""
    s = {"id": "t", "name": "t", "description": "",
         "market_gate": {"model": "node", "threshold": 0, "factors": []},
         "scoring_models": [{"id": "m", "name": "m", "weight": 1.0,
                             "factors": []}],
         "composite": {"mode": "sum"},
         "filters": {"candidate_min_model": 0},
         "sector_score": {"enabled": True, "threshold": 75,
                          "position": {"step": 0.05, "cap_ratio": 0.45}}}
    market_ctx = FactorContext(code="__MKT__", mkt={"sector": {}},
                               sector_map={"600000.SH": "801110"})
    stock_ctxs = {"600000.SH": FactorContext(code="600000.SH", kline=None)}
    out = engine.run_screen(s, market_ctx, stock_contexts=stock_ctxs)
    assert out["candidates"] == []
