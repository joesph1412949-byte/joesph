# -*- coding: utf-8 -*-
"""perf_store 单元测试 — 存档/回填/统计, 全离线(注入假 K线源)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np

from perf_store import PerfStore


def make_kline(closes):
    """构造含 close 列的日线 DataFrame(升序, 最后一行=最新)。"""
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=len(closes), freq="B"),
                         "close": closes})


def _cand(code, grade, composite):
    return {"code": code, "name": code,
            "scores": {"grade": grade, "composite": composite}}


def test_archive_daily_writes_and_reads(tmp_path):
    store = PerfStore(perf_dir=tmp_path)
    path = store.archive_daily([_cand("000001.SZ", "A", 6.0)], d=None)
    assert path.exists()
    rec = store.get_day(path.stem)
    assert rec["date"] == path.stem
    assert rec["settled"] is False
    assert rec["entries"][0]["code"] == "000001.SZ"
    assert rec["entries"][0]["grade"] == "A"


def test_list_days_sorted(tmp_path):
    store = PerfStore(perf_dir=tmp_path)
    store.archive_daily([_cand("1", "A", 6.0)], d=__import__("datetime").date(2026, 7, 30))
    store.archive_daily([_cand("2", "B", 5.0)], d=__import__("datetime").date(2026, 7, 29))
    assert store.list_days() == ["20260729", "20260730"]


def test_backfill_computes_n_day_return(tmp_path):
    # K线: 60 根, 从 10 涨到 16 → 每根约 +10%; 5 日收益应为正
    closes = list(np.linspace(10, 16, 60))
    src = lambda code, kdays: make_kline(closes)
    store = PerfStore(perf_dir=tmp_path, kline_source=src)
    store.archive_daily([_cand("000001.SZ", "A", 6.0)], d=None)
    res = store.backfill(days=5)
    assert res["settled"] == 1
    rec = store.get_day(store.list_days()[0])
    assert rec["settled"] is True
    e = rec["entries"][0]
    assert e["return_pct"] is not None
    assert e["entry_close"] == round(closes[-6], 4)   # 5 日前收盘 = 倒数第 6 根
    assert e["exit_close"] == round(closes[-1], 4)    # 最新收盘
    assert e["return_pct"] > 0


def test_backfill_skips_when_kline_too_short(tmp_path):
    src = lambda code, kdays: make_kline([10.0, 10.5])   # 不足 5+1 根
    store = PerfStore(perf_dir=tmp_path, kline_source=src)
    store.archive_daily([_cand("000001.SZ", "A", 6.0)], d=None)
    res = store.backfill(days=5)
    assert res["settled"] == 0
    rec = store.get_day(store.list_days()[0])
    assert rec["settled"] is False


def test_backfill_no_source_skips(tmp_path):
    store = PerfStore(perf_dir=tmp_path)   # kline_source=None
    store.archive_daily([_cand("000001.SZ", "A", 6.0)], d=None)
    res = store.backfill(days=5)
    assert res["settled"] == 0
    assert "no kline_source" in res["errors"]


def test_summary_by_grade(tmp_path):
    # 2 只 A: 000001 涨 + 000002 跌 → 胜率 50%
    # 1 只 B: 600000 涨 → 胜率 100%
    closes_up = list(np.linspace(10, 11, 6))       # 5 日上涨
    closes_dn = list(np.linspace(10, 9.5, 6))      # 5 日下跌
    src = lambda code, kdays: make_kline(
        closes_up if code in ("000001.SZ", "600000.SH") else closes_dn)
    store = PerfStore(perf_dir=tmp_path, kline_source=src)
    store.archive_daily([_cand("000001.SZ", "A", 6.0),
                         _cand("000002.SZ", "A", 6.0)], d=None)
    store.archive_daily([_cand("600000.SH", "B", 5.0)], d=__import__("datetime").date(2026, 7, 29))
    store.backfill(days=5)
    s = store.summary()
    by = {x["grade"]: x for x in s}
    assert by["A"]["total"] == 2
    assert by["A"]["settled"] == 2
    assert by["A"]["win_rate"] == 0.5
    assert by["B"]["win_rate"] == 1.0
    assert by["B"]["avg_return"] is not None


def test_summary_unsolved_has_null_stats(tmp_path):
    store = PerfStore(perf_dir=tmp_path)   # 无 kline_source → 永不回填
    store.archive_daily([_cand("000001.SZ", "A", 6.0)], d=None)
    s = store.summary()
    assert s[0]["grade"] == "A"
    assert s[0]["total"] == 1
    assert s[0]["settled"] == 0
    assert s[0]["win_rate"] is None
