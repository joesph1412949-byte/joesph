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

from shared.common import SIGNAL_ROOT

PAUSE_FILE = Path(r"D:/QMT_SIGNALS/paused")


def check_paused():
    """暂停开关: PAUSE_FILE 存在即暂停生成信号。"""
    return Path(PAUSE_FILE).exists()


def build_signal(code, action, price, volume, order_id, account_id="",
                 strategy_id="unknown", composite=None, created_at=None):
    """构造一条与桥 pending JSON 同构的信号(单一 schema 出口)。

    桥端消费字段: order_id / action / stock_code / price / volume /
    account_id。price=0 → 桥按对手价市价单(pr_type=2); price>0 → 限价单。
    """
    return {
        "order_id": order_id,
        "action": action,
        "stock_code": code,
        "price": price or 0,
        "volume": int(volume or 0),
        "account_id": account_id,
        "created_at": created_at or datetime.now().isoformat(),
        "status": "pending",
        "strategy_id": strategy_id,
        "composite": composite,
    }


def generate_signals(result, strategy, volume=100):
    """把选股结果转成买入信号列表(与桥 pending JSON 同构)。

    result: run_screen 的输出 {environment_ok, candidates, ...}。
    每只候选股生成一条 BUY 信号; 环境不达标返回空列表。
    信号额外携带 strategy_id / composite, 便于盘后追溯; order_id 用随机
    短 id(实盘确定性 id 由 live_daemon 两段式开盘发单时生成)。

    价格语义: price 取候选的 up_stop_price, 缺失时为 0 —— 桥端收到
    price=0 会按对手价市价单处理(pr_type=2); 若策略要求涨停价限价
    买入, 需确保 engine 候选带 up_stop_price(engine.evaluate_stock
    已透出 ctx.up_price)。真实盘保护在 run_daily 层(全缺 up_stop_price
    即拒绝), 本函数不额外拦截, 保持 sim/real 输出一致。
    """
    if not result.get("environment_ok"):
        return []
    return [build_signal(
        code=c["code"], action="BUY", price=c.get("up_stop_price"),
        volume=volume, order_id="BUY_%s" % uuid.uuid4().hex[:8],
        strategy_id=strategy.get("id", "unknown"),
        composite=c.get("scores", {}).get("composite"))
        for c in result.get("candidates", [])]


def write_signals(signals, env="sim", root=None):
    """写信号文件到 <root>/<env>/pending/。返回写入数量。

    每信号一个 JSON 文件(文件名 = order_id), 桥端扫描该目录消费。
    root 缺省用模块级 SIGNAL_ROOT(测试可 monkeypatch 或显式传目录)。
    """
    if not signals:
        return 0
    pending_dir = Path(root or SIGNAL_ROOT) / env / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for s in signals:
        path = pending_dir / ("%s.json" % s["order_id"])
        path.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        n += 1
    return n


def _check_gate_model(strat):
    """大盘闸门模型白名单: engine.run_screen 只实现了"按 node 类因子计数"。

    market_gate.model 改前是**零读取点**的键: 手改成别的取值不生效、也不报错
    (配置看着被尊重, 实际被忽略)。未知取值 → fail-closed 抛错, 由调用方的
    tick 循环捕获(本轮不选股/不落信号), 绝不按 node 语义静默跑一个声明了
    别的模型的策略。缺键 / "node" 一律放行(缺键 = 写盘侧的唯一取值)。

    ponytail: 覆盖范围只有本入口(run_daily: 实盘守护 + CLI)。模拟盘收盘选股走
    prism/paper.py、网页选股走 engine.run_screen, 都绕开这里 —— 要全路径覆盖,
    正解是把这三行搬进 engine.load_strategy(所有读取方共用; 该文件非本批可改)。
    """
    model = (strat.get("market_gate") or {}).get("model", "node")
    if model != "node":
        raise ValueError("未知大盘闸门模型: %r (只实现了 node)" % (model,))


def run_daily(strategy, provider, env="sim", volume=100, write=True):
    """盘后完整流程: 选股 → 生成并(可选)写入信号。

    步骤: 暂停检查 → 策略加载(engine.resolve_strategy) → build_market_context
    → 门槛因子(gate)预计算 → 涨停池 → 逐股上下文 → run_screen →
    generate_signals → write_signals。

    write=False 时只选股并返回 signals 列表, 不落 pending 目录 —— 供
    live_daemon 的"收盘选股 → 次日开盘再发单"两段式使用(收盘后立即
    写信号会被桥端在非交易时段拒单并丢进 failed)。

    返回 {"environment_ok", "candidates", "signals", "signals_written",
    "paused", "skipped_no_price"}; env="real" 时逐候选跳过缺
    up_stop_price 的候选, 全部缺价则返回 "error" 字段并拒单。
    """
    if check_paused():
        return {"environment_ok": False, "candidates": [], "signals": [],
                "signals_written": 0, "paused": True}
    from prism.engine import gate_evaluate, resolve_strategy, run_screen
    strat = resolve_strategy(strategy)
    _check_gate_model(strat)        # 未知闸门模型 → fail-closed, 不进选股
    market_ctx = provider.build_market_context()
    gate_fids = (strat.get("market_gate") or {}).get("factors", [])
    limit_ups = provider.get_limit_ups()
    stock_contexts = {}
    for lu in limit_ups:
        code = lu["code"]
        stock_contexts[code] = provider.build_stock_context(code)
    result = run_screen(strat, market_ctx,
                        gate_factors=gate_evaluate(gate_fids, market_ctx),
                        stock_contexts=stock_contexts)
    result["market"] = {"limit_up_count": len(limit_ups)}
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
                    "candidates": candidates, "signals": [],
                    "signals_written": 0, "paused": False,
                    "skipped_no_price": skipped_no_price,
                    "error": ("real盘候选缺 up_stop_price, 已拒绝生成信号: "
                              "桥端 price=0 会按对手价市价单处理, 与涨停价排队买入不符")}
        signal_candidates = priced
    signals = generate_signals({"environment_ok": result["environment_ok"],
                                "candidates": signal_candidates},
                               strat, volume=volume)
    written = write_signals(signals, env=env) if write else 0
    return {"environment_ok": result["environment_ok"],
            "candidates": candidates,   # 返回原始候选(含被跳过的缺价者, 便于盘后追溯)
            "signals": signals,
            "signals_written": written, "paused": False,
            "skipped_no_price": skipped_no_price}
