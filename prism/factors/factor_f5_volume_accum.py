# -*- coding: utf-8 -*-
"""F5 量价堆积: 20日内≥5天量>5日均量×1.5。"""
from prism.registry import factor


@factor(id="F5", name="量价堆积", category="first_board",
        description="20日内≥5天量>5日均量×1.5")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 20:
        return {"score": 0, "note": "量价堆积不足"}
    vols = kline["volume"].tolist()[-20:]
    ma5 = sum(vols[-5:]) / 5
    days = sum(1 for v in vols if v > ma5 * 1.5)
    f5 = 1 if days >= 5 else 0
    return {"score": f5, "note": "20日内≥5天量>5日均量×1.5" if f5 else "量价堆积不足"}
