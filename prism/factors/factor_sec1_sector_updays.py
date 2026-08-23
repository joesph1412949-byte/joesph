# -*- coding: utf-8 -*-
"""SEC1 板块连阳: 所属行业板块连续收阳天数 ≥ 3(回测友好, 数据来自市场数据层)。

数据通路: 回测/实盘把市场数据快照注入 ctx._extra["mkt"],
格式 {"sector": {板块代码: {"dates": [...], "close": [...]}}};
个股所属板块由 ctx.sector_map[code] 取(板块代码, 如 BK0475)。
数据缺失 → fail-open 0(并说明缺什么)。"""
from prism.registry import factor


@factor(id="SEC1", name="板块连阳", category="sector",
        description="所属行业板块连续收阳≥3天(需市场数据注入)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    rec = (mkt.get("sector") or {}).get(scode)
    if not rec or not rec.get("dates"):
        return {"score": 0, "note": "板块K线缺失(%s)" % scode}
    closes = rec.get("close") or []
    if len(closes) < 2:
        return {"score": 0, "note": "板块K线不足"}
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            n += 1
        else:
            break
    hit = n >= 3
    return {"score": 1 if hit else 0,
            "note": "板块%s连阳%d天" % (scode, n)}