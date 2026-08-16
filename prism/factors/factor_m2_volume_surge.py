# -*- coding: utf-8 -*-
"""M2 量能放大: 今日成交量 > 前5日均量 × 1.5(回测友好因子, 仅用K线)。"""
from prism.registry import factor


@factor(id="M2", name="量能放大", category="momentum",
        description="今日成交量超过前5日均量的1.5倍(仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 6:
        return {"score": 0, "note": "K线不足"}
    vols = kline["volume"].tolist()
    ma5_prev = sum(vols[-6:-1]) / 5
    if ma5_prev <= 0:
        return {"score": 0, "note": "均量为0"}
    ratio = vols[-1] / ma5_prev
    hit = ratio > 1.5
    return {"score": 1 if hit else 0,
            "note": "量能/前5日均量=%.2f倍" % ratio}
