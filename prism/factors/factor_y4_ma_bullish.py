# -*- coding: utf-8 -*-
"""Y4 均线多头: 5/10/20多头排列且站上60日线。"""
from prism.registry import factor
from prism._utils import ma


@factor(id="Y4", name="均线多头", category="monster",
        description="5/10/20多头排列且站上60日线")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 60:
        return {"score": 0, "note": "均线未多头"}
    closes = kline["close"]
    ma5 = ma(closes, 5).iloc[-1]
    ma10 = ma(closes, 10).iloc[-1]
    ma20 = ma(closes, 20).iloc[-1]
    ma60 = ma(closes, 60).iloc[-1]
    y4 = 1 if ma5 > ma10 > ma20 and closes.iloc[-1] > ma60 else 0
    return {"score": y4, "note": "5/10/20多头排列且站上60日线" if y4 else "均线未多头"}
