# -*- coding: utf-8 -*-
"""因子迁移比对: 新因子(prism.factors) vs 旧实现(datasource.factors) 输出一致。

对全部 23 个存活迁移因子逐因子构造相同假数据, 分别跑旧实现与新注册因子, 断言
score(必要时含 note)一致; 另含 36 因子注册核对与 full_factor_v1 验收位(文件 Task F2 落盘)。
东财因子不调网络: ctx.fund 直接注入假数据。
"""
import sys
import datetime as _dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
import pandas as pd
import pytest

import prism.registry as reg
from prism.context import FactorContext
import prism.factors  # noqa: F401  触发扫描注册

# ---- 旧实现(比对基准) ----
from datasource.factors import FactorEngine  # noqa: F401
from datasource.fundamental import FundamentalFeed  # noqa: F401


ALL_23_IDS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7",
              "Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7",
              "S2", "S3", "S4", "S6",
              "N1", "N2", "N3", "N4", "N5"]


def make_kline(closes, volumes=None):
    n = len(closes)
    volumes = volumes or np.full(n, 100000)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open": closes, "high": [c * 1.02 for c in closes],
        "low": [c * 0.98 for c in closes], "close": closes,
        "volume": volumes, "amount": [c * v * 100 for c, v in zip(closes, volumes)],
    })


class FakeDS:
    """旧实现需要的假 DataSource(注入 kline/index_kline, 不连行情)。"""

    def __init__(self, kline_map=None, index_map=None):
        self.kline_map = kline_map or {}
        self.index_map = index_map or {}

    def get_kline(self, code, days=120):
        return self.kline_map.get(code)

    def get_index_kline(self, code, days=60):
        return self.index_map.get(code)


@pytest.fixture(scope="module", autouse=True)
def _scan():
    reg.reset()
    # force=True: 模块顶部 import prism.factors 已导入因子模块, 无 force 时
    # importlib 一次性语义使重扫为 no-op → FACTORS 空 → 全部 UnknownFactorError
    reg.scan_factors("prism.factors", force=True)
    yield


def _ctx(**kw):
    return FactorContext(code=kw.pop("code", "600000.SH"), **kw)


# ---------- 单股因子: 新旧同输入 ----------

def _stock_ctx(code, tick, detail, kline=None, index_kline=None,
               sector_map=None, limit_ups=None):
    """新实现上下文: 与旧 compute_factors 的入参语义一一对应。
    sealed 按旧逻辑 (askPrice[0]==0) 推导; float_vol 从 detail 传入(供 F3)。"""
    sealed = bool((tick.get("askPrice") or [0])[0] == 0)
    return _ctx(code=code, tick=tick, kline=kline, index_kline=index_kline,
                sector_map=sector_map or {}, limit_ups=limit_ups or [],
                last=tick.get("lastPrice"), last_close=tick.get("lastClose"),
                up_price=detail.get("UpStopPrice"), sealed=sealed,
                float_vol=detail.get("FloatVolume"))


def _old_stock(code, tick, detail, kline=None, index_kline=None,
               sector_map=None, limit_ups=None):
    """旧实现: FactorEngine.compute_factors(同一份假数据)。"""
    ds = FakeDS(index_map={"000001.SH": index_kline}
                if index_kline is not None else {})
    return FactorEngine().compute_factors(
        code, tick, detail, ds, sector_map or {}, limit_ups=limit_ups,
        kline=kline, index_kline=index_kline)


def _hit_tick(last=11.0, last_close=10.0, **extra):
    tick = {"lastPrice": last, "lastClose": last_close, "askPrice": [0],
            "amount": 1e6, "volume": 200000}
    tick.update(extra)
    return tick


def _hit_detail(up=11.0, float_vol=1e8):
    return {"UpStopPrice": up, "FloatVolume": float_vol}


# ---------- F1 首板确认 ----------

def test_f1_matches_old():
    code = "600000.SH"
    closes = [10.0] * 100 + [11.0] + [10.0] * 148 + [11.0]
    kline = make_kline(closes, [100000] * 250)
    tick, detail = _hit_tick(11.0, 10.0), _hit_detail(11.0)
    old = _old_stock(code, tick, detail, kline=kline)["F1"]
    res = reg.get_factor("F1")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1
    assert res["note"] == old["note"]


def test_f1_prior_limitup_within_20d_matches_old():
    code = "000001.SZ"
    closes = [10.0] * 240 + [11.0, 11.0] + [12.1]
    kline = make_kline(closes, [100000] * 243)
    tick, detail = _hit_tick(12.1, 11.0), _hit_detail(12.1)
    old = _old_stock(code, tick, detail, kline=kline)["F1"]
    res = reg.get_factor("F1")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0
    assert "已有涨停" in res["note"]


def test_f1_bj_10pct_day_is_not_prior_limitup():
    """北交所 30% 档: 窗口内 10% 的上涨不是涨停, 不能据此排除候选。

    判别逻辑: 窗口内唯一可疑 K 线是 10.0 → 11.0(+10%)。
      * 用 10% 档(改前的 F1 自带实现, 北交所缺 30% 档) → 11.0 >= 10.0*1.1 - 0.01
        → prev_limit=True → score 0(误排除)。
      * 用 30% 档(与 shared/exit_rules 同口径) → 11.0 < 10.0*1.3 - 0.01
        → prev_limit=False; 今日 13.0 = 10.0*1.3 是真涨停 → score 1。
    旧实现(datasource.factors)同为 10% 档, 故此例对旧实现的差异是预期的, 不比对。
    """
    code = "920001.BJ"
    closes = [10.0] * 20 + [11.0, 10.0] + [13.0]
    kline = make_kline(closes, [100000] * len(closes))
    tick, detail = _hit_tick(13.0, 10.0), _hit_detail(13.0)
    res = reg.get_factor("F1")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == 1


# ---------- F2 早封板 ----------

def test_f2_epoch_ms_early_matches_old():
    code = "600000.SH"
    ts_ms = int(_dt.datetime(2026, 8, 11, 9, 30).timestamp() * 1000)
    tick, detail = _hit_tick(timetag=ts_ms), _hit_detail()
    old = _old_stock(code, tick, detail)["F2"]
    res = reg.get_factor("F2")["func"](_stock_ctx(code, tick, detail))
    assert res["score"] == old["score"] == 1


def test_f2_epoch_ms_late_matches_old():
    code = "600000.SH"
    ts_ms = int(_dt.datetime(2026, 8, 11, 14, 5).timestamp() * 1000)
    tick, detail = _hit_tick(timetag=ts_ms), _hit_detail()
    old = _old_stock(code, tick, detail)["F2"]
    res = reg.get_factor("F2")["func"](_stock_ctx(code, tick, detail))
    assert res["score"] == old["score"] == 0


def test_f2_string_timetag_matches_old():
    code = "600000.SH"
    tick, detail = _hit_tick(timetag="20260811 09:30:00"), _hit_detail()
    old = _old_stock(code, tick, detail)["F2"]
    res = reg.get_factor("F2")["func"](_stock_ctx(code, tick, detail))
    assert res["score"] == old["score"] == 1


def test_f2_not_sealed_matches_old():
    code = "600000.SH"
    tick, detail = _hit_tick(askPrice=[10.0], timetag="20260811 09:30:00"), _hit_detail()
    old = _old_stock(code, tick, detail)["F2"]
    res = reg.get_factor("F2")["func"](_stock_ctx(code, tick, detail))
    assert res["score"] == old["score"] == 0
    assert "未封板" in res["note"]


# ---------- F3 封单强度 ----------
# 注: 2026-09-02 单位修复(bidVol=手, 封单额=bidVol×100×价)。旧实现缺 ×100
# (按头算金额) 是 bug, 迁移对比不再适用 → 改为独立语义断言, 不再比对 _old_stock。

def test_f3_seal_strength_passes_when_sufficient():
    """1万手封单(≈1亿股) ×11元 = 1.1e9 ≥ 流通市值1.1e9×0.5%=5.5e6 → 1分。"""
    code = "000001.SZ"
    tick, detail = (_hit_tick(bidVol=[10000, 0, 0, 0, 0],
                              bidPrice=[11.0, 0, 0, 0, 0]),
                    _hit_detail(11.0, 1e8))     # 流通市值1.1e9
    res = reg.get_factor("F3")["func"](_stock_ctx(code, tick, detail))
    assert res["score"] == 1


def test_f3_seal_strength_fails_when_insufficient():
    """100手封单(1万股) ×11元 = 1.1e5 < 流通市值0.5% → 0分。"""
    code = "000001.SZ"
    tick, detail = (_hit_tick(bidVol=[100, 0, 0, 0, 0],
                              bidPrice=[11.0, 0, 0, 0, 0]),
                    _hit_detail(11.0, 1e8))
    res = reg.get_factor("F3")["func"](_stock_ctx(code, tick, detail))
    assert res["score"] == 0
    assert "封单不足" in res["note"]


# ---------- F4 板块共振 ----------

def test_f4_sector_resonance_matches_old():
    code = "000001.SZ"
    sector_map = {"000001.SZ": "SW2半导体", "000002.SZ": "SW2半导体",
                  "000003.SZ": "SW2半导体", "000004.SZ": "SW2半导体"}
    limit_ups = [{"code": c} for c in ["000001.SZ", "000002.SZ", "000003.SZ"]]
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, sector_map=sector_map,
                     limit_ups=limit_ups)["F4"]
    res = reg.get_factor("F4")["func"](_stock_ctx(
        code, tick, detail, sector_map=sector_map, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 1


def test_f4_insufficient_matches_old():
    code = "000001.SZ"
    sector_map = {"000001.SZ": "SW2半导体", "000002.SZ": "SW2半导体"}
    limit_ups = [{"code": c} for c in ["000001.SZ", "000002.SZ"]]
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, sector_map=sector_map,
                     limit_ups=limit_ups)["F4"]
    res = reg.get_factor("F4")["func"](_stock_ctx(
        code, tick, detail, sector_map=sector_map, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 0


# ---------- F5 量价堆积 ----------

def test_f5_volume_accum_matches_old():
    code = "600000.SH"
    # 前5天放量(3x基准量), 后15天基准量 → ma5=100000, 阈值150000, 前5天命中 → days=5
    vols = [300000] * 5 + [100000] * 15
    kline = make_kline([10.0] * 20, vols)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["F5"]
    res = reg.get_factor("F5")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1


def test_f5_miss_matches_old():
    code = "600000.SH"
    kline = make_kline([10.0] * 20, [100000] * 20)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["F5"]
    res = reg.get_factor("F5")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0


# ---------- F6 大盘配合 ----------

def test_f6_above_ma20_matches_old():
    code = "600000.SH"
    idx = make_kline(list(np.linspace(3000, 3400, 30)), [100000] * 30)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, index_kline=idx)["F6"]
    res = reg.get_factor("F6")["func"](_stock_ctx(code, tick, detail, index_kline=idx))
    assert res["score"] == old["score"] == 1


def test_f6_flat_matches_old():
    code = "600000.SH"
    idx = make_kline([3000.0] * 30, [100000] * 30)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, index_kline=idx)["F6"]
    res = reg.get_factor("F6")["func"](_stock_ctx(code, tick, detail, index_kline=idx))
    assert res["score"] == old["score"] == 0
    assert "大盘环境一般" in res["note"]


# ---------- 东财因子(F7/Y1/Y2/Y5/Y6/Y7): ctx.fund 注入, 不调网络 ----------

def test_f7_reads_fund_score():
    ctx = _ctx(fund={"F7": {"score": 1, "note": "今日题材 X 为近 5 日首次出现(新颖)"}})
    res = reg.get_factor("F7")["func"](ctx)
    assert res["score"] == 1
    assert res["note"] == ctx.fund["F7"]["note"]


def test_y1_matches_old_computation():
    # 旧逻辑 Y1 = FundamentalFeed._small_cap(纯计算); 新因子读 ctx.fund["Y1"] 的 score
    f = FundamentalFeed(http_get=_never_http, cache_path=None)
    old = f._small_cap(79e8)
    assert old["score"] == 1
    res = reg.get_factor("Y1")["func"](_ctx(fund={"Y1": old}))
    assert res["score"] == old["score"]


@pytest.mark.parametrize("fid,score", [("Y2", 1), ("Y5", 1), ("Y6", 1), ("Y7", 1),
                                       ("Y2", 0), ("Y5", 0), ("Y6", 0), ("Y7", 0)])
def test_fund_factor_reads_score(fid, score):
    ctx = _ctx(fund={fid: {"score": score, "note": "假数据"}})
    res = reg.get_factor(fid)["func"](ctx)
    assert res["score"] == score


@pytest.mark.parametrize("fid", ["F7", "Y1", "Y2", "Y5", "Y6", "Y7"])
def test_fund_factor_missing_fail_open(fid):
    res = reg.get_factor(fid)["func"](_ctx(fund={}))
    assert res["score"] == 0
    assert "缺失" in res["note"]


def _never_http(*a, **k):
    raise AssertionError("迁移测试不得发起网络请求")


# ---------- Y3 倍量突破 ----------

def test_y3_matches_old():
    code = "600000.SH"
    kline = make_kline([10] * 25, [100000] * 24 + [500000])
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["Y3"]
    res = reg.get_factor("Y3")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1


def test_y3_miss_matches_old():
    code = "600000.SH"
    kline = make_kline([10] * 25, [100000] * 25)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["Y3"]
    res = reg.get_factor("Y3")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0


# ---------- Y4 均线多头 ----------

def test_y4_matches_old():
    code = "600000.SH"
    kline = make_kline(list(np.linspace(10, 30, 80)), [100000] * 80)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["Y4"]
    res = reg.get_factor("Y4")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1


def test_y4_flat_matches_old():
    code = "600000.SH"
    kline = make_kline([10.0] * 80, [100000] * 80)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["Y4"]
    res = reg.get_factor("Y4")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0


# ---------- S2 量价堆积密度 ----------

def test_s2_matches_old():
    code = "600000.SH"
    # 60日窄幅(全10元) + 20天放量(3x) → big=20 >= 20 且 narrow → 1
    kline = make_kline([10.0] * 60, [100000] * 40 + [300000] * 20)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["S2"]
    res = reg.get_factor("S2")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1


def test_s2_miss_matches_old():
    code = "600000.SH"
    kline = make_kline([10.0] * 60, [100000] * 60)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["S2"]
    res = reg.get_factor("S2")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0


# ---------- S3 最小阻力突破 ----------

def test_s3_matches_old():
    code = "600000.SH"
    # 120日上升 + 今日放量3x(>ma60v*1.5) → 突破 → 1
    kline = make_kline(list(np.linspace(10, 20, 120)), [100000] * 119 + [300000])
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["S3"]
    res = reg.get_factor("S3")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1


def test_s3_flat_matches_old():
    code = "600000.SH"
    kline = make_kline([10.0] * 120, [100000] * 120)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["S3"]
    res = reg.get_factor("S3")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0


# ---------- S4 均线系统 ----------

def test_s4_matches_old():
    code = "600000.SH"
    kline = make_kline(list(np.linspace(10, 30, 300)), [100000] * 300)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["S4"]
    res = reg.get_factor("S4")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 1


def test_s4_flat_matches_old():
    code = "600000.SH"
    kline = make_kline([10.0] * 300, [100000] * 300)
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, kline=kline)["S4"]
    res = reg.get_factor("S4")["func"](_stock_ctx(code, tick, detail, kline=kline))
    assert res["score"] == old["score"] == 0


# ---------- S6 板块当日走强(2026-09-19 起**刻意**与旧实现分道) ----------
# 旧 S6 与 F4 表达式逐字相同(板块涨停≥3家), 同一信号被首板层与势能层各记一分。
# 用户拍板"给 S6 自由度" → 新 S6 改用板块指数当日涨幅≥1%(数据走 ctx.mkt, 本文件
# 其余用例不注入 mkt), 故此处**不再比对旧实现**, 只锁定新行为并记录差异 ——
# 与 test_f1_bj_10pct_day_is_not_prior_limitup 同一处理约定(旧口径差异是预期的,
# 不比对)。S6×F4 列联表/重叠率与双向反判别用例见 test_sector_factors.py。

def test_s6_no_longer_mirrors_old_sector_count():
    """板块内 3 只涨停: 旧 S6 得 1(与 F4 同式), 新 S6 无板块K线数据 → fail-closed 0。"""
    code = "000001.SZ"
    sector_map = {"000001.SZ": "SW2半导体", "000002.SZ": "SW2半导体",
                  "000003.SZ": "SW2半导体"}
    limit_ups = [{"code": c} for c in ["000001.SZ", "000002.SZ", "000003.SZ"]]
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, sector_map=sector_map,
                     limit_ups=limit_ups)["S6"]
    res = reg.get_factor("S6")["func"](_stock_ctx(
        code, tick, detail, sector_map=sector_map, limit_ups=limit_ups))
    assert old["score"] == 1        # 旧实现: 板块涨停≥3家(与 F4 逐字同式)
    assert res["score"] == 0        # 新实现: 无 mkt → 0, 不退回 sector_count


def test_s6_insufficient_still_zero():
    """板块内仅 2 只涨停: 新旧都 0(此处无分歧, 保留以防新实现误命中)。"""
    code = "000001.SZ"
    sector_map = {"000001.SZ": "SW2半导体", "000002.SZ": "SW2半导体"}
    limit_ups = [{"code": c} for c in ["000001.SZ", "000002.SZ"]]
    tick, detail = _hit_tick(), _hit_detail()
    old = _old_stock(code, tick, detail, sector_map=sector_map,
                     limit_ups=limit_ups)["S6"]
    res = reg.get_factor("S6")["func"](_stock_ctx(
        code, tick, detail, sector_map=sector_map, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 0


# ---------- 市场因子(N1-N5): ticks/limit_ups/em 构造 ----------

def _market_ctx(ticks, limit_ups=None, em=None, index_kline=None):
    return _ctx(code="__MARKET__", ticks=ticks, limit_ups=limit_ups or [],
                em=em or {}, index_kline=index_kline)


def _old_market(ticks, limit_ups=None, em=None, index_map=None):
    ds = FakeDS(index_map=index_map or {})
    return FactorEngine().compute_market_factors(ds, ticks,
                                                 limit_ups=limit_ups, em=em)


def test_n1_real_data_above_mean_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": [], "max_boards": 5}
    limit_ups = [{"code": "000001.SZ"}] * 40  # 40 > 均值25
    old = _old_market(ticks, limit_ups=limit_ups, em=em)["N1"]
    res = reg.get_factor("N1")["func"](_market_ctx(ticks, limit_ups=limit_ups, em=em))
    assert res["score"] == old["score"] == 1
    assert "东财真数据" in res["note"]


def test_n1_fallback_flat_index_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    idx = make_kline([3000.0] * 8, [100000] * 8)
    limit_ups = [{"code": "000001.SZ"}]
    old = _old_market(ticks, limit_ups=limit_ups, em=None,
                      index_map={"880368.SH": idx})["N1"]
    res = reg.get_factor("N1")["func"](_market_ctx(ticks, limit_ups=limit_ups,
                                                   index_kline=idx))
    assert res["score"] == old["score"] == 0
    assert "兜底" in res["note"]


def test_n1_fallback_no_index_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "000001.SZ"}]
    old = _old_market(ticks, limit_ups=limit_ups, em=None)["N1"]
    res = reg.get_factor("N1")["func"](_market_ctx(ticks, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 1  # 今日有涨停即1(兜底)


def test_n2_many_limitups_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": True} for i in range(55)]
    old = _old_market(ticks, limit_ups=limit_ups)["N2"]
    res = reg.get_factor("N2")["func"](_market_ctx(ticks, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 1


def test_n2_20_49_high_seal_ratio_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": (i % 5 != 0)} for i in range(30)]
    old = _old_market(ticks, limit_ups=limit_ups)["N2"]
    res = reg.get_factor("N2")["func"](_market_ctx(ticks, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 1  # 封板率80%>60%


def test_n2_low_seal_ratio_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": False} for i in range(30)]
    old = _old_market(ticks, limit_ups=limit_ups)["N2"]
    res = reg.get_factor("N2")["func"](_market_ctx(ticks, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 0


def test_n3_real_data_positive_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12, "lastPrice": 11.0, "lastClose": 10.0},
             "000002.SZ": {"amount": 1e12, "lastPrice": 10.5, "lastClose": 10.0}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001.SZ", "000002.SZ", "000003.SZ"],
          "max_boards": 5}
    old = _old_market(ticks, em=em)["N3"]
    res = reg.get_factor("N3")["func"](_market_ctx(ticks, em=em))
    assert res["score"] == old["score"] == 1
    assert "东财真数据" in res["note"]


def test_n3_bare_codes_resolve_suffix_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12, "lastPrice": 11.0, "lastClose": 10.0},
             "600519.SH": {"amount": 1e12, "lastPrice": 20.0, "lastClose": 19.0}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001", "600519", "999999"], "max_boards": 5}
    old = _old_market(ticks, em=em)["N3"]
    res = reg.get_factor("N3")["func"](_market_ctx(ticks, em=em))
    assert res["score"] == old["score"] == 1


def test_n3_no_match_keeps_zero_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": ["123456"],
          "max_boards": 5}
    old = _old_market(ticks, em=em)["N3"]
    res = reg.get_factor("N3")["func"](_market_ctx(ticks, em=em))
    assert res["score"] == old["score"] == 0
    assert "无匹配" in res["note"]


def test_n3_fallback_today_pool_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "000001.SZ", "last": 11.0, "last_close": 10.0}]
    old = _old_market(ticks, limit_ups=limit_ups, em=None)["N3"]
    res = reg.get_factor("N3")["func"](_market_ctx(ticks, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 1
    assert "兜底" in res["note"]


def test_n4_high_board_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30], "yesterday_codes": [], "max_boards": 5}
    old = _old_market(ticks, em=em)["N4"]
    res = reg.get_factor("N4")["func"](_market_ctx(ticks, em=em))
    assert res["score"] == old["score"] == 1
    assert "东财真数据" in res["note"]


def test_n4_promotion_ratio_hit_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001", "000002", "000003"],
          "yesterday_boards": ["000001", "000002", "000003"], "max_boards": 4}
    limit_ups = [{"code": "000001.SZ"}, {"code": "000002.SZ"}]  # 晋级2/3=66.7%
    old = _old_market(ticks, limit_ups=limit_ups, em=em)["N4"]
    res = reg.get_factor("N4")["func"](_market_ctx(ticks, limit_ups=limit_ups, em=em))
    assert res["score"] == old["score"] == 1
    assert "晋级率" in res["note"]


def test_n4_promotion_ratio_miss_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    em = {"daily_counts": [20, 30, 25, 20, 30],
          "yesterday_codes": ["000001", "000002", "000003"],
          "yesterday_boards": ["000001", "000002", "000003"], "max_boards": 4}
    limit_ups = [{"code": "600000.SH"}]  # 晋级率 0%
    old = _old_market(ticks, limit_ups=limit_ups, em=em)["N4"]
    res = reg.get_factor("N4")["func"](_market_ctx(ticks, limit_ups=limit_ups, em=em))
    assert res["score"] == old["score"] == 0


def test_n4_fallback_matches_old():
    ticks = {"000001.SZ": {"amount": 1e12}}
    limit_ups = [{"code": "00000%d.SZ" % i, "sealed": True} for i in range(35)]
    old = _old_market(ticks, limit_ups=limit_ups, em=None)["N4"]
    res = reg.get_factor("N4")["func"](_market_ctx(ticks, limit_ups=limit_ups))
    assert res["score"] == old["score"] == 1
    assert "60%" in res["note"]


def test_n5_above_threshold_matches_old():
    ticks = {"000001.SZ": {"amount": 1.5e12}, "000002.SZ": {"amount": 1.0e12}}
    old = _old_market(ticks, limit_ups=[])["N5"]
    res = reg.get_factor("N5")["func"](_market_ctx(ticks))
    assert res["score"] == old["score"] == 1


def test_n5_below_threshold_matches_old():
    ticks = {"000001.SZ": {"amount": 1.0e12}, "000002.SZ": {"amount": 0.5e12}}
    old = _old_market(ticks, limit_ups=[])["N5"]
    res = reg.get_factor("N5")["func"](_market_ctx(ticks))
    assert res["score"] == old["score"] == 0


# ---------- 注册完整性 + 分类 ----------

def test_all_23_factors_registered():
    for fid in ALL_23_IDS:
        assert fid in reg.FACTORS, "因子未注册: %s" % fid


def test_factor_categories():
    assert reg.FACTORS["F1"]["category"] == "first_board"
    assert reg.FACTORS["F7"]["category"] == "first_board"
    assert reg.FACTORS["Y1"]["category"] == "monster"
    assert reg.FACTORS["S2"]["category"] == "momentum"
    for fid in ["N1", "N2", "N3", "N4", "N5"]:
        assert reg.FACTORS[fid]["category"] == "node"


# ---------- 2026-09-03 清理: 删除因子缺席 + full_factor_v1 验收 ----------

def test_removed_factors_absent():
    """2026-09-03 清理: M1-M5 与 S1/S5/S7 已删除, 不再注册。"""
    for fid in ("M1", "M2", "M3", "M4", "M5", "S1", "S5", "S7"):
        assert fid not in reg.FACTORS, "%s 应已删除" % fid
    assert len(reg.FACTORS) == 36


def test_full_factor_v1_loads():
    """验收: full_factor_v1 四层 35 因子可加载且因子全部注册。

    2026-09-19: S2(量价堆积密度)已从势能板块层**摘除**(全窗口 0 命中, 改 T-1
    窗口后仍 0/492 —— 两子条件在涨停池宇宙量级互斥; A/B 同窗回测逐项一致),
    故评分 28→27、合计 36→35、cap 9.3333→9.0。S2 因子本身**仍注册**(未被删),
    只是不再进本策略; 见 `docs/reports/回测全因子复活对比_20260916.md` §6.2。
    """
    from prism.engine import load_strategy
    s = load_strategy(Path(__file__).parent.parent / "strategies"
                      / "full_factor_v1.json")
    assert s["id"] == "full_factor_v1"
    fids = [f for m in s["scoring_models"] for f in m["factors"]]
    gate = s["market_gate"]["factors"]
    assert len(fids) == 27            # 9 首板 + 8 妖股 + 10 势能板块(原 11, 摘 S2)
    assert len(gate) == 8             # N1-N8
    assert len(set(fids) | set(gate)) == 35   # 评分与门控无重叠, 合计 35
    assert "S2" not in fids, "S2 已于 2026-09-19 摘除(0 命中 + A/B 无差异)"
    assert all(f in reg.FACTORS for f in set(fids) | set(gate))
    comp = s["composite"]
    assert comp["mode"] == "average" and abs(comp["cap"] - 9.0) < 1e-9
    assert "weights" not in comp
    ex = s["execution"]
    assert ex["top_n"] == 5 and ex["pct"] == 0.15
    assert s["sell_rules"]["take_profit_pct"] == 0.15
