# -*- coding: utf-8 -*-
"""每日基本面快照采集(prism.fund_snapshot, 规格 §7) + 回测注入基本面 — 离线测试。

覆盖:
  ① snapshot_once(假 http 的 FundamentalFeed + tmp 缓存)落既有缓存, 键含日期
  ② 重复调用幂等: 命中缓存不再发请求 / 同 key 覆盖不重复写
  ③ 失败计数: 单股失败不阻塞其余; 一无所获(全因子 fail-open)计 failed
  ④ 无未来约束: 拒绝未来日期(绝不写未来键); 非法日期 → ValueError
  ⑤ default_codes: 本地涨停池索引 → 代码表(只读不触网)
  ⑥ CLI main(): --codes/--date + JSON 结果
  ⑦ build_day_feed 注入 fund: stock[code]["fund"](asof=当日, float_mv=当日),
     Y5/Y2 快照类剔除(与 Backtester._fund_for 同口径), 网络失败 fail-open,
     缺省(不传 fund_feed)行为不变(零注入)
  ⑧ 池条目 float_mv: 有股本 → 股本×当日收盘; 缺 → None(不造假)
  ⑨ 守护 15:05 选股后挂钩子: 可注入、异常只落日志不影响选股返回
  ⑩ 覆盖口径(I1 审查修复): F7/Y6/Y7(网络类)只在"已采集日"有值, 按天计覆盖;
     Y1/Y8(纯计算)不算覆盖 —— 缓存全空时必须是 0/M, 不许写成满覆盖
  ⑪ 守护钩子 I3: 采集前等涨停池刷新线程结束(防空采); 空池 → WARNING + skipped
  ⑫ C1: 快照(不带 float_mv)落下的条目缺 Y1/Y8 → 命中缓存时按当日 float_mv
     就地补齐(offline 只进返回副本; 非 offline 原子写回); 已有/无 float_mv 不写
  ⑬ I3 补强: 空池 → 无视 6h 节流强制刷新索引 + 重试一次(仍空才告警);
     刷新线程卡死 → 等 ZT_REFRESH_WAIT 秒后照常采集(M3)

全离线: FundamentalFeed 经 http_get 注入罐装 JSON; 守护注入假钩子。
"""
import json
import logging
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.fund_snapshot as fund_snapshot
from datasource.fundamental import FundamentalFeed

# ---------------- 假 http(datasource/tests 同款: 按 URL 子串路由) ----------------


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeHTTP:
    """按 URL/参数子串路由的假 http_get; 未匹配 → AssertionError(防意外联网)。"""

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
                if callable(payload):
                    payload = payload(params)
                return FakeResp(payload)
        raise AssertionError("未预期的请求: %s %r" % (url, params))


def _ztpool_handler(pool_by_date):
    def handler(params):
        return {"data": {"pool": pool_by_date.get((params or {}).get("date"))}}
    return handler


def _base_routes(slist_total=0):
    """全端点可用默认(空数据 → 各网络因子 0/None, 不抛)。"""
    return {
        "slist": {"data": {"diff": [], "total": slist_total}},
        "getTopicZTPool": _ztpool_handler({}),
        "RPT_DAILYBILLBOARD_DETAILSNEW": {"result": {"data": []}},
        "RPT_HOLDERNUMLATEST": {"result": {"data": []}},
        "np-anotice": {"data": {"list": []}},
    }


def _feed(tmp_path, routes=None, default_exc=None, **kw):
    return FundamentalFeed(http_get=FakeHTTP(routes, default_exc),
                           cache_path=tmp_path / "fund_cache.json", **kw)


def _read_cache(tmp_path):
    p = tmp_path / "fund_cache.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------- ① snapshot_once: 落缓存, 键含日期 ----------------

def test_snapshot_once_writes_dated_cache_key(tmp_path):
    """指定历史日 → compute_for_stock(asof=该日), 缓存键 = YYYYMMDD:code。

    历史基准日: 窗口类(Y7/Y6)照常; 快照类 Y5/Y2 接口无历史时点 → 硬跳过
    (不把今天的值错记成那天的历史, 见 datasource feed 的防未来测试)。
    """
    feed = _feed(tmp_path, _base_routes())
    res = fund_snapshot.snapshot_once(["000001.SZ"], date="20260915", feed=feed)
    assert res == {"saved": 1, "failed": 0}
    cache = _read_cache(tmp_path)
    assert "20260915:000001.SZ" in cache, "键必须含快照日期(按 asof 分日)"
    entry = cache["20260915:000001.SZ"]
    assert "Y7" in entry and "Y6" in entry, "窗口类按 asof 正常快照"
    assert "Y5" not in entry and "Y2" not in entry, \
        "历史基准日绝不许带当前快照值(无未来)"


def test_snapshot_once_today_key_when_no_date(tmp_path):
    """date=None → 今天: 不传 asof, 键 = 今日 YYYYMMDD:code; Y5/Y2 照常采。"""
    feed = _feed(tmp_path, _base_routes(slist_total=3))
    res = fund_snapshot.snapshot_once(["000001.SZ"], feed=feed)
    assert res == {"saved": 1, "failed": 0}
    today = date.today().strftime("%Y%m%d")
    entry = _read_cache(tmp_path).get("%s:000001.SZ" % today) or {}
    assert entry.get("Y5", {}).get("score") == 1, "当日快照采 Y5(Y2/Y5 目标因子)"


def test_snapshot_once_multi_codes_counts(tmp_path):
    feed = _feed(tmp_path, _base_routes())
    res = fund_snapshot.snapshot_once(
        ["000001.SZ", "600000.SH", "300001.SZ"], date="20260915", feed=feed)
    assert res == {"saved": 3, "failed": 0}
    cache = _read_cache(tmp_path)
    assert "20260915:600000.SH" in cache and "20260915:300001.SZ" in cache


# ---------------- ② 幂等: 同 key 覆盖不重复写 ----------------

def test_snapshot_once_idempotent(tmp_path):
    """重复采集: 缓存命中 → 不再发请求, 缓存文件逐字节不变。"""
    feed = _feed(tmp_path, _base_routes())
    r1 = fund_snapshot.snapshot_once(["000001.SZ"], date="20260915", feed=feed)
    assert r1 == {"saved": 1, "failed": 0}
    n_calls = len(feed.http_get.calls)
    raw = (tmp_path / "fund_cache.json").read_text(encoding="utf-8")
    r2 = fund_snapshot.snapshot_once(["000001.SZ"], date="20260915", feed=feed)
    assert r2 == {"saved": 1, "failed": 0}
    assert len(feed.http_get.calls) == n_calls, "命中缓存 → 绝不再发网络请求"
    assert (tmp_path / "fund_cache.json").read_text(encoding="utf-8") == raw, \
        "同 key 幂等: 覆盖写不改变文件"


# ---------------- ③ 失败计数 ----------------

def test_snapshot_once_counts_failures_and_continues(tmp_path):
    """网络全挂 → 一无所获计 failed, 单股失败绝不阻塞其余/不抛。"""
    feed = _feed(tmp_path, default_exc=RuntimeError("东财挂了"))
    res = fund_snapshot.snapshot_once(["000001.SZ", "600000.SH"],
                                      date="20260915", feed=feed)
    assert res == {"saved": 0, "failed": 2}


def test_snapshot_once_feed_raises_for_one_code(tmp_path):
    """feed 对某股抛异常 → 该股 failed, 其余照常 saved。"""
    real = _feed(tmp_path, _base_routes())

    class _Flaky:
        def __init__(self, inner):
            self._inner = inner

        def compute_for_stock(self, code, float_mv=None, asof=None):
            if code == "000002.SZ":
                raise RuntimeError("boom")
            return self._inner.compute_for_stock(code, float_mv=float_mv,
                                                 asof=asof)

    res = fund_snapshot.snapshot_once(["000001.SZ", "000002.SZ"],
                                      date="20260915", feed=_Flaky(real))
    assert res == {"saved": 1, "failed": 1}
    assert "20260915:000001.SZ" in _read_cache(tmp_path)


def _snapshot_logs(caplog):
    """只取 fund_snapshot 自己落的日志(排除 datasource 内部 per-factor 警告)。"""
    return [r.getMessage() for r in caplog.records if r.name == "fund_snapshot"]


def test_snapshot_once_logs_failure_reason(tmp_path, caplog):
    """M2: feed 抛异常 → fund_snapshot 必须落日志并带原因(不能只剩计数)。"""
    class _Flaky:
        def compute_for_stock(self, code, float_mv=None, asof=None):
            raise RuntimeError("boom-%s" % code)

    with caplog.at_level(logging.WARNING, logger="fund_snapshot"):
        res = fund_snapshot.snapshot_once(["000001.SZ"], date="20260915",
                                          feed=_Flaky())
    assert res == {"saved": 0, "failed": 1}
    mine = _snapshot_logs(caplog)
    assert any("000001.SZ" in m and "boom-000001.SZ" in m for m in mine), \
        "必须记下是哪只、因为什么(否则 failed 计数无从排查)"


def test_snapshot_once_logs_empty_result_reason(tmp_path, caplog):
    """M2 同根因: 网络全挂 → 全因子 fail-open 空结果(计 failed)也要留原因。

    这正是"系统性失败"的真实形态(单因子异常被 feed 内部吞掉), 只剩一个
    failed 计数等于没有线索。
    """
    feed = _feed(tmp_path, default_exc=RuntimeError("东财挂了"))
    with caplog.at_level(logging.WARNING, logger="fund_snapshot"):
        res = fund_snapshot.snapshot_once(["000001.SZ"], date="20260915",
                                          feed=feed)
    assert res == {"saved": 0, "failed": 1}
    mine = _snapshot_logs(caplog)
    assert any("000001.SZ" in m for m in mine), \
        "一无所获也要点名是哪只(全因子 fail-open 空结果)"


def test_snapshot_once_empty_codes_is_skipped_not_success(tmp_path):
    """I3: 空代码表 → 结果必须与"成功采到"有区分度(不许像 saved/failed 全 0)。

    守护侧据此打 WARNING: 空池多半是"涨停池索引还没刷新", 当成成功就会
    静默丢掉当天 Y2/Y5(这两类无历史可回补)。
    """
    feed = _feed(tmp_path, _base_routes())
    res = fund_snapshot.snapshot_once([], date="20260915", feed=feed)
    assert res == {"saved": 0, "failed": 0, "skipped": "empty_pool"}
    assert _read_cache(tmp_path) == {}, "空池绝不写缓存"


def test_snapshot_once_empty_codes_still_validates_date(tmp_path):
    """I3+④: 空池短路**不能**吞掉未来日期拒绝(校验先于空池判定)。"""
    feed = _feed(tmp_path, _base_routes())
    with pytest.raises(ValueError):
        fund_snapshot.snapshot_once([], date="29991231", feed=feed)


# ---------------- ④ 无未来约束 ----------------

def test_snapshot_once_rejects_future_date(tmp_path):
    """未来日期 → 拒绝(绝不写未来键: 否则回测会把今天的数据当那天的事实)。"""
    feed = _feed(tmp_path, _base_routes())
    with pytest.raises(ValueError):
        fund_snapshot.snapshot_once(["000001.SZ"], date="29991231", feed=feed)
    assert _read_cache(tmp_path) == {}, "不能留下未来键"


def test_snapshot_once_rejects_bad_date(tmp_path):
    feed = _feed(tmp_path, _base_routes())
    with pytest.raises(ValueError):
        fund_snapshot.snapshot_once(["000001.SZ"], date="not-a-date", feed=feed)
    with pytest.raises(ValueError):
        fund_snapshot.snapshot_once(["000001.SZ"], date="202609", feed=feed)


def test_snapshot_once_accepts_date_object(tmp_path):
    feed = _feed(tmp_path, _base_routes())
    res = fund_snapshot.snapshot_once(["000001.SZ"], date=date(2026, 9, 15),
                                      feed=feed)
    assert res == {"saved": 1, "failed": 0}
    assert "20260915:000001.SZ" in _read_cache(tmp_path)


# ---------------- ⑤ default_codes(本地索引, 不触网) ----------------

def test_default_codes_reads_local_zt_index(monkeypatch):
    from prism import zt_history
    monkeypatch.setattr(zt_history, "qmt_zt_feed",
                        lambda d8: [{"code": "600000.SH", "boards": 1},
                                    {"code": "000001.SZ", "boards": 1}])
    assert fund_snapshot.default_codes() == ["600000.SH", "000001.SZ"]
    assert fund_snapshot.default_codes("20260915") == ["600000.SH", "000001.SZ"]


def test_default_codes_empty_when_no_pool(monkeypatch):
    from prism import zt_history
    monkeypatch.setattr(zt_history, "qmt_zt_feed", lambda d8: [])
    assert fund_snapshot.default_codes() == []


def test_default_codes_swallows_index_errors(monkeypatch):
    from prism import zt_history

    def boom(d8):
        raise RuntimeError("索引坏了")
    monkeypatch.setattr(zt_history, "qmt_zt_feed", boom)
    assert fund_snapshot.default_codes() == []


# ---------------- ⑥ CLI main() ----------------

def test_main_runs_with_codes_and_prints_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    rc = fund_snapshot.main(["--date", "20260915",
                             "--codes", "000001.SZ", "600000.SH"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"saved": 2, "failed": 0}


def test_main_defaults_to_local_pool(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    seen = []

    def fake_default(day=None):
        seen.append(day)
        return ["000001.SZ"]
    monkeypatch.setattr(fund_snapshot, "default_codes", fake_default)
    rc = fund_snapshot.main(["--date", "20260915"])
    assert rc == 0
    assert seen == ["20260915"], "缺省代码表必须按快照基准日取该日涨停池"
    assert json.loads(capsys.readouterr().out.strip()) == {"saved": 1, "failed": 0}


def test_main_empty_pool_reports_zero_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    monkeypatch.setattr(fund_snapshot, "default_codes",
                        lambda day=None: [])
    rc = fund_snapshot.main([])
    assert rc == 0, "无池不算失败(可能只是非交易日)"
    captured = capsys.readouterr()
    assert "未采集" in captured.err
    assert "尚未刷新" in captured.err, "必须点名真因: 索引可能还没刷新"
    assert json.loads(captured.out.strip()) == {"saved": 0, "failed": 0,
                                                "skipped": "empty_pool"}, \
        "空池输出必须与真采到 0 只有区分度"


def test_main_empty_date_string_exits_2(tmp_path, monkeypatch, capsys):
    """M2: `--date ""` 必须报错 exit 2。

    旧实现 `if args.date:` —— 空串为假 → 跳过日期校验 → `default_codes("")`
    把 `_parse_date` 的 ValueError 吞成 [] → 打印"索引无该日数据"并 **exit 0**,
    非法输入被静默放过(带 --codes 时反而会因为 snapshot_once 里再解析一次而
    exit 2 —— 所以本测试**不带** --codes, 走的正是被吞掉的那条路径)。
    """
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    rc = fund_snapshot.main(["--date", ""])
    assert rc == 2, "空串日期是非法输入, 不许 exit 0"
    err = capsys.readouterr().err
    assert "日期格式" in err, "必须报真因(日期格式非法)"
    assert "索引无该日数据" not in err, "不许误报成索引缺数据"


def test_main_bad_date_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    rc = fund_snapshot.main(["--date", "garbage", "--codes", "000001.SZ"])
    assert rc == 2


def test_main_bad_date_without_codes_exits_2(tmp_path, monkeypatch, capsys):
    """M1: 非法 --date 且不带 --codes 也必须 exit 2。

    旧实现先解析代码表: default_codes 把 _parse_date 的 ValueError 吞成 [] →
    打印"索引无该日数据"并 exit 0, 真因(日期非法)被掩盖。
    """
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    rc = fund_snapshot.main(["--date", "garbage"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "日期格式" in err, "必须报真因(日期格式非法)"
    assert "索引无该日数据" not in err, "不许误报成索引缺数据"


def test_main_future_date_without_codes_exits_2(tmp_path, monkeypatch, capsys):
    """M1 同根因: 未来日期也不许被"无池"掩盖成 exit 0(校验先于代码表)。"""
    monkeypatch.setattr(fund_snapshot, "FundamentalFeed",
                        lambda: _feed(tmp_path, _base_routes()))
    rc = fund_snapshot.main(["--date", "29991231"])
    assert rc == 2
    assert "未来" in capsys.readouterr().err


# ---------------- ⑦/⑧ build_day_feed 注入 fund + 池条目 float_mv ----------------

_CAL = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
_POOLS = {
    _CAL[0]: [{"code": "600000.SH", "boards": 1}],
    _CAL[1]: [{"code": "600000.SH", "boards": 1},
              {"code": "000001.SZ", "boards": 1}],
    _CAL[2]: [{"code": "000001.SZ", "boards": 1}],
}
_IDX = {
    "000001.SH": [(d.strftime("%Y-%m-%d"), 3800.0 + i, 4.0e8, 8.7e11)
                  for i, d in enumerate(_CAL)],
    "399001.SZ": [(d.strftime("%Y-%m-%d"), 13000.0 + i, 5.0e8, 9.6e11)
                  for i, d in enumerate(_CAL)],
}
_CLOSES = [10.0, 11.0, 12.0]     # 三日收盘(升序)
_CODES = ("600000.SH", "000001.SZ")


def _klines():
    out = {}
    for c in _CODES:
        out[c] = [(d.strftime("%Y-%m-%d"), x, x * 1.02, x * 0.98, x, 2500.0)
                  for d, x in zip(_CAL, _CLOSES)]
    return out


def _stub_day_io(monkeypatch, float_volumes=None):
    """桩掉 build_day_feed 的四类 IO(日历/指数/池/K线/股本), 全离线。"""
    import backtest.cli as cli
    kl = _klines()
    pools_by_d8 = {d.strftime("%Y%m%d"): v for d, v in _POOLS.items()}

    def fake_zt(d8):
        return [dict(s) for s in pools_by_d8.get(d8, [])]
    monkeypatch.setattr(cli, "_get_zt_index",
                        lambda: {d.strftime("%Y%m%d"): 1 for d in _CAL})
    monkeypatch.setattr(cli, "_index_daily", lambda code, *a, **k: list(_IDX[code]))
    monkeypatch.setattr(cli, "zt_feed", fake_zt)
    monkeypatch.setattr(cli, "_batch_klines",
                        lambda codes: {c: kl[c] for c in codes if c in kl})
    monkeypatch.setattr(cli, "_float_volumes",
                        lambda codes: dict(float_volumes or {}))
    return cli


class _FakeFundFeed:
    """假基本面 feed: 记录 (code, float_mv, asof); 返回含快照类 Y5/Y2 的固定集。"""

    def __init__(self, fail_codes=()):
        self.calls = []
        self.fail_codes = set(fail_codes)

    def compute_for_stock(self, code, float_mv=None, asof=None):
        if code in self.fail_codes:
            raise RuntimeError("网络挂了")
        self.calls.append((code, float_mv, asof))
        return {"Y1": {"score": 1, "note": "y1"},
                "Y5": {"score": 1, "note": "y5-snapshot"},
                "Y2": {"score": 1, "note": "y2-snapshot"},
                "F7": {"score": 0, "note": "f7"}}


def test_build_day_feed_injects_fund_asof_float_mv(monkeypatch):
    """stock[code]["fund"] 注入: asof=当日, float_mv=当日股本×收盘; 幂等不重复算。"""
    cli = _stub_day_io(monkeypatch, {"600000.SH": 3.0e9})
    ffeed = _FakeFundFeed()
    feed = cli.build_day_feed(_CAL[0], _CAL[2], fund_feed=ffeed)
    ctx1 = feed(_CAL[1])
    st = ctx1["stock"]["600000.SH"]
    # Y5/Y2 是"当前快照"类, 回测必剔除(与 Backtester._fund_for 同口径, 保持现状)
    assert st["fund"] == {"Y1": {"score": 1, "note": "y1"},
                          "F7": {"score": 0, "note": "f7"}}
    code, float_mv, asof = ffeed.calls[0]
    assert code == "600000.SH"
    assert float_mv == pytest.approx(3.0e9 * 11.0), "float_mv = 股本×当日收盘"
    assert asof == _CAL[1], "asof 必须=决策日(防未来)"
    # 同日再请求 → 命中 payload 缓存, 不重复计算
    n = len(ffeed.calls)
    feed(_CAL[1])
    assert len(ffeed.calls) == n


def test_build_day_feed_no_float_mv_passes_none(monkeypatch):
    """缺股本 → float_mv=None(Y1/Y8 由 compute 内 fail-open 缺键)。"""
    cli = _stub_day_io(monkeypatch, {})          # 谁都没给股本
    ffeed = _FakeFundFeed()
    feed = cli.build_day_feed(_CAL[0], _CAL[2], fund_feed=ffeed)
    ctx = feed(_CAL[2])
    st = ctx["stock"]["000001.SZ"]
    assert st["fund"]["Y1"] == {"score": 1, "note": "y1"}   # fund 照常注入
    code, float_mv, asof = ffeed.calls[-1]
    assert float_mv is None, "缺股本 → None, 不造假"


def test_build_day_feed_fund_failure_fail_open(monkeypatch):
    """feed 对某股炸 → 该股不给 fund(得 0), 整日上下文照常返回。"""
    cli = _stub_day_io(monkeypatch, {})
    ffeed = _FakeFundFeed(fail_codes={"000001.SZ"})
    feed = cli.build_day_feed(_CAL[0], _CAL[2], fund_feed=ffeed)
    ctx = feed(_CAL[2])
    assert "fund" not in ctx["stock"]["000001.SZ"]
    # 其余日期照常
    ctx1 = feed(_CAL[1])
    assert ctx1["stock"]["600000.SH"]["fund"]["Y1"]["score"] == 1


def test_build_day_feed_without_fund_feed_unchanged(monkeypatch):
    """缺省(不传 fund_feed) → 行为不变: stock 无 fund 键(零网络、零注入)。"""
    cli = _stub_day_io(monkeypatch, {})
    feed = cli.build_day_feed(_CAL[0], _CAL[2])
    ctx = feed(_CAL[2])
    assert "fund" not in ctx["stock"]["000001.SZ"]


def test_day_payload_float_mv_none_when_missing():
    """池条目 float_mv 契约: 有股本 → 股本×收盘; 缺 → 显式 None(float_vol 仍缺省不造假)。"""
    from backtest.cli import _day_payload
    kl = _klines()
    pools = {k: v for k, v in _POOLS.items()}
    p = _day_payload(_CAL[1], pools, _CAL, kl, {"600000.SH": 3.0e9}, {})
    st = p["stock"]["600000.SH"]
    assert st["float_vol"] == 3.0e9 and st["float_mv"] == pytest.approx(3.0e9 * 11.0)
    p2 = _day_payload(_CAL[2], pools, _CAL, kl, {}, {})
    st2 = p2["stock"]["000001.SZ"]
    assert st2["float_mv"] is None, "缺股本 → float_mv=None"
    assert "float_vol" not in st2, "缺股本不造假(维持既有口径)"


# ---------------- ⑩ 回测侧默认离线: 只读基本面缓存, 绝不联网(2026-09-17 拍板) -------

def test_fund_feed_defaults_offline():
    """`_fund_feed()` 默认 offline=True(回测不联网); 显式 fetch=True 才联网。"""
    from backtest.cli import _fund_feed
    assert _fund_feed().offline is True, "回测默认必须只读缓存"
    assert _fund_feed(fetch=True).offline is False, "显式开启才联网"


def _seed_cache(tmp_path, day, codes):
    """预置既有缓存(键 YYYYMMDD:code, 同既有格式) → 模拟"该日已采过"。"""
    p = tmp_path / "fund_cache.json"
    d8 = day.strftime("%Y%m%d") if hasattr(day, "strftime") else str(day)
    data = {("%s:%s" % (d8, c)):
            {"Y5": {"score": 1, "note": "cached"},
             "Y7": {"score": 0, "note": "cached"}}
            for c in codes}
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_build_day_feed_offline_reads_cache_only(monkeypatch, tmp_path):
    """offline feed 经 build_day_feed 注入: 已缓存日有 fund, 未缓存日**无 fund 键**。

    这是回测侧"默认只读缓存"的行为证据: 未缓存 + **缺 float_mv**(本测试) →
    fail-open(连 Y1/Y8 也算不出来 → 不给 fund), 既不联网也不落盘
    (不把空结果钉进历史)。
    注: "未缓存日无 fund 键"**只在缺 float_mv 时成立** —— 有 float_mv 时
    Y1/Y8 纯计算照常给 fund(见 test_build_day_feed_offline_uncached_day_
    has_only_calc_factors)。
    """
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {})
    cache = _seed_cache(tmp_path, _CAL[2], ["000001.SZ"])   # 只有第 3 天已采
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    # 未缓存日(第 2 天有 2 根K线, 进 stock): 无 fund 键(fail-open)
    raw = cache.read_text(encoding="utf-8")
    st1 = feed(_CAL[1])["stock"]["600000.SH"]
    assert "fund" not in st1, "未缓存日不给 fund(离线 fail-open)"
    # 已缓存日: fund 来自缓存(注意 Y5/Y2 仍按回测口径剔除)
    st = feed(_CAL[2])["stock"]["000001.SZ"]
    assert st["fund"] == {"Y7": {"score": 0, "note": "cached"}}, \
        "已缓存日必须注入 fund, 且 Y5/Y2 照旧剔除"
    assert cache.read_text(encoding="utf-8") == raw, "offline 不许写缓存"


def test_build_day_feed_offline_never_writes_new_cache(monkeypatch, tmp_path):
    """offline 全程跑完 → 缓存文件里不出现任何新键(未命中就是不落盘)。"""
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {})
    cache = tmp_path / "empty_cache.json"
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    for d in _CAL:
        feed(d)
    assert not cache.exists(), "offline 回测绝不许产生缓存写入"


def test_build_day_feed_offline_uncached_day_has_only_calc_factors(monkeypatch,
                                                                  tmp_path):
    """M6: 未缓存日 + **有 float_mv** → fund 键存在, 但只含纯计算 Y1/Y8。

    补上原测试(传 {} 股本)漏掉的一半: 有 float_mv 时未缓存日照样给 fund,
    只是**不含网络类** F7/Y6/Y7 —— 这正是 I1 新口径要区分的两类。
    """
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {"600000.SH": 3.0e9})
    cache = _seed_cache(tmp_path, _CAL[2], ["000001.SZ"])   # 只有第 3 天已采
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    st = feed(_CAL[1])["stock"]["600000.SH"]                # 第 2 天未缓存
    assert set(st["fund"]) == {"Y1", "Y8"}, \
        "有 float_mv 的未缓存日: fund 只有纯计算因子"
    assert not ({"F7", "Y6", "Y7"} & set(st["fund"])), \
        "网络类必须缺席(该日未采集)"


def test_fund_note_offline_uncached_network_coverage_is_zero(monkeypatch,
                                                             tmp_path):
    """I1: 有 float_mv 但缓存一天都没有 → 网络类覆盖必须 **0/M**(不许算成已覆盖)。

    旧口径把"只有 Y1/Y8 纯计算"的股票日也计进覆盖 → 报告写"个股日覆盖 4/4"
    (即使基本面缓存一天都没有), 还把 0 归因于"网络失败/缺 float_mv";
    真因是**该日未采集**。
    """
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {"600000.SH": 3.0e9, "000001.SZ": 3.0e9})
    cache = tmp_path / "never_collected.json"       # 一天都没采过
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    for d in _CAL:
        feed(d)
    # 未采集 ≠ 没因子: Y1/Y8 由当日 float_mv 纯计算, 照常注入
    assert set(feed(_CAL[2])["stock"]["000001.SZ"]["fund"]) == {"Y1", "Y8"}
    n = [x for x in feed.data_notes if x.startswith("基本面")][0]
    assert "F7/Y6/Y7 覆盖 0/3 天" in n, \
        "缓存全空 → 网络类覆盖必须是 0/3, 绝不能算成已覆盖"
    assert "未采集" in n, "必须点名真因: 该日未采集"
    assert "网络失败" not in n, "不许再把 0 归因于网络失败"
    assert "个股日覆盖 4/4" not in n, "旧口径的假覆盖结论必须消失"
    assert "Y1/Y8" in n and "纯计算" in n, "Y1/Y8 要单独说明(与缓存覆盖无关)"
    assert "就地补齐" in n, \
        "C1: 命中缓存时会补齐缺的纯计算值 —— 不许再宣称\"不受缓存覆盖影响\""


def test_fund_note_offline_cached_day_counts_network_by_day(monkeypatch,
                                                            tmp_path):
    """I1: 该日已缓存 → 网络类覆盖按**天**计(同日多只命中只算 1 天)。"""
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {"600000.SH": 3.0e9, "000001.SZ": 3.0e9})
    cache = _seed_cache(tmp_path, _CAL[1], ["600000.SH", "000001.SZ"])
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    for d in _CAL:
        feed(d)
    n = [x for x in feed.data_notes if x.startswith("基本面")][0]
    assert "F7/Y6/Y7 覆盖 1/3 天" in n, \
        "只有第 2 天采过 → 1/3(同日两只也只算 1 天)"
    st = feed(_CAL[1])["stock"]["600000.SH"]
    # C1 后: 命中缓存的条目缺 Y1/Y8 而当日 float_mv 在 → 就地补齐(不进缓存:
    # 本 feed 是 offline)。Y5/Y2 仍按回测口径剔除。
    assert set(st["fund"]) == {"Y7", "Y1", "Y8"}, \
        "已缓存日: 网络类照常注入(Y5/Y2 剔除) + C1 补齐的纯计算类"


def test_fund_note_reports_network_stock_days(monkeypatch, tmp_path):
    """M1: `stats["net_got"]` 不再是"只写不读"的死状态 —— 网络类**股日**计数进说明。

    按天覆盖(1/3 天)看不出"这一天采到了几只", 股日计数补上这一层。
    """
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {"600000.SH": 3.0e9, "000001.SZ": 3.0e9})
    cache = _seed_cache(tmp_path, _CAL[1], ["600000.SH", "000001.SZ"])
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    for d in _CAL:
        feed(d)
    n = [x for x in feed.data_notes if x.startswith("基本面")][0]
    assert "网络类 2/3 股日" in n, \
        "第 2 天两只都命中 → 2/3 股日(asked=3: 第 1 天只有 1 根K线不进 stock)"

def test_fund_note_states_offline_and_backfill_cmd(monkeypatch, tmp_path):
    """data_notes 新增说明: 覆盖天数 + "默认只读缓存不联网" + 逐日回填命令。"""
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {})
    cache = _seed_cache(tmp_path, _CAL[1], ["600000.SH"])
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=True))
    feed(_CAL[1])
    notes = [n for n in feed.data_notes if n.startswith("基本面")]
    assert len(notes) == 1, "基本面说明必须只有一条(自我覆盖, 不挤占)"
    n = notes[0]
    assert "只读" in n and "不联网" in n, "必须声明默认离线"
    assert "F7/Y6/Y7 覆盖 1/1 天" in n, "必须给出覆盖天数(精确片段, 防 11/ 蒙混)"
    assert "python -m prism.fund_snapshot --date YYYYMMDD" in n, "必须给出回填命令"
    assert "已联网取数" not in n, "默认离线不许标成已联网"
    assert "--fetch-fund" in n, "离线时要点明联网取数的开关"


def test_fund_note_marks_fetch_mode(monkeypatch, tmp_path):
    """--fetch-fund(联网) → 同一条说明要标注「本次已联网取数(慢)」。"""
    from datasource.fundamental import FundamentalFeed
    cli = _stub_day_io(monkeypatch, {})
    cache = _seed_cache(tmp_path, _CAL[1], ["600000.SH"])
    feed = cli.build_day_feed(
        _CAL[0], _CAL[2],
        fund_feed=FundamentalFeed(cache_path=cache, offline=False))
    feed(_CAL[1])
    n = [x for x in feed.data_notes if x.startswith("基本面")][0]
    assert "本次已联网取数" in n and "慢" in n


# ---------------- ⑨ 守护 15:05 选股后挂钩子 ----------------

_CAND = [{"code": "600000.SH", "up_stop_price": 10.0,
          "scores": {"composite": 5.0}}]


class _FakeProvider:
    def __init__(self):
        from prism.context import FactorContext
        self._ctx = FactorContext

    def build_market_context(self):
        return self._ctx(code="__MARKET__", limit_ups=_CAND)

    def get_limit_ups(self):
        return _CAND

    def build_stock_context(self, code, **kw):
        return self._ctx(code=code)

    def invalidate(self):
        pass

    class ds:
        @staticmethod
        def get_full_market_ticks():
            return {"600000.SH": {"lastPrice": 9.8}}

        @staticmethod
        def get_kline(code, days=1):
            import pandas as pd
            return pd.DataFrame({"close": [9.8], "low": [9.7],
                                 "high": [10.0]}, index=["20260917"])


def _daemon(tmp_path, monkeypatch, fund_snapshot_fn):
    from prism.paper import PaperAccount
    from prism.paper_daemon import PaperDaemon
    import prism.engine
    monkeypatch.setattr(
        prism.engine, "run_screen",
        lambda s, m, gate_factors=None, stock_contexts=None:
        {"environment_ok": False, "gate_score": 0, "candidates": [],
         "summary": {"candidate_count": 0}})
    acc = PaperAccount(state_path=tmp_path / "paper.json")
    acc.init_account(created="2026-09-01")
    d = PaperDaemon(acc, zt_refresh_fn=lambda: {"injected_noop": True},
                    fund_snapshot_fn=fund_snapshot_fn)
    d.provider = _FakeProvider()
    return d, acc


def _patch_pick(acc, monkeypatch):
    """假 pick: 与真 pick_top5_at_close 同样登记幂等键(真实现 ts_key="pickT"+slot)。"""
    def fake_pick(provider, now=None, slot=None):
        acc.state["screens_done"].append("pickT%s" % slot)
        return {"picked": [], "env_ok": True}
    monkeypatch.setattr(acc, "pick_top5_at_close", fake_pick)


def test_daemon_pick_triggers_fund_snapshot(tmp_path, monkeypatch):
    """收盘选股分支: pick 落定后触发基本面快照钩子(daemon 线程)。"""
    done, calls = threading.Event(), []

    def fake_snap():
        calls.append(1)
        done.set()
        return {"saved": 1, "failed": 0}
    d, acc = _daemon(tmp_path, monkeypatch, fake_snap)
    _patch_pick(acc, monkeypatch)
    out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
    assert out["action"] == "pick"
    assert done.wait(5), "选股后必须触发基本面快照采集"
    assert calls == [1]


def test_daemon_fund_snapshot_failure_does_not_break_pick(tmp_path, monkeypatch,
                                                          caplog):
    """钩子炸 → 只落 warning, 选股返回值原样; 后续 tick 正常。"""
    entered = threading.Event()

    def boom():
        entered.set()
        raise RuntimeError("快照炸了")
    d, acc = _daemon(tmp_path, monkeypatch, boom)
    _patch_pick(acc, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="paper_daemon"):
        out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
    assert out["action"] == "pick"
    assert out["pick"]["env_ok"] is True, "选股结果不受钩子异常影响"
    assert entered.wait(5)
    for _ in range(100):                     # 等 worker 落日志(有界轮询)
        if "基本面快照采集失败" in caplog.text:
            break
        time.sleep(0.05)
    assert "基本面快照采集失败" in caplog.text
    out = d.tick_once(now=datetime(2026, 9, 17, 15, 10))
    assert out["action"] == "idle", "主循环不受影响"


def test_daemon_fund_snapshot_not_fired_outside_pick(tmp_path, monkeypatch):
    """非选股 tick(午休 idle)不触发钩子。"""
    calls = []
    d, acc = _daemon(tmp_path, monkeypatch, lambda: calls.append(1))
    out = d.tick_once(now=datetime(2026, 9, 17, 12, 0))
    assert out["action"] == "idle"
    assert calls == []


def _wait_until(pred, timeout, step=0.02):
    """有界轮询(不靠裸 sleep 撞竞态): 命中 → True; 超时 → 再判一次。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def test_daemon_fund_snapshot_waits_for_zt_refresh(tmp_path, monkeypatch):
    """I3: 采集必须先等涨停池刷新线程结束。

    默认钩子按本地 zt 索引取当日池, 而索引正被 _maybe_refresh_zt 在**另一个
    线程**里重建 —— 不等就会在 15:05 拿到空池 → 空采丢当天 Y2/Y5。
    """
    started, release = threading.Event(), threading.Event()
    order = []

    def slow_refresh():
        started.set()
        release.wait(5)
        order.append("refresh")
        return {"ok": True}

    def fake_snap():
        order.append("snapshot")
        return {"saved": 1, "failed": 0}
    d, acc = _daemon(tmp_path, monkeypatch, fake_snap)
    d.zt_refresh_fn = slow_refresh            # 换成"慢刷新"
    _patch_pick(acc, monkeypatch)
    out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
    assert out["action"] == "pick"
    assert started.wait(5), "收盘选股后必须先孵化涨停池刷新"
    assert _wait_until(lambda: "snapshot" in order, 1.0) is False, \
        "刷新还没结束 → 采集绝不许开跑(否则空采)"
    release.set()
    assert _wait_until(lambda: "snapshot" in order, 5.0), "刷新结束后必须采集"
    assert order == ["refresh", "snapshot"], "顺序必须是: 刷新完成 → 采集"


def test_daemon_fund_snapshot_empty_pool_warns_not_success(tmp_path, monkeypatch,
                                                           caplog):
    """I3: 空池 → WARNING 点名真因, 且不许打"采集完成"这种成功日志。"""
    def fake_snap():
        return {"saved": 0, "failed": 0, "skipped": "empty_pool"}
    d, acc = _daemon(tmp_path, monkeypatch, fake_snap)
    _patch_pick(acc, monkeypatch)
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
        assert out["action"] == "pick"
        assert _wait_until(lambda: "未采集" in caplog.text, 5.0), \
            "空采必须落日志(不许静默)"
    assert "WARNING" in caplog.text and "尚未刷新" in caplog.text
    assert "基本面快照采集完成" not in caplog.text, "空池不许长得像成功"


def test_daemon_default_fund_snapshot_fn_exists(tmp_path):
    """不注入时也有默认钩子(真实路径), 且与 zt_refresh_fn 同为可注入点。"""
    from prism.paper import PaperAccount
    from prism.paper_daemon import PaperDaemon
    acc = PaperAccount(state_path=tmp_path / "d.json")
    acc.init_account(created="2026-09-01")
    d = PaperDaemon(acc)
    assert callable(d.fund_snapshot_fn)


# ---------------- ⑫ C1: 缓存命中时补齐纯计算因子(Y1/Y8) ----------------

_SNAP_DAY = "20260915"


def test_snapshot_without_float_mv_then_hit_with_float_mv_gains_y1_y8(tmp_path):
    """C1: 快照回填的历史日条目**不含** Y1/Y8(快照不传 float_mv); 之后回测带
    当日 float_mv 以 asof=该日取数, **命中缓存也必须补齐** Y1/Y8。

    否则 `if key in self._cache: return` 短路 → 该 (股,日) 的 Y1/Y8 被永久钉成 0
    (回测报告写 0, 数据说明却宣称"Y1/Y8 不受缓存覆盖影响" —— 假话)。
    """
    feed = _feed(tmp_path, _base_routes())
    res = fund_snapshot.snapshot_once(["000001.SZ"], date=_SNAP_DAY, feed=feed)
    assert res == {"saved": 1, "failed": 0}
    entry = _read_cache(tmp_path)["%s:000001.SZ" % _SNAP_DAY]
    assert "Y1" not in entry and "Y8" not in entry, \
        "快照不带 float_mv → 条目里没有纯计算值(这就是 C1 的起点)"
    assert "Y6" in entry and "Y7" in entry, "网络类照常落下"

    hit = _feed(tmp_path, _base_routes(), offline=True).compute_for_stock(
        "000001.SZ", float_mv=50e8, asof=date(2026, 9, 15))
    assert {"Y1", "Y8"} <= set(hit), "命中缓存 + 有 float_mv → Y1/Y8 必须补齐"
    assert hit["Y1"]["score"] == 1 and hit["Y8"]["score"] == 1, \
        "50亿 流通市值: 小市值命中 + 中市值启动命中"
    assert hit["Y6"] == entry["Y6"], "缓存里的网络因子原样返回"


def test_offline_hit_completion_not_persisted(tmp_path):
    """C1: offline 命中时补齐只进**返回副本** —— 绝不落盘(离线不写缓存不变)。"""
    feed = _feed(tmp_path, _base_routes())
    fund_snapshot.snapshot_once(["000001.SZ"], date=_SNAP_DAY, feed=feed)
    p = tmp_path / "fund_cache.json"
    raw = p.read_text(encoding="utf-8")

    out = _feed(tmp_path, _base_routes(), offline=True).compute_for_stock(
        "000001.SZ", float_mv=50e8, asof=date(2026, 9, 15))
    assert {"Y1", "Y8"} <= set(out)
    assert p.read_text(encoding="utf-8") == raw, "offline 只读不写"


def test_online_hit_completion_written_back(tmp_path):
    """C1: 非 offline 命中时补齐结果原子写回 → 条目自此自足(下次不带 float_mv 也有)。"""
    feed = _feed(tmp_path, _base_routes())
    fund_snapshot.snapshot_once(["000001.SZ"], date=_SNAP_DAY, feed=feed)

    out = _feed(tmp_path, _base_routes()).compute_for_stock(
        "000001.SZ", float_mv=50e8, asof=date(2026, 9, 15))
    assert {"Y1", "Y8"} <= set(out)
    entry = _read_cache(tmp_path)["%s:000001.SZ" % _SNAP_DAY]
    assert {"Y1", "Y8"} <= set(entry), "补齐结果必须写回(否则每次都要重算)"
    assert entry["Y6"] == out["Y6"], "写回不许丢/改既有网络因子"
    # 已补齐的条目: 之后不带 float_mv 也能拿到 Y1/Y8
    assert {"Y1", "Y8"} <= set(
        _feed(tmp_path, _base_routes(), offline=True).compute_for_stock(
            "000001.SZ", asof=date(2026, 9, 15)))


def test_hit_with_complete_entry_not_rewritten(tmp_path):
    """C1: 命中且条目**已含** Y1/Y8 → 不重复写(缓存文件逐字节不变)。"""
    p = tmp_path / "fund_cache.json"
    entry = {"Y1": {"score": 0, "note": "缓存里的 Y1"},
             "Y8": {"score": 0, "note": "缓存里的 Y8"},
             "Y6": {"score": 1, "note": "缓存里的 Y6"}}
    p.write_text(json.dumps({"%s:000001.SZ" % _SNAP_DAY: entry},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    raw = p.read_text(encoding="utf-8")

    out = _feed(tmp_path, _base_routes()).compute_for_stock(
        "000001.SZ", float_mv=50e8, asof=date(2026, 9, 15))
    assert out == entry, "已有纯计算值 → 原样返回(不覆盖缓存里的既有结论)"
    assert p.read_text(encoding="utf-8") == raw, "无新增因子 → 不许写盘"


def test_hit_without_float_mv_adds_nothing(tmp_path):
    """C1: 命中但 float_mv 为假(缺股本) → 什么都不补、不写盘(无从算起, 不猜)。"""
    feed = _feed(tmp_path, _base_routes())
    fund_snapshot.snapshot_once(["000001.SZ"], date=_SNAP_DAY, feed=feed)
    p = tmp_path / "fund_cache.json"
    raw = p.read_text(encoding="utf-8")

    out = _feed(tmp_path, _base_routes()).compute_for_stock(
        "000001.SZ", asof=date(2026, 9, 15))
    assert "Y1" not in out and "Y8" not in out
    assert p.read_text(encoding="utf-8") == raw, "无 float_mv → 不补也不写"


# ---------------- ⑬ I3: 空池 → 无视节流强制刷新 + 重试一次 ----------------

def test_daemon_empty_pool_forces_refresh_and_retries(tmp_path, monkeypatch,
                                                      caplog):
    """I3: 空池 → **无视 6h 节流**强制刷新涨停池索引, 再重试一次采集。

    场景(真实): 守护在 15:00~15:05 之间重启 —— 启动那次刷新跑完时索引里还没有
    当日池, 15:05 的刷新被 6h 节流挡掉, `self._zt_thread` 是**已完成**线程 →
    join 立即返回 → `default_codes()` 返回 [] → 空采, 当天 Y2/Y5 永久缺失。
    """
    results = [{"saved": 0, "failed": 0, "skipped": "empty_pool"},
               {"saved": 3, "failed": 0}]
    refresh_calls, refresh_done = [], threading.Event()

    def fake_snap():
        return results.pop(0) if results else {"saved": 3, "failed": 0}

    def fake_refresh():
        refresh_calls.append(1)
        refresh_done.set()
        return {"ok": True}
    d, acc = _daemon(tmp_path, monkeypatch, fake_snap)
    d.zt_refresh_fn = fake_refresh
    d._zt_refresh_at = time.time()          # 6h 节流窗口内 → 常规路径会跳过刷新
    _patch_pick(acc, monkeypatch)
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
        assert out["action"] == "pick"
        assert _wait_until(lambda: not results, 5.0), "空池后必须重试一次采集"
        assert _wait_until(lambda: "基本面快照采集完成" in caplog.text, 5.0)
    assert refresh_calls == [1], "空池 → 必须无视节流强制刷新一次(且只一次)"
    assert refresh_done.is_set(), "强制刷新必须真跑完(重试前要等它)"
    assert "未采集" not in caplog.text, "重试成功后不许再报未采集"


def test_daemon_empty_pool_after_retry_warns(tmp_path, monkeypatch, caplog):
    """I3: 强制刷新+重试后仍空 → 只重试一次, 落 WARNING(不无限重试)。"""
    calls = []

    def fake_snap():
        calls.append(1)
        return {"saved": 0, "failed": 0, "skipped": "empty_pool"}
    d, acc = _daemon(tmp_path, monkeypatch, fake_snap)
    d.zt_refresh_fn = lambda: {"ok": True}
    _patch_pick(acc, monkeypatch)
    with caplog.at_level(logging.INFO, logger="paper_daemon"):
        out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
        assert out["action"] == "pick"
        assert _wait_until(lambda: len(calls) >= 2, 5.0), "必须重试一次(共 2 次)"
        assert _wait_until(lambda: "未采集" in caplog.text, 5.0)
        time.sleep(0.3)                     # 给"万一无限重试"留出暴露窗口
    assert len(calls) == 2, "只重试一次: 仍空就告警, 不空转"
    assert "WARNING" in caplog.text and "尚未刷新" in caplog.text
    assert "基本面快照采集完成" not in caplog.text


def test_daemon_snapshot_wait_times_out_and_still_collects(tmp_path, monkeypatch,
                                                           caplog):
    """M3: 刷新线程卡死 → 最多等 ZT_REFRESH_WAIT 秒, 超时照常采集 + 落 WARNING。"""
    import prism.paper_daemon as pd
    monkeypatch.setattr(pd, "ZT_REFRESH_WAIT", 0.05)
    started, release = threading.Event(), threading.Event()
    snap_done = threading.Event()

    def stuck_refresh():
        started.set()
        release.wait(5)
        return {"ok": True}

    def fake_snap():
        snap_done.set()
        return {"saved": 1, "failed": 0}
    d, acc = _daemon(tmp_path, monkeypatch, fake_snap)
    d.zt_refresh_fn = stuck_refresh
    _patch_pick(acc, monkeypatch)
    try:
        with caplog.at_level(logging.WARNING, logger="paper_daemon"):
            out = d.tick_once(now=datetime(2026, 9, 17, 15, 6))
            assert out["action"] == "pick"
            assert started.wait(5), "选股后必须先孵化刷新线程"
            assert snap_done.wait(5), "超时后必须照常采集(不许无限等刷新)"
            assert _wait_until(lambda: "未结束" in caplog.text, 5.0), \
                "超时必须有 WARNING 说明(取到的可能是旧索引)"
    finally:
        release.set()
