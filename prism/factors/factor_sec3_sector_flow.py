# -*- coding: utf-8 -*-
"""SEC3 板块资金流: 所属行业板块近5日主力资金净流入 > 0(累计)。

数据通路: ctx._extra["mkt"]["sector_flow"][板块代码] = {"dates": [...],
"main_net_in": [...]}(市场数据层注入; asof 切片保证防未来)。
个股所属板块由 ctx.sector_map[code] 取。数据缺失 → fail-open 0。
"""
from prism.registry import factor


@factor(id="SEC3", name="板块资金流入", category="sector",
        description="所属行业板块近5日主力净流入为正(需资金流注入)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    # mkt 可能带 sector_flow(资金流) 段
    fl = (mkt.get("sector_flow") or {}).get(scode)
    if not fl or not fl.get("dates"):
        return {"score": 0, "note": "板块资金流缺失(%s)" % scode}
    net = fl.get("main_net_in") or []
    if len(net) < 5:
        return {"score": 0, "note": "资金流不足5日"}
    total = sum(net[-5:])
    hit = total > 0
    return {"score": 1 if hit else 0,
            "note": "板块%s近5日净流入 %.2f亿" % (scode, total / 1e8)}