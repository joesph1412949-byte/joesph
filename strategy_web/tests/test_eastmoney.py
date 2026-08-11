# -*- coding: utf-8 -*-
"""eastmoney 单元测试 — 注入假 http_get(罐装 JSON/抛异常), 绝不发真实网络请求"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from eastmoney import EastMoneyError, EastMoneyFeed


class FakeResponse:
    """假 Response-like: .json() 返回罐装 payload, 或按 json_error 抛异常。"""
    def __init__(self, payload, json_error=None):
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeHTTP:
    """假 http_get: by_date 映射 日期→payload。
    payload 为 Exception → 在 http_get 层抛异常(模拟网络失败);
    payload 为 FakeResponse → 直接返回(模拟坏 JSON 响应);
    其余 → 包装为正常响应。未列出的日期默认非交易日(data.pool==null)。"""
    def __init__(self, by_date, default=None):
        self.by_date = by_date
        self.default = default if default is not None else {"data": {"pool": None}}
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        date_yyyymmdd = (params or {}).get("date")
        self.calls.append(date_yyyymmdd)
        payload = self.by_date.get(date_yyyymmdd, self.default)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def _stocks(*pairs):
    """构造涨停池条目: (code, name, boards) -> dict(用 continuousBoardCount 字段)"""
    return [{"c": c, "n": n, "continuousBoardCount": b} for c, n, b in pairs]


# ---------- fetch_limit_up_pool ----------

def test_fetch_limit_up_pool_parses_stocks():
    pool = _stocks(("000001", "平安银行", 1), ("002859", "洁美科技", 5)) + ["garbage"]
    feed = EastMoneyFeed(http_get=FakeHTTP({"20260810": {"data": {"pool": pool}}}))
    stocks = feed.fetch_limit_up_pool("20260810")
    assert stocks == [
        {"code": "000001", "name": "平安银行", "boards": 1},
        {"code": "002859", "name": "洁美科技", "boards": 5},
    ]


def test_fetch_limit_up_pool_empty_on_non_trading():
    # data.pool == null (非交易日) → []
    feed = EastMoneyFeed(http_get=FakeHTTP({"20260809": {"data": {"pool": None}}}))
    assert feed.fetch_limit_up_pool("20260809") == []
    # data == null → []
    feed2 = EastMoneyFeed(http_get=FakeHTTP({"20260808": {"data": None}}))
    assert feed2.fetch_limit_up_pool("20260808") == []
    # 空池(交易日但无涨停) → []
    feed3 = EastMoneyFeed(http_get=FakeHTTP({"20260807": {"data": {"pool": []}}}))
    assert feed3.fetch_limit_up_pool("20260807") == []


def test_fetch_limit_up_pool_raises_on_network_error():
    feed = EastMoneyFeed(http_get=FakeHTTP({"20260810": RuntimeError("connection refused")}))
    with pytest.raises(EastMoneyError):
        feed.fetch_limit_up_pool("20260810")


def test_fetch_limit_up_pool_raises_on_bad_json():
    bad = FakeResponse(None, json_error=ValueError("bad json"))
    feed = EastMoneyFeed(http_get=FakeHTTP({"20260810": bad}))
    with pytest.raises(EastMoneyError):
        feed.fetch_limit_up_pool("20260810")


# ---------- get_market_stats ----------

def test_get_market_stats_builds_counts():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks(("000001", "A", 5), ("000002", "B", 3))}},
        d(1): {"data": {"pool": _stocks(("000010", "X", 1))}},
        d(2): {"data": {"pool": []}},                        # 交易日但无涨停 → 计 0
        d(3): {"data": {"pool": None}},                      # 非交易日 → 跳过
        d(4): {"data": {"pool": _stocks(("000011", "Y", 2))}},
        d(5): {"data": {"pool": _stocks(("000012", "Z", 1))}},
        d(6): {"data": {"pool": _stocks(("000013", "W", 4))}},  # 凑满 5 个交易日
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["max_boards"] == 5               # 今日池最高连板
    assert stats["yesterday_codes"] == ["000010"]  # 最近一个交易日
    assert stats["daily_counts"] == [1, 0, 1, 1, 1]  # 近→远


def test_get_market_stats_empty_recent_day_keeps_yesterday_codes_empty():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    # 回归(复审发现): 最近交易日空池(0家)时, yesterday_codes 必须保持 [],
    # 不能误取更早一天的代码(否则 N3 会算到"前天"), 让 N3 走兜底。
    http = FakeHTTP({
        d(0): {"data": {"pool": []}},
        d(1): {"data": {"pool": []}},                          # 最近交易日: 空池
        d(2): {"data": {"pool": _stocks(("000001", "A", 1))}},  # 更早交易日有码
        d(3): {"data": {"pool": _stocks(("000002", "B", 1))}},
        d(4): {"data": {"pool": _stocks(("000003", "C", 1))}},
        d(5): {"data": {"pool": _stocks(("000004", "D", 1))}},
        d(6): {"data": {"pool": _stocks(("000005", "E", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["yesterday_codes"] == []
    assert stats["daily_counts"] == [0, 1, 1, 1, 1]


def test_get_market_stats_max_boards_zero_on_empty_today():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    http = FakeHTTP({
        d(0): {"data": {"pool": []}},   # 今日无涨停 → max_boards=0
        d(1): {"data": {"pool": _stocks(("000001", "A", 1))}},
        d(2): {"data": {"pool": _stocks(("000002", "B", 1))}},
        d(3): {"data": {"pool": _stocks(("000003", "C", 1))}},
        d(4): {"data": {"pool": _stocks(("000004", "D", 1))}},
        d(5): {"data": {"pool": _stocks(("000005", "E", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["max_boards"] == 0
    assert len(stats["daily_counts"]) == 5


def test_get_market_stats_requires_3_trading_days():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    # 只有 1 个历史交易日(其余默认非交易日) → <3 → None
    http = FakeHTTP({d(0): {"data": {"pool": []}}, d(1): {"data": {"pool": []}}})
    assert EastMoneyFeed(http_get=http).get_market_stats() is None


def test_get_market_stats_none_when_today_fails():
    today = date.today()
    http = FakeHTTP({today.strftime("%Y%m%d"): RuntimeError("down")})
    assert EastMoneyFeed(http_get=http).get_market_stats() is None


def test_get_market_stats_skips_failed_history_days():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    # 中间某天请求失败 → 跳过该日历日继续, 其余 5 天凑齐
    http = FakeHTTP({
        d(0): {"data": {"pool": []}},
        d(1): RuntimeError("transient"),
        d(2): {"data": {"pool": []}},
        d(3): {"data": {"pool": []}},
        d(4): {"data": {"pool": []}},
        d(5): {"data": {"pool": []}},
        d(6): {"data": {"pool": []}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["daily_counts"] == [0, 0, 0, 0, 0]


def test_get_market_stats_never_raises():
    # 全网络失败 → 返回 None 而非抛异常
    today = date.today()
    http = FakeHTTP({today.strftime("%Y%m%d"): RuntimeError("down")})
    feed = EastMoneyFeed(http_get=http)
    assert feed.get_market_stats() is None
