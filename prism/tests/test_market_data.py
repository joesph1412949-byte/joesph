# -*- coding: utf-8 -*-
"""market_data 单元测试 — 采集解析/缓存/查询, 全离线(注入假 http_get)。"""
import logging
import os
import pickle
import sys
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.market_data as md


@pytest.fixture()
def _ws_tmp(tmp_path_factory):
    """工作区内临时目录(沙箱拒绝系统 Temp 时用, 保证 CACHE_PATH 可写)。

    目录: <项目根>/pt_ws_tmp/<唯一名>, 用后尽力清理。

    **必须用唯一名, 不能用固定名**: 清理在沙箱下会失败(safe-delete 报
    windows-sandbox-recycle-bin-unavailable), 此时固定名目录会残留上一轮的
    缓存, 污染下一轮断言 —— 实测出现过 sectors 累积成 3(用例期望 1)、
    global 多出 UDI/US10Y/VIX、sector_map 被后跑的用例覆盖, 共 4 个用例假失败。
    清理只能当善后, 不能当作用例正确性的前提。
    """
    import shutil
    import tempfile
    base = Path(__file__).parent.parent.parent / "pt_ws_tmp"
    base.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="mkt_", dir=str(base)))
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------- 假响应

class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeGetter:
    """按 URL 关键字返回罐装 JSON; 可选 fail_urls 模拟网络失败。"""

    def __init__(self, url_data, fail_urls=None):
        self.url_data = url_data          # {关键字: data}
        self.fail_urls = fail_urls or []  # [关键字]
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        for key in self.fail_urls:
            if key in url:
                raise OSError("模拟网络失败: %s" % key)
        for key, data in self.url_data.items():
            if key in url:
                return FakeResp(data)
        raise AssertionError("未预期的 URL: %s" % url)


def _resp(data):
    return {"rc": 0, "data": data}


def _fake_probe(url_data=None, fail_urls=None):
    g = FakeGetter(url_data or {}, fail_urls=fail_urls)
    p = md.EastMoneyProbe(http_get=g)
    return p, g


# ---------------------------------------------------------------- 解析

def test_fetch_sector_list_parses():
    data = _resp({"total": 2,
                  "diff": [{"f12": "BK0475", "f14": "银行"},
                           {"f12": "BK0732", "f14": "证券"}]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: data})
    lst = p.fetch_sector_list(page_size=300)
    assert lst == [{"code": "BK0475", "name": "银行"},
                   {"code": "BK0732", "name": "证券"}]


def test_fetch_sector_list_multipage():
    d1 = _resp({"total": 3, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    d2 = _resp({"total": 3, "diff": [{"f12": "BK0732", "f14": "证券"},
                                     {"f12": "BK0800", "f14": "保险"}]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: {
        # 按分页参数区分: 简化处理 — 让 getter 对同一 URL 返回池
    }})
    # 多页测试用专门 getter: calls[0]→d1, calls[1]→d2
    g = FakeGetter2([d1, d2])
    p = md.EastMoneyProbe(http_get=g)
    lst = p.fetch_sector_list(page_size=1)
    assert len(lst) == 3
    assert lst[0]["code"] == "BK0475"
    assert lst[2]["code"] == "BK0800"


class FakeGetter2:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return FakeResp(self.responses.pop(0))


def test_fetch_kline():
    data = _resp({"klines": ["2026-07-01,4147.72,30064854350.00",
                             "2026-07-02,4160.67,32598801572.00"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: data})
    kl = p.fetch_kline("90.BK0475", "20260101", "20260823")
    assert len(kl) == 2
    assert kl[0]["date"] == "2026-07-01"
    assert kl[0]["close"] == 4147.72
    assert kl[0]["amount"] == 30064854350.00
    # 顺序应与接口一致(升序)
    assert kl[1]["close"] == 4160.67


def test_fetch_sector_flow():
    data = _resp({"klines": ["2026-07-13,-575423232.0,824968448.0,"
                             "-249544960.0,-238578432.0,-336844800.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.FFLOW_URL: data})
    fl = p.fetch_sector_flow("90.BK0475")
    assert len(fl) == 1
    assert fl[0]["date"] == "2026-07-13"
    assert fl[0]["main_net_in"] == -575423232.0
    assert fl[0]["big_net_in"] == 824968448.0


def test_fetch_global_kline():
    data = _resp({"klines": ["2026-07-01,18000.00", "2026-07-02,18100.00"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: data})
    kl = p.fetch_global_kline("100.NDX", "20260101", "20260823")
    assert kl[0] == {"date": "2026-07-01", "close": 18000.00}


def test_fetch_fail_after_retries():
    """网络失败重试后仍失败 → 抛 MarketDataError(不能静默返回空)。"""
    p, _ = _fake_probe({}, fail_urls=[md.EastMoneyProbe.CLIST_URL])
    try:
        p.fetch_sector_list()
        assert False, "应抛 MarketDataError"
    except md.MarketDataError:
        pass


# ---------------------------------------------------------------- 缓存与查询

def test_build_sector_cache_and_query(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")

    clist = _resp({"total": 1,
                   "diff": [{"f12": "BK0475", "f14": "银行"}]})
    kline = _resp({"klines": ["2026-07-01,100.0,1000.0",
                              "2026-07-02,101.0,1100.0"]})
    flow = _resp({"klines": ["2026-07-01,50.0,1.0,2.0,3.0,4.0",
                             "2026-07-02,60.0,1.0,2.0,3.0,4.0"]})
    p, g = _fake_probe({md.EastMoneyProbe.CLIST_URL: clist,
                        md.EastMoneyProbe.KLINE_URL: kline,
                        md.EastMoneyProbe.FFLOW_URL: flow})
    r = md.build_sector_cache(probe=p, beg="20260101", end="20260823")
    assert r["sectors"] == 1
    assert r["kline_codes"] == 1
    assert r["flow_codes"] == 1

    # 落盘内容(查询侧由 _load_cache 直接读段)
    cache = md._load_cache()
    assert cache["kline"]["BK0475"]["dates"] == ["2026-07-01", "2026-07-02"]
    assert cache["kline"]["BK0475"]["close"] == [100.0, 101.0]
    assert cache["flow"]["BK0475"]["main_net_in"] == [50.0, 60.0]
    assert cache["sectors"]["BK0475"]["name"] == "银行"


def test_build_sector_cache_skips_existing(_ws_tmp, monkeypatch):
    """增量: 已采集的板块不再重复请求。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    clist = _resp({"total": 1, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    p, g = _fake_probe({md.EastMoneyProbe.CLIST_URL: clist})
    r1 = md.build_sector_cache(probe=p, beg="20260101", end="20260823")
    assert r1["kline_codes"] == 0   # 无K线数据(只做了列表), 但列表已存
    # 再次运行: 列表刷新, 无K线请求
    r2 = md.build_sector_cache(probe=p, beg="20260101", end="20260823")
    assert r2["sectors"] == 1


def test_build_sector_cache_refreshes_stale_tail(_ws_tmp, monkeypatch):
    """存量K线/资金流尾部过期(最后日期<end) → 重采替换; 已追平 → 不重复请求。

    (09-07 实证: 缓存冻在 09-02 而源头已有 09-03/04, 根因=已有即跳过,
    与 zt_history ④ 同类病。)
    """
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    clist = _resp({"total": 1, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    p, g = _fake_probe({
        md.EastMoneyProbe.CLIST_URL: clist,
        md.EastMoneyProbe.KLINE_URL: _resp(
            {"klines": ["2026-07-01,100.0,1000.0"]}),
        md.EastMoneyProbe.FFLOW_URL: _resp(
            {"klines": ["2026-07-01,50.0,1.0,2.0,3.0,4.0"]})})

    def _kline_hits():
        return len([1 for u, _ in g.calls
                    if md.EastMoneyProbe.KLINE_URL in u])

    md.build_sector_cache(probe=p, beg="20260101", end="20260701")
    assert md._load_cache()["kline"]["BK0475"]["dates"] == ["2026-07-01"]
    hits_1 = _kline_hits()
    # 源头长出新交易日: 存量尾部过期 → 重采替换
    g.url_data[md.EastMoneyProbe.KLINE_URL] = _resp({"klines": [
        "2026-07-01,100.0,1000.0", "2026-07-02,101.0,1100.0",
        "2026-07-03,103.0,1200.0"]})
    g.url_data[md.EastMoneyProbe.FFLOW_URL] = _resp({"klines": [
        "2026-07-01,50.0,1.0,2.0,3.0,4.0",
        "2026-07-02,60.0,1.0,2.0,3.0,4.0",
        "2026-07-03,70.0,1.0,2.0,3.0,4.0"]})
    r = md.build_sector_cache(probe=p, beg="20260101", end="20260703")
    assert r["kline_codes"] == 1
    rec = md._load_cache()["kline"]["BK0475"]
    assert rec["dates"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert md._load_cache()["flow"]["BK0475"]["dates"][-1] == "2026-07-03"
    assert _kline_hits() > hits_1            # 确实重新请求了
    # 已追平(最后日期==end) → 不再重复请求
    hits_2 = _kline_hits()
    md.build_sector_cache(probe=p, beg="20260101", end="20260703")
    assert _kline_hits() == hits_2
    assert md._load_cache()["kline"]["BK0475"]["dates"][-1] == "2026-07-03"


def test_build_sector_cache_keeps_old_on_shorter_refetch(_ws_tmp, monkeypatch):
    """重采结果变短但末日期未回退 → 可疑截断, 保留旧段(M-1 守卫)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    clist = _resp({"total": 1, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    k3 = _resp({"klines": ["2026-07-01,100.0,1000.0",
                           "2026-07-02,101.0,1100.0",
                           "2026-07-03,103.0,1200.0"]})
    p, g = _fake_probe({
        md.EastMoneyProbe.CLIST_URL: clist,
        md.EastMoneyProbe.KLINE_URL: k3,
        md.EastMoneyProbe.FFLOW_URL: _resp(
            {"klines": ["2026-07-01,50.0,1.0,2.0,3.0,4.0",
                        "2026-07-02,60.0,1.0,2.0,3.0,4.0",
                        "2026-07-03,70.0,1.0,2.0,3.0,4.0"]})})
    md.build_sector_cache(probe=p, beg="20260101", end="20260703")
    assert len(md._load_cache()["kline"]["BK0475"]["dates"]) == 3
    # 次日(end=07-04)尾部过期 → 重采; 源头却返回截短的 2 根(07-03..07-04)
    # → 可疑截断: 保留旧 3 根段, 不静默缩短
    g.url_data[md.EastMoneyProbe.KLINE_URL] = _resp({"klines": [
        "2026-07-03,103.0,1200.0", "2026-07-04,104.0,1300.0"]})
    g.url_data[md.EastMoneyProbe.FFLOW_URL] = _resp({"klines": [
        "2026-07-01,50.0,1.0,2.0,3.0,4.0",
        "2026-07-02,60.0,1.0,2.0,3.0,4.0",
        "2026-07-03,70.0,1.0,2.0,3.0,4.0",
        "2026-07-04,80.0,1.0,2.0,3.0,4.0"]})
    md.build_sector_cache(probe=p, beg="20260101", end="20260704")
    assert len(md._load_cache()["kline"]["BK0475"]["dates"]) == 3
    assert md._load_cache()["kline"]["BK0475"]["dates"][-1] == "2026-07-03"


def test_build_global_cache(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    kline = _resp({"klines": ["2026-07-01,18000.0", "2026-07-02,18100.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: kline})
    g = md.build_global_cache(probe=p, beg="20260101", end="20260823")
    assert set(g) == {"NDX", "SPX", "DJIA", "UDI"}
    assert g["NDX"]["dates"] == ["2026-07-01", "2026-07-02"]
    assert g["NDX"]["close"] == [18000.0, 18100.0]


# ---------------------------------------------------------------- 申万通道(非东财备用)

class FakeSWAK:
    """假 akshare: sw_index_first_info / index_hist_sw, 免网络。"""

    def __init__(self, first_df=None, hist_map=None):
        import pandas as pd
        import datetime as _dt
        self._first = first_df if first_df is not None else pd.DataFrame({
            "行业代码": ["801010.SI", "801030.SI"],
            "行业名称": ["农林牧渔", "食品饮料"],
            "成分个数": [104, 411],
        })
        d1 = _dt.date(2026, 7, 1)
        d2 = _dt.date(2026, 7, 2)
        self._hist = hist_map or {
            "801010": pd.DataFrame({
                "指数代码": ["801010", "801010"],
                "日期": [d1, d2],
                "开盘": [1000.0, 1010.0],
                "收盘": [1005.0, 1018.0],
                "最高": [1010.0, 1020.0],
                "最低": [998.0, 1005.0],
                "成交量": [0.1, 0.2],
                "成交额": [1.2, 2.5],
            }),
        }

    def sw_index_first_info(self):
        return self._first

    def index_hist_sw(self, symbol="", period="day"):
        # 801030 无历史(模拟部分板块数据缺失); 缺失 → 空表
        if symbol == "801030":
            import pandas as pd
            return pd.DataFrame()
        return self._hist.get(symbol)


def test_sw_feed_sector_list():
    f = md.SWIndexFeed(ak=FakeSWAK())
    lst = f.fetch_sector_list()
    assert lst[0] == {"code": "801010", "name": "农林牧渔"}
    assert lst[1] == {"code": "801030", "name": "食品饮料"}


def test_sw_feed_kline():
    f = md.SWIndexFeed(ak=FakeSWAK())
    kl = f.fetch_sector_kline("801010")
    assert len(kl) == 2
    assert kl[0]["date"] == "2026-07-01"
    assert kl[0]["close"] == 1005.0
    assert kl[0]["amount"] == 1.2


def test_build_sector_cache_sw_source(_ws_tmp, monkeypatch):
    """source=sw: 用申万通道建缓存, flow 留空(申万无资金流)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    f = md.SWIndexFeed(ak=FakeSWAK())
    r = md.build_sector_cache(probe=f, beg="20260101", end="20260823",
                              source="sw")
    assert r["sectors"] == 2
    assert r["kline_codes"] == 1       # 只有 801010 有K线
    assert r["flow_codes"] == 0        # 申万无资金流
    kl = md._load_cache()["kline"]["801010"]
    assert kl["dates"] == ["2026-07-01", "2026-07-02"]
    assert kl["close"] == [1005.0, 1018.0]


# ---------------------------------------------------------------- 新浪美股通道

class FakeSinaAK:
    """假 akshare: index_us_stock_sina, 免网络。"""

    def __init__(self):
        import pandas as pd
        import datetime as _dt
        self._df = pd.DataFrame({
            "date": [_dt.date(2026, 7, 1), _dt.date(2026, 7, 2)],
            "open": [17000.0, 17100.0],
            "high": [17100.0, 17200.0],
            "low": [16900.0, 17000.0],
            "close": [17050.0, 17180.0],
            "volume": [1e9, 1.1e9],
            "amount": [2e11, 2.2e11],
        })

    def index_us_stock_sina(self, symbol="", **kwargs):
        return self._df


def test_sina_feed_global_kline():
    f = md.SinaUSIndexFeed(ak=FakeSinaAK())
    kl = f.fetch_global_kline(".IXIC")
    assert len(kl) == 2
    assert kl[0]["date"] == "2026-07-01"
    assert kl[0]["close"] == 17050.0
    assert kl[1]["close"] == 17180.0


def test_build_global_cache_sina_source(_ws_tmp, monkeypatch):
    """source=sina: 用新浪美股建 global 段, 只含纳指/标普/道指。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    f = md.SinaUSIndexFeed(ak=FakeSinaAK())
    g = md.build_global_cache(probe=f, beg="20260701", end="20260823",
                              source="sina")
    assert set(g.keys()) == {"NDX", "SPX", "DJIA"}   # 新浪无美元指数
    assert g["NDX"]["close"] == [17050.0, 17180.0]
    assert "UDI" not in g


# ---------------------------------------------------------------- 个股→行业映射

class FakeMapAK:
    """假 akshare: 行业列表 + 成分股。"""

    def __init__(self):
        import pandas as pd
        self._first = pd.DataFrame({
            "行业代码": ["801010.SI"], "行业名称": ["农林牧渔"],
            "成分个数": [2],
        })
        self._cons = pd.DataFrame({
            "序号": [1, 2],
            "股票代码": ["000019.SZ", "300999.SZ"],
            "股票名称": ["深粮控股", "金龙鱼"],
            "纳入时间": ["2021-07-30", "2020-09-21"],
            "所属行业": ["农林牧渔", "农林牧渔"],
        })

    def sw_index_first_info(self):
        return self._first

    def sw_index_third_cons(self, symbol="", **kwargs):
        return self._cons


def test_build_sector_map_and_query(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    f = md.SWIndexFeed(ak=FakeMapAK())
    r = md.build_sector_map(feed=f)
    assert r["stocks"] == 2
    # 落盘段(消费方 backtest/cli.py 与 prism/data.py 读 cache["sector_map"])
    smap = md._load_cache()["sector_map"]
    assert smap["000019"] == {"sector": "801010", "name": "深粮控股"}
    assert smap["300999"]["sector"] == "801010"
    # 带后缀/裸代码都归一成 6 位
    assert md._code6("000019.SZ") == "000019"
    assert md._code6("000019") == "000019"
    assert md._code6("600519") == "600519"
    assert md._code6("bad") is None


def test_rebuild_clears_old_kline(_ws_tmp, monkeypatch):
    """rebuild=True: 切换数据源时清空旧体系K线/资金流。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    # 先建申万缓存(旧体系)
    sw = md.SWIndexFeed(ak=FakeSWAK())
    md.build_sector_cache(probe=sw, beg="20260101", end="20260823",
                          source="sw")
    # 再东财 rebuild(新体系)
    clist = _resp({"total": 1, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    kline = _resp({"klines": ["2026-07-01,100.0,1000.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: clist,
                        md.EastMoneyProbe.KLINE_URL: kline})
    r = md.build_sector_cache(probe=p, beg="20260101", end="20260823",
                              source="eastmoney", rebuild=True)
    assert r["kline_codes"] <= 1          # 没有申万801010了
    assert "801010" not in (md._load_cache().get("kline") or {})


# ---------------------------------------------------------------- FRED 通道

class FakeFredResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeFredGetter:
    def __init__(self, series_data):
        self.series_data = series_data    # {series_id: [obs...]}
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        sid = (params or {}).get("series_id")
        obs = self.series_data.get(sid, [])
        return FakeFredResp({"observations": obs})


def _fred_obs():
    return [{"date": "2026-07-01", "value": "4.25"},
            {"date": "2026-07-02", "value": "4.28"},
            {"date": "2026-07-03", "value": "."}]   # "." 缺失值应跳过


def test_fred_feed_parse():
    g = FakeFredGetter({"DGS10": _fred_obs()})
    f = md.FREDFeed(http_get=g, api_key="test-key-123")
    kl = f.fetch_global_kline("DGS10", beg="20260701", end="20260823")
    assert len(kl) == 2                      # "." 被跳过
    assert kl[0] == {"date": "2026-07-01", "close": 4.25}
    assert kl[1] == {"date": "2026-07-02", "close": 4.28}


def test_fred_feed_key_required(monkeypatch):
    """无 api_key → MarketDataError(提示设置 FRED_API_KEY)。

    monkeypatch 屏蔽 _load_fred_api_key: 本地 .env 有真实key时, 测试也要
    验证"key缺失"分支(否则被环境副作用干扰)。"""
    monkeypatch.setattr(md, "_load_fred_api_key", lambda: None)
    f = md.FREDFeed(http_get=FakeFredGetter({}), api_key=None)
    try:
        f.fetch_global_kline("DGS10")
        assert False, "应抛 MarketDataError"
    except md.MarketDataError as e:
        assert "FRED_API_KEY" in str(e)


def test_build_global_cache_fred_source(_ws_tmp, monkeypatch):
    """source=fred: 美债/VIX 写入 global 段。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    g = FakeFredGetter({"DGS10": _fred_obs(), "VIXCLS": _fred_obs()})
    f = md.FREDFeed(http_get=g, api_key="test-key")
    out = md.build_global_cache(probe=f, beg="20260701", end="20260823",
                                source="fred")
    assert set(out.keys()) == {"US10Y", "VIX"}
    assert out["US10Y"]["close"] == [4.25, 4.28]
    assert out["VIX"]["name"] == "VIX恐慌指数"


# ---------------------------------------------------------------- 资金惯性/基准

def test_fetch_flow_rank_parses():
    data = _resp({"total": 3, "diff": [
        {"f12": "BK0486", "f14": "传媒", "f62": 6.174e9},
        {"f12": "BK0433", "f14": "农林牧渔", "f62": 3.016e9},
        {"f12": "BK0000", "f14": "无数据", "f62": "-"}]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: data})
    rows = p.fetch_flow_rank()
    assert [r["code"] for r in rows] == ["BK0486", "BK0433"]
    assert rows[0]["net_in"] == 6.174e9
    assert rows[0]["name"] == "传媒"


def test_build_flow_rank_idempotent(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    d1 = _resp({"total": 2, "diff": [
        {"f12": "BK0486", "f14": "传媒", "f62": 1e9},
        {"f12": "BK0433", "f14": "农林牧渔", "f62": 5e8}]})
    p1 = md.EastMoneyProbe(http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: d1}))
    r1 = md.build_flow_rank(probe=p1)
    assert r1 == {"dates": 1, "sectors_today": 2}
    # 盘后重跑: 当日覆盖, 不累积重复日期
    d2 = _resp({"total": 2, "diff": [
        {"f12": "BK0486", "f14": "传媒", "f62": 9e8},
        {"f12": "BK0433", "f14": "农林牧渔", "f62": 7e8}]})
    p2 = md.EastMoneyProbe(http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: d2}))
    r2 = md.build_flow_rank(probe=p2)
    assert r2["dates"] == 1
    fr = md._load_cache()["flow_rank"]
    assert len(fr["dates"]) == 1
    assert fr["rows"][fr["dates"][0]][0]["net_in"] == 9e8


def test_build_flow_rank_skips_empty_snapshot(_ws_tmp, monkeypatch):
    """全 '-' 快照(盘前/非交易日) → 不落盘, dates 不增长。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    data = _resp({"total": 2, "diff": [
        {"f12": "BK0001", "f14": "甲", "f62": "-"},
        {"f12": "BK0002", "f14": "乙", "f62": "-"}]})
    p = md.EastMoneyProbe(http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: data}))
    r = md.build_flow_rank(probe=p)
    assert r == {"dates": 0, "sectors_today": 0}
    assert (md._load_cache().get("flow_rank") or {}) == {}


def test_build_benchmark(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    data = _resp({"klines": ["2026-07-01,4147.72,30064854350.00",
                             "2026-07-02,4160.67,32598801572.00"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: data})
    r = md.build_benchmark(probe=p)
    assert r == {"days": 2, "kept_old": False}
    assert md._load_cache()["benchmark"]["close"][0] == 4147.72


def test_build_benchmark_keeps_old_on_failure(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    # 三路降级源全部封住(否则会真去问 QMT/通达信), 专测"原源失败 → 保留旧缓存"
    monkeypatch.setattr(md, "_qmt_xtdata", lambda: None)
    md._save_cache({"benchmark": {"dates": ["2026-07-01"], "close": [4000.0],
                                  "amount": [1e10]}})
    p, _ = _fake_probe({}, fail_urls=[md.EastMoneyProbe.KLINE_URL])
    r = md.build_benchmark(probe=p)
    assert r["kept_old"] is True
    assert md._load_cache()["benchmark"]["close"] == [4000.0]


def test_mkt_snapshot_new_segments(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "futures_snapshot", lambda: {})
    md._save_cache({
        "kline": {"801010": {"dates": ["2026-09-05"], "close": [100.0],
                             "amount": [1e8]}},
        "sectors": {"801010": {"name": "农林牧渔"}},
        "benchmark": {"dates": ["2026-09-05"], "close": [4000.0],
                      "amount": [1e10]},
        "flow_rank": {"dates": ["2026-09-05"],
                      "rows": {"2026-09-05": [{"code": "BK0486",
                                               "name": "传媒",
                                               "net_in": 1e9}]}}})
    snap = md.mkt_snapshot()
    assert snap["benchmark"]["close"] == [4000.0]
    assert snap["flow_rank"]["dates"] == ["2026-09-05"]
    assert snap["sector"]["801010"]["name"] == "农林牧渔"


def test_cli_build_flags_run_both(_ws_tmp, monkeypatch):
    """--build-benchmark --build-flow-rank 组合: 两个都执行(终审 I-1 回归锁)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-benchmark",
                                     "--build-flow-rank"])
    calls = []
    monkeypatch.setattr(md, "build_flow_rank",
                        lambda probe=None: calls.append("fr")
                        or {"dates": 1, "sectors_today": 2})
    monkeypatch.setattr(md, "build_benchmark",
                        lambda probe=None, beg=None, end=None:
                        calls.append("bm") or {"days": 1, "kept_old": False})
    md.build_cli()
    assert calls == ["fr", "bm"]
    assert (md._load_cache().get("benchmark")) is None  # 假实现不落盘, 只验证调用序


def test_cli_flow_rank_failure_does_not_block_benchmark(_ws_tmp, monkeypatch,
                                                        capsys):
    """flow_rank 被封(抛 MarketDataError) → benchmark 照常执行, 但整体非零退出。

    终审 I-1 补强(benchmark 不被连累) + 09-19 D1(点名失败非零退出: 一段失败
    不得让盘后自动化看到 exit 0 —— 原用例只断言"照常执行", 与 MEMORY 的非零
    退出承诺冲突, 这里补上退出码断言)。
    """
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-benchmark",
                                     "--build-flow-rank"])

    def boom(probe=None):
        raise md.MarketDataError("push2 banned")

    ran = []
    monkeypatch.setattr(md, "build_flow_rank", boom)
    monkeypatch.setattr(md, "build_benchmark",
                        lambda probe=None, beg=None, end=None:
                        ran.append("bm") or {"days": 1, "kept_old": False})
    with pytest.raises(SystemExit) as ei:
        md.build_cli()
    assert ran == ["bm"]           # benchmark 不被连累
    assert "flow_rank" in str(ei.value)
    out = capsys.readouterr().out
    assert "资金惯性快照失败" in out


def test_cli_single_flag_failure_exits_nonzero(_ws_tmp, monkeypatch):
    """只点名 flow_rank 且失败 → 非零退出(不静默吞, 自动化可感知)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-flow-rank"])

    def boom(probe=None):
        raise md.MarketDataError("push2 banned")

    monkeypatch.setattr(md, "build_flow_rank", boom)
    with pytest.raises(SystemExit) as ei:
        md.build_cli()
    assert "flow_rank" in str(ei.value)


# ---------------------------------------------------------------- ETF 行情(W1)

class FakeDF:
    """get_market_data_ex 单码返回值: 支持 df["col"] 取列 + df.index 日期。"""

    def __init__(self, cols):
        self._cols = cols
        self.index = list(cols.get("index") or [])

    def __getitem__(self, k):
        return list(self._cols[k])

    def __len__(self):
        return len(self._cols.get("close") or [])


class FakeXt:
    """假 xtdata: local=已有本地日K; pending=需 download 后才可见; 故障注入。"""

    def __init__(self, local=None, pending=None, fail_read=(), fail_dl=()):
        self.local = {k: FakeDF(v) for k, v in (local or {}).items()}
        self.pending = {k: FakeDF(v) for k, v in (pending or {}).items()}
        self.fail_read = set(fail_read)
        self.fail_dl = set(fail_dl)
        self.downloads = []
        self.dl_calls = 0

    def get_market_data_ex(self, empty, codes, period=None, count=None):
        out = {}
        for c in codes:
            if c in self.fail_read:
                raise RuntimeError("read boom: %s" % c)
            df = self.local.get(c)   # pending 只能 download 后才可见
            if df is not None:
                out[c] = df
        return out

    def download_history_data2(self, codes, period, start_time="", end_time=""):
        self.dl_calls += 1
        for c in codes:
            self.downloads.append(c)
            if c in self.fail_dl:
                raise RuntimeError("dl boom: %s" % c)
        self.local.update({c: self.pending.pop(c)
                           for c in codes if c in self.pending})


_TODAY = _date.today().strftime("%Y%m%d")
TWO_BARS = {"close": [1.00, 1.05], "amount": [9e8, 1.1e9],
            "index": ["20260105", _TODAY]}          # 末根=今天 → 新鲜
STALE_BARS = {"close": [2.00, 2.02], "amount": [5e8, 5.5e8],
              "index": ["20260101", "20260102"]}    # 末根<今天 → 过期


def test_fetch_etf_quotes_local_hit_no_download():
    xt = FakeXt(local={"512800.SH": TWO_BARS})
    out = md.fetch_etf_quotes(["512800.SH"], xtdata_mod=xt)
    assert out == {"512800.SH": {"amount": 1.1e9,
                                 "pct_chg": (1.05 / 1.00 - 1) * 100}}
    assert xt.downloads == []


def test_fetch_etf_quotes_downloads_when_missing():
    xt = FakeXt(pending={"159997.SZ": TWO_BARS})
    monkey_memo = {}
    orig = md._ETF_DL_MEMO
    md._ETF_DL_MEMO = monkey_memo
    try:
        out = md.fetch_etf_quotes(["159997.SZ"], xtdata_mod=xt)
    finally:
        md._ETF_DL_MEMO = orig
    assert xt.downloads == ["159997.SZ"]
    assert out["159997.SZ"]["amount"] == 1.1e9
    assert monkey_memo.get("159997.SZ")   # 记了 memo(当日不再下载)


def test_fetch_etf_quotes_redownloads_stale_local():
    """本地有数据但末根早于今日 → 重下载换新(I-1: 首下载后不得永久冻结)。"""
    xt = FakeXt(local={"512800.SH": STALE_BARS},
                pending={"512800.SH": TWO_BARS})
    orig = md._ETF_DL_MEMO
    md._ETF_DL_MEMO = {}
    try:
        out = md.fetch_etf_quotes(["512800.SH"], xtdata_mod=xt)
    finally:
        md._ETF_DL_MEMO = orig
    assert xt.dl_calls == 1               # 触发了重下载
    assert out["512800.SH"]["amount"] == 1.1e9   # 取到新末根, 非冻结旧值


def test_fetch_etf_quotes_batch_single_call():
    """多个过期码 → 一次批量下载(I-1: 不再逐码 24 连打)。"""
    xt = FakeXt(pending={"512800.SH": TWO_BARS, "159997.SZ": TWO_BARS})
    orig = md._ETF_DL_MEMO
    md._ETF_DL_MEMO = {}
    try:
        out = md.fetch_etf_quotes(["512800.SH", "159997.SZ"],
                                  xtdata_mod=xt)
    finally:
        md._ETF_DL_MEMO = orig
    assert xt.dl_calls == 1               # 一批, 不是两 calls
    assert set(out) == {"512800.SH", "159997.SZ"}


def test_fetch_etf_quotes_memo_skips_second_download_same_day():
    xt = FakeXt(pending={"159997.SZ": TWO_BARS})
    orig = md._ETF_DL_MEMO
    md._ETF_DL_MEMO = {}
    try:
        md.fetch_etf_quotes(["159997.SZ"], xtdata_mod=xt)
        n1 = len(xt.downloads)
        md.fetch_etf_quotes(["159997.SZ"], xtdata_mod=xt)
        assert len(xt.downloads) == n1 == 1   # 第二次当日不再下载
    finally:
        md._ETF_DL_MEMO = orig


def test_fetch_etf_quotes_fail_open_per_code():
    """坏码(读炸/下载炸/始终无数据)跳过, 好码照常返回。"""
    xt = FakeXt(local={"512800.SH": TWO_BARS},
                pending={"512000.BAD": TWO_BARS},
                fail_read={"111111.SH"}, fail_dl={"222222.SZ"})
    orig = md._ETF_DL_MEMO
    md._ETF_DL_MEMO = {}
    try:
        out = md.fetch_etf_quotes(
            ["512800.SH", "111111.SH", "222222.SZ", "512000.BAD",
             "333333.NONE"], xtdata_mod=xt)
    finally:
        md._ETF_DL_MEMO = orig
    # 222222.SZ 下载失败且无本地数据 → 跳过
    assert set(out) == {"512800.SH", "512000.BAD"}
    assert out["512800.SH"]["pct_chg"] > 0


def test_fetch_etf_quotes_single_bar_skipped():
    """只有 1 根K线算不出涨跌幅 → 跳过(fail-open)。"""
    xt = FakeXt(local={"512800.SH": {"close": [1.05], "amount": [1e9]}})
    assert md.fetch_etf_quotes(["512800.SH"], xtdata_mod=xt) == {}


# ================================================ D1~D6 数据完整性修复(09-19)
# 对照: .superpowers/sdd 只读审计逐条实跑证实的缺陷 D1(退出码)/D2(原子写+损坏读)/
# D3(整份回写丢更新)/D4(期货冻结)/D5(sectors 混码)/D6(--stats 死参数)。


# ---------------- D1: CLI 失败必须非零退出(不得吞成 exit 0)

def test_cli_benchmark_failure_exits_nonzero(_ws_tmp, monkeypatch):
    """只点名 benchmark 且失败 → 非零退出(与 flow_rank 同语义)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-benchmark"])

    def boom(probe=None, beg=None, end=None):
        raise md.MarketDataError("kline banned")

    monkeypatch.setattr(md, "build_benchmark", boom)
    with pytest.raises(SystemExit) as ei:
        md.build_cli()
    assert "benchmark" in str(ei.value)


def test_cli_benchmark_kept_old_exits_nonzero(_ws_tmp, monkeypatch):
    """build_benchmark 拉取失败时 fail-open 保留旧缓存(kept_old=True)也算点名失败。

    D1 第二层掩码: 真实网络封禁下 build_benchmark 不抛异常而是 kept_old=True,
    只把 return 改成条件返回挡不住它(缓存 benchmark 停在 09-07 无人察觉这条路径)。
    """
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-benchmark"])
    monkeypatch.setattr(md, "build_benchmark",
                        lambda probe=None, beg=None, end=None:
                        {"days": 165, "kept_old": True})
    with pytest.raises(SystemExit) as ei:
        md.build_cli()
    assert "benchmark" in str(ei.value)


def test_cli_benchmark_success_no_systemexit(_ws_tmp, monkeypatch):
    """回归锁: benchmark 正常路径不得被上面的机制误判成失败。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-benchmark"])
    monkeypatch.setattr(md, "build_benchmark",
                        lambda probe=None, beg=None, end=None:
                        {"days": 2, "kept_old": False})
    md.build_cli()          # 抛 SystemExit 即失败


# ---------------- D2: 缓存原子写 + 读失败留痕

def test_load_cache_corrupt_logs_warning(_ws_tmp, monkeypatch, caplog):
    """缓存文件损坏 → 记 WARNING 并 fail-open 返回 {}(不得静默当空)。"""
    p = _ws_tmp / "mkt.pkl"
    monkeypatch.setattr(md, "CACHE_PATH", p)
    p.write_bytes(b"\x80\x04truncated-not-a-pickle")
    with caplog.at_level(logging.WARNING):
        assert md._load_cache() == {}
    assert "读取失败" in caplog.text


def test_save_cache_file_is_raw_pickle_without_tmp(_ws_tmp, monkeypatch):
    """落盘是裸 pickle(消费方 qmt/tools/live_check.py 直接 pickle.load)且不留 tmp。"""
    p = _ws_tmp / "mkt.pkl"
    monkeypatch.setattr(md, "CACHE_PATH", p)
    md._save_cache({"a": 1})
    with open(p, "rb") as fp:
        assert pickle.load(fp) == {"a": 1}
    assert list(_ws_tmp.glob("*.tmp")) == []


def test_save_cache_publish_failure_keeps_old_file(_ws_tmp, monkeypatch):
    """发布失败 → 旧文件原样 + 收掉 tmp + 抛错(不得静默当写成功)。

    RED 判别: 旧的 CACHE_PATH.write_bytes 就地截断重写, 根本没有"发布"这一步,
    注入的 os.replace 故障不会被触发, 旧内容也已被覆盖。
    """
    p = _ws_tmp / "mkt.pkl"
    monkeypatch.setattr(md, "CACHE_PATH", p)
    md._save_cache({"a": 1})

    def boom(src, dst):
        raise OSError("replace boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        md._save_cache({"b": 2})
    assert md._load_cache() == {"a": 1}
    assert list(_ws_tmp.glob("*.tmp")) == []


def test_load_cache_strict_raises_on_corrupt(_ws_tmp, monkeypatch):
    """strict=True: 读失败原样抛出(给 _save_cache 判定"读不出≠空"用)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    md.CACHE_PATH.write_bytes(b"\x80\x04broken-pickle")
    with pytest.raises(Exception) as ei:
        md._load_cache(strict=True)
    assert not isinstance(ei.value, TypeError)     # 不是"没这个参数"的假通过
    assert md._load_cache() == {}                  # 非 strict 仍 fail-open


def test_build_does_not_overwrite_corrupt_cache(_ws_tmp, monkeypatch, caplog):
    """盘上有文件但读不出 → 任一个 build_* 落盘都必须"拒绝覆盖"(I2b 同口径)。

    原子写只降低自发损坏概率, 挡不住外部截断; 读不出≠空, 若不拒绝, 一次
    --build-flow-rank 就会把 5.9MB 里除 flow_rank 外的全部段抹掉。
    """
    p = _ws_tmp / "mkt.pkl"
    monkeypatch.setattr(md, "CACHE_PATH", p)
    p.write_bytes(b"\x80\x04truncated-not-a-pickle")
    before = p.read_bytes()
    d = _resp({"total": 1,
               "diff": [{"f12": "BK0486", "f14": "传媒", "f62": 1e9}]})
    probe = md.EastMoneyProbe(
        http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: d}))
    with caplog.at_level(logging.WARNING):
        r = md.build_flow_rank(probe=probe)
    assert r["sectors_today"] == 1                  # 采集本身照常成功
    assert p.read_bytes() == before                 # 盘上原文件一动不动
    assert str(p) in caplog.text and "跳过本次落盘" in caplog.text


def test_save_cache_empty_file_is_overwritable(_ws_tmp, monkeypatch):
    """回归锁: 0 字节空文件 ≠ 读不出(没有键可丢) → 照常落盘, 别把"空"也拒了。"""
    p = _ws_tmp / "mkt.pkl"
    monkeypatch.setattr(md, "CACHE_PATH", p)
    p.write_bytes(b"")
    md._save_cache({"a": 1})
    assert md._load_cache() == {"a": 1}


# ---------------- D3: 采集期间他人落的新段不得被陈旧快照写回

def test_build_global_cache_keeps_concurrent_flow_rank(_ws_tmp, monkeypatch):
    """global 采集期间另一写者落当日 flow_rank → 结束后必须还在(D3 丢更新)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    md._save_cache({"flow_rank": {"dates": ["2026-09-18"], "rows": {}}})
    kline = _resp({"klines": ["2026-07-01,18000.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: kline})
    orig = md.EastMoneyProbe.fetch_global_kline

    def spy(self, secid, beg, end, lmt=600):
        # 模拟并发写者: 本函数启动后、落盘前, 别人写了新的 flow_rank 段
        md._save_cache({"flow_rank": {"dates": ["2026-09-18", "2026-09-19"],
                                      "rows": {}}})
        return orig(self, secid, beg, end, lmt)

    monkeypatch.setattr(md.EastMoneyProbe, "fetch_global_kline", spy)
    md.build_global_cache(probe=p, beg="20260101", end="20260823")
    assert md._load_cache()["flow_rank"]["dates"] == ["2026-09-18", "2026-09-19"]


def test_fetch_futures_keeps_concurrent_flow_rank(_ws_tmp, monkeypatch):
    """fetch_futures 期间另一写者落当日 flow_rank → 结束后必须还在(D3 丢更新)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    md._save_cache({"flow_rank": {"dates": ["2026-09-18"], "rows": {}}})

    def fake(sym):
        md._save_cache({"flow_rank": {"dates": ["2026-09-18", "2026-09-19"],
                                      "rows": {}}})
        return _fut_df(_date.today())

    monkeypatch.setattr(md, "_fetch_futures_daily", fake)
    md.fetch_futures()
    assert md._load_cache()["flow_rank"]["dates"] == ["2026-09-18", "2026-09-19"]


# ---------------- D4: 期货尾部新鲜度门(首采后不得永久冻结)

def _fut_df(last_day, n=60):
    """n 根日K(date 列), 末根 = last_day(fetch_futures 对 <60 行按死数据跳过)。"""
    import pandas as pd
    days = [last_day - _timedelta(days=n - 1 - i) for i in range(n)]
    return pd.DataFrame({"date": days, "close": [100.0 + i for i in range(n)]})


def _seed_stale_futures():
    md._save_cache({"futures": {"MA0": {"name": "甲醇",
                                        "dates": ["2026-08-31"],
                                        "close": [1.0]}}})


def test_fetch_futures_refreshes_stale_tail(_ws_tmp, monkeypatch):
    """存量品种尾部早于今日 → 重采换新(D4: 只采"缓存没有的"会永久冻结)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    _seed_stale_futures()
    calls = []
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: calls.append(sym) or _fut_df(_date.today()))
    out = md.fetch_futures()
    assert calls == ["MA0"]                                    # 过期 → 确实重采
    assert out["MA0"]["dates"][-1] == _date.today().isoformat()


def test_fetch_futures_skips_fresh_tail(_ws_tmp, monkeypatch):
    """回归锁: 尾部已追平(末根=今日) → 不请求(门不能变成每次都重采)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    md._save_cache({"futures": {"MA0": {"name": "甲醇",
                                        "dates": [_date.today().isoformat()],
                                        "close": [1.0]}}})
    calls = []
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: calls.append(sym) or _fut_df(_date.today()))
    md.fetch_futures()
    assert calls == []


# ---------------- D4b: 读路径绝不回写(回测跑批不得改写缓存)

def test_futures_snapshot_default_is_read_only(_ws_tmp, monkeypatch):
    """futures_snapshot() 默认: 不联网、不改缓存 —— 只吃缓存里的旧数据。

    判别力: 旧实现在这里会调 fetch_futures() → 尾部过期即联网重采并回写,
    同一回测连跑两轮拿到不同期货数据(F8 命中 666↔719 漂移)。
    """
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    md._save_cache({"sectors": {"801030": {"name": "基础化工"}},
                    "futures": {"MA0": {"name": "甲醇",
                                        "dates": ["2026-08-31"],
                                        "close": [1.0]}}})
    before = (md.CACHE_PATH.read_bytes(),
              md.CACHE_PATH.stat().st_mtime_ns)
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: pytest.fail("纯读路径不得联网采集"))
    out = md.futures_snapshot()
    assert out["801030"]["commodities"]["MA0"]["dates"] == ["2026-08-31"]
    after = (md.CACHE_PATH.read_bytes(),
             md.CACHE_PATH.stat().st_mtime_ns)
    assert after == before          # 文件内容与 mtime 逐字节未变


def test_mkt_snapshot_is_read_only(_ws_tmp, monkeypatch):
    """mkt_snapshot()(prism/data.py 实盘与回测都走它)同样不得回写缓存。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    monkeypatch.setattr("prism.zt_history.prev_day_pool", lambda today=None: {})
    md._save_cache({"sectors": {"801030": {"name": "基础化工"}},
                    "futures": {"MA0": {"name": "甲醇",
                                        "dates": ["2026-08-31"],
                                        "close": [1.0]}}})
    before = (md.CACHE_PATH.read_bytes(),
              md.CACHE_PATH.stat().st_mtime_ns)
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: pytest.fail("纯读路径不得联网采集"))
    snap = md.mkt_snapshot()
    assert snap["futures"]["801030"]["commodities"]["MA0"]["close"] == [1.0]
    assert (md.CACHE_PATH.read_bytes(),
            md.CACHE_PATH.stat().st_mtime_ns) == before


def test_futures_snapshot_refresh_true_collects(_ws_tmp, monkeypatch):
    """显式 refresh=True 才采集+落盘(--build-futures 那条路)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    md._save_cache({"sectors": {"801030": {"name": "基础化工"}},
                    "futures": {"MA0": {"name": "甲醇",
                                        "dates": ["2026-08-31"],
                                        "close": [1.0]}}})
    calls = []
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: calls.append(sym) or _fut_df(_date.today()))
    out = md.futures_snapshot(refresh=True)
    assert calls == ["MA0"]
    assert out["801030"]["commodities"]["MA0"]["dates"][-1] == \
        _date.today().isoformat()
    assert md._load_cache()["futures"]["MA0"]["dates"][-1] == \
        _date.today().isoformat()


def test_cli_build_futures_refreshes(_ws_tmp, monkeypatch):
    """--build-futures: 显式刷新入口(尾部过期 → 重采落盘)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_FUTURES_NAMES", {"MA0": "甲醇"})
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-futures"])
    _seed_stale_futures()
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: _fut_df(_date.today()))
    md.build_cli()
    assert md._load_cache()["futures"]["MA0"]["dates"][-1] == \
        _date.today().isoformat()


# ---------------- D5: rebuild 必须清 sectors(申万/东财码不得混在一个缓存)

def test_rebuild_clears_mixed_source_sectors(_ws_tmp, monkeypatch):
    """rebuild=True: 换源时 sectors 一并清空, 只留新源重采到的码(D5)。

    真实缓存 sectors=527(31 个申万 801xxx + 496 个东财 BKxxxx) —— 每次
    --build-sectors --source sw 都会对 496 个 BK 码发注定失败的申万请求。
    """
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    md._save_cache({"sectors": {"801010": {"name": "农林牧渔"},
                                "BK0475": {"name": "银行"}},
                    "kline": {"801010": {"dates": ["2026-07-01"],
                                         "close": [1.0], "amount": [1.0]}}})
    f = md.SWIndexFeed(ak=FakeSWAK())
    r = md.build_sector_cache(probe=f, beg="20260101", end="20260823",
                              source="sw", rebuild=True)
    assert "BK0475" not in md._load_cache()["sectors"]
    assert r["sectors"] == 2                # 只留申万源重采到的 801010/801030


def test_no_rebuild_keeps_sectors(_ws_tmp, monkeypatch):
    """回归锁: 不带 rebuild 的增量采集不得清 sectors(名称沿用旧缓存)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    md._save_cache({"sectors": {"801010": {"name": "农林牧渔"}}})
    f = md.SWIndexFeed(ak=FakeSWAK())
    r = md.build_sector_cache(probe=f, beg="20260101", end="20260823",
                              source="sw")
    assert r["sectors"] == 2                # 801010(旧, 名称刷新) + 801030(新)


# ---------------- D6: --stats 死参数

def test_cli_stats_flag_removed(_ws_tmp, monkeypatch):
    """--stats 从不被读 → 从参数表删除(argparse 未知参数 → exit 2)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--stats"])
    with pytest.raises(SystemExit):
        md.build_cli()


def test_cli_no_args_prints_cache_stats(_ws_tmp, monkeypatch, capsys):
    """回归锁: 无采集动作时仍打印缓存统计(--stats 的能力不能被删掉)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py"])
    md._save_cache({"sectors": {"801010": {"name": "农林牧渔"}}})
    md.build_cli()
    assert "板块数: 1" in capsys.readouterr().out


# ---------------- D7: benchmark 降级链(东财被封 → 通达信指数通道)

class _DeadProbe:
    """东财通道被封: fetch_benchmark_kline 直接抛(不重试, 免 6s 等待)。"""

    def fetch_benchmark_kline(self, beg, end):
        raise md.MarketDataError("push2his banned")


def _tdx_index_df(rows):
    """通达信指数日K形状的数据帧(列名与 tdx_source._to_df 一致)。"""
    import pandas as pd
    return pd.DataFrame([{"datetime": d + " 15:00:00", "date": d,
                          "open": c, "high": c, "low": c, "close": c,
                          "volume": 1.0, "amount": a}
                         for d, c, a in rows])


def test_build_benchmark_falls_back_to_tdx(_ws_tmp, monkeypatch):
    """东财失败 → 通达信指数通道顶上, 产出逐字段同构(dates/close/amount)。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_qmt_xtdata", lambda: None)   # 只测 tdx 这一路
    seen = []

    def fake_index_kline(code, days=60):
        seen.append((code, days))
        return _tdx_index_df([("2026-09-16", 3860.0, 4.0e11),
                              ("2026-09-17", 3870.0, 4.1e11)])

    monkeypatch.setattr(tdx_source, "get_index_kline", fake_index_kline)
    r = md.build_benchmark(probe=_DeadProbe(), beg="20260101", end="20260918")
    assert r == {"days": 2, "kept_old": False}
    bench = md._load_cache()["benchmark"]
    assert bench["dates"] == ["2026-09-16", "2026-09-17"]
    assert bench["close"] == [3860.0, 3870.0]
    assert bench["amount"] == [4.0e11, 4.1e11]
    assert seen and seen[0][0] == md.BENCHMARK_INDEX_CODE    # 上证指数代码


def test_build_benchmark_tdx_filters_by_beg_end(_ws_tmp, monkeypatch):
    """降级通道也守 beg/end 区间(不得把区间外历史塞进基准段)。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "_qmt_xtdata", lambda: None)
    monkeypatch.setattr(tdx_source, "get_index_kline",
                        lambda code, days=60: _tdx_index_df([
                            ("2025-12-31", 3900.0, 1.0),
                            ("2026-09-16", 3860.0, 4.0e11),
                            ("2026-09-21", 3880.0, 4.2e11)]))
    r = md.build_benchmark(probe=_DeadProbe(), beg="20260101", end="20260918")
    assert md._load_cache()["benchmark"]["dates"] == ["2026-09-16"]
    assert r == {"days": 1, "kept_old": False}


class _FakeQmtXt:
    """假 xtdata: get_market_data_ex 返回单码日K(QMT 索引形如 '20260918')。"""

    def __init__(self, rows):
        import pandas as pd
        self._df = None if rows is None else pd.DataFrame(
            [{"open": c, "high": c, "low": c, "close": c, "volume": 1.0,
              "amount": a} for _, c, a in rows],
            index=[d for d, _, _ in rows])

    def get_market_data_ex(self, empty, codes, period=None, count=None):
        if self._df is None:
            return {}
        return {codes[0]: self._df}


def test_build_benchmark_falls_back_to_qmt_when_tdx_dead(_ws_tmp, monkeypatch):
    """东财 + 通达信都拿不到 → QMT 本地日K顶上(日期归一成东财同构)。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(tdx_source, "get_index_kline", lambda code, days=60: None)
    monkeypatch.setattr(md, "_qmt_xtdata",
                        lambda: _FakeQmtXt([("20260917", 3875.604, 8.687731e11),
                                            ("20260918", 3911.872, 9.941695e11)]))
    r = md.build_benchmark(probe=_DeadProbe(), beg="20260101", end="20260918")
    assert r == {"days": 2, "kept_old": False}
    bench = md._load_cache()["benchmark"]
    assert bench["dates"] == ["2026-09-17", "2026-09-18"]
    assert bench["close"] == [3875.604, 3911.872]
    assert bench["amount"] == [8.687731e11, 9.941695e11]


def test_build_benchmark_qmt_filters_by_beg_end(_ws_tmp, monkeypatch):
    """QMT 通道同样守 beg/end 区间。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(tdx_source, "get_index_kline", lambda code, days=60: None)
    monkeypatch.setattr(md, "_qmt_xtdata",
                        lambda: _FakeQmtXt([("20251231", 3900.0, 1.0),
                                            ("20260918", 3911.872, 9.9e11)]))
    r = md.build_benchmark(probe=_DeadProbe(), beg="20260101", end="20260918")
    assert md._load_cache()["benchmark"]["dates"] == ["2026-09-18"]
    assert r == {"days": 1, "kept_old": False}


def test_build_benchmark_both_sources_fail_keeps_old(_ws_tmp, monkeypatch):
    """三路都拿不到 → 保留旧缓存 + kept_old=True(不写成"拿到了空的")。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    md._save_cache({"benchmark": {"dates": ["2026-09-07"], "close": [3800.0],
                                  "amount": [3e11]}})
    monkeypatch.setattr(tdx_source, "get_index_kline",
                        lambda code, days=60: None)
    monkeypatch.setattr(md, "_qmt_xtdata", lambda: None)
    r = md.build_benchmark(probe=_DeadProbe(), beg="20260101", end="20260918")
    assert r == {"days": 1, "kept_old": True}
    assert md._load_cache()["benchmark"]["close"] == [3800.0]


def test_build_benchmark_fallback_raise_is_fail_open(_ws_tmp, monkeypatch):
    """补充源抛异常也不能炸掉基准任务: 按"取不到"处理, 保留旧缓存。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    md._save_cache({"benchmark": {"dates": ["2026-09-07"], "close": [3800.0],
                                  "amount": [3e11]}})

    def boom(code, days=60):
        raise RuntimeError("pytdx down")

    class _BoomQmt:
        def get_market_data_ex(self, *a, **k):
            raise RuntimeError("qmt down")

    monkeypatch.setattr(tdx_source, "get_index_kline", boom)
    monkeypatch.setattr(md, "_qmt_xtdata", lambda: _BoomQmt())
    r = md.build_benchmark(probe=_DeadProbe(), beg="20260101", end="20260918")
    assert r == {"days": 1, "kept_old": True}
    assert md._load_cache()["benchmark"]["close"] == [3800.0]


def test_build_benchmark_primary_wins_no_fallback_call(_ws_tmp, monkeypatch):
    """原源成功 → 不碰任何降级源(降级链只在原源失败时走)。"""
    from prism import tdx_source
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    data = _resp({"klines": ["2026-07-01,4147.72,30064854350.00"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: data})

    def must_not_call(code, days=60):
        raise AssertionError("原源成功时不得走通达信降级")

    monkeypatch.setattr(tdx_source, "get_index_kline", must_not_call)
    monkeypatch.setattr(md, "_qmt_xtdata",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("原源成功时不得走 QMT 降级")))
    r = md.build_benchmark(probe=p, beg="20260101", end="20260823")
    assert r == {"days": 1, "kept_old": False}
    assert md._load_cache()["benchmark"]["close"] == [4147.72]
