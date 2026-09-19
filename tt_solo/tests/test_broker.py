# -*- coding: utf-8 -*-
"""broker 测试: 用注入的 fake backend 覆盖只读语义与降级链。"""
import pytest

from ttcore import broker

from ._import_scan import forbidden_imports


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


class FailAfterConnect(FakeBackend):
    """连接成功、查询抛异常 —— fail-open 红线里最关键的那一半。"""

    def asset(self):
        raise RuntimeError("query_stock_asset 炸了")

    def positions(self):
        raise RuntimeError("query_stock_positions 炸了")


def test_asset_none_when_not_connected():
    acc = broker.LiveAccount(backend=FakeBackend(ok=False))
    assert acc.asset() is None
    # 未连接 = 拿不到持仓事实, 与"券商确认空仓"不是一回事
    assert acc.positions() is None


def test_positions_none_vs_confirmed_empty():
    """`{}` = 券商确认空仓; `None` = 查询失败/未连接。**两者必须可区分**。

    与 prism/live_account.py 同口径(tt_solo 自包含、不许 import, 所以这条
    注释就是唯一的同步手段)。查询失败若冒充"空仓":
      - 对账会把真持仓逐条抹掉并把空账本落盘, 卖出通道全灭;
      - 引擎侧 `positions is not None` 这道判据失效, 底仓/可卖量/持仓市值
        全被当成 0 —— 而 tt_solo 是**真正接了直连下单**的那条链。
    """
    confirmed_empty = broker.LiveAccount(backend=FakeBackend(positions={}))
    assert confirmed_empty.positions() == {}
    assert confirmed_empty.can_use_map() == {}

    failed = broker.LiveAccount(backend=FakeBackend(positions=None))
    assert failed.positions() is None
    assert failed.can_use_map() is None


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


# ------------------------------------------------------- fail-open 红线(查询期)

def test_asset_failure_after_connect_is_none():
    """连上了但资产查询炸 → None(不抛), 由调用方 fail-closed。"""
    acc = broker.LiveAccount(backend=FailAfterConnect())
    assert acc.connect() is True
    assert acc.asset() is None
    assert acc.total_asset() is None
    assert acc.available_cash() is None


def test_positions_failure_after_connect_is_none():
    """连上了但持仓查询炸 → None(不抛, 也**不**装空仓)。

    旧实现是 `return self.backend.positions() or {}` → 失败与空仓同形, 消费点
    无从分辨。
    """
    acc = broker.LiveAccount(backend=FailAfterConnect())
    assert acc.connect() is True
    assert acc.positions() is None
    assert acc.can_use_map() is None


def test_backend_positions_none_when_query_returns_none():
    """最底层 `_QmtBackend.positions()` 也不许把 query 的 None 装成空仓。

    QMT 的 `query_stock_positions` 在瞬时故障时返回 None —— 那一层就必须把
    "拿不到" 与 "确认空仓" 分开, 否则上面两层再怎么判都晚了一步。
    """
    b = broker._QmtBackend()

    class Trader:
        def __init__(self, rows):
            self.rows = rows

        def query_stock_positions(self, acc):
            return self.rows

    b._trader, b._acc = Trader(None), object()
    assert b.positions() is None

    b._trader = Trader([])                     # 确认空仓
    assert b.positions() == {}

    b._trader = Trader([type("P", (), {"stock_code": "600900.SH",
                                       "volume": 1000,
                                       "can_use_volume": 800})()])
    assert b.positions()["600900.SH"]["can_use_volume"] == 800


def test_can_use_map_coerces_weird_values():
    """QMT 字段可能是 str/None/空串/缺字段 —— 一律宽松转 int。"""
    acc = broker.LiveAccount(backend=FakeBackend(positions={
        "600900.SH": {"can_use_volume": "800"},   # 数字串 → 800
        "601088.SH": {"can_use_volume": None},    # None → 0
        "601398.SH": {"can_use_volume": ""},      # 空串 → 0
        "601857.SH": {},                          # 缺字段 → 0
        "600028.SH": {"can_use_volume": -5},      # 负值原样透出(裁剪由调用方决定)
    }))
    assert acc.can_use_map() == {"600900.SH": 800, "601088.SH": 0,
                                 "601398.SH": 0, "601857.SH": 0,
                                 "600028.SH": -5}


def test_connect_result_is_cached():
    """_ok 短路: 后续每次查询都不该重连。"""
    backend = FakeBackend(asset={"total_asset": 100.0, "cash": 50.0})
    acc = broker.LiveAccount(backend=backend)
    assert acc.connect() is True
    assert acc.connect() is True
    acc.asset()
    acc.positions()
    assert backend.calls == ["connect"]       # 只连一次


# ------------------------------------------------------------- 自包含硬约束


def test_module_does_not_import_forbidden_packages():
    """自包含硬约束: broker 的**真实 import** 里不得出现外部包。

    只认 AST 里的 import 语句, 不扫散文/注释 —— 词面扫描会把 docstring 里的
    来源说明也判死, 是维护陷阱。扫描器实现单一放在 tests/_import_scan.py,
    本文件与整树的 tests/test_selfcontained.py 共用它。动态导入(importlib)
    不在射程内, 由评审兜底。
    """
    assert forbidden_imports(broker.__file__) == []


def test_import_scanner_discriminates():
    """把共享扫描器的辨别力钉死(常驻负控): 相对导入不误报, 嵌套禁止导入必报。"""
    assert forbidden_imports(
        "x.py", "from . import state\nfrom .broker import X\n") == []
    assert forbidden_imports(
        "x.py", "def f():\n    if 0:\n        import prism.qmt\n") == ["prism"]
    # from X import Y 是另一条分支, 同样要钉住(否则删掉它整套测试仍全绿)
    assert forbidden_imports("x.py", "from prism.qmt import Trader\n") == ["prism"]
    assert forbidden_imports("x.py", "import os, time\n") == []
