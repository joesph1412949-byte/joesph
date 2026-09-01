# -*- coding: utf-8 -*-
"""数据适配层: 把 QMT/东财/手填 数据封装成因子上下文。

第一版复用 strategy_web 现有实现(DataSource/EastMoneyFeed/FundamentalFeed/
ManualStore), 迁移完成后逐步内联。因子永远不直接碰本层。
"""
import sys
from pathlib import Path

_WEB = str(Path(__file__).parent.parent / "strategy_web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)


class DataProvider:
    """薄封装: 对外只暴露 build_*_context / get_* 等数据提供接口。"""

    def __init__(self, ds=None, em_feed=None, fund_feed=None, manual=None):
        from data_source import DataSource
        from eastmoney import EastMoneyFeed
        from fundamental import FundamentalFeed
        from manual_store import ManualStore
        self.ds = ds or DataSource()
        self.em_feed = em_feed or EastMoneyFeed()
        self.fund_feed = fund_feed or FundamentalFeed()
        self.manual = manual or ManualStore()
        self._em_stats = None
        self._limit_ups_cache = None

    def connect(self):
        return self.ds.connect()

    @property
    def connected(self):
        return getattr(self.ds, "_connected", False)

    def get_limit_ups(self):
        """返回涨停股列表(与旧 screen 同构)。未连接 → []。

        provider 级缓存(self._limit_ups_cache): 成功结果(含空涨停池)缓存,
        避免每只股票都全市场拉取一次; 失败(异常)不缓存, 保持 None 以便下次重试。
        """
        if not self.connected:
            return []
        if self._limit_ups_cache is None:
            try:
                ticks = self.ds.get_full_market_ticks()
                self._limit_ups_cache = self.ds.get_limit_up_stocks(ticks)
            except Exception:
                return []
        return self._limit_ups_cache

    def get_market_stats(self):
        """东财市场统计(涨停池/题材/连板), 失败 → None。"""
        if self._em_stats is None:
            try:
                self._em_stats = self.em_feed.get_market_stats()
            except Exception:
                self._em_stats = None
        return self._em_stats

    def invalidate(self):
        """清空 provider 级缓存(盘后定时跑选股时刷新用)。"""
        self._limit_ups_cache = None
        self._em_stats = None

    def build_market_context(self):
        """市场因子上下文(N 系因子用)。"""
        from prism.context import FactorContext
        ticks = {}
        limit_ups = []
        index_kline = None
        if self.connected:
            try:
                ticks = self.ds.get_full_market_ticks()
                limit_ups = self.ds.get_limit_up_stocks(ticks)
            except Exception:
                pass
            # 成功结果预热 provider 级缓存, 后续单股上下文复用同一份涨停池,
            # 不再重复全市场拉取。仅预热非空结果, 失败/空不写缓存(保持可重试)。
            if limit_ups and self._limit_ups_cache is None:
                self._limit_ups_cache = limit_ups
            # N1 兜底需要 880368 涨停指数(迁移规则: ds.get_index_kline→ctx.index_kline)
            try:
                index_kline = self.ds.get_index_kline("880368.SH", days=8)
            except Exception:
                index_kline = None
        em = self.get_market_stats()
        ctx = FactorContext(code="__MARKET__", ticks=ticks, limit_ups=limit_ups,
                            em=em or {}, index_kline=index_kline)
        # v04 接线: mkt 快照 + 行业映射注入 _extra(F8/F9/SEC* 因子用)。
        # 全部读本地缓存, 失败 fail-open(缺数据 → 因子得 0, 不阻塞选股)。
        # sector_map 是 FactorContext 显式字段: get()/engine 优先读字段,
        # 只写 _extra 会被字段遮蔽, 故字段与 _extra 同引一份映射。
        try:
            from prism import market_data as _md
            cache = _md._load_cache()
            if cache:
                ctx._extra["mkt"] = _md.mkt_snapshot()
                smap = {}
                for c6, rec in (cache.get("sector_map") or {}).items():
                    if rec and rec.get("sector"):
                        suffix = ".SH" if c6.startswith("6") else ".SZ"
                        smap[c6 + suffix] = rec["sector"]
                ctx._extra["sector_map"] = smap
                ctx.sector_map = smap
        except Exception:
            pass
        return ctx

    def build_stock_context(self, code, kline=None, index_kline=None,
                            sector_map=None):
        """单股因子上下文。数据缺失一律 None(fail-open)。"""
        from prism.context import FactorContext
        tick = {}
        float_mv = None
        float_vol = None
        up_price = None
        sealed = None
        if self.connected:
            try:
                ticks = self.ds.get_full_market_ticks([code])
                tick = ticks.get(code, {})
            except Exception:
                pass
            # sealed: askPrice 首档为 0 → 封板(与旧 factors.py 同公式)。
            # askPrice 字段缺失时保持 None(fail-open), 避免把无数据误判为封板。
            ask = tick.get("askPrice")
            sealed = bool((ask or [0])[0] == 0) if ask is not None else None
            try:
                det = self.ds.get_instrument(code)
                float_mv = (det.get("FloatVolume") or 0) * (tick.get("lastPrice") or 0)
                float_vol = det.get("FloatVolume")
                up_price = det.get("UpStopPrice")
            except Exception:
                pass
        if kline is None and self.connected:
            try:
                kline = self.ds.get_kline(code, days=250)
            except Exception:
                kline = None
        fund = {}
        try:
            fund = self.fund_feed.compute_for_stock(code, float_mv=float_mv)
        except Exception:
            fund = {}
        manual = self.manual.get_manual(code)
        return FactorContext(
            code=code, tick=tick, kline=kline, index_kline=index_kline,
            sector_map=sector_map or {}, float_mv=float_mv, float_vol=float_vol,
            limit_ups=self.get_limit_ups(),
            em=self.get_market_stats() or {}, fund=fund, manual=manual,
            last=tick.get("lastPrice"), last_close=tick.get("lastClose"),
            up_price=up_price, sealed=sealed)


# 模块级单例: 旧代码/脚本直接 `from prism.data import provider` 取用。
# 构造只做轻量初始化(读两个 JSON 缓存), 无网络/无行情副作用。
provider = DataProvider()
