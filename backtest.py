# -*- coding: utf-8 -*-
"""回测框架: 用东财历史涨停池回放简化选股规则, 统计胜率/盈亏比/最大回撤。

与 QMT 完全解耦——只用东财公开数据(涨停池: code/boards/theme; 历史K线: 收盘价),
因此可以离线回放任意历史区间, 验证"环境门槛 + 主线题材 + 连板高度"这类
规则是否有效, 以及参数(涨停家数门槛/选股数/持有天数)怎么选。

数据源注入(测试用假实现):
  zt_feed(date_str) -> [{code, boards, theme}]   当日涨停池(东财字段)
  kline_feed(code)  -> [(date_str, close), ...]  该股日K线(升序)

规则(默认, 全部基于东财数据可算):
  1. 环境门槛: 当日涨停家数 >= min_limit_count 才选股
  2. 主线题材: 按涨停家数聚合取 Top1 题材
  3. 选股: 主线题材内连板最高的 max_picks 只
  4. 持有: 选股日收盘价买入, hold_days 个交易日后收盘价卖出
"""
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


class BacktestEngine:
    def __init__(self, zt_feed, kline_feed):
        self.zt_feed = zt_feed
        self.kline_feed = kline_feed

    # ---------------- 单日选股 ----------------
    def _pick_day(self, pool, params):
        """给定当日涨停池, 返回选中股票列表(按规则)。"""
        if len(pool) < params["min_limit_count"]:
            return []                       # 环境门槛不达标 → 空仓
        # 主线题材: 聚合后取 Top1(涨停家数最多的题材)
        buckets = {}
        for s in pool:
            theme = (s.get("theme") or "").strip() or "未知"
            b = buckets.setdefault(theme, {"count": 0, "max_boards": 0, "stocks": []})
            b["count"] += 1
            b["max_boards"] = max(b["max_boards"], int(s.get("boards") or 0))
            b["stocks"].append(s)
        top_theme = max(buckets.values(), key=lambda b: (b["count"], b["max_boards"]),
                        default=None)
        if not top_theme:
            return []
        # 主线题材内: 连板高度降序, 取前 max_picks
        ranked = sorted(top_theme["stocks"],
                        key=lambda s: (int(s.get("boards") or 0),
                                       s.get("code") or ""), reverse=True)
        return ranked[: params["max_picks"]]

    # ---------------- 收益计算 ----------------
    @staticmethod
    def _trade_return(kline, entry_date, hold_days):
        """kline: [(date_str, close)] 升序。entry_date 后第 hold_days 根的收益。
        返回 (entry_close, exit_close, return_pct) 或 None(K线不足)。"""
        if not kline:
            return None
        idx = None
        for i, (d, _c) in enumerate(kline):
            if d >= entry_date:
                idx = i
                break
        if idx is None:
            return None
        exit_idx = idx + hold_days
        if exit_idx >= len(kline):
            return None
        entry_close = kline[idx][1]
        exit_close = kline[exit_idx][1]
        if not entry_close:
            return None
        return (entry_close, exit_close,
                round((exit_close / entry_close - 1) * 100, 2))

    # ---------------- 主流程 ----------------
    def run(self, start_date, end_date, params=None, progress=None):
        """回放 [start_date, end_date] 区间。返回回测报告 dict。"""
        defaults = {"min_limit_count": 20, "max_picks": 3, "hold_days": 5}
        params = dict(defaults, **(params or {}))
        trades = []          # 每笔: {date, code, boards, theme, return_pct}
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
                for s in self._pick_day(pool, params):
                    code = s.get("code")
                    try:
                        kline = self.kline_feed(code) or []
                    except Exception as e:
                        logger.warning("回测 %s K线失败: %r", code, e)
                        kline = []
                    tr = self._trade_return(kline, d.strftime("%Y-%m-%d"),
                                            params["hold_days"])
                    if tr:
                        trades.append({
                            "date": d.strftime("%Y-%m-%d"),
                            "code": code,
                            "boards": int(s.get("boards") or 0),
                            "theme": s.get("theme") or "",
                            "entry_close": tr[0], "exit_close": tr[1],
                            "return_pct": tr[2],
                        })
            d += timedelta(days=1)
        return self._report(trades, dates, params)

    # ---------------- 统计 ----------------
    @staticmethod
    def _report(trades, dates, params):
        n = len(trades)
        if n == 0:
            return {"params": params, "trading_days": len(dates),
                    "trades": 0, "win_rate": None, "avg_return_pct": None,
                    "profit_loss_ratio": None, "max_drawdown_pct": None,
                    "total_return_pct": None}
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
            "params": params,
            "trading_days": len(dates),
            "trades": n,
            "win_rate": round(win_rate, 4),
            "avg_return_pct": round(avg_ret, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio else None,
            "max_drawdown_pct": round(max_dd, 2),
            "total_return_pct": round(sum(returns), 2),
        }

    # ---------------- 参数对比 ----------------
    def compare_params(self, start_date, end_date, param_grid, progress=None):
        """param_grid: [{min_limit_count, max_picks, hold_days}, ...] 多组参数 → 对比表。"""
        rows = []
        for params in param_grid:
            rep = self.run(start_date, end_date, params=params, progress=progress)
            rows.append({
                "min_limit_count": rep["params"]["min_limit_count"],
                "max_picks": rep["params"]["max_picks"],
                "hold_days": rep["params"]["hold_days"],
                "trades": rep["trades"],
                "win_rate": rep["win_rate"],
                "avg_return_pct": rep["avg_return_pct"],
                "profit_loss_ratio": rep["profit_loss_ratio"],
                "max_drawdown_pct": rep["max_drawdown_pct"],
            })
        return rows
