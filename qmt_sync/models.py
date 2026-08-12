"""数据模型 + 从 xtquant 对象转换(鸭子类型, 不 import xtquant)。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _f(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _i(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _normalize_time(v):
    """把 xtquant 紧凑时间 "YYYYMMDDHHMMSS" 规整为 "YYYY-MM-DD HH:MM:SS"。

    解析失败或输入不匹配时原样返回(不中断、不篡改其它格式)。
    """
    s = str(v)
    if len(s) != 14 or not s.isdigit():
        return s
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s


def _attr(obj, *names, default=None):
    if obj is None:
        return default
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


@dataclass
class AssetSnapshot:
    account_id: str
    total_asset: float
    cash: float
    market_value: float
    frozen_cash: float
    update_time: str

    @classmethod
    def from_xt(cls, xt):
        return cls(
            account_id=str(_attr(xt, "account_id", default="")),
            total_asset=_f(_attr(xt, "total_asset")),
            cash=_f(_attr(xt, "cash")),
            market_value=_f(_attr(xt, "market_value")),
            frozen_cash=_f(_attr(xt, "frozen_cash")),
            update_time=_now(),
        )

    def to_row(self) -> dict:
        return {"account_id": self.account_id, "total_asset": self.total_asset, "cash": self.cash,
                "market_value": self.market_value, "frozen_cash": self.frozen_cash,
                "update_time": self.update_time}


@dataclass
class PositionSnapshot:
    account_id: str
    stock_code: str
    volume: int
    can_use_volume: int
    open_price: float
    market_value: float
    frozen_volume: int
    on_road_volume: int
    yesterday_volume: int
    update_time: str

    @classmethod
    def from_xt(cls, xt):
        return cls(
            account_id=str(_attr(xt, "account_id", default="")),
            stock_code=str(_attr(xt, "stock_code", default="")),
            volume=_i(_attr(xt, "volume")),
            can_use_volume=_i(_attr(xt, "can_use_volume")),
            open_price=_f(_attr(xt, "open_price")),
            market_value=_f(_attr(xt, "market_value")),
            frozen_volume=_i(_attr(xt, "frozen_volume")),
            on_road_volume=_i(_attr(xt, "on_road_volume")),
            yesterday_volume=_i(_attr(xt, "yesterday_volume")),
            update_time=_now(),
        )

    def to_row(self) -> dict:
        return {"account_id": self.account_id, "stock_code": self.stock_code, "volume": self.volume,
                "can_use_volume": self.can_use_volume, "open_price": self.open_price,
                "market_value": self.market_value, "frozen_volume": self.frozen_volume,
                "on_road_volume": self.on_road_volume, "yesterday_volume": self.yesterday_volume,
                "update_time": self.update_time}


@dataclass
class TradeRecord:
    account_id: str
    stock_code: str
    order_type: int
    traded_id: str
    traded_time: str
    traded_price: float
    traded_volume: int
    traded_amount: float
    order_id: str
    received_at: str

    @classmethod
    def from_xt(cls, xt):
        return cls(
            account_id=str(_attr(xt, "account_id", default="")),
            stock_code=str(_attr(xt, "stock_code", default="")),
            order_type=_i(_attr(xt, "order_type")),
            traded_id=str(_attr(xt, "traded_id", default="")),
            traded_time=_normalize_time(_attr(xt, "traded_time", default="")),
            traded_price=_f(_attr(xt, "traded_price")),
            traded_volume=_i(_attr(xt, "traded_volume")),
            traded_amount=_f(_attr(xt, "traded_amount")),
            order_id=str(_attr(xt, "order_id", default="")),
            received_at=_now(),
        )

    def to_row(self) -> dict:
        return {"account_id": self.account_id, "stock_code": self.stock_code, "order_type": self.order_type,
                "traded_id": self.traded_id, "traded_time": self.traded_time, "traded_price": self.traded_price,
                "traded_volume": self.traded_volume, "traded_amount": self.traded_amount,
                "order_id": self.order_id, "received_at": self.received_at}


@dataclass
class OrderRecord:
    account_id: str
    stock_code: str
    order_id: str
    order_sysid: str
    order_time: str
    order_type: int
    order_volume: int
    price_type: int
    price: float
    traded_volume: int
    traded_price: float
    order_status: int
    status_msg: str
    received_at: str

    @classmethod
    def from_xt(cls, xt):
        return cls(
            account_id=str(_attr(xt, "account_id", default="")),
            stock_code=str(_attr(xt, "stock_code", default="")),
            order_id=str(_attr(xt, "order_id", default="")),
            order_sysid=str(_attr(xt, "order_sysid", default="")),
            order_time=str(_attr(xt, "order_time", default="")),
            order_type=_i(_attr(xt, "order_type")),
            order_volume=_i(_attr(xt, "order_volume")),
            price_type=_i(_attr(xt, "price_type")),
            price=_f(_attr(xt, "price")),
            traded_volume=_i(_attr(xt, "traded_volume")),
            traded_price=_f(_attr(xt, "traded_price")),
            order_status=_i(_attr(xt, "order_status")),
            status_msg=str(_attr(xt, "status_msg", default="")),
            received_at=_now(),
        )

    def to_row(self) -> dict:
        return {"account_id": self.account_id, "stock_code": self.stock_code, "order_id": self.order_id,
                "order_sysid": self.order_sysid, "order_time": self.order_time, "order_type": self.order_type,
                "order_volume": self.order_volume, "price_type": self.price_type, "price": self.price,
                "traded_volume": self.traded_volume, "traded_price": self.traded_price,
                "order_status": self.order_status, "status_msg": self.status_msg, "received_at": self.received_at}
