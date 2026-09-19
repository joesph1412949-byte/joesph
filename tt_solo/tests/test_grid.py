# -*- coding: utf-8 -*-
"""网格引擎纯逻辑测试。"""
import pytest

from ttcore import grid


# ---------------------------------------------------------------- sigma

def test_daily_sigma_zero_vol_returns_none():
    # 恒定对数收益 → 标准差为 0 → 视为"无波动信息", 返回 None(fail-closed)
    closes = [100.0 * (1.01 ** i) for i in range(30)]
    assert grid.daily_sigma(closes, window=60) is None


def test_daily_sigma_insufficient():
    assert grid.daily_sigma([1.0, 2.0]) is None
    assert grid.daily_sigma([]) is None
    assert grid.daily_sigma(None) is None


def test_daily_sigma_rejects_nonpositive():
    assert grid.daily_sigma([100.0, 0.0, 99.0, 98.0, 97.0]) is None


def test_daily_sigma_positive_for_noisy():
    import random
    random.seed(7)
    closes = [100.0]
    for _ in range(60):
        closes.append(closes[-1] * (1 + random.uniform(-0.02, 0.02)))
    sd = grid.daily_sigma(closes, window=60)
    assert sd is not None and 0.005 < sd < 0.05


# ---------------------------------------------------------------- trend

def test_trend_degree_monotonic_is_one():
    closes = list(range(100, 121))          # 单边
    td = grid.trend_degree(closes, 20)
    assert td is not None and td > 0.99


def test_trend_degree_oscillating_is_low():
    closes = [100.0 + (1 if i % 2 else -1) for i in range(21)]
    td = grid.trend_degree(closes, 20)
    assert td is not None and td < 0.15


# ---------------------------------------------------------------- ma

def test_ma():
    assert grid.ma([1, 2, 3, 4, 5], 5) == 3.0
    assert grid.ma([1, 2], 5) is None


# ---------------------------------------------------------------- switch

SW = {"dev_max_pct": 4.0, "slope_max_pct": 0.3, "r20_max_pct": 8.0}


def test_switch_enabled_when_flat():
    # 价格贴近均线、均线走平 → 启用
    assert grid.switch_state(28.2, 28.0, 28.0, 1.2, SW) == grid.ENABLED


def test_switch_disabled_on_trend_accel():
    # 偏离 +4.9%, 均线上斜 +2.75%, 近20日 +10.18% → 停做(松发场景)
    st = grid.switch_state(197.5, 188.3, 183.3, 10.18, SW)
    assert st == grid.DISABLED


def test_switch_half_above_rising_ma():
    # 价在均线上方 + 均线上斜, 但未同时满足另外两条 → 半量
    st = grid.switch_state(30.0, 29.0, 28.5, 3.0, SW)
    assert st == grid.HALF


def test_switch_disabled_on_data_missing():
    assert grid.switch_state(None, 28, 28, 1, SW) == grid.DISABLED
    assert grid.switch_state(28, None, 28, 1, SW) == grid.DISABLED
    assert grid.switch_state(28, 28, 28, None, SW) == grid.DISABLED
    assert grid.switch_state(0, 28, 28, 1, SW) == grid.DISABLED


def test_switch_disabled_on_downtrend():
    # 深度跌破均线且均线下斜 → 不越跌越买
    st = grid.switch_state(25.0, 29.0, 30.0, -12.0, SW)
    assert st == grid.DISABLED


# ---------------------------------------------------------------- band

def test_band_from_pct():
    assert grid.band_of({"band_pct": 0.53}, 0.009) == pytest.approx(0.0053)


def test_band_from_sigma():
    b = grid.band_of({}, sigma=0.01, band_k=1.5, band_mode="sigma")
    assert b == pytest.approx(0.015)


def test_band_none_when_unknown():
    assert grid.band_of({}, None, 1.0, "sigma") is None
    # 过窄被成本吃掉 → 拒绝
    assert grid.band_of({}, sigma=0.0001, band_k=1.0, band_mode="sigma") is None
    # 过宽 → 拒绝
    assert grid.band_of({"band_pct": 30.0}, None) is None


# ---------------------------------------------------------------- ladder

def test_build_ladder_prices():
    L = grid.build_ladder(100.0, 0.01, 3)
    assert L["sell"] == [101.0, 102.0, 103.0]
    assert L["buy"] == [99.0, 98.0, 97.0]
    assert L["ref"] == 100.0


def test_build_ladder_rejects_bad_input():
    assert grid.build_ladder(0, 0.01, 3) is None
    assert grid.build_ladder(100, 0, 3) is None
    assert grid.build_ladder(100, 0.01, 0) is None
    assert grid.build_ladder("x", 0.01, 3) is None


def test_build_ladder_rejects_negative_buy():
    # band 过大导致最低买档 ≤ 0 → 拒绝
    assert grid.build_ladder(100.0, 0.4, 3) is None


# ---------------------------------------------------------------- crossing

def test_crossed_sell():
    L = grid.build_ladder(100.0, 0.01, 5)
    assert grid.crossed_sell(L, 99.0) == 0
    assert grid.crossed_sell(L, 101.0) == 1
    assert grid.crossed_sell(L, 103.0) == 3
    assert grid.crossed_sell(L, 999.0) == 5


def test_crossed_buy():
    L = grid.build_ladder(100.0, 0.01, 5)
    assert grid.crossed_buy(L, 101.0) == 0
    assert grid.crossed_buy(L, 99.0) == 1
    assert grid.crossed_buy(L, 97.0) == 3
    assert grid.crossed_buy(L, 1.0) == 5


def test_crossed_handles_bad_input():
    L = grid.build_ladder(100.0, 0.01, 3)
    assert grid.crossed_sell(L, None) == 0
    assert grid.crossed_sell(None, 100) == 0
    assert grid.crossed_buy(L, "abc") == 0


def test_ladder_price():
    L = grid.build_ladder(100.0, 0.01, 3)
    assert grid.ladder_price(L, "SELL", 1) == 101.0
    assert grid.ladder_price(L, "SELL", 3) == 103.0
    assert grid.ladder_price(L, "BUY", 2) == 98.0
    assert grid.ladder_price(L, "SELL", 4) is None
    assert grid.ladder_price(L, "SELL", 0) is None


def test_half_scale():
    assert grid.half_scale(grid.ENABLED) == 1.0
    assert grid.half_scale(grid.HALF) == 0.5
    assert grid.half_scale(grid.DISABLED) == 0.0
    assert grid.half_scale("whatever") == 0.0
