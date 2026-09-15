# -*- coding: utf-8 -*-
"""N3 首板溢价: 昨日涨停股今日平均涨幅>0(东财真数据); 兜底: 今日涨停池均价。"""
from prism.registry import factor
from shared.common import with_market_suffix


@factor(id="N3", name="首板溢价", category="node",
        description="昨日涨停股今日均涨>0")
def compute(ctx):
    limit_ups = ctx.limit_ups or []
    em = ctx.em or {}
    ticks = ctx.get("ticks") or {}
    n3 = 0
    avg_chg = 0.0
    yesterday_codes = em.get("yesterday_codes") or []
    if yesterday_codes:
        chgs = []
        for code in yesterday_codes:
            # 东财返回裸6位代码(如 "002859"), 而 QMT ticks 键带交易所后缀("002859.SZ"),
            # 必须按前缀补后缀再查, 否则实盘永远命中不了 → N3 恒 0。找不到的代码跳过。
            key = with_market_suffix(code)
            t = ticks.get(key) or ticks.get(code)
            if not t:
                continue
            last = t.get("lastPrice") or 0
            last_close = t.get("lastClose") or 0
            if last > 0 and last_close > 0:
                chgs.append((last / last_close - 1) * 100)
        if chgs:
            avg_chg = sum(chgs) / len(chgs)
            n3 = 1 if avg_chg > 0 else 0
            n3_note = "昨日涨停股今日均涨 %.2f%% (东财真数据)" % avg_chg if n3 else \
                      "昨日涨停股今日均涨 %.2f%% (东财真数据, 溢价为负)" % avg_chg
        else:
            n3_note = "昨日涨停股(%d只)今日无匹配行情 (东财真数据)" % len(yesterday_codes)
    else:
        chgs = []
        try:
            for lu in limit_ups:
                lc = lu.get("last_close") or 0
                last = lu.get("last") or 0
                if lc > 0:
                    chgs.append((last / lc - 1) * 100)
            if chgs:
                avg_chg = sum(chgs) / len(chgs)
                n3 = 1 if avg_chg > 0 else 0
        except Exception:
            n3 = 0
        n3_note = "首板股今日均涨 %.2f%% (兜底)" % avg_chg if chgs else "首板溢价为负(兜底)"
    return {"score": n3, "note": n3_note}
