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
