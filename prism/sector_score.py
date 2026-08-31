# -*- coding: utf-8 -*-
"""板块综合评分(0-100): 动量35% + 资金流25% + 拥挤度30% + 宏观10%。

设计文档: docs/superpowers/specs/2026-08-31-sector-score-chain-design.md
- 纯计算模块: 只依赖 mkt 切片(dict), 不碰网络/缓存/registry。
- 防未来函数: 调用方必须传 _slice_mkt(mkt, asof) 之后的切片(只见当日及之前)。
- 缺数据降级: 分项 None → 权重按剩余分项比例归一化; 全缺 → 无评分(fail-closed)。
- 门槛严格大于: score > threshold(默认75)才可买; 无评分不买。
"""
DEFAULT_WEIGHTS = {"momentum": 0.35, "flow": 0.25,
                   "crowding": 0.30, "macro": 0.10}
DEFAULT_THRESHOLD = 75.0
DEFAULT_STEP = 0.05
DEFAULT_CAP_RATIO = None


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _momentum_part(rec):
    """板块K线 {close} → 动量分或 None。r5(6根)/r10(11根) 各半。"""
    closes = [c for c in (rec.get("close") or []) if c]
    subs = []
    if len(closes) >= 6:
        r5 = (closes[-1] / closes[-6] - 1.0) * 100.0
        subs.append(_clamp(r5 / 6.0 * 100.0))
    if len(closes) >= 11:
        r10 = (closes[-1] / closes[-11] - 1.0) * 100.0
        subs.append(_clamp(r10 / 10.0 * 100.0))
    if not subs:
        return None
    return sum(subs) / len(subs)


def _flow_part(rec):
    """资金流 {main_net_in} → 资金流分或 None。近5日合计, +5亿→100。"""
    vals = [v for v in (rec.get("main_net_in") or [])[-5:]
            if v is not None]
    if not vals:
        return None
    yi = sum(vals) / 1e8
    return _clamp(50.0 + 10.0 * yi)


def _crowding_part(rec):
    """板块K线 {amount} → 拥挤度分(反向)或 None。

    近5日均额 ÷ 基线均额, 基线不含近5日(与 SEC4 factor_sec4_sector_crowding
    同口径): ≥245日取近240日基线, 否则用全部剩余天。"""
    amounts = [a for a in (rec.get("amount") or []) if a]
    if len(amounts) < 10:
        return None
    recent = amounts[-5:]
    numer = sum(recent) / len(recent)
    base = amounts[-245:-5] if len(amounts) >= 245 else amounts[:-5]
    denom = sum(base) / len(base)
    if not denom:
        return None
    ratio = numer / denom
    return _clamp(100.0 - max(0.0, ratio - 1.0) * 40.0)


def _macro_part(glob):
    """global 段 {NDX/US10Y/VIX: {close}} → 宏观分(三者均)或 None。"""
    subs = []
    ndx = [c for c in ((glob.get("NDX") or {}).get("close") or []) if c]
    if len(ndx) >= 2 and ndx[-2]:
        pct = (ndx[-1] / ndx[-2] - 1.0) * 100.0
        subs.append(_clamp(50.0 + pct * 50.0))
    us = [c for c in ((glob.get("US10Y") or {}).get("close") or []) if c]
    if len(us) >= 21:
        # 百分点差值, 勿 ×100! round 消浮点残差(5.3-4.0=1.2999...8 → 0分应恰为0)
        chg = round(us[-1] - us[-21], 10)
        subs.append(_clamp(100.0 - max(0.0, chg - 0.3) * 100.0))
    vix = [c for c in ((glob.get("VIX") or {}).get("close") or []) if c]
    if vix:
        subs.append(_clamp(100.0 - max(0.0, vix[-1] - 20.0) * 20.0))
    if not subs:
        return None
    return sum(subs) / len(subs)


def compute_scores(mkt):
    """mkt 切片 → {板块码: {"score": float, "parts": {...}}}。

    分项缺数据 → 剔除并把权重按剩余项比例归一化; 全缺 → 该板块无评分
    (不进返回 dict, 调用方 fail-closed 不买)。"""
    if not isinstance(mkt, dict) or not mkt:
        return {}
    sectors = mkt.get("sector") or {}
    flows = mkt.get("sector_flow") or {}
    glob = mkt.get("global") or {}
    macro = _macro_part(glob)
    out = {}
    for code, rec in sectors.items():
        rec = rec or {}
        parts = {"momentum": _momentum_part(rec),
                 "flow": _flow_part(flows.get(code) or {}),
                 "crowding": _crowding_part(rec),
                 "macro": macro}
        avail = {k: v for k, v in parts.items() if v is not None}
        if not avail:
            continue
        wsum = sum(DEFAULT_WEIGHTS[k] for k in avail)
        score = sum(avail[k] * DEFAULT_WEIGHTS[k] for k in avail) / wsum
        out[str(code)] = {"score": round(score, 1),
                          "parts": {k: (round(v, 1) if v is not None
                                        else None)
                                    for k, v in parts.items()}}
    return out


def load_config(strategy):
    """strategy dict → 规范化评分配置(缺省关闭)。"""
    raw = (strategy or {}).get("sector_score") or {}
    pos = raw.get("position") or {}
    cap = pos.get("cap_ratio")
    return {"enabled": bool(raw.get("enabled")),
            "threshold": float(raw.get("threshold", DEFAULT_THRESHOLD)),
            "step": float(pos.get("step", DEFAULT_STEP)),
            "cap_ratio": float(cap) if cap is not None else None}


def position_multiplier(score, cfg, base_ratio):
    """score → 单笔仓位乘数(相对 position_ratio)。

    每高 threshold 10 分相对 +step; 封顶 cap_ratio/base_ratio(绝对仓位上限);
    score None 或 ≤ threshold → 0.0(不该买)。评分关闭时调用方须用 1.0。"""
    threshold = cfg.get("threshold", DEFAULT_THRESHOLD)
    if score is None or score <= threshold:
        return 0.0
    mult = 1.0 + (score - threshold) / 10.0 * cfg.get("step", DEFAULT_STEP)
    cap = cfg.get("cap_ratio")
    if cap and base_ratio > 0:
        mult = min(mult, cap / base_ratio)
    return round(mult, 10)   # round 消浮点残差(0.21/0.20 → 1.049999...8)
