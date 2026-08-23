# -*- coding: utf-8 -*-
"""market_data 单元测试 — 采集解析/缓存/查询/索引, 全离线(注入假 http_get)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.market_data as md


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

def test_build_sector_cache_and_query(tmp_path, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", tmp_path / "mkt_idx.pkl")

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


def test_build_sector_cache_skips_existing(tmp_path, monkeypatch):
    """增量: 已采集的板块不再重复请求。"""
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", tmp_path / "mkt_idx.pkl")
    clist = _resp({"total": 1, "diff": [{"f12": "BK0475", "f14": "银行"}]})
    p, g = _fake_probe({md.EastMoneyProbe.CLIST_URL: clist})
    r1 = md.build_sector_cache(probe=p, beg="20260101", end="20260823")
    assert r1["kline_codes"] == 0   # 无K线数据(只做了列表), 但列表已存
    # 再次运行: 列表刷新, 无K线请求
    r2 = md.build_sector_cache(probe=p, beg="20260101", end="20260823")
    assert r2["sectors"] == 1


def test_build_global_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "mkt.pkl")
    kline = _resp({"klines": ["2026-07-01,18000.0", "2026-07-02,18100.0"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: kline})
    g = md.build_global_cache(probe=p, beg="20260101", end="20260823")
    assert "NDX" in g
    assert md.index_close_on("NDX", "2026-07-02") == 18100.0
    assert md.index_close_on("NDX", "2026-07-03") is None


def test_day_snapshot_asof(tmp_path, monkeypatch):
    """asof 语义: 只返回该日及之前的数据(防未来函数)。"""
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", tmp_path / "mkt_idx.pkl")
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


def test_no_cache_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", tmp_path / "none.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", tmp_path / "none_idx.pkl")
    assert md.sector_close_on("BK0475", "2026-07-01") is None
    assert md.sector_flow_on("BK0475", "2026-07-01") is None
    assert md.index_close_on("NDX", "2026-07-01") is None
    assert md.day_snapshot("20260701") == {"sector_close": {},
                                           "sector_flow": {},
                                           "global": {}}