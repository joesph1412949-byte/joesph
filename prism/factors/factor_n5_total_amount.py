# -*- coding: utf-8 -*-
"""N5 两市成交额: 全市场成交额≥2万亿。"""
from prism.registry import factor


@factor(id="N5", name="两市成交额", category="node",
        description="两市成交额≥2万亿")
def compute(ctx):
    ticks = ctx.get("ticks") or {}
    total = sum((t.get("amount") or 0) for t in ticks.values())
    n5 = 1 if total >= 2e12 else 0
    n5_note = "两市成交额 %.0f 亿 >= 2万亿" % (total / 1e8) if n5 else \
              "两市成交额 %.0f 亿 < 2万亿" % (total / 1e8)
    return {"score": n5, "note": n5_note}
