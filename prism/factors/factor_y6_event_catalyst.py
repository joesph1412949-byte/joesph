# -*- coding: utf-8 -*-
"""Y6 事件催化: 近5日公告命中利好关键词(东财计算, 数据在 ctx.fund["Y6"])。"""
from prism.registry import factor


@factor(id="Y6", name="事件催化", category="monster",
        description="近5日公告命中利好关键词")
def compute(ctx):
    fund = ctx.fund.get("Y6")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}
