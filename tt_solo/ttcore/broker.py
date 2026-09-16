# -*- coding: utf-8 -*-
"""miniQMT 实盘账户适配层(只读): 资产 / 持仓 / T+1 可卖量 / 可用资金。

ponytail: 整体搬自 prism/live_account.py @2026-09-16 —— 该模块本就不依赖
prism 内部, 搬过来即可让 tt_solo 自包含。

安全边界:
  - 只做 query_* 查询, 绝不调用 order_stock / cancel_order_stock 等下单接口;
  - 全部 fail-open: 未连接/查询异常 → None / {}, 由调用方 fail-closed 决策;
  - 后端可注入(backend 参数), 离线测试无需 QMT 终端。

用法:
    acc = LiveAccount()              # 自动枚举已登录资金账号
    if acc.connect():
        acc.asset()                  # {'total_asset':..., 'cash':...}
        acc.positions()              # {code: {'volume':..,'can_use_volume':..}}
        calc_buy_volume(10.55, acc.total_asset(), 0.15)
"""
import time

QMT_DATA_DIR = r"D:\QMT\userdata_mini"
LOT = 100                       # A股一手 = 100 股


def _num(v, default=0.0):
    """宽松数值转换(QMT 字段可能是 str/None/float)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _attr(obj, *names, default=None):
    """按候选名取属性(兼容 QMT 各版本字段命名差异)。"""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


class _QmtBackend:
    """真实 miniQMT 后端: 懒加载 xtquant, 完成连接/订阅/查询。"""

    def __init__(self, data_dir=QMT_DATA_DIR, account_id=""):
        self.data_dir = data_dir
        self.account_id = account_id
        self._trader = None
        self._acc = None

    def connect(self):
        if self._trader is not None and self._acc is not None:
            return True
        from xtquant import xttrader, xttype
        trader = xttrader.XtQuantTrader(self.data_dir, int(time.time()))
        trader.start()
        if trader.connect() != 0:
            return False
        acc_id = self.account_id
        if not acc_id:
            infos = trader.query_account_infos() or []
            ids = [str(_attr(a, "account_id", "accountId", "accountID", default=""))
                   for a in infos]
            ids = [i for i in ids if i]
            if not ids:
                return False
            acc_id = ids[0]
        acc = xttype.StockAccount(acc_id, "STOCK")
        trader.subscribe(acc)
        self._trader, self._acc = trader, acc
        return True

    def asset(self):
        a = self._trader.query_stock_asset(self._acc)
        if a is None:
            return None
        return {
            "total_asset": _num(_attr(a, "total_asset", "totalAsset", default=0)),
            "cash": _num(_attr(a, "cash", default=0)),
            "market_value": _num(_attr(a, "market_value", "marketValue", default=0)),
            "frozen_cash": _num(_attr(a, "frozen_cash", "frozenCash", default=0)),
        }

    def positions(self):
        rows = self._trader.query_stock_positions(self._acc) or []
        out = {}
        for p in rows:
            code = str(_attr(p, "stock_code", "stockCode", default="") or "")
            if not code:
                continue
            out[code] = {
                "volume": int(_num(_attr(p, "volume", default=0))),
                "can_use_volume": int(_num(_attr(
                    p, "can_use_volume", "canUseVolume", default=0))),
                "open_price": _num(_attr(p, "open_price", "openPrice", default=0)),
                "market_value": _num(_attr(p, "market_value", "marketValue",
                                           default=0)),
            }
        return out


class LiveAccount:
    """实盘账户门面(只读)。backend 可注入 fake 以便离线测试。"""

    def __init__(self, data_dir=QMT_DATA_DIR, account_id="", backend=None):
        self.data_dir = data_dir
        self.account_id = account_id
        self.backend = backend or _QmtBackend(data_dir, account_id)
        self._ok = False

    def connect(self):
        if self._ok:
            return True
        try:
            self._ok = bool(self.backend.connect())
        except Exception:
            self._ok = False
        return self._ok

    def asset(self):
        """{'total_asset','cash','market_value','frozen_cash'} 或 None。"""
        if not self.connect():
            return None
        try:
            return self.backend.asset()
        except Exception:
            return None

    def positions(self):
        """{code: {...}}; 查询失败/未连接 → {}(调用方须与 asset() 交叉验证)。"""
        if not self.connect():
            return {}
        try:
            return self.backend.positions() or {}
        except Exception:
            return {}

    def total_asset(self):
        a = self.asset()
        if not a:
            return None
        v = _num(a.get("total_asset"), 0)
        return v if v > 0 else None

    def available_cash(self):
        a = self.asset()
        if not a:
            return None
        v = _num(a.get("cash"), 0)
        return v if v > 0 else None

    def can_use_map(self):
        """{code: 可卖量} — 供 exit_rules.evaluate_all 做 T+1 双保险。"""
        return {c: int(_num(p.get("can_use_volume"), 0))
                for c, p in self.positions().items()}


def calc_buy_volume(price, total_asset, position_ratio=0.15, lot=LOT):
    """按 总资产 × 单只比例 ÷ 挂单价 计算买入股数, 向下取整到整手。

    参数缺失/非法 → 0(调用方据此跳过该候选, 宁缺勿错)。与
    strategy_close_pick.calc_buy_volume 同思路, 但基数用总资产(净值)而非
    可用现金 —— 与 paper 侧"每只 15% 净值"口径对齐。
    """
    price = _num(price, 0)
    total = _num(total_asset, 0)
    if price <= 0 or total <= 0 or position_ratio <= 0:
        return 0
    shares = int(total * position_ratio / price // lot * lot)
    return shares if shares >= lot else 0
