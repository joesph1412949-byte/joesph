# -*- coding: utf-8 -*-
"""S4 均线系统: 60/120/250多头排列且斜率向上。"""
from prism.registry import factor
from prism._utils import ma


@factor(id="S4", name="均线系统", category="momentum",
        description="60/120/250多头排列且斜率向上")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 250:
        return {"score": 0, "note": "长均线未多头"}
    closes = kline["close"]
    ma60 = ma(closes, 60).iloc[-1]
    ma120 = ma(closes, 120).iloc[-1]
    ma250 = ma(closes, 250).iloc[-1]
    slope_up = closes.iloc[-1] > closes.iloc[-6]
    s4 = 1 if (ma60 > ma120 > ma250 and slope_up) else 0
    return {"score": s4, "note": "60/120/250多头排列且斜率向上" if s4 else "长均线未多头"}
