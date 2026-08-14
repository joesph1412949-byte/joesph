# -*- coding: utf-8 -*-
"""Y5 多概念: 概念标签≥3(东财计算, 数据在 ctx.fund["Y5"])。"""
from prism.registry import factor


@factor(id="Y5", name="多概念", category="monster",
        description="概念标签≥3")
def compute(ctx):
    fund = ctx.fund.get("Y5")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}
