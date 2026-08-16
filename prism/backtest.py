# -*- coding: utf-8 -*-
"""回测引擎: 逐日回放真实策略(与实盘同一 engine), 含交易模拟。

与实盘共用同一选股逻辑: load_strategy + compute_model_scores(engine),
不另写一套规则; 回测只补交易模拟层——手续费(fee_rate, 默认万2.5)
+ 滑点(slippage, 默认0.1%) + 仓位(单只资金上限, position_ratio, 预留)
+ 卖出规则(复用 exit_rules.ExitRule: 止损 > 止盈 > 持有期满)。

数据源注入(测试用假实现 / 东财 backtest_cli):
  zt_feed(date_str) -> [{code, boards, theme}, ...]   当日涨停池
  kline_feed(code)  -> [(date_str, close), ...]       该股日K线(升序)

回测数据适配层(审查 I1, 让真实因子可在回测命中):
  - kline_feed 的元组列表组装成 DataFrame(close/volume/open/high/low,
    日期做行索引), 满足真实因子按 kline["close"]/kline["volume"]/len(kline)
    访问(旧实现直接把元组列表塞进 FactorContext, 真实因子全 fail-open 0);
  - 市场上下文从 zt_feed 池子合成 limit_ups(补 sealed/last/last_close,
    没有就 None), 并允许注入 em/ticks(供 N3/N5 等节点因子); 未注入时
    依赖这些数据的门槛因子得 0 并记入报告 gate_notes 供诊断。
  - run() 默认采用策略配置的 sell_rules(策略 JSON 是唯一策略描述),
    显式传入的 sell_rules 参数优先覆盖。

注意: 回测里用到的因子必须在 registry 注册(测试用 @factor 装饰器注入
N1/A1 等假因子); 引擎不依赖 prism.factors 的真实因子(它们需要真实行情,
回测只有 K线, 但适配层保证"真实形态"的 K线类因子可命中)。
"""
import logging
from datetime import date, timedelta

import pandas as pd

from exit_rules import ExitRule
from prism import registry as reg
from prism.context import FactorContext
from prism.engine import compute_model_scores, load_strategy

logger = logging.getLogger(__name__)

# 门槛因子 → 依赖的回测可注入数据源(缺失时该因子无法命中, 记入 gate_notes)。
# 不在表内的门槛因子(如 N1/N2 兜底靠池子即可算)不归因数据缺失。
_GATE_DATA_NEEDS = {
    "N3": ("em", "ticks"),   # 需要昨日涨停代码 + 今日行情(兜底需池内 last/last_close)
    "N4": ("em",),           # 需要 em.max_boards(兜底需池内 sealed)
    "N5": ("ticks",),        # 需要全市场 ticks 求和
}


class Backtester:
    """真实策略回放 + 交易模拟。"""

    def __init__(self, strategy, zt_feed, kline_feed,
                 fee_rate=0.00025, slippage=0.001, position_ratio=0.3):
        self.strategy = load_strategy(strategy)
        self.zt_feed = zt_feed
        self.kline_feed = kline_feed
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.position_ratio = position_ratio   # 单只资金上限(报告暂按等权, 预留)

    # ---------------- 选股(复用 engine) ----------------
    def _stock_ctx(self, code, kline):
        """回测环境的股票上下文(只用回测可得的字段)。

        数据适配层(审查 I1): kline_feed 的元组列表 [(date, close), ...] 组装成
        DataFrame(close/volume/open/high/low, 日期做行索引), 满足真实因子按
        kline["close"]/kline["volume"]/len(kline) 访问。回测 feeds 没有
        volume/open/high/low → 占位: volume 恒 1.0(量比类因子不会误命中),
        open/high/low 取 close(形态类因子语义不受影响)。
        """
        rows = []
        for dt, px in kline or []:
            try:
                rows.append((str(dt), float(px)))
            except (TypeError, ValueError):
                continue
        if not rows:
            return FactorContext(code=code, kline=None)
        df = pd.DataFrame({
            "close": [px for _dt, px in rows],
            "open": [px for _dt, px in rows],
            "high": [px for _dt, px in rows],
            "low": [px for _dt, px in rows],
            "volume": [1.0] * len(rows),
        }, index=[dt for dt, _px in rows])
        return FactorContext(code=code, kline=df)

    def _pick(self, pool, em=None, ticks=None):
        """用策略引擎对当日涨停池选股。返回 [(code, boards, theme, composite), ...]。

        市场门槛(节点因子)不达标 → 空仓; 达标后逐股 build FactorContext
        调 compute_model_scores, 按 candidate_min_model 过滤, 综合分降序。

        数据适配层(审查 I1): 市场上下文从池子合成 limit_ups(补
        sealed/last/last_close, 没有就 None), em/ticks 可注入(供 N3/N5 等);
        未注入时依赖这些数据的门槛因子得 0(见 _gate_notes 归因)。
        """
        gate = self.strategy.get("market_gate") or {}
        gate_fids = gate.get("factors", [])
        threshold = gate.get("threshold", 3)
        gate_score = 0
        pool_ctx = []
        for lu in pool:
            item = dict(lu)
            item.setdefault("sealed", None)
            item.setdefault("last", None)
            item.setdefault("last_close", None)
            pool_ctx.append(item)
        market_ctx = FactorContext(code="__MKT__", limit_ups=pool_ctx,
                                   em=em or {}, ticks=ticks or {})
        for fid in gate_fids:
            meta = reg.get_factor(fid)
            try:
                res = meta["func"](market_ctx)
                if isinstance(res, dict) and res.get("score"):
                    gate_score += 1
            except Exception:
                pass
        if gate_score < threshold:
            return []
        min_model = (self.strategy.get("filters") or {}).get(
            "candidate_min_model", 3)
        out = []
        for s in pool:
            code = s["code"]
            try:
                kline = self.kline_feed(code) or []
            except Exception as e:
                logger.warning("回测 %s K线失败: %r", code, e)
                kline = []
            ctx = self._stock_ctx(code, kline)
            scores = compute_model_scores(ctx, self.strategy)
            best = max([scores[m["id"]]
                        for m in self.strategy["scoring_models"]], default=0)
            if best >= min_model:
                out.append((code, s.get("boards", 0), s.get("theme", ""),
                            scores["composite"]))
        out.sort(key=lambda x: x[3], reverse=True)
        return out

    def _gate_notes(self, em, ticks):
        """诊断(审查 I1): 门槛因子因未注入数据而得 0 的归因, 附在报告 gate_notes。

        只归因"缺注入数据"可解释的 0(N1 兜底靠池子、N2 只靠池子不归因),
        让回测空报告时用户知道该注入什么数据, 而非静默 fail-open。
        """
        notes = []
        gate = self.strategy.get("market_gate") or {}
        for fid in gate.get("factors", []):
            need_sources = _GATE_DATA_NEEDS.get(fid)
            if not need_sources:
                continue
            lack = [s for s in need_sources
                    if (s == "em" and not em) or (s == "ticks" and not ticks)]
            if lack:
                notes.append("%s 缺%s数据 → 0" % (fid, "+".join(lack)))
        return notes

    # ---------------- 主流程 ----------------
    def run(self, start_date, end_date, sell_rules=None, progress=None,
            em=None, ticks=None):
        """回放 [start_date, end_date]。返回报告 dict(与旧 backtest 同构)。

        sell_rules: {take_profit_pct, stop_loss_pct, max_hold_days},
        显式传入时优先覆盖; 缺省采用策略配置 strategy["sell_rules"]
        (审查 I1: 策略 JSON 是唯一策略描述, 不再硬编码默认值), 配置也缺时
        回退默认(止盈8%/止损5%/持有5天)。
        em/ticks: 可注入的市场数据(供 N3/N5 等节点因子), 缺省 None →
        依赖它们的门槛因子得 0 并记入报告 gate_notes。
        """
        defaults = {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                    "max_hold_days": 5}
        cfg_rules = dict(defaults)
        cfg_rules.update(self.strategy.get("sell_rules") or {})
        rules = dict(cfg_rules, **(sell_rules or {}))
        gate_notes = self._gate_notes(em, ticks)
        trades = []
        dates = []
        d = start_date
        while d <= end_date:
            if progress:
                progress(d)
            try:
                pool = self.zt_feed(d.strftime("%Y%m%d")) or []
            except Exception as e:
                logger.warning("回测 %s 涨停池失败: %r", d, e)
                pool = []
            if pool:
                dates.append(d)
                for code, boards, theme, composite in self._pick(
                        pool, em=em, ticks=ticks):
                    try:
                        kline = self.kline_feed(code) or []
                    except Exception:
                        kline = []
                    tr = self._simulate_trade(code, kline, d, rules)
                    if tr:
                        trades.append({
                            "date": d.strftime("%Y-%m-%d"), "code": code,
                            "boards": boards, "theme": theme,
                            "composite": composite,
                            "entry": tr[0], "exit": tr[1], "return_pct": tr[2],
                        })
            d += timedelta(days=1)
        return self._report(trades, dates, gate_notes=gate_notes)

    # ---------------- 交易模拟 ----------------
    def _simulate_trade(self, code, kline, entry_date, rules):
        """模拟一笔: 选股日收盘买入(加滑点), 按卖出规则/持有期卖出。

        卖出判定复用 exit_rules.ExitRule(止损 > 止盈 > 持有期满),
        以含滑点的买入价为成本基准; 卖出价减滑点; 手续费双向。
        返回 (entry, exit, return_pct) 或 None(无法成交)。
        """
        if not kline:
            return None
        idx = None
        for i, (dt, _c) in enumerate(kline):
            if dt >= entry_date.strftime("%Y-%m-%d"):
                idx = i
                break
        if idx is None:
            return None
        entry_close = kline[idx][1]
        if not entry_close:
            return None
        # 买入: 收盘价 + 滑点
        buy_price = entry_close * (1 + self.slippage)
        rule_kwargs = {k: rules[k] for k in ("take_profit_pct",
                                             "stop_loss_pct",
                                             "max_hold_days") if k in rules}
        exit_close = None
        for j in range(idx + 1, len(kline)):
            dt_str, px = kline[j]
            today = date(*[int(x) for x in dt_str.split("-")])
            rule = ExitRule(code, code, buy_price, entry_date, today=today,
                            **rule_kwargs)
            action, _reason = rule.evaluate(px)
            if action:
                exit_close = px
                break
        if exit_close is None:
            return None
        # 卖出: 收盘价 - 滑点; 手续费双向
        sell_price = exit_close * (1 - self.slippage)
        fee = buy_price * self.fee_rate + sell_price * self.fee_rate
        net = (sell_price - buy_price - fee) / buy_price * 100
        return (round(buy_price, 4), round(sell_price, 4), round(net, 2))

    # ---------------- 统计 ----------------
    @staticmethod
    def _report(trades, dates, gate_notes=None):
        n = len(trades)
        base = {"trading_days": len(dates), "trades": n,
                "gate_notes": gate_notes or []}
        if n == 0:
            base.update({"win_rate": None, "avg_return_pct": None,
                         "profit_loss_ratio": None, "max_drawdown_pct": None,
                         "total_return_pct": None})
            return base
        returns = [t["return_pct"] for t in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / n
        avg_ret = sum(returns) / n
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        pl_ratio = (avg_win / abs(avg_loss)) if losses and avg_loss != 0 else None
        # 最大回撤: 按逐日累计收益(每笔等权, 累计到所在日)
        cum = {}
        for t in trades:
            cum[t["date"]] = cum.get(t["date"], 0.0) + t["return_pct"]
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for d in sorted(cum):
            equity += cum[d]
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
        return {
            "trading_days": len(dates), "trades": n,
            "gate_notes": gate_notes or [],
            "win_rate": round(win_rate, 4),
            "avg_return_pct": round(avg_ret, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio else None,
            "max_drawdown_pct": round(max_dd, 2),
            "total_return_pct": round(sum(returns), 2),
        }

    # ---------------- 参数对比 ----------------
    def compare_params(self, start_date, end_date, param_grid, progress=None):
        """param_grid: [{take_profit, stop_loss, hold_days}, ...] → 对比表 rows。"""
        rows = []
        for params in param_grid:
            sell = {"take_profit_pct": params.get("take_profit", 0.08),
                    "stop_loss_pct": params.get("stop_loss", 0.05),
                    "max_hold_days": params.get("hold_days", 5)}
            rep = self.run(start_date, end_date, sell_rules=sell,
                           progress=progress)
            rows.append({
                "take_profit": sell["take_profit_pct"],
                "stop_loss": sell["stop_loss_pct"],
                "hold_days": sell["max_hold_days"],
                "trades": rep["trades"], "win_rate": rep["win_rate"],
                "avg_return_pct": rep["avg_return_pct"],
                "max_drawdown_pct": rep["max_drawdown_pct"],
            })
        return rows
