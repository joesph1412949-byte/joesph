# -*- coding: utf-8 -*-
"""N6 美股隔夜映射: 昨日纳指涨跌幅 ≥ 1% 视为外围偏暖。

数据通路: ctx._extra["mkt"]["global"]["NDX"] = {"dates": [...], "close": [...]}
(回测/实盘注入; asof 切片已保证只见当日及之前)。数据缺失 → fail-open 0。
"""
from prism.registry import factor


@factor(id="N6", name="美股映射", category="node",
        description="昨日纳指涨跌幅≥1% 外围偏暖(需全球指数注入)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    rec = (mkt.get("global") or {}).get("NDX")
    if not rec or not rec.get("dates"):
        return {"score": 0, "note": "纳指数据缺失"}
    closes = rec.get("close") or []
    if len(closes) < 2:
        return {"score": 0, "note": "纳指K线不足"}
    prev = closes[-2]
    if not prev or prev <= 0:
        return {"score": 0, "note": "前收为0"}
    chg = (closes[-1] / prev - 1) * 100
    hit = chg >= 1.0
    return {"score": 1 if hit else 0,
            "note": "纳指昨日涨幅 %.2f%%" % chg}