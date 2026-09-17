# -*- coding: utf-8 -*-
"""F3 封单强度: 封单金额 ≥ 流通市值 × 0.5%(主板) / 0.2%(创业科创)。

回测代理(规格 2026-09-16 §5): 回测无盘口队列(分笔/五档不可得, 不造假) →
注入 bt_seal_ratio(板上成交额/流通市值)时改判对偶量"板上额占比 ≤ T",
note 标"回测代理"; ctx 无该键时保持实盘口径(bidVol 队列)不变。
"""
from prism.registry import factor

# 回测代理阈值 T: 板上成交额/流通市值 ≤ T = "封得结实"。
# 实盘口径是队列厚度 bidVol[0]×100×涨停价 ≥ 流通市值×(0.5%|0.2%); 回测拿不到
# 队列 → 用可观测的对偶量: 板上成交额占比越小 = 队列越结实。T 初值 0.5% 与
# 实盘主板阈值同量纲(创业科创 0.2% 暂不分档, 单一 T 先跑通)。
# ponytail: 校准法 = 上线后拿最近有实时数据的交易日, 回放同日 bt_seal_ratio
# 分布对照真 F3(bidVol) 命中率调 T; 校准后只改这一个常量。
BT_SEAL_RATIO_T = 0.005


@factor(id="F3", name="封单强度", category="first_board",
        description="封单≥流通市值0.5%(主板)/0.2%(创业科创)")
def compute(ctx):
    if not ctx.sealed or not ctx.up_price:
        return {"score": 0, "note": "封单不足"}
    # 回测代理分支: 只判 is None(0.0 也是合法值 —— 未封死时板上额可为 0,
    # 不能用 or 兜底, 否则 0.0 会被误当"未注入"而走实盘口径)。
    ratio = ctx.get("bt_seal_ratio")
    if ratio is not None:
        f3 = 1 if ratio <= BT_SEAL_RATIO_T else 0
        if f3:
            note = "板上成交额/流通市值≤%.1f%% 封得结实(回测代理)" % (
                BT_SEAL_RATIO_T * 100)
        else:
            note = "板上成交额占比%.2f%%>%.1f%% 封单弱(回测代理)" % (
                ratio * 100, BT_SEAL_RATIO_T * 100)
        return {"score": f3, "note": note}
    float_vol = ctx.get("float_vol") or 0
    if float_vol <= 0:
        return {"score": 0, "note": "封单不足"}
    tick = ctx.tick or {}
    bid0 = (tick.get("bidPrice") or [0])[0]
    bidv0 = (tick.get("bidVol") or [0])[0]
    # bidVol 单位=手(xtdata 官方示例实证) → ×100 折股 → 封单金额(元)
    seal_amount = bid0 * bidv0 * 100
    float_mv = ctx.up_price * float_vol
    code = ctx.code or ""
    ratio = 0.005 if not code.startswith(("300", "301", "688")) else 0.002
    f3 = 1 if seal_amount >= float_mv * ratio else 0
    return {"score": f3,
            "note": "封单≥流通市值0.5%(主板)/0.2%(创业科创)" if f3 else "封单不足"}
