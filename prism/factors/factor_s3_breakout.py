# -*- coding: utf-8 -*-
"""S3 最小阻力突破: 放量突破60/120日均线。"""
from prism.registry import factor


@factor(id="S3", name="最小阻力突破", category="momentum",
        description="放量突破60/120日均线")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 120:
        return {"score": 0, "note": "未突破长期均线"}
    closes = kline["close"].tolist()
    vols = kline["volume"].tolist()
    ma60 = sum(closes[-60:]) / 60
    ma120 = sum(closes[-120:]) / 120
    ma60v = sum(vols[-60:]) / 60
    today_v = vols[-1]
    broke = closes[-1] > ma60 and closes[-1] > ma120
    volup = today_v > ma60v * 1.5
    s3 = 1 if (broke and volup) else 0
    return {"score": s3, "note": "放量突破60/120日均线" if s3 else "未突破长期均线"}
