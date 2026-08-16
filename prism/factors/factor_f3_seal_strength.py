# -*- coding: utf-8 -*-
"""F3 封单强度: 封单金额 ≥ 流通市值 × 0.5%(主板) / 0.2%(创业科创)。"""
from prism.registry import factor


@factor(id="F3", name="封单强度", category="first_board",
        description="封单≥流通市值0.5%(主板)/0.2%(创业科创)")
def compute(ctx):
    if not ctx.sealed or not ctx.up_price:
        return {"score": 0, "note": "封单不足"}
    float_vol = ctx.get("float_vol") or 0
    if float_vol <= 0:
        return {"score": 0, "note": "封单不足"}
    tick = ctx.tick or {}
    bid0 = (tick.get("bidPrice") or [0])[0]
    bidv0 = (tick.get("bidVol") or [0])[0]
    seal_amount = bid0 * bidv0
    float_mv = ctx.up_price * float_vol
    code = ctx.code or ""
    ratio = 0.005 if not code.startswith(("300", "301", "688")) else 0.002
    f3 = 1 if seal_amount >= float_mv * ratio else 0
    return {"score": f3,
            "note": "封单≥流通市值0.5%(主板)/0.2%(创业科创)" if f3 else "封单不足"}
