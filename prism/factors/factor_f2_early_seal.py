# -*- coding: utf-8 -*-
"""F2 早封板: 封板时间≤10:00。"""
from prism.registry import factor
from prism._utils import _parse_timetag_hhmm


@factor(id="F2", name="早封板", category="first_board",
        description="封板时间≤10:00")
def compute(ctx):
    if not ctx.sealed:
        return {"score": 0, "note": "未封板或封板时间晚于10:00"}
    hm = _parse_timetag_hhmm((ctx.tick or {}).get("timetag"))
    f2 = 1 if hm is not None and hm <= (10, 0) else 0
    return {"score": f2, "note": "封板时间≤10:00" if f2 else "未封板或封板时间晚于10:00"}
