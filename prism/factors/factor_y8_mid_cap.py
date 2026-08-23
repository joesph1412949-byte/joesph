# -*- coding: utf-8 -*-
"""Y8 中市值启动: 流通市值 ∈ [30, 100)亿(数据在 ctx.fund["Y8"])。

文档艾艾精工案例: 启动时约48亿属于中市值; Y1小市值(≤80亿)其实能覆盖,
但 Y8 把中市值区间单独标记, 策略配置可将其作为妖股模型的补充因子,
用于捕捉"市值略大但启动结构强"的票(需东财数据驱动)。
"""
from prism.registry import factor


@factor(id="Y8", name="中市值启动", category="monster",
        description="流通市值30-100亿的中市值启动票(东财计算)")
def compute(ctx):
    fund = ctx.fund.get("Y8")
    if not fund:
        return {"score": 0, "note": "东财数据缺失"}
    return {"score": fund.get("score", 0), "note": fund.get("note", "东财数据缺失")}