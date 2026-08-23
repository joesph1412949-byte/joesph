# -*- coding: utf-8 -*-
"""SEC2 板块近10日涨幅: 所属行业板块近10日累计涨幅 > 5%(回测友好)。

数据通路同 SEC1: ctx._extra["mkt"]["sector"][板块代码] → 按日K线。
板块收盘序列为升序(最新在最后)。数据缺失 → fail-open 0。"""
from prism.registry import factor


@factor(id="SEC2", name="板块10日涨幅", category="sector",
        description="所属行业板块近10日累计涨幅>5%(需市场数据注入)")
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
    if len(closes) < 11:
        return {"score": 0, "note": "板块K线不足10日"}
    base = closes[-11]
    if not base or base <= 0:
        return {"score": 0, "note": "板块前收为0"}
    chg = (closes[-1] / base - 1) * 100
    hit = chg > 5.0
    return {"score": 1 if hit else 0,
            "note": "板块%s近10日涨幅%.1f%%" % (scode, chg)}