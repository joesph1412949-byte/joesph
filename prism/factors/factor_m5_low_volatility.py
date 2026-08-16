# -*- coding: utf-8 -*-
"""M5 波动收敛: 近20日振幅 < 15%(回测友好因子, 仅用K线)。
振幅 = (最高-最低)/最低, 衡量蓄势整理。"""
from prism.registry import factor


@factor(id="M5", name="波动收敛", category="momentum",
        description="近20日振幅小于15%——缩量蓄势形态(仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 20:
        return {"score": 0, "note": "K线不足"}
    closes = kline["close"].tolist()[-20:]
    hi = max(closes)
    lo = min(closes)
    if lo <= 0:
        return {"score": 0, "note": "最低价为0"}
    amp = (hi - lo) / lo * 100
    hit = amp < 15.0
    return {"score": 1 if hit else 0,
            "note": "20日振幅 %.1f%%" % amp}
