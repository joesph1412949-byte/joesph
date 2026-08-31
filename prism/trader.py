# -*- coding: utf-8 -*-
"""交易模块: 选股结果 → 信号文件 → QMT 桥(需授权)。

信号文件协议与 qmt_signal_bridge_real.py 完全一致(桥读取 pending/*.json,
消费字段: order_id / action / stock_code / price / volume / account_id)。
安全: 真实盘(env=real)下桥只消费授权文件存在时的信号; 本模块还提供
PAUSE_FILE 一键暂停(存在该文件即不生成新信号)。

价格语义: 信号 price=0 时桥端按对手价市价单处理(pr_type=2); 若策略
要求涨停价排队买入, 需确保 engine 候选带 up_stop_price(engine.evaluate_stock
已透出 ctx.up_price)。真实盘(env=real)下 run_daily 会逐候选跳过缺
up_stop_price 的候选(全部缺价则整体拒单), 防止误按市价单。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from common import SIGNAL_ROOT

PAUSE_FILE = Path(r"D:/QMT_SIGNALS/paused")


def check_paused():
    """暂停开关: PAUSE_FILE 存在即暂停生成信号。"""
    return Path(PAUSE_FILE).exists()


def generate_signals(result, strategy, env="sim", volume=100):
    """把选股结果转成买入信号列表(与桥 pending JSON 同构)。

    result: run_screen 的输出 {environment_ok, candidates, ...}。
    每只候选股生成一条 BUY 信号; 环境不达标返回空列表。
    信号额外携带 strategy_id / composite, 便于盘后追溯。

    价格语义: price 取候选的 up_stop_price, 缺失时为 0 —— 桥端收到
    price=0 会按对手价市价单处理(pr_type=2); 若策略要求涨停价限价
    买入, 需确保 engine 候选带 up_stop_price(engine.evaluate_stock
    已透出 ctx.up_price)。真实盘保护在 run_daily 层(全缺 up_stop_price
    即拒绝), 本函数不额外拦截, 保持 sim/real 输出一致。
    """
    if not result.get("environment_ok"):
        return []
    out = []
    for c in result.get("candidates", []):
        order_id = "BUY_%s" % uuid.uuid4().hex[:8]
        out.append({
            "order_id": order_id,
            "action": "BUY",
            "stock_code": c["code"],
            "order_type": "BUY",
            "price": c.get("up_stop_price") or 0,
            "volume": volume,
            "account_id": "",
            "created_at": datetime.now().isoformat(),
            "status": "pending",
            "strategy_id": strategy.get("id", "unknown"),
            "composite": c.get("scores", {}).get("composite"),
            # 协议兼容: 旧版 QMT 信号消费端忽略未知键, 新增键须可缺省(None)
            "sector_score": c.get("sector_score"),
        })
    return out


def write_signals(signals, env="sim"):
    """写信号文件到 SIGNAL_ROOT/<env>/pending/。返回写入数量。

    每信号一个 JSON 文件(文件名 = order_id), 桥端扫描该目录消费。
    """
    if not signals:
        return 0
    pending_dir = Path(SIGNAL_ROOT) / env / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for s in signals:
        path = pending_dir / ("%s.json" % s["order_id"])
        path.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        n += 1
    return n


def run_daily(strategy, provider, env="sim", volume=100, archive=None):
    """盘后完整流程: 选股 → (可选绩效存档) → 生成并写入信号。

    步骤: 暂停检查 → load_strategy → build_market_context → 门槛因子
    (gate) 预计算 → 涨停池 → 逐股上下文 → run_screen → archive 回调 →
    generate_signals → write_signals。

    返回 {"environment_ok", "candidates", "signals_written", "paused",
    "skipped_no_price"}; env="real" 时逐候选跳过缺 up_stop_price 的候选,
    全部缺价则返回 "error" 字段并拒单。
    """
    if check_paused():
        return {"environment_ok": False, "candidates": [],
                "signals_written": 0, "paused": True}
    from prism.engine import load_strategy, run_screen
    strat = load_strategy(strategy)
    market_ctx = provider.build_market_context()
    gate_fids = (strat.get("market_gate") or {}).get("factors", [])
    gate_factors = {}
    if gate_fids:
        from prism import registry as reg
        for fid in gate_fids:
            try:
                res = reg.get_factor(fid)["func"](market_ctx)
                gate_factors[fid] = 1 if (isinstance(res, dict) and res.get("score")) else 0
            except Exception:
                gate_factors[fid] = 0
    limit_ups = provider.get_limit_ups()
    stock_contexts = {}
    for lu in limit_ups:
        code = lu["code"]
        stock_contexts[code] = provider.build_stock_context(code)
    result = run_screen(strat, market_ctx, gate_factors=gate_factors,
                        stock_contexts=stock_contexts)
    result["market"] = {"limit_up_count": len(limit_ups)}
    if archive is not None:
        try:
            archive(result.get("candidates", []))
        except Exception:
            pass
    candidates = result.get("candidates", [])
    # 真实盘保护(审查 C1, 逐候选): env="real" 时逐个检查 up_stop_price,
    # 缺价(含 None/0/空)候选跳过不生成信号, 累计到 skipped_no_price;
    # 带价候选正常生成。桥端对 price=0 按对手价市价单处理(pr_type=2),
    # 与"涨停价排队买入"语义不符, 真实盘下宁可跳过/拒单也不误按市价单。
    # 全部候选都缺价 → 拒绝生成信号并附 error(保留原"全缺拒单"语义)。
    # sim 环境行为不变(不检查价格, 与 generate_signals 一致)。
    skipped_no_price = 0
    signal_candidates = candidates
    if env == "real" and candidates:
        priced = [c for c in candidates if c.get("up_stop_price")]
        skipped_no_price = len(candidates) - len(priced)
        if not priced:
            return {"environment_ok": result["environment_ok"],
                    "candidates": candidates,
                    "signals_written": 0, "paused": False,
                    "skipped_no_price": skipped_no_price,
                    "error": ("real盘候选缺 up_stop_price, 已拒绝生成信号: "
                              "桥端 price=0 会按对手价市价单处理, 与涨停价排队买入不符")}
        signal_candidates = priced
    if signal_candidates is not candidates:
        sub = dict(result)
        sub["candidates"] = signal_candidates
        result = sub
    signals = generate_signals(result, strat, env=env, volume=volume)
    written = write_signals(signals, env=env)
    return {"environment_ok": result["environment_ok"],
            "candidates": candidates,   # 返回原始候选(含被跳过的缺价者, 便于盘后追溯)
            "signals_written": written, "paused": False,
            "skipped_no_price": skipped_no_price}
