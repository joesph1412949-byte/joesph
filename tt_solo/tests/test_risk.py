# -*- coding: utf-8 -*-
"""风控引擎测试: 每一道闸门单独验证 + 聚合短路顺序。"""
from datetime import datetime

import pytest

from ttcore import risk
from ttcore.risk import CircuitBreaker, RiskGate

SESSION = {"open_start": "09:30", "open_end": "14:55",
           "converge_after": "14:30", "hard_stop_after": "14:57"}

# 固定"今天" = 2026-09-14(周一)。session_phase 现在带日历闸门, 不传 now 会
# 按真实墙钟判 —— 周六跑测试就会全线 SESSION_CLOSED, 所以每个用例都要钉住它。
MONDAY = datetime(2026, 9, 14, 10, 0, 0)
SATURDAY = datetime(2026, 9, 19, 10, 0, 0)


# ---------------------------------------------------------------- 时段

def test_session_phase():
    assert risk.session_phase("09:00", SESSION, MONDAY) == risk.PHASE_CLOSED
    assert risk.session_phase("09:30", SESSION, MONDAY) == risk.PHASE_OPEN
    assert risk.session_phase("11:00", SESSION, MONDAY) == risk.PHASE_OPEN
    assert risk.session_phase("14:30", SESSION, MONDAY) == risk.PHASE_CONVERGE
    assert risk.session_phase("14:55", SESSION, MONDAY) == risk.PHASE_CLOSED
    assert risk.session_phase("15:30", SESSION, MONDAY) == risk.PHASE_CLOSED
    assert risk.session_phase("", SESSION, MONDAY) == risk.PHASE_CLOSED
    assert risk.session_phase("abc", SESSION, MONDAY) == risk.PHASE_CLOSED


def test_session_phase_weekend_is_closed():
    """非交易日(周六/周日)一律 CLOSED。

    实测 2026-09-19(周六)QMT 仍在推 tick, 且 lastPrice 比 09-18 真实收盘高
    10%(600900 报 31.10, 真收盘 28.27) —— 拿它做T就是按假价真报单。
    """
    assert risk.session_phase("10:00", SESSION, SATURDAY) == risk.PHASE_CLOSED
    sunday = datetime(2026, 9, 20, 10, 0, 0)
    assert risk.session_phase("10:00", SESSION, sunday) == risk.PHASE_CLOSED


def test_session_phase_weekday_same_time_still_open():
    """周一同一时刻不受周末闸门影响(别把闸门做成恒闭)。"""
    monday = datetime(2026, 9, 21, 10, 0, 0)
    assert monday.weekday() == 0
    assert risk.session_phase("10:00", SESSION, monday) == risk.PHASE_OPEN


def test_check_session_open_allows_both():
    assert risk.check_session("10:00", SESSION, "BUY", now=MONDAY).ok
    assert risk.check_session("10:00", SESSION, "SELL", now=MONDAY).ok


def test_check_session_converge_only_allows_closing_buy():
    # 收敛时段: 已卖 500 未买回 → 归位买入放行
    assert risk.check_session("14:40", SESSION, "BUY", sold_today=500,
                              bought_today=0, now=MONDAY).ok
    # 收敛时段: 再开新卖出 → 拒
    v = risk.check_session("14:40", SESSION, "SELL", sold_today=500,
                           now=MONDAY)
    assert not v.ok and v.code == "SESSION_CONVERGE"
    # 收敛时段: 无敞口时买入(等于新开仓) → 拒
    v = risk.check_session("14:40", SESSION, "BUY", sold_today=0,
                           bought_today=0, now=MONDAY)
    assert not v.ok


def test_check_session_closed():
    v = risk.check_session("15:10", SESSION, "BUY", now=MONDAY)
    assert not v.ok and v.code == "SESSION_CLOSED"


def test_check_session_weekend_rejects_in_hours():
    v = risk.check_session("10:00", SESSION, "SELL", now=SATURDAY)
    assert not v.ok and v.code == "SESSION_CLOSED"


# ---------------------------------------------------------------- 价格

def test_price_sanity():
    assert risk.check_price_sanity(28.45).ok
    assert not risk.check_price_sanity(0).ok
    assert not risk.check_price_sanity(-1).ok
    assert not risk.check_price_sanity(None).ok
    assert not risk.check_price_sanity(999999).ok


def test_price_deviation():
    assert risk.check_price_deviation(28.45, 28.09, 0.05).ok
    assert not risk.check_price_deviation(30.0, 28.09, 0.05).ok
    assert not risk.check_price_deviation(28.45, 0, 0.05).ok
    assert not risk.check_price_deviation(28.45, None, 0.05).ok


def test_limit_band():
    # 主板 10%
    assert risk.check_limit_band(28.09 * 1.09, 28.09, "600900.SH").ok
    v = risk.check_limit_band(28.09 * 1.11, 28.09, "600900.SH")
    assert not v.ok and v.code == "BAND_OUT"
    # 创业板 20%
    assert risk.check_limit_band(28.09 * 1.19, 28.09, "300001.SZ").ok
    assert not risk.check_limit_band(28.09 * 1.21, 28.09, "300001.SZ").ok
    # 缺昨收 → 拒(fail-closed)
    assert not risk.check_limit_band(28.0, None, "600900.SH").ok


def test_slippage():
    assert risk.check_slippage(28.45, 28.45, 0.03).ok
    assert not risk.check_slippage(29.5, 28.45, 0.03).ok
    # 阈值为 0/None → 关闭该项
    assert risk.check_slippage(29.5, 28.45, 0).ok
    assert risk.check_slippage(29.5, 28.45, None).ok


# ---------------------------------------------------------------- 规模

def test_lot():
    assert risk.check_lot(100).ok
    assert risk.check_lot(0).ok is False
    assert not risk.check_lot(150).ok
    assert risk.check_lot(300, lot=100).ok


def test_order_amount():
    assert risk.check_order_amount(28.45, 1000, 50000).ok
    v = risk.check_order_amount(28.45, 2000, 50000)
    assert not v.ok and v.code == "AMOUNT_TOO_BIG"


def test_position_value():
    assert risk.check_position_value(10000, 0, 500000, 0.20).ok
    v = risk.check_position_value(110000, 0, 500000, 0.20)
    assert not v.ok and v.code == "POSITION_CAP"
    # 缺总资产 → 放行(由其他闸门兜底)
    assert risk.check_position_value(1e9, 0, None, 0.20).ok


def test_position_value_sell_side_always_allowed():
    """SELL 不受"加仓上限"约束 —— 卖出只会降低市值。

    旧实现不传 side: held_value=99500 卖 1000@28 → after=127500 > cap 100000
    → **把卖出腿拦死**, 文案还写"该标的市值将超上限"。真实账户里市值占比
    ≥ max_position_pct(约 20%)的票(如松发股份 ~20.5%)因此整片卖不出去 ——
    恰好是策略赖以赚钱的那条腿。
    """
    assert risk.check_position_value(28000, 99500, 500000, 0.20,
                                     side="SELL").ok
    # BUY 语义不变
    v = risk.check_position_value(28000, 99500, 500000, 0.20, side="BUY")
    assert not v.ok and v.code == "POSITION_CAP"
    # 不传 side → 按最保守的 BUY 语义(旧行为, fail-closed)
    v = risk.check_position_value(28000, 99500, 500000, 0.20)
    assert not v.ok and v.code == "POSITION_CAP"


def test_sellable_t_plus_1():
    assert risk.check_sellable("BUY", 9999, None).ok       # 买入不受限
    assert risk.check_sellable("SELL", 500, 500).ok
    v = risk.check_sellable("SELL", 600, 500)
    assert not v.ok and v.code == "SELLABLE_INSUFFICIENT"
    # 拿不到可卖量 → 拒
    v = risk.check_sellable("SELL", 100, None)
    assert not v.ok and v.code == "SELLABLE_UNKNOWN"


# ---------------------------------------------------------------- 频次/亏损

def test_daily_trades():
    assert risk.check_daily_trades(5, 20).ok
    v = risk.check_daily_trades(20, 20)
    assert not v.ok and v.code == "DAILY_TRADES_CAP"


def test_daily_loss():
    assert risk.check_daily_loss(-100, 3000).ok
    v = risk.check_daily_loss(-3000, 3000)
    assert not v.ok and v.code == "DAILY_LOSS_CAP"
    assert risk.check_daily_loss(500, 3000).ok


def test_net_exposure_strict():
    # 上限 0 = 严格归位: 买入不得超过已卖出
    assert risk.check_net_exposure("SELL", 1000, 0, 0, 0).ok
    v = risk.check_net_exposure("BUY", 100, 0, 0, 0)
    assert not v.ok and v.code == "NET_EXPOSURE"
    # 已卖 1000, 买回 1000 → 恰好归位
    assert risk.check_net_exposure("BUY", 1000, 1000, 0, 0).ok
    # 已卖 1000, 已买 500, 再买 500 → 归位
    assert risk.check_net_exposure("BUY", 500, 1000, 500, 0).ok
    # 已卖 1000, 已买 500, 再买 600 → 超 100
    v = risk.check_net_exposure("BUY", 600, 1000, 500, 0)
    assert not v.ok


def test_net_exposure_with_allowance():
    # 允许净买入 500 股
    assert risk.check_net_exposure("BUY", 500, 0, 0, 500).ok
    assert not risk.check_net_exposure("BUY", 501, 0, 0, 500).ok


# ---------------------------------------------------------------- 熔断

def test_circuit_breaker():
    cb = CircuitBreaker(3)
    assert cb.check().ok
    assert not cb.record_failure("e1")
    assert not cb.record_failure("e2")
    assert cb.record_failure("e3")          # 第 3 次触发
    v = cb.check()
    assert not v.ok and v.code == "CIRCUIT_BREAKER"
    cb.record_success()
    assert cb.check().ok and cb.count == 0


def test_circuit_breaker_default_3():
    cb = CircuitBreaker(None)
    assert cb.max_failures == 3
    # 非法值也退化到 3
    assert CircuitBreaker(0).max_failures == 3


# ---------------------------------------------------------------- 聚合

def _gate(**over):
    cfg = {"max_single_order_amount": 50000, "max_daily_trades": 20,
           "max_daily_loss": 3000, "max_price_deviation_pct": 0.05,
           "max_position_pct": 0.20, "max_net_buy_today_ratio": 0.0,
           "max_consecutive_failures": 3, "max_slippage_pct": 0.03}
    cfg.update(over)
    return RiskGate(cfg)


def _kwargs(**over):
    kw = dict(side="SELL", code="600900.SH", price=28.239, volume=500,
              ref_price=28.09, ladder_price_ref=28.239, last_close=28.09,
              hhmm="10:00", session_cfg=SESSION, lot=100,
              sold_today=0, bought_today=0, can_use_volume=5000,
              held_value=0.0, total_asset=500000.0, daily_trades=0,
              realized_pnl=0.0, max_net_buy_qty=0, enabled=True, now=MONDAY)
    kw.update(over)
    return kw


def test_gate_passes_valid_sell():
    assert _gate().check(**_kwargs()).ok


def test_gate_short_circuits_on_session():
    v = _gate().check(**_kwargs(hhmm="20:00"))
    assert not v.ok and v.code == "SESSION_CLOSED"


def test_gate_blocks_disabled_symbol():
    v = _gate().check(**_kwargs(enabled=False))
    assert not v.ok and v.code == "SYMBOL_DISABLED"


def test_gate_blocks_sell_without_position():
    v = _gate().check(**_kwargs(can_use_volume=0))
    assert not v.ok and v.code == "SELLABLE_INSUFFICIENT"


def test_gate_blocks_buy_beyond_net_exposure():
    v = _gate().check(**_kwargs(side="BUY", price=27.941, volume=500,
                                sold_today=0, bought_today=0))
    assert not v.ok and v.code == "NET_EXPOSURE"


def test_gate_allows_closing_buy():
    v = _gate().check(**_kwargs(side="BUY", price=27.941, volume=500,
                                sold_today=500, bought_today=0))
    assert v.ok


def test_gate_blocks_amount_cap():
    v = _gate(max_single_order_amount=1000).check(**_kwargs())
    assert not v.ok and v.code == "AMOUNT_TOO_BIG"


def test_gate_blocks_daily_trades_cap():
    v = _gate().check(**_kwargs(daily_trades=20))
    assert not v.ok and v.code == "DAILY_TRADES_CAP"


def test_gate_blocks_daily_loss_cap():
    v = _gate().check(**_kwargs(realized_pnl=-3000))
    assert not v.ok and v.code == "DAILY_LOSS_CAP"


def test_gate_breaker_blocks_everything():
    g = _gate()
    for _ in range(3):
        g.breaker.record_failure("boom")
    v = g.check(**_kwargs())
    assert not v.ok and v.code == "CIRCUIT_BREAKER"


def test_gate_blocks_limit_band_out():
    v = _gate().check(**_kwargs(price=28.09 * 1.15, ladder_price_ref=28.09 * 1.15))
    assert not v.ok and v.code == "BAND_OUT"


def test_gate_sell_not_blocked_by_position_cap():
    """聚合闸门里 SELL 也不再被 POSITION_CAP 拦(传 side 到位)。"""
    v = _gate().check(**_kwargs(held_value=99500.0, price=28.0, volume=1000))
    assert v.ok, "卖出腿被 POSITION_CAP 掐死: %s %s" % (v.code, v.msg)
    # 同一组数字换成 BUY → 仍须拦下
    v = _gate().check(**_kwargs(side="BUY", held_value=99500.0, price=28.0,
                                volume=1000, sold_today=1000,
                                can_use_volume=5000))
    assert not v.ok and v.code == "POSITION_CAP"


def test_gate_weekend_rejected():
    """周六给闸门 → SESSION_CLOSED(日历闸门必须进聚合链)。"""
    v = _gate().check(**_kwargs(now=SATURDAY))
    assert not v.ok and v.code == "SESSION_CLOSED"
