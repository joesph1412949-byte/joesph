import threading
from types import SimpleNamespace
from qmt_sync.db import QmtDb
from qmt_sync.qmt_client import QmtCallback
from qmt_sync.sync import SyncEngine


class FakeClient:
    def __init__(self):
        self.asset = SimpleNamespace(account_id="A", total_asset=100000.0, cash=50000.0,
                                     market_value=50000.0, frozen_cash=0.0)
        # open_price 9.0: 现价 9000/1000=9.0 与成本持平, 避免默认开启的 stop_loss 触发
        self.positions = [SimpleNamespace(account_id="A", stock_code="600000.SH", volume=1000,
                                          can_use_volume=1000, open_price=9.0, market_value=9000.0,
                                          frozen_volume=0, on_road_volume=0, yesterday_volume=1000)]
    def query_asset(self): return self.asset
    def query_positions(self): return self.positions


def test_poll_once_writes_and_alerts(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    eng = SyncEngine(SimpleNamespace(alert_rules={}, poll_interval_s=5.0), db, FakeClient(),
                     now=lambda: "2026-08-12 10:00:00")
    eng.poll_once()
    a = db.latest_asset()
    assert a["total_asset"] == 100000.0
    pos = db.latest_positions()
    assert pos and pos[0]["stock_code"] == "600000.SH"
    # 持仓 9000/100000=9% < 30%, 无告警
    assert db.query_alerts() == []


def test_poll_detects_ratio_alert(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    rules = {"position_ratio": {"enabled": True, "max": 0.05}}
    client = FakeClient()
    eng = SyncEngine(SimpleNamespace(alert_rules=rules, poll_interval_s=5.0), db, client,
                     now=lambda: "2026-08-12 10:00:00")
    eng.poll_once()
    alerts = db.query_alerts()
    assert len(alerts) == 1 and alerts[0]["rule"] == "position_ratio"


def test_on_trade_records(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    eng = SyncEngine(SimpleNamespace(alert_rules={}, poll_interval_s=5.0), db, FakeClient(),
                     now=lambda: "2026-08-12 10:00:00")
    trade = SimpleNamespace(account_id="A", stock_code="600000.SH", order_type=23, traded_id="t1",
                            traded_time="20260812100000", traded_price=9.0, traded_volume=100,
                            traded_amount=900.0, order_id="o1")
    eng.on_trade(trade)
    trades = db.query_trades()
    assert len(trades) == 1 and trades[0]["traded_volume"] == 100


def test_on_trade_from_worker_thread(tmp_path):
    """xtquant 回调在 worker 线程跑: 跨线程写库必须可用。"""
    db = QmtDb(str(tmp_path / "t.db"))
    eng = SyncEngine(SimpleNamespace(alert_rules={}, poll_interval_s=5.0), db, FakeClient(),
                     now=lambda: "2026-08-12 10:00:00")
    trade = SimpleNamespace(account_id="A", stock_code="600000.SH", order_type=23, traded_id="t-thr",
                            traded_time="20260812100000", traded_price=9.0, traded_volume=100,
                            traded_amount=900.0, order_id="o1")
    t = threading.Thread(target=eng.on_trade, args=(trade,))
    t.start()
    t.join()
    trades = db.query_trades()
    assert len(trades) == 1 and trades[0]["traded_id"] == "t-thr"


def test_callback_single_arg_signatures():
    """xtquant dispatcher 以单参数调用回调 on_stock_trade(data)/on_stock_order(data)。"""
    calls = []

    class FakeEngine:
        def on_trade(self, trade): calls.append(("trade", trade))
        def on_order(self, order): calls.append(("order", order))

    cb = QmtCallback(FakeEngine())
    trade = SimpleNamespace(traded_id="t1")
    order = SimpleNamespace(order_id="o1")
    cb.on_stock_trade(trade)
    cb.on_stock_order(order)
    cb.on_disconnected()
    assert calls == [("trade", trade), ("order", order)]
