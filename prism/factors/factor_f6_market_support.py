# -*- coding: utf-8 -*-
"""F6 大盘配合: 上证站上20日均线或连续两日放量上涨。"""
from prism.registry import factor


@factor(id="F6", name="大盘配合", category="first_board",
        description="上证站上20日均线或连两日放量涨")
def compute(ctx):
    f6 = 0
    note = "大盘环境一般"
    try:
        # F6 要的是上证指数日K(需 >=21 根算 MA20)。此前直接读 ctx.index_kline,
        # 而该字段装的是涨停指数 880368 且只抓 8 根 —— 长度不够且语义不对,
        # 导致 F6 恒 0。现在优先读专门的 sh_index_kline(上证指数),
        # 缺失时才退回旧字段(兜底, 语义不严谨但优于静默失效)。
        # 注意不能用 `a or b`: a 是 DataFrame 时 or 的真值测试会抛
        # ValueError, 被下面的 except 吞掉后又变回恒 0(factor_check 实测抓到)。
        idx = ctx.get("sh_index_kline")
        if idx is None:
            idx = ctx.index_kline
        if idx is None:
            note = "缺大盘指数数据(F6 未生效)"
        elif len(idx) < 21:
            note = "大盘K线不足21根(仅%d根, F6 未生效)" % len(idx)
        else:
            closes = idx["close"].tolist()
            ma20 = sum(closes[-20:]) / 20
            above = closes[-1] > ma20
            # 连续两日放量上涨
            chg = [closes[i] / closes[i - 1] - 1 for i in range(-2, 0)]
            vols = idx["volume"].tolist()[-3:]
            up2 = chg[0] > 0 and chg[1] > 0 and vols[-1] > vols[-3]
            f6 = 1 if (above or up2) else 0
            note = "上证站上20日均线或连两日放量涨" if f6 else "大盘环境一般"
    except Exception:
        f6 = 0
    return {"score": f6, "note": note}
