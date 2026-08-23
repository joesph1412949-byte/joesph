# -*- coding: utf-8 -*-
"""M7 年线企稳 — 回踩年线不破的二次买点。

识别趋势股回踩 MA250 获得支撑的形态(纯K线, 回测可用):
  1. 中期趋势: 近20日收盘均值 > MA250(趋势向上)
  2. 回踩不破: 今日收盘 >= MA250 × 0.97(允许3%容忍度, 防虚破)
  3. 近5日曾下探至年线附近(回踩动作): 近5日最低 <= MA250 × 1.03

语义: 股价沿年线上方运行, 近期回踩年线获得支撑——低吸买点。
"""
from prism.registry import factor


@factor(id="M7", name="年线企稳", category="momentum",
        description="回踩年线获支撑的二次买点(仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 251:
        return {"score": 0, "note": "K线不足250日"}
    closes = kline["close"].tolist()
    today = closes[-1]
    ma250 = sum(closes[-251:-1]) / 250
    if ma250 <= 0:
        return {"score": 0, "note": "年线为0"}
    # 1. 中期趋势: 近20日均值 > 年线
    ma20 = sum(closes[-20:]) / 20
    if ma20 <= ma250:
        return {"score": 0,
                "note": "中期趋势未向上(MA20 %.2f <= MA250 %.2f)" % (ma20, ma250)}
    # 2. 回踩不破: 今日收盘 >= 年线×0.97
    if today < ma250 * 0.97:
        return {"score": 0,
                "note": "已跌破年线(今 %.2f < 年线×0.97 %.2f)" % (today, ma250 * 0.97)}
    # 3. 近5日曾下探至年线附近(回踩动作存在)
    low5 = min(closes[-5:])
    if low5 > ma250 * 1.03:
        return {"score": 0, "note": "近5日未回踩(最低 %.2f 距年线远)" % low5}
    return {"score": 1,
            "note": "回踩年线(MA250=%.2f)企稳, MA20=%.2f" % (ma250, ma20)}