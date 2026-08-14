# -*- coding: utf-8 -*-
"""S1 手填因子: 人工评估得分(数据在 ctx.manual["S1"])。"""
from prism.registry import factor


@factor(id="S1", name="手填因子", category="momentum",
        description="人工评估得分")
def compute(ctx):
    score = ctx.manual.get("S1") or 0
    return {"score": score, "note": "人工评估得分 %d" % score}
