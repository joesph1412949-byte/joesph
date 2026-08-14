# -*- coding: utf-8 -*-
"""S6 板块共振强度: 板块内≥3只走强。"""
from prism.registry import factor
from prism._utils import sector_count


@factor(id="S6", name="板块共振强度", category="momentum",
        description="板块≥3只走强")
def compute(ctx):
    s6 = 1 if sector_count(ctx.code or "", ctx.sector_map, ctx.limit_ups) >= 3 else 0
    return {"score": s6, "note": "板块≥3只走强" if s6 else "板块内同步走强不足"}
