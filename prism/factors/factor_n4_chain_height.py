# -*- coding: utf-8 -*-
"""N4 连板高度: 最高连板≥5 或 昨日连板晋级率>25%(东财真数据); 兜底: 涨停家数+封板率。"""
from prism.registry import factor
from shared.common import with_market_suffix


@factor(id="N4", name="连板高度", category="node",
        description="最高连板≥5 或 晋级率>25%")
def compute(ctx):
    limit_ups = ctx.limit_ups or []
    em = ctx.em or {}
    n4 = 0
    max_boards = em.get("max_boards") if em else None
    if max_boards is not None:
        # 晋级率: 昨日连板股今日仍涨停的占比(东财真数据)
        yesterday_boards = em.get("yesterday_boards") or []
        today_codes = {lu.get("code") for lu in limit_ups}
        advanced = sum(1 for c in yesterday_boards
                       if with_market_suffix(c) in today_codes)
        if yesterday_boards:
            adv_ratio = advanced / len(yesterday_boards)
        else:
            adv_ratio = 0.0
        if max_boards >= 5:
            n4 = 1
            n4_note = "最高连板 %d 板 >= 5 (东财真数据)" % max_boards
        elif adv_ratio > 0.25:
            n4 = 1
            n4_note = "昨日连板晋级率 %.0f%% > 25%% (东财真数据)" % (adv_ratio * 100)
        else:
            n4_note = ("最高连板 %d 板 < 5 且晋级率 %.0f%% (东财真数据)"
                       % (max_boards, adv_ratio * 100)) if yesterday_boards else \
                      "最高连板 %d 板 < 5 且昨日无连板股 (东财真数据)" % max_boards
    else:
        n4_note = "连板数据需历史K线, 简化以涨停家数+封板率近似"
        if len(limit_ups) >= 30:
            sealed_cnt = sum(1 for lu in limit_ups if lu.get("sealed"))
            if len(limit_ups) > 0 and sealed_cnt / len(limit_ups) > 0.6:
                n4 = 1
        n4_note = "涨停家数 %d 且封板率>60%% (兜底)" % len(limit_ups) if n4 else "连板高度不足(兜底)"
    return {"score": n4, "note": n4_note}
