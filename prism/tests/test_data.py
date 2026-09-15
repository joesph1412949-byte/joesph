# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.data import DataProvider


class FakeDS:
    """模拟已连接的 DataSource(无 QMT 依赖)。

    注: 真实 DataSource 依赖 xtquant, 测试一律注入假实现, 绝不打真行情。
    除简报给定的 get_limit_up_stocks/get_kline 外, 补 _connected 与
    get_full_market_ticks —— DataProvider.connected/get_limit_ups 会读它们,
    缺了无法走到假数据(fail-open 会静默吞掉)。
    tick 带 askPrice/bidPrice: build_stock_context 用 askPrice[0]==0 判断
    sealed(F2/F3 依赖); get_instrument 提供 UpStopPrice/FloatVolume。
    """

    _connected = True

    def get_full_market_ticks(self, codes=None):
        return {"600000.SH": {"lastPrice": 10.5, "lastClose": 10.0,
                              "askPrice": [0, 0, 0, 0, 0],
                              "bidPrice": [10.5, 10.49, 0, 0, 0],
                              "bidVol": [1000000, 0, 0, 0, 0]}}

    def get_limit_up_stocks(self, ticks=None):
        return [{"code": "600000.SH", "name": "浦发", "last": 10.5}]

    def get_instrument(self, code):
        return {"UpStopPrice": 11.0, "FloatVolume": 2e8}

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


class _F3DS(FakeDS):
    """带 get_instrument(FloatVolume) 与 get_index_kline 的假 DS(供 F3/N1 数据扩展)。"""

    def get_instrument(self, code):
        return {"FloatVolume": 2e8}

    def get_index_kline(self, index_code, days=60):
        return "idx-kline-%s" % index_code


def test_build_stock_context_provides_float_vol_for_f3():
    """F3 需要流通股本(FloatVolume): ctx.get("float_vol") 必须携带。"""
    p = FakeProvider()
    p.ds = _F3DS()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.get("float_vol") == 2e8


def test_build_stock_context_injects_up_price_and_sealed():
    """回归: F1/F2/F3 依赖 up_price/sealed, 数据层必须注入(曾硬编码 None)。

    生产链路 F1(需 up_price)/F2(需 sealed)/F3(需 sealed+float_vol) 曾恒为 0,
    因为 build_stock_context 硬编码 up_price=None/sealed=None; Task 5 迁移测试
    手动注入这些字段绕过数据层, 掩盖了问题。
    """
    p = FakeProvider()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.up_price == 11.0              # 从 instrument 详情 UpStopPrice 取
    assert ctx.sealed is True                # 从 tick askPrice[0]==0 判断
    assert ctx.get("float_vol") == 2e8       # F3 用


def test_build_stock_context_sealed_false_when_ask_has_price():
    """sealed 分支: askPrice 首档 > 0 → 未封板(False), 不能误判为封板。"""
    class AskHasPriceDS(FakeDS):
        def get_full_market_ticks(self, codes=None):
            return {"600000.SH": {"lastPrice": 10.5, "lastClose": 10.0,
                                  "askPrice": [10.51, 10.52, 0, 0, 0]}}

    p = FakeProvider()
    p.ds = AskHasPriceDS()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.sealed is False


def test_build_stock_context_sealed_none_when_tick_missing_ask():
    """fail-open: tick 无 askPrice 字段时 sealed 保持 None, 不得误判为封板。"""
    class NoAskDS(FakeDS):
        def get_full_market_ticks(self, codes=None):
            return {"600000.SH": {"lastPrice": 10.5, "lastClose": 10.0}}

    p = FakeProvider()
    p.ds = NoAskDS()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.sealed is None
    assert ctx.up_price == 11.0             # instrument 详情不受 tick 缺失影响


def test_data_layer_to_factors_smoke_f1_f2_f3():
    """冒烟: 假 DS 构造含 askPrice/UpStopPrice/FloatVolume 数据, 走
    build_stock_context → 因子 compute → F1/F2/F3 必须能拿到正确输入出分
    (曾因数据层硬编码 None 而恒 0, 迁移测试手动注入掩盖)。"""
    import pandas as pd
    from prism import registry as reg

    closes = [10.0] * 249 + [11.0]           # 今日涨停(11.0), 近20日无涨停
    kline = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=250, freq="B"),
        "open": closes, "high": [c * 1.02 for c in closes],
        "low": [c * 0.98 for c in closes], "close": closes,
        "volume": [100000] * 250, "amount": [c * 100000 * 100 for c in closes],
    })

    class SmokeDS(FakeDS):
        def get_kline(self, code, days=250):
            return kline

        def get_full_market_ticks(self, codes=None):
            return {"600000.SH": {"lastPrice": 11.0, "lastClose": 10.0,
                                  "askPrice": [0, 0, 0, 0, 0],
                                  "bidPrice": [11.0, 10.99, 0, 0, 0],
                                  "bidVol": [2000000, 0, 0, 0, 0],
                                  "timetag": "20260811 09:30:00"}}

    reg.scan_factors("prism.factors", force=True)
    p = FakeProvider()
    p.ds = SmokeDS()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.up_price == 11.0 and ctx.sealed is True
    for fid in ("F1", "F2", "F3"):
        res = reg.get_factor(fid)["func"](ctx)
        assert res["score"] == 1, "%s 应为 1: %s" % (fid, res["note"])


def test_build_market_context_provides_880368_index_for_n1():
    """N1 兜底需要 880368 涨停指数: ctx.index_kline 必须携带(供 N1 因子)。"""
    p = FakeProvider()
    p.ds = _F3DS()
    ctx = p.build_market_context()
    assert ctx.index_kline == "idx-kline-880368.SH"


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


# ---------------------------------------------------- Y1/Y8 float_mv 通达信兜底
def test_build_stock_context_float_mv_tdx_fallback(monkeypatch):
    """Y1/Y8 兜底: QMT FloatVolume 缺失时降级通达信流通股本(单位=股, 同量纲)。

    float_mv/float_vol 均应来自 tdx 兜底值 × 实时价(600519 实测 ≈ 12.5 亿股)。
    """
    from prism import tdx_source

    class NoFloatDS(FakeDS):
        def get_instrument(self, code):
            return {"UpStopPrice": 11.0}          # 无 FloatVolume

    monkeypatch.setattr(tdx_source, "float_shares", lambda code: 1.25e9)
    p = FakeProvider()
    p.ds = NoFloatDS()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.get("float_vol") == 1.25e9
    assert ctx.float_mv == 1.25e9 * 10.5


def test_build_stock_context_qmt_float_present_skips_tdx(monkeypatch):
    """QMT FloatVolume 正常时行为不变且不发起 tdx 调用。

    patch 成"被调即 raise"并另记调用列表 —— fail-open 会吞掉异常,
    只靠"不炸"断不住误调用, 必须显式断言 calls 为空(测试确定性)。
    """
    from prism import tdx_source
    calls = []

    def _no_tdx(code):
        calls.append(code)
        raise AssertionError("FloatVolume 正常时不应调用 tdx float_shares")

    monkeypatch.setattr(tdx_source, "float_shares", _no_tdx)
    p = FakeProvider()                            # FakeDS: FloatVolume=2e8
    ctx = p.build_stock_context("600000.SH")
    assert calls == []
    assert ctx.get("float_vol") == 2e8
    assert ctx.float_mv == 2e8 * 10.5


def test_build_stock_context_tdx_boom_fail_open(monkeypatch):
    """tdx 兜底抛异常 → 优雅降级: float_mv/float_vol 为 None, 上下文照常返回。"""
    from prism import tdx_source

    def _boom(code):
        raise RuntimeError("tdx boom")

    monkeypatch.setattr(tdx_source, "float_shares", _boom)

    class NoFloatDS(FakeDS):
        def get_instrument(self, code):
            return {}

    p = FakeProvider()
    p.ds = NoFloatDS()
    ctx = p.build_stock_context("600000.SH")      # 不抛即优雅
    assert ctx.float_mv is None
    assert ctx.get("float_vol") is None


def test_build_stock_context_float_mv_none_without_price():
    """价格 fail-open: 有股本无 lastPrice → float_mv None(现状算出 0, 不拿 0 凑)。"""
    class NoPriceDS(FakeDS):
        def get_full_market_ticks(self, codes=None):
            return {"600000.SH": {"lastClose": 10.0,
                                  "askPrice": [0, 0, 0, 0, 0]}}

    p = FakeProvider()
    p.ds = NoPriceDS()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.get("float_vol") == 2e8            # QMT 股本仍在, 无 tdx 介入
    assert ctx.float_mv is None
