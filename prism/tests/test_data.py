# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext
from prism.data import DataProvider


class FakeDS:
    """模拟已连接的 DataSource(无 QMT 依赖)。

    注: 真实 DataSource 依赖 xtquant, 测试一律注入假实现, 绝不打真行情。
    除简报给定的 get_limit_up_stocks/get_kline 外, 补 _connected 与
    get_full_market_ticks —— DataProvider.connected/get_limit_ups 会读它们,
    缺了无法走到假数据(fail-open 会静默吞掉)。
    """

    _connected = True

    def get_full_market_ticks(self, codes=None):
        return {"600000.SH": {"lastPrice": 10.5}}

    def get_limit_up_stocks(self, ticks=None):
        return [{"code": "600000.SH", "name": "浦发", "last": 10.5}]

    def get_kline(self, code, days=120):
        return None


class _FakeManual:
    """手动因子存储的假实现(get_manual 返回空字典)。"""

    def get_manual(self, code):
        return {}


class FakeProvider(DataProvider):
    """绕过真实构造(不实例化线上数据源), 只注入假数据。

    简报原版把 fund/manual/em 设成裸 dict, 但实现读的是
    fund_feed/em_feed/manual/_em_stats 这几个属性 —— 这里补齐为
    实现真正读取的对象, 断言与简报完全一致。
    """

    def __init__(self):
        self.ds = FakeDS()
        self.em_feed = None
        self.fund_feed = None
        self.manual = _FakeManual()
        self._em_stats = None
        self._limit_ups_cache = None


def test_provider_builds_stock_context():
    p = FakeProvider()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.code == "600000.SH"
    assert ctx.kline is None          # 数据缺失 → None, 不崩


def test_provider_get_limit_ups():
    p = FakeProvider()
    ups = p.get_limit_ups()
    assert ups[0]["code"] == "600000.SH"


def test_build_stock_context_includes_limit_ups():
    """回归: ctx.limit_ups 必须包含涨停池(曾因 _lazy 标志未设置而恒空)。"""
    p = FakeProvider()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.limit_ups[0]["code"] == "600000.SH"


class _CountingDS(FakeDS):
    """在 FakeDS 上给 get_limit_up_stocks 加调用计数。"""

    def __init__(self):
        self.calls = 0

    def get_limit_up_stocks(self, ticks=None):
        self.calls += 1
        return [{"code": "600000.SH", "name": "浦发", "last": 10.5}]


def test_get_limit_ups_cached():
    """缓存验证: 连续两次 get_limit_ups 只触发一次底层全市场拉取。"""
    ds = _CountingDS()
    p = FakeProvider()
    p.ds = ds
    p._limit_ups_cache = None
    assert p.get_limit_ups()[0]["code"] == "600000.SH"
    assert p.get_limit_ups()[0]["code"] == "600000.SH"
    assert ds.calls == 1


def test_get_limit_ups_not_cached_on_failure():
    """失败不缓存: 底层抛异常返回 [] 且缓存保持 None, 下次调用会重试。"""
    class BoomDS(FakeDS):
        def get_limit_up_stocks(self, ticks=None):
            raise RuntimeError("limit-up boom")

    p = FakeProvider()
    p.ds = BoomDS()
    p._limit_ups_cache = None
    assert p.get_limit_ups() == []
    assert p._limit_ups_cache is None


def test_build_market_context_warms_limit_ups_cache():
    """市场上下文成功后预热缓存: 后续 get_limit_ups 不再重复全市场拉取。"""
    ds = _CountingDS()
    p = FakeProvider()
    p.ds = ds
    p._limit_ups_cache = None
    ctx = p.build_market_context()
    assert ctx.limit_ups[0]["code"] == "600000.SH"
    assert p.get_limit_ups()[0]["code"] == "600000.SH"
    assert ds.calls == 1


def test_invalidate_clears_caches():
    """invalidate 清空 provider 级缓存(盘后定时跑选股时刷新用)。"""
    p = FakeProvider()
    p._limit_ups_cache = [{"code": "600000.SH"}]
    p._em_stats = {"some": "stats"}
    p.invalidate()
    assert p._limit_ups_cache is None
    assert p._em_stats is None


def test_context_from_provider_delegates():
    """FactorContext.from_provider 薄委托给 provider.build_stock_context。"""
    p = FakeProvider()
    ctx = FactorContext.from_provider(p, "600000.SH")
    assert ctx.code == "600000.SH"
    assert ctx.kline is None


def test_build_stock_context_fail_open_when_ds_raises():
    """真实 DataProvider 构造(注入假 DS): ds.get_kline 抛异常仍返回上下文(fail-open)。

    同时验证 import 现有 strategy_web 模块(data_source/eastmoney/fundamental/
    manual_store)不破坏构造 —— 本测试走的是真实 __init__ 路径。
    """
    class BoomDS:
        _connected = True

        def get_full_market_ticks(self, codes=None):
            return {}

        def get_instrument(self, code):
            return {}

        def get_kline(self, code, days=250):
            raise RuntimeError("kline boom")

    class BoomEM:
        def get_market_stats(self):
            raise RuntimeError("em boom")

    class BoomFund:
        def compute_for_stock(self, code, float_mv=None):
            raise RuntimeError("fund boom")

    p = DataProvider(ds=BoomDS(), em_feed=BoomEM(), fund_feed=BoomFund(),
                     manual=_FakeManual())
    ctx = p.build_stock_context("600000.SH")
    assert ctx.code == "600000.SH"
    assert ctx.kline is None
    assert ctx.fund == {}
