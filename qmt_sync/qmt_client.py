# d:\cc-joesph\qmt_sync\qmt_client.py
"""xtquant 适配层(薄)。需 QMT 终端已登录并开启 miniQMT 模式。

连接约定(与官方 miniQMT 示例一致):
    trader = XtQuantTrader(cfg.qmt_data_dir, session_id, callback)
    trader.start(); trader.connect(); trader.subscribe(StockAccount(account_id))
"""
from __future__ import annotations
import logging
import sys
import time

logger = logging.getLogger("qmt_sync")


def _require_xtquant():
    """返回 (module_xttrader, module_xttype)。import 失败给出可操作提示。"""
    try:
        from xtquant import xttrader, xttype
        return xttrader, xttype
    except ImportError as exc:
        raise RuntimeError(
            "xtquant 不可导入。请确认: 1) QMT 终端已登录并开启 miniQMT 模式; "
            "2) 运行前设置 PYTHONPATH=D:\\QMT\\bin.x64\\Lib\\site-packages "
            "(或把 qmt_sync 用 QMT 自带 python 运行)。原始错误: %r" % exc
        ) from exc


class QmtCallback:
    """把 xtquant 回调转发给 SyncEngine。为避免强制继承 XtQuantTraderCallback,
    只依赖其 on_stock_trade / on_stock_order / on_disconnected 三个方法名。"""

    def __init__(self, engine):
        self._engine = engine

    def on_stock_trade(self, trade):
        try:
            self._engine.on_trade(trade)
        except Exception as exc:  # noqa: BLE001
            logger.exception("on_stock_trade failed: %s", exc)

    def on_stock_order(self, order):
        try:
            self._engine.on_order(order)
        except Exception as exc:  # noqa: BLE001
            logger.exception("on_stock_order failed: %s", exc)

    def on_disconnected(self):
        logger.warning("QMT disconnected")


class QmtClient:
    """对 SyncEngine 暴露 query_asset / query_positions; connect() 订阅回调。"""

    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self._account = None
        self._trader = None
        self._callback = None

    def start(self) -> None:
        xttrader, xttype = _require_xtquant()
        self._callback = xttrader.XtQuantTraderCallback.__new__(xttrader.XtQuantTraderCallback)
        # 把回调方法绑定到本对象的同名方法
        self._callback.on_stock_trade = QmtCallback(self.engine).on_stock_trade
        self._callback.on_stock_order = QmtCallback(self.engine).on_stock_order
        self._callback.on_disconnected = QmtCallback(self.engine).on_disconnected
        self._trader = xttrader.XtQuantTrader(self.cfg.qmt_data_dir, int(time.time()),
                                              self._callback)
        self._trader.start()
        result = self._trader.connect()
        logger.info("connect result: %s", result)
        if result != 0:
            raise RuntimeError("QMT connect failed, code=%s (请确认 miniQMT 已登录)" % result)

    def connect(self, account_id: str) -> None:
        _, xttype = _require_xtquant()
        self._account = xttype.StockAccount(account_id)
        self._trader.subscribe(self._account)

    def query_asset(self):
        return self._trader.query_stock_asset(self._account)

    def query_positions(self):
        return self._trader.query_stock_positions(self._account)


def make_client(cfg, engine) -> QmtClient:
    return QmtClient(cfg, engine)
