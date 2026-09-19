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
  ⑨ 审查 I-1: 昨日池样本进 ticks(否则 N3 只统计连板样本 → 近乎恒 1)
  ⑩ 审查 I-2: 缺失判定三处同源(键存在且非 None; 空 dict 也算已提供)
  ⑪ 审查 I-3: 静态 em 与 day_feed em 同时非空 → day_feed 胜出 + 每日调用计数
  ⑫ build_day_feed 惰性构造(按需取数 + 每日缓存 + 返回值契约不变)

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


# ---------------- ⑨ 审查 I-1: 昨日池样本必须进 ticks ----------------
# 改前: ticks 只装当日涨停池 → N3(首板溢价 = 昨日涨停股今日表现)只看得到
# "昨涨停且今仍涨停"的连板样本, 均涨幅恒正 → N3 近乎恒 1(门控形同虚设)。

def test_day_payload_prev_pool_codes_reach_ticks_not_stock():
    """I-1: 昨日涨停、今日未涨停的股票 → 进 ticks(N3 样本), 不进 stock(F1)。"""
    prev, day = CAL[2], CAL[3]                 # prev_pool 独有的 688888.SH
    pools = {prev: [{"code": "688888.SH", "boards": 1}],
             day: [{"code": "600000.SH", "boards": 1}]}
    klines = {"600000.SH": _kline6([10.0, 11.0], start=prev),
              "688888.SH": _kline6([10.0, 9.0], start=prev)}
    p = _day_payload(day, pools, [prev, day], klines, {}, {})
    # N3 样本: 昨涨停股今日 -10%(未涨停) → 必须在 ticks 里, 否则 N3 只看连板样本
    assert p["ticks"]["688888.SH"] == {"lastPrice": 9.0, "lastClose": 10.0}
    # stock 只服务 F1(今日涨停), 昨池独有股不能进(否则 F1 假命中)
    assert "688888.SH" not in p["stock"]
    assert p["stock"]["600000.SH"]["last"] == 11.0


def _n3_case(move_pct, shared=False):
    """当日池 [600000.SH]; 昨日池 = 昨池独有股 300002.SZ(今日 move_pct)
    [+ 当日池那只 600000.SH(昨涨停今仍涨停, shared=True)]。"""
    prev, day = date(2026, 7, 8), date(2026, 7, 9)
    cal = [prev, day]
    prev_pool = [{"code": "300002.SZ", "boards": 1}]
    if shared:                       # 昨日涨停 + 今日仍涨停(连板样本)
        prev_pool.append({"code": "600000.SH", "boards": 1})
    pools = {prev: prev_pool, day: [{"code": "600000.SH", "boards": 1}]}
    klines = {"600000.SH": _kline6([10.0, 11.0, 11.5], start=prev),
              "300002.SZ": _kline6([10.0, 10.0 * (1 + move_pct),
                                    10.0 * (1 + move_pct)], start=prev)}
    return prev, day, pools, cal, klines


def _n3_run(pools, cal, klines, day):
    """N3 门控 + 真实因子, day_feed 由 _day_payload 装配(与 build_day_feed 同口径)。"""
    s = _mk_strategy(scoring=("N1",), gate=("N3",), threshold=1, min_model=1)
    bt = _bt(s, _pool_zt({_day8(day)}),
             lambda c: klines.get(c) or [])
    feed = lambda d: (_day_payload(d, pools, cal, klines, {}, {})  # noqa: E731
                      if d in cal else None)
    return bt.run(day, day, day_feed=feed)


def test_prev_pool_only_sample_opens_n3_gate():
    """I-1: 昨涨停股今日 +5%(涨但没涨停) → N3 命中 → 门开(改前: ticks 里没有它 → 0)。"""
    prev, day, pools, cal, klines = _n3_case(+0.05)
    rep = _n3_run(pools, cal, klines, day)
    assert rep["trades"] == 1
    assert rep["filter_stats"]["gate_blocked_days"] == 0


def test_prev_pool_losers_flip_n3_negative():
    """I-1 系统性高估: 昨池里"今未涨停"的亏钱样本反转均涨幅 → 门关。

    改前只统计连板样本(600000.SH +10%) → avg>0 → N3=1 → 门开(假信号);
    改后 300002.SZ -12% 进样本 → avg<0 → N3=0 → 门关。
    """
    prev, day, pools, cal, klines = _n3_case(-0.12, shared=True)
    rep = _n3_run(pools, cal, klines, day)
    assert rep["trades"] == 0
    assert rep["filter_stats"]["gate_blocked_days"] == 1


# ---------------- ⑩ 审查 I-2: 缺失判定三处同源 ----------------

def test_day_feed_empty_em_is_provided_not_missing():
    """I-2: day_feed 给 em={} → 视为"已提供"(门控用空 em 算), 不报"缺 em 数据"。

    改前: _pick 用 `is None`(空 dict 算有)、note_em 用真值(空 dict 算无)
    → 门控按空 em 判定, 归因却报"N4 缺em数据 → 0"(与实际不符)。
    """
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    s = _mk_strategy(scoring=("T1",), gate=("N4",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), lambda c: _kline2(closes))
    rep = bt.run(LIMIT_DAY, date(2026, 7, 8), day_feed=lambda d: {"em": {}})
    assert rep["trades"] == 0
    assert rep["filter_stats"]["gate_blocked_days"] == 1
    assert rep["gate_notes"] == [], "空 em 已提供, 不该归因为缺数据"


def test_day_feed_empty_em_beats_static_em():
    """I-2: 空 dict 也算已提供 → 静态 em 不顶替(判据 = 键存在且非 None)。"""
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    s = _mk_strategy(scoring=("T1",), gate=("N4",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), lambda c: _kline2(closes))
    rep = bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 5},
                 day_feed=lambda d: {"em": {}})
    # 静态 em 5 板本会开门; day_feed 给空 em → 无连板数据 → N4 兜底 0 → 门关
    assert rep["trades"] == 0
    assert rep["filter_stats"]["gate_blocked_days"] == 1
    assert rep["gate_notes"] == []


# ---------------- ⑪ 审查 I-3: 优先性 + 每日调用计数 ----------------

def test_day_feed_em_wins_over_static_when_both_non_empty():
    """I-3①: 静态 em 与 day_feed em 都非空且值不同 → day_feed 胜出(两向都验)。"""
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    s = _mk_strategy(scoring=("T1",), gate=("N4",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    bt = _bt(s, _pool_zt({_day8(LIMIT_DAY)}), lambda c: _kline2(closes))
    # 静态 5 板(开门) + day_feed 2 板(关门) → 门关 ⇒ 门控按 day_feed 判
    assert bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 5},
                  day_feed=lambda d: {"em": {"max_boards": 2}})["trades"] == 0
    # 反向: 静态 2 板(关门) + day_feed 5 板(开门) → 成交 ⇒ 静态没把 day_feed 顶掉
    rep = bt.run(LIMIT_DAY, date(2026, 7, 8), em={"max_boards": 2},
                 day_feed=lambda d: {"em": {"max_boards": 5}})
    assert rep["trades"] == 1


def test_day_feed_called_once_per_pool_day():
    """I-3②: day_feed 每日取用一次 —— 计数 == 有池交易日数(防将来改回静态仍绿)。"""
    @reg.factor(id="T1", name="t", category="test", description="")
    def f_t(ctx):
        return {"score": 1, "note": ""}

    days = [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]
    s = _mk_strategy(scoring=("T1",), gate=("N1",))
    closes = [10.0 * (1.02 ** i) for i in range(10)]
    calls = []
    bt = _bt(s, _pool_zt({_day8(d) for d in days}), lambda c: _kline2(closes))
    rep = bt.run(days[0], days[-1],
                 day_feed=lambda d: calls.append(d) or {"em": {"max_boards": 5}})
    assert calls == days, "每个回放日都要按日取一次上下文"
    assert len(calls) == rep["trading_days"] == 3


# ---------------- ⑫ build_day_feed 惰性构造 ----------------

def _lazy_io(monkeypatch):
    """离线桩掉 build_day_feed 的四类 IO(交易日历/指数/涨停池/日线), 记录调用。"""
    import backtest.cli as cli
    calls = {"pool": [], "kline": [], "float": []}
    iso_by_d8 = {_day8(d): d.strftime("%Y-%m-%d") for d in CAL}
    pools_by_iso = {d.strftime("%Y-%m-%d"): v for d, v in _pools().items()}
    kl = _klines()

    monkeypatch.setattr(cli, "_get_zt_index",
                        lambda: {_day8(d): 1 for d in CAL})
    monkeypatch.setattr(cli, "_index_daily", lambda *a, **k: list(IDX[a[0]]))

    def fake_zt(d8):
        calls["pool"].append(d8)
        return [dict(s) for s in pools_by_iso.get(iso_by_d8.get(d8), [])]

    def fake_klines(codes):
        calls["kline"].append(list(codes))
        return {c: kl[c] for c in codes if c in kl}

    monkeypatch.setattr(cli, "zt_feed", fake_zt)
    monkeypatch.setattr(cli, "_batch_klines", fake_klines)
    monkeypatch.setattr(cli, "_float_volumes",
                        lambda codes: calls["float"].append(list(codes)) or {})
    return cli, calls


def test_build_day_feed_is_lazy_and_caches(monkeypatch):
    """⑫ 装配返回时不取任何逐日数据(大窗口不预计算); 首次请求某日才算, 之后命中缓存。"""
    cli, calls = _lazy_io(monkeypatch)
    progress = []
    feed = cli.build_day_feed(
        CAL[0], CAL[3],
        progress=lambda done, total: progress.append((done, total)))
    assert calls["pool"] == [] and calls["kline"] == []
    assert progress == [], "惰性: 装配阶段不该报进度"
    ctx = feed(CAL[3])
    # 返回值契约与"全量预计算版"逐字段一致(同 _day_payload 口径)
    expected = _day_payload(CAL[3], _pools(), CAL, _klines(), {}, IDX)
    assert ctx == expected
    assert progress == [(1, 4)]          # progress(done, total): 已构造 / 区间交易日
    n_pool, n_kline = len(calls["pool"]), len(calls["kline"])
    assert n_pool and n_kline
    assert feed(CAL[3]) == ctx           # 同日再次请求 → 命中缓存
    assert len(calls["pool"]) == n_pool and len(calls["kline"]) == n_kline
    assert progress == [(1, 4)]
    assert feed(date(2026, 6, 30)) is None       # 区间外 → 静态参数兜底
    assert feed(date(2026, 7, 5)) is None        # 区间内非交易日(无池无指数) → None


def test_build_day_feed_fetches_prev_pool_before_start(monkeypatch):
    """⑫ 区间开始日的"昨日池"按需补取(区间前一日只作上下文, 本身返回 None)。"""
    cli, calls = _lazy_io(monkeypatch)
    feed = cli.build_day_feed(CAL[1], CAL[3])
    assert feed(CAL[0]) is None
    ctx = feed(CAL[1])
    assert ctx["em"]["yesterday_codes"] == ["600000.SH", "300001.SZ"]  # CAL[0] 的池
    assert _day8(CAL[0]) in calls["pool"]        # 区间前一日被按需取用
    assert calls["pool"].count(_day8(CAL[1])) == 1


def test_build_day_feed_offline_no_calendar_returns_none_feed(monkeypatch):
    """⑫ 无交易日历也无指数日线 → 恒 None 的 feed(等于不注入, 静态参数照常生效)。"""
    import backtest.cli as cli
    monkeypatch.setattr(cli, "_get_zt_index", lambda: {})
    monkeypatch.setattr(cli, "_index_daily", lambda *a, **k: [])
    feed = cli.build_day_feed(CAL[0], CAL[3])
    assert feed(CAL[0]) is None and feed(CAL[3]) is None


def test_build_day_feed_single_day_io_failure_does_not_kill_run(monkeypatch):
    """⑫ 惰性取数把 IO 挪进了 callable → 单日失败不能炸整段回测
    (该日退回静态参数; 失败不缓存, 下次请求可重试)。"""
    cli, calls = _lazy_io(monkeypatch)

    def boom(codes):
        raise RuntimeError("QMT 挂了")

    monkeypatch.setattr(cli, "_batch_klines", boom)
    feed = cli.build_day_feed(CAL[2], CAL[3])
    assert feed(CAL[3]) is None           # 当天降级, 不抛
    assert feed(CAL[3]) is None           # 仍可再试(失败不入缓存)
    assert feed(CAL[2]) is None


# ---------------- ⑬ 日K契约塌陷披露(无 volume 的兜底行) ----------------

def test_close_only_kline_contract_is_disclosed(monkeypatch):
    """⑬ 老缓存兜底只有 (date, close) → volume 缺失, 必须在 data_notes 里点名。

    2026-09-19 S2 诊断附带发现: `qmt_kline_feed` 的 close-only 兜底**非空** ⇒
    联网 OHLCV 回退永不被走到, F5/Y3/S2/S3/M6/M7 对该股静默失效。若不披露,
    读者只会看到"某因子命中率骤降"却找不到原因。
    """
    cli, calls = _lazy_io(monkeypatch)

    def fake_klines(codes):
        return {c: (_kline2(KLINE_CLOSES, start=CAL[0]) if c == "600000.SH"
                    else _kline6(KLINE_CLOSES, start=CAL[0]))
                for c in codes}

    monkeypatch.setattr(cli, "_batch_klines", fake_klines)
    feed = cli.build_day_feed(CAL[0], CAL[3])
    feed(CAL[3])
    notes = list(getattr(feed, "data_notes", []) or [])
    hit = [n for n in notes if n.startswith("日K契约: ")]
    assert len(hit) == 1, notes
    assert "只有收盘价" in hit[0] and "F5/Y3/S2/S3/M6/M7" in hit[0]
    num, den = hit[0].split()[1].split("/")      # 形如 "2/5"
    assert 1 <= int(num) <= int(den), hit[0]


def test_kline_note_absent_when_all_rows_have_volume(monkeypatch):
    """⑬ 全是 6 元组(有 volume) → 不许出现这条说明(避免噪音)。"""
    cli, calls = _lazy_io(monkeypatch)
    feed = cli.build_day_feed(CAL[0], CAL[3])
    feed(CAL[3])
    notes = list(getattr(feed, "data_notes", []) or [])
    assert not [n for n in notes if n.startswith("日K契约: ")], notes
