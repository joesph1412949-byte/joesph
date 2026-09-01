# -*- coding: utf-8 -*-
"""策略校验器测试 — 真注册表(已含全部44因子), 全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism import registry as reg
from prism.engine import validate_strategy_payload


def _good(**over):
    p = {"name": "我的组合",
         "models": [{"id": "first_board", "name": "首板", "weight": 1.0,
                     "factors": ["F1", "F8"], "weights": [1, 1]}],
         "gate_factors": ["N1"], "gate_threshold": 1,
         "candidate_min_model": 3,
         "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                  "max_hold_days": 5}}
    p.update(over)
    return p


@pytest.fixture(scope="module", autouse=True)
def _scan():
    reg.scan_factors(force=True)


def test_valid_payload():
    ok, errs, s = validate_strategy_payload(_good())
    assert ok is True and errs == []
    assert s["composite"]["cap"] == 2.0        # 1.0×(1+1)
    assert s["market_gate"] == {"model": "node", "threshold": 1,
                                "factors": ["N1"]}
    assert s["sell_rules"]["take_profit_pct"] == 0.08
    assert s["id"] is None                     # id 由端点生成


def test_name_empty():
    ok, errs, _ = validate_strategy_payload(_good(name="  "))
    assert ok is False and any("名称" in e for e in errs)


def test_factor_unknown():
    ok, errs, _ = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 1.0,
                 "factors": ["F1", "ZZ9"], "weights": []}]))
    assert ok is False and any("ZZ9" in e for e in errs)


def test_factor_dup_across_models():
    ok, errs, _ = validate_strategy_payload(_good(
        models=[{"id": "a", "name": "a", "weight": 0.5,
                 "factors": ["F1", "F2"], "weights": []},
                {"id": "b", "name": "b", "weight": 0.5,
                 "factors": ["F2", "Y1"], "weights": []}]))
    assert ok is False and any("重复" in e for e in errs)


def test_gate_must_be_node():
    ok, errs, _ = validate_strategy_payload(_good(gate_factors=["F1"]))
    assert ok is False and any("node" in e for e in errs)


def test_gate_can_be_empty():
    ok, errs, s = validate_strategy_payload(_good(gate_factors=[]))
    assert ok is True and s["market_gate"]["factors"] == []


def test_sell_out_of_range():
    ok, errs, _ = validate_strategy_payload(_good(
        sell={"take_profit_pct": 0.8, "stop_loss_pct": 0.05,
              "max_hold_days": 5}))
    assert ok is False and any("止盈" in e for e in errs)


def test_weights_alignment():
    """weights 长度不齐 → 截断+补1.0; cap 按对齐后求和。"""
    ok, errs, s = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 0.5,
                 "factors": ["F1", "F8", "F9"], "weights": [2.0, 0.0, 3.0]}]))
    assert ok is False and any("因子权重" in e for e in errs)   # 0.0 非法
    ok2, _, s2 = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 0.5,
                 "factors": ["F1", "F8", "F9"], "weights": [2.0]}]))
    assert ok2 is True
    assert s2["scoring_models"][0]["weights"] == [2.0, 1.0, 1.0]
    assert s2["composite"]["cap"] == round(0.5 * 4.0, 2)


# ---------------- 审查修复回归(I-1 防崩 / M-2 id查重 / sell 显式报错) ----------------

def test_factor_non_string_no_crash():
    """I-1: factors 携带嵌套 list/dict 条目 → ok=False 明确报错, 不再 TypeError 500。"""
    ok, errs, _ = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 1.0,
                 "factors": [["F2"], {"id": "F8"}], "weights": []}]))
    assert ok is False
    assert any("需为字符串编号" in e for e in errs)


def test_gate_non_string_no_crash():
    """I-1 同类: 门槛因子携带嵌套条目 → 明确报错, 不崩。"""
    ok, errs, _ = validate_strategy_payload(_good(gate_factors=[["N1"]]))
    assert ok is False and any("需为字符串编号" in e for e in errs)


def test_model_id_dup():
    """M-2: 模型 id 重复 → 显式报错。"""
    ok, errs, _ = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "a", "weight": 0.5,
                 "factors": ["F1"], "weights": []},
                {"id": "m", "name": "b", "weight": 0.5,
                 "factors": ["F8"], "weights": []}]))
    assert ok is False and any("模型 id m 重复" in e for e in errs)


def test_sell_non_dict_rejected():
    """sell 非 dict 不再静默落 {} → 显式报错(与其他字段一致性)。"""
    ok, errs, _ = validate_strategy_payload(_good(sell="x"))
    assert ok is False and any("卖出规则" in e for e in errs)
