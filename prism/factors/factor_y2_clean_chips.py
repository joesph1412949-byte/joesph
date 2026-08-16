# -*- coding: utf-8 -*-
"""Y2 筹码干净: 股东户数环比下降(东财计算, 数据在 ctx.fund["Y2"])。"""
from prism.registry import factor


@factor(id="Y2", name="筹码干净", category="monster",
        description="股东户数环比下降")
def compute(ctx):
    fund = ctx.fund.get("Y2")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}
