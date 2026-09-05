# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.registry as reg
from prism import factor_check


def test_check_fake_data_runs():
    reg.reset()
    @reg.factor(id="TCHK", name="体检因子", category="通用", description="")
    def compute(ctx):
        return {"score": 1, "note": "ok"}
    results = factor_check.run_checks(scan=False)
    by_id = {fid: ok for fid, ok, msg in results}
    assert by_id["TCHK"] is True


def test_check_catches_bad_signature():
    reg.reset()
    @reg.factor(id="TBAD", name="坏因子", category="通用", description="")
    def compute():   # 缺 ctx 参数
        return {"score": 0, "note": ""}
    results = factor_check.run_checks(scan=False)
    by_id = {fid: ok for fid, ok, msg in results}
    assert by_id["TBAD"] is False


def test_rich_scenario_reports_hits():
    """rich 场景下能命中的因子, 消息应带命中标注(区分'不命中'与'数据缺')。"""
    reg.reset()
    @reg.factor(id="TRICH", name="富数据因子", category="通用", description="")
    def compute(ctx):
        idx = ctx.get("sh_index_kline")
        if idx is not None and len(idx) >= 21:
            return {"score": 1, "note": "hit"}
        return {"score": 0, "note": "miss"}
    results = factor_check.run_checks(scan=False, rich=True)
    msg = dict((fid, msg) for fid, ok, msg in results)["TRICH"]
    assert "rich命中" in msg


def test_rich_scenario_reports_zero_hit():
    """数据齐全仍恒 0 的因子应被标 ZERO-HIT(而不是静默混过)。"""
    reg.reset()
    @reg.factor(id="TZERO", name="恒零因子", category="通用", description="")
    def compute(ctx):
        return {"score": 0, "note": "always zero"}
    results = factor_check.run_checks(scan=False, rich=True)
    msg = dict((fid, msg) for fid, ok, msg in results)["TZERO"]
    assert "ZERO-HIT" in msg


def test_rich_scenarios_all_constructible():
    """5 个合成场景必须全部能构造 —— 场景挂了等于命中率检查整体失效。"""
    import pytest
    for name, fn in factor_check._rich_scenarios():
        try:
            ctx = fn()
        except Exception as e:
            pytest.fail("合成场景 %s 构造失败: %r" % (name, e))
        assert ctx is not None
