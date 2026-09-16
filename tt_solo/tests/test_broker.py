# -*- coding: utf-8 -*-
"""broker 测试: 用注入的 fake backend 覆盖只读语义与降级链。"""
import ast
from pathlib import Path

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


class FailAfterConnect(FakeBackend):
    """连接成功、查询抛异常 —— fail-open 红线里最关键的那一半。"""

    def asset(self):
        raise RuntimeError("query_stock_asset 炸了")

    def positions(self):
        raise RuntimeError("query_stock_positions 炸了")


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


# ------------------------------------------------------- fail-open 红线(查询期)

def test_asset_failure_after_connect_is_none():
    """连上了但资产查询炸 → None(不抛), 由调用方 fail-closed。"""
    acc = broker.LiveAccount(backend=FailAfterConnect())
    assert acc.connect() is True
    assert acc.asset() is None
    assert acc.total_asset() is None
    assert acc.available_cash() is None


def test_positions_failure_after_connect_is_empty():
    """连上了但持仓查询炸 → {}(不抛)。"""
    acc = broker.LiveAccount(backend=FailAfterConnect())
    assert acc.connect() is True
    assert acc.positions() == {}
    assert acc.can_use_map() == {}


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

FORBIDDEN_ROOT_IMPORTS = {"prism", "shared", "qmt_sync", "backtest", "legacy"}


def _imported_roots(src):
    """源码里所有 import 的根模块名(ast.walk 覆盖缩进/条件/函数内导入)。"""
    roots = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # 相对导入(from . import x / from .broker import Y)本就自包含 → 跳过
            names = [node.module] if node.module and not node.level else []
        else:
            continue
        roots.update(n.split(".")[0] for n in names)
    return roots


def test_module_does_not_import_forbidden_packages():
    """自包含硬约束: broker 的**真实 import** 里不得出现外部包。

    只认 AST 里的 import 语句, 不扫散文/注释 —— 词面扫描会把 docstring 里的
    来源说明也判死, 是维护陷阱。后续任务的整树行正则扫描
    (tests/test_selfcontained.py)覆盖全树, 本文件级检查与它互补: AST 认真实
    import 语句(含缩进的嵌套导入), 且与措辞无关。动态导入(importlib)不在
    本检查射程内, 由整树扫描 + 评审兜底。
    """
    src = Path(broker.__file__).read_text(encoding="utf-8")
    bad = _imported_roots(src) & FORBIDDEN_ROOT_IMPORTS
    assert not bad, f"broker 不得 import 外部包: {sorted(bad)}"


def test_import_scanner_discriminates():
    """把检查器的辨别力钉死(常驻负控): 相对导入不误报, 嵌套禁止导入必报。"""
    assert _imported_roots("from . import state\nfrom .broker import X\n") == set()
    assert _imported_roots(
        "def f():\n    if 0:\n        import prism.qmt\n") == {"prism"}
    assert _imported_roots("import os, time") == {"os", "time"}
