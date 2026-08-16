# -*- coding: utf-8 -*-
"""zt_history 单元测试 — 涨停判断/连板数/缓存, 全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.zt_history import _limit_ratio, _limit_up_price, qmt_zt_feed


def test_limit_ratio_by_board():
    assert _limit_ratio("600000") == 0.10    # 主板
    assert _limit_ratio("000001") == 0.10
    assert _limit_ratio("300001") == 0.20    # 创业板
    assert _limit_ratio("688001") == 0.20    # 科创板
    assert _limit_ratio("830799") == 0.30    # 北交所
    assert _limit_ratio("430001") == 0.30


def test_limit_up_price_rounds_to_cent():
    # A股涨停价四舍五入到分
    assert _limit_up_price(10.0, 0.10) == 11.0
    assert _limit_up_price(5.24, 0.10) == 5.76   # 5.764 → 5.76
    assert _limit_up_price(6.34, 0.10) == 6.97   # 6.974 → 6.97


def _cache_with(dates, closes, pre):
    """构造单股缓存片段。dates: ["2026-07-01", ...] 升序。"""
    return {"600000.SH": {"dates": dates, "close": closes, "pre": pre}}


def test_qmt_zt_feed_detects_limit_up():
    # 7-02: close 11.0 = round(10.0×1.1) → 涨停
    cache = _cache_with(
        ["2026-07-01", "2026-07-02"], [10.0, 11.0], [10.0, 10.0])
    pool = qmt_zt_feed("20260702", cache)
    assert len(pool) == 1
    assert pool[0]["code"] == "600000.SH"
    assert pool[0]["boards"] == 1


def test_qmt_zt_feed_ignores_non_limit():
    # 7-02: close 10.5 < 11.0 → 非涨停
    cache = _cache_with(
        ["2026-07-01", "2026-07-02"], [10.0, 10.5], [10.0, 10.0])
    assert qmt_zt_feed("20260702", cache) == []


def test_qmt_zt_feed_counts_consecutive_boards():
    # 7-01, 7-02, 7-03 连续涨停 → boards=3
    # pre 序列: 7-01 pre=9.0(→涨停价9.9, close 10.0已涨停)
    cache = _cache_with(
        ["2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03"],
        [9.0, 10.0, 11.0, 12.1],
        [9.0, 9.0, 10.0, 11.0])
    pool = qmt_zt_feed("20260703", cache)
    assert len(pool) == 1
    assert pool[0]["boards"] == 3


def test_qmt_zt_feed_no_cache_returns_empty():
    assert qmt_zt_feed("20260701", {}) == []
    assert qmt_zt_feed("20260701", None) == []


def test_qmt_zt_feed_unknown_date_returns_empty():
    cache = _cache_with(["2026-07-01"], [10.0], [10.0])
    assert qmt_zt_feed("20260799", cache) == []


def test_qmt_zt_feed_st_10pct_not_flagged_as_20pct():
    # 简化: 主板非ST 10% 涨停, 不因容差误判(9.5%不触发)
    cache = _cache_with(
        ["2026-07-01", "2026-07-02"], [10.0, 10.95], [10.0, 10.0])
    assert qmt_zt_feed("20260702", cache) == []   # +9.5% 未到 10%


def test_gem_20pct_limit():
    # 创业板 300xxx: 20% 涨停价 = 12.0, close 12.0 → 涨停
    cache = {"300001.SZ": {"dates": ["2026-07-01", "2026-07-02"],
                           "close": [10.0, 12.0], "pre": [10.0, 10.0]}}
    pool = qmt_zt_feed("20260702", cache)
    assert len(pool) == 1
    # 主板 600000: 10% 涨停价 = 11.0, close 12.0 是+20% —— 真实市场不会出现,
    # 但算法按"≥涨停价"判涨停(真实数据中主板不会 +20%, 无实际影响)
    # 用合理的边界验证: 主板 close 11.0(=涨停价) → 涨停; 10.99 → 不涨停
    cache2 = {"600000.SH": {"dates": ["2026-07-01", "2026-07-02"],
                            "close": [10.0, 11.0], "pre": [10.0, 10.0]}}
    assert len(qmt_zt_feed("20260702", cache2)) == 1
    cache3 = {"600000.SH": {"dates": ["2026-07-01", "2026-07-02"],
                            "close": [10.0, 10.99], "pre": [10.0, 10.0]}}
    assert qmt_zt_feed("20260702", cache3) == []
