# -*- coding: utf-8 -*-
"""zt_history 单元测试 — 涨停判断/连板数/缓存/刷新/陈旧保护, 全离线。"""
import logging
import pickle
import sys
import time
import types
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd

import prism.zt_history as zt
from prism.zt_history import (_limit_ratio, _limit_up_price, qmt_zt_feed,
                              _run_with_timeout)

import pytest
import time


def _feed(date_str, cache):
    """测试助手: 强制走缓存全表扫描分支(index 传空 dict, 不读磁盘索引)。
    cache 为 None 时传 {} → 空缓存返回空列表(不读磁盘缓存文件)。"""
    return qmt_zt_feed(date_str, cache=cache or {}, index={})


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
    pool = _feed("20260702", cache)
    assert len(pool) == 1
    assert pool[0]["code"] == "600000.SH"
    assert pool[0]["boards"] == 1


def test_qmt_zt_feed_ignores_non_limit():
    # 7-02: close 10.5 < 11.0 → 非涨停
    cache = _cache_with(
        ["2026-07-01", "2026-07-02"], [10.0, 10.5], [10.0, 10.0])
    assert _feed("20260702", cache) == []


def test_qmt_zt_feed_counts_consecutive_boards():
    # 7-01, 7-02, 7-03 连续涨停 → boards=3
    # pre 序列: 7-01 pre=9.0(→涨停价9.9, close 10.0已涨停)
    cache = _cache_with(
        ["2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03"],
        [9.0, 10.0, 11.0, 12.1],
        [9.0, 9.0, 10.0, 11.0])
    pool = _feed("20260703", cache)
    assert len(pool) == 1
    assert pool[0]["boards"] == 3


def test_qmt_zt_feed_no_cache_returns_empty():
    assert _feed("20260701", {}) == []
    assert _feed("20260701", None) == []


def test_qmt_zt_feed_unknown_date_returns_empty():
    cache = _cache_with(["2026-07-01"], [10.0], [10.0])
    assert _feed("20260799", cache) == []


def test_qmt_zt_feed_st_10pct_not_flagged_as_20pct():
    # 简化: 主板非ST 10% 涨停, 不因容差误判(9.5%不触发)
    cache = _cache_with(
        ["2026-07-01", "2026-07-02"], [10.0, 10.95], [10.0, 10.0])
    assert _feed("20260702", cache) == []   # +9.5% 未到 10%


def test_gem_20pct_limit():
    # 创业板 300xxx: 20% 涨停价 = 12.0, close 12.0 → 涨停
    cache = {"300001.SZ": {"dates": ["2026-07-01", "2026-07-02"],
                           "close": [10.0, 12.0], "pre": [10.0, 10.0]}}
    pool = _feed("20260702", cache)
    assert len(pool) == 1
    # 主板 600000: 10% 涨停价 = 11.0, close 12.0 是+20% —— 真实市场不会出现,
    # 但算法按"≥涨停价"判涨停(真实数据中主板不会 +20%, 无实际影响)
    # 用合理的边界验证: 主板 close 11.0(=涨停价) → 涨停; 10.99 → 不涨停
    cache2 = {"600000.SH": {"dates": ["2026-07-01", "2026-07-02"],
                            "close": [10.0, 11.0], "pre": [10.0, 10.0]}}
    assert len(_feed("20260702", cache2)) == 1
    cache3 = {"600000.SH": {"dates": ["2026-07-01", "2026-07-02"],
                            "close": [10.0, 10.99], "pre": [10.0, 10.0]}}
    assert _feed("20260702", cache3) == []


# ---------------- 看门狗(QMT下载偶发永久挂起, 超时跳批) ----------------

def test_run_with_timeout_returns_result():
    assert _run_with_timeout(lambda a, b: a + b, (1, 2)) == 3


def test_run_with_timeout_propagates_error():
    def boom():
        raise ValueError("boom")
    with pytest.raises(ValueError):
        _run_with_timeout(boom, timeout=1.0)


def test_run_with_timeout_times_out():
    def hang():
        time.sleep(2.0)
    with pytest.raises(TimeoutError):
        _run_with_timeout(hang, timeout=0.2)


# ---------------- refresh_cache(存量尾部续传) ----------------

class _FakeXtData:
    """假 xtdata: 内存返回预置 DataFrame, 记录下载参数 — 绝不触网/真 QMT。"""

    def __init__(self, dfs, read_fail=False):
        self.dfs = dfs
        self.downloads = []          # [(chunk, start_time)]
        self.read_fail = read_fail

    def get_stock_list_in_sector(self, name):
        return sorted(self.dfs)

    def download_history_data2(self, chunk, period, start_time="", end_time=""):
        self.downloads.append((list(chunk), start_time))
        return 0

    def get_market_data_ex(self, lst, chunk, period="", start_time="",
                           end_time="", count=0):
        if self.read_fail:
            raise RuntimeError("读取炸")
        return {c: self.dfs[c] for c in chunk if c in self.dfs}


def _install_fake_xtdata(monkeypatch, fake):
    mod = types.ModuleType("xtquant")
    mod.xtdata = fake
    monkeypatch.setitem(sys.modules, "xtquant", mod)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake)


def _ms(d):
    """date → epoch毫秒(本地时区往返自洽, 与 paper_daemon 测试同套路)。"""
    return int(datetime(d.year, d.month, d.day, 15).timestamp() * 1000)


def _df(days, closes):
    """日期/收盘 → xtdata 日K DataFrame(time 毫秒 + close/preClose 列)。"""
    pre = [closes[0]] + closes[:-1]
    return pd.DataFrame({"time": [_ms(d) for d in days],
                         "close": closes, "preClose": pre})


def _dstr(d):
    return d.strftime("%Y-%m-%d")


@pytest.fixture()
def zt_tmp_paths(tmp_path, monkeypatch):
    """缓存/索引路径指到 tmp — 绝不碰真实 .pkl。"""
    monkeypatch.setattr(zt, "CACHE_PATH", tmp_path / ".zt_history_cache.pkl")
    monkeypatch.setattr(zt, "INDEX_PATH", tmp_path / ".zt_history_index.pkl")
    return tmp_path


def test_refresh_cache_merges_tail_replacing_old(zt_tmp_paths, monkeypatch):
    """合并规则: 旧缓存 <START 头部保留, >=START 尾部被新拉记录整体替换,
    结果按日期升序; download start_time 用 8 位日期串。"""
    today = date.today()
    old_days = [today - timedelta(days=100), today - timedelta(days=95),
                today - timedelta(days=30), today - timedelta(days=10)]
    old_cache = {"600000.SH": {
        "dates": [_dstr(d) for d in old_days],
        "close": [9.0, 9.5, 10.0, 11.0],
        "pre": [9.0, 9.0, 9.5, 10.0]}}
    zt.CACHE_PATH.write_bytes(pickle.dumps(old_cache, protocol=4))
    new_days = [today - timedelta(days=30), today - timedelta(days=10),
                today - timedelta(days=1)]
    fake = _FakeXtData({"600000.SH": _df(new_days, [10.5, 11.2, 12.32])})
    _install_fake_xtdata(monkeypatch, fake)

    r = zt.refresh_cache(days=90)

    merged = pickle.loads(zt.CACHE_PATH.read_bytes())["600000.SH"]
    assert merged["dates"] == [_dstr(old_days[0]), _dstr(old_days[1]),
                               _dstr(new_days[0]), _dstr(new_days[1]),
                               _dstr(new_days[2])]
    assert merged["close"] == [9.0, 9.5, 10.5, 11.2, 12.32]
    assert merged["pre"] == [9.0, 9.0, 10.5, 10.5, 11.2]
    assert r["codes"] == 1
    assert r["last_date"] == _dstr(new_days[2])
    # start_time = 今天-90天 的 8 位日期串
    assert fake.downloads, "download_history_data2 未被调用"
    assert fake.downloads[0][1] == (today - timedelta(days=90)).strftime("%Y%m%d")
    # 原子写: 不留 tmp 文件
    assert not (zt_tmp_paths / ".zt_history_cache.pkl.tmp").exists()


def test_refresh_cache_new_stock_gets_tail_only(zt_tmp_paths, monkeypatch):
    """新股(缓存里没有的代码)也进 — 但只有 START 以来的尾部
    (全量 400 根仍走 build_cache, docstring 声明的限制)。"""
    today = date.today()
    old_cache = {"600000.SH": {
        "dates": ["2026-01-05", "2026-01-06"], "close": [9.0, 9.9],
        "pre": [9.0, 9.0]}}
    zt.CACHE_PATH.write_bytes(pickle.dumps(old_cache, protocol=4))
    new_days = [today - timedelta(days=2), today - timedelta(days=1)]
    fake = _FakeXtData({"600000.SH": _df(new_days, [9.5, 10.45]),
                        "301999.SZ": _df(new_days, [20.0, 24.0])})
    _install_fake_xtdata(monkeypatch, fake)

    r = zt.refresh_cache(days=90)

    cache = pickle.loads(zt.CACHE_PATH.read_bytes())
    assert r["codes"] == 2
    assert cache["301999.SZ"]["dates"] == [_dstr(d) for d in new_days]
    assert cache["301999.SZ"]["close"] == [20.0, 24.0]
    # 存量股不受影响: 头部(1月, <START)保留 + 尾部替换
    assert cache["600000.SH"]["dates"] == ["2026-01-05", "2026-01-06",
                                           _dstr(new_days[0]),
                                           _dstr(new_days[1])]


def test_refresh_cache_batch_failure_keeps_going(zt_tmp_paths, monkeypatch):
    """读取批失败 → 跳过该批不炸, 旧缓存原样保留(不丢进度)。"""
    old_cache = {"600000.SH": {"dates": ["2026-01-05"], "close": [9.0],
                               "pre": [9.0]}}
    zt.CACHE_PATH.write_bytes(pickle.dumps(old_cache, protocol=4))
    fake = _FakeXtData({}, read_fail=True)
    _install_fake_xtdata(monkeypatch, fake)

    r = zt.refresh_cache(days=90)

    cache = pickle.loads(zt.CACHE_PATH.read_bytes())
    assert cache["600000.SH"]["close"] == [9.0]
    assert r["codes"] == 1


# ---------------- prev_day_pool 陈旧保护(>10 自然日 → 空契约) ----------------

def _write_index(day_items):
    """day_items: {日期串: [{code, boards}]} → 落盘到已 monkeypatch 的索引路径。"""
    zt.INDEX_PATH.write_bytes(pickle.dumps(day_items, protocol=4))


def test_prev_day_pool_stale_beyond_10_days_returns_empty(zt_tmp_paths, caplog):
    """索引最新日期距 today >10 自然日 → {"date": None, "codes": []}
    (fail-closed, 与空索引同契约) + warning 提示跑 --refresh。"""
    _write_index({"2026-08-20": [{"code": "600000.SH", "boards": 1}],
                  "2026-08-26": [{"code": "000001.SZ", "boards": 2}]})
    with caplog.at_level(logging.WARNING, logger="prism.zt_history"):
        r = zt.prev_day_pool(today=date(2026, 9, 7))
    assert r == {"date": None, "codes": []}
    assert "陈旧" in caplog.text and "--refresh" in caplog.text


def test_prev_day_pool_stale_boundary_exactly_10_days_is_fresh(zt_tmp_paths):
    """边界钉死: 距今正好 10 自然日 = 新鲜(覆盖国庆/春节 8 天长假口径);
    >10 才算陈旧。"""
    _write_index({"2026-08-28": [{"code": "600000.SH", "boards": 1}]})
    r = zt.prev_day_pool(today=date(2026, 9, 7))          # 距今 10 天
    assert r == {"date": "2026-08-28", "codes": ["600000.SH"]}


def test_prev_day_pool_fresh_returns_normal(zt_tmp_paths):
    """新鲜索引 → 行为不变(取 <today 最大日期的池)。"""
    _write_index({"2026-09-03": [{"code": "000001.SZ", "boards": 1}],
                  "2026-09-04": [{"code": "600000.SH", "boards": 2}]})
    r = zt.prev_day_pool(today=date(2026, 9, 7))
    assert r == {"date": "2026-09-04", "codes": ["600000.SH"]}


# ---------------- build_index / 原子写 ----------------

def test_build_index_writes_index_file(zt_tmp_paths):
    """build_index 落盘到 INDEX_PATH(原子写), 可被 prev_day_pool 消费。"""
    cache = {"600000.SH": {"dates": ["2026-09-03", "2026-09-04"],
                           "close": [10.0, 11.0], "pre": [10.0, 10.0]}}
    idx = zt.build_index(cache=cache)
    assert "2026-09-04" in idx
    assert zt.INDEX_PATH.exists()                       # 已落盘
    assert not (zt_tmp_paths / ".zt_history_index.pkl.tmp").exists()
    r = zt.prev_day_pool(today=date(2026, 9, 7))
    assert r == {"date": "2026-09-04", "codes": ["600000.SH"]}
