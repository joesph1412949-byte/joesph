# -*- coding: utf-8 -*-
"""S2 量价堆积密度: 60日≥20天放量且价格窄幅震荡。

⚠️ 2026-09-19 取证结论: 本因子在"当日涨停池"宇宙里**结构性不触发**, 已从
`full_factor_v1` 的势能板块层**摘除**(因子本身仍注册, 可被其它策略/研究复用)。

证据(详见 `docs/reports/回测全因子复活对比_20260916.md` §6.2 与
`.superpowers/sdd/s2-diagnosis.md`):
- 全窗口 254 日 / 16524 次评估 **0 命中**;
- 根因一: `ctx.kline` 末根 = 决策日(涨停日), +10% 的收盘把 `(hi-lo)/lo` 抬爆
  ⇒ "振幅≤10%" 含 T 时 **0/2980**(20% 板下算术上不可能);
- 根因二: 即便改成"截止 T-1 的 60 根"(无未来函数), 交集仍是 **0/492** ——
  "涨停池 × 振幅≤10%" 与 "60 日里 ≥20 天 >1.5×均量" 量级互斥(corr=+0.32);
- 放宽阈值能救活(如 振幅≤15% + 放量≥10 天 ≈ 0.61% 命中), 但那属**策略口径
  变更**, 需独立提案 + 回测对比, 本轮未做。
"""
from prism.registry import factor


@factor(id="S2", name="量价堆积密度", category="momentum",
        description="60日≥20天放量且价格窄幅震荡")
def compute(ctx):
    kline = ctx.kline
    if kline is None or len(kline) < 60:
        return {"score": 0, "note": "量价密度不足"}
    vols = kline["volume"].tolist()[-60:]
    closes = kline["close"].tolist()[-60:]
    ma60 = sum(vols) / 60
    big = sum(1 for v in vols if v > ma60 * 1.5)
    hi, lo = max(closes), min(closes)
    narrow = (hi - lo) / lo <= 0.10 if lo > 0 else False
    s2 = 1 if (big >= 20 and narrow) else 0
    return {"score": s2, "note": "60日≥20天放量且价格窄幅震荡" if s2 else "量价密度不足"}
