# -*- coding: utf-8 -*-
"""按日上下文注入(day_feed) + 日线 OHLCV 6 元组契约 — 规格 §4 的离线测试。

覆盖:
  ① 静态 em/ticks 仍生效(向后兼容回归: 旧调用/旧测试的契约不许破坏)
  ② day_feed 的 em.max_boards 逐日生效(同一策略两天不同门控 → 只有一天成交)
  ③ day_feed 的 stock[code].up_price/last → 真实 F1(首板确认)命中
  ④ day_feed 的 sh_index_kline → 真实 F6(大盘配合)命中
  ⑤ kline 6 元组 → 真实成交量可读(旧 2 元组仍占位 1.0 不造假)
  ⑥ 6 元组契约下成交价取收盘价(不是开盘价)
  ⑦ day_feed 缺键 / 返回 None → 静态参数兜底, 不崩
  ⑧ _day_payload(纯函数装配): em/ticks/指数/个股口径(离线夹具, 不连 QMT)

全离线: 不调 QMT/网络(真 QMT 取数只在 backtest/cli.py 的取数函数里)。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.factors  # noqa: F401  触发真实因子注册(F1/F6/N1/N4 参与断言)
import prism.registry as reg
from backtest.cli import _day_payload, _upto  # noqa: E402
from prism import backtest
from prism.engine import load_strategy

START = date(2026, 7, 1)
LIMIT_DAY = date(2026, 7, 7)          # 600000.SH 首板日(涨停池当天只有它)


def _day8(d):
    return d.strftime("%Y%m%d")


def _kline2(closes, start=START):
    """旧契约: [(date, close), ...]。"""
    return [((start + timedelta(days=i)).strftime("%Y-%m-%d"), c)
            for i, c in enumerate(closes)]


def _kline6(closes, volumes=None, start=START):
    """全量契约: [(date, open, high, low, close, volume), ...]。"""
    vols = volumes if volumes is not None else [1500.0] * len(closes)
    out = []
    for i, c in enumerate(closes):
        dt = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        # open 刻意与 close 不同: 买价取错的测试(⑥)靠它区分
        out.append((dt, c * 0.9, c * 1.05, c * 0.88, c, vols[i]))
    return out


def _mk_strategy(scoring=("F1",), gate=("N1",), threshold=1, min_model=1):
    return load_strategy({
        "id": "bt_dayfeed", "name": "按日注入回测", "description": "",
        "market_gate": {"model": "node", "threshold": threshold,
                        "factors": list(gate)},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": f, "op": ">", "threshold": 0}
                         for f in scoring]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": min_model,
                    "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.01, "stop_loss_pct": 0.5,
                       "max_hold_days": 5},
    })


def _bt(strategy, zt, kf):
    """零成本回测器: 只验语义, 不让滑点/税费混淆断言。"""
    return backtest.Backtester(strategy, zt_feed=zt, kline_feed=kf,
                               fee_rate=0.0, slippage=0.0, stamp_duty=0.0,
                               transfer_fee=0.0)


@pytest.fixture(autouse=True)
def _real_factors():
    """真实因子库(F1/F6/N4/...)在场: 本文件的断言直接验"复活"效果。"""
    reg.reset()
    reg.scan_factors("prism.factors", force=True)
    yield


def _pool_zt(days, code="600000.SH", boards=1):
    def zf(d):
        return [{"code": code, "boards": boards}] if d in days else []
    return zf


# ---------------- ① 静态参数回归 ----------------

def test_static_em_still_gates():
    """① 静态 em 仍生效: N4 读 em.max_boards, 5 板开门 / 2 板关门。"""
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    s = _mk_strategy(scoring=("T1",), gate=("N4",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), lambda c: _kline2(closes))
    ok = bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 5})
    assert ok["trades"] == 1
    bad = bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 2})
    assert bad["trades"] == 0
    assert bad["filter_stats"]["gate_blocked_days"] == 1


# ---------------- ② 逐日 em ----------------

def test_day_feed_em_switches_per_day():
    """② day_feed 的 em 逐日生效: 07-07 max_boards=2 门关, 07-08 =5 门开。

    静态注入做不到这一点(全程同一份) —— 这正是"按日快照"要解决的事。
    """
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    s = _mk_strategy(scoring=("T1",), gate=("N4",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    by_day = {date(2026, 7, 7): {"em": {"max_boards": 2}},
              date(2026, 7, 8): {"em": {"max_boards": 5}}}
    bt = _bt(s, _pool_zt({_day8(date(2026, 7, 7)), _day8(date(2026, 7, 8))}),
             lambda c: _kline2(closes))
    rep = bt.run(date(2026, 7, 7), date(2026, 7, 9),
                 day_feed=lambda d: by_day.get(d))
    assert {t["date"] for t in rep["trade_log"]} == {"2026-07-08"}
    assert rep["filter_stats"]["gate_blocked_days"] == 1


# ---------------- ③ F1 复活 ----------------

def test_day_feed_stock_revives_f1():
    """③ stock[code] 的 up_price/last → 真实 F1 在回测中命中(改前恒 0)。"""
    s = _mk_strategy(scoring=("F1",), gate=("N1",))
    # 前 6 日横盘 + 首板日涨停收盘(11.0 = 10.0×1.1), 之后继续走高供卖出
    closes = [10.0] * 6 + [11.0, 11.2, 11.4]
    kf = lambda c: _kline2(closes)                            # noqa: E731
    stock = {"600000.SH": {"up_price": 11.0, "last": 11.0,
                           "last_close": 10.0, "float_vol": 3.0e9}}
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), kf)
    rep = bt.run(LIMIT_DAY, date(2026, 7, 9),
                 day_feed=lambda d: ({"stock": stock}
                                     if d == LIMIT_DAY else None))
    assert rep["trades"] == 1
    assert rep["trade_log"][0]["date"] == "2026-07-07"
    # 不注入 → up_price 缺失 → F1 得 0 → 无候选(旧行为, 旧测试依赖)
    assert _bt(s, _pool_zt({_day8(LIMIT_DAY)}), kf).run(
        LIMIT_DAY, date(2026, 7, 9))["trades"] == 0


# ---------------- ④ F6 复活 ----------------

def test_day_feed_sh_index_revives_f6():
    """④ sh_index_kline(26 根) → 真实 F6 命中; 不注入 → 缺大盘数据得 0。"""
    s = _mk_strategy(scoring=("F6",), gate=("N1",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    kf = lambda c: _kline2(closes)                            # noqa: E731
    idx_start = date(2026, 6, 1)
    sh = [((idx_start + timedelta(days=i)).strftime("%Y-%m-%d"),
           3800.0 + i * 5) for i in range(26)]
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), kf)

    def feed(d):
        if d != LIMIT_DAY:
            return None
        return {"sh_index_kline": [r for r in sh
                                   if r[0] <= d.strftime("%Y-%m-%d")]}

    assert bt.run(LIMIT_DAY, date(2026, 7, 8), day_feed=feed)["trades"] == 1
    assert bt.run(LIMIT_DAY, date(2026, 7, 8))["trades"] == 0


# ---------------- ⑤⑥ 6 元组契约 ----------------

def test_kline6_volume_reaches_factors():
    """⑤ 6 元组 K线 → 因子读到真实成交量(旧契约占位 1.0 会误判)。"""
    @reg.factor(id="V1", name="v", category="test", description="")
    def f_v(ctx):
        k = ctx.kline
        if k is None or len(k) == 0:
            return {"score": 0, "note": "无K线"}
        vol = float(k["volume"].tolist()[-1])
        return {"score": 1 if vol > 1000 else 0, "note": "vol=%.1f" % vol}

    s = _mk_strategy(scoring=("V1",), gate=("N1",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    vols = [2500.0 + i for i in range(10)]
    zt = _pool_zt({_day8(LIMIT_DAY)})
    assert _bt(s, zt, lambda c: _kline6(closes, vols)).run(
        LIMIT_DAY, date(2026, 7, 8))["trades"] == 1
    # 旧 2 元组契约: volume 占位 1.0 → 量能类因子静默失效(既有语义, 不造假)
    assert _bt(s, zt, lambda c: _kline2(closes)).run(
        LIMIT_DAY, date(2026, 7, 8))["trades"] == 0


def test_stock_ctx_supports_three_kline_contracts():
    """⑤ _stock_ctx 三档契约: 2 元组占位 1.0 / 3 元组量能 / 6 元组全量。"""
    s = _mk_strategy(scoring=("F1",))
    bt = backtest.Backtester(s, zt_feed=lambda d: [], kline_feed=lambda c: [])
    c2 = bt._stock_ctx("600000.SH", [("2026-07-01", 10.0)])
    assert list(c2.kline["volume"]) == [1.0]
    c3 = bt._stock_ctx("600000.SH", [("2026-07-01", 10.0, 1234.0)])
    assert list(c3.kline["volume"]) == [1234.0]
    c6 = bt._stock_ctx("600000.SH",
                       [("2026-07-01", 9.0, 10.5, 8.8, 10.0, 2500.0)])
    assert list(c6.kline["close"]) == [10.0]
    assert list(c6.kline["open"]) == [9.0]
    assert list(c6.kline["volume"]) == [2500.0]


def test_kline6_trade_uses_close_not_open():
    """⑥ 6 元组契约下成交价=收盘价(买价口径: 选股日收盘, 用户已拍板)。

    6 元组 row[1] 是开盘价: 若按长度盲取 [1], 买价会静默变成开盘价。
    """
    s = _mk_strategy(scoring=("N1",), gate=("N1",))
    closes = [10.0, 10.5, 11.0, 11.5, 12.0]
    bt = _bt(s, _pool_zt({_day8(START)}), lambda c: _kline6(closes))
    rep = bt.run(START, date(2026, 7, 2))
    assert rep["trades"] == 1
    assert rep["trade_log"][0]["entry"] == 10.0        # 收盘价, 非开盘 9.0
    assert rep["trade_log"][0]["exit"] == 10.5         # 次日收盘 10.5 止盈(非开盘)


# ---------------- ⑦ 兜底 ----------------

def test_day_feed_none_or_missing_key_falls_back_to_static():
    """⑦ day_feed 返回 None / 只给部分键 → 静态参数兜底, 不崩。"""
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    s = _mk_strategy(scoring=("T1",), gate=("N4",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), lambda c: _kline2(closes))
    assert bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 5},
                  day_feed=lambda d: None)["trades"] == 1
    # day_feed 只给 ticks(不给 em) → em 仍用静态参数
    rep = bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 5},
                 day_feed=lambda d: {"ticks": {}})
    assert rep["trades"] == 1


def test_day_feed_stock_extra_reaches_ctx():
    """⑧ stock_extra 落 ctx: 显式字段 + 未知键进 _extra(供 Task2/3 的 sealed/fund)。"""
    s = _mk_strategy(scoring=("F1",))
    bt = backtest.Backtester(s, zt_feed=lambda d: [], kline_feed=lambda c: [])
    ctx = bt._stock_ctx(
        "600000.SH", [("2026-07-01", 9.0, 11.0, 8.9, 10.0, 2500.0)],
        stock_extra={"sealed": True, "up_price": 11.0, "last": 10.0,
                     "last_close": 9.0, "float_vol": 3.0e9,
                     "float_mv": 3.0e10, "bt_seal_ratio": 0.001,
                     "fund": {"F7": {"score": 1, "note": "t"}}})
    assert ctx.sealed is True
    assert ctx.up_price == 11.0
    assert ctx.last == 10.0 and ctx.last_close == 9.0
    assert ctx.float_mv == 3.0e10
    assert ctx.get("float_vol") == 3.0e9
    assert ctx.get("bt_seal_ratio") == 0.001        # 未知键 → _extra
    assert ctx.fund == {"F7": {"score": 1, "note": "t"}}
    # 值为 None 的键不覆盖(宁缺勿假): 不传 stock_extra 时字段保持 None
    ctx2 = bt._stock_ctx("600000.SH", [("2026-07-01", 10.0)],
                         stock_extra={"last": None, "float_mv": None})
    assert ctx2.last is None and ctx2.float_mv is None


def test_stock_ctx_receives_index_kline_for_f6():
    """④ sh_index_kline 进个股 ctx(F6 是评分因子, 只在个股 ctx 上求值)。"""
    s = _mk_strategy(scoring=("F6",))
    bt = backtest.Backtester(s, zt_feed=lambda d: [], kline_feed=lambda c: [])
    sh = [("2026-06-%02d" % (i + 1), 3800.0 + i) for i in range(26)]
    ctx = bt._stock_ctx("600000.SH", [("2026-07-01", 10.0)],
                        sh_index_kline=sh)
    assert reg.get_factor("F6")["func"](ctx)["score"] == 1


# ---------------- ⑧ _day_payload 装配口径(纯函数, 离线) ----------------

CAL = [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9)]
DAY = CAL[3]                                   # 决策日 07-09
# 指数日线行: (iso_date, close, volume, amount) —— amount 供 N5 两市成交额
IDX = {
    "000001.SH": [("2026-07-06", 3800.0, 4.0e8, 8.7e11),
                  ("2026-07-07", 3850.0, 4.1e8, 9.0e11),
                  ("2026-07-08", 3860.0, 4.2e8, 9.1e11),
                  ("2026-07-09", 3870.0, 4.3e8, 9.2e11)],
    "399001.SZ": [("2026-07-06", 13000.0, 5.0e8, 9.6e11),
                  ("2026-07-08", 13100.0, 5.1e8, 9.8e11),
                  ("2026-07-09", 13200.0, 5.2e8, 9.9e11)],
}
KLINE_CLOSES = [10.0, 10.5, 11.0, 12.0]        # 07-06 ~ 07-09


def _pools():
    return {
        CAL[0]: [{"code": "600000.SH", "boards": 3},
                 {"code": "300001.SZ", "boards": 2}],          # 家数 2
        CAL[1]: [{"code": "600000.SH", "boards": 1},
                 {"code": "300001.SZ", "boards": 1},
                 {"code": "688001.SH", "boards": 1}],          # 家数 3
        CAL[2]: [{"code": "600000.SH", "boards": 4},
                 {"code": "300001.SZ", "boards": 1}],          # 家数 2
        CAL[3]: [{"code": "600000.SH", "boards": 1},
                 {"code": "300001.SZ", "boards": 1},
                 {"code": "688001.SH", "boards": 1}],          # 家数 3
    }


def _klines():
    return {"600000.SH": _kline6(KLINE_CLOSES, volumes=[100, 200, 300, 400],
                                 start=CAL[0]),
            "300001.SZ": _kline6(KLINE_CLOSES, start=CAL[0]),
            "688001.SH": _kline6(KLINE_CLOSES, start=CAL[0])}


def test_day_payload_em_market_and_stock_fields():
    """⑧ em(最高连板/昨日池/近5日家数) + stock(涨停价按档位) + ticks 口径。"""
    p = _day_payload(DAY, _pools(), CAL, _klines(), {"600000.SH": 3.3e9}, IDX)
    # 门控: 今日最高连板 = 1(昨日池里那 4 板是"昨日"的); 昨日池 = 07-08 那天
    assert p["em"]["max_boards"] == 1
    assert p["em"]["yesterday_codes"] == ["600000.SH", "300001.SZ"]
    assert p["em"]["yesterday_boards"] == ["600000.SH"]      # 仅连板≥2
    assert p["em"]["daily_counts"] == [2, 3, 2]              # 近→远, 与东财同序
    # 个股: 涨停价 = 昨收 × 档位(主板10% / 创业板20%), 四舍五入两位
    st = p["stock"]["600000.SH"]
    assert st["last"] == 12.0 and st["last_close"] == 11.0
    assert st["up_price"] == pytest.approx(12.1)
    assert st["float_vol"] == 3.3e9
    assert st["float_mv"] == pytest.approx(3.3e9 * 12.0)
    assert p["stock"]["300001.SZ"]["up_price"] == pytest.approx(13.2)  # 11.0×1.2
    assert "float_vol" not in p["stock"]["300001.SZ"]        # 缺股本 → 不造假
    # ticks: 指数条只带 amount(N5 求和), 池内股只带 lastPrice/lastClose(N3)
    assert p["ticks"]["000001.SH"] == {"amount": 9.2e11}
    assert p["ticks"]["399001.SZ"] == {"amount": 9.9e11}
    assert p["ticks"]["600000.SH"] == {"lastPrice": 12.0, "lastClose": 11.0}
    assert "amount" not in p["ticks"]["600000.SH"], "个股成交额已含在两市成交额里"
    # 指数序列: sh 逐日切片(≤d; (date, close, volume) 供 F6), index_kline = 池子合成的涨停家数
    assert [r[1] for r in p["sh_index_kline"]] == [3800.0, 3850.0, 3860.0, 3870.0]
    assert p["sh_index_kline"][-1][2] == 4.3e8
    assert p["index_kline"] == [("2026-07-06", 2), ("2026-07-07", 3),
                               ("2026-07-08", 2), ("2026-07-09", 3)]


def test_day_payload_skips_stock_without_prev_close():
    """⑧ 无昨收(上市首日/数据缺失) → 算不出涨停价, 宁缺勿假: 不进 stock/ticks。"""
    klines = {"600000.SH": _kline6([10.0], start=CAL[1])}
    pools = {CAL[0]: [], CAL[1]: [{"code": "600000.SH", "boards": 1}]}
    p = _day_payload(CAL[1], pools, [CAL[0], CAL[1]], klines, {}, {})
    assert p["stock"] == {}
    assert p["ticks"] == {}
    assert p["em"]["max_boards"] == 1
    assert "daily_counts" not in p["em"]      # 不足 3 天 → 与东财口径一致地不给


def test_day_payload_slices_index_series_to_30_bars():
    """⑧ 指数序列只取 ≤d 的最后 30 根(F6 只需 21 根, 不搬全历史)。"""
    rows = [("2026-06-%02d" % (i + 1), 3800.0 + i, 1.0e8, 1.0e11)
            for i in range(28)]
    rows.append(("2026-07-06", 3900.0, 1.0e8, 1.0e11))
    p = _day_payload(CAL[0], {CAL[0]: []}, [CAL[0]], {}, {},
                     {"000001.SH": rows})
    assert len(p["sh_index_kline"]) == 29
    assert p["sh_index_kline"][-1][0] == "2026-07-06"
    # 40 根 → 截到最后 30
    rows2 = [("2026-05-%02d" % (i + 1), 3700.0 + i, 1.0e8, 1.0e11)
             for i in range(30)]
    rows2 += [("2026-07-%02d" % (i + 1), 3800.0 + i, 1.0e8, 1.0e11)
              for i in range(10)]
    p2 = _day_payload(date(2026, 7, 10), {date(2026, 7, 10): []},
                      [date(2026, 7, 10)], {}, {}, {"000001.SH": rows2})
    assert len(p2["sh_index_kline"]) == 30
    assert p2["sh_index_kline"][-1][0] == "2026-07-10"


# ---------------- ⑨ QMT DataFrame → 上下文行(日期归一) ----------------

def test_index_rows_normalize_yyyymmdd_index_key():
    """⑨ 指数行日期必须归一到 ISO: get_local_data 收窄 field_list 后不带
    time 列, 日期只剩索引键 "20260904" → 与 ISO 的逐日切片比较时被整段滤掉
    (实跑抓到过: sh_index_kline/两市成交额双双静默为空 → F6/N5 恒 0)。
    """
    import pandas as pd
    from backtest.cli import _df_to_index_rows
    # 无 time 列(只取部分字段): 索引是 YYYYMMDD
    df = pd.DataFrame({"close": [3900.0, 3850.0], "volume": [4.0e8, 3.9e8],
                       "amount": [9.0e11, 8.8e11]},
                      index=["20260904", "20260903"])
    rows = _df_to_index_rows(df)
    assert rows == [("2026-09-03", 3850.0, 3.9e8, 8.8e11),
                    ("2026-09-04", 3900.0, 4.0e8, 9.0e11)]
    # 归一后逐日切片才命中(未归一 → 被 _upto 整段滤掉)
    assert _upto(rows, "2026-09-04") == rows
    # 有 time 列(全字段): 毫秒 epoch → ISO
    from datetime import datetime as _dt
    ms = int(_dt(2026, 9, 4).timestamp() * 1000)
    df2 = pd.DataFrame({"time": [ms], "close": [3900.0], "volume": [4.0e8],
                        "amount": [9.0e11]}, index=["20260904"])
    assert _df_to_index_rows(df2)[0][0] == "2026-09-04"
    assert _df_to_index_rows(None) == []
    assert _df_to_index_rows(pd.DataFrame({"close": [], "volume": [],
                                           "amount": []})) == []


def test_iso_kline_three_contracts_and_date_normalization():
    """⑨ _iso_kline: 三档契约 + 8 位日期归一 + 脏行丢弃(旧缓存兜底路径要用)。"""
    from backtest.cli import _iso_kline
    # 6 元组(真实量) + 8 位日期
    assert _iso_kline([("20260904", 9.0, 10.5, 8.8, 10.0, 2500.0)]) == [
        ("2026-09-04", 9.0, 10.5, 8.8, 10.0, 2500.0)]
    # 2 元组(zt 老缓存只有 close) → volume=None(不占位 1.0, 不造假)
    rows = _iso_kline([("2026-09-04", 10.0), ("2026-09-03", 9.9)])
    assert [r[0] for r in rows] == ["2026-09-03", "2026-09-04"]
    assert rows[-1][4] == 10.0 and rows[-1][5] is None
    # 脏行: 非法日期 / 非数值 → 丢弃, 不炸整段
    assert _iso_kline([("xx", 10.0), ("2026-09-04", None), (), None]) == []


def test_batch_klines_normalizes_suffix_but_keeps_pool_key(monkeypatch):
    """⑨ 东财兜底池给的是裸 6 位码 → 查 QMT 必须补后缀, 但键要跟池条目一致。

    QMT 只认带后缀代码(get_local_data('600000') 查不到); 而 _day_payload 用
    池条目的原样 code 取 klines —— 两头都得对上, 否则个股上下文全空
    (F1 又变恒 0)。离线: 桩掉 _qmt_daily, 不连 QMT。
    """
    import backtest.cli as cli
    seen = []

    def fake_qmt(code):
        seen.append(code)
        return [("20260904", 9.0, 10.5, 8.8, 10.0, 2500.0)]

    monkeypatch.setattr(cli, "_qmt_daily", fake_qmt)
    out = cli._batch_klines(["600000", "300001.SZ"])
    assert seen == ["600000.SH", "300001.SZ"]     # QMT 侧补后缀
    assert sorted(out) == ["300001.SZ", "600000"]  # 键 = 池条目原样 code
