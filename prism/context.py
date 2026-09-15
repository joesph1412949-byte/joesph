# -*- coding: utf-8 -*-
"""因子上下文: 所有因子收到的统一数据包。字段取不到时一律 None(fail-open)。"""


class FactorContext:
    """因子只从 ctx 取自己需要的字段, 不碰任何数据源代码。"""

    def __init__(self, code=None, kline=None, tick=None, index_kline=None,
                 sector_map=None, limit_ups=None, em=None, fund=None,
                 manual=None, float_mv=None, last=None, last_close=None,
                 up_price=None, sealed=None, **kwargs):
        self.code = code
        self.kline = kline
        self.tick = tick
        self.index_kline = index_kline
        self.sector_map = sector_map or {}
        self.limit_ups = limit_ups or []
        self.em = em or {}
        self.fund = fund or {}
        self.manual = manual or {}
        self.float_mv = float_mv
        self.last = last
        self.last_close = last_close
        self.up_price = up_price
        self.sealed = sealed
        self._extra = kwargs

    def get(self, name):
        """按名字取字段, 缺失返回 None。支持因子取自定义扩展字段。"""
        if hasattr(self, name):
            return getattr(self, name)
        return self._extra.get(name)
