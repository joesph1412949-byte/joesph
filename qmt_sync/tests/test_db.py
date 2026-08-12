from qmt_sync.db import QmtDb

def test_schema_and_asset(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    db.insert_asset("A", 100000.0, 50000.0, 50000.0, 0.0, "2026-08-12 10:00:00")
    db.insert_asset("A", 101000.0, 51000.0, 50000.0, 0.0, "2026-08-12 10:00:05")
    latest = db.latest_asset("A")
    assert latest["total_asset"] == 101000.0

def test_positions_poll_seq(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    db.upsert_positions(1, [{"stock_code": "600000.SH", "volume": 100, "market_value": 8000.0}])
    db.upsert_positions(2, [{"stock_code": "600000.SH", "volume": 200, "market_value": 16000.0}])
    rows = db.latest_positions()
    assert len(rows) == 1 and rows[0]["volume"] == 200

def test_trade_dedup(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    tr = {"account_id": "A", "stock_code": "600000.SH", "traded_id": "t1",
          "traded_price": 8.0, "traded_volume": 100, "traded_amount": 800.0,
          "order_type": 23, "traded_time": "2026-08-12 09:30:00", "received_at": "2026-08-12 09:30:01"}
    db.insert_trade(tr); db.insert_trade(tr)  # 同 traded_id 去重
    assert len(db.query_trades()) == 1

def test_prev_day_asset(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    db.insert_asset("A", 100000.0, 0, 0, 0, "2026-08-11 15:00:00")
    db.insert_asset("A", 99000.0, 0, 0, 0, "2026-08-12 10:00:00")
    prev = db.prev_day_last_asset("A", "2026-08-12")
    assert prev["total_asset"] == 100000.0
