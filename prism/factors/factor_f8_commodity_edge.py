# -*- coding: utf-8 -*-
"""F8 行业边际变化: 所属板块映射的商品期货任一品种 20 日涨幅 > +3% → 1 分。

数据通路: 回测/实盘把市场数据快照注入 ctx._extra["mkt"],
格式 {"futures": {板块代码: {"name": 板块名,
    "commodities": {品种代码: {"name": 品种名, "dates": [...], "close": [...]}}}}};
个股所属板块由 ctx.sector_map[code] 取(板块代码, 与 futures 键同源)。
20 日涨幅 = closes[-1] / closes[-21] - 1(需 ≥21 个收盘点)。
数据缺失/板块未映射/无期货数据 → fail-open 0(并说明缺什么)。"""
from prism.registry import factor

THRESHOLD = 0.03   # 20 日涨幅阈值(设计 §3.1 固定 +3%, 调参范围 +2%~+5%)
WINDOW = 20        # 回看交易日数
EPS = 1e-9         # 浮点容差: 恰好 +3.0% 因二进制表示误差(如 103/100-1
                   # = 0.030000000000000027)不得误判命中


@factor(id="F8", name="行业边际变化", category="sector",
        description="映射商品任一品种20日涨幅>+3%(需mkt注入, fail-open)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    futs = mkt.get("futures") or {}
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    rec = futs.get(scode)
    if not rec:
        return {"score": 0, "note": "板块无期货映射(%s)" % scode}
    best = None
    for sym, k in (rec.get("commodities") or {}).items():
        closes = k.get("close") or []
        if len(closes) < WINDOW + 1:
            continue
        base = closes[-(WINDOW + 1)]
        if not base:
            continue
        gain = closes[-1] / base - 1
        if best is None or gain > best[1]:
            best = (sym, gain)
    if best is None:
        return {"score": 0, "note": "期货数据不足20日(%s)" % scode}
    if best[1] > THRESHOLD + EPS:
        return {"score": 1,
                "note": "%s 20日涨%.1f%%" % (best[0], best[1] * 100)}
    return {"score": 0,
            "note": "商品无边际改善(最强%.1f%%)" % (best[1] * 100)}
