# -*- coding: utf-8 -*-
"""模拟盘守护调度测试 — mock 时钟/行情/选股, 全离线。"""
import logging
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.engine
import prism.paper_daemon as dm
from prism.paper import PaperAccount
from prism.paper_daemon import PaperDaemon


CAND = [{"code": "600000.SH", "up_stop_price": 10.0,
         "scores": {"composite": 5.0}}]


class _FakeProvider:
    def __init__(self):
        from prism.context import FactorContext
        self._ctx = FactorContext

    def build_market_context(self):
        return self._ctx(code="__MARKET__", limit_ups=CAND)

    def get_limit_ups(self):
        return CAND

    def build_stock_context(self, code, **kw):
        return self._ctx(code=code)

    def invalidate(self):
        pass

    class ds:
        @staticmethod
        def get_full_market_ticks():
            return {"600000.SH": {"lastPrice": 9.8}}

        @staticmethod
        def get_kline(code, days=1):
            import pandas as pd
            return pd.DataFrame({"close": [9.8], "low": [9.7],
                                 "high": [10.0]}, index=["20260902"])


def _daemon(tmp_path, monkeypatch, now_fn=None, ticks=None):
    # 默认 mock run_screen: env_ok=False(不买入) — 需买入行为的测试自行覆盖
    monkeypatch.setattr(
        prism.engine, "run_screen",
        lambda s, m, gate_factors=None, stock_contexts=None:
        {"environment_ok": False, "gate_score": 0, "candidates": [],
         "summary": {"candidate_count": 0}})
    acc = PaperAccount(state_path=tmp_path / "paper.json")
    acc.init_account(created="2026-09-01")
    d = PaperDaemon(acc, ticks_fn=(lambda: ticks) if ticks else None,
                    now_fn=now_fn)
    d.provider = _FakeProvider()
    return d, acc


def test_in_session():
    d = PaperDaemon(PaperAccount(state_path="x.json"))
    assert d.in_session(datetime(2026, 9, 2, 10, 0)) is True      # 周三盘中
    assert d.in_session(datetime(2026, 9, 2, 12, 0)) is False     # 午休
    assert d.in_session(datetime(2026, 9, 2, 15, 1)) is False     # 收盘后
    assert d.in_session(datetime(2026, 9, 5, 10, 0)) is False     # 周六
    assert d.in_session(datetime(2026, 9, 2, 9, 25)) is False     # 未开盘


def test_tick_idle_outside_session(tmp_path, monkeypatch):
    d, _ = _daemon(tmp_path, monkeypatch)
    out = d.tick_once(now=datetime(2026, 9, 2, 12, 0))
    assert out["action"] == "idle" and out["sells"] == []


def test_tick_sells_in_session(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1e6})
    out = d.tick_once(now=datetime(2026, 9, 2, 10, 0, 5))
    # tick 价 9.8 在 9.025~10.26 之间 → 不触发卖出
    assert out["action"] == "tick" and out["sells"] == []
    # live_nav 按 tick 盯市: 100万现金 + 1000×9.8
    assert acc.state["live_nav"] == 1009800.0


def test_tick_screen_at_timepoint(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    calls = []

    def fake_run_screen(strategy, market_ctx, gate_factors=None,
                        stock_contexts=None):
        calls.append(1)
        return {"environment_ok": True, "gate_score": 1, "candidates": CAND,
                "summary": {"candidate_count": 1}}
    monkeypatch.setattr(prism.engine, "run_screen", fake_run_screen)
    out = d.tick_once(now=datetime(2026, 9, 2, 10, 0, 5))
    assert len(calls) == 1                        # 10:00 时点触发选股
    assert acc.state["screens_done"] == ["2026-09-02T10:00"]
    out2 = d.tick_once(now=datetime(2026, 9, 2, 10, 5))
    assert len(calls) == 1                        # 幂等: 同时点不重复
    assert out2["buys"] == []


def test_tick_settle_after_close(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1e6})
    out = d.tick_once(now=datetime(2026, 9, 8, 15, 5))
    assert out["action"] == "settle"
    # I-3 自然日口径: 09-01 买入 → 09-08 结算日 +7 自然日 >= 5 → 到期卖出
    assert acc.state["settled_dates"] == ["2026-09-08"]
    assert len(out["settle"]["closed"]) == 1
    assert out["settle"]["closed"][0]["reason"] == "hold_expire"
    # nav = 现金 + 卖出回款: 1e6 + 9790.2(=1000×9.8×0.999) - fee 7.44
    assert out["settle"]["nav"] == 1009782.76
    # 幂等: 二次不重复结算
    out2 = d.tick_once(now=datetime(2026, 9, 8, 15, 10))
    assert out2["settle"] is None


# ---------- 上游硬性要求: K线日期提取(time列新schema, Task4审查#1) ----------
def _ms_dates(days, hour=15):
    """指定日期 → epoch毫秒(本地时区往返自洽, 测试不依赖机器时区)。"""
    return [int(datetime(2026, 9, d, hour, 0).timestamp() * 1000)
            for d in days]


def test_kline_day_strs_time_column(tmp_path):
    """有 time 列(epoch毫秒, 注意单位)+整数index → 按 time 列提取 YYYYMMDD。"""
    import pandas as pd
    acc = PaperAccount(state_path=tmp_path / "k1.json")
    df = pd.DataFrame({"time": _ms_dates([2, 3, 4]),
                       "close": [9.8, 9.9, 10.0]})
    assert acc._kline_day_strs(df) == ["20260902", "20260903", "20260904"]


def test_kline_day_strs_index_fallback(tmp_path):
    """无 time 列 → index 兜底且只保留 8 位纯数字; RangeIndex 整数 → []。"""
    import pandas as pd
    acc = PaperAccount(state_path=tmp_path / "k2.json")
    df = pd.DataFrame({"close": [9.8, 9.9]},
                      index=["20260902", "20260903"])
    assert acc._kline_day_strs(df) == ["20260902", "20260903"]
    # 修复场景: str(整数index)="0/1/2" 非日期串 → 必须被丢弃(否则到期判定永假)
    assert acc._kline_day_strs(pd.DataFrame({"close": [1.0, 2.0, 3.0]})) == []


def test_due_by_kline_time_column(tmp_path):
    """新 schema(time列+RangeIndex)下 _due_by_kline 仍正确判定到期。"""
    import pandas as pd
    acc = PaperAccount(state_path=tmp_path / "k3.json")

    class _P:
        class ds:
            @staticmethod
            def get_kline(code, days=15):
                return pd.DataFrame({"time": _ms_dates(range(2, 9)),
                                     "close": [1.0] * 7})
    # 2026-09-01 买入 → 买日后 7 根 ≥ max_hold_days(5) → 到期
    assert acc._due_by_kline("600000.SH", "2026-09-01", _P()) is True
    # 2026-09-04 买入 → 买日后 4 根 < 5 → 未到期
    assert acc._due_by_kline("600000.SH", "2026-09-04", _P()) is False


def test_close_fn_time_column(tmp_path):
    """daemon close_fn 在新 schema(time列)下取 ≤day 最后一根收盘价。"""
    import pandas as pd

    class _DS:
        @staticmethod
        def get_kline(code, days=5):
            return pd.DataFrame({"time": _ms_dates([2, 3, 4]),
                                 "close": [9.8, 9.9, 10.0]})

    d = PaperDaemon(PaperAccount(state_path=tmp_path / "c.json"))

    class _P:
        ds = _DS()
    d.provider = _P()
    assert d.close_fn("600000.SH", "2026-09-03") == 9.9
    assert d.close_fn("600000.SH") == 10.0                # 无 day → 最新
    assert d.close_fn("600000.SH", "2026-09-01") is None  # 无 ≤day bar


def test_run_forever_backfill_after_connect(tmp_path, monkeypatch):
    """run_forever: 缺口补算必须在 connect 之后(backfill 依赖 provider,
    简报蓝图顺序 init→backfill→connect 在生产路径 backfill 恒为空转 →
    违背设计 §5 重启补算承诺, 按设计意图修正顺序并注明)。"""
    d, _ = _daemon(tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(PaperDaemon, "connect_provider",
                        lambda self, **kw: order.append("connect") or True)
    monkeypatch.setattr(PaperDaemon, "backfill",
                        lambda self: order.append("backfill") or 0)
    monkeypatch.setattr(
        PaperDaemon, "tick_once",
        lambda self, now=None: order.append("tick") or
        {"action": "idle", "sells": [], "buys": [], "settle": None})

    class _Stop(BaseException):          # 测试哨兵: 绕过 tick 层 except Exception
        pass

    def _sleep(sec):
        raise _Stop()
    d.sleep_fn = _sleep
    with pytest.raises(_Stop):
        d.run_forever()
    assert order == ["connect", "backfill", "tick"]


# ---------- 终审修复 I-1..I-4 / M-e ----------
def test_catchup_after_late_start(tmp_path, monkeypatch):
    """I-1 补跑风暴: 14:00 重启 → 10:00/13:30 两时点各补跑 1 次, 14:05
    不再重复, 14:35 触发 14:30 时点 → 三轮 tick 后总选股次数=3(幂等键=时点)。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    calls = []

    def fake_run_screen(strategy, market_ctx, gate_factors=None,
                        stock_contexts=None):
        calls.append(1)
        return {"environment_ok": False, "gate_score": 0, "candidates": [],
                "summary": {"candidate_count": 0}}
    monkeypatch.setattr(prism.engine, "run_screen", fake_run_screen)
    d.tick_once(now=datetime(2026, 9, 2, 14, 0))     # 补 10:00 + 13:30
    assert len(calls) == 2
    assert acc.state["screens_done"] == ["2026-09-02T10:00", "2026-09-02T13:30"]
    d.tick_once(now=datetime(2026, 9, 2, 14, 5))     # 两键已判已过 → 不重复
    assert len(calls) == 2
    d.tick_once(now=datetime(2026, 9, 2, 14, 35))    # 14:30 时点首次触发
    assert len(calls) == 3
    assert acc.state["screens_done"] == ["2026-09-02T10:00", "2026-09-02T13:30",
                                         "2026-09-02T14:30"]


def test_backfill_skips_today_before_close(tmp_path, monkeypatch):
    """I-2 盘中重启: 今日未收盘 → backfill 不补今日(净值点与 settled 留给
    15:00 的 settle_day), 只补昨日及更早缺口。"""
    d, acc = _daemon(tmp_path, monkeypatch,
                     now_fn=lambda: datetime(2026, 9, 3, 10, 0))
    monkeypatch.setattr(PaperDaemon, "_trade_days",
                        lambda self: ["2026-09-01", "2026-09-02",
                                      "2026-09-03"])
    n = d.backfill()
    assert n == 2
    dates = [h["date"] for h in acc.state["nav_history"]]
    assert dates == ["2026-09-01", "2026-09-02"]
    assert "2026-09-03" not in dates
    assert acc.state["settled_dates"] == []          # 今日不标 settled


def test_due_natural_parity(tmp_path, monkeypatch):
    """I-3 到期口径=自然日(结算日-buy_date).days >= max_hold_days,
    对齐实盘/回测 ExitRule; 周三(09-02)买入 → 09-07(+5)到期, 09-04(+2)未到。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    assert d._due_natural("2026-09-02", "2026-09-07") is True   # +5 自然日
    assert d._due_natural("2026-09-02", "2026-09-04") is False  # +2
    assert d._due_natural("2026-09-02", "2026-09-06") is False  # +4(周末结算日)
    # 端到端: daemon 结算分支走自然日到期 → 09-07 收盘卖出
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-02", "buy_price": 9.5, "entry_nav": 1e6})
    out = d.tick_once(now=datetime(2026, 9, 7, 15, 5))
    assert out["action"] == "settle"
    assert len(out["settle"]["closed"]) == 1
    assert out["settle"]["closed"][0]["reason"] == "hold_expire"
    assert acc.state["settled_dates"] == ["2026-09-07"]


def test_corrupt_ledger_refuses_start(tmp_path, monkeypatch, caplog):
    """I-4 损坏账本: load 失败且文件存在 → startup_guard=False, 保留原文件
    不覆盖; run_forever 开头即拒绝(不 init、不连 QMT)。"""
    p = tmp_path / "paper.json"
    p.write_text("{broken json!!", encoding="utf-8")
    raw = p.read_bytes()
    acc = PaperAccount(state_path=p)
    d = PaperDaemon(acc)
    with caplog.at_level(logging.ERROR, logger="paper_daemon"):
        assert d.startup_guard() is False
    assert p.read_bytes() == raw                     # 未覆盖写
    assert "拒绝启动" in caplog.text
    order = []
    monkeypatch.setattr(PaperDaemon, "connect_provider",
                        lambda self, **kw: order.append("connect") or True)

    class _Stop(BaseException):
        pass

    def _sleep(sec):
        raise _Stop()
    d.sleep_fn = _sleep
    d.run_forever()                      # 守卫拒绝 → 直接返回(不进主循环)
    assert order == []                               # 守卫在 connect 之前拦截
    assert p.read_bytes() == raw                     # 全程未覆盖


def test_settle_skips_weekend(tmp_path, monkeypatch):
    """M-e 周六 15:05 → 不结算不记平点(idle, settled_dates/nav_history 不变)。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-04", "buy_price": 9.5, "entry_nav": 1e6})
    out = d.tick_once(now=datetime(2026, 9, 5, 15, 5))
    assert out["action"] == "idle"
    assert acc.state["settled_dates"] == []
    assert acc.state["nav_history"] == []
