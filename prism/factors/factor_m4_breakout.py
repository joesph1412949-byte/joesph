# -*- coding: utf-8 -*-
"""M4 突破: 今日收盘创近20日新高(回测友好因子, 仅用K线)。"""
from prism.registry import factor


@factor(id="M4", name="突破新高", category="momentum",
        description="今日收盘价创近20日新高(仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 21:
        return {"score": 0, "note": "K线不足"}
    closes = kline["close"].tolist()
    today = closes[-1]
    prev_high = max(closes[-21:-1])   # 近20日(不含今日)最高
    hit = today > prev_high
    return {"score": 1 if hit else 0,
            "note": "今日 %.2f > 20日高点 %.2f" % (today, prev_high)}
