# -*- coding: utf-8 -*-
"""F6 大盘配合: 上证站上20日均线或连续两日放量上涨。"""
from prism.registry import factor


@factor(id="F6", name="大盘配合", category="first_board",
        description="上证站上20日均线或连两日放量涨")
def compute(ctx):
    f6 = 0
    try:
        idx = ctx.index_kline
        if idx is not None and len(idx) >= 21:
            closes = idx["close"].tolist()
            ma20 = sum(closes[-20:]) / 20
            above = closes[-1] > ma20
            # 连续两日放量上涨
            chg = [closes[i] / closes[i - 1] - 1 for i in range(-2, 0)]
            vols = idx["volume"].tolist()[-3:]
            up2 = chg[0] > 0 and chg[1] > 0 and vols[-1] > vols[-3]
            f6 = 1 if (above or up2) else 0
    except Exception:
        f6 = 0
    return {"score": f6, "note": "上证站上20日均线或连两日放量涨" if f6 else "大盘环境一般"}
