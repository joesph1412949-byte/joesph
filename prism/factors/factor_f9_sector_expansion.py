# -*- coding: utf-8 -*-
"""F9 板块延展性: 所属板块"有高度"且"在扩张" → 1 分(设计 §3.2)。

口径(设计修正, 已获用户确认): "题材"统一用申万板块(sector_map)代理——
回测历史池无 hybk 题材字段, 申万口径 live/backtest 一致、全窗口可回测
(F4 板块共振同口径)。昨日池由 mkt["zt_prev"]["codes"] 注入(回测 Task 4 /
实盘 Task 7 接线)。
格式契约(Task 5 审查 Minor#1): zt_prev["codes"] 条目与 sector_map 键必须
同格式(带 .SH/.SZ 后缀的完整代码), 否则 sector_map.get(c) 全 miss →
prev_n 恒 0 → "今日≥昨日"恒真(扩张误判)。prev_day_pool 直接透传
zt_history 缓存条目 code(本就是 QMT 带后缀格式), data.build_market_context
注入 sector_map 时按首位补 .SH/.SZ 后缀, 两端一致。
有高度: 板块内当日涨停 ≥3 家 且 连板股(boards≥2) ≥2 只;
在扩张: 板块今日涨停家数 ≥ 昨日。
昨日池缺失 → fail-closed 0(无昨日基准无法判定扩张)。"""
from prism.registry import factor
from prism._utils import sector_count


@factor(id="F9", name="板块延展性", category="sector",
        description="板块有高度(涨停≥3且连板≥2)且扩张(今日≥昨日)")
def compute(ctx):
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    ups = ctx.limit_ups or []
    prev = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes") or []
    if not prev:
        return {"score": 0, "note": "昨日涨停池缺失"}
    today_n = sector_count(ctx.code or "", sector_map, ups)
    if today_n < 3:
        return {"score": 0, "note": "板块涨停%d家(<3)" % today_n}
    lb = sum(1 for c in ups
             if sector_map.get(c.get("code")) == scode
             and (c.get("boards") or 0) >= 2)
    if lb < 2:
        return {"score": 0, "note": "板块连板%d只(<2)" % lb}
    prev_n = sum(1 for c in prev if sector_map.get(c) == scode)
    if today_n < prev_n:
        return {"score": 0,
                "note": "今日%d家<昨日%d家 不扩张" % (today_n, prev_n)}
    return {"score": 1,
            "note": "板块涨停%d家 连板%d只 昨日%d家" % (today_n, lb, prev_n)}
