# -*- coding: utf-8 -*-
"""Y3 倍量突破: 涨停日量≥前5日均量×3。"""
from prism.registry import factor


@factor(id="Y3", name="倍量突破", category="monster",
        description="涨停日量≥前5日均量×3")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 6:
        return {"score": 0, "note": "量能未达3倍"}
    vols = kline["volume"].tolist()
    today = vols[-1]
    ma5_prev = sum(vols[-6:-1]) / 5
    y3 = 1 if today >= ma5_prev * 3 else 0
    return {"score": y3, "note": "涨停日量≥前5日均量×3" if y3 else "量能未达3倍"}
