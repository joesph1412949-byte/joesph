# -*- coding: utf-8 -*-
"""F1 首板确认: 近20日无涨停且今日首次涨停。"""
from prism.registry import factor
from shared.common import limit_ratio_for_code


@factor(id="F1", name="首板确认", category="first_board",
        description="近20日无涨停且今日首次涨停")
def compute(ctx):
    kline = ctx.kline
    up_price = ctx.up_price
    last = ctx.last or 0
    if kline is None or not up_price:
        return {"score": 0, "note": "K线或涨停价缺失"}
    closes = kline["close"].tolist()
    ratio = limit_ratio_for_code(ctx.code or "")
    start = max(1, len(closes) - 20)
    prev_limit = False
    for i in range(start, len(closes) - 1):
        limit_px = round(closes[i - 1] * (1 + ratio), 2)
        if closes[i] >= limit_px - 0.01:
            prev_limit = True
            break
    f1 = 1 if (not prev_limit and last >= up_price - 0.01) else 0
    return {"score": f1,
            "note": "近20日无涨停且今日首次涨停" if f1 else "近20日已有涨停或今日未涨停"}
