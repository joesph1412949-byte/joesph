# -*- coding: utf-8 -*-
"""N1 涨停指数: 今日涨停>近5日均值(东财真数据); 兜底: 涨停指数(880368)站上5日线。"""
from prism.registry import factor


@factor(id="N1", name="涨停指数", category="node",
        description="今日涨停>近5日均值 或 涨停指数站上5日线(兜底)")
def compute(ctx):
    limit_ups = ctx.limit_ups or []
    em = ctx.em or {}
    n1 = 0
    daily_counts = em.get("daily_counts") or []
    if len(daily_counts) >= 3:
        mean5 = sum(daily_counts) / len(daily_counts)
        n1 = 1 if len(limit_ups) > mean5 else 0
        n1_note = "今日涨停 %d > 近5日均值 %.1f (东财真数据)" % (len(limit_ups), mean5) if n1 else \
                  "今日涨停 %d <= 近5日均值 %.1f (东财真数据)" % (len(limit_ups), mean5)
    else:
        try:
            idx = ctx.index_kline
            if idx is not None and len(idx) >= 6:
                vals = idx["close"].tolist()
                n1 = 1 if vals[-1] > sum(vals[-6:-1]) / 5 else 0
                n1_note = "涨停指数在5日线上方(兜底)" if n1 else "涨停指数走弱(兜底)"
            else:
                n1 = 1 if len(limit_ups) > 0 else 0  # 兜底
                n1_note = "今日涨停 %d 家(兜底)" % len(limit_ups)
        except Exception:
            n1 = 1 if len(limit_ups) > 0 else 0
            n1_note = "今日涨停 %d 家(兜底)" % len(limit_ups)
    return {"score": n1, "note": n1_note}
