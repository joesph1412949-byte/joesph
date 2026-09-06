# -*- coding: utf-8 -*-
"""fundamental 单元测试 — 注入假 http_get(按 URL/参数路由罐装 JSON/抛异常), 绝不发真实请求。
Y5(slist)/F7(ztpool)/Y7、Y2(datacenter)/Y6(ann) 响应形状各异, FakeHTTP 按 URL/参数子串分发。
"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fundamental import FundamentalFeed, _code6, _secid


class FakeResp:
    """假 Response-like: .json() 返回罐装 payload。"""

    def __init__(self, payload, error=None):
        self._p = payload
        self._e = error

    def raise_for_status(self):
        if self._e:
            raise self._e

    def json(self):
        return self._p


class FakeHTTP:
    """按 URL/参数子串路由的假 http_get。
    routes: {匹配子串: payload 或 callable(params)->payload}。
      - payload 为 Exception → 在 http_get 层抛异常(模拟网络失败)
      - payload 为 dict → 固定响应体
      - callable → 以 params 为参求响应体(用于 ztpool 按日期分发)
    未匹配任何路由 → AssertionError(防未预期请求)。"""
    def __init__(self, routes=None, default_exc=None):
        self.routes = routes or {}
        self.default_exc = default_exc
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if self.default_exc:
            raise self.default_exc
        probe = url + " " + repr(params or {})
        for key, payload in self.routes.items():
            if key in probe:
                if isinstance(payload, Exception):
                    raise payload
                if callable(payload):
                    payload = payload(params)
                return FakeResp(payload)
        raise AssertionError("未预期的请求: %s %r" % (url, params))


def _ztpool_handler(pool_by_date):
    """ztpool 路由: 按 params.date 分发; 未列日期默认非交易日(data.pool=null)。"""
    def handler(params):
        p = pool_by_date.get((params or {}).get("date"))
        if isinstance(p, Exception):
            raise p
        return {"data": {"pool": p}}
    return handler


# ---------- 日期工具(相对今日, 模块用 date.today()) ----------
TODAY = date.today()


def d(offset):
    return (TODAY - timedelta(days=offset)).strftime("%Y-%m-%d")


def dk(offset):
    return (TODAY - timedelta(days=offset)).strftime("%Y%m%d")


# ---------- 罐装数据构造 ----------
def _concept_board(name):
    """slist data.diff 条目(f14=板块名)。"""
    return {"f14": name}


def _lhb_row(code, trd_date, net_amt):
    """龙虎榜 result.data 行。"""
    return {"SECURITY_CODE": code, "TRADE_DATE": trd_date + " 00:00:00",
            "BILLBOARD_NET_AMT": net_amt}


def _holder_row(code, change):
    """股东户数 result.data 行。"""
    return {"SECURITY_CODE": code, "HOLDER_NUM": 100000,
            "PRE_HOLDER_NUM": 100000 - change, "HOLDER_NUM_CHANGE": change}


def _ann_row(title, notice_date):
    """公告 data.list 条目。"""
    return {"title": title, "notice_date": notice_date + " 00:00:00", "art_code": "x"}


def _pool_row(code, hybk):
    """涨停池 data.pool 条目。"""
    return {"c": code, "n": "测试股", "hybk": hybk}


def _base_routes(pool_by_date=None, lhb=None, gdhs=None, ann=None, slist_diff=None):
    """给所有因子端点提供可用默认路由(空数据→各因子 0/None, 不抛), 按需覆写。"""
    return {
        "slist": {"data": {"diff": slist_diff or [], "total": len(slist_diff or [])}},
        "getTopicZTPool": _ztpool_handler(pool_by_date or {}),
        "RPT_DAILYBILLBOARD_DETAILSNEW": {"result": {"data": lhb or []}},
        "RPT_HOLDERNUMLATEST": {"result": {"data": gdhs or []}},
        "np-anotice": {"data": {"list": ann or []}},
    }


# ---------- Y1 小市值(纯计算) ----------
def test_y1_small_cap_threshold():
    f = FundamentalFeed(http_get=FakeHTTP())
    assert f._small_cap(79e8)["score"] == 1
    assert f._small_cap(80e8)["score"] == 0
    assert f._small_cap(None) is None


# ---------- Y8 中市值(纯计算) ----------
def test_y8_mid_cap_threshold():
    f = FundamentalFeed(http_get=FakeHTTP())
    assert f._mid_cap(29e8)["score"] == 0    # < 30亿 → 非中市值
    assert f._mid_cap(30e8)["score"] == 1    # 30亿 边界含
    assert f._mid_cap(48e8)["score"] == 1    # 艾艾精工类 48亿
    assert f._mid_cap(99e8)["score"] == 1    # < 100亿
    assert f._mid_cap(100e8)["score"] == 0   # >= 100亿 → 非中市值
    assert f._mid_cap(None) is None


# ---------- secid 辅助 ----------
def test_secid_market_mapping():
    assert _secid("000001.SZ") == "0.000001"
    assert _secid("002859.SZ") == "0.002859"
    assert _secid("600000.SH") == "1.600000"
    assert _secid("830799.BJ") == "0.830799"
    assert _secid("000001") == "0.000001"   # 裸代码按首位推断(深)
    assert _secid("600000") == "1.600000"   # 裸代码 6 开头(沪)
    assert _code6("000001.SZ") == "000001"
    assert _code6("600000.SH") == "600000"


# ---------- Y5 多概念(slist) ----------
def test_y5_concepts_hit():
    diff = [_concept_board("储能"), _concept_board("机器人"), _concept_board("低空经济")]
    http = FakeHTTP(_base_routes(slist_diff=diff))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y5"]["score"] == 1
    assert "3" in out["Y5"]["note"]


def test_y5_concepts_miss():
    diff = [_concept_board("储能"), _concept_board("机器人")]
    http = FakeHTTP(_base_routes(slist_diff=diff))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y5"]["score"] == 0


# ---------- F7 题材新颖(ztpool) ----------
def test_f7_novel_concept_hit():
    # 今日题材不在近 N-1 个交易日的题材集合 → 新颖 → 1
    pool_by_date = {
        dk(0): [_pool_row("000001", "全新技术题材")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(2): None,                                     # 非交易日, 跳过
        dk(3): [_pool_row("999999", "旧题材B")],
        dk(4): [_pool_row("999999", "旧题材C")],
        dk(5): [_pool_row("999999", "旧题材D")],
    }
    http = FakeHTTP(_base_routes(pool_by_date=pool_by_date))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["F7"]["score"] == 1


def test_f7_novel_concept_miss():
    # 今日题材在历史集合里出现过 → 不新颖 → 0
    pool_by_date = {
        dk(0): [_pool_row("000001", "旧题材B")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(2): None,
        dk(3): [_pool_row("999999", "旧题材B")],
        dk(4): [_pool_row("999999", "旧题材C")],
        dk(5): [_pool_row("999999", "旧题材D")],
    }
    http = FakeHTTP(_base_routes(pool_by_date=pool_by_date))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["F7"]["score"] == 0


def test_f7_stock_not_in_today_pool_fail_open():
    # 今日池里没有该股 → 无今日题材可判 → F7 跳过(fail-open)
    pool_by_date = {
        dk(0): [_pool_row("999999", "其他题材")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(3): [_pool_row("999999", "旧题材B")],
        dk(4): [_pool_row("999999", "旧题材C")],
        dk(5): [_pool_row("999999", "旧题材D")],
    }
    http = FakeHTTP(_base_routes(pool_by_date=pool_by_date))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert "F7" not in out


def test_f7_history_fetch_error_fail_open():
    # 历史日 ztpool 端点失败(抛异常, 而非 data.pool==null 非交易日) → F7 整体 fail-open:
    # 该失败日不能当"无该题材"(否则会假判新颖), 直接跳过 F7, 且不崩。
    pool_by_date = {
        dk(0): [_pool_row("000001", "全新技术题材")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(2): RuntimeError("ztpool timeout"),   # 端点失败, 不是非交易日
    }
    http = FakeHTTP(_base_routes(pool_by_date=pool_by_date))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert "F7" not in out


# ---------- Y7 游资现身(龙虎榜) ----------
def test_y7_dragon_tiger_hit():
    lhb = [_lhb_row("000001", d(3), 5000000), _lhb_row("000001", d(1), 1.2e6)]
    http = FakeHTTP(_base_routes(lhb=lhb))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y7"]["score"] == 1


def test_y7_dragon_tiger_miss():
    # 净买入 <=0 或 窗口外(>RECENT_DAYS 天) → 0
    lhb = [_lhb_row("000001", d(1), -200000), _lhb_row("000001", d(30), 500000)]
    http = FakeHTTP(_base_routes(lhb=lhb))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y7"]["score"] == 0


def test_y7_asof_excludes_future_rows():
    """防未来函数: 榜单日 > asof 的记录绝不能计入(回测场景)。

    榜单在 asof 之后 1 天(未来数据), 即使净买入为正也不得命中。
    """
    future = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    lhb = [_lhb_row("000001", future, 5000000)]
    http = FakeHTTP(_base_routes(lhb=lhb))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ", asof=TODAY)
    assert out["Y7"]["score"] == 0


def test_compute_for_stock_cache_scoped_by_asof():
    """缓存按 asof 分日: 同一只股不同基准日是不同快照, 不串数据。"""
    http = FakeHTTP(_base_routes(lhb=[_lhb_row("000001", d(1), 5000000)]))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out_today = f.compute_for_stock("000001.SZ")
    out_past = f.compute_for_stock("000001.SZ", asof=TODAY - timedelta(days=10))
    # 今日: 榜在窗口内 → 1; asof=TODAY-10: 榜在未来 → 0(两份快照不同键)
    assert out_today["Y7"]["score"] == 1
    assert out_past["Y7"]["score"] == 0
    keys = [k for k in f._cache if k.endswith("000001.SZ")]
    assert len(keys) == 2


# ---------- Y2 筹码干净(股东户数) ----------
def test_y2_shareholders_hit():
    gdhs = [_holder_row("000001", -800)]
    http = FakeHTTP(_base_routes(gdhs=gdhs))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y2"]["score"] == 1


def test_y2_shareholders_miss():
    gdhs = [_holder_row("000001", 300)]
    http = FakeHTTP(_base_routes(gdhs=gdhs))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y2"]["score"] == 0


# ---------- Y6 事件催化(公告) ----------
def test_y6_event_catalyst_hit():
    ann = [_ann_row("2026年半年度业绩预增公告", d(2))]
    http = FakeHTTP(_base_routes(ann=ann))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y6"]["score"] == 1


def test_y6_event_catalyst_miss():
    # 无关键词命中 → 0
    ann = [_ann_row("关于召开股东大会的通知", d(1))]
    http = FakeHTTP(_base_routes(ann=ann))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert out["Y6"]["score"] == 0


# ---------- S5 融资融券(fail-open, 保持手填) ----------
def test_s5_financing_stays_manual():
    http = FakeHTTP(_base_routes())
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ")
    assert "S5" not in out
    assert f._financing("000001") is None


# ---------- 全因子命中 + 缓存 + fail-open ----------
def test_compute_for_stock_full_hit_confirmed_factors():
    pool_by_date = {
        dk(0): [_pool_row("000001", "全新技术题材")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(3): [_pool_row("999999", "旧题材B")],
        dk(4): [_pool_row("999999", "旧题材C")],
        dk(5): [_pool_row("999999", "旧题材D")],
    }
    http = FakeHTTP(_base_routes(
        pool_by_date=pool_by_date,
        slist_diff=[_concept_board("储能"), _concept_board("机器人"),
                    _concept_board("低空经济")],
        lhb=[_lhb_row("000001", d(1), 800000)],
        gdhs=[_holder_row("000001", -1000)],
        ann=[_ann_row("公司中标重大合同公告", d(1))],
    ))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ", float_mv=50e8)
    assert {k: v["score"] for k, v in out.items()} == {
        "Y1": 1, "Y8": 1, "Y5": 1, "F7": 1, "Y7": 1, "Y2": 1, "Y6": 1,
    }


def test_compute_for_stock_never_raises_on_http_failure():
    # 所有网络因子都失败 → 只保留 Y1/Y8(纯计算), 不崩
    f = FundamentalFeed(http_get=FakeHTTP(default_exc=RuntimeError("down")),
                        cache_path=None)
    out = f.compute_for_stock("000001.SZ", float_mv=50e8)
    assert set(out.keys()) == {"Y1", "Y8"}
    assert out["Y1"]["score"] == 1
    assert out["Y8"]["score"] == 1


def test_compute_for_stock_non_numeric_float_mv_fails_open_y1():
    # 非数值 float_mv(如字符串)→ Y1 被捕获跳过, compute_for_stock 不抛、不崩(fail-open)
    http = FakeHTTP(_base_routes())
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ", float_mv="not-a-number")
    assert "Y1" not in out
    assert set(out.keys()) <= {"Y5", "F7", "Y7", "Y2", "Y6"}


def test_compute_for_stock_y1_zero_still_cached():
    f = FundamentalFeed(http_get=FakeHTTP(default_exc=RuntimeError("down")),
                        cache_path=None)
    out = f.compute_for_stock("000001.SZ", float_mv=200e8)
    assert out["Y1"]["score"] == 0


def test_compute_for_stock_uses_cache():
    http = FakeHTTP(_base_routes(
        lhb=[_lhb_row("000001", d(1), 100000)],
        gdhs=[_holder_row("000001", -500)]))
    f = FundamentalFeed(http_get=http, cache_path=None)
    f.compute_for_stock("000001.SZ", float_mv=50e8)
    n1 = len(http.calls)
    f.compute_for_stock("000001.SZ", float_mv=50e8)   # 命中缓存, 不再请求
    assert len(http.calls) == n1


def test_hybk_history_cached_shared_across_stocks():
    # F7 历史题材集合按日缓存(hybk_history:YYYYMMDD), 第二只股不再回溯历史
    pool_by_date = {
        dk(0): [_pool_row("000001", "全新技术题材"), _pool_row("000002", "另一个新题材")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(3): [_pool_row("999999", "旧题材B")],
        dk(4): [_pool_row("999999", "旧题材C")],
        dk(5): [_pool_row("999999", "旧题材D")],
    }
    http = FakeHTTP(_base_routes(pool_by_date=pool_by_date))
    f = FundamentalFeed(http_get=http, cache_path=None)
    f.compute_for_stock("000001.SZ")
    n_first = len(http.calls)
    f.compute_for_stock("000002.SZ")
    # 第二只股只多 4 次请求(Y5/Y7/Y2/Y6): 今日池与历史池均已按日缓存, 不再回溯历史
    assert len(http.calls) - n_first == 4


def test_ztpool_today_cached_shared_across_stocks():
    # 今日涨停池按日缓存(ztpool:YYYYMMDD): 两只股对今日池总共只发 1 次请求
    pool_by_date = {
        dk(0): [_pool_row("000001", "全新技术题材"), _pool_row("000002", "另一个新题材")],
        dk(1): [_pool_row("999999", "旧题材A")],
        dk(3): [_pool_row("999999", "旧题材B")],
        dk(4): [_pool_row("999999", "旧题材C")],
        dk(5): [_pool_row("999999", "旧题材D")],
    }
    http = FakeHTTP(_base_routes(pool_by_date=pool_by_date))
    f = FundamentalFeed(http_get=http, cache_path=None)
    today = dk(0)
    f.compute_for_stock("000001.SZ")
    ztpool_after_first = [u for u, p in http.calls if "getTopicZTPool" in u]
    assert sum(1 for u, p in http.calls
               if "getTopicZTPool" in u and (p or {}).get("date") == today) == 1
    f.compute_for_stock("000002.SZ")
    ztpool_total = [u for u, p in http.calls if "getTopicZTPool" in u]
    # 第二只股零 ztpool 请求(今日池与历史池均命中缓存), 今日池总数仍为 1
    assert len(ztpool_total) == len(ztpool_after_first)
    assert sum(1 for u, p in http.calls
               if "getTopicZTPool" in u and (p or {}).get("date") == today) == 1
