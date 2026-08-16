# -*- coding: utf-8 -*-
"""M1 动量: 近20日涨幅 > 5%(回测友好因子, 仅用K线)。"""
from prism.registry import factor


@factor(id="M1", name="动量", category="momentum",
        description="近20日涨幅超过5%(仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 21:
        return {"score": 0, "note": "K线不足"}
    closes = kline["close"].tolist()
    if closes[-21] <= 0:
        return {"score": 0, "note": "前收为0"}
    chg = (closes[-1] / closes[-21] - 1) * 100
    hit = chg > 5.0
    return {"score": 1 if hit else 0,
            "note": "近20日涨幅 %.1f%%" % chg}
