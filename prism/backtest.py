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

from shared.exit_rules import ExitRule
from prism import registry as reg
from prism import sector_score
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


def _cut(rec, key, asof_str):
    """K线记录切片: {"dates": [...], key: [...]}, 只留 dates <= asof 的下标。

    key 传 str 或 tuple(str)(tuple 供同记录多值列, 如 sector 的 close+amount);
    值为 None 的列不切(与旧实现 `if rec.get("amount") is not None` 同语义)。
    无 <=asof 的数据 → None。
    防未来函数核心, 逐字保持: 同下标同步拷贝, 不重排不改值。
    """
    dates = rec.get("dates") or []
    keep = [i for i, d in enumerate(dates) if d <= asof_str]
    if not keep:
        return None
    out = {"dates": [dates[i] for i in keep]}
    for k in ((key,) if isinstance(key, str) else key):
        if rec.get(k) is not None:
            out[k] = [rec[k][i] for i in keep]
    return out


def _cut_rows(rows, key, asof_str):
    """{code: 记录} 整体切片: 空切片项直接丢弃(与旧实现 `if keep` 同语义)。"""
    out = {}
    for code, rec in (rows or {}).items():
        cut = _cut(rec, key, asof_str)
        if cut:
            out[code] = cut
    return out


def _slice_mkt(mkt, asof):
    """把完整市场数据缓存切成 asof 日快照(防未来函数)。

    mkt: 市场数据层缓存, {"sector": {code: {"dates":[...], "close":[...]},
    "global": {...}}}。返回 {"sector": {...}} 只含
    dates <= asof 的K线(与 _pick 的股票K线同语义: 因子只见当日及之前)。
    """
    if not mkt:
        return {}
    fields = mkt if isinstance(mkt, dict) else {}   # 非 dict 脏数据 → 只剩空 sector
    asof_str = asof.strftime("%Y-%m-%d")
    out = {"sector": {}}
    # sector: close 恒切, amount(可选, SEC4 拥挤度)有值才切
    for code, rec in (fields.get("sector") or {}).items():
        keys = ("close", "amount") if rec.get("amount") is not None else "close"
        cut = _cut(rec, keys, asof_str)
        if cut:
            out["sector"][code] = cut
    # global(美股映射) / sector_flow(SEC3 板块资金流) 同款切片, 无数据不带键
    for name, key in (("global", "close"), ("sector_flow", "main_net_in")):
        if fields.get(name):
            out[name] = _cut_rows(fields[name], key, asof_str)
    # 商品期货(F8 行业边际变化): 板块 → 品种 → K线 两层嵌套, 单独展开
    futs = fields.get("futures") or {}
    if futs:
        out["futures"] = {}
        for scode, rec in futs.items():
            comms = {}
            for sym, k in (rec.get("commodities") or {}).items():
                cut = _cut(k, "close", asof_str)
                if cut:
                    comms[sym] = dict(cut, name=k.get("name"))
            out["futures"][scode] = {"name": rec.get("name"),
                                     "commodities": comms}
    return out


class Backtester:
    """真实策略回放 + 交易模拟。"""

    def __init__(self, strategy, zt_feed, kline_feed,
                 fee_rate=0.00025, slippage=0.001, position_ratio=0.3,
                 stamp_duty=0.0005, transfer_fee=0.00001,
                 initial_capital=1000000.0, max_positions=5, fund_feed=None):
        self.strategy = load_strategy(strategy)
        self.zt_feed = zt_feed
        self.kline_feed = kline_feed
        # fund_feed: 可选(datasource.fundamental.FundamentalFeed)。
        # 注入后 F7/Y6/Y7 在回测中按选股日 asof 取数(防未来函数, feed 内部
        # 保证窗口不越 asof)。默认 None → fund 不注入, 这三个因子得 0
        # (与旧行为一致)。注意 Y5/Y2 是"当前快照"类数据, 回测接入自带
        # 未来性——默认接入也不启用它们(skip_snapshot_fund)。
        self.fund_feed = fund_feed
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
        # 过滤统计(2026-09-04): 零交易时能分辨"门控没过"还是"候选被过滤"
        # ——网页回测曾缺市场数据注入导致三个策略静默零交易, 无从归因。
        self._filter_stats = self._new_filter_stats()

    @staticmethod
    def _new_filter_stats():
        return {"days": 0, "gate_blocked_days": 0, "candidates": 0,
                "filtered_min_model": 0, "filtered_sector": 0}

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
    def _stock_ctx(self, code, kline, mkt=None, sector_map=None,
                   limit_ups=None, fund=None):
        """回测环境的股票上下文(只用回测可得的字段)。

        kline_feed 元组 → DataFrame(open/high/low/close/volume, 日期做行索引),
        满足真实因子按 kline["close"]/kline["volume"]/len(kline) 访问。

        **kline_feed 契约(向后兼容三档, 按元组长度自动识别)**:
          (date, close)                          旧契约 —— 只有收盘价
          (date, close, volume)                  量能契约
          (date, open, high, low, close, volume) 全量 OHLCV 契约
        只有收盘价的旧契约下: volume 占位 1.0(量比类因子不会误命中)、
        open/high/low 取 close。注意此时 F5/Y3/S2/S3 量能因子与形态因子
        是"静默失效"而非"不命中", 回测结果不能用来评估这些因子。
        mkt: 市场数据层快照(供 SEC 板块因子), 注入 ctx._extra["mkt"]。
        sector_map: code → 行业板块代码(供 SEC 因子查个股所属板块)。
        """
        rows = []
        for row in kline or []:
            # 契约三档按长度识别: 2=(date,close) / 3=(date,close,vol) /
            # 6=(date,open,high,low,close,vol)。异常行直接丢弃, 不让单条脏数据炸整段。
            try:
                dt = str(row[0])
                if len(row) >= 6:
                    o, h, l, c, v = (float(row[1]), float(row[2]),
                                     float(row[3]), float(row[4]),
                                     float(row[5]))
                elif len(row) >= 3:
                    c = float(row[1])
                    v = float(row[2])
                    o = h = l = c
                else:
                    c = float(row[1])
                    o = h = l = c
                    v = 1.0
                rows.append((dt, o, h, l, c, v))
            except (TypeError, ValueError, IndexError):
                continue
        extra = {}
        if mkt is not None:
            extra["mkt"] = mkt
        if sector_map is not None:
            extra["sector_map"] = sector_map
        if limit_ups is not None:
            extra["limit_ups"] = limit_ups
        if fund is not None:
            extra["fund"] = fund
        if not rows:
            return FactorContext(code=code, kline=None, **extra)
        df = pd.DataFrame({
            "close": [r[4] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "volume": [r[5] for r in rows],
        }, index=[r[0] for r in rows])
        return FactorContext(code=code, kline=df, **extra)

    def _pick(self, pool, asof, em=None, ticks=None, mkt=None, sector_map=None,
              prev_pool=None):
        """用策略引擎对当日涨停池选股。返回 [(code, boards, theme, composite,
        sec_score), ...](sec_score: 板块综合评分, 评分关闭/无数据时 None)。

        asof: 选股日期(date)——关键!因子只能看到 <= asof 的K线,
        绝不使用未来数据(未来函数会让回测结果虚假虚高)。

        市场门槛(节点因子)不达标 → 空仓; 达标后逐股 build FactorContext
        调 compute_model_scores, 按 candidate_min_model 过滤, 综合分降序。
        策略开启 sector_score(v5)时: 切片数据现算板块综合评分,
        无评分或 ≤threshold → 剔除(fail-closed, 设计 §4.2)。

        数据适配层(审查 I1): 市场上下文从池子合成 limit_ups(补
        sealed/last/last_close, 没有就 None), em/ticks 可注入(供 N3/N5 等);
        mkt(市场数据层快照, 供 SEC 板块因子)可注入, 防未来函数由外部保证
        (只传入 asof 当日及之前的数据); 未注入时依赖这些数据的门槛因子得 0
        (见 _gate_notes 归因)。
        prev_pool: 上一有效交易日涨停池, 注入 mkt['zt_prev'] 供 F9。
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
        # 防未来函数: 市场数据也按 asof 切片(因子只见当日及之前)
        mkt_sliced = _slice_mkt(mkt, asof)
        # F9 板块延展性: 上一交易日涨停代码表(由 run() 维护传入)
        if prev_pool is not None:
            mkt_sliced["zt_prev"] = {"codes": [s["code"] for s in prev_pool]}
        # v5 板块综合评分(可选): 切片数据现算一次, 门槛过滤 + 记分
        score_cfg = sector_score.load_config(self.strategy)
        sec_scores = (sector_score.compute_scores(mkt_sliced)
                      if score_cfg["enabled"] else {})
        mkt_extra = {"mkt": mkt_sliced}
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
            self._filter_stats["gate_blocked_days"] += 1
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
            ctx = self._stock_ctx(code, kline, mkt_sliced, sector_map,
                                  limit_ups=pool_ctx,
                                  fund=self._fund_for(code, asof,
                                                      s.get("float_mv")))
            scores = compute_model_scores(ctx, self.strategy)
            best = max([scores[m["id"]]
                        for m in self.strategy["scoring_models"]], default=0)
            self._filter_stats["candidates"] += 1
            if best >= min_model:
                sec_score = None
                if score_cfg["enabled"]:
                    sec = (sector_map or {}).get(code)
                    if isinstance(sec, dict):
                        sec = sec.get("sector")
                    # str() 归一(审查 Minor#5): compute_scores 键为 str(code),
                    # sector_map 值为 int 时直接 get 会静默 miss → 误判无评分
                    rec = sec_scores.get(str(sec)) if sec else None
                    sec_score = rec["score"] if rec else None
                    # fail-closed: 无评分/低分板块不买(设计 §4.2)
                    if sec_score is None or sec_score <= score_cfg["threshold"]:
                        self._filter_stats["filtered_sector"] += 1
                        continue
                out.append((code, s.get("boards", 0), s.get("theme", ""),
                            scores["composite"], sec_score))
            else:
                self._filter_stats["filtered_min_model"] += 1
        out.sort(key=lambda x: x[3], reverse=True)
        # 每日选股上限: 与净值模拟的 max_positions 一致(每天最多买 N 只,
        # 否则一天 50+ 只候选会把资金抽干, 净值模拟里几乎全部跳过)
        return out[: self.max_positions]

    def _fund_for(self, code, asof, float_mv=None):
        """个股基本面因子快照(fund_feed 注入时)。

        float_mv: 涨停池条目自带的流通市值(如有), 供 Y1/Y8 纯计算因子。
        防未来函数: 传 asof=选股日, feed 内部(Y7 龙虎榜/Y6 公告/F7 涨停池)
        窗口不越 asof。"当前快照"类(Y5 概念/Y2 股东户数)对回测自带未来性,
        剔除 —— 宁缺勿假(与 fail-closed 原则一致)。
        fund_feed 未注入 → None(ctx.fund 为空, F7/Y6/Y7 得 0, 旧行为)。
        """
        if self.fund_feed is None:
            return None
        try:
            fund = dict(self.fund_feed.compute_for_stock(
                code, float_mv=float_mv, asof=asof) or {})
        except Exception:
            return None
        for k in ("Y5", "Y2"):   # 快照类: 回测场景剔除, 防未来数据
            fund.pop(k, None)
        return fund or None

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
        策略开启 sector_score(v5)时: 板块评分 >threshold 才买(fail-closed),
        trade 记 sector_score/pos_mult, 单笔投入按 pos_mult 放大。
        """
        defaults = {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                    "max_hold_days": 5}
        cfg_rules = dict(defaults)
        cfg_rules.update(self.strategy.get("sell_rules") or {})
        rules = dict(cfg_rules, **(sell_rules or {}))
        score_cfg = sector_score.load_config(self.strategy)
        gate_notes = self._gate_notes(em, ticks)
        trades = []
        dates = []
        self._filter_stats = self._new_filter_stats()   # 每次 run 重置诊断计数
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
        prev_pool = []   # 上一有效交易日涨停池(池空日不更新, 近似"昨日池")
        while d <= end_date:
            if progress:
                progress(d)
            pool = self._pool_for(d)
            if pool:
                dates.append(d)
                self._filter_stats["days"] += 1
                for code, boards, theme, composite, sec_score in self._pick(
                        pool, asof=d, em=em, ticks=ticks, mkt=mkt,
                        sector_map=sector_map, prev_pool=prev_pool):
                    kline = self._kline_for(code)
                    tr = self._simulate_trade(code, kline, d, rules)
                    if tr:
                        if score_cfg["enabled"]:
                            mult = sector_score.position_multiplier(
                                sec_score, score_cfg, self.position_ratio)
                        else:
                            mult = 1.0
                        trades.append({
                            "date": d.strftime("%Y-%m-%d"), "code": code,
                            "boards": boards, "theme": theme,
                            "composite": composite,
                            "sector_score": sec_score, "pos_mult": mult,
                            "entry": tr[0], "exit": tr[1],
                            "return_pct": tr[2], "cost_pct": tr[3],
                            "exit_date": tr[4].strftime("%Y-%m-%d")
                            if tr[4] else None,
                        })
                prev_pool = pool   # 今日池成为下一有效交易日的"昨日池"
            d += timedelta(days=1)
        # 净值模拟: 资金约束下的净值曲线 → 总收益/回撤/夏普(真实口径)
        curve, skipped = self._simulate_equity(trades)
        eq_stats = self._equity_stats(curve, self.initial_capital)
        return self._report(trades, dates, gate_notes=gate_notes,
                            equity_stats=eq_stats, skipped_cash=skipped,
                            filter_stats=self._filter_stats)

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
        # 移动止盈(可选): 达到起赚点后回撤 N% 即卖, 锁住浮盈
        trailing = rules.get("trailing_pct")
        exit_close = None
        exit_date = None
        peak_pct = 0.0          # 持有期内最高浮盈(相对买入价)
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
            # 移动止盈: 需退出规则未触发时检查(用 trailing 阈值)
            if trailing:
                cur_pct = (px / buy_price - 1) * 100
                peak_pct = max(peak_pct, cur_pct)
                if peak_pct >= trailing[0] and \
                        cur_pct <= peak_pct - trailing[1]:
                    exit_close = px
                    exit_date = today
                    break
        if exit_close is None:
            return None
        # ---- 真实交易成本(A股标准) ----
        # 买入成本: 佣金(双向) + 过户费(双向) + 滑点(买+; buy_price 见上)
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
          - 每笔投入 = 当日净值 × position_ratio × pos_mult
            (动态仓位, 复利增长; pos_mult 为 v5 板块评分乘数, 缺省 1.0)
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
        # 持仓: [(uid, 投入本金, 该笔收益率)]
        # 盯市: 持有期内按已实现收益率估算当前市值(day_nav 用于下一笔建仓定档)
        holdings = []
        all_days = sorted(set(list(events.keys()) +
                              [t["date"] for t in trades]))
        for day in all_days:
            ev = events.get(day, {"sell": [], "buy": []})
            # 1) 卖出: 回款 本金×(1+收益率), 从持仓移除(按 uid 精确匹配)
            for t in ev["sell"]:
                uid = "%s|%s" % (t["date"], t["code"])
                for i, h in enumerate(holdings):
                    if h[0] == uid:
                        holdings.pop(i)
                        cash += h[1] * (1 + t["return_pct"] / 100.0)
                        break
                # 未成交的买入(不在持仓) → 不回款
            # 2) 买入: 按当日净值×仓位比例投入, 现金足且持仓未满
            day_nav = cash + sum(p * (1 + r / 100.0)
                                 for _u, p, r in holdings)
            for t in ev["buy"]:
                # per_trade 必须在循环内用本笔的 t(审查 Critical#1: 循环外
                # 引用泄漏变量, 当日多笔买入共用无关交易的乘数)
                mult = float(t.get("pos_mult") or 1.0)
                per_trade = day_nav * self.position_ratio * mult
                if cash >= per_trade and len(holdings) < self.max_positions:
                    cash -= per_trade
                    holdings.append(("%s|%s" % (t["date"], t["code"]),
                                     per_trade, t["return_pct"]))
                else:
                    skipped += 1
            # 3) 当日净值: 现金 + 持仓按成本计(持有期内保守按成本)
            nav = cash + sum(p for _u, p, _r in holdings)
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
    def _report(trades, dates, gate_notes, equity_stats, skipped_cash=0,
                filter_stats=None):
        """汇总回测报告。

        equity_stats: _equity_stats 的结果(基于资金模拟净值曲线), 含
        total_return_pct/max_drawdown_pct/sharpe_ratio。
        filter_stats: 过滤统计(门控拦了几天/候选被 min_model/板块过滤多少只)——
        零交易时用于归因, 避免"静默零交易"。"""
        n = len(trades)
        base = {"trading_days": len(dates), "trades": n,
                "gate_notes": gate_notes or [], "skipped_cash": skipped_cash,
                "filter_stats": filter_stats or {}}
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
        # 净值口径指标: 总收益/最大回撤/夏普 基于资金模拟曲线
        total_ret = equity_stats.get("total_return_pct")
        max_dd = equity_stats.get("max_drawdown_pct")
        sharpe = equity_stats.get("sharpe_ratio")
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
            "filter_stats": filter_stats or {},
            "win_rate": round(win_rate, 4),
            "avg_return_pct": round(avg_ret, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio else None,
            "max_drawdown_pct": max_dd,
            "total_return_pct": total_ret,
            "avg_cost_pct": round(avg_cost, 3) if avg_cost is not None else None,
            "sharpe_ratio": sharpe,
            "trade_log": trade_log,
        }

    # ---------------- 防过拟合: 样本外验证 ----------------
    def run_oos(self, start_date, end_date, split_ratio=0.5,
                sell_rules=None, progress=None, em=None, ticks=None,
                mkt=None, sector_map=None):
        """样本外验证(Out-of-Sample): 把区间按时间切成两段,
        前段(样本内)回测 + 后段(样本外)回测, 对比两者绩效。

        防过拟合逻辑: 若策略只在样本内好、样本外崩, 说明过拟合了参数;
        样本外绩效与样本内接近(或不明显恶化)才算稳健。
        返回 {in_sample: 报告, out_sample: 报告, verdict: 判语}。
        em/ticks/mkt/sector_map: 透传给 run(与 run 语义一致)。
        """
        total = (end_date - start_date).days
        if total < 6:
            return {"error": "区间太短(<6天), 无法做样本外分割"}
        split = start_date + timedelta(days=int(total * split_ratio))
        ins = self.run(start_date, split, sell_rules=sell_rules,
                       progress=progress, em=em, ticks=ticks,
                       mkt=mkt, sector_map=sector_map)
        oos = self.run(split + timedelta(days=1), end_date,
                       sell_rules=sell_rules, progress=progress,
                       em=em, ticks=ticks, mkt=mkt, sector_map=sector_map)
        # 判语: 样本外有交易 且 样本外均值收益不为负 → 稳健; 否则警告
        verdict = "稳健(样本外仍有正收益)"
        if not oos.get("trades"):
            verdict = "警告: 样本外无交易(数据不足或策略失效)"
        elif (oos.get("avg_return_pct") or 0) < 0:
            verdict = "警告: 样本外平均收益为负, 疑似过拟合"
        return {"in_sample": ins, "out_sample": oos, "verdict": verdict}
