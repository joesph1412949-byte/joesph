# -*- coding: utf-8 -*-
"""状态机与持久化测试。"""
import json
from datetime import datetime

from ttcore.state import Ledger


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
    from ttcore.state import MAX_EVENTS
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


def test_archive_writes_one_line_on_roll(tmp_path, now_fn):
    """跨日重置前, 前一日的账本摘要须追加到 history.jsonl。"""
    from datetime import datetime
    from ttcore.state import Ledger

    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.archive_current() is True
    lines = (tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 100
    assert "realized_pnl" in row


def test_archive_skips_empty_ledger(tmp_path, now_fn):
    """空账本不产生归档行(避免跨日被反复写垃圾行)。"""
    from ttcore.state import Ledger
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    assert led.archive_current() is False
    assert not (tmp_path / "tt_history.jsonl").exists()


def test_archive_is_idempotent_per_date(tmp_path, now_fn):
    """同一天重复调用只归档一次。"""
    from datetime import datetime
    from ttcore.state import Ledger
    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.archive_current() is True
    assert led.archive_current() is False
    assert len((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 1


def test_archive_failure_does_not_raise(tmp_path, now_fn, monkeypatch):
    """归档异常必须被吞掉 —— 绝不影响交易主流程。"""
    from ttcore import state as st
    led = st.Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(st, "atomic_write", boom)
    assert led.archive_current() is False        # 不抛异常


def test_load_archives_stale_day_before_reset(tmp_path):
    """load() 发现旧日账本时, 必须先归档再重置(否则历史永久丢失)。"""
    import json
    from datetime import datetime
    from ttcore.state import Ledger

    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({
        "version": 1, "date": "2026-09-14",
        "updated_at": "2026-09-14T15:00:00",
        "symbols": {"600900.SH": {"sold_today": 200, "bought_today": 100,
                                  "trips": 1, "realized_pnl": 33.0}},
        "events": [],
    }), encoding="utf-8")

    day2 = lambda: datetime(2026, 9, 15, 10, 0, 0)
    led = Ledger(path=p, now_fn=day2)
    led.load()
    assert led.state["date"] == "2026-09-15"          # 已重置
    row = json.loads((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip())
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 200


def test_read_history_returns_rows(tmp_path, now_fn):
    from datetime import datetime
    from ttcore.state import Ledger
    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    led.archive_current()
    rows = Ledger.read_history(tmp_path / "tt_state.json")
    assert len(rows) == 1 and rows[0]["date"] == "2026-09-14"


def test_corrupt_history_does_not_break_trading_path(tmp_path, now_fn):
    """history.jsonl 损坏(非 UTF-8)不得让账本构造/记账失败 —— 归档是旁路。

    __init__ 会读一次归档来建幂等集合, 这一读在交易路径上, 必须 fail-open。
    """
    from ttcore.state import Ledger
    (tmp_path / "tt_history.jsonl").write_bytes(b"\xff\xfe\x00 not utf8")
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.sym("600900.SH")["sold_today"] == 100


def test_roll_if_new_day_archives_previous_day(tmp_path):
    """roll_if_new_day → reset_day 这条重置路径也必须先归档(否则历史丢失)。"""
    import json
    from datetime import datetime
    from ttcore.state import Ledger

    t = {"now": datetime(2026, 9, 14, 10, 0)}
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    t["now"] = datetime(2026, 9, 15, 10, 0)
    assert led.roll_if_new_day() is True
    assert led.state["date"] == "2026-09-15"          # 已重置
    row = json.loads((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip())
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 100


def test_fresh_instance_does_not_re_archive(tmp_path, now_fn):
    """幂等靠读文件, 不只靠内存 —— 另开一个实例(守护/面板各持一个)不重复写。"""
    from ttcore.state import Ledger
    p = tmp_path / "tt_state.json"
    a = Ledger(path=p, now_fn=now_fn)
    a.load()
    a.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                  reason="档位1", order_id="TT_1")
    assert a.archive_current() is True
    b = Ledger(path=p, now_fn=now_fn)                  # 新实例
    b.load()
    assert b.archive_current() is False
    assert len((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 1
