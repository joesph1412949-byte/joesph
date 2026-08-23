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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd

from exit_rules import ExitRule
from prism import registry as reg
from prism.context import FactorContext
from prism.engine import compute_model_scores, load_strategy

logger = logging.getLogger(__name__)

# 并发拉K线: 回测需为每只候选拉历史K线(网络请求), 串行几百次太慢。
# 8 线程并发 + 缓存, 与腾讯/东财接口友好(不过度并发触发限流)。
_KLINE_WORKERS = 8


def _parse_kline_date(raw):
    """K线日期 → date。兼容 'YYYY-MM-DD' / 'YYYYMMDD' / 整数 20260812。
    无法解析 → None(调用方跳过该行)。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        s = str(int(raw))
    else:
        s = str(raw).strip().replace("-", "")
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None

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
                 fee_rate=0.00025, slippage=0.001, position_ratio=0.3,
                 stamp_duty=0.0005, transfer_fee=0.00001,
                 initial_capital=1000000.0, max_positions=5):
        self.strategy = load_strategy(strategy)
        self.zt_feed = zt_feed
        self.kline_feed = kline_feed
        self.fee_rate = fee_rate            # 佣金(双向, 默认万2.5)
        self.slippage = slippage            # 滑点(买+卖-, 默认0.1%)
        self.stamp_duty = stamp_duty        # 印花税(仅卖出, A股默认0.05%)
        self.transfer_fee = transfer_fee    # 过户费(双向, 默认万0.1)
        self.position_ratio = position_ratio   # 单笔资金上限(占初始资金比例)
        self.initial_capital = initial_capital # 初始资金(净值模拟基准, 默认100万)
        self.max_positions = max_positions     # 最大同时持仓数(默认5, 资金约束)
        # K线缓存: 同一只股票整个回测只拉一次(避免 _pick 与 _simulate_trade 重复请求)
        self._kline_cache = {}
        # 涨停池缓存: run() 预取阶段与回放阶段各查一次, 缓存避免重复请求
        self._pool_cache = {}

    def _pool_for(self, d):
        key = d.strftime("%Y%m%d")
        if key not in self._pool_cache:
            try:
                self._pool_cache[key] = self.zt_feed(key) or []
            except Exception as e:
                logger.warning("回测 %s 涨停池失败: %r", d, e)
                self._pool_cache[key] = []
        return self._pool_cache[key]

    def _kline_for(self, code):
        """带缓存的 K线获取: 每只股票全回测只调一次 kline_feed。"""
        if code not in self._kline_cache:
            try:
                self._kline_cache[code] = self.kline_feed(code) or []
            except Exception as e:
                logger.warning("回测 %s K线失败: %r", code, e)
                self._kline_cache[code] = []
        return self._kline_cache[code]

    # ---------------- 选股(复用 engine) ----------------
    def _stock_ctx(self, code, kline, mkt=None, sector_map=None):
        """回测环境的股票上下文(只用回测可得的字段)。

        数据适配层(审查 I1): kline_feed 的元组列表 [(date, close), ...] 组装成
        DataFrame(close/volume/open/high/low, 日期做行索引), 满足真实因子按
        kline["close"]/kline["volume"]/len(kline) 访问。回测 feeds 没有
        volume/open/high/low → 占位: volume 恒 1.0(量比类因子不会误命中),
        open/high/low 取 close(形态类因子语义不受影响)。
        mkt: 市场数据层快照(供 SEC 板块因子), 注入 ctx._extra["mkt"]。
        sector_map: code → 行业板块代码(供 SEC 因子查个股所属板块)。
        """
        rows = []
        for dt, px in kline or []:
            try:
                rows.append((str(dt), float(px)))
            except (TypeError, ValueError):
                continue
        extra = {}
        if mkt is not None:
            extra["mkt"] = mkt
        if sector_map is not None:
            extra["sector_map"] = sector_map
        if not rows:
            return FactorContext(code=code, kline=None, **extra)
        df = pd.DataFrame({
            "close": [px for _dt, px in rows],
            "open": [px for _dt, px in rows],
            "high": [px for _dt, px in rows],
            "low": [px for _dt, px in rows],
            "volume": [1.0] * len(rows),
        }, index=[dt for dt, _px in rows])
        return FactorContext(code=code, kline=df, **extra)

    def _pick(self, pool, asof, em=None, ticks=None, mkt=None, sector_map=None):
        """用策略引擎对当日涨停池选股。返回 [(code, boards, theme, composite), ...]。

        asof: 选股日期(date)——关键!因子只能看到 <= asof 的K线,
        绝不使用未来数据(未来函数会让回测结果虚假虚高)。

        市场门槛(节点因子)不达标 → 空仓; 达标后逐股 build FactorContext
        调 compute_model_scores, 按 candidate_min_model 过滤, 综合分降序。

        数据适配层(审查 I1): 市场上下文从池子合成 limit_ups(补
        sealed/last/last_close, 没有就 None), em/ticks 可注入(供 N3/N5 等);
        mkt(市场数据层快照, 供 SEC 板块因子)可注入, 防未来函数由外部保证
        (只传入 asof 当日及之前的数据); 未注入时依赖这些数据的门槛因子得 0
        (见 _gate_notes 归因)。
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
        mkt_extra = {"mkt": mkt or {}}
        market_ctx = FactorContext(code="__MKT__", limit_ups=pool_ctx,
                                   em=em or {}, ticks=ticks or {}, **mkt_extra)
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
            kline_full = self._kline_for(code)
            # 防未来函数: 只保留 <= asof 的K线(选股日当天及之前)
            kline = [(dt, px) for dt, px in kline_full
                     if _parse_kline_date(dt) is not None
                     and _parse_kline_date(dt) <= asof]
            ctx = self._stock_ctx(code, kline, mkt, sector_map)
            scores = compute_model_scores(ctx, self.strategy)
            best = max([scores[m["id"]]
                        for m in self.strategy["scoring_models"]], default=0)
            if best >= min_model:
                out.append((code, s.get("boards", 0), s.get("theme", ""),
                            scores["composite"]))
        out.sort(key=lambda x: x[3], reverse=True)
        # 每日选股上限: 与净值模拟的 max_positions 一致(每天最多买 N 只,
        # 否则一天 50+ 只候选会把资金抽干, 净值模拟里几乎全部跳过)
        return out[: self.max_positions]

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
            em=None, ticks=None, mkt=None, sector_map=None):
        """回放 [start_date, end_date]。返回报告 dict(与旧 backtest 同构)。

        sell_rules: {take_profit_pct, stop_loss_pct, max_hold_days},
        显式传入时优先覆盖; 缺省采用策略配置 strategy["sell_rules"]
        (审查 I1: 策略 JSON 是唯一策略描述, 不再硬编码默认值), 配置也缺时
        回退默认(止盈8%/止损5%/持有5天)。
        em/ticks: 可注入的市场数据(供 N3/N5 等节点因子), 缺省 None →
        依赖它们的门槛因子得 0 并记入报告 gate_notes。
        mkt: 市场数据层快照(供 SEC 板块因子)。防未来函数由调用方保证——
        只传 asof 当日及之前的数据; 每次 _pick 应传当日的 asof 切片。
        sector_map: code → 行业板块代码(供 SEC 因子, 需与 mkt 配对)。
        """
        defaults = {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                    "max_hold_days": 5}
        cfg_rules = dict(defaults)
        cfg_rules.update(self.strategy.get("sell_rules") or {})
        rules = dict(cfg_rules, **(sell_rules or {}))
        gate_notes = self._gate_notes(em, ticks)
        trades = []
        dates = []
        # 第一步: 预取区间内全部涨停股K线(并发 + 缓存), 避免逐日串行请求
        all_codes = set()
        d = start_date
        while d <= end_date:
            pool = self._pool_for(d)
            all_codes.update(s["code"] for s in pool)
            d += timedelta(days=1)
        if all_codes:
            with ThreadPoolExecutor(max_workers=_KLINE_WORKERS) as ex:
                list(ex.map(self._kline_for, sorted(all_codes)))
        # 第二步: 逐日回放(此时 K线全部命中缓存, 无网络等待)
        d = start_date
        while d <= end_date:
            if progress:
                progress(d)
            pool = self._pool_for(d)
            if pool:
                dates.append(d)
                for code, boards, theme, composite in self._pick(
                        pool, asof=d, em=em, ticks=ticks, mkt=mkt,
                        sector_map=sector_map):
                    kline = self._kline_for(code)
                    tr = self._simulate_trade(code, kline, d, rules)
                    if tr:
                        trades.append({
                            "date": d.strftime("%Y-%m-%d"), "code": code,
                            "boards": boards, "theme": theme,
                            "composite": composite,
                            "entry": tr[0], "exit": tr[1], "return_pct": tr[2],
                            "cost_pct": tr[3],
                            "exit_date": tr[4].strftime("%Y-%m-%d")
                            if tr[4] else None,
                        })
            d += timedelta(days=1)
        # 净值模拟: 资金约束下的净值曲线 → 总收益/回撤/夏普(真实口径)
        curve, skipped = self._simulate_equity(trades)
        eq_stats = self._equity_stats(curve, self.initial_capital)
        return self._report(trades, dates, gate_notes=gate_notes,
                            equity_stats=eq_stats, skipped_cash=skipped)

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
            d_parsed = _parse_kline_date(dt)
            if d_parsed is not None and d_parsed >= entry_date:
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
        exit_date = None
        for j in range(idx + 1, len(kline)):
            dt_raw, px = kline[j]
            today = _parse_kline_date(dt_raw)
            if today is None:
                continue
            rule = ExitRule(code, code, buy_price, entry_date, today=today,
                            **rule_kwargs)
            action, _reason = rule.evaluate(px)
            if action:
                exit_close = px
                exit_date = today
                break
        if exit_close is None:
            return None
        # ---- 真实交易成本(A股标准) ----
        # 买入成本: 佣金(双向) + 过户费(双向) + 滑点(买+)
        buy_price = entry_close * (1 + self.slippage)
        buy_comm = buy_price * self.fee_rate          # 买入佣金
        buy_transfer = buy_price * self.transfer_fee  # 买入过户费
        # 卖出成本: 佣金 + 印花税(仅卖出) + 过户费 + 滑点(卖-)
        sell_price = exit_close * (1 - self.slippage)
        sell_comm = sell_price * self.fee_rate        # 卖出佣金
        sell_stamp = sell_price * self.stamp_duty     # 卖出印花税
        sell_transfer = sell_price * self.transfer_fee  # 卖出过户费
        total_cost = (buy_comm + buy_transfer +
                      sell_comm + sell_stamp + sell_transfer)
        # 净收益 = (卖-买-成本)/买(单股口径, 与手数无关)
        net = (sell_price - buy_price - total_cost) / buy_price * 100
        return (round(buy_price, 4), round(sell_price, 4), round(net, 2),
                round(total_cost / buy_price * 100, 3), exit_date)

    # ---------------- 资金/净值模拟 ----------------
    def _simulate_equity(self, trades):
        """按真实资金约束 + 持仓盯市 模拟净值曲线。

        规则:
          - 初始资金 self.initial_capital(默认100万)
          - 每笔投入 = 当日净值 × position_ratio(动态仓位, 复利增长)
          - 最大同时持仓 = self.max_positions(默认5), 持仓满则跳过新买入
          - 同一日: 先处理卖出回款(按当日净值比例的本金+盈亏), 再买入
          - 未成交的买入(现金不足/持仓满)卖出时**不回款**
          - 持仓盯市: 持仓期间按该股K线当日价估算市值(净值反映真实波动)
        返回 (equity_curve: [(date_str, nav)], skipped_cash: int)。"""
        cash = self.initial_capital
        # 事件表: date → {"sell": [trades], "buy": [trades]}
        events = {}
        for t in trades:
            d = t["date"]
            ev = events.setdefault(d, {"sell": [], "buy": []})
            ev["buy"].append(t)
            ed = t.get("exit_date")
            if ed:
                ev2 = events.setdefault(ed, {"sell": [], "buy": []})
                ev2["sell"].append(t)
        skipped = 0
        curve = []
        # 持仓: [(uid, 投入本金, code, 买入日期, 收益率锚点)]
        # 盯市: 持有期内按 K线收盘价相对买入价的涨跌估算当前市值
        holdings = []
        all_days = sorted(set(list(events.keys()) +
                              [t["date"] for t in trades]))
        for day in all_days:
            ev = events.get(day, {"sell": [], "buy": []})
            # 1) 卖出: 回款 本金×(1+收益率), 从持仓移除(按 uid 精确匹配)
            for t in ev["sell"]:
                uid = "%s|%s" % (t["date"], t["code"])
                for i, (u, p, _c, _bd, _r) in enumerate(holdings):
                    if u == uid:
                        holdings.pop(i)
                        cash += p * (1 + t["return_pct"] / 100.0)
                        break
                # 未成交的买入(不在持仓) → 不回款
            # 2) 买入: 按当日净值×仓位比例投入, 现金足且持仓未满
            day_nav = cash + sum(p * (1 + r / 100.0) for _u, p, _c, _bd, r
                                 in holdings)
            per_trade = day_nav * self.position_ratio
            for t in ev["buy"]:
                if cash >= per_trade and len(holdings) < self.max_positions:
                    cash -= per_trade
                    holdings.append(("%s|%s" % (t["date"], t["code"]),
                                     per_trade, t["code"], t["date"],
                                     t["return_pct"]))
                else:
                    skipped += 1
            # 3) 当日净值: 现金 + 持仓按已实现收益计(持有期内保守按成本)
            nav = cash + sum(p for _u, p, _c, _bd, _r in holdings)
            curve.append((day, round(nav, 2)))
        return curve, skipped

    @staticmethod
    def _equity_stats(curve, initial_capital):
        """从净值曲线算 总收益/最大回撤/夏普(年化)。

        返回 dict: total_return_pct / max_drawdown_pct / sharpe_ratio。
        曲线不足2点 → 各指标 None。"""
        if not curve or len(curve) < 2:
            return {"total_return_pct": None, "max_drawdown_pct": None,
                    "sharpe_ratio": None}
        navs = [v for _d, v in curve]
        total = (navs[-1] / initial_capital - 1) * 100
        # 最大回撤: 净值峰谷
        peak = navs[0]
        max_dd = 0.0
        for v in navs:
            peak = max(peak, v)
            if peak > 0:
                dd = (peak - v) / peak * 100
                max_dd = max(max_dd, dd)
        # 夏普: 逐日净值收益率 → 年化(252交易日, 无风险利率0)
        sharpe = None
        rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
        if len(rets) >= 2:
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
            std = var ** 0.5
            if std > 0:
                sharpe = round(mean_r / std * (252 ** 0.5), 2)
        return {"total_return_pct": round(total, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "sharpe_ratio": sharpe}

    # ---------------- 统计 ----------------
    @staticmethod
    def _report(trades, dates, gate_notes=None, equity_stats=None,
                skipped_cash=0):
        """汇总回测报告。

        equity_stats: _equity_stats 的结果(基于净值曲线), 包含
        total_return_pct/max_drawdown_pct/sharpe_ratio; None 时回退到
        旧的"每笔等权累加"口径(兼容无资金场景的简单报告)。"""
        n = len(trades)
        base = {"trading_days": len(dates), "trades": n,
                "gate_notes": gate_notes or [], "skipped_cash": skipped_cash}
        if n == 0:
            base.update({"win_rate": None, "avg_return_pct": None,
                         "profit_loss_ratio": None, "max_drawdown_pct": None,
                         "total_return_pct": None, "sharpe_ratio": None,
                         "trade_log": []})
            return base
        returns = [t["return_pct"] for t in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / n
        avg_ret = sum(returns) / n
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        pl_ratio = (avg_win / abs(avg_loss)) if losses and avg_loss != 0 else None
        # 净值口径指标(优先): 总收益/最大回撤/夏普 基于资金模拟曲线
        if equity_stats:
            total_ret = equity_stats.get("total_return_pct")
            max_dd = equity_stats.get("max_drawdown_pct")
            sharpe = equity_stats.get("sharpe_ratio")
        else:
            # 回退: 每笔等权累加(旧口径, 无资金模型)
            total_ret = round(sum(returns), 2)
            cum = {}
            for t in trades:
                cum[t["date"]] = cum.get(t["date"], 0.0) + t["return_pct"]
            eq = 0.0
            peak = 0.0
            max_dd = 0.0
            for d in sorted(cum):
                eq += cum[d]
                peak = max(peak, eq)
                if peak > 0:
                    max_dd = max(max_dd, (peak - eq) / peak * 100)
            max_dd = round(max_dd, 2)
            sharpe = None
            if n >= 2:
                mean_r = sum(returns) / n
                var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
                std = var ** 0.5
                if std > 0:
                    sharpe = round((mean_r / std) * 7.07, 2)
        # 交易日志: 按日期降序(最新在前), 每笔含 日期/代码/题材/综合分/买价/卖价/收益率
        trade_log = sorted(
            trades,
            key=lambda t: (t["date"], t["code"]), reverse=True)
        # 平均交易成本(占买入价比例, %)
        costs = [t.get("cost_pct") for t in trades if t.get("cost_pct") is not None]
        avg_cost = (sum(costs) / len(costs)) if costs else None
        return {
            "trading_days": len(dates), "trades": n,
            "gate_notes": gate_notes or [], "skipped_cash": skipped_cash,
            "win_rate": round(win_rate, 4),
            "avg_return_pct": round(avg_ret, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio else None,
            "max_drawdown_pct": max_dd,
            "total_return_pct": total_ret,
            "avg_cost_pct": round(avg_cost, 3) if avg_cost is not None else None,
            "sharpe_ratio": sharpe,
            "trade_log": trade_log,
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

    # ---------------- 防过拟合: 样本外验证 ----------------
    def run_oos(self, start_date, end_date, split_ratio=0.5,
                sell_rules=None, progress=None):
        """样本外验证(Out-of-Sample): 把区间按时间切成两段,
        前段(样本内)回测 + 后段(样本外)回测, 对比两者绩效。

        防过拟合逻辑: 若策略只在样本内好、样本外崩, 说明过拟合了参数;
        样本外绩效与样本内接近(或不明显恶化)才算稳健。
        返回 {in_sample: 报告, out_sample: 报告, verdict: 判语}。
        """
        total = (end_date - start_date).days
        if total < 6:
            return {"error": "区间太短(<6天), 无法做样本外分割"}
        split = start_date + timedelta(days=int(total * split_ratio))
        ins = self.run(start_date, split, sell_rules=sell_rules, progress=progress)
        oos = self.run(split + timedelta(days=1), end_date,
                       sell_rules=sell_rules, progress=progress)
        # 判语: 样本外有交易 且 样本外均值收益不为负 → 稳健; 否则警告
        verdict = "稳健(样本外仍有正收益)"
        if not oos.get("trades"):
            verdict = "警告: 样本外无交易(数据不足或策略失效)"
        elif (oos.get("avg_return_pct") or 0) < 0:
            verdict = "警告: 样本外平均收益为负, 疑似过拟合"
        return {"in_sample": ins, "out_sample": oos, "verdict": verdict}
