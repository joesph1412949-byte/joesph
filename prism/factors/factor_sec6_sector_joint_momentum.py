# -*- coding: utf-8 -*-
"""SEC6 行业联合动量: 个股与所属板块共振(文档"行业联合动量因子")。

国信研究: 个股大涨当天若行业涨幅也较大 → 后续超额收益显著。
A股版量化(个股K线 × 板块K线五维共振简化):
  * 个股近5日涨幅 > 3%(个股动量成立)
  * 所属板块近5日涨幅 > 2%(板块动量共振)

数据通路:
  * 个股K线 ctx.kline(回测适配层已注入, asof 防未来)
  * 板块K线 ctx._extra["mkt"]["sector"][板块代码]["close"]
数据缺失/不足 → fail-open 0。
"""
from prism.registry import factor


@factor(id="SEC6", name="行业共振", category="sector",
        description="个股近5日涨幅>3%且板块近5日涨幅>2%(量价共振)")
def compute(ctx):
    # 1. 个股动量
    kline = ctx.kline
    if kline is None or len(kline) < 6:
        return {"score": 0, "note": "个股K线不足"}
    closes = kline["close"].tolist()
    if closes[-6] <= 0:
        return {"score": 0, "note": "个股前收为0"}
    stock_chg = (closes[-1] / closes[-6] - 1) * 100
    if stock_chg <= 3.0:
        return {"score": 0,
                "note": "个股近5日涨幅 %.1f%%(<3%%)" % stock_chg}
    # 2. 板块动量
    mkt = ctx.get("mkt") or {}
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    rec = (mkt.get("sector") or {}).get(scode)
    if not rec or not rec.get("dates") or len(rec.get("close") or []) < 6:
        return {"score": 0, "note": "板块K线不足(%s)" % scode}
    scloses = rec["close"]
    if scloses[-6] <= 0:
        return {"score": 0, "note": "板块前收为0"}
    sector_chg = (scloses[-1] / scloses[-6] - 1) * 100
    if sector_chg <= 2.0:
        return {"score": 0,
                "note": "个股涨%.1f%% 但板块仅涨%.1f%%, 无共振" % (
                    stock_chg, sector_chg)}
    return {"score": 1,
            "note": "个股涨%.1f%% 板块涨%.1f%% 共振" % (stock_chg, sector_chg)}