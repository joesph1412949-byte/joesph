# -*- coding: utf-8 -*-
"""eastmoney 单元测试 — 注入假 http_get(罐装 JSON/抛异常), 绝不发真实网络请求"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import eastmoney
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
    """构造涨停池条目: (code, name, boards) -> dict(用东财真实字段 lbc=连板数)"""
    return [{"c": c, "n": n, "lbc": b} for c, n, b in pairs]


def _n_stocks(n, prefix):
    """构造 n 条涨停池条目(代码 = prefix + 3 位序号)。"""
    return _stocks(*[("%s%03d" % (prefix, i), "S%d" % i, 1) for i in range(n)])


def _fixed_today(monkeypatch, iso):
    """把 eastmoney 模块里的 date.today() 钉到指定日历日(离线复现"周一/周末/长假"场景)。

    get_market_stats 只调用 date.today(), 因此覆盖模块级 date 即可完全离线复现审计实况,
    且不受测试当天是星期几影响。"""
    fixed = date(*[int(x) for x in iso.split("-")])

    class _FixedDate:
        @staticmethod
        def today():
            return fixed

    monkeypatch.setattr(eastmoney, "date", _FixedDate)
    return fixed


# ---------- fetch_limit_up_pool ----------

def test_fetch_limit_up_pool_parses_stocks():
    pool = _stocks(("000001", "平安银行", 1), ("002859", "洁美科技", 5)) + ["garbage"]
    feed = EastMoneyFeed(http_get=FakeHTTP({"20260810": {"data": {"pool": pool}}}))
    stocks = feed.fetch_limit_up_pool("20260810")
    assert stocks == [
        {"code": "000001", "name": "平安银行", "boards": 1, "theme": ""},
        {"code": "002859", "name": "洁美科技", "boards": 5, "theme": ""},
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
#
# 空池语义(2026-09-19 修复): 东财涨停池对**非交易日与超保留期**返回的是空列表 []
# 而不是 null(真打公开端点实测, 见本文件 test_get_market_stats_weekend_* 的对照注释):
#   data 缺失 / data.pool == null → 显式无数据(非交易日) → 跳过;
#   data.pool == []               → "该日历日没有可用涨停池"。既可能是非交易日/超保留期,
#                                   也可能是(极罕见)真交易日 0 家涨停 —— 接口上**不可分辨**,
#                                   故按"没有数据的日历日"处理: 不占 5 日配额、不计 0、
#                                   不当"昨日"。判据写进 get_market_stats docstring:
#                                   连续空池由 20 个日历日回溯上限兜底(全空 → 不足 3 个
#                                   有效交易日 → None), **不**用"连续 ≥2 个空池即停" ——
#                                   周一/节后首日必须先跨过周末/长假才能找到最近交易日,
#                                   在 2 连空处停下正好卡在周末(正是本 bug 的成因)。

def test_empty_pool_stays_distinguishable_from_missing_data():
    """三种"空"在低层必须保持可区分: [] (无可用池) vs None (显式无数据/pool==null)。

    网络失败仍由 EastMoneyError 抛出(不落入任何一种空), 见
    test_fetch_limit_up_pool_raises_on_network_error。"""
    feed = EastMoneyFeed(http_get=FakeHTTP({
        "20260810": {"data": {"pool": []}},
        "20260811": {"data": {"pool": None}},
        "20260812": {"data": None},
    }))
    assert feed._get_pool_data("20260810") == []
    assert feed._get_pool_data("20260811") is None
    assert feed._get_pool_data("20260812") is None


def test_get_market_stats_builds_counts():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks(("000001", "A", 5), ("000002", "B", 3))}},
        d(1): {"data": {"pool": _stocks(("000010", "X", 1))}},
        d(2): {"data": {"pool": None}},                      # 显式 null(非交易日) → 跳过
        d(3): {"data": {"pool": None}},                      # 同上
        d(4): {"data": {"pool": _stocks(("000011", "Y", 2))}},
        d(5): {"data": {"pool": _stocks(("000012", "Z", 1))}},
        d(6): {"data": {"pool": _stocks(("000013", "W", 4))}},
        d(7): {"data": {"pool": _stocks(("000014", "V", 1))}},  # 凑满 5 个有效交易日
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["max_boards"] == 5               # 今日池最高连板
    assert stats["yesterday_codes"] == ["000010"]  # 最近一个有数据的交易日
    assert stats["daily_counts"] == [1, 1, 1, 1, 1]  # 近→远


def test_get_market_stats_weekend_empty_pools_are_not_trading_days(monkeypatch):
    """R1 核心(离线复现审计实况): 周一 asof 回溯先撞上周末。

    真打东财 getTopicZTPool(2026-09-19 只读对照)实测:
      20260911(周五) = 40 家 / 20260912(周六) = 0 家 / 20260913(周日) = 0 家
      (20260820 超保留期 = 0 家)。即非交易日/超期返回的是**空列表而非 null**。

    修复前: 周日、周六各被当成"0 家涨停的交易日"计入 → daily_counts 前两位是 0,
    且 first_day_found 落在周日 → yesterday_codes=[] ⇒ N3 真数据直接失效(走兜底)。
    """
    _fixed_today(monkeypatch, "2026-09-14")   # 周一
    fri = _n_stocks(40, "600")
    http = FakeHTTP({
        "20260914": {"data": {"pool": _stocks(("000001", "T", 3))}},  # 今日(周一)
        "20260913": {"data": {"pool": []}},                     # 周日: 空池(非交易日)
        "20260912": {"data": {"pool": []}},                     # 周六: 空池(非交易日)
        "20260911": {"data": {"pool": fri}},                    # 周五 40 家 ← 真正的"昨日"
        "20260910": {"data": {"pool": _n_stocks(35, "601")}},   # 周四
        "20260909": {"data": {"pool": _n_stocks(48, "602")}},   # 周三
        "20260908": {"data": {"pool": _n_stocks(73, "603")}},   # 周二
        "20260907": {"data": {"pool": _n_stocks(93, "604")}},   # 周一
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["daily_counts"] == [40, 35, 48, 73, 93], \
        "空池不能当交易日: 周末不得吃掉 daily_counts 的前两位"
    assert stats["yesterday_codes"] == [s["c"] for s in fri], \
        "周一 asof 的'昨日'必须是周五(最近有数据的交易日) —— 否则 N3 真数据失效"


def test_get_market_stats_crosses_long_holiday_gap(monkeypatch):
    """R1 判据: 不设"连续 ≥2 个空池即停" —— 节后首日必须跨过长假找到节前交易日。

    2026-02-24(周二, 春节后首日)场景: 0213(节前最后交易日)与今日之间有 10 个空池
    日历日(周末 + 春节假期)。修复前它们被计成 0 家涨停的交易日 → 5 日配额被假日吃掉;
    修复后照常跳过, 节前交易日成为"昨日"。
    """
    _fixed_today(monkeypatch, "2026-02-24")
    http = FakeHTTP({
        "20260224": {"data": {"pool": _stocks(("000001", "T", 2))}},
        "20260213": {"data": {"pool": _n_stocks(30, "610")}},
        "20260212": {"data": {"pool": _n_stocks(20, "611")}},
        "20260211": {"data": {"pool": _n_stocks(25, "612")}},
        "20260210": {"data": {"pool": _n_stocks(28, "613")}},
        "20260209": {"data": {"pool": _n_stocks(33, "614")}},
    }, default={"data": {"pool": []}})       # 其余日历日(春节假期+周末)全为空池
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["daily_counts"] == [30, 20, 25, 28, 33]
    assert stats["yesterday_codes"] == [s["c"] for s in _n_stocks(30, "610")]


def test_get_market_stats_all_empty_window_returns_none(monkeypatch):
    """R1: 连续空池 = 越过保留期边界(或整体取不到数) → 不能伪装成"天天 0 家涨停"。

    真打实测: 20260803..20260826 连续 18 个交易日全返回空列表(超保留期; 保留期边界
    实测落在 20260828(空) 与 20260831(88 家) 之间)。20 个日历日全空 → 0 个有效
    交易日 → None(调用方回落兜底); 绝不返回 daily_counts=[0,0,0] 让 N1 拿
    "0 vs 0 均值"当真数据。
    """
    _fixed_today(monkeypatch, "2026-09-14")
    http = FakeHTTP({}, default={"data": {"pool": []}})
    assert EastMoneyFeed(http_get=http).get_market_stats() is None


def test_get_market_stats_yesterday_is_latest_data_day():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    # 口径变更(2026-09-19): "昨日" = 最近一个**有数据**的交易日。
    # 旧测试断言"最近交易日空池 → yesterday_codes 保持 []", 其前提是"空池 = 真交易日
    # 0 家涨停"; 真打端点实测推翻该前提(空池来自非交易日/超保留期)。若保留旧行为,
    # 周一/节后首日必然取到 [] → N3 真数据全程失效。这里锁新口径(与 backtest/cli.py
    # 按交易日历取 prev_pool 的口径一致)。
    http = FakeHTTP({
        d(0): {"data": {"pool": []}},
        d(1): {"data": {"pool": []}},                          # 空池(非交易日/超期) → 跳过
        d(2): {"data": {"pool": _stocks(("000001", "A", 1))}},  # 最近有数据的交易日
        d(3): {"data": {"pool": _stocks(("000002", "B", 1))}},
        d(4): {"data": {"pool": _stocks(("000003", "C", 1))}},
        d(5): {"data": {"pool": _stocks(("000004", "D", 1))}},
        d(6): {"data": {"pool": _stocks(("000005", "E", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["yesterday_codes"] == ["000001"]
    assert stats["daily_counts"] == [1, 1, 1, 1, 1]


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

    # 只有 2 个"有数据"的历史交易日(其余默认非交易日) → <3 → None
    http = FakeHTTP({d(0): {"data": {"pool": _stocks(("000001", "A", 1))}},
                     d(1): {"data": {"pool": _stocks(("000002", "B", 1))}},
                     d(2): {"data": {"pool": _stocks(("000003", "C", 1))}}})
    assert EastMoneyFeed(http_get=http).get_market_stats() is None


def test_get_market_stats_none_when_today_fails():
    today = date.today()
    http = FakeHTTP({today.strftime("%Y%m%d"): RuntimeError("down")})
    assert EastMoneyFeed(http_get=http).get_market_stats() is None


def test_get_market_stats_skips_failed_history_days():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    # R3: 网络失败(异常) ≠ 空池 ≠ 交易日。失败日跳过该日历日继续(既有 fail-open 方向),
    # 且**不计 0、不占配额、不当"昨日"** —— (c) 绝不能伪装成 (a)/(b) 的数据日。
    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks(("000001", "T", 1))}},
        d(1): RuntimeError("transient"),
        d(2): {"data": {"pool": _stocks(("000010", "X", 1))}},
        d(3): {"data": {"pool": _stocks(("000011", "Y", 1))}},
        d(4): {"data": {"pool": _stocks(("000012", "Z", 1))}},
        d(5): {"data": {"pool": _stocks(("000013", "W", 1))}},
        d(6): {"data": {"pool": _stocks(("000014", "V", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["daily_counts"] == [1, 1, 1, 1, 1]
    assert stats["yesterday_codes"] == ["000010"], "失败日不是交易日 → 最近有效日 = d(2)"


def test_get_market_stats_never_raises():
    # 全网络失败 → 返回 None 而非抛异常
    today = date.today()
    http = FakeHTTP({today.strftime("%Y%m%d"): RuntimeError("down")})
    feed = EastMoneyFeed(http_get=http)
    assert feed.get_market_stats() is None


def test_get_market_stats_yesterday_boards_collected():
    # 回归(修复): N4 晋级率需要"昨日连板≥2"的代码列表 → get_market_stats 必须携带。
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks(("000001", "A", 4), ("000002", "B", 1))}},
        d(1): {"data": {"pool": _stocks(("000010", "X", 2), ("000011", "Y", 1),
                                         ("000012", "Z", 5))}},
        d(2): {"data": {"pool": None}},
        d(3): {"data": {"pool": _stocks(("000020", "W", 1))}},
        d(4): {"data": {"pool": _stocks(("000021", "V", 1))}},
        d(5): {"data": {"pool": _stocks(("000022", "U", 1))}},
        d(6): {"data": {"pool": _stocks(("000023", "T", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    # 最近交易日(d1)涨停池: 000010(2板) / 000011(1板) / 000012(5板)
    # → yesterday_codes 全部, yesterday_boards 只收连板≥2 的
    assert stats["yesterday_codes"] == ["000010", "000011", "000012"]
    assert stats["yesterday_boards"] == ["000010", "000012"]


def test_get_market_stats_yesterday_boards_empty_when_all_first_boards():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks(("000001", "A", 1))}},
        d(1): {"data": {"pool": _stocks(("000010", "X", 1), ("000011", "Y", 1))}},
        d(2): {"data": {"pool": None}},
        d(3): {"data": {"pool": _stocks(("000020", "W", 1))}},
        d(4): {"data": {"pool": _stocks(("000021", "V", 1))}},
        d(5): {"data": {"pool": _stocks(("000022", "U", 1))}},
        d(6): {"data": {"pool": _stocks(("000023", "T", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["yesterday_codes"] == ["000010", "000011"]
    assert stats["yesterday_boards"] == []   # 昨日全是首板(1板)


# ---------- 题材主线聚合(hybk) ----------

def _stocks_with_theme(*triples):
    """构造带题材的东财**原始**涨停池条目: (code, name, boards, theme) → {c,n,lbc,hybk}。
    供 get_market_stats(内部会再过 _pool_to_stocks)直接使用。"""
    return [{"c": c, "n": n, "lbc": b, "hybk": t} for c, n, b, t in triples]


def test_aggregate_by_theme_ranks_by_count_then_boards():
    # 走完整转换链路: 原始东财池 → _pool_to_stocks → aggregate_by_theme
    raw = _stocks_with_theme(
        ("000001", "A", 1, "机器人"),
        ("000002", "B", 2, "机器人"),
        ("000003", "C", 5, "机器人"),
        ("000010", "X", 1, "AI算力"),
        ("000011", "Y", 1, "AI算力"),
        ("000020", "W", 3, "低空经济"),
    )
    stocks = EastMoneyFeed._pool_to_stocks(raw)
    themes = EastMoneyFeed.aggregate_by_theme(stocks)
    assert themes[0]["theme"] == "机器人"      # 3家 > 2家
    assert themes[0]["count"] == 3
    assert themes[0]["max_boards"] == 5
    assert themes[1]["theme"] == "AI算力"
    assert themes[1]["max_boards"] == 1        # 同级按连板高度排序
    assert themes[2]["theme"] == "低空经济"
    assert set(themes[0]["codes"]) == {"000001", "000002", "000003"}


def test_aggregate_by_theme_unknown_bucket_last():
    raw = _stocks_with_theme(
        ("000001", "A", 1, "机器人"),
        ("000002", "B", 1, ""),       # 无题材
        ("000003", "C", 1, None),     # 无题材
    )
    stocks = EastMoneyFeed._pool_to_stocks(raw)
    themes = EastMoneyFeed.aggregate_by_theme(stocks)
    assert themes[0]["theme"] == "机器人"
    assert themes[-1]["theme"] == "未知"       # 无题材聚到最后
    assert themes[-1]["count"] == 2


def test_aggregate_by_theme_empty():
    assert EastMoneyFeed.aggregate_by_theme([]) == []
    assert EastMoneyFeed.aggregate_by_theme(None) == []


def test_get_market_stats_includes_theme_map_and_top_themes():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks_with_theme(
            ("000001", "A", 1, "机器人"), ("000002", "B", 3, "机器人"))}},
        d(1): {"data": {"pool": _stocks(("000010", "X", 1))}},
        d(2): {"data": {"pool": _stocks(("000020", "W", 1))}},
        d(3): {"data": {"pool": _stocks(("000021", "V", 1))}},
        d(4): {"data": {"pool": _stocks(("000022", "U", 1))}},
        d(5): {"data": {"pool": _stocks(("000023", "T", 1))}},
        d(6): {"data": {"pool": _stocks(("000024", "S", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    # 今日涨停池: 000001/000002 都属于"机器人" → theme_map + top_themes
    assert stats["today_theme_map"] == {"000001": "机器人", "000002": "机器人"}
    assert stats["top_themes"][0]["theme"] == "机器人"
    assert stats["top_themes"][0]["count"] == 2
    assert stats["top_themes"][0]["max_boards"] == 3


def test_get_market_stats_theme_fields_empty_without_hybk():
    today = date.today()

    def d(offset):
        return (today - timedelta(days=offset)).strftime("%Y%m%d")

    # 东财涨停池条目无 hybk 字段 → theme_map 为空, top_themes 为 [未知]
    http = FakeHTTP({
        d(0): {"data": {"pool": _stocks(("000001", "A", 1), ("000002", "B", 2))}},
        d(1): {"data": {"pool": _stocks(("000010", "X", 1))}},
        d(2): {"data": {"pool": _stocks(("000020", "W", 1))}},
        d(3): {"data": {"pool": _stocks(("000021", "V", 1))}},
        d(4): {"data": {"pool": _stocks(("000022", "U", 1))}},
        d(5): {"data": {"pool": _stocks(("000023", "T", 1))}},
        d(6): {"data": {"pool": _stocks(("000024", "S", 1))}},
    })
    stats = EastMoneyFeed(http_get=http).get_market_stats()
    assert stats is not None
    assert stats["today_theme_map"] == {}
    assert stats["top_themes"] == [{"theme": "未知", "count": 2, "max_boards": 2,
                                    "codes": ["000001", "000002"]}]
