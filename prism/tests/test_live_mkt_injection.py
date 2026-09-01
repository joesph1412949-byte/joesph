# -*- coding: utf-8 -*-
"""实盘 mkt/sector_map 接线测试 — 快照组装 + provider 注入 + run_screen 下发。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
import prism.zt_history as zt_history
from prism import market_data as md
from prism.context import FactorContext
from prism.data import DataProvider
from prism.engine import load_strategy, run_screen


@pytest.fixture(autouse=True)
def _fresh_registry():
    reg.reset()
    yield
    reg.reset()
    reg.scan_factors("prism.factors", force=True)   # 恢复真实因子库


class _FakeEM:
    def get_market_stats(self):
        return None


class _FakeManual:
    def get_manual(self, code):
        return {}


class _FakeProvider(DataProvider):
    """绕过真实构造(不实例化线上数据源), 同 test_data.py 模式。"""

    def __init__(self):
        self.ds = None
        self.em_feed = _FakeEM()
        self.fund_feed = None
        self.manual = _FakeManual()
        self._em_stats = None
        self._limit_ups_cache = None


# ---------------- zt_history.prev_day_pool ----------------

def test_prev_day_pool_picks_latest_before_today(monkeypatch):
    monkeypatch.setattr(zt_history, "_load_index", lambda: {
        "20260701": [{"code": "600000.SH"}],
        "20260702": [{"code": "600000.SH"}, {"code": "000001.SZ"}],
        "20260703": [{"code": "x"}]})
    monkeypatch.setattr(
        zt_history, "qmt_zt_feed",
        lambda day, cache=None, index=None:
            [{"code": "600000.SH"}, {"code": "000001.SZ"}])
    out = zt_history.prev_day_pool(today="2026-07-03")
    assert out == {"date": "2026-07-02", "codes": ["600000.SH", "000001.SZ"]}


def test_prev_day_pool_empty_index(monkeypatch):
    monkeypatch.setattr(zt_history, "_load_index", lambda: {})
    out = zt_history.prev_day_pool(today="2026-07-03")
    assert out == {"date": None, "codes": []}


# ---------------- market_data.mkt_snapshot ----------------

def test_mkt_snapshot_assembles_all_sections(monkeypatch):
    monkeypatch.setattr(md, "_load_cache", lambda: {
        "kline": {"BK0475": {"dates": ["2026-07-01"], "close": [1.0]}},
        "global": {"NDX": {"dates": ["2026-07-01"], "close": [1.0]}},
        "flow": {"BK0475": {"dates": ["2026-07-01"], "main_net_in": [1.0]}}})
    monkeypatch.setattr(md, "futures_snapshot", lambda: {
        "BK0475": {"name": "基础化工",
                   "commodities": {"MA0": {"close": [1.0]}}}})
    monkeypatch.setattr(zt_history, "prev_day_pool",
                        lambda today=None: {"date": "2026-07-02",
                                            "codes": ["600000.SH"]})
    snap = md.mkt_snapshot()
    assert snap["sector"]["BK0475"]["close"] == [1.0]
    assert "NDX" in snap["global"]
    assert "BK0475" in snap["sector_flow"]
    assert snap["futures"]["BK0475"]["commodities"]["MA0"]["close"] == [1.0]
    assert snap["zt_prev"]["codes"] == ["600000.SH"]


def test_mkt_snapshot_fail_open(monkeypatch):
    monkeypatch.setattr(md, "_load_cache", lambda: {})

    def boom():
        raise RuntimeError("net down")
    monkeypatch.setattr(md, "futures_snapshot", boom)
    monkeypatch.setattr(zt_history, "prev_day_pool", boom)
    assert md.mkt_snapshot() == {}          # 全缺 → 空快照, 不抛


# ---------------- provider 注入 ----------------

def test_build_market_context_injects_mkt_and_sector_map(monkeypatch):
    monkeypatch.setattr(md, "_load_cache", lambda: {
        "kline": {},
        "sector_map": {"600000": {"sector": "BK0475", "name": "浦发"}}})
    monkeypatch.setattr(md, "mkt_snapshot", lambda: {
        "sector": {}, "zt_prev": {"date": None, "codes": ["600000.SH"]}})
    p = _FakeProvider()
    ctx = p.build_market_context()
    assert ctx.get("mkt")["zt_prev"]["codes"] == ["600000.SH"]
    assert ctx.get("sector_map")["600000.SH"] == "BK0475"


def test_build_market_context_fail_open(monkeypatch):
    def boom():
        raise RuntimeError("cache locked")
    monkeypatch.setattr(md, "_load_cache", boom)
    p = _FakeProvider()
    ctx = p.build_market_context()
    assert ctx.get("mkt") is None           # fail-open: 不抛, 无 mkt


# ---------------- run_screen 下发 ----------------

def _mk_strategy(for_factor):
    return {
        "id": "live_wire", "name": "下发测试", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": for_factor, "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    }


def test_run_screen_f9_hits_with_live_style_pool():
    """实盘通路(F9): strategy_web/data_source.get_limit_up_stocks 产物条目
    无 boards 键, F9 连板判定改用 zt_prev(今日∩昨日)后 run_screen 全链路
    可命中候选(修复前 boards 恒缺失 → 连板恒 0 → F9 实盘完全失效)。"""
    reg.scan_factors("prism.factors", force=True)   # 注册真实 F9/N1(_fresh_registry 已 reset)
    # 实盘样式涨停池条目(与 data_source.get_limit_up_stocks 返回同形, 无 boards)
    ups = [
        {"code": "600000.SH", "name": "浦发银行", "last": 10.0,
         "last_close": 9.09, "up_stop_price": 10.0, "sealed": True,
         "amount": 1e8, "volume": 1e6, "float_volume": 1e9,
         "open_date": "1999-11-10"},
        {"code": "000001.SZ", "name": "平安银行", "last": 11.0,
         "last_close": 10.0, "up_stop_price": 11.0, "sealed": True,
         "amount": 1e8, "volume": 1e6, "float_volume": 1e9,
         "open_date": "1991-04-03"},
        {"code": "000002.SZ", "name": "万科A", "last": 9.9,
         "last_close": 9.0, "up_stop_price": 9.9, "sealed": True,
         "amount": 1e8, "volume": 1e6, "float_volume": 1e9,
         "open_date": "1991-01-29"},
    ]
    smap = {"600000.SH": "BK0475", "000001.SZ": "BK0475",
            "000002.SZ": "BK0475"}
    market_ctx = FactorContext(
        code="__MARKET__",
        mkt={"zt_prev": {"date": "2026-07-02",
                         "codes": ["600000.SH", "000001.SZ"]}},
        sector_map=smap)
    # 个股 ctx: 实盘构建时拿不到 mkt/sector_map, 由 run_screen 下发兜底
    stock = FactorContext(code="600000.SH", kline=None, limit_ups=ups)
    s = load_strategy(_mk_strategy("F9"))
    out = run_screen(s, market_ctx, gate_factors={"N1": 1},
                     stock_contexts={"600000.SH": stock})
    assert len(out["candidates"]) == 1, out["summary"]
    assert out["candidates"][0]["factors"]["F9"] == 1


def test_run_screen_downgrades_mkt_and_sector_map():
    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}

    @reg.factor(id="T8", name="t", category="test", description="")
    def f_t(ctx):
        ok = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes") \
            == ["x"] \
            and (ctx.sector_map or {}).get("600000.SH") == "BK0475"
        return {"score": 1 if ok else 0, "note": ""}

    s = load_strategy(_mk_strategy("T8"))
    market_ctx = FactorContext(code="__MKT__",
                               mkt={"zt_prev": {"codes": ["x"]}},
                               sector_map={"600000.SH": "BK0475"})
    stock = FactorContext(code="600000.SH", kline=None, sector_map={},
                          limit_ups=[])
    out = run_screen(s, market_ctx, gate_factors={"N1": 1},
                     stock_contexts={"600000.SH": stock})
    assert len(out["candidates"]) == 1      # 下发后 T8 在个股 ctx 命中
