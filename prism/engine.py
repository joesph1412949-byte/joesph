# -*- coding: utf-8 -*-
"""策略引擎: 读策略配置 → 算因子 → 模型分 → 综合分 → 过滤排序。

与旧 screen.py 的输出结构保持同构, 网页/绩效/桥无缝对接。
"""
import json
import os
from datetime import datetime
from pathlib import Path

from prism import registry as reg
from prism.context import FactorContext
from prism import sector_score
from shared.common import atomic_write
_registry = reg  # validate 的 reg=None 形参遮蔽模块级名 → 别名兜底(审查: 删函数内本地 import)

# 综合分组合方式
composite_modes = {
    "top3_weighted": lambda scores, cfg: _top3_weighted(scores, cfg),
    "sum": lambda scores, cfg: sum(scores),
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


def gate_evaluate(fids, market_ctx):
    """门禁因子求值: {fid: 1/0}。

    因子缺失/抛异常/无 score → 0(fail-closed, 与 run_screen 门槛口径一致)。
    模拟盘/实盘/交易三处共用(此前各写一份同逻辑)。
    """
    out = {}
    for fid in fids:
        try:
            res = reg.get_factor(fid)["func"](market_ctx)
            score = res.get("score") if isinstance(res, dict) else res
            out[fid] = 1 if score else 0
        except Exception:
            out[fid] = 0
    return out


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
        fs = m.get("factors") or []
        try:
            ws = [float(w) for w in (m.get("weights") or [])]
        except (TypeError, ValueError):
            ws = []     # 脏权重 → 整体回退 1.0(与校验器兜底一致)
        for idx, item in enumerate(fs):
            fid, weight, op, threshold = _factor_entry(item)
            if isinstance(item, str) and idx < len(ws):
                weight = ws[idx]    # I-2: 字符串形态按位读模型权重(缺位/无键→1.0)
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
        # v04 接线: 市场级数据(个股 ctx 构建时拿不到)统一下发到个股 ctx。
        # mkt 进 _extra(ctx.get("mkt")); sector_map 是字段, 个股未填时兜底。
        sec_map = (market_ctx.get("sector_map")
                   if market_ctx is not None else None)
        if mkt_extra:
            for ctx in stock_contexts.values():
                if isinstance(getattr(ctx, "_extra", None), dict):
                    ctx._extra.setdefault("mkt", mkt_extra)
        if sec_map:
            for ctx in stock_contexts.values():
                if not (getattr(ctx, "sector_map", None) or {}):
                    ctx.sector_map = sec_map
        # 指数K线回填(F6 专用)。此前漏了这一步 —— 个股 ctx 构建时拿不到指数
        # 数据, 而 build_market_context 抓到的又没下发, 导致 F6 在实盘与回测
        # 双向恒 0。两个字段语义不同, 必须分开下发:
        #   index_kline      -> 涨停指数 880368(N1 兜底, 需 >=6 根)
        #   sh_index_kline   -> 上证指数 000001(F6, 需 >=21 根算 MA20)
        if market_ctx is not None:
            idx = getattr(market_ctx, "index_kline", None)
            sh_idx = market_ctx.get("sh_index_kline")
            if idx is not None or sh_idx is not None:
                for ctx in stock_contexts.values():
                    if idx is not None and getattr(ctx, "index_kline", None) is None:
                        ctx.index_kline = idx
                    extra = getattr(ctx, "_extra", None)
                    if sh_idx is not None and isinstance(extra, dict):
                        extra.setdefault("sh_index_kline", sh_idx)
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
                candidates.append(ev)
        candidates.sort(key=lambda c: c["scores"]["composite"], reverse=True)

    summary = {"candidate_count": len(candidates)}
    for g in ("A", "B", "C", "D"):
        summary["%s_count" % g.lower()] = sum(
            1 for c in candidates if c["scores"]["grade"] == g)
    return {"environment_ok": environment_ok, "gate_score": gate_score,
            "candidates": candidates, "summary": summary}


# ---------------- 默认策略指针(策略编辑器 spec §5) ----------------
STRATEGIES_DIR = Path(__file__).parent / "strategies"
ACTIVE_FILENAME = ".active.json"
_ACTIVE_FALLBACK = "first_board_v04"


def active_strategy_id(pointer_path=None):
    """读默认策略指针; 缺文件/损坏/所指策略不存在 → 回落 first_board_v04。"""
    p = Path(pointer_path) if pointer_path \
        else STRATEGIES_DIR / ACTIVE_FILENAME
    try:
        sid = json.loads(p.read_text(encoding="utf-8")).get("id")
    except Exception:
        return _ACTIVE_FALLBACK
    if not sid or not (STRATEGIES_DIR / ("%s.json" % sid)).is_file():
        return _ACTIVE_FALLBACK
    return sid


def resolve_strategy(strategy_or_id=None):
    """统一策略加载(模拟盘/实盘/交易共用): dict 原样校验返回; id/None → 读默认指针。

    非 dict 时先幂等重扫因子库(load_strategy 的因子存在性校验依赖它)。
    """
    if isinstance(strategy_or_id, dict):
        return load_strategy(strategy_or_id)
    reg.scan_factors(force=True)
    sid = strategy_or_id or active_strategy_id()
    return load_strategy(STRATEGIES_DIR / ("%s.json" % sid))


def execution_sizing(strategy):
    """策略 execution 块的 (pct, top_n); 缺失 → (None, None)(调用方各自回落)。"""
    ex = (strategy or {}).get("execution") or {}
    return (float(ex["pct"]) if ex.get("pct") else None,
            int(ex["top_n"]) if ex.get("top_n") else None)


def set_active_strategy(sid, pointer_path=None):
    """写默认策略指针(原子写; sid 存在性由调用方校验)。

    走 shared.common.atomic_write: 唯一 tmp(pid+线程) + fsync + 有界退避
    replace + 失败清理 —— 编辑器/网页/守护都写这份指针, 固定 tmp 名会互踩。
    """
    p = Path(pointer_path) if pointer_path \
        else STRATEGIES_DIR / ACTIVE_FILENAME
    atomic_write(p, json.dumps(
        {"id": sid, "updated": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1))


# ---------------- 策略校验器(策略编辑器 spec §3/§4) ----------------


def _num(v, default):
    """宽松取数: None → default; 非数值 → nan(调用方按非法处理)。"""
    try:
        return float(v) if v is not None else float(default)
    except Exception:
        return float("nan")


def validate_strategy_payload(payload, reg=None):
    """策略编辑器 payload 全规则校验(spec §4)。返回 (ok, errors, strategy)。

    errors 非空 → strategy=None(零写入契约)。全过才组装 dict:
    id=None 由端点生成; cap 自动 = Σ(模型权重×对齐后因子权重和) round 2;
    因子权重对齐 = 截断到因子数、不足补 1.0。
    """
    reg = reg or _registry
    if not isinstance(payload, dict):
        payload = {}    # ponytail: 脏 body 不崩, 落入"名称非空"等既有错误路径
    errors = []
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 40:
        errors.append("策略名称: 需非空且不超过40字")
    models = payload.get("models") or []
    if not 1 <= len(models) <= 3:
        errors.append("评分模型: 需1-3个")
    seen = set()
    seen_ids = set()
    model_rows, model_weights = [], []
    for i, m in enumerate(models, 1):
        if not isinstance(m, dict):
            errors.append("模型%d: 结构非法" % i)
            continue
        fs = list(m.get("factors") or [])
        if not fs:
            errors.append("模型%d: 至少勾选1个因子" % i)
        for fid in fs:
            if not isinstance(fid, str):    # I-1: 嵌套条目不可哈希 → 明确报错不崩
                errors.append("模型%d: 因子 %r 需为字符串编号" % (i, fid))
                continue
            if fid not in reg.FACTORS:
                errors.append("模型%d: 因子 %s 不存在" % (i, fid))
            if fid in seen:
                errors.append("因子 %s 在多个模型重复" % fid)
            seen.add(fid)
        try:
            raw_w = [float(x) for x in (m.get("weights") or [])][:len(fs)]
        except (TypeError, ValueError):
            raw_w = []
            errors.append("模型%d: 因子权重需为正数" % i)
        fws = raw_w + [1.0] * (len(fs) - len(raw_w))
        # 0<w<inf 一次性挡掉 ≤0 / nan / inf(JSON 可传 NaN 字面量)
        if any(not (0 < w < float("inf")) for w in fws):
            errors.append("模型%d: 因子权重需为正数" % i)
        mw = _num(m.get("weight"), 1.0)
        if not (0 < mw < float("inf")):
            errors.append("模型%d: 模型权重需为正数" % i)
        mid = m.get("id") or "model_%d" % i
        if str(mid) in seen_ids:        # M-2: 模型 id 查重(str 键, 脏 id 不崩)
            errors.append("模型 id %s 重复" % mid)
        seen_ids.add(str(mid))
        model_rows.append({"id": mid,
                           "name": m.get("name") or m.get("id") or "自定义",
                           "weight": mw, "factors": fs, "weights": fws})
        model_weights.append(mw)
    gate = list(payload.get("gate_factors") or [])
    for fid in gate:
        if not isinstance(fid, str):    # I-1 同类: 门槛因子嵌套条目防崩
            errors.append("门槛因子 %r 需为字符串编号" % fid)
            continue
        meta = reg.FACTORS.get(fid)
        if meta is None or meta.get("category") != "node":
            errors.append("门槛因子 %s 不是环境门槛类(node)" % fid)
    gt = payload.get("gate_threshold")
    try:
        gt = int(gt) if gt is not None else 0
    except Exception:
        gt = -1
    if gt < 0:
        errors.append("门槛线: 需≥0整数")
    try:
        cmm = int(payload.get("candidate_min_model"))
    except Exception:
        cmm = -1
    if cmm < 1:
        errors.append("候选资质线: 需≥1整数")
    s = payload.get("sell")
    if not isinstance(s, dict):
        errors.append("卖出规则: 需为对象")   # ponytail: 不再静默落 {}
        s = {}
    tp = _num(s.get("take_profit_pct"), 0.08)
    sl = _num(s.get("stop_loss_pct"), 0.05)
    hold = _num(s.get("max_hold_days"), 5)
    if not 0 < tp <= 0.5:
        errors.append("止盈比例需在 0~50%")
    if not 0 < sl <= 0.5:
        errors.append("止损比例需在 0~50%")
    if not 1 <= hold <= 30:
        errors.append("持有天数需 1~30")
    if errors:
        return False, errors, None
    cap = round(sum(m["weight"] * sum(m["weights"]) for m in model_rows), 2)
    strategy = {
        "id": None, "name": name, "description": "网页编辑器生成",
        "market_gate": {"model": "node", "threshold": gt, "factors": gate},
        "scoring_models": model_rows,
        "composite": {"mode": "top3_weighted", "weights": model_weights,
                      "cap": cap},
        "filters": {"candidate_min_model": cmm, "environment_threshold": gt},
        "sell_rules": {"take_profit_pct": tp, "stop_loss_pct": sl,
                       "max_hold_days": int(hold)}}
    return True, [], strategy
