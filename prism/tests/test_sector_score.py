# -*- coding: utf-8 -*-
"""板块综合评分模块测试 — 全离线, 只测纯计算。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism import sector_score as ss


def _sector_rec(closes, amounts=None, dates=None):
    n = len(closes)
    rec = {"dates": dates or ["2026-07-%02d" % (i + 1) for i in range(n)],
           "close": closes}
    if amounts is not None:
        rec["amount"] = amounts
    return rec


# ---------------- 动量分 ----------------

def test_momentum_full_when_r5_6pct_and_r10_10pct():
    # 11根: 首根100 → 末根110+ (r10=10.5% 满分, r5=6.25%>6% 满分)
    # (brief 原稿 closes[-6]=closes[-7]*1.06 只抬高窗口基点, 实际 r5≈0.14%,
    #  与注释"保证 r5≥6%"矛盾 → 改为重写近6根, 真正使 r5≥6%)
    closes = [100.0 * (1.01 ** i) for i in range(11)]   # r10≈10.5% 满分
    closes[-6:] = [104.0, 105.0, 106.0, 107.0, 108.0, 110.5]  # 近5日+6.25% → r5 满分
    assert ss._momentum_part(_sector_rec(closes)) == 100.0


def test_momentum_flat_is_zero():
    closes = [100.0] * 11
    assert ss._momentum_part(_sector_rec(closes)) == 0.0


def test_momentum_insufficient_data_is_none():
    assert ss._momentum_part(_sector_rec([100.0, 101.0])) is None


# ---------------- 资金流分 ----------------

def test_flow_full_at_5yi_inflow():
    rec = {"main_net_in": [5e8] * 5}
    assert ss._flow_part(rec) == 100.0


def test_flow_zero_is_50():
    assert ss._flow_part({"main_net_in": [0.0] * 5}) == 50.0


def test_flow_minus_5yi_is_zero():
    assert ss._flow_part({"main_net_in": [-1e8] * 5}) == 0.0   # -5亿→0


def test_flow_missing_is_none():
    assert ss._flow_part({}) is None
    assert ss._flow_part({"main_net_in": [None] * 5}) is None


# ---------------- 拥挤度分(反向) ----------------

def test_crowding_shrink_volume_full():
    rec = _sector_rec([100.0] * 12, amounts=[1e9] * 12)
    assert ss._crowding_part(rec) == 100.0        # ratio=1 → 100


def test_crowding_2x_is_60():
    amounts = [1e9] * 12
    amounts[-5:] = [2e9] * 5
    rec = _sector_rec([100.0] * 12, amounts=amounts)
    assert ss._crowding_part(rec) == 60.0         # ratio=2 → 100-40=60


def test_crowding_3_5x_is_zero():
    amounts = [1e9] * 12
    amounts[-5:] = [3.5e9] * 5
    rec = _sector_rec([100.0] * 12, amounts=amounts)
    assert ss._crowding_part(rec) == 0.0


def test_crowding_needs_amount_history():
    rec = _sector_rec([100.0] * 12, amounts=[1e9] * 3)
    assert ss._crowding_part(rec) is None         # <10日 → 缺失


# ---------------- 宏观分 ----------------

def _glob(ndx=None, us10y=None, vix=None):
    g = {}
    if ndx:
        g["NDX"] = {"dates": ["d"] * len(ndx), "close": ndx}
    if us10y:
        g["US10Y"] = {"dates": ["d"] * len(us10y), "close": us10y}
    if vix:
        g["VIX"] = {"dates": ["d"] * len(vix), "close": vix}
    return g


def test_macro_ndx_up1pct_full():
    assert ss._macro_part(_glob(ndx=[100.0, 101.0])) == 100.0


def test_macro_ndx_down1pct_zero():
    assert ss._macro_part(_glob(ndx=[100.0, 99.0])) == 0.0


def test_macro_us10y_percent_point_not_times_100():
    # 21天: 前20天4.0, 末天5.3 → 变化1.3个百分点 → 0 分(若误×100 会异常)
    us = [4.0] * 20 + [5.3]
    assert ss._macro_part(_glob(us10y=us)) == 0.0
    us2 = [4.0] * 21
    assert ss._macro_part(_glob(us10y=us2)) == 100.0   # 变化0 ≤0.3 → 满分


def test_macro_vix_levels():
    assert ss._macro_part(_glob(vix=[15.0])) == 100.0
    assert ss._macro_part(_glob(vix=[25.0])) == 0.0


def test_macro_all_missing_is_none():
    assert ss._macro_part({}) is None


# ---------------- compute_scores 综合与降级 ----------------

def test_compute_scores_renormalizes_missing_flow():
    # 板块: 动量满分(r5=r10=10%), 拥挤满分(平量), 宏观满分(纳指+1%),
    # 资金流缺失 → 权重归一 (0.35+0.30+0.10)=0.75 → score=100
    # (brief 原稿 1.011**i 的 r5≈5.6%<6% → 动量仅≈96.9 分, 与注释"动量满分"
    #  矛盾 → 改用 Task2 预检修正同款数据: 近6根 100→110)
    closes = [100.0] * 6 + [106.0, 107.0, 108.0, 109.0, 110.0]
    mkt = {"sector": {"801110": _sector_rec(closes, amounts=[1e9] * 11)},
           "global": _glob(ndx=[100.0, 101.0])}
    out = ss.compute_scores(mkt)
    assert out["801110"]["score"] == 100.0
    assert out["801110"]["parts"]["flow"] is None


def test_compute_scores_weighted_mix():
    # 动量100 拥挤0(近5日3.5倍) 宏观缺失(剔除, 设计§3.5) 资金流缺失
    # → 权重归一 (0.35+0.30)=0.65 → (35+0)/65 = 53.8
    # (brief 原稿三处笔误: 1.011**i 动量不满分; 3.5e9 只写 4 根近5日均≠3.5倍;
    #  "无global→宏观0 计入 35/75=46.7" 违反设计§3.5 缺失剔除归一化)
    closes = [100.0] * 6 + [106.0, 107.0, 108.0, 109.0, 110.0]
    amounts = [1e9] * 6 + [3.5e9] * 5       # 近5日3.5倍 → 拥挤0分
    mkt = {"sector": {"801110": _sector_rec(closes, amounts=amounts)}}
    out = ss.compute_scores(mkt)
    assert out["801110"]["score"] == 53.8
    assert out["801110"]["parts"]["macro"] is None


def test_compute_scores_no_data_sector_absent():
    out = ss.compute_scores({"sector": {"801999": _sector_rec([])}})
    assert "801999" not in out              # fail-closed: 无K线 → 无评分
    assert ss.compute_scores({}) == {}
    assert ss.compute_scores(None) == {}


# ---------------- load_config / position_multiplier ----------------

def test_load_config_defaults_disabled():
    cfg = ss.load_config({})
    assert cfg == {"enabled": False, "threshold": 75.0,
                   "step": 0.05, "cap_ratio": None}


def test_load_config_full_block():
    cfg = ss.load_config({"sector_score": {
        "enabled": True, "threshold": 80,
        "position": {"step": 0.02, "cap_ratio": 0.5}}})
    assert cfg == {"enabled": True, "threshold": 80.0,
                   "step": 0.02, "cap_ratio": 0.5}


def test_position_multiplier_ramp():
    cfg = {"enabled": True, "threshold": 75.0, "step": 0.05,
           "cap_ratio": None}
    # (brief 原稿 76→1.05 与设计§4.2 线性公式矛盾: (76-75)/10×0.05=0.005 → 1.005;
    #  85/100 两例原稿即按线性公式注释)
    assert ss.position_multiplier(76, cfg, 0.30) == 1.005  # 线性: +0.5%
    assert ss.position_multiplier(85, cfg, 0.30) == 1.05   # (85-75)/10=1 步
    assert ss.position_multiplier(100, cfg, 0.30) == 1.125
    assert ss.position_multiplier(75, cfg, 0.30) == 0.0    # 严格大于
    assert ss.position_multiplier(74, cfg, 0.30) == 0.0
    assert ss.position_multiplier(None, cfg, 0.30) == 0.0


def test_position_multiplier_cap_binds():
    cfg = {"enabled": True, "threshold": 75.0, "step": 0.05,
           "cap_ratio": 0.21}
    # base 0.20 → 相对乘数封顶 0.21/0.20 = 1.05
    assert ss.position_multiplier(100, cfg, 0.20) == 1.05
