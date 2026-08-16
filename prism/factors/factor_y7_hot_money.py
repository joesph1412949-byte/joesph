# -*- coding: utf-8 -*-
"""Y7 游资现身: 近5日龙虎榜净买入>0(东财计算, 数据在 ctx.fund["Y7"])。"""
from prism.registry import factor


@factor(id="Y7", name="游资现身", category="monster",
        description="近5日龙虎榜净买入>0")
def compute(ctx):
    fund = ctx.fund.get("Y7")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}
