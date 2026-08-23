# -*- coding: utf-8 -*-
"""M6 横盘蓄势突破 — "阻力最小方向"形态(艾艾精工类)。

识别长期横盘后首次放量突破的启动形态:
  1. 长期横盘: 近60日振幅 < 30%(箱体/低位整理, 抛压出清)
  2. 站上年线: 今日收盘 > MA250(长期趋势由空转多的分水岭)
  3. 突破确认: 今日收盘创近60日新高(突破箱体上沿)

回测兼容(M2 量能因子因回测 volume 恒 1.0 失真, 这里不用量能):
  放量突破在仅K线环境下用"突破幅度"代替——今日收盘既创60日新高又
  站上年线, 本身就是强势突破的K线证据; 真实验证时 volume 可用自动启用量能确认。
"""
from prism.registry import factor


@factor(id="M6", name="横盘突破", category="momentum",
        description="长期横盘后放量突破年线(阻力最小方向形态, 仅用K线, 回测可用)")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 251:
        return {"score": 0, "note": "K线不足250日"}
    closes = kline["close"].tolist()
    today = closes[-1]
    # 1. 长期横盘: 近60日(不含今日)振幅 < 30%
    box = closes[-61:-1]
    hi, lo = max(box), min(box)
    if lo <= 0:
        return {"score": 0, "note": "最低价为0"}
    amp = (hi - lo) / lo * 100
    if amp >= 30.0:
        return {"score": 0, "note": "近60日振幅 %.0f%%(非横盘)" % amp}
    # 2. 站上年线: 今日收盘 > MA250(以今日之前250日的均值)
    ma250 = sum(closes[-251:-1]) / 250
    if today <= ma250:
        return {"score": 0,
                "note": "未站上年线(今 %.2f <= MA250 %.2f)" % (today, ma250)}
    # 3. 突破确认: 今日收盘创近60日新高
    prev_high = max(box)
    hit = today > prev_high
    if not hit:
        return {"score": 0,
                "note": "未创新高(今 %.2f <= 60日高 %.2f)" % (today, prev_high)}
    return {"score": 1,
            "note": "横盘60日振幅%.0f%%后站上年线(MA250=%.2f)创新高" % (amp, ma250)}