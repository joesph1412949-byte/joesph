# -*- coding: utf-8 -*-
"""策略引擎: 读策略配置 → 算因子 → 模型分 → 综合分 → 过滤排序。

与旧 screen.py 的输出结构保持同构, 网页/绩效/桥无缝对接。
"""
import json
from pathlib import Path

from prism import registry as reg
from prism.context import FactorContext
from prism import sector_score

# 综合分组合方式
composite_modes = {
    "top3_weighted": lambda scores, cfg: _top3_weighted(scores, cfg),
    "sum": lambda scores, cfg: sum(scores),
    "max": lambda scores, cfg: max(scores) if scores else 0.0,
    "average": lambda scores, cfg: (sum(scores) / len(scores)) if scores else 0.0,
}

GRADE_RULES = [
    (6.0, None, "A"), (5.0, 3.0, "B"), (4.0, None, "C"), (3.0, None, "D"),
]
STRENGTH_RULES = [(6.0, "极强", "仓位上限75%"), (5.0, "强", "仓位上限50%"),
                  (4.0, "中等", "仓位上限30%"), (0.0, "弱", "观察/空仓")]


def _top3_weighted(scores, cfg):
    weights = cfg.get("weights") or [0.60, 0.25, 0.15]
    cap = cfg.get("cap") or 7.0
    ordered = sorted(scores, reverse=True)
    total = sum(ordered[i] * weights[i] for i in range(min(len(ordered), len(weights))))
    return min(total, cap)


def load_strategy(path_or_dict):
    """加载并校验策略配置。path_or_dict: JSON 文件路径或 dict。"""
    if isinstance(path_or_dict, (str, Path)):
        p = Path(path_or_dict)
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = path_or_dict
    if not data.get("id"):
        raise ValueError("策略缺少 id")
    if not data.get("scoring_models"):
        raise ValueError("策略缺少 scoring_models")
    # 校验因子存在性(市场门槛 + 各模型)
    gate = data.get("market_gate") or {}
    for fid in gate.get("factors", []):
        reg.get_factor(fid)
    for m in data["scoring_models"]:
        for item in m.get("factors", []):
            fid = item["id"] if isinstance(item, dict) else item
            reg.get_factor(fid)
    # 校验组合模式
    mode = (data.get("composite") or {}).get("mode", "top3_weighted")
    if mode not in composite_modes:
        raise ValueError("未知组合模式: %s" % mode)
    return data


def _factor_entry(item):
    """因子条目 → (fid, weight, op, threshold)。支持简写/加权/阈值三种形态。"""
    if isinstance(item, str):
        return item, 1.0, None, None
    fid = item["id"]
    return (fid, item.get("weight", 1.0), item.get("op"), item.get("threshold"))


def _factor_hit(ctx, fid, op, threshold):
    """算单因子并按阈值判定是否命中。返回 (raw_score, hit)。"""
    meta = reg.get_factor(fid)
    try:
        res = meta["func"](ctx)
    except Exception:
        res = {"score": 0, "note": "异常"}
    if not isinstance(res, dict):
        return 0, False
    score = res.get("score", 0) or 0
    if op and threshold is not None:
        try:
            hit = {"<": score < threshold, "<=": score <= threshold,
                   ">": score > threshold, ">=": score >= threshold,
                   "==": score == threshold}[op]
        except KeyError:
            hit = score > 0
        except TypeError:
            # 审查 Minor: threshold 类型错误(如 "abc")→ 视为未命中, 不逃逸
            # fail-open(旧实现同类错误会让网页 500)。
            return score, False
        return score, hit
    return score, score > 0


def _compute_scores(ctx, strategy):
    """内部: 算各模型分 + 综合分 + 分级 + 强弱, 返回 (scores_dict, factors_dict)。"""
    model_scores = {}
    factors_out = {}
    for m in strategy["scoring_models"]:
        total = 0.0
        for item in m.get("factors", []):
            fid, weight, op, threshold = _factor_entry(item)
            raw, hit = _factor_hit(ctx, fid, op, threshold)
            factors_out[fid] = 1 if hit else 0
            if hit:
                total += weight
        model_scores[m["id"]] = total
    scores = [model_scores[m["id"]] for m in strategy["scoring_models"]]
    comp_cfg = strategy.get("composite") or {}
    mode = comp_cfg.get("mode", "top3_weighted")
    composite = composite_modes[mode](scores, comp_cfg)
    best = max(scores) if scores else 0
    second = sorted(scores, reverse=True)[1] if len(scores) > 1 else 0
    grade = "E"
    for min_best, min_second, g in GRADE_RULES:
        if best >= min_best and (min_second is None or second >= min_second):
            grade = g
            break
    strength, position = "弱", "观察/空仓"
    for min_best, st, pos in STRENGTH_RULES:
        if best >= min_best:
            strength, position = st, pos
            break
    out = dict(model_scores)
    out.update({"composite": round(composite, 2), "grade": grade,
                "strength": strength, "position": position})
    return out, factors_out


def compute_model_scores(ctx, strategy):
    """对单股: 各模型分(加权因子命中数) + 综合分 + 分级 + 强弱, 返回 dict。"""
    out, _ = _compute_scores(ctx, strategy)
    return out


def evaluate_stock(code, ctx, strategy):
    """单股完整评估。ctx 由调用方构造(数据适配层负责填数据)。

    候选 dict 额外携带 up_stop_price / last(直接透出 ctx 字段):
    交易信号以涨停价排队买入需要 up_stop_price; ctx.up_price 可能为
    None, 保留 None(由 trader 端兜底为 0 —— 桥端 price=0 按对手价
    市价单处理, 真实盘下 run_daily 会拒绝无价候选, 见 trader.py)。
    """
    scores, factors = _compute_scores(ctx, strategy)
    return {"code": code, "scores": scores, "factors": factors,
            "up_stop_price": ctx.up_price, "last": ctx.last}


def run_screen(strategy, market_ctx, gate_factors=None, stock_contexts=None):
    """完整选股编排(与旧 screen.run 输出同构)。

    market_ctx: 市场数据上下文(算节点因子用)
    gate_factors: 市场节点因子预计算值 {N1: 0/1, ...}; None 则用 market_ctx 现算
    stock_contexts: {code: FactorContext}
    """
    gate = strategy.get("market_gate") or {}
    gate_fids = gate.get("factors", [])
    threshold = gate.get("threshold", 3)
    gate_score = 0
    if gate_factors is not None:
        gate_score = sum(1 for f in gate_fids if gate_factors.get(f) == 1)
    else:
        for fid in gate_fids:
            raw, hit = _factor_hit(market_ctx, fid, None, None)
            gate_score += 1 if hit else 0
    environment_ok = gate_score >= threshold

    candidates = []
    score_cfg = sector_score.load_config(strategy)
    mkt_extra = market_ctx.get("mkt") if market_ctx is not None else None
    sec_scores = (sector_score.compute_scores(mkt_extra)
                  if score_cfg["enabled"] and mkt_extra else {})
    if environment_ok and stock_contexts:
        min_model = (strategy.get("filters") or {}).get("candidate_min_model", 3)
        for code, ctx in stock_contexts.items():
            ev = evaluate_stock(code, ctx, strategy)
            best = max([ev["scores"][m["id"]]
                        for m in strategy["scoring_models"]], default=0)
            if best >= min_model:
                # fail-closed(设计 §3.5): mkt 注入但评分算不出(sec_scores 空)
                # 也进入过滤 → 候选查不到评分被全剔除, 与回测侧同语义
                if score_cfg["enabled"] and mkt_extra:
                    # sector_map 是市场级数据 → 从 market_ctx 取(非个股ctx)
                    sec = (market_ctx.get("sector_map") or {}).get(code)
                    if isinstance(sec, dict):
                        sec = sec.get("sector")
                    # str() 归一(审查 Minor#5): compute_scores 键为 str(code),
                    # sector_map 值为 int 时直接 get 会静默 miss → 误判无评分
                    rec = sec_scores.get(str(sec)) if sec else None
                    sec_score = rec["score"] if rec else None
                    if sec_score is None or sec_score <= score_cfg["threshold"]:
                        continue
                    ev["sector_score"] = sec_score
                candidates.append(ev)
        candidates.sort(key=lambda c: c["scores"]["composite"], reverse=True)

    summary = {"candidate_count": len(candidates)}
    for g in ("A", "B", "C", "D"):
        summary["%s_count" % g.lower()] = sum(
            1 for c in candidates if c["scores"]["grade"] == g)
    return {"environment_ok": environment_ok, "gate_score": gate_score,
            "candidates": candidates, "summary": summary}
