"""同步引擎: 轮询资产/持仓, 处理成交/委托回调, 评估告警, 落库。"""
from __future__ import annotations
import logging
import time
from datetime import datetime

from qmt_sync.alerts import evaluate_asset_alerts, evaluate_position_alerts
from qmt_sync.models import AssetSnapshot, OrderRecord, PositionSnapshot, TradeRecord

logger = logging.getLogger("qmt_sync")


class SyncEngine:
    def __init__(self, cfg, db, client, now=None):
        self.cfg = cfg
        self.db = db
        self.client = client
        self._now = now or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._poll_seq = 0
        self._prev_codes: set | None = None

    def poll_once(self) -> None:
        asset = self.client.query_asset()
        positions = self.client.query_positions() or []
        ts = self._now()
        self._poll_seq += 1
        if asset is None:
            logger.warning("poll: no asset returned")
            return
        a = AssetSnapshot.from_xt(asset)
        self.db.insert_asset(a.account_id, a.total_asset, a.cash, a.market_value,
                             a.frozen_cash, ts)
        today = ts[:10]
        prev_day = self.db.prev_day_last_asset(a.account_id, today)
        for rule, code, msg in evaluate_asset_alerts(a, prev_day, self.cfg.alert_rules):
            self.db.insert_alert(rule, code, msg, ts)

        pos_rows = [PositionSnapshot.from_xt(p) for p in positions]
        self.db.upsert_positions(self._poll_seq, [r.to_row() for r in pos_rows])
        for rule, code, msg in evaluate_position_alerts(a, pos_rows, self._prev_codes,
                                                        self.cfg.alert_rules):
            self.db.insert_alert(rule, code, msg, ts)
        self._prev_codes = {p.stock_code for p in pos_rows}

    def on_trade(self, trade) -> None:
        self.db.insert_trade(TradeRecord.from_xt(trade).to_row())

    def on_order(self, order) -> None:
        self.db.insert_order(OrderRecord.from_xt(order).to_row())

    def run(self, stop_event=None) -> None:
        while not (stop_event is not None and stop_event.is_set()):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — 轮询循环必须存活
                logger.exception("poll failed: %s", exc)
            time.sleep(self.cfg.poll_interval_s)
