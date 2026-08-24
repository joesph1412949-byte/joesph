# -*- coding: utf-8 -*-
"""N7 美债平稳: 10年美债近20日上行 ≤ 0.3% → 流动性平稳(门槛正向语义)。

文档宏观因子: 美债上行 → 压制A股成长板块估值 → 流动性收紧=环境差。
在门槛框架里表达为正向: score=1 表示美债平稳(20日上行≤0.3%可做多),
美债快速上行 >0.3% 时 score=0(门槛不通过→观望)。
数据在 ctx._extra["mkt"]["global"]["US10Y"](FRED DGS10)。缺失 → fail-open 0。
"""
from prism.registry import factor


@factor(id="N7", name="美债平稳", category="node",
        description="10Y美债20日上行≤0.3% 流动性平稳(需FRED注入)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    rec = (mkt.get("global") or {}).get("US10Y")
    if not rec or not rec.get("dates"):
        return {"score": 0, "note": "美债数据缺失"}
    closes = rec.get("close") or []
    if len(closes) < 21:
        return {"score": 0, "note": "美债K线不足20日"}
    today = closes[-1]
    base = closes[-21]
    if today is None or base is None:
        return {"score": 0, "note": "美债数据无效"}
    # US10Y 值即百分比(4.4=4.4%), 20日变化直接用差值(百分点)
    diff = today - base
    hit = diff <= 0.3
    return {"score": 1 if hit else 0,
            "note": "美债20日变化 %+.2f个百分点" % diff}