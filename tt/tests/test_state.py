# -*- coding: utf-8 -*-
"""状态机与持久化测试。"""
import json
from datetime import datetime

from tt.state import Ledger


def test_new_ledger_is_today(ledger):
    s = ledger.load()
    assert s["date"] == "2026-09-14"
    assert s["symbols"] == {}


def test_record_fill_sell_then_buy_pairs_pnl(ledger):
    """正T: 先卖后买, 价差为收益。"""
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.239, 500, hhmm="10:00")
    s = ledger.sym("600900.SH")
    assert s["sold_today"] == 500
    assert s["realized_pnl"] == 0.0            # 卖出未配对, 无盈亏

    ev = ledger.record_fill("600900.SH", "BUY", 27.94, 500, hhmm="14:00")
    # (28.239 - 27.94) * 500 = 149.5
    assert ev["matched_qty"] == 500
    assert abs(ev["pnl"] - 149.5) < 1e-6
    assert ledger.sym("600900.SH")["bought_today"] == 500
    assert ledger.sym("600900.SH")["trips"] == 1


def test_record_fill_buy_then_sell_pairs_pnl(ledger):
    """反T: 先买后卖, 卖出时结出价差。"""
    ledger.load()
    ledger.record_fill("600900.SH", "BUY", 27.90, 300)
    assert ledger.sym("600900.SH")["realized_pnl"] == 0.0
    ev = ledger.record_fill("600900.SH", "SELL", 28.30, 300)
    assert ev["matched_qty"] == 300
    assert abs(ev["pnl"] - (28.30 - 27.90) * 300) < 1e-6


def test_partial_pairing_leaves_remainder(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.00, 500)
    ev = ledger.record_fill("600900.SH", "BUY", 27.50, 300)
    assert ev["matched_qty"] == 300
    s = ledger.sym("600900.SH")
    # 卖出剩 200 股留在队列里(净卖出敞口)
    assert s["sell_queue"] == [[28.00, 200]]
    assert s["bought_today"] - s["sold_today"] == -200


def test_fifo_multi_lot(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "BUY", 10.0, 100)
    ledger.record_fill("600900.SH", "BUY", 12.0, 100)
    ev = ledger.record_fill("600900.SH", "SELL", 15.0, 200)
    # FIFO: (15-10)*100 + (15-12)*100 = 500 + 300 = 800
    assert abs(ev["pnl"] - 800.0) < 1e-6


def test_loss_recorded_negative(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.00, 100)
    ev = ledger.record_fill("600900.SH", "BUY", 28.50, 100)
    assert ev["pnl"] < 0
    assert ledger.total_realized_pnl() < 0


def test_daily_roll_resets_on_new_day(tmp_path):
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    led = Ledger(path=tmp_path / "s.json", now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.0, 500)
    assert led.sym("600900.SH")["sold_today"] == 500

    t["now"] = datetime(2026, 9, 15, 10, 0)
    assert led.roll_if_new_day() is True
    assert led.state["date"] == "2026-09-15"
    assert led.sym("600900.SH", create=False) is None       # 新日干净
    assert led.daily_trades() == 0


def test_load_ignores_stale_day(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 1, "date": "2020-01-01",
                             "symbols": {"X": {"sold_today": 999}},
                             "events": []}), encoding="utf-8")
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    assert led.state["date"] == "2026-09-14"
    assert "X" not in led.state["symbols"]


def test_load_recovers_from_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ this is not json", encoding="utf-8")
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    assert led.state["date"] == "2026-09-14"
    assert led.state["symbols"] == {}


def test_persistence_roundtrip(tmp_path):
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    p = tmp_path / "s.json"
    led = Ledger(path=p, now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.0, 100)
    led.set_ref("600900.SH", 28.09)
    led.set_units("600900.SH", "SELL", 2)

    led2 = Ledger(path=p, now_fn=lambda: t["now"])
    led2.load()
    assert led2.sym("600900.SH")["sold_today"] == 100
    assert led2.get_ref("600900.SH") == 28.09
    assert led2.get_units("600900.SH", "SELL") == 2


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "s.json"
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    led.save()
    assert p.exists()
    assert not (tmp_path / "s.json.tmp").exists()


def test_events_capped(ledger):
    ledger.load()
    from tt.state import MAX_EVENTS
    for _ in range(MAX_EVENTS + 40):
        ledger.record_fill("600900.SH", "SELL", 28.0, 100)
    assert len(ledger.state["events"]) <= MAX_EVENTS


def test_snapshot_shape(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.0, 500)
    ledger.record_fill("600900.SH", "BUY", 27.5, 500)
    snap = ledger.snapshot()
    assert snap["date"] == "2026-09-14"
    s = snap["symbols"]["600900.SH"]
    assert s["sold_today"] == 500
    assert s["bought_today"] == 500
    assert s["net_exposure"] == 0
    assert snap["total_trips"] == 1
    assert snap["daily_trades"] == 2


def test_units_ops(ledger):
    ledger.load()
    assert ledger.get_units("X.SH", "SELL") == 0
    ledger.set_units("X.SH", "SELL", 3)
    assert ledger.get_units("X.SH", "SELL") == 3
