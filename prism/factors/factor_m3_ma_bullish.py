# -*- coding: utf-8 -*-
"""M3 均线多头: MA5 > MA10 > MA20(回测友好因子, 仅用K线)。"""
from prism.registry import factor


@factor(id="M3", name="均线多头", category="momentum",
        description="5/10/20日均线多头排列(仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 20:
        return {"score": 0, "note": "K线不足"}
    closes = kline["close"].tolist()
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    hit = ma5 > ma10 > ma20
    return {"score": 1 if hit else 0,
            "note": "MA5=%.2f MA10=%.2f MA20=%.2f" % (ma5, ma10, ma20)}
