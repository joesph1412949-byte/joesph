# -*- coding: utf-8 -*-
"""S2 量价堆积密度: 60日≥20天放量且价格窄幅震荡。"""
from prism.registry import factor


@factor(id="S2", name="量价堆积密度", category="momentum",
        description="60日≥20天放量且价格窄幅震荡")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 60:
        return {"score": 0, "note": "量价密度不足"}
    vols = kline["volume"].tolist()[-60:]
    closes = kline["close"].tolist()[-60:]
    ma60 = sum(vols) / 60
    big = sum(1 for v in vols if v > ma60 * 1.5)
    hi, lo = max(closes), min(closes)
    narrow = (hi - lo) / lo <= 0.10 if lo > 0 else False
    s2 = 1 if (big >= 20 and narrow) else 0
    return {"score": s2, "note": "60日≥20天放量且价格窄幅震荡" if s2 else "量价密度不足"}
