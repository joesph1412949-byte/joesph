# -*- coding: utf-8 -*-
"""模拟盘守护调度测试 — mock 时钟/行情/选股, 全离线。"""
import logging
import sys
import threading
import time
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
                    now_fn=now_fn,
                    zt_refresh_fn=lambda: {"injected_noop": True},
                    fund_snapshot_fn=lambda: {"injected_noop": True})
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


def test_tick_settle_after_close(tmp_path, monkeypatch):
    d, acc = _daemon(tmp_path, monkeypatch)
    # 屏蔽 15:05 pick(本测试只管 settle; pick 行为见 test_tick_once_pick_slot)
    acc.state["screens_done"].append("pickT2026-09-08T15:05")
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
    # (屏蔽 15:05 pick, 本测试只管 settle)
    acc.state["screens_done"].append("pickT2026-09-07T15:05")
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


# ---------- Task 3: 守护排板轮询/结算清理/跨日清理 ----------
def _pending_tick():
    """建委托用合格 tick(原生字段, bidVol=手: 50万手×100×10元=5亿 ≥ 2000万)。"""
    return {"lastPrice": 10.0, "lastVolume": 1_000_000,
            "bidVol": [500_000], "lastClose": 10.0}


class _QueueTickDS:
    """排板轮询 ds: 带 codes(轮询) → 穿越队列量的封板 tick(手差60万手=
    6000万股 ≥ queued 5000万股+30000股); 无参(卖盯市) → 原默认 9.8。"""

    @staticmethod
    def get_full_market_ticks(codes=None):
        if codes:
            return {c: {"lastPrice": 10.0, "lastVolume": 1_600_000,
                        "bidVol": [500_000], "lastClose": 10.0}
                    for c in codes}
        return {"600000.SH": {"lastPrice": 9.8}}


def test_tick_once_checks_pending_each_round(tmp_path, monkeypatch):
    """每轮排板检查(§2.3): 成交后 pending 移除、仓位出现; 返回 dict 带
    pending_filled/pending_canceled 键。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    d.provider.ds = _QueueTickDS
    assert acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        _pending_tick()) is not None
    out = d.tick_once(now=datetime(2026, 9, 2, 10, 1, 0))
    assert out["pending_filled"] == ["600000"]
    assert out["pending_canceled"] == []
    assert acc.state["pending_buys"] == []
    assert len(acc.state["holdings"]) == 1
    assert acc.state["holdings"][0]["buy_date"] == "2026-09-02"
    fee = 300000.0 * 0.00026
    assert acc.state["cash"] == pytest.approx(1_000_000.0 - 300000.0 - fee)


def test_quote_ticks_fail_open(tmp_path, monkeypatch):
    """轮询行情异常 → {}(fail-open 保守): pending 保持排队, tick_once 不炸。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    assert acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        _pending_tick()) is not None

    class _BoomDS:
        @staticmethod
        def get_full_market_ticks(codes=None):
            if codes:
                raise RuntimeError("行情断")
            return {}
    d.provider.ds = _BoomDS
    out = d.tick_once(now=datetime(2026, 9, 2, 10, 1, 0))
    assert out["pending_filled"] == [] and out["pending_canceled"] == []
    assert len(acc.state["pending_buys"]) == 1      # tick 缺失 → 保持排队


def test_tick_settle_clears_pending(tmp_path, monkeypatch):
    """收盘失效(§2.3.3): 15:00 后结算轮先撤 pending(queue_expire/解冻)再结算;
    撤单幂等键随结算日切重置(I-2)。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    assert acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 8, 10, 0, 5),
        _pending_tick()) is not None
    acc.state["canceled_pending_codes"].append("000001")   # 当日早前撤单键
    # 屏蔽 15:05 pick(本测试只管 settle; pick 行为见 test_tick_once_pick_slot)
    acc.state["screens_done"].append("pickT2026-09-08T15:05")
    out = d.tick_once(now=datetime(2026, 9, 8, 15, 5))
    assert out["action"] == "settle"
    assert out["pending_canceled"] == ["600000"]
    assert acc.state["pending_buys"] == []
    assert acc.state["canceled_pending_codes"] == []
    assert any(t["reason"] == "queue_expire" for t in acc.state["trades"])
    assert acc.available_cash() == acc.state["cash"]       # 解冻
    assert acc.state["settled_dates"] == ["2026-09-08"]


def _run_forever_one_tick(monkeypatch, d):
    """mock connect/backfill/tick + sleep 哨兵: run_forever 跑一轮即停。"""
    monkeypatch.setattr(PaperDaemon, "connect_provider",
                        lambda self, **kw: True)
    monkeypatch.setattr(PaperDaemon, "backfill", lambda self: 0)
    monkeypatch.setattr(
        PaperDaemon, "tick_once",
        lambda self, now=None: {"action": "idle", "sells": [], "buys": [],
                                "settle": None, "pending_filled": [],
                                "pending_canceled": []})

    class _Stop(BaseException):          # 测试哨兵: 绕过 tick 层 except Exception
        pass

    def _sleep(sec):
        raise _Stop()
    d.sleep_fn = _sleep
    return _Stop


def test_run_forever_expires_cross_day_pending(tmp_path, monkeypatch):
    """跨日启动(§2.4): 昨日 pending 启动即收盘失效; 撤单幂等键(属昨日,
    按最后流水日判定)一并重置 — 审查 I-2 forward-requirement。"""
    d, acc = _daemon(tmp_path, monkeypatch,
                     now_fn=lambda: datetime(2026, 9, 2, 9, 0))
    assert acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 1, 10, 0, 5),
        _pending_tick()) is not None
    acc.state["canceled_pending_codes"].append("000001")
    acc.state["trades"].append({"side": "buy", "code": "000001",
                                "date": "2026-09-01",
                                "reason": "queue_cancel_break"})
    acc.save()          # 落盘: run_forever.startup_guard 会 load 重读磁盘态
    with pytest.raises(_run_forever_one_tick(monkeypatch, d)):
        d.run_forever()
    assert acc.state["pending_buys"] == []
    assert acc.state["canceled_pending_codes"] == []
    assert any(t["reason"] == "queue_expire" for t in acc.state["trades"])


def test_run_forever_keeps_same_day_pending(tmp_path, monkeypatch):
    """同日重启: 今日 pending/撤单键保留(当日禁排不因重启失效, §2.4)。"""
    d, acc = _daemon(tmp_path, monkeypatch,
                     now_fn=lambda: datetime(2026, 9, 2, 11, 0))
    assert acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        _pending_tick()) is not None
    acc.state["canceled_pending_codes"].append("000001")
    acc.state["trades"].append({"side": "buy", "code": "000001",
                                "date": "2026-09-02",
                                "reason": "queue_cancel_break"})
    acc.save()          # 落盘: run_forever.startup_guard 会 load 重读磁盘态
    with pytest.raises(_run_forever_one_tick(monkeypatch, d)):
        d.run_forever()
    assert len(acc.state["pending_buys"]) == 1
    assert acc.state["canceled_pending_codes"] == ["000001"]
    assert not any(t["reason"] == "queue_expire" for t in acc.state["trades"])


# ---------- Task 4: 15:05 收盘选股 + 09:26-09:35 开盘买入窗口 ----------
def test_tick_once_pick_slot(tmp_path, monkeypatch, caplog):
    """15:05 后 → pick_top5_at_close 触发, slot 带日期(C1: 幂等键
    pickT<日期>T15:05 每日唯一); 同日不重复, 次日同一时刻重新选股;
    结果落日志(C3)。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    slots = []

    def fake_pick(provider, now=None, slot=None):
        slots.append(slot)
        acc.state["screens_done"].append("pickT" + slot)   # 同真实幂等键
        return {"picked": [], "env_ok": True}

    monkeypatch.setattr(acc, "pick_top5_at_close", fake_pick)
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        out = d.tick_once(now=datetime(2026, 9, 4, 15, 6))       # 周五
        assert out["action"] == "pick"
        assert slots == ["2026-09-04T15:05"]          # slot 必须带日期(C1)
        assert "收盘选股" in caplog.text              # 结果落日志(C3)
        d.tick_once(now=datetime(2026, 9, 4, 15, 10))
        assert slots == ["2026-09-04T15:05"]          # 同日幂等: 不重复
        d.tick_once(now=datetime(2026, 9, 7, 15, 6))  # 次个交易日同时刻
        assert slots == ["2026-09-04T15:05",
                         "2026-09-07T15:05"]          # 重新选股(C1)


def test_tick_once_open_window(tmp_path, monkeypatch, caplog):
    """09:26-09:35 窗口: 有今日计划 → execute_open_buys 触发,
    action=open_buys; 窗口外不触发; 结果落日志(C3)。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    calls = []

    def fake_exec(tick_provider, now=None):
        calls.append(tick_provider)
        acc.state["planned_buys"] = []                # 同真实消费语义
        return {"bought": ["600000"], "queued": [], "skipped": []}

    monkeypatch.setattr(acc, "execute_open_buys", fake_exec)
    plan = {"code": "600000", "score": 5.0, "date": "2026-09-07",
            "for_date": "2026-09-08"}
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        acc.state["planned_buys"] = [plan]
        out = d.tick_once(now=datetime(2026, 9, 8, 9, 25))
        assert out["action"] == "idle" and calls == []      # 窗口前
        acc.state["planned_buys"] = [plan]
        out = d.tick_once(now=datetime(2026, 9, 8, 9, 26))
        assert out["action"] == "open_buys"                 # 窗口起点(含)
        assert out["open_buys"]["bought"] == ["600000"]
        assert len(calls) == 1 and callable(calls[0])
        assert "开盘买入" in caplog.text                    # C3
        acc.state["planned_buys"] = [plan]
        out = d.tick_once(now=datetime(2026, 9, 8, 9, 36))
        assert out["action"] == "tick" and len(calls) == 1  # 窗口终点(不含)


def test_tick_once_no_intraday_queue(tmp_path, monkeypatch):
    """盘中 10:00/13:30/14:30 不建买入队列(Task 4 删除盘中时点后
    buy_from_screen 已整体移除): 只盯盘, 不建 pending/不成交。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    for hm in ((10, 1), (13, 31), (14, 31)):
        out = d.tick_once(now=datetime(2026, 9, 2, *hm))
        assert out["action"] == "tick" and out["buys"] == []
    assert acc.state["pending_buys"] == []
    assert acc.state["holdings"] == []


def test_open_buy_ticks_inject_limits(tmp_path, monkeypatch):
    """C2: 开盘买入行情注入真实涨跌停价(ds.get_instrument 的
    UpStopPrice/DownStopPrice); 拿不到 → 不补(paper 层分板回落)。"""
    d, acc = _daemon(tmp_path, monkeypatch)

    class _LimitDS:
        @staticmethod
        def get_full_market_ticks(codes=None):
            return {c: {"lastPrice": 10.0, "open": 10.5, "lastClose": 10.0}
                    for c in (codes or [])}

        @staticmethod
        def get_instrument(code):
            # 11.5 ≠ 昨收10×1.1 → 与分板公式不巧合, 证明显式字段生效(T4-M1)
            return ({"UpStopPrice": 11.5, "DownStopPrice": 9.0}
                    if code == "600000.SH" else {})

    d.provider.ds = _LimitDS
    captured = []

    def fake_exec(tick_provider, now=None):
        captured.append(tick_provider)
        acc.state["planned_buys"] = []
        return {"bought": [], "queued": [], "skipped": []}

    monkeypatch.setattr(acc, "execute_open_buys", fake_exec)
    acc.state["planned_buys"] = [{"code": "600000.SH", "score": 5.0,
                                  "date": "2026-09-07",
                                  "for_date": "2026-09-08"}]
    out = d.tick_once(now=datetime(2026, 9, 8, 9, 30))
    assert out["action"] == "open_buys"
    ticks = captured[0](["600000.SH", "000001.SZ"])
    assert ticks["600000.SH"]["upStopPrice"] == 11.5     # 真实涨停价注入
    assert ticks["600000.SH"]["downStopPrice"] == 9.0
    assert "upStopPrice" not in ticks["000001.SZ"]       # 缺失不补(回落)


def test_startup_purges_stale_plans(tmp_path, monkeypatch, caplog):
    """启动清理(spec §6): for_date<今日 的 planned_buys 作废(错过开盘
    窗口不追买), 今日计划保留待窗口消费。"""
    d, acc = _daemon(tmp_path, monkeypatch,
                     now_fn=lambda: datetime(2026, 9, 8, 9, 0))
    acc.state["planned_buys"] = [
        {"code": "600000", "score": 5.0, "date": "2026-09-07",
         "for_date": "2026-09-07"},               # 昨日计划 → 作废
        {"code": "600001", "score": 4.0, "date": "2026-09-07",
         "for_date": "2026-09-08"}]               # 今日计划 → 保留
    acc.save()          # 落盘: run_forever.startup_guard 会 load 重读磁盘态
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        with pytest.raises(_run_forever_one_tick(monkeypatch, d)):
            d.run_forever()
    assert [p["code"] for p in acc.state["planned_buys"]] == ["600001"]
    assert "计划清理" in caplog.text


# ---------- Task A: 涨停池缓存自动刷新挂钩(6h 节流, daemon 线程) ----------
def test_maybe_refresh_zt_calls_fn_then_throttles(tmp_path):
    """到点 → 注入的 zt_refresh_fn 被调(daemon 线程); 6h 内第二次不调。"""
    acc = PaperAccount(state_path=tmp_path / "zt1.json")
    acc.init_account(created="2026-09-01")
    calls = []
    done = threading.Event()

    def fake_zt():
        calls.append(1)
        done.set()
        return {"codes": 1}
    d = PaperDaemon(acc, zt_refresh_fn=fake_zt)
    d._maybe_refresh_zt()
    assert done.wait(5)
    assert calls == [1]
    d._maybe_refresh_zt()                    # 刚刷过(<6h) → 节流跳过
    assert calls == [1]


def test_maybe_refresh_zt_throttle_window_is_6h(tmp_path):
    """节流窗口: 6h 前刷过 → 再调; 6h 内 → 不调(时间戳语义钉死)。"""
    acc = PaperAccount(state_path=tmp_path / "zt2.json")
    acc.init_account(created="2026-09-01")
    calls = []
    done = threading.Event()

    def fake_zt():
        calls.append(1)
        done.set()
        return {}
    d = PaperDaemon(acc, zt_refresh_fn=fake_zt)
    d._zt_refresh_at = time.time() - (6 * 3600) - 1     # 6h+1s 前 → 到点
    d._maybe_refresh_zt()
    assert done.wait(5)
    assert calls == [1]
    # 5h59m 前刷过 → 不调(不需要等线程, 节流在主线程同步判定)
    done.clear()
    d._zt_refresh_at = time.time() - (6 * 3600) + 60
    d._maybe_refresh_zt()
    assert calls == [1]


def test_maybe_refresh_zt_swallows_exception(tmp_path, caplog):
    """refresh 抛异常 → worker 内吞掉落 warning, 绝不外溢; 后续 tick 正常。"""
    acc = PaperAccount(state_path=tmp_path / "zt3.json")
    acc.init_account(created="2026-09-01")
    entered = threading.Event()

    def boom():
        entered.set()
        raise RuntimeError("refresh 炸")
    d = PaperDaemon(acc, zt_refresh_fn=boom)
    with caplog.at_level(logging.WARNING, logger="paper_daemon"):
        d._maybe_refresh_zt()
        assert entered.wait(5)
        for _ in range(100):                 # 等 worker 落日志(有界轮询)
            if "涨停池缓存刷新失败" in caplog.text:
                break
            time.sleep(0.05)
    assert "涨停池缓存刷新失败" in caplog.text
    # 后续 tick 不受影响(不炸主循环)
    out = d.tick_once(now=datetime(2026, 9, 2, 12, 0))
    assert out["action"] == "idle"


def test_tick_once_pick_triggers_refresh_zt(tmp_path, monkeypatch):
    """收盘选股分支: pick 落定后触发 _maybe_refresh_zt(保鲜挂钩)。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(acc, "pick_top5_at_close",
                        lambda provider, now=None, slot=None:
                        {"picked": [], "env_ok": True})
    zt_calls = []
    monkeypatch.setattr(PaperDaemon, "_maybe_refresh_zt",
                        lambda self: zt_calls.append(1))
    out = d.tick_once(now=datetime(2026, 9, 4, 15, 6))
    assert out["action"] == "pick"
    assert zt_calls == [1]


def test_tick_once_pick_failure_logs_warning_and_retries(tmp_path, monkeypatch,
                                                        caplog):
    """F1(daemon 侧): 选股失败 → LOG.warning 带 error 与重试意图, 不再打
    "env_ok=None picked=[]" 的假成功 INFO; 因 paper 侧未占当日幂等键,
    下一轮(约 5 秒后)仍会真的重调 pick。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    calls = []
    snapshots = []

    def fake_pick(provider, now=None, slot=None):
        calls.append(slot)
        return {"error": "选股失败: RuntimeError('QMT 抖')"}   # 不登记幂等键

    monkeypatch.setattr(acc, "pick_top5_at_close", fake_pick)
    monkeypatch.setattr(PaperDaemon, "_maybe_fund_snapshot",
                        lambda self: snapshots.append(1))
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        out = d.tick_once(now=datetime(2026, 9, 4, 15, 6))
        assert out["action"] == "pick"
        assert "QMT 抖" in out["pick"]["error"]
        assert "收盘选股失败" in caplog.text and "重试" in caplog.text
        assert "env_ok" not in caplog.text          # 假成功日志已消失
        d.tick_once(now=datetime(2026, 9, 4, 15, 10))
    assert calls == ["2026-09-04T15:05", "2026-09-04T15:05"]   # 未占键 → 重试
    assert snapshots == []          # 失败轮不采基本面快照(留待选股成功那轮)


def test_run_forever_echoes_cost_config(tmp_path, monkeypatch, caplog):
    """F2 回显: 启动日志带费用口径(含 min_commission), 不靠读源码猜。"""
    d, acc = _daemon(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        with pytest.raises(_run_forever_one_tick(monkeypatch, d)):
            d.run_forever()
    assert "min_commission=5.0" in caplog.text


def test_run_forever_refresh_zt_after_connect(tmp_path, monkeypatch):
    """启动自愈: connect_provider 成功后触发 _maybe_refresh_zt
    (守护停了几天再开也能补), 早于 backfill/tick。"""
    d, _ = _daemon(tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(PaperDaemon, "connect_provider",
                        lambda self, **kw: order.append("connect") or True)
    monkeypatch.setattr(PaperDaemon, "_maybe_refresh_zt",
                        lambda self: order.append("zt"))
    monkeypatch.setattr(PaperDaemon, "backfill",
                        lambda self: order.append("backfill") or 0)
    monkeypatch.setattr(
        PaperDaemon, "tick_once",
        lambda self, now=None: order.append("tick") or
        {"action": "idle", "sells": [], "buys": [], "settle": None})

    class _Stop(BaseException):
        pass

    def _sleep(sec):
        raise _Stop()
    d.sleep_fn = _sleep
    with pytest.raises(_Stop):
        d.run_forever()
    assert order == ["connect", "zt", "backfill", "tick"]
