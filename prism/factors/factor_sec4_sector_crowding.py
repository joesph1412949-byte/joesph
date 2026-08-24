# -*- coding: utf-8 -*-
"""SEC4 板块拥挤度: 板块近5日均成交额未超近1年均值2倍(门槛正向语义)。

文档风控维度"板块拥挤度监控": 成交额占比MA5近一年分位>90%视为拥挤度高。
A股版量化(用板块指数成交额):
  * 近5日均成交额 / 近240日均成交额 > 2.0 → 交易拥挤过热(score=0, 门槛不通过)
  * ≤ 2.0 → 健康(score=1)

数据通路: ctx._extra["mkt"]["sector"][板块代码]["amount"](板块K线成交额,
asof 切片保证防未来)。数据缺失/不足 → fail-open 0。
"""
from prism.registry import factor


@factor(id="SEC4", name="板块不拥挤", category="sector",
        description="板块成交额未放大2倍以上(拥挤度监控, 需板块K线amount)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    rec = (mkt.get("sector") or {}).get(scode)
    if not rec or not rec.get("dates"):
        return {"score": 0, "note": "板块K线缺失(%s)" % scode}
    amounts = rec.get("amount") or []
    if len(amounts) < 241:
        return {"score": 0, "note": "板块成交额不足240日"}
    # 近5日均 vs 近240日均(不含近5日)
    ma5 = sum(amounts[-5:]) / 5
    ma240 = sum(amounts[-245:-5]) / 240 if len(amounts) >= 245 \
        else sum(amounts[:-5]) / (len(amounts) - 5)
    if ma240 <= 0:
        return {"score": 0, "note": "板块基数成交额为0"}
    ratio = ma5 / ma240
    hit = ratio <= 2.0
    return {"score": 1 if hit else 0,
            "note": "板块%s量能放大%.1f倍%s" % (
                scode, ratio, "拥挤" if not hit else "正常")}