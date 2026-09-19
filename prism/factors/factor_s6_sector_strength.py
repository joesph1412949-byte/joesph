# -*- coding: utf-8 -*-
"""S6 板块当日走强: 所属板块指数当日涨幅 ≥ 1%。

2026-09-19 用户拍板"给 S6 自由度"。改前本因子与 F4(factor_f4_sector_resonance)
表达式**逐字相同** —— 都是 _utils.sector_count(所属板块当日涨停家数 ≥ 3)。而 F4 属
首板层、S6 属势能层, 同一信号给两层各加一分(双重计数); 且本文件声明的语义是
"板块共振强度/走强", 实际数的却是涨停家数 —— 名实不符。回测实证: 改前同窗口
F4 命中 5041 次、S6 命中 5041 次(hits 逐位相同)。

改后口径: **板块整体当日走强** = 所属板块指数当日涨幅 ≥ 1%。
数据通路: ctx._extra["mkt"]["sector"][板块代码]["close"] —— 市场数据层已注入且按
asof 切片(防未来), 与 SEC1/SEC2/SEC4/SEC6 同源; 未新增取数/依赖/字段。
1% 与同层已有阈值同档(SEC6 板块5日>2%、SEC2 板块10日>5% ≈ 1%/日); 必须带量级
门槛: 仅"当日>0"会完全包含 SEC1(板块连阳≥3 ⇒ 今日收阳) → 变成同层冗余。
数据缺失 → fail-closed 0(**不**退回旧的 sector_count, 否则缺口处又变回 F4)。

独立性实测(2026-06-01~09-18, 真实候选 6255 家, 只读探针; 回测内 evals=6217 同向):
  命中率 27.7%; P(F4|S6)=85.8% vs F4 基线 80.9% → lift 1.06x(≈独立);
  Jaccard(F4,S6)=28.0%。注意 F4 本身在该候选域命中 80.9%(涨停池宇宙),
  故"重叠率"必须对照基线读, 不能只看 85.8%。
"""
from prism.registry import factor


@factor(id="S6", name="板块当日走强", category="momentum",
        description="所属板块指数当日涨幅≥1%(需市场数据注入)")
def compute(ctx):
    mkt = ctx.get("mkt")
    if not isinstance(mkt, dict):
        # None / DataFrame 等脏数据: 显式判类型。本仓踩过 ctx.get(x) or y 在
        # DataFrame 上真值测试抛 ValueError、异常被上层吞成恒 0 的坑。
        mkt = {}
    scode = (ctx.sector_map or {}).get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    rec = (mkt.get("sector") or {}).get(scode)
    if not rec or not rec.get("dates"):
        return {"score": 0, "note": "板块K线缺失(%s)" % scode}
    closes = rec.get("close") or []
    if len(closes) < 2:
        return {"score": 0, "note": "板块K线不足"}
    base = closes[-2]
    if not base or base <= 0:
        return {"score": 0, "note": "板块前收为0"}
    chg = (closes[-1] / base - 1) * 100
    hit = chg >= 1.0
    return {"score": 1 if hit else 0,
            "note": "板块%s当日涨%.2f%%" % (scode, chg) if hit
            else "板块%s当日涨%.2f%%(<1%%)" % (scode, chg)}
