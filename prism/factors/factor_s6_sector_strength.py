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
1% 门槛的推导链(不用"经验值"): 同层累计阈值折算**日均** ——
  * SEC6 门槛 = 板块近5日涨幅 > 2%  → 2/5  = 0.4%/日
  * SEC2 门槛 = 板块近10日累计 > 5% → 5/10 = 0.5%/日
  当日 1% ≈ 该日均节奏的 2~2.5 倍 ⇒ 语义是"单日显著走强"而非日常波动。
  (更正: 早先草稿把这一段写成"≈1%/日", 是算错的 —— 2/5 与 5/10 都不是 1%/日。)
  必须带量级门槛: 仅"当日>0"会完全包含 SEC1(板块连阳≥3 ⇒ 今日收阳) → 同层冗余。
数据缺失 → fail-closed 0(**不**退回旧的 sector_count, 否则缺口处又变回 F4)。

独立性实测(2026-06-01~09-18, 真实候选 6255 家, 只读探针; 回测内 evals=6217 同向):
  命中率 27.7%; P(F4|S6)=85.8% vs F4 基线 80.9% → lift 1.06x(≈独立);
  Jaccard(F4,S6)=28.0%。注意 F4 本身在该候选域命中 80.9%(涨停池宇宙),
  故"重叠率"必须对照基线读, 不能只看 85.8%。

副作用(单一变量对照实测, 候选 6217 不变): 资质线 filters.candidate_min_model=3 吃的是
**原始命中数**(不是加权 composite —— 见 prism/engine.py _compute_scores 与
test_engine_weights.py::test_candidate_min_model_ignores_layer_weights);
S6 命中率 81%→28% ⇒ 势能层更难凑够 3 分 ⇒ filtered_min_model 959→1274(+315)。
该副作用与 composite 模式无关: weighted_sum 与 average 下同为 1274(候选数据逐因子相同)。
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
