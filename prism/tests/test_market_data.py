# -*- coding: utf-8 -*-
"""market_data 单元测试 — 采集解析/缓存/查询/索引, 全离线(注入假 http_get)。"""
import sys
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


def test_fetch_sector_quotes():
    data = _resp({"diff": [{"f12": "BK0475", "f3": 2.31, "f6": 1.2e10,
                            "f8": 3.4, "f62": 5.6e8}]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: data})
    q = p.fetch_sector_quotes(["BK0475"])
    assert q["BK0475"]["pct"] == 2.31
    assert q["BK0475"]["main_net_in"] == 5.6e8


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


def test_fetch_global_indices():
    data = _resp({"diff": [{"f12": "NDX", "f14": "纳斯达克",
                            "f2": 26180.45, "f3": 0.43}]})
    p, _ = _fake_probe({md.EastMoneyProbe.ULIST_URL: data})
    g = p.fetch_global_indices()
    assert g["NDX"]["close"] == 26180.45
    assert g["NDX"]["pct"] == 0.43


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
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")

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

    # 查询接口
    assert md.sector_close_on("BK0475", "2026-07-01") == 100.0
    assert md.sector_close_on("BK0475", "2026-07-02") == 101.0
    assert md.sector_close_on("BK0475", "2026-07-03") is None     # 无此日
    assert md.sector_flow_on("BK0475", "2026-07-01") == 50.0
    assert md.sector_flow_on("BK9999", "2026-07-01") is None      # 无此板块
    # 索引
    idx = md.build_index()
    assert "2026-07-01" in idx
    assert idx["2026-07-01"]["sector_close"]["BK0475"] == 100.0
    assert idx["2026-07-01"]["sector_flow"]["BK0475"] == 50.0


def test_build_sector_cache_skips_existing(_ws_tmp, monkeypatch):
    """增量: 已采集的板块不再重复请求。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
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
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
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


def test_build_global_cache(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    kline = _resp({"klines": ["2026-07-01,18000.0", "2026-07-02,18100.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: kline})
    g = md.build_global_cache(probe=p, beg="20260101", end="20260823")
    assert "NDX" in g
    assert md.index_close_on("NDX", "2026-07-02") == 18100.0
    assert md.index_close_on("NDX", "2026-07-03") is None


def test_day_snapshot_asof(_ws_tmp, monkeypatch):
    """asof 语义: 只返回该日及之前的数据(防未来函数)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    clist = _resp({"total": 1, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    kline = _resp({"klines": ["2026-07-01,100.0,1000.0",
                              "2026-07-02,101.0,1100.0"]})
    flow = _resp({"klines": ["2026-07-01,50.0,1.0,2.0,3.0,4.0",
                             "2026-07-02,60.0,1.0,2.0,3.0,4.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: clist,
                        md.EastMoneyProbe.KLINE_URL: kline,
                        md.EastMoneyProbe.FFLOW_URL: flow})
    md.build_sector_cache(probe=p, beg="20260101", end="20260823")
    md.build_index()
    snap = md.day_snapshot("20260701")
    assert snap["sector_close"]["BK0475"] == 100.0
    sn2 = md.day_snapshot("20260703")   # 无此日 → 空快照(不掺未来)
    assert sn2["sector_close"] == {}
    assert "BK0475" not in sn2["sector_close"]


def test_no_cache_returns_empty(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "none.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "none_idx.pkl")
    assert md.sector_close_on("BK0475", "2026-07-01") is None
    assert md.sector_flow_on("BK0475", "2026-07-01") is None
    assert md.index_close_on("NDX", "2026-07-01") is None
    assert md.day_snapshot("20260701") == {"sector_close": {},
                                           "sector_flow": {},
                                           "global": {}}


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
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    f = md.SWIndexFeed(ak=FakeSWAK())
    r = md.build_sector_cache(probe=f, beg="20260101", end="20260823",
                              source="sw")
    assert r["sectors"] == 2
    assert r["kline_codes"] == 1       # 只有 801010 有K线
    assert r["flow_codes"] == 0        # 申万无资金流
    assert md.sector_close_on("801010", "2026-07-01") == 1005.0
    assert md.sector_flow_on("801010", "2026-07-01") is None
    # 索引也构建成功
    idx = md.build_index()
    assert idx["2026-07-01"]["sector_close"]["801010"] == 1005.0


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
    assert md.index_close_on("NDX", "2026-07-02") == 17180.0
    assert md.index_close_on("UDI", "2026-07-02") is None


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
    # 查询: 带后缀/裸代码都命中
    assert md.stock_sector("000019.SZ") == "801010"
    assert md.stock_sector("000019") == "801010"
    assert md.stock_sector("600519") is None       # 不在映射
    assert md._code6("bad") is None
    assert md._code6("000019.SZ") == "000019"


def test_stock_sector_no_cache(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "none.pkl")
    assert md.stock_sector("000019") is None


def test_rebuild_clears_old_kline(_ws_tmp, monkeypatch):
    """rebuild=True: 切换数据源时清空旧体系K线/资金流。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
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
    assert md.sector_close_on("801010", "2026-07-01") is None


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
    assert md.index_close_on("US10Y", "2026-07-01") == 4.25
    assert md.index_close_on("VIX", "2026-07-02") == 4.28


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
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
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
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    data = _resp({"total": 2, "diff": [
        {"f12": "BK0001", "f14": "甲", "f62": "-"},
        {"f12": "BK0002", "f14": "乙", "f62": "-"}]})
    p = md.EastMoneyProbe(http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: data}))
    r = md.build_flow_rank(probe=p)
    assert r == {"dates": 0, "sectors_today": 0}
    assert (md._load_cache().get("flow_rank") or {}) == {}


def test_build_benchmark(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    data = _resp({"klines": ["2026-07-01,4147.72,30064854350.00",
                             "2026-07-02,4160.67,32598801572.00"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: data})
    r = md.build_benchmark(probe=p)
    assert r == {"days": 2, "kept_old": False}
    assert md._load_cache()["benchmark"]["close"][0] == 4147.72


def test_build_benchmark_keeps_old_on_failure(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    md._save_cache({"benchmark": {"dates": ["2026-07-01"], "close": [4000.0],
                                  "amount": [1e10]}})
    p, _ = _fake_probe({}, fail_urls=[md.EastMoneyProbe.KLINE_URL])
    r = md.build_benchmark(probe=p)
    assert r["kept_old"] is True
    assert md._load_cache()["benchmark"]["close"] == [4000.0]


def test_mkt_snapshot_new_segments(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
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
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
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
    """flow_rank 被封(抛 MarketDataError) → benchmark 照常执行(终审 I-1 补强)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-benchmark",
                                     "--build-flow-rank"])

    def boom(probe=None):
        raise md.MarketDataError("push2 banned")

    ran = []
    monkeypatch.setattr(md, "build_flow_rank", boom)
    monkeypatch.setattr(md, "build_benchmark",
                        lambda probe=None, beg=None, end=None:
                        ran.append("bm") or {"days": 1, "kept_old": False})
    md.build_cli()
    assert ran == ["bm"]           # benchmark 不被连累
    out = capsys.readouterr().out
    assert "资金惯性快照失败" in out


def test_cli_single_flag_failure_exits_nonzero(_ws_tmp, monkeypatch):
    """只点名 flow_rank 且失败 → 非零退出(不静默吞, 自动化可感知)。"""
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    monkeypatch.setattr("sys.argv", ["market_data.py", "--build-flow-rank"])

    def boom(probe=None):
        raise md.MarketDataError("push2 banned")

    monkeypatch.setattr(md, "build_flow_rank", boom)
    with pytest.raises(SystemExit) as ei:
        md.build_cli()
    assert "flow_rank" in str(ei.value)


# ---------------------------------------------------------------- ETF 行情(W1)

class FakeDF:
    """get_market_data_ex 单码返回值: 仅支持 df["col"] 取列(实现只用这个)。"""

    def __init__(self, cols):
        self._cols = cols

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
        for c in codes:
            self.downloads.append(c)
            if c in self.fail_dl:
                raise RuntimeError("dl boom: %s" % c)
        self.local.update({c: self.pending.pop(c)
                           for c in codes if c in self.pending})


TWO_BARS = {"close": [1.00, 1.05], "amount": [9e8, 1.1e9]}


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
