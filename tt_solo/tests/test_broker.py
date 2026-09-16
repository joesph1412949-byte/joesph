# -*- coding: utf-8 -*-
"""broker 测试: 用注入的 fake backend 覆盖只读语义与降级链。"""
import pytest

from ttcore import broker


class FakeBackend:
    def __init__(self, ok=True, asset=None, positions=None):
        self._ok = ok
        self._asset = asset
        self._positions = positions
        self.calls = []

    def connect(self):
        self.calls.append("connect")
        return self._ok

    def asset(self):
        return self._asset

    def positions(self):
        return self._positions


def test_asset_none_when_not_connected():
    acc = broker.LiveAccount(backend=FakeBackend(ok=False))
    assert acc.asset() is None
    assert acc.positions() == {}


def test_asset_and_positions_pass_through():
    acc = broker.LiveAccount(backend=FakeBackend(
        asset={"total_asset": 274648.96, "cash": 188570.96},
        positions={"600900.SH": {"volume": 1000, "can_use_volume": 1000,
                                 "market_value": 28630.0}}))
    a = acc.asset()
    assert a["total_asset"] == pytest.approx(274648.96)
    assert a["cash"] == pytest.approx(188570.96)
    p = acc.positions()
    assert p["600900.SH"]["can_use_volume"] == 1000


def test_connect_exception_is_swallowed():
    class Boom(FakeBackend):
        def connect(self):
            raise RuntimeError("xtquant missing")
    acc = broker.LiveAccount(backend=Boom())
    assert acc.connect() is False
    assert acc.asset() is None


def test_total_asset_and_cash_zero_become_none():
    acc = broker.LiveAccount(backend=FakeBackend(
        asset={"total_asset": 0, "cash": 0}))
    assert acc.total_asset() is None
    assert acc.available_cash() is None


def test_can_use_map():
    acc = broker.LiveAccount(backend=FakeBackend(positions={
        "600900.SH": {"volume": 1000, "can_use_volume": 800},
        "601088.SH": {"volume": 300, "can_use_volume": 0},
    }))
    assert acc.can_use_map() == {"600900.SH": 800, "601088.SH": 0}


def test_calc_buy_volume_floor_to_lot():
    # 100000 × 0.15 / 28.5 = 526.3 → 500
    assert broker.calc_buy_volume(28.5, 100000, 0.15) == 500


def test_calc_buy_volume_rejects_bad_input():
    assert broker.calc_buy_volume(0, 100000) == 0
    assert broker.calc_buy_volume(28.5, 0) == 0
    assert broker.calc_buy_volume(28.5, 100000, 0) == 0
    # 不足一手 → 0
    assert broker.calc_buy_volume(999.0, 1000, 0.15) == 0


def test_module_does_not_import_prism_or_shared():
    """自包含的硬约束: broker 不得引入外部包。"""
    src = (broker.__file__)
    text = open(src, encoding="utf-8").read()
    assert "prism" not in text.replace("prism/live_account.py", "")
    assert "shared" not in text
