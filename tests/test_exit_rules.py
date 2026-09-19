# -*- coding: utf-8 -*-
"""exit_rules 单元测试 — 止盈/止损/持有期规则与持仓档案, 全离线。"""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from shared.exit_rules import ExitRule, PositionBook


def test_stop_loss_triggers():
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=date(2026, 8, 1),
                 stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5,
                 today=date(2026, 8, 3))
    action, reason = r.evaluate(9.5)     # -5% → 止损
    assert action == "SELL"
    assert "止损" in reason


def test_take_profit_triggers():
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=date(2026, 8, 1),
                 stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5,
                 today=date(2026, 8, 3))
    action, reason = r.evaluate(10.8)    # +8% → 止盈
    assert action == "SELL"
    assert "止盈" in reason


def test_hold_days_triggers():
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=date(2026, 8, 1),
                 stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=3,
                 today=date(2026, 8, 4))  # 第 3 天
    action, reason = r.evaluate(10.2)    # 未到止盈止损
    assert action == "SELL"
    assert "持有期满" in reason


def test_no_action_within_band():
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=date(2026, 8, 1),
                 stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5,
                 today=date(2026, 8, 3))
    action, _ = r.evaluate(10.2)         # +2% 在带内
    assert action is None


def test_stop_loss_priority_over_take_profit():
    # 不可能同时触发, 但若 price 传错让两规则都命中, 止损优先(代码顺序保证)
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=date(2026, 8, 1),
                 stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5,
                 today=date(2026, 8, 3))
    action, reason = r.evaluate(9.0)
    assert action == "SELL" and "止损" in reason


def test_invalid_price_no_action():
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=date(2026, 8, 1))
    assert r.evaluate(0) == (None, None)
    assert r.evaluate(None) == (None, None)
    r0 = ExitRule("600000.SH", "浦发", buy_price=0, buy_date=date(2026, 8, 1))
    assert r0.evaluate(10.0) == (None, None)


def test_position_book_add_evaluate_all():
    book = PositionBook()
    book.add("600000.SH", "浦发", 10.0, "2026-08-01", volume=100)
    book.add("000001.SZ", "平安", 20.0, "2026-08-01", volume=200)
    hits = book.evaluate_all(
        {"600000.SH": 9.4, "000001.SZ": 21.6},       # 浦发-6%止损; 平安+8%止盈
        take_profit_pct=0.08, stop_loss_pct=0.05, max_hold_days=5)
    by_code = {c: (a, r) for c, _p, a, r in hits}
    assert set(by_code) == {"600000.SH", "000001.SZ"}
    assert "止损" in by_code["600000.SH"][1]
    assert "止盈" in by_code["000001.SZ"][1]


def test_position_book_evaluate_all_skips_missing_price():
    book = PositionBook()
    book.add("600000.SH", "浦发", 10.0, "2026-08-01")
    hits = book.evaluate_all({})     # 无行情 → 不判定
    assert hits == []


def test_position_book_remove_and_get():
    book = PositionBook()
    book.add("600000.SH", "浦发", 10.0, "2026-08-01")
    assert book.get("600000.SH")["buy_price"] == 10.0
    book.remove("600000.SH")
    assert book.get("600000.SH") is None


# ---------------- 坏账本数据不得瘫痪/永久卡死卖出 ----------------

def test_evaluate_all_skips_missing_buy_price_without_raising():
    """缺 buy_price 的持仓跳过, 不能 KeyError 逃出 evaluate_all(整轮巡检瘫痪)。"""
    book = PositionBook({
        "600000.SH": {"code": "600000.SH", "name": "浦发", "buy_date": "2026-08-01",
                      "volume": 100},                       # 无 buy_price
        "000001.SZ": {"code": "000001.SZ", "name": "平安", "buy_price": 20.0,
                      "buy_date": "2026-08-01", "volume": 200},
    })
    hits = book.evaluate_all({"600000.SH": 1.0, "000001.SZ": 19.0},
                             take_profit_pct=0.08, stop_loss_pct=0.05,
                             max_hold_days=5)
    assert [c for c, _p, _a, _r in hits] == ["000001.SZ"]


def test_exit_rule_unknown_buy_date_does_not_fall_back_to_today():
    """buy_date 不可解析 → 视为未知: 不回落 today(否则 T+1 恒拦、持有期恒 0)。"""
    r = ExitRule("600000.SH", "浦发", buy_price=10.0, buy_date=None,
                 today=date(2026, 9, 15), enforce_t1=True,
                 stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5)
    assert r.evaluate(9.0)[0] == "SELL"        # 止损照常(不再被当"今日买入")
    assert r.evaluate(10.2) == (None, None)    # 持有期算不出 → 不误触发


def test_evaluate_all_unknown_buy_date_t1_delegated_to_can_use_volume():
    book = PositionBook({
        "600000.SH": {"code": "600000.SH", "name": "浦发", "buy_price": 10.0,
                      "buy_date": "坏日期", "volume": 1000}})
    kw = dict(stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5,
              today=date(2026, 9, 15), enforce_t1=True)
    prices = {"600000.SH": 9.0}                # -10% → 止损
    # 券商可卖量 > 0(权威) → 卖
    hits = book.evaluate_all(prices, can_use={"600000.SH": 1000}, **kw)
    assert [c for c, _p, _a, _r in hits] == ["600000.SH"]
    # 券商可卖量 0 → 不卖(T+1/冻结)
    assert book.evaluate_all(prices, can_use={"600000.SH": 0}, **kw) == []


def test_evaluate_all_unknown_buy_date_without_can_use_is_fail_closed():
    """边界裁定: 日期未知 且 可卖量两条来源都拿不到 → 不卖(防 T+1 违规)。"""
    kw = dict(stop_loss_pct=0.05, take_profit_pct=0.08, max_hold_days=5,
              today=date(2026, 9, 15), enforce_t1=True)
    prices = {"600000.SH": 9.0}
    # 账本记 can_use_volume=0 → 不卖
    book0 = PositionBook({
        "600000.SH": {"code": "600000.SH", "buy_price": 10.0, "buy_date": None,
                      "volume": 1000, "can_use_volume": 0}})
    assert book0.evaluate_all(prices, can_use={}, **kw) == []
    # 账本没记 + 券商没给 → 不卖
    book_x = PositionBook({
        "600000.SH": {"code": "600000.SH", "buy_price": 10.0, "buy_date": None,
                      "volume": 1000}})
    assert book_x.evaluate_all(prices, can_use={}, **kw) == []
    assert book_x.evaluate_all(prices, **kw) == []          # 旧语义 can_use=None


def test_position_book_from_json():
    book = PositionBook.from_json({"600000.SH": {"code": "600000.SH", "name": "浦发",
                                                 "buy_price": 10.0,
                                                 "buy_date": "2026-08-01",
                                                 "volume": 100}})
    assert book.get("600000.SH")["buy_price"] == 10.0
    assert PositionBook.from_json(None).all() == {}
