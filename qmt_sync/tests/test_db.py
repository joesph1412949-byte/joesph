from qmt_sync.db import QmtDb
from qmt_sync.models import TradeRecord
import logging
import sqlite3
from types import SimpleNamespace

def test_db_creates_missing_parent_dir(tmp_path):
    # sqlite 不会自动建父目录; D:\QMT_SYNC 首次运行时不存在, 必须由 QmtDb 创建
    nested = tmp_path / "not_yet_created" / "t.db"
    db = QmtDb(str(nested))
    assert nested.is_file()

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


def _trade_xt(vol, price, ttime="20260812093000", oid="o1"):
    return SimpleNamespace(account_id="A", stock_code="600000.SH", order_type=23, traded_id=None,
                           traded_time=ttime, traded_price=price, traded_volume=vol,
                           traded_amount=vol * price, order_id=oid)


def test_missing_traded_id_three_fills_do_not_collapse(tmp_path):
    """I4: 同秒 3 笔 traded_id 缺失的成交必须落 3 行(旧行为: 全落 "" -> 只有 1 行)。"""
    db = QmtDb(str(tmp_path / "t.db"))
    for vol, price in ((100, 8.5), (200, 8.5), (300, 8.6)):
        db.insert_trade(TradeRecord.from_xt(_trade_xt(vol, price)).to_row())
    assert len(db.query_trades()) == 3
    # 重复回调同一笔 -> 仍然去重(合成键确定性)
    db.insert_trade(TradeRecord.from_xt(_trade_xt(100, 8.5)).to_row())
    assert len(db.query_trades()) == 3


def test_busy_timeout_explicit(tmp_path):
    """I3: busy_timeout 必须显式设置(不依赖 sqlite3.connect 的隐式默认), 单位毫秒。"""
    db = QmtDb(str(tmp_path / "t.db"))
    assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_locked_write_is_observable_not_raised(tmp_path, caplog):
    """I3: 第二写者持锁超过 busy_timeout 时, 写入失败必须是 WARNING + 返回 False,
    而不是抛异常打断整轮轮询(旧行为: OperationalError 冒泡到 sync.run, 整轮数据丢成一行日志)。"""
    p = str(tmp_path / "t.db")
    db = QmtDb(p)
    held = sqlite3.connect(p)  # 模拟第二个写者(重复启动的 qmt_sync / Vibe 侧工具)
    held.execute("BEGIN IMMEDIATE")
    db._conn.execute("PRAGMA busy_timeout=50")  # 压缩等待, 测试不必真等 5s
    try:
        with caplog.at_level(logging.WARNING, logger="qmt_sync"):
            ok = db.insert_asset("A", 1.0, 0, 0, 0, "2026-08-12 10:00:00")
        assert ok is False
        assert "database is locked" in caplog.text
    finally:
        held.rollback()
        held.close()
    # 锁释放后必须恢复正常写入(失败没把连接留在坏事务里)
    assert db.insert_asset("A", 1.0, 0, 0, 0, "2026-08-12 10:00:01") is True
    assert db.latest_asset("A")["total_asset"] == 1.0


def test_trade_and_order_write_failure_is_observable(tmp_path, caplog):
    """I3: 成交/委托写入失败同样不能静默 —— 返回 False + WARNING 带原因。"""
    p = str(tmp_path / "t.db")
    db = QmtDb(p)
    held = sqlite3.connect(p)
    held.execute("BEGIN IMMEDIATE")
    db._conn.execute("PRAGMA busy_timeout=50")
    try:
        with caplog.at_level(logging.WARNING, logger="qmt_sync"):
            assert db.insert_trade({"traded_id": "t1", "received_at": "x"}) is False
            assert db.insert_order({"order_id": "o1", "received_at": "x"}) is False
            assert db.upsert_positions(1, [{"stock_code": "600000.SH"}]) is False
        assert caplog.text.count("database is locked") == 3
    finally:
        held.rollback()
        held.close()
