# -*- coding: utf-8 -*-
"""N8 VIX 平稳: VIX ≤ 20 → 市场情绪平稳(门门槛正向语义)。

文档"市场情绪因子": VIX>25 恐慌触发A股防御模式(该模式=观望/降仓)。
在门槛框架里表达"防御"= 环境差时门槛不通过 → 本因子设计为正向:
score=1 表示 VIX 平稳(≤20, 可积极做多), VIX>25 时 score=0(不通过→观望)。
数据在 ctx._extra["mkt"]["global"]["VIX"](FRED VIXCLS)。缺失 → fail-open 0。
"""
from prism.registry import factor


@factor(id="N8", name="VIX平稳", category="node",
        description="VIX≤20 情绪平稳可做多, VIX>25 恐慌触发防御(需FRED VIX注入)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    rec = (mkt.get("global") or {}).get("VIX")
    if not rec or not rec.get("dates"):
        return {"score": 0, "note": "VIX数据缺失"}
    closes = rec.get("close") or []
    if not closes:
        return {"score": 0, "note": "VIX数据为空"}
    vix = closes[-1]
    if vix is None:
        return {"score": 0, "note": "VIX数据无效"}
    hit = vix <= 20.0
    if vix > 25.0:
        note = "VIX=%.1f 恐慌(防御模式, 门槛不通过)" % vix
    elif hit:
        note = "VIX=%.1f 平稳" % vix
    else:
        note = "VIX=%.1f 偏高" % vix
    return {"score": 1 if hit else 0, "note": note}