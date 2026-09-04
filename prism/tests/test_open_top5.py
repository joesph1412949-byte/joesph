# -*- coding: utf-8 -*-
"""次日开盘前五执行: 收盘选股 + 开盘买入 + 一字板替补 (全离线)。"""
import json
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.engine
from prism.paper import PaperAccount, _next_weekday


STRATEGY = {"market_gate": {"factors": ["N1"], "threshold": 1},
            "scoring_models": [{"id": "m", "weight": 1.0, "factors": ["F1"]}],
            "composite": {"mode": "average", "cap": 9.3333},
            "sell_rules": {"take_profit_pct": 0.15, "stop_loss_pct": 0.05,
                           "max_hold_days": 5},
            "execution": {"mode": "next_open_topn", "top_n": 2, "pct": 0.15,
                          "open_window": "09:26-09:35", "pick_slot": "15:05",
                          "one_word_fallback": "queue"}}


class FakeProvider:
    """build_market_context→门禁因子全 1; get_limit_ups→候选池。"""
    def __init__(self, codes):
        self.codes = codes
    def build_market_context(self):
        return {"__fake__": True}   # 门禁因子直接打桩, ctx 内容本组不依赖
    def get_limit_ups(self):
        return [{"code": c, "up_stop_price": 10.0} for c in self.codes]
    def build_stock_context(self, code):
        return {"code": code}


@pytest.fixture
def acc(tmp_path, monkeypatch):
    # 策略钉死(免激活指针漂移, 同 test_paper_sell 模式): 本组用 execution 块策略
    monkeypatch.setattr(PaperAccount, "strategy", property(lambda self: STRATEGY))
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-04")
    # 门禁/评分因子离线桩(对齐实际结构 reg.FACTORS/get_factor(fid)["func"]):
    # N1/F1 恒 {"score": 1} → 门禁恒命中; 评分走 run_screen 桩不触真实因子
    import prism.registry as reg
    monkeypatch.setitem(reg.FACTORS, "N1",
                        {"id": "N1", "func": lambda ctx: {"score": 1}})
    monkeypatch.setitem(reg.FACTORS, "F1",
                        {"id": "F1", "func": lambda ctx: {"score": 1}})
    return a


def _patch_run_screen(monkeypatch, codes, composite="diff"):
    """run_screen 桩: composite 同分(code 升序决胜)或递减 5.0-i(分降序)。"""
    def f(strategy, market_ctx, gate_factors=None, stock_contexts=None):
        cands = [{"code": c, "up_stop_price": 10.0,
                  "scores": {"composite": 5.0 if composite == "same"
                             else 5.0 - i}}
                 for i, c in enumerate(codes)]
        return {"environment_ok": True, "gate_score": 1,
                "candidates": cands, "summary": {}}
    monkeypatch.setattr("prism.engine.run_screen", f)


def test_next_weekday_skips_weekend():
    assert _next_weekday("2026-09-04") == "2026-09-07"   # 周五→周一
    assert _next_weekday("2026-09-07") == "2026-09-08"   # 周一→周二


def test_pick_top5_stores_plans(acc, monkeypatch):
    prov = FakeProvider(["600000", "600001", "600002"])
    _patch_run_screen(monkeypatch, prov.codes)
    out = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 5),
                                 slot="15:05")
    assert out["env_ok"] and len(out["picked"]) == 2      # top_n=2
    plans = acc.state["planned_buys"]
    assert [p["code"] for p in plans] == ["600000", "600001"]  # 分降序
    assert all(p["for_date"] == "2026-09-07" for p in plans)   # 周五→周一
    assert "pickT15:05" in acc.state["screens_done"]           # 幂等键(pickT 前缀)
    # 重复调用同日同时点 → already_done, 计划不变
    out2 = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 6),
                                  slot="15:05")
    assert out2.get("already_done") and len(acc.state["planned_buys"]) == 2


def test_pick_gate_fail_empty(acc, monkeypatch):
    prov = FakeProvider(["600000"])
    monkeypatch.setattr("prism.engine.run_screen",
                        lambda strategy, market_ctx, gate_factors=None,
                        stock_contexts=None: {
                            "environment_ok": False, "gate_score": 0,
                            "candidates": [], "summary": {}})
    out = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 5),
                                 slot="15:05")
    assert out["env_ok"] is False and acc.state["planned_buys"] == []


def test_pick_tie_breaks_by_code(acc, monkeypatch):
    """候选 composite 相同 → 按 code 升序取前 N(spec §5 同分决胜)。"""
    prov = FakeProvider(["600002", "600000", "600001"])
    _patch_run_screen(monkeypatch, prov.codes, composite="same")
    out = acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 5),
                                 slot="15:05")
    assert [p["code"] for p in out["picked"]] == ["600000", "600001"]


def test_pick_replaces_old_plans(acc, monkeypatch):
    acc.state["planned_buys"] = [{"code": "999999", "score": 9.0,
                                  "date": "2026-09-03",
                                  "for_date": "2026-09-04"}]
    prov = FakeProvider(["600000", "600001", "600002"])
    _patch_run_screen(monkeypatch, prov.codes)
    acc.pick_top5_at_close(prov, now=datetime(2026, 9, 4, 15, 5),
                           slot="15:05")
    assert [p["code"] for p in acc.state["planned_buys"]] != ["999999"]
    assert [p["code"] for p in acc.state["planned_buys"]] == \
        ["600000", "600001"]          # 整体替换语义


def _load_tmp_strategy(tmp_path, monkeypatch, strat):
    """临时 STRATEGIES_DIR + 指针 id, 走真实 strategy property 加载路径。"""
    d = tmp_path / "strats"
    d.mkdir()
    (d / ("%s.json" % strat["id"])).write_text(json.dumps(strat),
                                               encoding="utf-8")
    monkeypatch.setattr(prism.engine, "STRATEGIES_DIR", d)
    monkeypatch.setattr(prism.engine, "active_strategy_id",
                        lambda *a, **k: strat["id"])


def test_position_ratio_from_execution_pct(tmp_path, monkeypatch):
    """execution.pct 覆盖 position_ratio(spec §4: 硬编码 0.3 → 读 pct)。"""
    _load_tmp_strategy(tmp_path, monkeypatch, dict(STRATEGY,
                                                   id="open_top5_pct"))
    a = PaperAccount(state_path=tmp_path / "paper.json", position_ratio=0.42)
    a.init_account(created="2026-09-04")
    assert a.strategy["id"] == "open_top5_pct"      # 触发真实加载
    assert a.position_ratio == 0.15                 # 被 execution.pct 覆盖


def test_position_ratio_fallback_without_execution(tmp_path, monkeypatch):
    """无 execution 块 → 回落构造参数值(兼容 v04/v03, spec §4)。"""
    strat = {k: v for k, v in STRATEGY.items() if k != "execution"}
    strat["id"] = "open_top5_noexec"
    _load_tmp_strategy(tmp_path, monkeypatch, strat)
    a = PaperAccount(state_path=tmp_path / "paper.json", position_ratio=0.42)
    a.init_account(created="2026-09-04")
    assert a.strategy["id"] == "open_top5_noexec"
    assert a.position_ratio == 0.42                 # 回落构造参数


def _otick(open_px, last_close, bid_vol=100_000, last_volume=1_000_000,
           up_stop=None, down_stop=None):
    """开盘买入用 tick: open=开盘价, lastClose=昨收; 涨跌停价默认不给
    (回落分板系数), 显式传入时带 upStopPrice/downStopPrice(Task4 注入口径)。"""
    t = {"lastPrice": open_px, "open": open_px, "lastClose": last_close,
         "lastVolume": last_volume, "bidVol": [bid_vol]}
    if up_stop is not None:
        t["upStopPrice"] = up_stop
    if down_stop is not None:
        t["downStopPrice"] = down_stop
    return t


def test_open_buy_at_open_price(acc, monkeypatch):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    # acc fixture 桩掉 strategy 属性 → execution.pct 覆盖未触发, 显式对齐 15%
    monkeypatch.setattr(acc, "position_ratio", 0.15)
    ticks = {"600000": _otick(10.50, 10.0)}       # 开盘+5%, 非板
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["bought"] == ["600000"] and out["queued"] == []
    h = acc.state["holdings"][0]
    assert h["cost"] == 10.50                      # 开盘价无滑点
    assert h["shares"] == 14200                    # 100万×15%=15万; 150000//(10.5*100)=142手→14200股
    assert acc.state["planned_buys"] == []         # 消费后清空


def test_open_buy_one_word_queues(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"600000": _otick(11.0, 10.0, bid_vol=2_000_000)}  # 开盘=涨停(一字)
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["queued"] == ["600000"] and out["bought"] == []
    p = acc.state["pending_buys"][0]
    assert p["price"] == 11.0 and p["base_volume"] == 1_000_000  # 走排队状态机
    assert acc.state["planned_buys"] == []


def test_open_buy_20cm_one_word_queues(acc):
    """创业板(20cm)无显式涨跌停字段 → 分板系数回落 up=12.0, 一字排队价 12.0
    (回归: 旧全局 10% 误算 11.0 → 死价永不成交)。"""
    acc.state["planned_buys"] = [{"code": "300001", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"300001": _otick(12.0, 10.0, bid_vol=2_000_000)}  # 开盘=+20%涨停
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["queued"] == ["300001"] and out["bought"] == []
    p = acc.state["pending_buys"][0]
    assert p["price"] == 12.0                       # 分板回落 10×1.2, 非 11.0
    assert acc.state["planned_buys"] == []


def test_open_buy_explicit_upstop_wins(acc):
    """tick 显式 upStopPrice 优先于分板回落(模拟 ST ±5%):
    昨收10 开盘11 == 真实涨停 → 排队价 10.5, 不得按 11.0 误排/照买。"""
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"600000": _otick(11.0, 10.0, bid_vol=2_000_000, up_stop=10.5)}
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["queued"] == ["600000"] and out["bought"] == []
    p = acc.state["pending_buys"][0]
    assert p["price"] == 10.5                       # 显式字段胜过 10% 回落
    assert acc.state["planned_buys"] == []


def test_open_buy_down_limit_skipped(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    ticks = {"600000": _otick(9.0, 10.0)}          # 开盘=跌停
    out = acc.execute_open_buys(lambda codes: ticks,
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["bought"] == [] and out["queued"] == []
    assert out["skipped"] == [{"code": "600000", "reason": "跌停开盘"}]
    assert acc.state["planned_buys"] == []         # 仍清空(消费)


def test_open_buy_missing_tick_skipped(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-04",
                                  "for_date": "2026-09-07"}]
    out = acc.execute_open_buys(lambda codes: {},
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["skipped"] == [{"code": "600000", "reason": "无行情"}]
    assert acc.state["planned_buys"] == []


def test_open_buy_stale_plan_untouched(acc):
    acc.state["planned_buys"] = [{"code": "600000", "score": 5.0,
                                  "date": "2026-09-03",
                                  "for_date": "2026-09-04"}]   # 昨日计划
    out = acc.execute_open_buys(lambda codes: {"600000": _otick(10.5, 10.0)},
                                now=datetime(2026, 9, 7, 9, 26))
    assert out["bought"] == [] and len(acc.state["planned_buys"]) == 1
