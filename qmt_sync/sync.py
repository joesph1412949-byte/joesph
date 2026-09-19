"""同步引擎: 轮询资产/持仓, 处理成交/委托回调, 评估告警, 落库。"""
from __future__ import annotations
import logging
import time
from datetime import datetime

from qmt_sync.alerts import evaluate_asset_alerts, evaluate_gap_alert, evaluate_position_alerts
from qmt_sync.models import AssetSnapshot, OrderRecord, PositionSnapshot, TradeRecord

logger = logging.getLogger("qmt_sync")

# I1: 持仓查询返回空 list 时用资产侧市值分辨"查询抖动"与"真空仓"。
# 残值 <= 1 元(零股/退市残值)视为没有市值, 与用户真的清空等价。
_EMPTY_MV_TOLERANCE = 1.0


class SyncEngine:
    def __init__(self, cfg, db, client, now=None):
        self.cfg = cfg
        self.db = db
        self.client = client
        self._now = now or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self._poll_seq = 0
        self._prev_codes: set | None = None
        # I2: 最近一次"资产行真的落库"的时刻 —— data_gap 的心跳起点 = 进程启动时刻
        self._last_ok_ts = self._now()

    def poll_once(self) -> None:
        """单轮同步。

        I1 —— 怎样才算"用户真的清空全部持仓"(三个判据, 按顺序):
          1. `query_positions()` 返回 None: 明确的查询失败(QMT 未就绪/断线), 无论资产如何,
             本轮无持仓数据;
          2. 返回空 list 但 `asset.market_value > 1` 元: 资产侧与持仓侧自相矛盾(查询抖动),
             本轮无持仓数据;
          3. 返回空 list 且 `asset.market_value ≈ 0`(<= 1 元): 资产侧独立证实已无市值,
             判为真空仓 —— 照常评估 position_change 并发"清仓"告警。

        "本轮无持仓数据" = 不评估 position_change、**不覆盖 `_prev_codes`**、打 WARNING;
        资产行照常落库(它是独立数据源)。这样既不会把瞬时 None/[] 说成"全部卖出",
        也不会让假清仓占掉 insert_alert 的 60 分钟同规则同代码去重窗, 从而吞掉恢复后
        真正的"新开仓"信号。
        """
        ts = self._now()
        self._poll_seq += 1
        asset = self.client.query_asset()
        if asset is None:
            logger.warning("poll: no asset returned")
            self._check_gap(ts)
            return
        a = AssetSnapshot.from_xt(asset)
        if not self.db.insert_asset(a.account_id, a.total_asset, a.cash, a.market_value,
                                    a.frozen_cash, ts):
            # I3: 写库失败(如 database is locked)。不推进心跳, 持续失败由 data_gap 规则暴露,
            # 而不是只留一行日志静默丢行。
            logger.warning("poll: asset row not written, count round as no data")
            self._check_gap(ts)
            return
        self._last_ok_ts = ts
        today = ts[:10]
        prev_day = self.db.prev_day_last_asset(a.account_id, today)
        for rule, code, msg in evaluate_asset_alerts(a, prev_day, self.cfg.alert_rules):
            self.db.insert_alert(rule, code, msg, ts)

        xt_positions = self.client.query_positions()
        if xt_positions is None:
            logger.warning("poll: query_positions() returned None — 本轮无持仓数据, 跳过持仓评估")
            return
        if not xt_positions and (a.market_value or 0.0) > _EMPTY_MV_TOLERANCE:
            logger.warning("poll: empty positions but market_value=%.2f — 本轮无持仓数据, 跳过持仓评估",
                           a.market_value or 0.0)
            return
        pos_rows = [PositionSnapshot.from_xt(p) for p in xt_positions]
        self.db.upsert_positions(self._poll_seq, [r.to_row() for r in pos_rows])
        for rule, code, msg in evaluate_position_alerts(a, pos_rows, self._prev_codes,
                                                        self.cfg.alert_rules):
            self.db.insert_alert(rule, code, msg, ts)
        self._prev_codes = {p.stock_code for p in pos_rows}

    def _check_gap(self, ts: str) -> None:
        """I2: 距上次有效数据超过阈值 -> data_gap 告警。节流复用 insert_alert 的 60 分钟去重窗。"""
        for rule, code, msg in evaluate_gap_alert(self._last_ok_ts, ts, self.cfg.alert_rules):
            self.db.insert_alert(rule, code, msg, ts)

    def on_trade(self, trade) -> None:
        self.db.insert_trade(TradeRecord.from_xt(trade).to_row())

    def on_order(self, order) -> None:
        self.db.insert_order(OrderRecord.from_xt(order).to_row())

    def on_order_error(self, err) -> None:
        """I2: xtquant 委托失败(废单)回调 —— 原来不入库, orders 表只有正常回报。"""
        self._record_error("order_error", "委托失败", err)

    def on_cancel_error(self, err) -> None:
        """I2: xtquant 撤单失败回调。"""
        self._record_error("cancel_error", "撤单失败", err)

    def _record_error(self, rule: str, label: str, err) -> None:
        ts = self._now()
        order_id = str(getattr(err, "order_id", "") or "")
        code = str(getattr(err, "stock_code", "") or "")
        msg = "{} order_id={} error_id={} {}".format(
            label, order_id, getattr(err, "error_id", ""), getattr(err, "error_msg", ""))
        self.db.insert_order({"order_id": order_id, "stock_code": code, "status_msg": msg,
                              "order_status": -1, "received_at": ts})
        self.db.insert_alert(rule, code or order_id, msg, ts)

    def on_disconnect(self) -> None:
        """I2: 断线原来只写一行日志, 对话侧/货主看不到; 落 alerts, 由去重窗防刷屏。"""
        ts = self._now()
        self.db.insert_alert("disconnected", "", "QMT 连接断开", ts)
        logger.warning("QMT disconnected (alert recorded)")

    def run(self, stop_event=None) -> None:
        while not (stop_event is not None and stop_event.is_set()):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — 轮询循环必须存活
                logger.exception("poll failed: %s", exc)
                try:
                    self._check_gap(self._now())  # I2: 异常也算"本轮无有效数据"
                except Exception:  # noqa: BLE001 — 告警失败不得杀死轮询
                    logger.warning("data_gap alert failed", exc_info=True)
            time.sleep(self.cfg.poll_interval_s)
