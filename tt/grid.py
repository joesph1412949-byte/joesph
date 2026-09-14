# -*- coding: utf-8 -*-
"""网格引擎(纯逻辑, 零 IO, 零副作用)。

职责: 把"前收 + 波动率 + 档位数"翻译成一组挂单价, 并把当前行情翻译成
"应该成交几档"。所有函数都是纯函数, 便于单测与回放。

## 为什么是"日重置网格"

实盘做T的收益来自日内价差, 而非方向。若网格中枢跟着价格漂移, 就退化成
趋势跟踪(在趋势年份必然负超额, 见 analysis 里的分区间回测)。因此:
    ref 固定为当日参考价(默认前收), 当日不再变动;
    档位价 = ref × (1 ± 单档间距 × 档序号)。

## 为什么单档间距 = 波动率 × k

间距必须显著大于往返成本(约 0.08%~0.12%), 否则每次成交净亏。
用日波动率作为尺度, 让"每档被触发的频率"在不同标的上保持一致;
长电(日波动 ~0.9%)与海油A(日波动 ~2.5%)的挂单密度因此可比。

## 成交模型: 穿越全成交

真实挂单是"每档一张限价单"。价格从 ref 冲到 sell[3] 的过程中,
sell[1]/sell[2]/sell[3] 依次成交。故用 high/low 判定穿越了几档,
而不是只看收盘价 —— 后者会系统性漏掉日内冲高回落的机会。
"""
import math

# 开关状态(与 switch_state 返回值一致)
ENABLED = "ENABLED"      # 正常做T
HALF = "HALF"            # 半量(价格已在均线上方, 趋势倾向)
DISABLED = "DISABLED"    # 停做(趋势加速段)

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"


def daily_sigma(closes, window=60):
    """近 window 根收盘价的日对数收益标准差(小数, 未年化)。

    closes 需升序(旧→新)。不足 3 根 / 有非正价 → 返回 None(调用方降级)。
    """
    if not closes or len(closes) < 3:
        return None
    seg = [float(c) for c in closes[-window:]] if len(closes) > window \
        else [float(c) for c in closes]
    if any(c <= 0 for c in seg):
        return None
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg))]
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    # 浮点噪声会产生 ~1e-16 的"假波动"(恒定收益序列); 低于该阈值视为无波动。
    # 真日波动率量级在 1e-3 ~ 1e-1, 阈值不会误伤。
    return sd if sd > 1e-9 else None


def trend_degree(closes, window=20):
    """近 window 日趋势度 = |区间涨跌| / Σ|日涨跌| ∈ [0, 1]。

    越小越震荡(适合做T), 越大越单边。做T超额与该指标负相关。
    数据不足 → None。
    """
    if not closes or len(closes) < 3:
        return None
    seg = [float(c) for c in closes[-(window + 1):]]
    if len(seg) < 3 or any(c <= 0 for c in seg):
        return None
    total = sum(abs(math.log(seg[i] / seg[i - 1])) for i in range(1, len(seg)))
    if total <= 0:
        return None
    return abs(seg[-1] / seg[0] - 1) / total


def ma(closes, window):
    """简单均线; 数据不足 → None。"""
    if not closes or len(closes) < window:
        return None
    return sum(float(c) for c in closes[-window:]) / window


def switch_state(close, ma20, ma20_prev, r20_pct, switch_cfg):
    """做T启停开关。

    close/ma20/ma20_prev: 价格与均线(ma20_prev 用于算斜率, 建议取 T-5 的 MA20)。
    r20_pct: 近 20 日涨跌幅(百分数, 如 10.18 表示 +10.18%)。
    switch_cfg: {dev_max_pct, slope_max_pct, r20_max_pct}。

    返回 ENABLED / HALF / DISABLED。
    任一输入缺失 → DISABLED(fail-closed: 数据不全时宁可不做)。
    """
    try:
        close, ma20, ma20_prev = float(close), float(ma20), float(ma20_prev)
        r20_pct = float(r20_pct)
        cfg = switch_cfg or {}
        dev_max = float(cfg.get("dev_max_pct", 4.0))
        slope_max = float(cfg.get("slope_max_pct", 0.3))
        r20_max = float(cfg.get("r20_max_pct", 8.0))
    except (TypeError, ValueError):
        return DISABLED
    if close <= 0 or ma20 <= 0 or ma20_prev <= 0:
        return DISABLED

    dev_pct = (close / ma20 - 1) * 100.0
    slope_pct = (ma20 / ma20_prev - 1) * 100.0

    # 趋势加速段: 偏离大 + 均线上斜 + 区间涨幅大 → 停做
    if dev_pct >= dev_max and slope_pct > slope_max and r20_pct > r20_max:
        return DISABLED
    # 价格在均线上方且均线上斜 → 半量
    if close > ma20 and slope_pct > slope_max:
        return HALF
    # 价格显著跌破均线: 属下跌趋势, 也不宜越跌越买
    if dev_pct <= -dev_max and slope_pct < -slope_max:
        return DISABLED
    return ENABLED


def band_of(symbol_cfg, sigma, band_k=1.0, band_mode="sigma"):
    """单档间距(小数)。symbol_cfg.band_pct 优先(百分数), 否则 sigma × k。

    全部缺失/非法 → None(该标的当日跳过)。
    """
    try:
        pct = float((symbol_cfg or {}).get("band_pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if pct > 0:
        band = pct / 100.0
    elif band_mode == "sigma" and sigma and sigma > 0:
        band = float(sigma) * float(band_k)
    else:
        return None
    # 单档间距过小(< 0.05%)几乎必然被成本吃掉; 过大(> 15%)当日不可能触发
    if not (0.0005 <= band <= 0.15):
        return None
    return band


def build_ladder(ref, band, n_units):
    """构建挂单阶梯。

    返回 {'sell': [sell1..sellN], 'buy': [buy1..buyN], 'ref': ref, 'band': band}
    sell 升序(sell1 最近), buy 降序(buy1 最近)。ref/参数非法 → None。
    """
    try:
        ref, band, n_units = float(ref), float(band), int(n_units)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or band <= 0 or n_units < 1:
        return None
    sell = [round(ref * (1 + band * i), 3) for i in range(1, n_units + 1)]
    buy = [round(ref * (1 - band * i), 3) for i in range(1, n_units + 1)]
    if buy[-1] <= 0:
        return None
    return {"ref": ref, "band": band, "n_units": n_units,
            "sell": sell, "buy": buy}


def crossed_sell(ladder, high):
    """当日最高价穿越的卖出档数(0..n)。"""
    if not ladder or high is None:
        return 0
    try:
        high = float(high)
    except (TypeError, ValueError):
        return 0
    return sum(1 for p in ladder["sell"] if high >= p)


def crossed_buy(ladder, low):
    """当日最低价穿越的买入档数(0..n)。"""
    if not ladder or low is None:
        return 0
    try:
        low = float(low)
    except (TypeError, ValueError):
        return 0
    return sum(1 for p in ladder["buy"] if low <= p)


def ladder_price(ladder, side, units):
    """取第 units 档的挂单价(units 从 1 起)。越界 → None。"""
    if not ladder or units < 1:
        return None
    arr = ladder.get(side.lower() if side.lower() in ("buy", "sell")
                     else ("sell" if side == SIDE_SELL else "buy"))
    if not arr or units > len(arr):
        return None
    return arr[units - 1]


def target_units(ladder, high, low, side):
    """当前行情下, 该方向"累计应成交的档数"。"""
    return crossed_sell(ladder, high) if side == SIDE_SELL \
        else crossed_buy(ladder, low)


def half_scale(state):
    """开关状态 → 档位缩放系数。ENABLED=1, HALF=0.5, DISABLED=0。"""
    if state == ENABLED:
        return 1.0
    if state == HALF:
        return 0.5
    return 0.0
