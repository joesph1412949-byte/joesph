# -*- coding: utf-8 -*-
"""Y1 小市值: 流通市值<80亿(东财计算, 数据在 ctx.fund["Y1"])。"""
from prism.registry import factor


@factor(id="Y1", name="小市值", category="monster",
        description="流通市值<80亿")
def compute(ctx):
    fund = ctx.fund.get("Y1")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}
