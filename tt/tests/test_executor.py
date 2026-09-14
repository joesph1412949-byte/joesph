# -*- coding: utf-8 -*-
"""tt 直连执行器测试(DirectExecutor) —— 全程 fake backend, 绝不触碰真实账户。

覆盖: dry_run 不提交 / 真实提交参数正确 / 幂等 / 风控未过跳过 /
账户不匹配整批拒单 / BUY-SELL 常量 / 执行层纵深防御 / 柜台拒单 / 未连接 / 撤单。
"""
import pytest

from tt.engine import Intent
from tt.executor import DirectExecutor, STOCK_BUY, STOCK_SELL, FIX_PRICE


class FakeBackend:
    """记录调用, 不真下单。"""

    def __init__(self, account_id="88869979", seq_start=1000):
        self.account_id = account_id
        self.orders_sent = []
        self._next = seq_start

    def connect(self):
        return True

    def order(self, code, order_type, volume, price, strategy="", remark=""):
        self._next += 1
        self.orders_sent.append(
            {"code": code, "type": order_type, "volume": volume,
             "price": price, "strategy": strategy, "remark": remark})
        return self._next

    def orders(self):
        return []

    def trades(self):
        return []

    def cancel(self, oid):
        return True


def mk_intent(code="600900.SH", side="SELL", price=28.537, volume=600,
              ok=True, rc="", unit=3, name="长江电力"):
    return Intent(code, name, side, price, volume, "档位%d" % unit, ok, rc, "",
                  "TT_20260914_600900SH_%s_%d" % (side, unit),
                  {"unit": unit})


@pytest.fixture
def ex():
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be)
    return e, be


# ---------------------------------------------------------------- 基本

def test_dry_run_does_not_submit(ex):
    e, be = ex
    r = e.execute([mk_intent()], dry_run=True)
    assert be.orders_sent == []
    assert r[0]["ok"] is True and r[0]["code"] == "DRY_RUN"


def test_dry_run_is_default(ex):
    e, be = ex
    r = e.execute([mk_intent()])
    assert be.orders_sent == [] and r[0]["code"] == "DRY_RUN"


def test_live_submits_with_correct_params(ex):
    e, be = ex
    r = e.execute([mk_intent()], dry_run=False)
    assert len(be.orders_sent) == 1
    o = be.orders_sent[0]
    assert o["code"] == "600900.SH"
    assert o["type"] == STOCK_SELL
    assert o["volume"] == 600
    assert o["price"] == pytest.approx(28.537)
    assert o["remark"] == "TT_20260914_600900SH_SELL_3"
    assert o["strategy"] == "tt_grid_v1"
    assert r[0]["ok"] and r[0]["code"] == "SUBMITTED"


def test_buy_uses_stock_buy_constant(ex):
    e, be = ex
    e.execute([mk_intent(side="BUY", price=28.239)], dry_run=False)
    assert be.orders_sent[0]["type"] == STOCK_BUY


def test_constants_match_official():
    assert (STOCK_BUY, STOCK_SELL, FIX_PRICE) == (23, 24, 11)


# ---------------------------------------------------------------- 幂等

def test_same_order_id_not_submitted_twice(ex):
    e, be = ex
    e.execute([mk_intent()], dry_run=False)
    r2 = e.execute([mk_intent()], dry_run=False)
    assert len(be.orders_sent) == 1
    assert r2[0]["code"] == "ALREADY_PLACED"


def test_different_order_ids_both_submitted(ex):
    e, be = ex
    e.execute([mk_intent(unit=1, price=28.239)], dry_run=False)
    e.execute([mk_intent(unit=2, price=28.388)], dry_run=False)
    assert len(be.orders_sent) == 2


# ---------------------------------------------------------------- 拒单

def test_unapproved_intent_skipped(ex):
    e, be = ex
    r = e.execute([mk_intent(ok=False, rc="DEVIATION_TOO_BIG")], dry_run=False)
    assert be.orders_sent == []
    assert r[0]["code"] == "NOT_APPROVED"


def test_account_mismatch_rejects_whole_batch():
    be = FakeBackend(account_id="99999999")
    e = DirectExecutor(account_id="88869979", backend=be)
    r = e.execute([mk_intent(), mk_intent(unit=4)], dry_run=False)
    assert be.orders_sent == []
    assert all(x["code"] == "ACCOUNT_MISMATCH" for x in r)


def test_order_rejected_by_counter():
    class RejectBackend(FakeBackend):
        def order(self, *a, **k):
            return -1

    e = DirectExecutor(account_id="88869979", backend=RejectBackend())
    r = e.execute([mk_intent()], dry_run=False)
    assert r[0]["ok"] is False and r[0]["code"] == "ORDER_REJECTED"


def test_not_connected_rejects_batch():
    class DeadBackend(FakeBackend):
        def connect(self):
            return False

    e = DirectExecutor(account_id="88869979", backend=DeadBackend())
    r = e.execute([mk_intent()], dry_run=False)
    assert r[0]["code"] == "NOT_CONNECTED"


# ---------------------------------------------------------------- 纵深防御

def test_amount_cap_defense():
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be,
                       max_order_amount=1000.0)
    r = e.execute([mk_intent(price=28.537, volume=600)], dry_run=False)
    assert be.orders_sent == [] and r[0]["code"] == "AMOUNT_TOO_BIG"


def test_price_cap_defense():
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, max_price=10.0)
    r = e.execute([mk_intent(price=28.537)], dry_run=False)
    assert be.orders_sent == [] and r[0]["code"] == "PRICE_TOO_HIGH"


def test_volume_not_lot(ex):
    e, be = ex
    r = e.execute([mk_intent(volume=150)], dry_run=False)
    assert be.orders_sent == [] and r[0]["code"] == "VOLUME_INVALID"


def test_price_invalid(ex):
    e, be = ex
    r = e.execute([mk_intent(price=0)], dry_run=False)
    assert be.orders_sent == [] and r[0]["code"] == "PRICE_INVALID"


# ---------------------------------------------------------------- propose_price

def test_propose_price_overrides(ex):
    e, be = ex
    r = e.execute([mk_intent(price=28.537)], dry_run=False,
                  propose_price=lambda it: 28.500)
    assert be.orders_sent[0]["price"] == pytest.approx(28.500)
    assert r[0]["price"] == pytest.approx(28.500)


def test_propose_price_failure_falls_back(ex):
    e, be = ex
    def boom(it):
        raise RuntimeError("no quote")
    e.execute([mk_intent(price=28.537)], dry_run=False, propose_price=boom)
    assert be.orders_sent[0]["price"] == pytest.approx(28.537)


# ---------------------------------------------------------------- 统计

def test_stats_tracked(ex):
    e, be = ex
    e.execute([mk_intent()], dry_run=True)      # skipped
    e.execute([mk_intent()], dry_run=False)     # submitted
    assert e.stats["submitted"] == 1
    assert e.stats["skipped"] >= 1


# ---------------------------------------------------------------- dict 入参

def test_accepts_signal_dict(ex):
    """daemon 传的是信号 dict(无 ok 字段) → 应放行并正确解析字段。"""
    e, be = ex
    sig = {"order_id": "TT_20260915_600900SH_SELL_1", "action": "SELL",
           "stock_code": "600900.SH", "price": 28.239, "volume": 300}
    r = e.execute([sig], dry_run=False)
    assert len(be.orders_sent) == 1
    o = be.orders_sent[0]
    assert o["code"] == "600900.SH" and o["type"] == STOCK_SELL
    assert o["volume"] == 300 and o["remark"] == sig["order_id"]
    assert r[0]["ok"] and r[0]["code"] == "SUBMITTED"


def test_signal_dict_missing_price_rejected(ex):
    e, be = ex
    sig = {"order_id": "X1", "action": "BUY", "stock_code": "600900.SH",
           "volume": 100}
    r = e.execute([sig], dry_run=False)
    assert be.orders_sent == [] and r[0]["code"] == "PRICE_INVALID"


def test_intent_and_dict_mixed(ex):
    e, be = ex
    sig = {"order_id": "TT_X_SELL_1", "action": "SELL",
           "stock_code": "600900.SH", "price": 28.239, "volume": 300}
    e.execute([mk_intent(unit=2), sig], dry_run=False)
    assert len(be.orders_sent) == 2


# ---------------------------------------------------------------- 撤单

def test_cancel_all_skips_finished():
    class OrderBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.cancelled = []

        def orders(self):
            class O:
                def __init__(self, oid, st):
                    self.order_id, self.order_status = oid, st
            return [O(1, 50), O(2, 56), O(3, 54)]   # 50 可撤, 56/54 终态

        def cancel(self, oid):
            self.cancelled.append(oid)
            return True

    be = OrderBackend()
    e = DirectExecutor(account_id="88869979", backend=be)
    assert e.cancel_all() == 1
    assert be.cancelled == [1]
