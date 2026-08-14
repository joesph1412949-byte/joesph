# -*- coding: utf-8 -*-
"""S5 机构流入: 融资余额近5日增长(东财接口探针未确认, 保持手填, 数据在 ctx.manual["S5"])。"""
from prism.registry import factor


@factor(id="S5", name="机构流入", category="momentum",
        description="融资余额近5日增长(东财接口未确认, 保持手填)")
def compute(ctx):
    score = ctx.manual.get("S5") or 0
    return {"score": score, "note": "人工评估得分 %d" % score}
