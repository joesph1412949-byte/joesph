# -*- coding: utf-8 -*-
"""N2 情绪周期: 涨停家数≥50 直接判暖; 20~49 家需封板率>60% 佐证。"""
from prism.registry import factor


@factor(id="N2", name="情绪周期", category="node",
        description="涨停家数≥50 或 20~49家且封板率>60%")
def compute(ctx):
    limit_ups = ctx.limit_ups or []
    n2 = 0
    if len(limit_ups) >= 50:
        n2 = 1
    elif len(limit_ups) >= 20:
        sealed_cnt = sum(1 for lu in limit_ups if lu.get("sealed"))
        sealed_ratio = sealed_cnt / len(limit_ups) if limit_ups else 0.0
        if sealed_ratio > 0.6:
            n2 = 1
    n2_note = "涨停家数 %d, 情绪偏暖" % len(limit_ups) if n2 else \
              "涨停家数 %d, 情绪偏冷" % len(limit_ups)
    return {"score": n2, "note": n2_note}
