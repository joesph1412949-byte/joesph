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

def test_alert_60min_dedup(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    assert db.insert_alert("position_ratio", "600000.SH", "m1", "2026-08-12 10:00:00") is True
    # 同规则同代码 30 分钟后 -> 去重, 返回 False, 不落库
    assert db.insert_alert("position_ratio", "600000.SH", "m2", "2026-08-12 10:30:00") is False
    assert len(db.query_alerts()) == 1
    # 不同代码 -> 插入
    assert db.insert_alert("position_ratio", "600519.SH", "m3", "2026-08-12 10:30:00") is True
    # 同规则同代码 61 分钟后 -> 插入
    assert db.insert_alert("position_ratio", "600000.SH", "m4", "2026-08-12 11:01:00") is True
    assert len(db.query_alerts()) == 3

def test_alert_dedup_fail_open_on_bad_time(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    # 非法时间戳 -> fail-open, 直接插入(不中断告警)
    assert db.insert_alert("daily_loss", "", "m1", "not-a-time") is True
    assert db.insert_alert("daily_loss", "", "m2", "not-a-time") is True
    assert len(db.query_alerts()) == 2
