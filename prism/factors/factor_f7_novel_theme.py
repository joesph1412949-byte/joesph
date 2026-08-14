# -*- coding: utf-8 -*-
"""F7 题材新颖: 今日题材为近5日首次出现(东财计算, 数据在 ctx.fund["F7"])。"""
from prism.registry import factor


@factor(id="F7", name="题材新颖", category="first_board",
        description="今日题材为近5日首次出现(东财)")
def compute(ctx):
    fund = ctx.fund.get("F7")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}
