# -*- coding: utf-8 -*-
"""tt 直连执行器测试(DirectExecutor) —— 全程 fake backend, 绝不触碰真实账户。

覆盖: dry_run 不提交 / 真实提交参数正确 / 幂等 / 风控未过跳过 /
账户不匹配整批拒单 / BUY-SELL 常量 / 执行层纵深防御 / 柜台拒单 / 未连接 / 撤单 /
propose_price 的滑点复核 / 跨进程 order_id 幂等 / 方向字段校验。
"""
import json
from datetime import datetime

import pytest

from ttcore.engine import Intent
from ttcore.executor import DirectExecutor, STOCK_BUY, STOCK_SELL, FIX_PRICE


class FakeBackend:
    """记录调用, 不真下单。

    seq_fn=None → 自增正数(全受理); 给了 callable(第n笔) → 用它当返回值,
    <0 表示柜台拒单(C1 的取证场景)。
    """

    def __init__(self, account_id="88869979", seq_start=1000, seq_fn=None):
        self.account_id = account_id
        self.orders_sent = []
        self._next = seq_start
        self.seq_fn = seq_fn

    def connect(self):
        return True

    def order(self, code, order_type, volume, price, strategy="", remark=""):
        self.orders_sent.append(
            {"code": code, "type": order_type, "volume": volume,
             "price": price, "strategy": strategy, "remark": remark})
        if self.seq_fn is not None:
            return self.seq_fn(len(self.orders_sent))
        self._next += 1
        return self._next

    def orders(self):
        return []

    def trades(self):
        return []

    def cancel(self, oid):
        return True


def mk_intent(code="600900.SH", side="SELL", price=28.537, volume=600,
              ok=True, rc="", unit=3, name="长江电力", meta=None):
    return Intent(code, name, side, price, volume, "档位%d" % unit, ok, rc, "",
                  "TT_20260914_600900SH_%s_%d" % (side, unit),
                  meta if meta is not None else {"unit": unit})


def mk_gated_intent(**kw):
    """带上阶梯价/中枢的 Intent —— 执行层复核滑点/偏离所需的 meta。"""
    kw.setdefault("meta", {"unit": 3, "ladder_price": 28.537, "ref": 28.09})
    return mk_intent(**kw)


def _now():
    return datetime(2026, 9, 14, 10, 0, 0)


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
    """改价生效 —— 前提是 meta 里带着可比参考价(见 _verify_proposed_price)。"""
    e, be = ex
    r = e.execute([mk_gated_intent(price=28.537)], dry_run=False,
                  propose_price=lambda it: 28.500)
    assert be.orders_sent[0]["price"] == pytest.approx(28.500)
    assert r[0]["price"] == pytest.approx(28.500)


def test_propose_price_failure_falls_back(ex):
    e, be = ex
    def boom(it):
        raise RuntimeError("no quote")
    e.execute([mk_intent(price=28.537)], dry_run=False, propose_price=boom)
    assert be.orders_sent[0]["price"] == pytest.approx(28.537)


# ------------------------------------------------- propose_price 的滑点复核
# 生产链路(daemon → 信号文件→QMT 桥)本来就没有 propose_price, 而文档声称
# "执行器会再跑一遍闸门"。这里把它做成真的: propose_price 一旦顶替了档位价,
# 必须用 Intent.meta 里的 ladder_price/ref 复核滑点与偏离。

def test_propose_price_beyond_slippage_rejected():
    """委托价相对阶梯价偏离 > 3% → 拒(旧代码照样 SUBMITTED)。"""
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now,
                       max_slippage_pct=0.03)
    # 阶梯价 28.537, 改报 27.0 → 偏离 5.39%
    r = e.execute([mk_gated_intent()], dry_run=False,
                  propose_price=lambda it: 27.0)
    assert be.orders_sent == []
    assert r[0]["ok"] is False and r[0]["code"] == "SLIPPAGE_TOO_BIG"
    assert e.stats["rejected"] == 1


def test_propose_price_within_slippage_accepted():
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now,
                       max_slippage_pct=0.03)
    r = e.execute([mk_gated_intent()], dry_run=False,
                  propose_price=lambda it: 28.500)
    assert be.orders_sent[0]["price"] == pytest.approx(28.500)
    assert r[0]["ok"] is True and r[0]["code"] == "SUBMITTED"


def test_propose_price_beyond_deviation_rejected():
    """相对中枢(meta['ref'])的偏离同样要复核。"""
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now,
                       max_price_deviation_pct=0.05)
    r = e.execute([mk_gated_intent()], dry_run=False,
                  propose_price=lambda it: 30.0)      # 30.0/28.09 = +6.8%
    assert be.orders_sent == []
    assert r[0]["code"] == "DEVIATION_TOO_BIG"


def test_propose_price_without_reference_is_rejected():
    """没有任何参考价可比 → 拒(fail-closed: 否则 propose_price 就是绕过闸门)。"""
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now,
                       max_slippage_pct=0.03)
    r = e.execute([mk_intent(meta={"unit": 1})], dry_run=False,
                  propose_price=lambda it: 28.500)
    assert be.orders_sent == []
    assert r[0]["code"] == "PRICE_UNVERIFIED"


def test_no_propose_price_skips_extra_check(ex):
    """propose_price 的替代价与档位价相同(或没给)时, 不额外拦(引擎侧已查过)。"""
    e, be = ex
    r = e.execute([mk_intent()], dry_run=False,
                  propose_price=lambda it: 28.537)
    assert r[0]["ok"] is True and len(be.orders_sent) == 1


# ------------------------------------------------- order_id 跨进程幂等(I4)

def test_placed_ids_persist_across_processes(tmp_path):
    """进程 A 提交过的 order_id, 进程 B(新对象)必须凭盘上记录拒重。

    旧实现 _placed 只在内存: 盘中升版重启(本仓承认这是正常发版窗口, 见
    tests/test_state.py 的同日版本不符用例)→ 账本被重置 → 同一批 order_id
    重现 → 柜台收到两遍(实测 4 笔, 重复 2 个)。
    """
    p = tmp_path / "tt_placed.jsonl"
    be1 = FakeBackend()
    DirectExecutor(account_id="88869979", backend=be1, now_fn=_now,
                   placed_path=p).execute([mk_intent()], dry_run=False)
    assert len(be1.orders_sent) == 1

    be2 = FakeBackend()                      # 模拟重启: 全新 _placed
    r = DirectExecutor(account_id="88869979", backend=be2, now_fn=_now,
                       placed_path=p).execute([mk_intent()], dry_run=False)
    assert be2.orders_sent == []
    assert r[0]["code"] == "ALREADY_PLACED"


def test_placed_ids_expire_next_day(tmp_path):
    """跨日自动失效: 昨天的 order_id 不放行今天(否则永久锁死)。"""
    p = tmp_path / "tt_placed.jsonl"
    be1 = FakeBackend()
    DirectExecutor(account_id="88869979", backend=be1, now_fn=_now,
                   placed_path=p).execute([mk_intent()], dry_run=False)

    be2 = FakeBackend()
    tomorrow = DirectExecutor(
        account_id="88869979", backend=be2,
        now_fn=lambda: datetime(2026, 9, 15, 10, 0, 0),
        placed_path=p)
    r = tomorrow.execute([mk_intent()], dry_run=False)
    assert len(be2.orders_sent) == 1
    assert r[0]["code"] == "SUBMITTED"


def test_placed_file_is_append_only_jsonl(tmp_path):
    """落盘形态: 每行一个 JSON(append-only, 与 tt_history.jsonl 同款手法)。"""
    p = tmp_path / "tt_placed.jsonl"
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now,
                       placed_path=p)
    e.execute([mk_intent(unit=1, price=28.239),
               mk_intent(unit=2, price=28.388)], dry_run=False)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    assert [r["order_id"] for r in rows] == [
        "TT_20260914_600900SH_SELL_1", "TT_20260914_600900SH_SELL_2"]
    assert all(r["date"] == "20260914" for r in rows)


def test_rejected_order_is_not_recorded_as_placed(tmp_path):
    """被柜台拒的单不能进 placed 文件 —— 否则重试窗口被永久关掉。"""
    p = tmp_path / "tt_placed.jsonl"

    class RejectBackend(FakeBackend):
        def order(self, *a, **k):
            return -1

    e = DirectExecutor(account_id="88869979", backend=RejectBackend(),
                       now_fn=_now, placed_path=p)
    assert e.execute([mk_intent()], dry_run=False)[0]["ok"] is False
    assert not p.exists()
    assert e.execute([mk_intent()], dry_run=False)[0]["code"] == "ORDER_REJECTED"


# ------------------------------------------------- 方向字段校验(Minor 5)

@pytest.mark.parametrize("bad", ["", None, "LONG", "SEL", "BUYY", "  "])
def test_invalid_side_rejected(bad):
    """`action` 缺失/空串/乱写 → 一律拒 SIDE_INVALID。

    旧实现 `"BUY" if side.startswith("B") else "SELL"`: 空串与任何非 B 开头
    的垃圾都会**被当成 SELL 提交**(真实卖出), 这是最危险的一类静默降级。
    """
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now)
    sig = {"order_id": "X1", "stock_code": "600900.SH", "price": 28.5,
           "volume": 100, "action": bad}
    r = e.execute([sig], dry_run=False)
    assert be.orders_sent == []
    assert r[0]["code"] == "SIDE_INVALID"


@pytest.mark.parametrize("good,expect", [("BUY", STOCK_BUY), ("buy", STOCK_BUY),
                                        ("SELL", STOCK_SELL),
                                        ("sell", STOCK_SELL)])
def test_valid_side_accepted(good, expect):
    be = FakeBackend()
    e = DirectExecutor(account_id="88869979", backend=be, now_fn=_now)
    sig = {"order_id": "X2", "stock_code": "600900.SH", "price": 28.5,
           "volume": 100, "action": good}
    assert e.execute([sig], dry_run=False)[0]["ok"] is True
    assert be.orders_sent[0]["type"] == expect


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
