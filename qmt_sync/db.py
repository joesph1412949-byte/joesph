"""SQLite 存储层: schema + 写入/查询。同步进程写, Vibe-Trading 工具只读。"""
from __future__ import annotations
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT NOT NULL,
  total_asset REAL, cash REAL, market_value REAL, frozen_cash REAL,
  update_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_acc_time ON account_assets(account_id, update_time);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_seq INTEGER NOT NULL,
  account_id TEXT NOT NULL,
  stock_code TEXT NOT NULL,
  volume INTEGER, can_use_volume INTEGER, open_price REAL, market_value REAL,
  frozen_volume INTEGER, on_road_volume INTEGER, yesterday_volume INTEGER,
  update_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_seq ON positions(poll_seq);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT, stock_code TEXT, order_type INTEGER, traded_id TEXT,
  traded_time TEXT, traded_price REAL, traded_volume INTEGER, traded_amount REAL,
  order_id TEXT, received_at TEXT NOT NULL,
  UNIQUE(account_id, traded_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_code_time ON trades(stock_code, traded_time);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id TEXT, stock_code TEXT, order_id TEXT, order_sysid TEXT,
  order_time TEXT, order_type INTEGER, order_volume INTEGER, price_type INTEGER,
  price REAL, traded_volume INTEGER, traded_price REAL, order_status INTEGER,
  status_msg TEXT, received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule TEXT NOT NULL, stock_code TEXT, message TEXT NOT NULL,
  triggered_at TEXT NOT NULL
);
"""


class QmtDb:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- writes ----------
    def insert_asset(self, account_id, total_asset, cash, market_value, frozen_cash, update_time):
        self._conn.execute(
            "INSERT INTO account_assets(account_id,total_asset,cash,market_value,frozen_cash,update_time) "
            "VALUES(?,?,?,?,?,?)",
            (account_id, total_asset, cash, market_value, frozen_cash, update_time),
        )
        self._conn.commit()

    def upsert_positions(self, poll_seq, rows):
        for r in rows:
            self._conn.execute(
                "INSERT INTO positions(poll_seq,account_id,stock_code,volume,can_use_volume,open_price,"
                "market_value,frozen_volume,on_road_volume,yesterday_volume,update_time) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (poll_seq, r.get("account_id", ""), r["stock_code"], r.get("volume"), r.get("can_use_volume"),
                 r.get("open_price"), r.get("market_value"), r.get("frozen_volume"),
                 r.get("on_road_volume"), r.get("yesterday_volume"), r.get("update_time", "")),
            )
        self._conn.commit()

    def insert_trade(self, r):
        self._conn.execute(
            "INSERT OR IGNORE INTO trades(account_id,stock_code,order_type,traded_id,traded_time,"
            "traded_price,traded_volume,traded_amount,order_id,received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r.get("account_id"), r.get("stock_code"), r.get("order_type"), r.get("traded_id"),
             r.get("traded_time"), r.get("traded_price"), r.get("traded_volume"),
             r.get("traded_amount"), r.get("order_id"), r.get("received_at", "")),
        )
        self._conn.commit()

    def insert_order(self, r):
        self._conn.execute(
            "INSERT INTO orders(account_id,stock_code,order_id,order_sysid,order_time,order_type,"
            "order_volume,price_type,price,traded_volume,traded_price,order_status,status_msg,received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("account_id"), r.get("stock_code"), r.get("order_id"), r.get("order_sysid"),
             r.get("order_time"), r.get("order_type"), r.get("order_volume"), r.get("price_type"),
             r.get("price"), r.get("traded_volume"), r.get("traded_price"), r.get("order_status"),
             r.get("status_msg"), r.get("received_at", "")),
        )
        self._conn.commit()

    def insert_alert(self, rule, stock_code, message, triggered_at):
        self._conn.execute(
            "INSERT INTO alerts(rule,stock_code,message,triggered_at) VALUES(?,?,?,?)",
            (rule, stock_code or "", message, triggered_at),
        )
        self._conn.commit()

    # ---------- queries ----------
    def _rows(self, sql, args=()):
        return [dict(zip([d[0] for d in self._conn.execute(sql, args).description],
                         row)) for row in self._conn.execute(sql, args).fetchall()]

    def latest_asset(self, account_id=None):
        sql = "SELECT * FROM account_assets"
        args: tuple = ()
        if account_id:
            sql += " WHERE account_id=?"
            args = (account_id,)
        sql += " ORDER BY update_time DESC, id DESC LIMIT 1"
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def prev_day_last_asset(self, account_id, today):
        rows = self._rows(
            "SELECT * FROM account_assets WHERE account_id=? AND date(update_time) < ? "
            "ORDER BY update_time DESC, id DESC LIMIT 1",
            (account_id, today),
        )
        return rows[0] if rows else None

    def latest_positions(self, account_id=None):
        args: tuple = ()
        sql = "SELECT * FROM positions WHERE poll_seq=(SELECT MAX(poll_seq) FROM positions)"
        if account_id:
            sql += " AND account_id=?"
            args = (account_id,)
        return self._rows(sql, args)

    def query_position(self, stock_code, account_id=None):
        args: tuple = (stock_code,)
        sql = "SELECT * FROM positions WHERE poll_seq=(SELECT MAX(poll_seq) FROM positions) AND stock_code=?"
        if account_id:
            sql += " AND account_id=?"
            args += (account_id,)
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def query_asset_history(self, start=None, end=None):
        sql = "SELECT * FROM account_assets WHERE 1=1"
        args: list = []
        if start:
            sql += " AND update_time >= ?"; args.append(start)
        if end:
            sql += " AND update_time <= ?"; args.append(end)
        sql += " ORDER BY update_time, id"
        return self._rows(sql, tuple(args))

    def query_trades(self, stock_code=None, start=None, end=None, limit=50):
        sql = "SELECT * FROM trades WHERE 1=1"
        args: list = []
        if stock_code:
            sql += " AND stock_code=?"; args.append(stock_code)
        if start:
            sql += " AND traded_time >= ?"; args.append(start)
        if end:
            sql += " AND traded_time <= ?"; args.append(end)
        sql += " ORDER BY traded_time DESC LIMIT ?"; args.append(int(limit))
        return self._rows(sql, tuple(args))

    def query_alerts(self, limit=20):
        return self._rows(
            "SELECT * FROM alerts ORDER BY triggered_at DESC, id DESC LIMIT ?", (int(limit),)
        )
