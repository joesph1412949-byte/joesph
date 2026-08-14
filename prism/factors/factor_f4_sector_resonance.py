# -*- coding: utf-8 -*-
"""F4 板块共振: 所属板块内当日涨停≥3家。"""
from prism.registry import factor
from prism._utils import sector_count


@factor(id="F4", name="板块共振", category="first_board",
        description="板块涨停≥3家")
def compute(ctx):
    f4 = 1 if sector_count(ctx.code or "", ctx.sector_map, ctx.limit_ups) >= 3 else 0
    return {"score": f4, "note": "板块涨停≥3家" if f4 else "板块共振不足"}
