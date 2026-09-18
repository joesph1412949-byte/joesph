# -*- coding: utf-8 -*-
"""fundamental 单元测试 — 注入假 http_get(按 URL/参数路由罐装 JSON/抛异常), 绝不发真实请求。
Y5(slist)/F7(ztpool)/Y7、Y2(datacenter)/Y6(ann) 响应形状各异, FakeHTTP 按 URL/参数子串分发。
"""
import sys
import json
import logging
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


# ---------- 快照类(Y5/Y2)对过去基准日必须跳过(防未来数据错记成历史) ----------
def test_compute_for_stock_past_asof_skips_snapshot_factors():
    """基准日 < 今天 → Y5(概念)/Y2(股东户数)硬跳过, 不请求不落缓存。

    这两类接口不提供历史时点(类 docstring 自述): 硬算只能拿"今天"的值,
    对过去基准日是未来数据, 且会把今天的值错记成那天的快照 —— 污染
    Y2/Y5 每日真实历史积累(规格 §7)。窗口类(Y7/Y6)不受影响照常计算。
    """
    gdhs = [_holder_row("000001", -800)]              # 若硬算会命中 Y2=1
    slist_diff = [_concept_board("储能"), _concept_board("机器人"),
                  _concept_board("低空经济")]          # 若硬算会命中 Y5=1
    http = FakeHTTP(_base_routes(slist_diff=slist_diff, gdhs=gdhs))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ", asof=TODAY - timedelta(days=10))
    assert "Y5" not in out and "Y2" not in out, "过去基准日绝不许带当前快照值"
    assert "Y7" in out, "窗口类(Y7)照常按 asof 计算"


def test_compute_for_stock_today_asof_keeps_snapshot_factors():
    """基准日 = 今天(显式 asof=今天) → Y5/Y2 照常计算(当日快照, 实盘/每日采集用)。"""
    gdhs = [_holder_row("000001", -800)]
    slist_diff = [_concept_board("储能"), _concept_board("机器人"),
                  _concept_board("低空经济")]
    http = FakeHTTP(_base_routes(slist_diff=slist_diff, gdhs=gdhs))
    f = FundamentalFeed(http_get=http, cache_path=None)
    out = f.compute_for_stock("000001.SZ", asof=TODAY)
    assert out["Y5"]["score"] == 1 and out["Y2"]["score"] == 1


# ---------- offline: 回测侧默认只读缓存, 绝不联网(2026-09-17 用户拍板) ----------
def test_offline_makes_zero_network_calls(tmp_path):
    """offline=True → 全部网络因子(Y5/Y2/F7/Y7/S5/Y6)跳过, 一次请求都不发。

    实测背景: 东财单股取数 40s+(`np-anotice-stock` 35.5s), 全窗口 254 日
    ≈100 小时 → 回测侧只读缓存, 联网是显式选择。
    """
    http = FakeHTTP(_base_routes(slist_diff=[_concept_board("储能")] * 3))
    f = FundamentalFeed(http_get=http, cache_path=tmp_path / "c.json",
                        offline=True)
    out = f.compute_for_stock("000001.SZ", float_mv=50e8)
    assert http.calls == [], "offline 绝不许发任何网络请求"
    assert sorted(out) == ["Y1", "Y8"], "只有纯计算因子(Y1/Y8)可离线算"
    assert out["Y1"]["score"] == 1 and out["Y8"]["score"] == 1


def test_offline_without_float_mv_yields_empty(tmp_path):
    """offline 且无 float_mv → 空 dict(Y1/Y8 都算不出), 依然零请求。"""
    http = FakeHTTP(_base_routes())
    f = FundamentalFeed(http_get=http, cache_path=tmp_path / "c.json",
                        offline=True)
    assert f.compute_for_stock("000001.SZ") == {}
    assert http.calls == []


def test_offline_returns_cached_value_readonly(tmp_path):
    """offline 命中既有缓存 → 照常返回缓存值(含 Y5/Y2 等联网因子), 只读不写。"""
    p = tmp_path / "c.json"
    key = "%s:000001.SZ" % TODAY.strftime("%Y%m%d")
    cached = {"Y5": {"score": 1, "note": "缓存里的概念"},
              "Y2": {"score": 1, "note": "缓存里的股东户数"}}
    p.write_text(json.dumps({key: cached}, ensure_ascii=False), encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    http = FakeHTTP(_base_routes())
    f = FundamentalFeed(http_get=http, cache_path=p, offline=True)
    out = f.compute_for_stock("000001.SZ")
    assert out == cached, "命中缓存必须原样返回(含联网因子)"
    assert http.calls == []
    assert p.read_text(encoding="utf-8") == before, "offline 只读不写"


def test_offline_never_writes_cache_on_miss(tmp_path):
    """offline 未命中 → **绝不写缓存**: 空/半截结果一旦落盘会被钉进历史。

    `compute_for_stock` 开头 `if key in self._cache: return` 会短路 —— 离线时
    写进去的"只有 Y1/Y8"条目, 之后联网运行与每日快照再也补不上该日真实值。
    """
    p = tmp_path / "c.json"
    http = FakeHTTP(_base_routes(slist_diff=[_concept_board("储能")] * 3))
    f = FundamentalFeed(http_get=http, cache_path=p, offline=True)
    f.compute_for_stock("000001.SZ", float_mv=50e8)
    assert not p.exists(), "offline 未命中不许落盘"
    assert f._cache == {}, "内存缓存也不许记(否则同进程后续会短路)"


# ---------- I2: 回写前先读回磁盘合并(跨写者不丢更新) ----------
def test_save_cache_keeps_keys_added_by_other_writer(tmp_path):
    """I2: 长寿命 feed 的整文件回写必须先合并磁盘 —— 别的写者新增的键不许被抹掉。

    实盘/盯盘进程的 `prism.data.DataProvider.fund_feed` 只建一次(__init__ 时
    load 一次缓存); 这之后快照 CLI / 守护快照线程新增的 (股,日) 键, 必须活过
    它的下一次 `_save_cache` —— 否则丢的正是本批次要积累的 Y2/Y5 日快照与
    `--date` 回填历史, 且两边都报成功。
    """
    p = tmp_path / "c.json"
    http = FakeHTTP(_base_routes(slist_diff=[_concept_board("储能")] * 3))
    # 长寿命实例: 此刻磁盘还不存在 → 它的内存缓存从空开始(实盘/盯盘的真实形态)
    long_lived = FundamentalFeed(http_get=http, cache_path=p)
    day = TODAY.strftime("%Y%m%d")
    snap_key = "%s:000001.SZ" % day
    # 另一个写者(快照 CLI / 守护线程)写入它的键
    FundamentalFeed(http_get=http, cache_path=p).compute_for_stock(
        "000001.SZ", asof=TODAY)
    assert snap_key in json.loads(p.read_text(encoding="utf-8"))

    long_lived.compute_for_stock("000002.SZ", asof=TODAY)
    disk = json.loads(p.read_text(encoding="utf-8"))
    assert snap_key in disk, "别的写者新增的键被整文件回写抹掉了(跨写者丢更新)"
    assert "%s:000002.SZ" % day in disk, "本进程的键照常写入"


def test_save_cache_memory_value_wins_for_duplicate_key(tmp_path):
    """I2: 同一个键两边都有 → 以本进程内存值为准(磁盘同键不许盖回内存值)。"""
    p = tmp_path / "c.json"
    key = "%s:000001.SZ" % TODAY.strftime("%Y%m%d")
    p.write_text(json.dumps({key: {"Y6": {"score": 0, "note": "磁盘旧值"}}},
                            ensure_ascii=False), encoding="utf-8")
    f = FundamentalFeed(http_get=FakeHTTP(_base_routes()), cache_path=p)
    f._cache[key] = {"Y6": {"score": 1, "note": "本进程新算的"}}
    f._save_cache()
    assert json.loads(p.read_text(encoding="utf-8"))[key] == \
        {"Y6": {"score": 1, "note": "本进程新算的"}}, "内存值必须优先"


def test_save_cache_read_failure_skips_write_and_keeps_disk(tmp_path, caplog):
    """I2b: 回写前**读盘失败** → 跳过本次落盘, 绝不退化成整文件覆盖。

    修复前 `_load_cache` 把任何异常都当 `{}` ⇒ `disk.update(self._cache)` 后整
    文件覆盖, 磁盘上别的写者写的键被整批抹掉 —— 正是 I2 要修的病复发(而且更
    隐蔽: 盘上明明有内容, 却被当空的)。fail-safe 口径: 读失败时**不动盘** +
    WARNING 说明原因, 让下一次(读得通时)再合并。"""
    p = tmp_path / "c.json"
    f = FundamentalFeed(http_get=FakeHTTP(_base_routes()), cache_path=p)
    f._cache["%s:000002.SZ" % TODAY.strftime("%Y%m%d")] = {"Y5": {"n": 3}}
    # 盘上是坏 JSON(半截写/外部截断) → 真实读失败(不靠 mock)
    p.write_bytes(b'{"broken": ')
    broken = p.read_bytes()

    with caplog.at_level(logging.WARNING, logger="fundamental"):
        f._save_cache()

    assert p.read_bytes() == broken, \
        "读失败时不许覆盖: 盘上别的写者的键可能就在那份读不出的文件里"
    assert "读盘失败" in caplog.text and "跳过" in caplog.text, \
        "必须留下 WARNING 说明为什么没落盘: %r" % caplog.text


# ---------- I3: 联网路径的"半截条目"绝不落盘 ----------
_DOWN = RuntimeError("东财全挂")


def test_online_all_network_factors_failed_not_persisted(tmp_path):
    """I3: 联网路径下网络类因子一个都没成功 → 半截条目**不落盘**(留待重试)。

    落盘就会被开头 `if key in self._cache: return` 永久短路 → 该 (股,日) 的
    F7/Y6/Y7 永远是 0, 而 backtest 的"已覆盖"判定(该日任一网络类有值)又把这些
    天统计成"已采集" —— 覆盖数偏乐观。
    """
    p = tmp_path / "c.json"
    key = "%s:000001.SZ" % TODAY.strftime("%Y%m%d")
    f = FundamentalFeed(http_get=FakeHTTP(default_exc=_DOWN), cache_path=p)
    out = f.compute_for_stock("000001.SZ", float_mv=50e8)
    assert {"Y1", "Y8"} <= set(out), "纯计算因子照常返回(fail-open 不抛)"
    assert not [n for n in ("Y5", "Y2", "F7", "Y7", "Y6") if n in out], \
        "网络全挂 → 一个网络类因子都没有"
    disk = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    assert key not in disk, "网络类全失败的半截条目绝不能落盘"
    assert key not in f._cache, "内存缓存也不许记(否则同进程后续一律短路)"


def test_online_network_failure_retried_after_recovery(tmp_path):
    """I3: 上轮网络全挂不落盘 → 网络恢复后**真的重取**(不被半截缓存短路)并落盘。"""
    p = tmp_path / "c.json"
    key = "%s:000001.SZ" % TODAY.strftime("%Y%m%d")
    FundamentalFeed(http_get=FakeHTTP(default_exc=_DOWN),
                    cache_path=p).compute_for_stock("000001.SZ", float_mv=50e8)
    assert key not in (json.loads(p.read_text(encoding="utf-8"))
                       if p.exists() else {})

    http = FakeHTTP(_base_routes(slist_diff=[_concept_board("储能")] * 3))
    out = FundamentalFeed(http_get=http, cache_path=p).compute_for_stock(
        "000001.SZ", float_mv=50e8)
    assert http.calls, "网络恢复后必须真的重新请求(旧实现被半截缓存短路 → 0 次)"
    assert {"Y6", "Y7"} <= set(out), "网络类因子这次必须真取到"
    assert key in json.loads(p.read_text(encoding="utf-8")), "取到了才落盘"


def test_online_partial_network_success_still_persisted(tmp_path):
    """I3: 网络类至少有一个成功 → 照常落盘(不许因个别端点挂掉就不落)。

    尤其 Y5/Y2 是**无历史可回补**的快照类: 拿 F7/Y6/Y7 的失败去连坐它们,
    等于把 I1 的"当天永久丢失"换个位置再挖一遍。
    """
    p = tmp_path / "c.json"
    key = "%s:000001.SZ" % TODAY.strftime("%Y%m%d")
    routes = _base_routes(slist_diff=[_concept_board("储能")] * 3)
    routes["getTopicZTPool"] = RuntimeError("zt 端点挂")   # F7 挂, Y5/Y6/Y7 正常
    http = FakeHTTP(routes)
    out = FundamentalFeed(http_get=http, cache_path=p).compute_for_stock(
        "000001.SZ", float_mv=50e8)
    assert "F7" not in out, "F7 端点挂了 → 该因子 fail-open 缺键"
    assert {"Y5", "Y6", "Y7"} <= set(out), "其余网络类照常"
    assert json.loads(p.read_text(encoding="utf-8"))[key] == out, \
        "有网络类成功 → 必须落盘"

