# -*- coding: utf-8 -*-
"""backtest 单元测试 — 全离线: 注入假涨停池/假K线, 验证选股规则与统计。"""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest import BacktestEngine


def _pool(*items):
    """items: (code, boards, theme)"""
    return [{"code": c, "boards": b, "theme": t} for c, b, t in items]


def _kline(start, closes):
    """按自然日递增构造 [(date_str, close), ...]; start 为选股日(第一根)。"""
    return [(start + __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(len(closes))], closes


def _feed(zt_by_date, kline_by_code):
    def zf(d):
        return zt_by_date.get(d, [])
    def kf(code):
        dates, closes = kline_by_code.get(code, ([], []))
        return list(zip(dates, closes))
    return BacktestEngine(zt_feed=zf, kline_feed=kf)


def test_pick_day_requires_environment_gate():
    # 涨停家数 < min_limit_count → 不选股
    eng = BacktestEngine(zt_feed=lambda d: [], kline_feed=lambda c: [])
    picked = eng._pick_day(_pool(("000001", 1, "机器人")),
                           {"min_limit_count": 20, "max_picks": 3})
    assert picked == []


def test_pick_day_top_theme_and_boards():
    # 环境达标: 机器人(3家, 最高5板) > AI算力(2家) → 选机器人内连板最高的2只
    pool = _pool(("000001", 1, "机器人"), ("000002", 3, "机器人"),
                 ("000003", 5, "机器人"),
                 ("000010", 2, "AI算力"), ("000011", 1, "AI算力"),
                 ("000020", 4, "低空经济"))
    eng = BacktestEngine(zt_feed=lambda d: [], kline_feed=lambda c: [])
    picked = eng._pick_day(pool, {"min_limit_count": 3, "max_picks": 2})
    codes = [p["code"] for p in picked]
    assert codes == ["000003", "000002"]     # 连板 5 > 3


def test_trade_return_computes_hold_days():
    kline = [("2026-07-01", 10.0), ("2026-07-02", 10.5),
             ("2026-07-03", 11.0), ("2026-07-06", 11.55),
             ("2026-07-07", 12.0)]
    tr = BacktestEngine._trade_return(kline, "2026-07-01", 3)
    assert tr is not None
    assert tr[0] == 10.0
    assert tr[1] == 11.55      # 第 3 根(2026-07-06)
    assert abs(tr[2] - 15.5) < 0.01


def test_trade_return_insufficient_kline():
    kline = [("2026-07-01", 10.0), ("2026-07-02", 10.5)]
    assert BacktestEngine._trade_return(kline, "2026-07-01", 3) is None
    assert BacktestEngine._trade_return([], "2026-07-01", 3) is None


def test_run_full_backtest_report():
    # 07-01: 机器人主线(2家) → 选中 000003(5板)、000001(1板)
    # 07-02: AI算力主线(2家) → 选中 000010(2板)、000011(1板)  → 共 4 笔
    start = date(2026, 7, 1)
    end = date(2026, 7, 2)

    def zf(d):
        if d == "20260701":
            return _pool(("000001", 1, "机器人"), ("000003", 5, "机器人"),
                         ("000010", 2, "AI算力"))
        if d == "20260702":
            return _pool(("000010", 2, "AI算力"), ("000011", 1, "AI算力"),
                         ("000020", 3, "低空经济"))
        return []

    def kf(code):
        # 所有股票: 选股日 10.0, 之后每天 +2% → 5 日后 +10%
        closes = [10.0 * (1.02 ** i) for i in range(8)]
        dates = [(start + __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(8)]
        return list(zip(dates, closes))

    eng = BacktestEngine(zt_feed=zf, kline_feed=kf)
    rep = eng.run(start, end, params={"min_limit_count": 2, "max_picks": 2,
                                      "hold_days": 5})
    assert rep["trades"] == 4
    assert rep["trading_days"] == 2
    assert rep["win_rate"] == 1.0
    assert rep["avg_return_pct"] > 0
    assert rep["total_return_pct"] > 0
    assert rep["max_drawdown_pct"] is not None


def test_run_empty_report():
    eng = BacktestEngine(zt_feed=lambda d: [], kline_feed=lambda c: [])
    rep = eng.run(date(2026, 7, 1), date(2026, 7, 2),
                  params={"min_limit_count": 20, "max_picks": 3, "hold_days": 5})
    assert rep["trades"] == 0
    assert rep["win_rate"] is None


def test_compare_params_returns_grid_rows():
    start = date(2026, 7, 1)
    end = date(2026, 7, 2)

    def zf(d):
        return _pool(("000001", 1, "机器人"), ("000003", 5, "机器人"),
                     ("000010", 2, "AI算力"))

    def kf(code):
        closes = [10.0 * (1.01 ** i) for i in range(8)]
        dates = [(start + __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(8)]
        return list(zip(dates, closes))

    eng = BacktestEngine(zt_feed=zf, kline_feed=kf)
    grid = [{"min_limit_count": 1, "max_picks": 1, "hold_days": 5},
            {"min_limit_count": 99, "max_picks": 1, "hold_days": 5}]
    rows = eng.compare_params(start, end, grid)
    assert len(rows) == 2
    assert rows[0]["trades"] > 0      # 门槛低 → 有交易
    assert rows[1]["trades"] == 0     # 门槛极高 → 无交易
